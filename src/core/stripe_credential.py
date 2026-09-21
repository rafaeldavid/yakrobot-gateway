"""A gateway-minted HMAC credential for card-paid (Stripe) leases.

This gateway both mints and verifies this credential — unlike an x402 capability,
which ``yakrobot-payments`` mints and this gateway only *verifies* (see
``core.capability``). It is **not** a capability and carries no signature recoverable
to a public issuer; its authority is a shared HMAC key derived from the operator's
``STRIPE_SECRET_KEY`` (plan decision A.3), so it verifies locally and survives a
gateway restart.

It keeps the same two-part wire shape a capability uses — ``<b64url(payload)>.<b64url(
signature)>`` — so the console's existing ``decodeCapabilityToken`` (which reads
``token.split(".")[0]`` as the payload) and its storage/countdown code need no changes.
The two are told apart by a ``"kind": "stripe"`` field in the payload, not a wire
prefix, because a prefix would break that ``split(".")`` read (decision A.4). An x402
claim set has an exact key set with no ``kind``, and a free token's payload has no
``kind`` either, so they cannot be confused.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass

from core.capability import CapabilityError, normalize_host


@dataclass(frozen=True)
class StripeClaims:
    v: int
    kind: str
    robot: str
    gateway: str
    lease: str
    payer: str
    iat: int
    exp: int


_REQUIRED_CLAIMS = ("v", "kind", "robot", "gateway", "lease", "payer", "iat", "exp")

# Mirrors capability._DURATION_LEEWAY_S: the only tolerance on the claimed lease
# duration, folded into the single "invalid lease" reason.
_DURATION_LEEWAY_S = 30

_CREDENTIAL_INFO = b"yakrobot-gateway/stripe-credential/v1"


def derive_key(secret_key: str) -> bytes:
    """The shared HMAC key, derived from the Stripe secret (plan decision A.3).

    Derived, not the raw secret, so the credential key is a fixed-length digest with
    no relationship the secret might leak, and it reproduces after a gateway restart
    (so credentials survive restart). Rotating the Stripe key logs out drivers
    mid-lease — acceptable and documented.
    """
    return hmac.new(secret_key.encode(), _CREDENTIAL_INFO, hashlib.sha256).digest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(claims: dict) -> bytes:
    """UTF-8 JSON, keys sorted ascending, no insignificant whitespace."""
    return json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def mint(claims: StripeClaims, key: bytes) -> str:
    """Canonical-JSON payload, HMAC-SHA256 over the payload bytes, two-part token."""
    payload = _canonical(asdict(claims))
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(signature)}"


def is_stripe_credential(token: str) -> bool:
    """Never raises; True iff part 0 decodes to a dict with ``kind == "stripe"``."""
    try:
        payload = _b64url_decode(token.split(".")[0])
        obj = json.loads(payload)
        return isinstance(obj, dict) and obj.get("kind") == "stripe"
    except Exception:
        return False


def _is_int(value) -> bool:
    # bool is an int subclass in Python — a JSON `true`/`false` must not pass.
    return isinstance(value, int) and not isinstance(value, bool)


def verify(
    token: str,
    key: bytes,
    *,
    robot: str,
    gateway: str,
    lease_minutes: int,
    now: int,
) -> StripeClaims:
    """Verify the HMAC, claim shape, version, kind and duration, then robot/gateway/exp.

    Every shape/format/crypto/duration failure is the single reason "invalid lease";
    the distinct reasons — another robot, another gateway, expired — are reserved for
    the post-parse checks, in the order the plan specifies, so the console's
    ``PAYWALL_REASONS`` set stays unchanged.
    """
    parts = token.split(".")
    if len(parts) != 2:
        raise CapabilityError("invalid lease")

    try:
        payload = _b64url_decode(parts[0])
        signature = _b64url_decode(parts[1])
    except Exception:
        raise CapabilityError("invalid lease") from None

    expected = hmac.new(key, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise CapabilityError("invalid lease")

    try:
        obj = json.loads(payload)
    except Exception:
        raise CapabilityError("invalid lease") from None

    if not isinstance(obj, dict) or set(obj) != set(_REQUIRED_CLAIMS):
        raise CapabilityError("invalid lease")

    if not _is_int(obj["v"]) or obj["v"] != 1:
        raise CapabilityError("invalid lease")
    if not isinstance(obj["kind"], str) or obj["kind"] != "stripe":
        raise CapabilityError("invalid lease")
    if not isinstance(obj["robot"], str) or not isinstance(obj["gateway"], str):
        raise CapabilityError("invalid lease")
    if not isinstance(obj["lease"], str) or not isinstance(obj["payer"], str):
        raise CapabilityError("invalid lease")
    if not _is_int(obj["iat"]) or not _is_int(obj["exp"]):
        raise CapabilityError("invalid lease")

    if obj["exp"] - obj["iat"] > lease_minutes * 60 + _DURATION_LEEWAY_S:
        raise CapabilityError("invalid lease")

    claims = StripeClaims(**obj)
    if claims.robot != robot:
        raise CapabilityError("lease is for another robot")
    if normalize_host(claims.gateway) != normalize_host(gateway):
        raise CapabilityError("lease is for another gateway")
    if claims.exp <= now:
        raise CapabilityError("lease expired")
    return claims
