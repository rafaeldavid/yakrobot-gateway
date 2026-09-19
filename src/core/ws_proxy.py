"""Reverse-proxy realtime WebSockets from the gateway through to a robot.

The gateway is the only host with a public URL — the tunnel terminates here
(``core/tunnel.py``), not on the robot, which sits on a private LAN with no
inbound ports. A browser driving the car therefore cannot open a socket to the
robot directly; it opens one to ``/{robot}/ws/<path>`` here and this module
carries it the last LAN hop.

**This is two sockets spliced, not a pass-through.** A WebSocket cannot be
forwarded the way an HTTP request can: the gateway terminates the browser's
socket, opens its own client socket to the robot, and pumps frames between
them until either end closes.

    browser --wss--> tunnel edge --ws--> gateway :8000
                                            |  accepts the Upgrade, then
                                            |  connects outward as a client
                                            v
                              ws://picar-finland-01.local:8080/ws/control

Three properties this proxy must preserve, all of them load-bearing for the
safety model that lives on the robot:

1. **No queueing.** The control protocol sends *absolute* velocity state, so a
   lost frame is harmless but a *late* one is not: a buffered frame delivered
   after a stop would re-apply a stale velocity. Every relay here is a direct
   ``await send`` with no intermediate queue, so backpressure propagates to the
   robot, which then simply emits fewer frames.
2. **Prompt close in both directions.** The robot's deadman timer is the
   backstop, but a half-open socket held after the browser vanishes delays the
   robot's own ``stop()``-on-disconnect. When either side ends, the other is
   torn down immediately.
3. **No *safety* state of its own.** Deadman, duty caps and the single-driver
   slot are enforced on the robot, where the hardware is — a gateway that
   believes it stopped a car it cannot reach is worse than one that never
   claimed to. This module adds none of them and must not start.

   This is narrower than "no state at all." **Admission state** — who is
   allowed to open a socket here — is already this module's job, decided
   with a static token set (below). A paid-teleop capability
   (paid-teleop-access.md §2) is the same decision with an expiry and a name
   attached: it is verified here, and the resulting hold on the robot is
   recorded in the *reservation registry* — the gateway's existing per-robot
   arbiter, shared with MCP agents — not invented fresh. A free reservation is the same
   decision again, minus any money or signature — an unpaid, gateway-local,
   restart-amnesiac ``free_leases`` dict stands in for the capability, driving the same
   registry hold, expiry and release machinery. A card-paid (Stripe) lease is the same
   decision once more: it is verified once against Stripe at confirm, then carried by a
   locally verified HMAC credential, and the gateway stores no commercial record. The
   brief reachability cache below is a
   fourth, narrower thing again: it remembers only that a connect just failed, which
   changes how quickly a refusal is returned, never what the robot is permitted to do.
"""

import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from core.capability import CapabilityError, Claims, normalize_host, verify
from core.descriptor_route import _public_domain
from core.plugin import RobotPlugin
from core.stripe_credential import (
    StripeClaims,
    derive_key,
    is_stripe_credential,
    mint as mint_stripe,
    verify as verify_stripe,
)

logger = logging.getLogger(__name__)

# JPEG video frames are the big ones — ~20-40 KB at 400x300, but a unit
# configured for a larger stream should not hit a wall in the proxy before it
# hits one on the robot. Generous, but still a bound: unset would remove the
# only guard against a runaway frame exhausting gateway memory.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# Fail fast when a robot is off or unreachable, so the browser gets a prompt
# refusal rather than hanging on a socket that will never carry anything.
CONNECT_TIMEOUT_S = 5.0

# How long one failed connect suppresses further attempts for that robot. A
# console whose robot is off retries two sockets on a timer, and without this
# every retry re-runs the whole candidate list and its connect timeouts. Short
# enough that a robot finishing its boot is picked up on the next retry.
OFFLINE_CACHE_S = 3.0

# How often a static-token /ws/control socket renews its reservation while open.
# The registry's default TTL (reservation.SESSION_TTL, 300s) would otherwise lapse
# mid-drive and hand the robot to an agent — paid-teleop-execution.md §0.5.
RESERVATION_RENEW_S = 60.0

# Read at call time, not inlined, purely so a test can monkeypatch it to something
# other than a real 60 seconds — a free lease's exp is entirely gateway-derived (no
# client-minted exp=now+2 trick like the paid path's expiry test uses), so exercising a
# real expiry here would otherwise mean sleeping out a full TELEOP_LEASE_MINUTES.
SECONDS_PER_LEASE_MINUTE = 60

# Stripe API version pinned on every call, so response shapes stay fixed (decision A.7).
STRIPE_API_VERSION = "2024-06-20"

# How long a Stripe REST call may take before the gateway gives up and shows the buyer a
# retry page. Mirrors LEDGER_RELEASE_TIMEOUT_S below.
STRIPE_TIMEOUT_S = 5.0

# Checkout Session ids are cs_(test|live)_…; validating this before building the Stripe
# URL blocks path injection into the retrieve endpoint.
_SESSION_ID_RE = re.compile(r"^cs_(test|live)_[A-Za-z0-9]+$")


def _stripe_headers(cfg) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg.secret_key}",
        "Stripe-Version": STRIPE_API_VERSION,
    }


def _public_origin(conn: HTTPConnection) -> str:
    """The browser-facing origin for success/cancel URLs.

    Tunnels terminate TLS, so ``request.url.scheme`` is wrong behind them — derive the
    scheme from the resolved public domain instead: http on loopback, https otherwise.
    """
    domain = _public_domain(conn)
    scheme = "http" if domain.startswith(("127.0.0.1", "localhost")) else "https"
    return f"{scheme}://{domain}"


def _stripe_page(status: int, robot: str, message: str) -> HTMLResponse:
    """A small inline page for the buyer — raw JSON is the wrong shape for a browser."""
    body = (
        '<!doctype html><html><head><meta charset="utf-8"><title>Card payment</title></head>'
        f"<body><p>{html.escape(message)}</p>"
        f'<p><a href="/{robot}/ui">Back to {html.escape(robot)}</a></p>'
        "</body></html>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _ws_url(base_url: str, path: str, query: str) -> str:
    """Turn a robot's HTTP base URL into the ws:// URL for one of its sockets."""
    parts = urlparse(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    prefix = parts.path.rstrip("/")
    url = f"{scheme}://{parts.netloc}{prefix}/ws/{path}"
    return f"{url}?{query}" if query else url


def _upstream_query(query: str, robot_token: str | None, gateway_auth: bool) -> str:
    """Build the query string for the upstream handshake.

    Browsers cannot set headers on a WebSocket handshake, so both the gateway
    and the robot take their credential as a ``token`` query parameter — the
    same name for two different secrets, one per leg of this proxy. Keeping
    them straight is the whole job here:

    * **Gateway auth on** — the client's ``token`` is the *gateway's*
      credential. It is consumed by the check above and stripped here: the
      robot would reject it anyway, and a browser credential has no business
      travelling further into the network than the boundary that validates it.
      The robot's own token is injected in its place, so the browser never
      holds it.
    * **Gateway auth off** (the deferred-auth state) — there is no gateway
      credential to consume, so a client-supplied ``token`` is meant for the
      robot and passes through untouched.
    """
    pairs = parse_qsl(query, keep_blank_values=True)
    if gateway_auth:
        pairs = [(k, v) for k, v in pairs if k != "token"]
    if robot_token and not any(k == "token" for k, _ in pairs):
        pairs.append(("token", robot_token))
    return urlencode(pairs)


def video_enabled() -> bool:
    """Whether ``/ws/video`` may be proxied at all.

    A hard switch for a metered or congested link: video is by far the most
    expensive thing crossing the tunnel, and this refuses it for every client
    regardless of what any browser chooses to do. Control keeps working — the
    car stays drivable, just blind.

    Set ``VIDEO_ENABLED=0`` (or false/no/off) to disable. Absent means enabled,
    so the default posture is unchanged.
    """
    raw = os.getenv("VIDEO_ENABLED", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _gateway_token_clients() -> dict[str, str]:
    """``token -> client_id`` for every admit source ``_make_auth`` would use.

    The teleop admit path (paid-teleop-execution.md §0.5, and its free-reservation
    sibling) must accept ``MCP_TOKENS_FILE`` as well as ``MCP_TOKENS``/
    ``MCP_BEARER_TOKEN``, and needs the ``client_id`` each token maps to — that is who
    the reservation is for — so this mirrors ``core.server._make_auth``'s precedence
    (file if set, else env) rather than a plain token set.
    """
    from core.server import _load_tokens, _parse_token_file

    token_file = os.getenv("MCP_TOKENS_FILE", "").strip()
    if token_file:
        try:
            raw = _parse_token_file(token_file)
        except OSError:
            return {}
        return {token: info["client_id"] for token, info in raw.items()}
    return {token: info["client_id"] for token, info in _load_tokens().items()}


async def _connect_upstream(candidates: list[str], path: str, query: str):
    """Open a client socket to the first candidate URL that accepts one.

    Candidates come from the plugin (mDNS name, IP, ...) precisely because
    neither naming scheme is reliable alone; see RobotPlugin.control_base_urls.
    """
    last_error: Exception | None = None
    for base_url in candidates:
        url = _ws_url(base_url, path, query)
        try:
            return await websockets.connect(
                url,
                open_timeout=CONNECT_TIMEOUT_S,
                max_size=MAX_FRAME_BYTES,
                # JPEG is already compressed; permessage-deflate would burn CPU
                # on both ends to make the frames very slightly larger.
                compression=None,
            ), url
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
            logger.info("ws proxy: %s unreachable (%s)", url, exc)
            last_error = exc
    raise ConnectionError(
        f"no reachable candidate among {candidates!r}"
    ) from last_error


async def _refuse(ws: WebSocket, code: int, reason: str) -> None:
    """Turn a browser away with a code it can actually read.

    Closing before ``accept()`` makes Starlette reject the handshake with HTTP
    403, and a browser reports *any* failed handshake as close code 1006 — the
    reason never arrives and every refusal looks alike. Accepting first costs
    one round trip and delivers the code intact, which is what lets the console
    tell "stop retrying" (1008, policy) from "try again shortly" (1013).

    Delivery is best-effort. Deciding to refuse can take seconds — long enough
    for the browser to give up, reload, or be closed — and neither the accept
    nor the close can reach a client that has already gone. That is not an
    error worth a traceback: a caller who left needs no explanation.
    """
    try:
        await ws.accept()
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


async def _pump_to_robot(ws: WebSocket, robot) -> None:
    """browser -> robot. Text (control JSON) and binary both pass through."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None:
            await robot.send(text)
            continue
        data = message.get("bytes")
        if data is not None:
            await robot.send(data)


async def _pump_to_browser(ws: WebSocket, robot) -> None:
    """robot -> browser. Video frames arrive binary, telemetry as text."""
    async for message in robot:
        try:
            if isinstance(message, str):
                await ws.send_text(message)
            else:
                await ws.send_bytes(message)
        except (RuntimeError, OSError):
            # The browser socket was closed underneath this relay — by a lease release or
            # expiry closing it from another task, or the peer vanishing mid-frame. The
            # session is over either way; a routine teardown, not a traceback.
            return


async def _close_at_expiry(ws: WebSocket, exp: int) -> None:
    """Close a capability-admitted browser socket at its lease's ``exp``.

    A handshake-time check alone would let one long-lived socket outlive its lease
    indefinitely — this is what makes expiry safe by construction rather than by the
    gateway doing anything clever: the robot's own deadman stops the car the moment this
    closes (paid-teleop-access.md §3.1).
    """
    delay = exp - int(time.time())
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await ws.close(code=1008, reason="lease expired")
    except Exception:
        # Already closed by the peer, or the socket is already gone — same reasoning as
        # every other best-effort close in this module.
        pass


async def _renew_reservation(registry, robot: str, client_id: str) -> None:
    """Keep a static-token /ws/control reservation alive for as long as the socket is
    open. See ``RESERVATION_RENEW_S``."""
    while True:
        await asyncio.sleep(RESERVATION_RENEW_S)
        registry.reserve(robot, client_id)


LEDGER_RELEASE_TIMEOUT_S = 5.0


async def _release_ledger(payments_url: str, token: str) -> bool:
    """Tell the payments service the lease ended early, so its ledger stops refusing the
    next sale until the original exp.

    The one outbound call this gateway makes to the payments service, and deliberately
    off the admission path (paid-teleop-access.md §4.1 rules out a gateway that depends
    on the service to let a driver in). Best-effort: the robot is already free here
    whatever happens, and the service verifies the token's signature itself rather than
    trusting this gateway. False on any failure.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=LEDGER_RELEASE_TIMEOUT_S) as client:
            r = await client.post(f"{payments_url}/v1/release", json={"token": token})
        return r.status_code == 200 and r.json().get("released") is True
    except Exception:
        logger.warning("ws proxy: could not release the lease with the payments service")
        return False


def _verify_lease(token: str, robot: str, conn: HTTPConnection, payments, now: int) -> Claims:
    """Verify a capability names ``robot`` and this gateway, and is not expired —
    the socket-admission checks of paid-teleop-execution.md §0.5, factored out so the
    lease-release endpoint below enforces exactly the same rules rather than a second,
    driftable copy of them. Raises ``CapabilityError`` with the exact §0.5 reason
    string on any failure.
    """
    claims = verify(token, payments.issuer, lease_minutes=payments.lease_minutes, now=now)
    if claims.robot != robot:
        raise CapabilityError("lease is for another robot")
    if normalize_host(claims.gateway) != normalize_host(_public_domain(conn)):
        raise CapabilityError("lease is for another gateway")
    if claims.exp <= now:
        raise CapabilityError("lease expired")
    return claims


@dataclass(frozen=True)
class FreeLease:
    """An unpaid, gateway-local reservation.

    Deliberately not a ``Claims``: no signature, no issuer, nothing to verify
    cryptographically. The only authority is this entry's presence in the
    ``free_leases`` dict — a restart forgets it, which is the safe direction (a
    forgotten free lease just means a re-reserve, never a stale one stealing the
    robot back from whoever is using it now).
    """

    lease_id: str
    robot: str
    exp: int


def _mint_free_token(lease_id: str, robot: str, iat: int, exp: int) -> str:
    """A token in the same two-part wire shape a paid capability uses, so the console's
    existing ``decodeCapabilityToken``/storage/countdown code needs no changes to handle
    it — but the second segment here is a random nonce, **never a signature**. Nothing
    about the token itself is trusted; ``_verify_free_lease`` below only ever trusts the
    server's own ``free_leases`` dict, keyed by this exact string.
    """
    payload = json.dumps(
        {"v": 1, "lease": lease_id, "robot": robot, "iat": iat, "exp": exp},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"{encoded}.{secrets.token_urlsafe(32)}"


def _verify_free_lease(
    token: str, robot: str, free_leases: dict[str, FreeLease], now: int
) -> FreeLease:
    """The free-lease equivalent of ``_verify_lease``: an exact lookup against this
    gateway's own in-memory ledger, never a signature check — there is nothing to
    cryptographically verify about an unpaid reservation. Raises ``CapabilityError``
    with the same §0.5-shaped reason strings the paid path uses, so both the socket
    route and the release endpoint stay on one refusal vocabulary.
    """
    entry = free_leases.get(token)
    if entry is None:
        raise CapabilityError("invalid lease")
    if entry.robot != robot:
        raise CapabilityError("lease is for another robot")
    if entry.exp <= now:
        raise CapabilityError("lease expired")
    return entry


def _admit_static_client(
    registry, robot: str, client_id: str, is_video: bool
) -> tuple[bool, str | None, str | None]:
    """Admit (or refuse) a pre-shared static agent token — identical in the paid and
    free branches, so it lives once here rather than as two copies that could drift.
    Video never reserves, only checks who currently holds the robot; control reserves
    and is renewed/released with the socket, exactly as it always has been.

    Returns ``(admitted, renew_client_id, release_client_id)``.
    """
    if is_video:
        if registry.blocks(robot, client_id) is not None:
            return False, None, None
        return True, None, None
    if not registry.reserve(robot, client_id):
        return False, None, None
    return True, client_id, client_id


def register_ws_proxy(
    app: FastAPI,
    plugins: dict[str, RobotPlugin],
    registry,
    reachability,
    payments,
    free,
    stripe,
) -> None:
    """Add ``/{robot}/ws/{path}`` to the gateway.

    **Must be called before the per-robot MCP apps are mounted.** Starlette
    matches routes in registration order and ``app.mount("/picar_freenove", ...)``
    claims everything beneath that prefix — mount first and this route is dead
    code that never sees a request.
    """
    proxied = {
        name: (p.control_base_urls(), p.control_auth_token())
        for name, p in plugins.items()
    }
    proxied = {name: entry for name, entry in proxied.items() if entry[0]}

    if not proxied:
        logger.info("ws proxy: no plugin exposes a control server; /ws/* not served")
        return
    for name, (urls, _) in proxied.items():
        logger.info("ws proxy: /%s/ws/* -> %s", name, ", ".join(urls))

    # Reachability, remembered briefly. Not safety state — deadman, duty caps
    # and the single-driver slot stay on the robot, as the module docstring
    # requires; this only remembers that the last connect attempt failed, which
    # changes how fast we say no, never what the robot is allowed to do.
    offline_until: dict[str, float] = {}
    offline_logged: set[str] = set()

    # Every currently-open lease-admitted socket, keyed by (robot, holder) — the same
    # holder string the registry reservation uses (e.g. "lease:<uuid>" for a paid
    # capability, "free:<uuid>" for a free reservation). Populated on accept(), emptied
    # on teardown. This is what lets a release endpoint actually disconnect a live
    # session instead of only freeing the reservation for the *next* connect attempt
    # while this one drives on unaware.
    lease_sockets: dict[tuple[str, str], list[WebSocket]] = {}

    # Holder -> its exp, for paid leases given up early. The capability itself stays
    # cryptographically valid until exp, so without this the same holder could reconnect
    # and take the robot back from whoever buys next. In memory only: a gateway restart
    # forgets it, which reopens that window for at most the released lease's remaining
    # minutes. Free leases need no equivalent — releasing one deletes it from
    # `free_leases` outright, which *is* the released-set (nothing else makes a free
    # token valid).
    released_leases: dict[str, int] = {}

    # Free reservations, keyed by the full token string `_mint_free_token` returns —
    # this dict is the only thing that makes such a token mean anything at all (see
    # FreeLease). Empty and unused unless `free.enabled`.
    free_leases: dict[str, FreeLease] = {}

    # The HMAC key for card-paid credentials, derived from the operator's Stripe secret
    # (decision A.3) — so credentials verify locally and survive a restart. None unless
    # `stripe.enabled`, and never touched otherwise.
    stripe_key = derive_key(stripe.secret_key) if stripe.enabled else None

    # Session id -> (robot, exp): an idempotency cache so refreshing the confirm URL
    # doesn't call Stripe again. NOT a blocklist — re-presenting the same session id is
    # a reconnect, not a replay. Pruned of expired entries on each confirm.
    confirmed_sessions: dict[str, tuple[str, int]] = {}

    @app.websocket("/{robot}/ws/{path:path}")
    async def robot_ws_proxy(ws: WebSocket, robot: str, path: str):
        entry = proxied.get(robot)
        if not entry:
            # Unknown robot, or one with no HTTP control server (the Tello
            # speaks UDP). Permanent, so send the code that stops the retries.
            await _refuse(ws, 1008, "no realtime socket for this robot")
            return
        candidates, robot_token = entry

        is_video = path.strip("/") == "video"

        if is_video and not video_enabled():
            # 1008 (policy violation) rather than a generic error: the console
            # keys off this code to stop retrying, instead of reconnecting into
            # a refusal every second.
            await _refuse(ws, 1008, "video disabled on the gateway")
            return

        # What the teardown below needs to know about *how* this socket was admitted:
        # a capability closes itself at exp; a static /ws/control token renews its
        # reservation while open and releases it when the socket closes.
        expiry_at: int | None = None
        renew_client_id: str | None = None
        release_client_id: str | None = None
        lease_key: tuple[str, str] | None = None

        if payments.enabled or stripe.enabled:
            # A paid gate — x402 capability, card-paid HMAC credential, or both — in
            # order. Every branch below either admits (falling through to
            # gateway_auth = True) or refuses and returns.
            supplied = dict(parse_qsl(ws.url.query, keep_blank_values=True)).get("token", "")
            if not supplied:
                await _refuse(ws, 1008, "payment required")
                return

            static_clients = _gateway_token_clients()
            if supplied in static_clients:
                client_id = static_clients[supplied]
                admitted, renew_client_id, release_client_id = _admit_static_client(
                    registry, robot, client_id, is_video
                )
                if not admitted:
                    await _refuse(ws, 1008, "robot is held by another session")
                    return
            elif stripe.enabled and is_stripe_credential(supplied):
                # Card-paid: verified locally against the derived key, never against
                # Stripe (Stripe is called only at start/confirm, never while admitting
                # a socket). The credential was minted at confirm with this gateway's
                # domain, so robot/gateway/exp are checked inside verify().
                now = int(time.time())
                try:
                    claims = verify_stripe(
                        supplied,
                        stripe_key,
                        robot=robot,
                        gateway=_public_domain(ws),
                        lease_minutes=stripe.lease_minutes,
                        now=now,
                    )
                except CapabilityError as exc:
                    await _refuse(ws, 1008, str(exc))
                    return
                holder = f"stripe:{claims.lease}"
                if released_leases.get(holder, 0) > now:
                    await _refuse(ws, 1008, "lease released")
                    return
                if not registry.reserve(robot, holder, ttl=claims.exp - now):
                    await _refuse(ws, 1008, "robot is held by another session")
                    return
                expiry_at = claims.exp
                lease_key = (robot, holder)
            elif payments.enabled:
                # paid-teleop-execution.md §0.5, in order (unchanged from before the
                # Stripe gate existed).
                now = int(time.time())
                try:
                    claims = _verify_lease(supplied, robot, ws, payments, now)
                except CapabilityError as exc:
                    await _refuse(ws, 1008, str(exc))
                    return
                holder = f"lease:{claims.lease}"
                if released_leases.get(holder, 0) > now:
                    await _refuse(ws, 1008, "lease released")
                    return
                # Two checks, not one: `now + leeway` decided verify()'s "iat/duration
                # valid"; a zero-or-negative ttl here would hand the registry a lease it
                # treats as instantly lapsed. Refused above instead.
                if not registry.reserve(robot, holder, ttl=claims.exp - now):
                    await _refuse(ws, 1008, "robot is held by another session")
                    return
                expiry_at = claims.exp
                lease_key = (robot, holder)
            else:
                await _refuse(ws, 1008, "invalid lease")
                return

            gateway_auth = True
        else:
            # Free reservation — the unpaid sibling of the block above, same shape, no
            # signature, no money. This is the default whenever payments are off: there
            # is no separate toggle and no fully-open fallback, so *something* — a
            # static agent token or a free reservation — is always required.
            supplied = dict(parse_qsl(ws.url.query, keep_blank_values=True)).get("token", "")
            if not supplied:
                await _refuse(ws, 1008, "reservation required")
                return

            static_clients = _gateway_token_clients()
            if supplied in static_clients:
                client_id = static_clients[supplied]
                admitted, renew_client_id, release_client_id = _admit_static_client(
                    registry, robot, client_id, is_video
                )
                if not admitted:
                    await _refuse(ws, 1008, "robot is held by another session")
                    return
            else:
                now = int(time.time())
                try:
                    entry = _verify_free_lease(supplied, robot, free_leases, now)
                except CapabilityError as exc:
                    await _refuse(ws, 1008, str(exc))
                    return
                holder = f"free:{entry.lease_id}"
                if not registry.reserve(robot, holder, ttl=entry.exp - now):
                    await _refuse(ws, 1008, "robot is held by another session")
                    return
                expiry_at = entry.exp
                lease_key = (robot, holder)

            gateway_auth = True

        query = _upstream_query(ws.url.query, robot_token, gateway_auth)

        loop = asyncio.get_running_loop()
        if offline_until.get(robot, 0.0) > loop.time():
            # A connect attempted moments ago found nothing there. Answering
            # from that verdict keeps a retrying console cheap: no candidate
            # sweep, no connect timeouts, no second log line.
            await _refuse(ws, 1013, "robot offline")
            return

        try:
            robot_ws, url = await _connect_upstream(candidates, path, query)
        except ConnectionError as exc:
            # Dated from here, not from before the sweep: an unreachable mDNS
            # name can absorb the whole connect timeout, and an entry stamped
            # with the pre-sweep clock would already have expired on arrival.
            offline_until[robot] = loop.time() + OFFLINE_CACHE_S
            reachability.mark(robot, False)
            # Once per outage, not once per retry — a console reconnecting on a
            # timer would otherwise bury every other line in the log.
            if robot not in offline_logged:
                offline_logged.add(robot)
                logger.warning("ws proxy: %s is offline — %s", robot, exc)
            # 1013 (try again later), not 1008: the robot may simply be
            # rebooting, and the console must keep retrying so it reconnects
            # on its own when the robot comes back.
            await _refuse(ws, 1013, "robot offline")
            return

        offline_until.pop(robot, None)
        reachability.mark(robot, True)
        if robot in offline_logged:
            offline_logged.discard(robot)
            # Warning, not info, purely so it is visible: uvicorn leaves this
            # module's logger at the root level, where info is filtered out.
            # An outage that logs its start and not its end reads like an
            # outage that never ended.
            logger.warning("ws proxy: %s is back", robot)

        await ws.accept()
        logger.info("ws proxy: %s/ws/%s <-> %s", robot, path, url)

        # Started only now, after accept() — admission (including the reservation
        # itself) happens earlier per §0.5's order, but a task tied to *this* socket
        # must not be created until there is a socket, or a robot-offline refusal
        # between admission and here would leak it.
        extra_tasks = []
        if expiry_at is not None:
            extra_tasks.append(asyncio.create_task(_close_at_expiry(ws, expiry_at)))
        if renew_client_id is not None:
            extra_tasks.append(
                asyncio.create_task(_renew_reservation(registry, robot, renew_client_id))
            )
        if lease_key is not None:
            lease_sockets.setdefault(lease_key, []).append(ws)

        async with robot_ws:
            tasks = [
                asyncio.create_task(_pump_to_robot(ws, robot_ws)),
                asyncio.create_task(_pump_to_browser(ws, robot_ws)),
                *extra_tasks,
            ]
            try:
                _, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                # One direction ended (or, for a capability, its expiry fired); the
                # socket is finished either way. Tear the rest down now rather than
                # leaving the robot streaming video into a browser that has gone.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                try:
                    await ws.close()
                except Exception:
                    # Already closed by the peer, or closed so abruptly that the
                    # server's own socket machinery is half torn down. A browser
                    # vanishing mid-drive is routine teleop, not an incident.
                    pass
                # A capability's reservation is deliberately NOT released here — it
                # lapses at exp, so a reconnect on the same capability re-acquires it
                # (§0.5). Only a static /ws/control token's reservation follows the
                # socket.
                if release_client_id is not None:
                    registry.release(robot, release_client_id)
                if lease_key is not None:
                    sockets = lease_sockets.get(lease_key)
                    if sockets and ws in sockets:
                        sockets.remove(ws)
                        if not sockets:
                            lease_sockets.pop(lease_key, None)
        logger.info("ws proxy: %s/ws/%s closed", robot, path)

    async def _force_close_lease_sockets(robot: str, holder: str) -> None:
        for sock in list(lease_sockets.get((robot, holder), [])):
            try:
                await sock.close(code=1008, reason="lease released")
            except Exception:
                # Same reasoning as every other best-effort close in this module: the
                # peer may already be gone.
                pass

    @app.post("/{robot}/lease/release")
    async def release_lease(request: Request, robot: str) -> dict:
        """Voluntarily give up a lease before it expires — paid (x402 or card), or free.

        Never refunds — for an x402 lease, settlement is a direct wallet-to-owner
        transfer with no escrow (paid-teleop-access.md §4.2), so there is no key
        anywhere that could claw money back; a card lease *could* be refunded through
        Stripe but v1 deliberately never does (refunds and disputes are the operator's
        job in the Stripe dashboard); a free lease never took any money to begin with.
        Either way this only frees the *robot* early: the reservation is released so the
        next person does not wait out someone else's unused time, and this session's own
        live sockets (if any) are force-closed so the page's "released" state and the
        car's actual drivability agree, instead of a stale socket quietly outliving the
        paywall/reserve card the console shows after this call.
        """
        if robot not in proxied:
            raise HTTPException(status_code=404, detail=f"no realtime socket for {robot!r}")
        if not payments.enabled and not stripe.enabled and not free.enabled:
            raise HTTPException(
                status_code=404, detail="teleop reservations are not enabled on this gateway"
            )

        body = await request.json()
        token = body.get("token") if isinstance(body, dict) else None
        if not token:
            raise HTTPException(status_code=400, detail="token is required")

        now = int(time.time())

        if stripe.enabled and is_stripe_credential(token):
            try:
                claims = verify_stripe(
                    token,
                    stripe_key,
                    robot=robot,
                    gateway=_public_domain(request),
                    lease_minutes=stripe.lease_minutes,
                    now=now,
                )
            except CapabilityError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            holder = f"stripe:{claims.lease}"
            for stale_holder, exp in list(released_leases.items()):
                if exp <= now:
                    del released_leases[stale_holder]
            released_leases[holder] = claims.exp
            confirmed_sessions.pop(claims.lease, None)
            registry.release(robot, holder)
            await _force_close_lease_sockets(robot, holder)
            return {"released": True}

        if payments.enabled:
            try:
                claims = _verify_lease(token, robot, request, payments, now)
            except CapabilityError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            holder = f"lease:{claims.lease}"
            for stale_holder, exp in list(released_leases.items()):
                if exp <= now:
                    del released_leases[stale_holder]
            released_leases[holder] = claims.exp
            registry.release(robot, holder)
            await _force_close_lease_sockets(robot, holder)
            return {"released": True, "ledger_released": await _release_ledger(payments.url, token)}

        # free.enabled — no signature, no ledger, no external service: deleting the
        # entry from free_leases *is* the released-set, nothing else to track.
        try:
            entry = _verify_free_lease(token, robot, free_leases, now)
        except CapabilityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        holder = f"free:{entry.lease_id}"
        free_leases.pop(token, None)
        registry.release(robot, holder)
        await _force_close_lease_sockets(robot, holder)
        return {"released": True}

    @app.post("/{robot}/lease/reserve")
    async def reserve_lease(robot: str) -> dict:
        """Grant a free, timed, exclusive turn — no payment, no signature.

        Never renews an existing token: the hard cap at ``TELEOP_LEASE_MINUTES`` is the
        point, so a re-click after expiry always mints a fresh lease, never stretches
        the old one.
        """
        if robot not in proxied:
            raise HTTPException(status_code=404, detail=f"no realtime socket for {robot!r}")
        if not free.enabled:
            raise HTTPException(
                status_code=404, detail="free reservations are not enabled on this gateway"
            )

        now = int(time.time())
        for stale_token, entry in list(free_leases.items()):
            if entry.exp <= now:
                del free_leases[stale_token]

        lease_id = str(uuid.uuid4())
        exp = now + free.lease_minutes * SECONDS_PER_LEASE_MINUTE
        token = _mint_free_token(lease_id, robot, now, exp)
        holder = f"free:{lease_id}"

        if not registry.reserve(robot, holder, ttl=exp - now):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "robot is held by another session",
                    "reservation": registry.status(robot),
                },
            )

        free_leases[token] = FreeLease(lease_id=lease_id, robot=robot, exp=exp)
        return {"token": token, "lease": lease_id, "robot": robot, "exp": exp}

    @app.get("/{robot}/stripe/start")
    async def stripe_start(robot: str, request: Request):
        """Begin a card purchase: create a Checkout Session and send the buyer to Stripe."""
        if robot not in proxied or not stripe.enabled:
            return _stripe_page(404, robot, "card payments are not enabled for this robot")
        if registry.status(robot)["reserved"]:
            # The buyer has no identity yet, so any hold blocks them — refuse before any
            # money moves (decision A.5).
            return _stripe_page(409, robot, "this robot is currently in use")
        if reachability.get(robot) is False:
            return _stripe_page(503, robot, "this robot is offline right now")

        origin = _public_origin(request)
        form = {
            "mode": "payment",
            "payment_method_types[0]": "card",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": stripe.currency,
            "line_items[0][price_data][unit_amount]": str(stripe.price_cents),
            "line_items[0][price_data][product_data][name]": (
                f"Drive {robot} for {stripe.lease_minutes} min"
            ),
            "metadata[robot]": robot,
            "metadata[gateway]": _public_domain(request),
            # Literal braces: Stripe substitutes the real session id.
            "success_url": f"{origin}/{robot}/stripe/confirm?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{origin}/{robot}/ui",
        }

        import httpx

        try:
            async with httpx.AsyncClient(timeout=STRIPE_TIMEOUT_S) as client:
                r = await client.post(
                    f"{stripe.api_base}/v1/checkout/sessions",
                    headers=_stripe_headers(stripe),
                    data=form,
                )
        except httpx.HTTPError:
            logger.warning("ws proxy: stripe start for %s failed to reach Stripe", robot)
            return _stripe_page(503, robot, "card payments are unavailable right now")

        if not 200 <= r.status_code < 300:
            # Log the status, never the key.
            logger.warning("ws proxy: stripe start for %s returned %d", robot, r.status_code)
            return _stripe_page(503, robot, "card payments are unavailable right now")

        try:
            session = r.json()
            url = session["url"]
        except (ValueError, KeyError, TypeError):
            logger.warning("ws proxy: stripe start for %s returned no url", robot)
            return _stripe_page(503, robot, "card payments are unavailable right now")

        return RedirectResponse(url, status_code=303)

    @app.get("/{robot}/stripe/confirm")
    async def stripe_confirm(robot: str, request: Request):
        """The success_url target: verify the payment, reserve, and hand back a credential."""
        if robot not in proxied or not stripe.enabled:
            return _stripe_page(404, robot, "card payments are not enabled for this robot")

        session_id = request.query_params.get("session_id", "")
        # Before any Stripe call — also blocks path injection into the retrieve URL.
        if not session_id or not _SESSION_ID_RE.match(session_id):
            return _stripe_page(400, robot, "invalid checkout session")

        now = int(time.time())

        # Prune the idempotency cache of expired entries.
        for sid, (_, exp) in list(confirmed_sessions.items()):
            if exp <= now:
                del confirmed_sessions[sid]

        cached = confirmed_sessions.get(session_id)
        if cached is not None and cached[0] == robot and cached[1] > now:
            exp = cached[1]
        else:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=STRIPE_TIMEOUT_S) as client:
                    r = await client.get(
                        f"{stripe.api_base}/v1/checkout/sessions/{session_id}",
                        headers=_stripe_headers(stripe),
                        params=[("expand[]", "payment_intent")],
                    )
            except httpx.HTTPError:
                return _stripe_page(503, robot, "couldn't reach Stripe; refresh to retry")

            if r.status_code == 404:
                return _stripe_page(400, robot, "unknown checkout session")
            if r.status_code >= 500:
                return _stripe_page(503, robot, "couldn't reach Stripe; refresh to retry")
            if r.status_code != 200:
                return _stripe_page(502, robot, "card payments are unavailable right now")

            try:
                session = r.json()
            except ValueError:
                return _stripe_page(502, robot, "card payments are unavailable right now")

            if session.get("status") != "complete" or session.get("payment_status") != "paid":
                return _stripe_page(402, robot, "payment not complete")

            if (
                session.get("mode") != "payment"
                or session.get("livemode") != stripe.livemode
                or session.get("amount_total") != stripe.price_cents
                or session.get("currency") != stripe.currency
            ):
                logger.warning(
                    "ws proxy: stripe payment mismatch for %s (amount=%r currency=%r livemode=%r)",
                    robot,
                    session.get("amount_total"),
                    session.get("currency"),
                    session.get("livemode"),
                )
                return _stripe_page(
                    409,
                    robot,
                    "payment does not match this gateway's price; contact the operator for a refund",
                )

            metadata = session.get("metadata") or {}
            if metadata.get("robot") != robot:
                return _stripe_page(400, robot, "session is for another robot")

            payment_intent = session.get("payment_intent")
            created = payment_intent.get("created") if isinstance(payment_intent, dict) else None
            if not isinstance(created, int) or isinstance(created, bool):
                return _stripe_page(502, robot, "card payments are unavailable right now")

            # min() guards against Stripe's clock running ahead of this host's clock,
            # which would otherwise make exp - iat exceed the lease and fail verify()'s
            # duration check.
            exp = min(created, now) + stripe.lease_minutes * SECONDS_PER_LEASE_MINUTE
            if exp <= now:
                return _stripe_page(409, robot, "lease expired")

            holder = f"stripe:{session_id}"
            if released_leases.get(holder, 0) > now:
                return _stripe_page(409, robot, "lease released")

            if not registry.reserve(robot, holder, ttl=exp - now):
                # Decision A.5: still issue the credential — the console retries "robot is
                # held by another session" every 5s. Log the robot and the id's tail, never
                # the full session id.
                logger.info(
                    "ws proxy: stripe confirm for %s lost the reserve race (…%s)",
                    robot,
                    session_id[-6:],
                )

            confirmed_sessions[session_id] = (robot, exp)

        claims = StripeClaims(
            v=1,
            kind="stripe",
            robot=robot,
            gateway=_public_domain(request),
            lease=session_id,
            payer="card",
            iat=now,
            exp=exp,
        )
        credential = mint_stripe(claims, stripe_key)
        return RedirectResponse(
            f"/{robot}/ui#token={quote(credential)}",
            status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
