"""Unit tests for core.payments_config — no servers, no network.

Every test sets and restores env via monkeypatch, per paid-teleop-execution.md §0.1.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.payments_config import (  # noqa: E402
    FreeTeleopConfig,
    PaymentsConfig,
    PaymentsConfigError,
    StripeConfig,
    index_summary,
    load_free_teleop_config,
    load_payments_config,
    load_stripe_config,
    stripe_summary,
    teleop_summary,
)

VALID = {
    "PAYMENTS_ENABLED": "1",
    "PAYMENTS_URL": "https://pay.yakrobot.com",
    "PAYMENTS_ISSUER": "0xF39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
}

VALID_STRIPE = {
    "STRIPE_GATE_ENABLED": "1",
    "STRIPE_SECRET_KEY": "sk_test_51abcdefghijk",
    "STRIPE_PRICE_CENTS": "100",
}


def _set(monkeypatch, **env):
    for key in (
        "PAYMENTS_ENABLED", "PAYMENTS_URL", "PAYMENTS_ISSUER",
        "TELEOP_PRICE_USDC", "TELEOP_LEASE_MINUTES",
        "STRIPE_GATE_ENABLED", "STRIPE_SECRET_KEY", "STRIPE_PRICE_CENTS",
        "STRIPE_CURRENCY", "STRIPE_API_BASE",
        "STRIPE_AUTOMATIC_TAX", "STRIPE_TAX_CODE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_disabled_by_default(monkeypatch):
    _set(monkeypatch)
    cfg = load_payments_config()
    assert cfg.enabled is False
    assert cfg.url is None
    assert cfg.issuer is None
    assert cfg.price_usdc is None
    assert cfg.lease_minutes is None


def test_disabled_ignores_invalid_other_vars(monkeypatch):
    _set(
        monkeypatch,
        PAYMENTS_URL="not a url",
        PAYMENTS_ISSUER="not an address",
        TELEOP_PRICE_USDC="garbage",
        TELEOP_LEASE_MINUTES="garbage",
    )
    cfg = load_payments_config()
    assert cfg.enabled is False


def test_enabled_requires_url_and_issuer(monkeypatch):
    _set(monkeypatch, PAYMENTS_ENABLED="1")
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_URL" in str(exc)

    _set(monkeypatch, PAYMENTS_ENABLED="1", PAYMENTS_URL="https://pay.yakrobot.com")
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_ISSUER" in str(exc)


def test_price_accepts_one_and_six_decimals(monkeypatch):
    for price in ("1", "1.00", "0.000001"):
        _set(monkeypatch, **VALID, TELEOP_PRICE_USDC=price)
        cfg = load_payments_config()
        assert cfg.price_usdc == price


def test_price_rejects_bad_values(monkeypatch):
    for price in ("0", "-1", "1.0000001", "abc", "01.5", ""):
        _set(monkeypatch, **VALID, TELEOP_PRICE_USDC=price)
        try:
            load_payments_config()
            raise AssertionError(f"expected rejection for price {price!r}")
        except PaymentsConfigError as exc:
            assert "TELEOP_PRICE_USDC" in str(exc)


def test_lease_minutes_bounds(monkeypatch):
    for minutes in ("0", "61"):
        _set(monkeypatch, **VALID, TELEOP_LEASE_MINUTES=minutes)
        try:
            load_payments_config()
            raise AssertionError(f"expected rejection for lease minutes {minutes!r}")
        except PaymentsConfigError as exc:
            assert "TELEOP_LEASE_MINUTES" in str(exc)

    for minutes in ("1", "60"):
        _set(monkeypatch, **VALID, TELEOP_LEASE_MINUTES=minutes)
        cfg = load_payments_config()
        assert cfg.lease_minutes == int(minutes)


def test_issuer_lowercased_and_validated(monkeypatch):
    _set(monkeypatch, **VALID)
    cfg = load_payments_config()
    assert cfg.issuer == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"

    _set(monkeypatch, **{**VALID, "PAYMENTS_ISSUER": "0xnotanaddress"})
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_ISSUER" in str(exc)


def test_url_requires_https_except_localhost(monkeypatch):
    for url in (
        "https://pay.yakrobot.com/",
        "http://127.0.0.1:8080",
        "http://localhost:3000",
    ):
        _set(monkeypatch, **{**VALID, "PAYMENTS_URL": url})
        cfg = load_payments_config()
        assert cfg.url == url.rstrip("/")

    _set(monkeypatch, **{**VALID, "PAYMENTS_URL": "http://evil.example.com"})
    try:
        load_payments_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "PAYMENTS_URL" in str(exc)


def test_index_summary_shapes(monkeypatch):
    _set(monkeypatch)
    assert index_summary(load_payments_config()) == {"enabled": False}

    _set(monkeypatch, **VALID)
    cfg = load_payments_config()
    assert index_summary(cfg) == {
        "enabled": True,
        "url": "https://pay.yakrobot.com",
        "issuer": "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "teleop": {"price_usdc": "1.00", "lease_minutes": 5},
    }


def test_free_teleop_enabled_by_default():
    """No separate toggle: free reservations are on whenever no paid gate is on."""
    cfg = load_free_teleop_config(
        PaymentsConfig(enabled=False), StripeConfig(enabled=False)
    )
    assert cfg.enabled is True
    assert cfg.lease_minutes == 5  # the shared TELEOP_LEASE_MINUTES default


def test_free_teleop_reads_shared_lease_minutes(monkeypatch):
    _set(monkeypatch, TELEOP_LEASE_MINUTES="30")
    cfg = load_free_teleop_config(
        PaymentsConfig(enabled=False), StripeConfig(enabled=False)
    )
    assert cfg.lease_minutes == 30


def test_free_teleop_rejects_bad_lease_minutes(monkeypatch):
    _set(monkeypatch, TELEOP_LEASE_MINUTES="0")
    try:
        load_free_teleop_config(
            PaymentsConfig(enabled=False), StripeConfig(enabled=False)
        )
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "TELEOP_LEASE_MINUTES" in str(exc)


def test_free_teleop_disabled_when_payments_enabled():
    cfg = load_free_teleop_config(
        PaymentsConfig(enabled=True), StripeConfig(enabled=False)
    )
    assert cfg.enabled is False
    assert cfg.lease_minutes is None


def test_teleop_summary_shapes():
    off = PaymentsConfig(enabled=False)
    paid = PaymentsConfig(enabled=True, lease_minutes=5)
    free_off = FreeTeleopConfig(enabled=False)
    free_on = FreeTeleopConfig(enabled=True, lease_minutes=10)
    stripe_off = StripeConfig(enabled=False)

    assert teleop_summary(off, free_off, stripe_off) == {
        "reservation": "none", "lease_minutes": None,
    }
    assert teleop_summary(off, free_on, stripe_off) == {
        "reservation": "free", "lease_minutes": 10,
    }
    assert teleop_summary(paid, free_off, stripe_off) == {
        "reservation": "paid", "lease_minutes": 5,
    }
    # Paid takes precedence in the (unreachable in practice — load_free_teleop_config
    # only ever constructs free_on when neither gate is enabled) case both configs
    # claim to be enabled.
    assert teleop_summary(paid, free_on, stripe_off) == {
        "reservation": "paid", "lease_minutes": 5,
    }


# --- Stripe gate ---------------------------------------------------------------


def test_stripe_disabled_by_default(monkeypatch):
    _set(monkeypatch)
    cfg = load_stripe_config()
    assert cfg.enabled is False
    assert cfg.secret_key is None
    assert cfg.price_cents is None
    assert cfg.currency is None
    assert cfg.lease_minutes is None
    assert cfg.api_base is None
    assert cfg.livemode is None


def test_stripe_disabled_ignores_invalid_other_vars(monkeypatch):
    _set(
        monkeypatch,
        STRIPE_SECRET_KEY="not a key",
        STRIPE_PRICE_CENTS="abc",
        STRIPE_CURRENCY="USDOLLARS",
        STRIPE_API_BASE="not a url",
    )
    cfg = load_stripe_config()
    assert cfg.enabled is False


def test_stripe_requires_key_and_price(monkeypatch):
    _set(monkeypatch, STRIPE_GATE_ENABLED="1")
    try:
        load_stripe_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "STRIPE_SECRET_KEY" in str(exc)

    _set(monkeypatch, STRIPE_GATE_ENABLED="1", STRIPE_SECRET_KEY="sk_test_51abcdefghijk")
    try:
        load_stripe_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "STRIPE_PRICE_CENTS" in str(exc)


def test_stripe_key_format(monkeypatch):
    for key in ("sk_test_51abcdefghijk", "rk_live_ABCDEFGH12345678"):
        _set(monkeypatch, STRIPE_GATE_ENABLED="1", STRIPE_SECRET_KEY=key, STRIPE_PRICE_CENTS="100")
        assert load_stripe_config().enabled is True

    for key in ("pk_test_51abcdefghijk", "sk_test_", "sk_", "totally wrong"):
        _set(monkeypatch, STRIPE_GATE_ENABLED="1", STRIPE_SECRET_KEY=key, STRIPE_PRICE_CENTS="100")
        try:
            load_stripe_config()
            raise AssertionError(f"expected rejection for key {key!r}")
        except PaymentsConfigError as exc:
            assert "STRIPE_SECRET_KEY" in str(exc)
            assert key not in str(exc)  # the rejected value must never be echoed


def test_stripe_price_floor(monkeypatch):
    for cents in ("49", "abc", "-5", "0"):
        _set(monkeypatch, STRIPE_GATE_ENABLED="1", STRIPE_SECRET_KEY="sk_test_51abcdefghijk",
             STRIPE_PRICE_CENTS=cents)
        try:
            load_stripe_config()
            raise AssertionError(f"expected rejection for price {cents!r}")
        except PaymentsConfigError as exc:
            assert "STRIPE_PRICE_CENTS" in str(exc)

    for cents in ("50", "100", "999999"):
        _set(monkeypatch, STRIPE_GATE_ENABLED="1", STRIPE_SECRET_KEY="sk_test_51abcdefghijk",
             STRIPE_PRICE_CENTS=cents)
        assert load_stripe_config().price_cents == int(cents)


def test_stripe_currency_default_and_validation(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE)
    assert load_stripe_config().currency == "usd"

    _set(monkeypatch, **VALID_STRIPE, STRIPE_CURRENCY="EUR")
    assert load_stripe_config().currency == "eur"

    for currency in ("USDOLLARS", "eu", "12"):
        _set(monkeypatch, **VALID_STRIPE, STRIPE_CURRENCY=currency)
        try:
            load_stripe_config()
            raise AssertionError(f"expected rejection for currency {currency!r}")
        except PaymentsConfigError as exc:
            assert "STRIPE_CURRENCY" in str(exc)


def test_stripe_api_base_https_or_localhost(monkeypatch):
    for url in (
        "https://api.stripe.com/",
        "http://127.0.0.1:8193",
        "http://localhost:3000",
    ):
        _set(monkeypatch, **VALID_STRIPE, STRIPE_API_BASE=url)
        assert load_stripe_config().api_base == url.rstrip("/")

    _set(monkeypatch, **VALID_STRIPE, STRIPE_API_BASE="http://evil.example.com")
    try:
        load_stripe_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "STRIPE_API_BASE" in str(exc)


def test_stripe_livemode_from_key(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE)
    assert load_stripe_config().livemode is False

    _set(monkeypatch, **{**VALID_STRIPE, "STRIPE_SECRET_KEY": "rk_live_ABCDEFGH12345678"})
    assert load_stripe_config().livemode is True


def test_stripe_automatic_tax_off_by_default(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE)
    cfg = load_stripe_config()
    assert cfg.automatic_tax is False
    assert cfg.tax_code is None


def test_stripe_automatic_tax_and_tax_code(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE, STRIPE_AUTOMATIC_TAX="1")
    cfg = load_stripe_config()
    assert cfg.automatic_tax is True
    assert cfg.tax_code is None  # Stripe falls back to the account's default tax code

    _set(monkeypatch, **VALID_STRIPE, STRIPE_AUTOMATIC_TAX="1", STRIPE_TAX_CODE="txcd_10000000")
    assert load_stripe_config().tax_code == "txcd_10000000"

    for code in ("10000000", "txcd_123", "txcd_1000000x"):
        _set(monkeypatch, **VALID_STRIPE, STRIPE_AUTOMATIC_TAX="1", STRIPE_TAX_CODE=code)
        try:
            load_stripe_config()
            raise AssertionError(f"expected rejection for tax code {code!r}")
        except PaymentsConfigError as exc:
            assert "STRIPE_TAX_CODE" in str(exc)


def test_stripe_tax_code_needs_automatic_tax(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE, STRIPE_TAX_CODE="txcd_10000000")
    try:
        load_stripe_config()
        raise AssertionError("expected PaymentsConfigError")
    except PaymentsConfigError as exc:
        assert "STRIPE_AUTOMATIC_TAX" in str(exc)


def test_stripe_repr_hides_secret(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE)
    cfg = load_stripe_config()
    assert "sk_test_51abcdefghijk" not in repr(cfg)


def test_free_teleop_off_when_stripe_on(monkeypatch):
    _set(monkeypatch, **VALID_STRIPE)
    stripe = load_stripe_config()
    cfg = load_free_teleop_config(PaymentsConfig(enabled=False), stripe)
    assert cfg.enabled is False
    assert cfg.lease_minutes is None


def test_teleop_summary_paid_when_only_stripe():
    off = PaymentsConfig(enabled=False)
    stripe = StripeConfig(enabled=True, lease_minutes=7)
    free_off = FreeTeleopConfig(enabled=False)
    assert teleop_summary(off, free_off, stripe) == {
        "reservation": "paid", "lease_minutes": 7,
    }


def test_stripe_summary_shapes():
    assert stripe_summary(StripeConfig(enabled=False)) == {"enabled": False}

    cfg = StripeConfig(enabled=True, price_cents=150, currency="usd", lease_minutes=5)
    assert stripe_summary(cfg) == {
        "enabled": True,
        "price_cents": 150,
        "currency": "usd",
        "lease_minutes": 5,
    }
