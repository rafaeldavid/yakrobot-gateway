"""Serve a robot's descriptor JSON live: ``GET /{robot}/descriptor``.

The same document ``yakrobot-py export`` writes to disk, built on demand from the
plugin's ``metadata()``. It exists so a browser can read a robot's descriptor straight
off the gateway — the registration page at ``register.yakrobot.com`` pastes a tunnel URL,
reads this route, and puts the result on-chain. That makes this the one cross-origin
surface the gateway has, hence the CORS headers below.

Nothing here is chain code: the gateway builds and serves the descriptor, and never
signs or broadcasts anything.
"""

import logging
import os

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from core.plugin import RobotPlugin

logger = logging.getLogger(__name__)

# Public, read-only, no credentials — so ``*`` is the right origin here, and scoping the
# headers to these two routes leaves the teleop console and the reservation surface with
# no cross-origin access at all.
#
# The OPTIONS handlers are not optional. Free-tier ngrok serves a browser interstitial
# instead of the response unless the request carries ``ngrok-skip-browser-warning``, and
# that custom header makes the request non-simple — so the browser preflights. Without
# OPTIONS the flow dead-ends on exactly the tunnels most operators paste.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "ngrok-skip-browser-warning",
}

_DOMAIN_ENV_VARS = ("NGROK_DOMAIN", "CLOUDFLARE_DOMAIN")


def _public_domain(request: HTTPConnection) -> str:
    """Resolve the public host the descriptor's endpoints should point at.

    Same precedence as the ``export`` CLI (``$NGROK_DOMAIN`` then ``$CLOUDFLARE_DOMAIN``),
    then the request's own ``Host`` header. The Host fallback is what lets a gateway be
    registered with no env change at all: behind a tunnel the header already carries the
    public name the caller reached us by.

    Read per-request rather than captured at startup, so a tunnel that comes up after the
    gateway does is picked up without a restart. Returns ``""`` if nothing resolves.

    Takes ``HTTPConnection`` rather than ``Request`` — both ``Request`` and
    ``WebSocket`` subclass it and expose the same ``.headers`` — so ``ws_proxy``'s
    capability verification (paid-teleop-execution.md §0.5) can reuse this exact
    resolution for a ``WebSocket`` instead of inventing a second one.
    """
    for var in _DOMAIN_ENV_VARS:
        value = os.getenv(var, "").strip()
        if value:
            return value
    return (request.headers.get("host") or "").strip()


def register_descriptor_route(app: FastAPI, plugins: dict[str, RobotPlugin]) -> None:
    """Add ``GET /{robot}/descriptor`` (and its preflight).

    **Must be called before the per-robot MCP apps are mounted**, for the same reason as
    the console and the WebSocket proxy: ``app.mount("/{name}")`` claims every path
    beneath its prefix, so a route registered afterwards never sees a request.
    """
    for name in sorted(plugins):
        logger.info("descriptor: /%s/descriptor", name)

    @app.options("/{robot}/descriptor")
    async def robot_descriptor_preflight(robot: str) -> Response:
        return Response(status_code=204, headers=CORS_HEADERS)

    @app.get("/{robot}/descriptor")
    async def robot_descriptor(robot: str, request: Request) -> JSONResponse:
        if robot not in plugins:
            raise HTTPException(404, f"unknown robot {robot!r}", headers=CORS_HEADERS)

        # Imported here, not at module scope, so serving a robot still does not require
        # the contract package — core.descriptor pulls in yakrobot_descriptor, and the
        # `export` extra stays optional for a gateway that only drives hardware.
        try:
            from core.descriptor import build_descriptor
        except ImportError as exc:
            # Report the import that actually failed. "not installed" is only one
            # reason this raises, and stating it as the reason sends an operator
            # to re-run a sync that already succeeded: an editable install whose
            # .pth is not on sys.path, a partially-built venv, or a broken
            # transitive dependency all land here and all look identical
            # otherwise. The underlying message is the whole diagnosis.
            logger.warning("descriptor import failed: %s", exc)
            raise HTTPException(
                501,
                f"descriptor export unavailable: {exc}. "
                f"If yakrobot-descriptor is not installed, run "
                f"`uv sync --extra export`.",
                headers=CORS_HEADERS,
            ) from None

        domain = _public_domain(request)
        if not domain:
            # build_descriptor would happily emit "" for both endpoints. A descriptor
            # with no reachable MCP URL is worse than no descriptor: it registers
            # cleanly and resolves to nothing.
            raise HTTPException(
                503,
                "no public domain resolved — set NGROK_DOMAIN or CLOUDFLARE_DOMAIN, "
                "or reach this gateway by its public hostname",
                headers=CORS_HEADERS,
            )

        descriptor = build_descriptor(plugins[robot], domain)
        return JSONResponse(descriptor.model_dump(mode="json"), headers=CORS_HEADERS)
