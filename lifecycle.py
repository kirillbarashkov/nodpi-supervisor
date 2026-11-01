#!/usr/bin/env python3
"""Process lifecycle for the switch: start, stop, watch, and read its state.

Nothing in here knows about any UI. The CLI (`net.py`) and the optional Hermes
panel both sit on top of this module, so they cannot drift apart.

    stack = Stack()
    stack.start()          # make sure switch + NoDPI are up
    stack.overview()       # one dict with state, components and logs
    stack.stop()           # stop NoDPI and the switch
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# Everything is overridable so the project can live anywhere.
HOME = os.environ.get("NODPI_SWITCH_HOME", HERE)
SWITCH_SCRIPT = os.path.join(HOME, "switch.py")
RUNTIME_FILE = os.path.join(HOME, "runtime.json")
DESIRED_FILE = os.path.join(HOME, "desired.json")
STATE_FILE = os.path.join(HOME, "state.json")
ROUTES_FILE = os.path.join(HOME, "routes.json")
LOG_DIR = os.path.join(HOME, "logs")

PROXY_PORT = int(os.environ.get("SWITCH_PORT", "8881"))
CONTROL_PORT = int(os.environ.get("SWITCH_CONTROL_PORT", "8891"))
TIMEOUT_S = float(os.environ.get("SWITCH_TIMEOUT", "8"))


def _port_open(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_until(predicate, timeout: float, step: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _tail(path: str, lines: int) -> list:
    """Last `lines` lines of a text file, tolerant to any encoding."""
    if lines <= 0:
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 256 * 1024:
                f.seek(size - 256 * 1024)
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    return [ln.rstrip("\r") for ln in text.splitlines()][-lines:]


class Stack:
    """One switch process, its NoDPI child, and the files they own."""

    def __init__(self, home: str | None = None,
                 proxy_port: int | None = None,
                 control_port: int | None = None):
        self.home = home or HOME
        self.proxy_port = proxy_port or PROXY_PORT
        self.control_port = control_port or CONTROL_PORT
        self.script = os.path.join(self.home, "switch.py")
        self.runtime_file = os.path.join(self.home, "runtime.json")
        self.desired_file = os.path.join(self.home, "desired.json")
        self.log_dir = os.path.join(self.home, "logs")
        self._spawn_lock = threading.Lock()
        self._watchdog = None
        # The proxy is usually configured machine-wide (HTTP_PROXY points at the
        # switch itself), so a plain urllib call to loopback would be routed
        # through the proxy and deadlock. Bypass proxies for control calls.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------ control API

    def control_base(self) -> str:
        """Port the running switch published, falling back to the default."""
        try:
            with open(self.runtime_file, "r", encoding="utf-8") as f:
                control = str(json.load(f).get("control") or "")
            port = int(control.rsplit(":", 1)[-1]) if control else 0
            if port:
                return f"http://127.0.0.1:{port}"
        except (OSError, ValueError, TypeError):
            pass
        return f"http://127.0.0.1:{self.control_port}"

    def call(self, path: str, method: str = "GET") -> dict:
        """Call the switch; never raises, always returns a dict with `ok`."""
        default = f"http://127.0.0.1:{self.control_port}"
        bases = [self.control_base()]
        if bases[0] != default:
            bases.append(default)

        last_error = None
        for base in bases:
            try:
                req = urllib.request.Request(base.rstrip("/") + path, method=method)
                with self._opener.open(req, timeout=TIMEOUT_S) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                data = json.loads(raw)
                if isinstance(data, dict):
                    data.setdefault("ok", True)
                    data["control"] = base
                    return data
                return {"ok": True, "data": data}
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                try:
                    parsed = json.loads(body)
                except (ValueError, TypeError):
                    parsed = {"error": body or str(exc)}
                parsed.setdefault("ok", False)
                return parsed
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = (base, exc)

        base, exc = last_error
        return {"ok": False, "reachable": False,
                "error": f"switch is not answering on {base}: {exc}"}

    # ------------------------------------------------------------ intent

    def desired(self) -> bool:
        """Should the stack be running? Set by start/stop, survives restarts."""
        try:
            with open(self.desired_file, "r", encoding="utf-8") as f:
                return bool(json.load(f).get("stack"))
        except (OSError, ValueError, TypeError):
            return True

    def set_desired(self, value: bool) -> None:
        try:
            os.makedirs(self.home, exist_ok=True)
            tmp = self.desired_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"stack": bool(value),
                           "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
            os.replace(tmp, self.desired_file)
        except OSError:
            pass

    # ------------------------------------------------------------ lifecycle

    def spawn_switch(self) -> dict:
        """Launch switch.py detached, so closing the caller does not kill the network."""
        with self._spawn_lock:
            if self.call("/health").get("ok"):
                return {"ok": True, "started": False, "note": "switch is already running"}
            if not os.path.exists(self.script):
                return {"ok": False, "error": f"switch.py not found at {self.script}"}
            if _port_open(self.proxy_port):
                return {"ok": False,
                        "error": f"port {self.proxy_port} is held by another process"}

            os.makedirs(self.log_dir, exist_ok=True)
            flags = 0
            if os.name == "nt":
                flags = (subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP
                         | subprocess.CREATE_NO_WINDOW)
            try:
                subprocess.Popen(
                    [sys.executable, self.script,
                     "--port", str(self.proxy_port),
                     "--control-port", str(self.control_port)],
                    cwd=self.home,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    close_fds=True,
                )
            except OSError as exc:
                return {"ok": False, "error": f"cannot start the switch: {exc}"}

            if _wait_until(lambda: self.call("/health").get("ok"), timeout=15):
                return {"ok": True, "started": True}
            return {"ok": False,
                    "error": "switch did not come up in 15 s — see logs/switch.log"}

    def stop(self) -> dict:
        self.set_desired(False)
        self.call("/shutdown", "POST")
        gone = _wait_until(
            lambda: not _port_open(self.proxy_port) and not _port_open(self.control_port),
            timeout=10,
        )
        return {"ok": gone, "stopped": gone,
                "error": None if gone else
                f"ports {self.proxy_port}/{self.control_port} are still busy"}

    def start(self) -> dict:
        self.set_desired(True)
        spawned = self.spawn_switch()
        if not spawned.get("ok"):
            return spawned
        nodpi = self.call("/nodpi/start", "POST")
        return {"ok": bool(nodpi.get("ok")), "spawned": spawned,
                "nodpi": nodpi.get("component"),
                "error": None if nodpi.get("ok") else nodpi.get("error", "NoDPI did not start")}

    def restart(self) -> dict:
        self.stop()
        _wait_until(lambda: not _port_open(self.proxy_port), timeout=5)
        return self.start()

    # ------------------------------------------------------------ reading

    def status(self) -> dict:
        return self.call("/status")

    def logs(self, name: str = "nodpi-access", lines: int = 40) -> list:
        return self.call(f"/logs?name={name}&lines={lines}").get("lines", [])

    def routes(self) -> dict:
        return self.call("/routes").get("routes", {})

    def overview(self, lines: int = 40) -> dict:
        """Everything a UI needs, in one call, with no invented state."""
        data = self.call("/status")
        if not data.get("ok"):
            return {
                "switch_up": False,
                "desired": self.desired(),
                "mode": "—",
                "proxy": f"127.0.0.1:{self.proxy_port}",
                "connections": 0,
                "by_mode": {"direct": 0, "nodpi": 0},
                "components": [],
                "logs": {"switch": [], "access": [], "error": []},
                "routes": {},
                "error": data.get("error", "switch is not answering"),
            }
        return {
            "switch_up": True,
            "desired": self.desired(),
            "mode": data.get("mode", "—"),
            "upstream": data.get("upstream", "—"),
            "proxy": data.get("proxy"),
            "control": data.get("control"),
            "connections": data.get("connections", 0),
            "by_mode": data.get("by_mode", {}),
            "started": data.get("started", "—"),
            "components": data.get("components", []),
            "logs": {
                "switch": self.logs("switch", lines),
                "access": self.logs("nodpi-access", lines),
                "error": self.logs("nodpi-error", lines),
            },
            "routes": data.get("routes", {}),
            "error": None,
        }

    # ------------------------------------------------------------ watchdog

    def start_watchdog(self, interval: float = 5.0) -> threading.Thread:
        """Keep the switch alive while this process lives (unless stopped)."""
        if self._watchdog and self._watchdog.is_alive():
            return self._watchdog

        def loop() -> None:
            time.sleep(interval)
            while True:
                try:
                    if self.desired() and not _port_open(self.control_port):
                        self.spawn_switch()
                except Exception:
                    pass
                time.sleep(interval)

        self._watchdog = threading.Thread(target=loop, name="switch-watchdog", daemon=True)
        self._watchdog.start()
        return self._watchdog
