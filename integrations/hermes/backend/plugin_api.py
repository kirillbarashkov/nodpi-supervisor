"""Hermes panel backend for nodpi-supervisor.

A thin HTTP adapter: every route delegates to ``lifecycle.Stack``, which is the
same code the CLI uses. There is no second implementation of start/stop here, so
the panel and the command line cannot disagree about what the stack is doing.

Install: copy this file to ``<hermes>/plugins/nodpi-supervisor/dashboard/plugin_api.py``
and keep ``NODPI_SUPERVISOR_HOME`` pointing at the core checkout (or place the
core files next to this one).

Routes (mounted at /api/plugins/nodpi-supervisor/):
  GET  /overview              state + components + logs in one call
  POST /stack/start|stop|restart
  POST /nodpi/<start|stop|restart>
  POST /mode/<direct|nodpi>
  GET  /test?host=...
  GET  /logs?name=...&lines=N
  GET  /routes
"""

from __future__ import annotations

import os
import sys

from fastapi import APIRouter

# The core lives elsewhere (a checkout, not a site-package). Find it via env or
# by walking one level up from this file, which covers "core copied next to the
# plugin" as well.
CORE = os.environ.get("NODPI_SUPERVISOR_HOME") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if CORE not in sys.path:
    sys.path.insert(0, CORE)

import lifecycle  # noqa: E402  (after sys.path surgery, on purpose)

router = APIRouter()
stack = lifecycle.Stack(home=os.environ.get("NODPI_SWITCH_HOME", CORE))

# Keep the switch alive while the app is open; "Stop" writes desired=false and
# the watchdog leaves it alone.
stack.start_watchdog()


@router.get("/overview")
async def overview(lines: int = 40) -> dict:
    return stack.overview(lines=max(1, min(300, lines)))


@router.post("/stack/start")
async def stack_start() -> dict:
    result = stack.start()
    result["overview"] = stack.overview()
    return result


@router.post("/stack/stop")
async def stack_stop() -> dict:
    result = stack.stop()
    result["overview"] = stack.overview()
    return result


@router.post("/stack/restart")
async def stack_restart() -> dict:
    result = stack.restart()
    result["overview"] = stack.overview()
    return result


@router.post("/nodpi/{action}")
async def nodpi_action(action: str) -> dict:
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": "action must be start, stop or restart"}
    result = stack.call(f"/nodpi/{action}", "POST")
    result["overview"] = stack.overview()
    return result


@router.post("/mode/{mode}")
async def set_mode(mode: str) -> dict:
    if mode not in ("direct", "nodpi"):
        return {"ok": False, "error": "mode must be direct or nodpi"}
    return stack.call(f"/mode/{mode}", "POST")


@router.get("/test")
async def test(host: str = "example.com") -> dict:
    return stack.call(f"/test?host={host}")


@router.get("/logs")
async def logs(name: str = "nodpi-access", lines: int = 60) -> dict:
    if name not in ("switch", "nodpi-access", "nodpi-error", "nodpi-boot"):
        return {"ok": False, "error": "unknown log name"}
    return stack.call(f"/logs?name={name}&lines={max(1, min(500, lines))}")


@router.get("/routes")
async def routes() -> dict:
    return stack.call("/routes")


@router.get("/health")
async def health() -> dict:
    return {"ok": stack.call("/health").get("ok", False),
            "control": stack.control_base()}
