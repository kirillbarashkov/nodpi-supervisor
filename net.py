#!/usr/bin/env python3
"""Command line for the NoDPI supervisor.

    net.py status                 what is running, what mode, last logs
    net.py start | stop | restart
    net.py mode nodpi|direct      switch traffic mode
    net.py logs [access|error|switch|boot]
    net.py test                   CONNECT test through the current mode
    net.py routes                 show per-host routes
    net.py check                  environment self-check

Exit code is 0 on success and 1 when something is wrong, so it can be used in
scripts and cron without parsing output.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import lifecycle
from lifecycle import Stack


def fmt_uptime(seconds: int) -> str:
    if not seconds:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def cmd_status(stack: Stack) -> int:
    data = stack.call("/status")
    if not data.get("ok"):
        print("stack: NOT RUNNING")
        print(f"  reason: {data.get('error')}")
        print(f"  desired: {'up' if stack.desired() else 'down'}")
        print("  start it with: net.py start")
        return 1

    print(f"stack: running | mode: {data.get('mode')} | connections: {data.get('connections', 0)}")
    print()
    print(f"  {'component':<12} {'port':<6} {'pid':<8} {'state':<10} {'uptime':<8} restarts")
    for comp in data.get("components", []):
        print(f"  {comp['name']:<12} {comp['port']:<6} {str(comp['pid'] or '—'):<8} "
              f"{comp['state']:<10} {fmt_uptime(comp.get('uptime_s', 0)):<8} {comp.get('restarts', 0)}")
        if comp.get("last_error"):
            print(f"    └ {comp['last_error']}")

    routes = data.get("routes") or {}
    if routes:
        print()
        print("  routes:")
        for host, upstream in sorted(routes.items()):
            print(f"    {host} -> {upstream}")
    return 0


def cmd_logs(stack: Stack, name: str, lines: int) -> int:
    names = {
        "access": "nodpi-access",
        "error": "nodpi-error",
        "switch": "switch",
        "boot": "nodpi-boot",
    }
    key = names.get(name, name)
    result = stack.call(f"/logs?name={key}&lines={lines}")
    if not result.get("ok"):
        print(f"cannot read logs: {result.get('error')}")
        return 1
    rows = result.get("lines", [])
    if not rows:
        print("(empty)")
        return 0
    for row in rows:
        print(row)
    return 0


def cmd_mode(stack: Stack, mode: str) -> int:
    if mode not in ("nodpi", "direct"):
        print("mode must be nodpi or direct")
        return 2
    result = stack.call(f"/mode/{mode}", "POST")
    if not result.get("ok"):
        print(f"failed: {result.get('error')}")
        return 1
    print(f"mode: {result.get('mode')}")
    return 0


def cmd_test(stack: Stack, host: str) -> int:
    result = stack.call(f"/test?host={host}")
    if result.get("ok"):
        print(f"ok: {host} via {result.get('mode')} in {result.get('ms')} ms")
        return 0
    print(f"failed: {result.get('error')} (mode {result.get('mode')})")
    return 1


def cmd_routes(stack: Stack) -> int:
    result = stack.call("/routes")
    if not result.get("ok"):
        print(f"cannot read routes: {result.get('error')}")
        return 1
    routes = result.get("routes") or {}
    if not routes:
        print("no per-host routes (everything follows the mode)")
        return 0
    for host, upstream in sorted(routes.items()):
        print(f"{host} -> {upstream}")
    return 0


def cmd_check(stack: Stack) -> int:
    """Environment self-check: what is missing, before anything is started."""
    problems = []

    print(f"python      {sys.version.split()[0]}")
    if sys.version_info < (3, 9):
        problems.append("python 3.9+ is required")

    print(f"home        {stack.home}")
    for name in ("switch.py", "child.py", "lifecycle.py"):
        path = os.path.join(stack.home, name)
        ok = os.path.exists(path)
        print(f"{'found':<11} {name}" if ok else f"{'MISSING':<11} {name}")
        if not ok:
            problems.append(f"{name} is missing")

    try:
        import switch as switch_mod          # no side effects: constants only
        nodpi = switch_mod.NODPI_EXE
        exists = os.path.exists(nodpi)
        print(f"{'found' if exists else 'MISSING':<11} NoDPI: {nodpi}")
        if not exists:
            problems.append("NoDPI executable not found — pass --nodpi-exe or copy it to ./nodpi/")
    except Exception as exc:                  # noqa: BLE001 - report, do not crash
        print(f"{'error':<11} cannot inspect switch.py: {exc}")
        problems.append("switch.py cannot be imported")

    for label, port in (("proxy port", stack.proxy_port), ("control port", stack.control_port)):
        busy = lifecycle._port_open(port)
        holder = "in use" if busy else "free"
        print(f"{holder:<11} {label} {port}")
        if busy and not stack.call("/health").get("ok") and port == stack.proxy_port:
            problems.append(f"port {port} is taken by another process (not our switch)")

    health = stack.call("/health")
    print(f"{'alive' if health.get('ok') else 'down':<11} control API on {stack.control_base()}")
    data = stack.call("/status")
    if data.get("ok"):
        nodpi_state = next((c for c in data.get("components", []) if c.get("role") != "switch"), {})
        print(f"{nodpi_state.get('state', '?'):<11} NoDPI on port {nodpi_state.get('port', '?')}")

    routes_path = os.path.join(stack.home, "routes.json")
    if os.path.exists(routes_path):
        try:
            with open(routes_path, "r", encoding="utf-8") as f:
                json.load(f)
            print(f"{'ok':<11} routes.json")
        except ValueError as exc:
            print(f"{'invalid':<11} routes.json: {exc}")
            problems.append("routes.json is not valid JSON")
    else:
        print(f"{'absent':<11} routes.json (optional)")

    print()
    if problems:
        print(f"FAILED: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("OK: everything needed is in place")
    return 0


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description="NoDPI supervisor CLI")
    parser.add_argument("command", nargs="?", default="status",
                        choices=["status", "start", "stop", "restart", "mode",
                                 "logs", "test", "routes", "check"])
    parser.add_argument("value", nargs="?", default=None,
                        help="mode name for `mode`, log name for `logs`, host for `test`")
    parser.add_argument("--lines", type=int, default=40, help="how many log lines")
    parser.add_argument("--home", default=None, help="directory with switch.py")
    args = parser.parse_args(argv[1:])

    stack = Stack(home=args.home)

    if args.command == "status":
        return cmd_status(stack)
    if args.command == "start":
        result = stack.start()
        print("started" if result.get("ok") else f"failed: {result.get('error')}")
        return 0 if result.get("ok") else 1
    if args.command == "stop":
        result = stack.stop()
        print("stopped" if result.get("ok") else f"failed: {result.get('error')}")
        return 0 if result.get("ok") else 1
    if args.command == "restart":
        result = stack.restart()
        print("restarted" if result.get("ok") else f"failed: {result.get('error')}")
        return 0 if result.get("ok") else 1
    if args.command == "mode":
        return cmd_mode(stack, args.value or "")
    if args.command == "logs":
        return cmd_logs(stack, args.value or "access", args.lines)
    if args.command == "test":
        return cmd_test(stack, args.value or "example.com")
    if args.command == "routes":
        return cmd_routes(stack)
    if args.command == "check":
        return cmd_check(stack)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
