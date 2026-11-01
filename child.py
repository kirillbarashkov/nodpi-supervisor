#!/usr/bin/env python3
"""Supervised child process for the Hermes network switch.

The switch owns NoDPI: it starts it, watches its port, restarts it when it dies
and keeps its output in a log file. Everything it knows is exposed through the
switch control API, so the desktop page shows live state instead of a console
window nobody is looking at.

Why not a bare `start nodpi.exe`: a crashed child used to close its window and
take the error with it. Here stdout/stderr land in logs/<name>.log, the exit code
is recorded, and a crash loop stops after MAX_CRASHES_IN_WINDOW attempts instead
of spinning forever and hiding the real cause.
"""

from __future__ import annotations

import ctypes
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque

IS_WIN = os.name == "nt"

# NoDPI draws a live statistics panel: ANSI colours, cursor-up escapes and \r
# redraws. In a file that is 99% noise (13 MB of redrawn frames) and unreadable
# in a <pre>, so the panel is filtered out when the log is read, and the file is
# capped on every start.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
PANEL_RE = re.compile(r"^\s*[\u2500-\u257f]")   # ╭─ STATISTICS ─╮ / │ Conns …
BOXED_RE = re.compile(r"^[\s\u2500-\u257f]+$")

MAX_LOG_BYTES = 2 * 1024 * 1024      # rotate NoDPI output past this
KEEP_LOG_BYTES = 512 * 1024          # keep the tail instead of losing it

# A child that dies three times inside this window is considered broken, not
# flaky: we stop retrying and surface the last error instead of looping.
MAX_CRASHES_IN_WINDOW = 3
CRASH_WINDOW_S = 60.0
MAX_BACKOFF_S = 10.0
PORT_PROBE_TIMEOUT = 0.6


def pid_alive(pid: int) -> bool:
    """True if the process still exists. Works without psutil."""
    if not pid:
        return False
    if IS_WIN:
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def port_open(host: str, port: int, timeout: float = PORT_PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _tail(path: str, lines: int) -> list:
    """Readable tail: ANSI stripped, redraws collapsed, panel removed.

    The live statistics panel is redrawn several times a second. The child's
    output is already filtered on the way into the file, so this is a second
    line of defence plus a normaliser for anything written by older builds.
    """
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

    out = []
    prev = None
    for raw in re.split(r"[\r\n]+", data.decode("utf-8", "replace")):
        line = ANSI_RE.sub("", raw).rstrip()
        if not line.strip() or BOXED_RE.match(line) or PANEL_RE.match(line):
            continue
        if line == prev:
            continue
        out.append(line)
        prev = line
    return out[-lines:]


def clean_stream_chunk(text: str) -> list:
    """Turn a raw chunk of child output into the lines worth keeping.

    NoDPI animates its statistics panel with \\r and cursor-up escapes, so the
    same visible line arrives many times per second. Only the segment after the
    last control character is what a terminal would actually show.
    """
    kept = []
    for raw in re.split(r"[\r\n]+", ANSI_RE.sub("", text)):
        line = raw.rstrip()
        if not line.strip() or BOXED_RE.match(line) or PANEL_RE.match(line):
            continue
        kept.append(line)
    return kept


def rotate_log(path: str, max_bytes: int = MAX_LOG_BYTES,
               keep_bytes: int = KEEP_LOG_BYTES) -> None:
    """Keep the log bounded: past max_bytes, leave only the newest tail."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size <= max_bytes:
        return
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - keep_bytes))
            tail = f.read()
        # Do not start mid-line or mid-UTF-8-sequence after the cut.
        cut = tail.find(b"\n")
        if 0 <= cut < len(tail) - 1:
            tail = tail[cut + 1:]
        with open(path, "wb") as f:
            f.write(tail)
    except OSError:
        pass


def find_port_pid(port: int) -> int:
    """Who is listening on this port? Needed to show a real PID for an external
    process we adopted instead of started ourselves."""
    try:
        if IS_WIN:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                        and parts[3].upper() == "LISTENING":
                    return int(parts[4])
        else:
            out = subprocess.run(["ss", "-lptnH", f"sport = :{port}"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if "pid=" in line:
                    return int(line.split("pid=")[1].split(",")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 0


class Child:
    """One supervised process: start / stop / restart / health."""

    def __init__(self, name: str, exe: str, args: list, host: str, port: int,
                 log_path: str, cwd: str | None = None, extra_logs: list | None = None):
        self.name = name
        self.exe = exe
        self.args = list(args)
        self.host = host
        self.port = port
        self.log_path = log_path
        self.cwd = cwd
        self.extra_logs = list(extra_logs or [])

        self.proc: subprocess.Popen | None = None
        self.pid = 0
        self.started_at = 0.0
        self.restarts = 0
        self.last_error = ""
        self.last_exit_code = None
        self.desired = False          # user wants it running
        self.failed = False           # gave up after a crash loop
        self.adopted = False          # someone else (previous run) already serves the port
        self.crashes: deque = deque()  # timestamps of recent crashes
        self._next_attempt = 0.0
        self._port_misses = 0
        self.events: deque = deque(maxlen=100)

    # ---------- helpers ----------

    def _event(self, kind: str, text: str) -> None:
        self.events.append({
            "t": time.strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
        })

    def alive(self) -> bool:
        return pid_alive(self.pid)

    def serving(self) -> bool:
        return port_open(self.host, self.port)

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at) if self.started_at and self.alive() else 0

    def state(self) -> str:
        if self.failed:
            return "failed"
        if not self.desired:
            return "stopped"
        if self.adopted and self.serving():
            return "external"
        if self.alive():
            return "running" if self.serving() else "starting"
        return "restarting" if self._next_attempt else "down"

    def adopt_if_listening(self) -> bool:
        """A previous stack may still own the port; take it under observation
        instead of spawning a second copy that would fail to bind."""
        if self.alive() or not self.serving():
            return False
        self.adopted = True
        self.desired = True
        self.pid = find_port_pid(self.port)
        self.started_at = time.time()
        self._event("adopt", f"{self.name}: port {self.port} is already in use — watching it")
        return True

    # ---------- lifecycle ----------

    def start(self, reason: str = "manual") -> bool:
        if self.alive():
            self.desired = True
            self.failed = False
            return True

        # Stopping the switch does not always reach NoDPI: an instance from a
        # previous run keeps holding the port, the new one cannot bind and lives
        # on as a phantom the supervisor "watches". Adopting the port owner is
        # more honest than spawning a duplicate.
        if self.serving() and self.adopt_if_listening():
            self._event("adopt", f"{self.name}: port {self.port} is held by a previous instance ({reason})")
            return True

        if os.path.isabs(self.exe) and not os.path.exists(self.exe):
            self.last_error = f"executable not found: {self.exe}"
            self.failed = True
            self._event("error", self.last_error)
            return False

        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        rotate_log(self.log_path)
        for extra in self.extra_logs:
            rotate_log(extra)
        flags = 0
        if IS_WIN:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        try:
            logf = open(self.log_path, "ab", buffering=0)
        except OSError as exc:
            self.last_error = f"cannot open log {self.log_path}: {exc}"
            self.failed = True
            self._event("error", self.last_error)
            return False

        try:
            logf.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start ({reason}) "
                f"{self.exe} {' '.join(self.args)} ===\n".encode("utf-8")
            )
            # Force UTF-8 into the log regardless of the console code page, so
            # the file never mixes cp1251 and UTF-8 bytes.
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            self.proc = subprocess.Popen(
                [self.exe] + self.args,
                cwd=self.cwd or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=child_env,
                creationflags=flags,
                close_fds=True,
            )
            # Filter the child's output on the way into the file: NoDPI's live
            # statistics panel redraws itself constantly and would otherwise
            # fill the log with escape codes nobody can read.
            threading.Thread(
                target=self._pump_output,
                args=(self.proc, self.log_path),
                daemon=True,
                name="pump-" + self.name,
            ).start()
        except OSError as exc:
            self.last_error = f"cannot start {self.exe}: {exc}"
            self.failed = True
            self._event("error", self.last_error)
            return False
        finally:
            try:
                logf.close()
            except Exception:
                pass

        self.pid = self.proc.pid
        self.started_at = time.time()
        self.desired = True
        self.failed = False
        self._next_attempt = 0.0
        self._port_misses = 0
        self._event("start", f"{self.name} started, pid {self.pid} ({reason})")
        return True

    def _pump_output(self, proc, path: str) -> None:
        """Copy the child's output into the log, minus the animated panel."""
        try:
            out = open(path, "ab", buffering=0)
        except OSError:
            return
        try:
            buf = b""
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                buf += chunk
                cut = max(buf.rfind(b"\n"), buf.rfind(b"\r"))
                if cut >= 0:
                    data, buf = buf[:cut + 1], buf[cut + 1:]
                elif len(buf) > 65536:
                    data, buf = buf, b""      # runaway line: flush rather than leak
                else:
                    continue
                for line in clean_stream_chunk(data.decode("utf-8", "replace")):
                    try:
                        out.write((line + "\n").encode("utf-8"))
                    except OSError:
                        return
            if buf:
                for line in clean_stream_chunk(buf.decode("utf-8", "replace")):
                    out.write((line + "\n").encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                out.close()
            except OSError:
                pass

    def stop(self, reason: str = "manual") -> bool:
        self.desired = False
        self.failed = False
        pid = self.pid
        proc = self.proc
        self.pid = 0
        self.started_at = 0.0
        if not pid or not pid_alive(pid):
            self._event("stop", f"{self.name} is already stopped ({reason})")
            return True

        # PyInstaller --onefile runs a bootloader plus the real app as its child.
        # Killing only the pid we spawned leaves the app alive holding the port
        # (it then looks like an "external" process on the next start), so the
        # whole tree has to go.
        if IS_WIN:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(os.getpgid(pid), 15)
            except OSError:
                try:
                    if proc is not None:
                        proc.terminate()
                except OSError:
                    pass

        deadline = time.time() + 4.0
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.1)
        if pid_alive(pid):
            try:
                if proc is not None:
                    proc.kill()
                else:
                    os.kill(pid, 9)
            except OSError:
                pass
        self._event("stop", f"{self.name} stopped, pid {pid} ({reason})")
        return True

    def restart(self, reason: str = "manual") -> bool:
        self.restarts += 1
        self.stop("restart")
        time.sleep(0.2)
        return self.start(reason)

    # ---------- watchdog ----------

    def tick(self) -> None:
        """Called every few seconds: detect death, restart with backoff."""
        if not self.desired or self.failed:
            return
        now = time.time()

        if self.adopted:
            if self.serving():
                self._port_misses = 0
                return
            # The external process we were riding on is gone; now we own it.
            self.adopted = False
            self._port_misses = 0
            self._event("crash", f"{self.name}: the external process on {self.port} is gone, starting our own")
            self.start(reason="adopted gone")
            return

        if self.alive():
            if self.serving():
                self._port_misses = 0
                self.last_error = ""
            else:
                # Alive but not listening yet: give it a couple of cycles before
                # declaring it broken (PyInstaller bootloader needs a moment).
                self._port_misses += 1
                if self._port_misses >= 3:
                    self.last_error = f"port {self.port} is not accepting connections"
                    self._crash(now, "port closed", kill=True)
            return

        if now < self._next_attempt:
            return
        self._crash(now, f"process exited (code {self.last_exit_code})")

    def _crash(self, now: float, why: str, kill: bool = False) -> None:
        if kill:
            self.stop("watchdog")
            self.desired = True
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > CRASH_WINDOW_S:
            self.crashes.popleft()

        if self.proc is not None and not kill:
            self.last_exit_code = self.proc.poll()

        if len(self.crashes) >= MAX_CRASHES_IN_WINDOW:
            self.failed = True
            self.last_error = (
                f"{len(self.crashes)} crashes in {int(CRASH_WINDOW_S)} s; "
                f"last one: {why}"
            )
            self._event("failed", f"{self.name}: {self.last_error} — automatic restart disabled")
            self.pid = 0
            return

        backoff = min(MAX_BACKOFF_S, 2.0 ** (len(self.crashes) - 1))
        self.restarts += 1
        self._next_attempt = now + backoff
        self.last_error = why
        self._event("crash", f"{self.name} crashed ({why}), restarting in {backoff:.0f} s")
        self.pid = 0
        self.started_at = 0.0
        self.start(reason="auto")

    # ---------- reporting ----------

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "state": self.state(),
            "port": self.port,
            "pid": self.pid if self.alive() else 0,
            "uptime_s": self.uptime_s(),
            "restarts": self.restarts,
            "last_error": self.last_error,
            "desired": self.desired,
            "failed": self.failed,
            "adopted": self.adopted,
            "log": self.log_path,
            "events": list(self.events)[-12:],
        }

    def tail(self, lines: int = 60) -> list:
        return _tail(self.log_path, lines)
