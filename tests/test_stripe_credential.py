"""Unit tests for core.stripe_credential — pure, no network, no servers.

The credential is minted and verified locally by the gateway (HMAC-SHA256 over a
canonical-JSON payload). This file exercises the crypto, the shape/type checks, and
the distinct refusal reasons, plus ``is_stripe_credential``'s discrimination against
x402 and free tokens.
"""

import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capability_helper import mint as mint_x402  # noqa: E402

from core.capability import CapabilityError  # noqa: E402
from core.stripe_credential import (  # noqa: E402
    StripeClaims,
    derive_key,
    is_stripe_credential,
    mint,
    verify,
)

SECRET = "sk_test_0123456789abcdef"
KEY = derive_key(SECRET)
LEASE_MINUTES = 5

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "capability-v1.json").read_text()
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mint_dict(claims: dict, key: bytes) -> str:
    """Mint a token from a raw dict (for the extra-claim case ``mint`` can't build)."""
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(signature)}"


def _claims(**overrides) -> StripeClaims:
    base = dict(
        v=1,
        kind="stripe",
        robot="fakerobot_picar",
        gateway="g.example",
        lease="cs_test_123",
        payer="card",
        iat=1000,
        exp=1300,
    )
    base.update(overrides)
    return StripeClaims(**base)


def _verify(token: str, key: bytes = KEY, **kwargs):
    defaults = dict(
        robot="fakerobot_picar", gateway="g.example", lease_minutes=LEASE_MINUTES, now=1100
    )
    defaults.update(kwargs)
    return verify(token, key, **defaults)


def test_round_trip():
    claims = _claims()
    token = mint(claims, KEY)
    assert token.count(".") == 1
    assert is_stripe_credential(token) is True
    assert _verify(token) == claims


def test_tampered_payload_rejected():
    token = mint(_claims(), KEY)
    part0, part1 = token.split(".")
    payload = base64.urlsafe_b64decode(part0 + "=" * (-len(part0) % 4))
    flipped = payload[:5] + bytes([payload[5] ^ 1]) + payload[6:]
    with pytest.raises(CapabilityError, match="invalid lease"):
        _verify(f"{_b64url(flipped)}.{part1}")


def test_tampered_signature_rejected():
    token = mint(_claims(), KEY)
    part0, part1 = token.split(".")
    sig = base64.urlsafe_b64decode(part1 + "=" * (-len(part1) % 4))
    flipped = sig[:8] + bytes([sig[8] ^ 1]) + sig[9:]
    with pytest.raises(CapabilityError, match="invalid lease"):
        _verify(f"{part0}.{_b64url(flipped)}")


def test_wrong_key_rejected():
    token = mint(_claims(), KEY)
    with pytest.raises(CapabilityError, match="invalid lease"):
        _verify(token, key=derive_key("sk_test_othersecret9999"))


def test_other_robot_rejected():
    with pytest.raises(CapabilityError, match="lease is for another robot"):
        _verify(mint(_claims(), KEY), robot="other_robot")


def test_other_gateway_rejected():
    with pytest.raises(CapabilityError, match="lease is for another gateway"):
        _verify(mint(_claims(), KEY), gateway="other.example")


def test_expired_rejected():
    # exp == now is expired (the lease window is [iat, exp)).
    with pytest.raises(CapabilityError, match="lease expired"):
        _verify(mint(_claims(), KEY), now=1300)


def test_overlong_rejected():
    # 331s > lease_minutes*60 + 30 = 330s.
    with pytest.raises(CapabilityError, match="invalid lease"):
        _verify(mint(_claims(exp=1331), KEY))


def test_extra_claim_rejected():
    claims = dict(
        v=1,
        kind="stripe",
        robot="fakerobot_picar",
        gateway="g.example",
        lease="cs_test_123",
        payer="card",
        iat=1000,
        exp=1300,
        extra="nope",
    )
    with pytest.raises(CapabilityError, match="invalid lease"):
        _verify(_mint_dict(claims, KEY))


def test_derive_key_deterministic_and_secret_specific():
    assert derive_key("sk_test_a") == derive_key("sk_test_a")
    assert derive_key("sk_test_a") != derive_key("sk_test_b")
    assert len(derive_key("sk_test_a")) == 32  # SHA-256


def test_is_stripe_credential_false_for_x402():
    token = mint_x402(FIXTURE["claims"], FIXTURE["private_key"])
    assert is_stripe_credential(token) is False


def test_is_stripe_credential_false_for_free_token():
    from core.ws_proxy import _mint_free_token

    free = _mint_free_token("free:123", "fakerobot_picar", iat=1000, exp=1300)
    assert is_stripe_credential(free) is False


def test_is_stripe_credential_false_for_garbage():
    for garbage in ("", "a.b", "not base64"):
        assert is_stripe_credential(garbage) is False
