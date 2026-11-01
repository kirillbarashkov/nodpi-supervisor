#!/usr/bin/env python3
"""
nodpi-supervisor — local CONNECT switch with a supervised NoDPI child.
=====================================================================

Many clients read ``HTTP_PROXY`` / ``HTTPS_PROXY`` once at startup, so you cannot
repoint them at NoDPI without restarting them. This process solves that: the
client keeps talking to one fixed address, and every new connection re-reads
``state.json`` to decide where it goes.

    client --(HTTP(S)_PROXY -> :8881)--> switch --+--> direct
                                                  +--> NoDPI (:8883) --> internet

The switch also owns NoDPI: it starts it, watches its port, restarts it when it
dies and keeps its logs. One process to start, and it knows why something is down.

Control API (HTTP, loopback only, no auth — keep it on 127.0.0.1):

    GET  /status                 state, components, counters
    POST /mode/direct|nodpi      switch traffic mode
    POST /toggle                 flip the mode
    GET  /test?host=...          CONNECT test through the current mode
    POST /nodpi/start|stop|restart
    GET  /logs?name=...&lines=N  switch | nodpi-access | nodpi-error | nodpi-boot
    GET  /routes | POST /routes  per-host upstream overrides
    GET  /health                 200 when alive
    POST /shutdown               stop the child and exit
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
import traceback

import child as childmod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
ROUTES_FILE = os.path.join(BASE_DIR, "routes.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# NoDPI is a child of the switch: the switch is the process the client cannot
# live without, so making it also responsible for NoDPI means one place that
# knows why something is down.
def _find_nodpi() -> str:
    """Locate the NoDPI executable: env first, then next to us, then ./nodpi/."""
    explicit = os.environ.get("NODPI_EXE")
    if explicit:
        return explicit
    names = ("nodpi_switch.exe", "nodpi_switch", "nodpi.exe", "nodpi")
    for folder in (BASE_DIR, os.path.join(BASE_DIR, "nodpi")):
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.exists(candidate):
                return candidate
    # Nothing found: name the expected path so the error message is actionable.
    return os.path.join(BASE_DIR, "nodpi", names[0])


NODPI_EXE = _find_nodpi()
NODPI_PORT = int(os.environ.get("NODPI_PORT", "8883"))
FRAG_SIZE = int(os.environ.get("NODPI_FRAGMENT_SIZE", "32"))
FRAG_OFFSET = int(os.environ.get("NODPI_FRAGMENT_OFFSET", "100"))

# NoDPI keeps its own logs: one line per closed connection, and a traceback per
# failure. Point it at files we own, otherwise there is nothing to show but the
# startup banner.
NODPI_BOOT_LOG = os.path.join(LOG_DIR, "nodpi.log")
NODPI_ACCESS_LOG = os.path.join(LOG_DIR, "nodpi-access.log")
NODPI_ERROR_LOG = os.path.join(LOG_DIR, "nodpi-error.log")


def build_nodpi_args(port: int, frag_size: int, frag_offset: int) -> list:
    """NoDPI command line. ``NODPI_ARGS`` overrides it wholesale if set."""
    override = os.environ.get("NODPI_ARGS")
    if override:
        return override.split()
    return [
        "--port", str(port),
        "--fragment-size", str(frag_size),
        "--fragment-offset", str(frag_offset),
        "--no-disorder",
        "--no-blacklist",
        "--log-access", NODPI_ACCESS_LOG,
        "--log-error", NODPI_ERROR_LOG,
    ]


NODPI_ARGS = build_nodpi_args(NODPI_PORT, FRAG_SIZE, FRAG_OFFSET)

# Upstream is derived, never persisted: a stale port in state.json was the kind
# of bug where the switch silently talks to a different NoDPI than it started.
# Point at an external proxy with NODPI_UPSTREAM if you need to.
UPSTREAM = os.environ.get("NODPI_UPSTREAM") or f"http://127.0.0.1:{NODPI_PORT}"

DEFAULTS = {
    "mode": "nodpi",            # nodpi | direct
}

HEADER_LIMIT = 65536


def read_state() -> dict:
    """Mode from state.json, upstream from the running configuration."""
    state = {"mode": DEFAULTS["mode"], "upstream": UPSTREAM}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict) and "mode" in loaded:
            state["mode"] = loaded["mode"]
    except (OSError, ValueError):
        pass
    if state["mode"] not in ("direct", "nodpi"):
        state["mode"] = "direct"
    return state


def write_state(state: dict) -> None:
    state = {"mode": state.get("mode", DEFAULTS["mode"])}
    state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def read_routes() -> dict:
    """Host routes for hosts the default path cannot carry.

    ``routes.json`` maps a host (or a parent domain) to the upstream that should
    carry it: ``"http://127.0.0.1:8118"`` for an HTTP proxy, ``"direct"`` to skip
    the mode and dial the destination as-is. No file means no exceptions: the
    switch then behaves exactly as before.
    """
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    routes = {}
    for host, upstream in loaded.items():
        if not isinstance(host, str) or not isinstance(upstream, str):
            continue
        host = host.strip().lower().lstrip(".")
        upstream = upstream.strip()
        if host and upstream:
            routes[host] = upstream
    return routes


def write_routes(routes: dict) -> None:
    tmp = ROUTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, sort_keys=True)
    os.replace(tmp, ROUTES_FILE)


def route_for(host: str, routes: dict) -> str:
    """Upstream for *host* from the route table, or '' when it is not routed.

    Matching is by host and then by parent domain, so ``context7.com`` covers
    ``mcp.context7.com`` — the same shape NoDPI's blacklist uses.
    """
    if not routes:
        return ""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in routes:
            return routes[candidate]
    return ""


def write_runtime(info: dict) -> None:
    """Publish which ports this instance actually owns (control may shift)."""
    path = os.path.join(BASE_DIR, "runtime.json")
    info = dict(info)
    info["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"cannot write runtime.json: {exc}")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # The panel reads this file; stdout alone would be lost when the switch is
    # spawned detached by the plugin backend.
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "switch.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_hostport(value: str) -> tuple:
    """Parse '127.0.0.1:8883' or 'http://127.0.0.1:8883' into (host, port)."""
    value = value.strip()
    for prefix in ("http://", "https://", "socks5://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.rstrip("/")
    if value.count(":") == 1:
        host, port = value.split(":")
        return host, int(port)
    return value, 8080


async def read_headers(reader: asyncio.StreamReader) -> bytes:
    """Read an HTTP head block (request or response) up to the blank line."""
    try:
        return await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        return exc.partial or b""
    except asyncio.LimitOverrunError:
        return b""


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class Switch:
    def __init__(self, host: str, port: int, control_port: int):
        self.host = host
        self.port = port
        self.control_port = control_port
        self.started = time.strftime("%Y-%m-%d %H:%M:%S")
        self.connections = 0
        self.by_mode = {"direct": 0, "nodpi": 0}

        self.nodpi = childmod.Child(
            name="NoDPI",
            exe=NODPI_EXE,
            args=NODPI_ARGS,
            host="127.0.0.1",
            port=NODPI_PORT,
            log_path=NODPI_BOOT_LOG,
            cwd=os.path.dirname(NODPI_EXE),
            extra_logs=[NODPI_ACCESS_LOG, NODPI_ERROR_LOG],
        )
        self.log_path = os.path.join(LOG_DIR, "switch.log")

    def snapshot(self) -> dict:
        return {
            "name": "switch",
            "state": "running",
            "role": "switch",
            "port": self.port,
            "control_port": self.control_port,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self.booted_at) if hasattr(self, "booted_at") else 0,
            "restarts": 0,
            "last_error": "",
            "adopted": False,
            "log": self.log_path,
            "events": [],
        }

    async def monitor(self) -> None:
        """Watch the child every few seconds. Nothing else does."""
        ticks = 0
        while True:
            try:
                self.nodpi.tick()
                ticks += 1
                if ticks % 100 == 0:          # ~5 min: keep our own log bounded too
                    childmod.rotate_log(self.log_path)
            except Exception:
                log("monitor error: " + traceback.format_exc(limit=2))
            await asyncio.sleep(3)

    # ---------- business logic ----------

    async def open_tunnel(self, host: str, port: int, mode: str, upstream: str):
        """Return (reader, writer) to the destination, honouring the mode."""
        if mode == "nodpi":
            up_host, up_port = parse_hostport(upstream)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(up_host, up_port), timeout=10
            )
            request = (
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Proxy-Connection: Keep-Alive\r\n\r\n"
            )
            writer.write(request.encode())
            await writer.drain()
            head = await asyncio.wait_for(read_headers(reader), timeout=10)
            if not head.startswith(b"HTTP/1."):
                writer.close()
                raise ConnectionError("upstream proxy gave no HTTP response")
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if " 200" not in status_line:
                writer.close()
                raise ConnectionError(f"upstream refused: {status_line}")
            return reader, writer

        return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=15)

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            head = await asyncio.wait_for(read_headers(reader), timeout=30)
            if not head:
                writer.close()
                return

            lines = head.split(b"\r\n")
            try:
                method, target = lines[0].split(b" ")[:2]
            except ValueError:
                writer.close()
                return
            method = method.decode("latin-1")
            target = target.decode("latin-1")

            state = read_state()
            mode, upstream = state["mode"], state["upstream"]

            if method.upper() == "CONNECT":
                if ":" in target:
                    host, _, port_s = target.rpartition(":")
                    port = int(port_s)
                else:
                    host, port = target, 443
                # Route exceptions first (routes.json), then the mode as before:
                # a routed host that cannot be reached falls back instead of
                # taking the whole connection down.
                route = route_for(host, read_routes())
                attempts = [(mode, upstream)]
                if route:
                    attempts.insert(
                        0,
                        ("direct", upstream) if route.lower() == "direct" else ("nodpi", route),
                    )
                remote_reader = remote_writer = None
                used_route = False
                for index, (try_mode, try_upstream) in enumerate(attempts):
                    try:
                        remote_reader, remote_writer = await self.open_tunnel(
                            host, port, try_mode, try_upstream
                        )
                    except Exception as exc:
                        if index + 1 < len(attempts):
                            log(f"route miss {host}:{port} via {try_upstream} -> {exc}")
                            continue
                        log(f"FAIL {host}:{port} mode={try_mode} -> {exc}")
                        writer.write(
                            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"
                        )
                        await writer.drain()
                        writer.close()
                        return
                    mode, upstream = try_mode, try_upstream
                    used_route = bool(route) and index == 0
                    break

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                self.connections += 1
                self.by_mode[mode] = self.by_mode.get(mode, 0) + 1
                suffix = f" [route {route}]" if used_route else ""
                log(f"CONNECT {host}:{port} via {mode}{suffix}")
                await asyncio.gather(
                    pipe(reader, remote_writer),
                    pipe(remote_reader, writer),
                )
                return

            # Plain HTTP: forward the request line as-is in nodpi mode,
            # rewrite absolute-form to origin-form when going direct.
            if mode == "nodpi":
                up_host, up_port = parse_hostport(upstream)
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(up_host, up_port), timeout=10
                )
                remote_writer.write(head)
                await remote_writer.drain()
            else:
                if target.startswith("http://"):
                    rest = target[len("http://"):]
                    hostport, _, path = rest.partition("/")
                    path = "/" + path if path else "/"
                    host, _, port_s = hostport.partition(":")
                    port = int(port_s) if port_s else 80
                    rewritten = b" ".join(
                        [method.encode(), path.encode(), lines[0].split(b" ")[2]]
                    )
                    body = head.split(b"\r\n", 1)[1] if b"\r\n" in head else b""
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port), timeout=15
                    )
                    remote_writer.write(rewritten + b"\r\n" + body)
                    await remote_writer.drain()
                else:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return

            self.connections += 1
            await asyncio.gather(
                pipe(reader, remote_writer),
                pipe(remote_reader, writer),
            )
        except Exception:
            log("client error: " + traceback.format_exc(limit=2))
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self) -> None:
        server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )
        self.booted_at = time.time()
        childmod.rotate_log(self.log_path)
        log(f"switch listening on {self.host}:{self.port} "
            f"(control {self.host}:{self.control_port})")
        log(f"state: {read_state()}")

        # Take over NoDPI: ride the one already listening, otherwise start ours.
        if not self.nodpi.adopt_if_listening():
            self.nodpi.start(reason="switch boot")
        log(f"nodpi: {self.nodpi.snapshot()['state']} "
            f"(pid {self.nodpi.pid}, port {NODPI_PORT})")

        asyncio.create_task(self.monitor())
        async with server:
            await server.serve_forever()

    # ---------- control API ----------

    async def handle_control(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(read_headers(reader), timeout=10)
            if not head:
                writer.close()
                return
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = request_line.split(" ")
            method = parts[0].upper() if parts else "GET"
            if method == "OPTIONS":
                writer.write(
                    b"HTTP/1.1 204 No Content\r\n"
                    b"Access-Control-Allow-Origin: *\r\n"
                    b"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                    b"Access-Control-Allow-Headers: Content-Type\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            path = parts[1] if len(parts) > 1 else "/"
            # curl and browsers send an absolute URI when a proxy is configured
            for prefix in ("http://", "https://"):
                if path.startswith(prefix):
                    rest = path[len(prefix):]
                    tail = rest.partition("/")[2]
                    path = "/" + tail if tail else "/"
                    break
            status, payload = "200 OK", {}

            body = ""
            for header_line in head.split(b"\r\n")[1:]:
                if header_line.lower().startswith(b"content-length:"):
                    try:
                        length = int(header_line.split(b":", 1)[1].strip())
                    except ValueError:
                        length = 0
                    if 0 < length <= 65536:
                        body = (await asyncio.wait_for(reader.readexactly(length), timeout=5)).decode(
                            "utf-8", "replace"
                        )
                    break

            state = read_state()

            if path.startswith("/status") or path == "/":
                payload = {
                    "mode": state["mode"],
                    "upstream": state["upstream"],
                    "routes": read_routes(),
                    "proxy": f"{self.host}:{self.port}",
                    "control": f"{self.host}:{self.control_port}",
                    "connections": self.connections,
                    "by_mode": self.by_mode,
                    "started": self.started,
                    "components": [self.snapshot(), self.nodpi.snapshot()],
                }
            elif path.startswith("/routes"):
                if method == "POST":
                    try:
                        data = json.loads(body or "{}")
                    except ValueError as exc:
                        status = "400 Bad Request"
                        payload = {"ok": False, "error": f"bad JSON: {exc}"}
                    else:
                        routes = {}
                        if isinstance(data, dict):
                            for host, upstream in data.items():
                                if (isinstance(host, str) and isinstance(upstream, str)
                                        and host.strip() and upstream.strip()):
                                    routes[host.strip().lower().lstrip(".")] = upstream.strip()
                        write_routes(routes)
                        log(f"routes -> {routes}")
                        payload = {"ok": True, "routes": routes}
                else:
                    payload = {"ok": True, "routes": read_routes()}
            elif path.startswith("/nodpi/"):
                action = path.split("/nodpi/", 1)[1].split("?")[0]
                if action == "start":
                    ok = self.nodpi.start(reason="panel")
                elif action == "stop":
                    ok = self.nodpi.stop(reason="panel")
                elif action == "restart":
                    ok = self.nodpi.restart(reason="panel")
                else:
                    status = "400 Bad Request"
                    payload = {"ok": False, "error": "action must be start, stop or restart"}
                    action = ""
                    ok = False
                if action:
                    log(f"nodpi {action} -> {self.nodpi.state()}")
                    payload = {
                        "ok": bool(ok),
                        "action": action,
                        "component": self.nodpi.snapshot(),
                    }
            elif path.startswith("/logs"):
                name = "nodpi"
                if "name=" in path:
                    name = path.split("name=", 1)[1].split("&")[0]
                lines = 60
                if "lines=" in path:
                    try:
                        lines = max(1, min(500, int(path.split("lines=", 1)[1].split("&")[0])))
                    except ValueError:
                        pass
                sources = {
                    "switch": self.log_path,
                    "nodpi-boot": NODPI_BOOT_LOG,
                    "nodpi": NODPI_ACCESS_LOG,        # the useful one: one line per connection
                    "nodpi-access": NODPI_ACCESS_LOG,
                    "nodpi-error": NODPI_ERROR_LOG,
                }
                target = sources.get(name)
                if target is None:
                    status = "400 Bad Request"
                    payload = {
                        "ok": False,
                        "error": "name must be switch, nodpi-access or nodpi-error",
                    }
                else:
                    payload = {"name": name, "lines": childmod._tail(target, lines)}
            elif path.startswith("/mode/"):
                want = path.split("/mode/", 1)[1].split("?")[0]
                if want in ("direct", "nodpi"):
                    state["mode"] = want
                    write_state(state)
                    log(f"mode -> {want}")
                    payload = {"mode": want, "upstream": state["upstream"]}
                else:
                    status = "400 Bad Request"
                    payload = {"error": "mode must be direct or nodpi"}
            elif path.startswith("/toggle"):
                state["mode"] = "direct" if state["mode"] == "nodpi" else "nodpi"
                write_state(state)
                log(f"toggle -> {state['mode']}")
                payload = {"mode": state["mode"], "upstream": state["upstream"]}
            elif path.startswith("/test"):
                host = "opencode.ai"
                if "host=" in path:
                    host = path.split("host=", 1)[1].split("&")[0]
                port = 443
                if ":" in host:
                    host, _, port_s = host.rpartition(":")
                    port = int(port_s)
                t0 = time.time()
                try:
                    r, w = await self.open_tunnel(
                        host, port, state["mode"], state["upstream"]
                    )
                    w.close()
                    payload = {
                        "ok": True,
                        "mode": state["mode"],
                        "host": host,
                        "ms": round((time.time() - t0) * 1000),
                    }
                except Exception as exc:
                    payload = {
                        "ok": False,
                        "mode": state["mode"],
                        "host": host,
                        "ms": round((time.time() - t0) * 1000),
                        "error": str(exc),
                    }
            elif path.startswith("/health"):
                payload = {"ok": True}
            elif path.startswith("/shutdown"):
                payload = {"ok": True, "stopping": True}
                body = json.dumps(payload).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
                log("shutdown requested")
                try:
                    self.nodpi.stop(reason="switch shutdown")
                except Exception:
                    pass
                loop = asyncio.get_event_loop()
                loop.call_later(0.3, lambda: os._exit(0))
                return
            else:
                status = "404 Not Found"
                payload = {"error": "unknown endpoint"}

            body = json.dumps(payload).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except Exception:
            log("control error: " + traceback.format_exc(limit=2))
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def serve_control(self) -> None:
        # The control port can be squatted by a leftover instance (that is exactly
        # how a stale switch made the new one die silently). Walk forward until a
        # port is actually free, then publish the chosen one in runtime.json so the
        # UI backend can find us.
        server = None
        last_error = None
        for offset in range(0, 20):
            candidate = self.control_port + offset
            try:
                server = await asyncio.start_server(
                    self.handle_control, self.host, candidate
                )
            except OSError as exc:
                last_error = exc
                continue
            self.control_port = candidate
            break

        if server is None:
            raise OSError(
                f"no free control port in {self.control_port}..{self.control_port + 19}: {last_error}"
            )

        write_runtime({
            "proxy": f"{self.host}:{self.port}",
            "control": f"{self.host}:{self.control_port}",
            "pid": os.getpid(),
            "started": self.started,
        })
        log(f"control api on {self.host}:{self.control_port}")
        async with server:
            await server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local CONNECT switch that supervises NoDPI"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8881,
                        help="proxy port your client points at")
    parser.add_argument("--control-port", type=int, default=8891)
    parser.add_argument("--nodpi-exe", default=None,
                        help="path to the NoDPI executable (default: found next to this file)")
    parser.add_argument("--nodpi-port", type=int, default=None,
                        help="port NoDPI listens on (default: 8883)")
    parser.add_argument("--fragment-size", type=int, default=None,
                        help="TLS fragment size passed to NoDPI (default: 32)")
    parser.add_argument("--fragment-offset", type=int, default=None,
                        help="bytes kept intact before fragmentation starts (default: 100)")
    args = parser.parse_args()

    # CLI wins over the module defaults; Switch() is built below, so rebinding
    # the globals here is enough to steer the child.
    global NODPI_EXE, NODPI_PORT, NODPI_ARGS, UPSTREAM
    if args.nodpi_exe:
        NODPI_EXE = os.path.abspath(args.nodpi_exe)
    NODPI_PORT = args.nodpi_port or NODPI_PORT
    if not os.environ.get("NODPI_UPSTREAM"):
        UPSTREAM = f"http://127.0.0.1:{NODPI_PORT}"
    NODPI_ARGS = build_nodpi_args(
        NODPI_PORT,
        args.fragment_size if args.fragment_size is not None else FRAG_SIZE,
        args.fragment_offset if args.fragment_offset is not None else FRAG_OFFSET,
    )

    if not os.path.exists(STATE_FILE):
        write_state(dict(DEFAULTS))

    switch = Switch(args.host, args.port, args.control_port)

    async def run() -> None:
        await asyncio.gather(switch.serve(), switch.serve_control())

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log("stopped")
    except OSError as exc:
        log(f"cannot start: {exc}")
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, f"Switch failed to start: {exc}", "Network switch", 0x10
                )
            except Exception:
                pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
