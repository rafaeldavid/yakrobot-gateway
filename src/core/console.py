"""Serve the browser driving console.

The page lives here rather than on the robot for two reasons: the gateway is
the only host with a public URL, so serving it here means it reaches an
operator over the tunnel with no extra proxy hop; and one file then serves
every robot, instead of a copy per robot drifting on its own SD card.

It is one self-contained HTML file — inline CSS and JS, no build step, no
asset requests. Over a transatlantic link, extra round trips cost more than
bytes do.

The console is generic: it speaks the ``/ws/control`` and ``/ws/video``
protocol and resolves both socket URLs relative to its own path, so the same
file works for any robot mounted here and needs no per-robot templating.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from core.plugin import RobotPlugin

logger = logging.getLogger(__name__)

CONSOLE_HTML = Path(__file__).resolve().parent / "static" / "console.html"
TRACE_HTML = Path(__file__).resolve().parent / "static" / "console_trace.html"


def register_console(app: FastAPI, plugins: dict[str, RobotPlugin]) -> None:
    """Add ``GET /{robot}/ui`` and ``GET /{robot}/ui2``.

    ``/ui2`` is the camera-less console: it draws a dead-reckoned path and the
    sensor state instead of a video feed. Built for a car whose camera ribbon
    has failed, but useful on any robot with no camera at all.

    **Must be called before the per-robot MCP apps are mounted**, for the same
    reason as the WebSocket proxy: ``app.mount("/{name}")`` claims everything
    beneath its prefix, and a route registered afterwards never sees a request.
    """
    drivable = {name for name, p in plugins.items() if p.control_base_urls()}
    if not drivable:
        logger.info("console: no plugin exposes a control server; /ui not served")
        return
    for name in sorted(drivable):
        logger.info("console: /%s/ui  /%s/ui2", name, name)

    @app.get("/{robot}/ui2")
    async def robot_trace_console(robot: str):
        """Trace console — same socket, same auth, no video.

        Registered BEFORE /{robot}/ui purely for readability; both are plain
        routes and order between them does not matter. What does matter is that
        register_console() runs before the MCP apps are mounted (see above).
        """
        if robot not in drivable:
            raise HTTPException(status_code=404, detail=f"no console for {robot!r}")
        return FileResponse(
            TRACE_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/{robot}/ui")
    async def robot_console(robot: str):
        if robot not in drivable:
            # Either an unknown robot or one with no realtime socket to drive
            # (the Tello speaks UDP). Refuse rather than serve a console whose
            # sockets could never connect.
            raise HTTPException(status_code=404, detail=f"no console for {robot!r}")
        # Deliberately unauthenticated even when gateway tokens are set: the
        # page is inert markup, and every socket it opens is checked on
        # connect. Gating the HTML would only hide the page that explains the
        # operator needs a token.
        # no-cache = always revalidate, not "never store": the ETag FileResponse sets
        # still makes an unchanged page a tiny 304. Without it a browser applies
        # heuristic freshness from Last-Modified and can keep serving a stale console
        # for hours after a gateway upgrade — including on the payment redirect back.
        return FileResponse(
            CONSOLE_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"}
        )
