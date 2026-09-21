"""A local fake Stripe, for tests and hands-on smoke runs — never a real Stripe.

The gateway talks to Stripe over exactly two REST endpoints (``POST
/v1/checkout/sessions`` to create a session, ``GET /v1/checkout/sessions/{id}`` to
retrieve it). This module fakes both, so the whole card gate can be exercised with no
account, no key and no network.

"Paying" is simply following the redirect: ``POST`` returns the ``success_url`` it was
given with ``{CHECKOUT_SESSION_ID}`` replaced by the real id, and it also records a
``paid_session`` for that id, so the follow-up ``GET`` (the confirm step) already sees a
complete, paid session.

Two ways to use it:

* **In tests** — ``create_fake_stripe(state)`` returns the FastAPI app; each ``POST``
  mints a fresh ``cs_test_<n>`` id and records the parsed form in
  ``state.created_forms``. Tests can override ``state.sessions[sid]`` (with
  ``paid_session(...)``) to fake unpaid / wrong-amount / wrong-currency / livemode
  cases before hitting confirm.

* **By hand** — ``uv run python tests/tools/fake_stripe.py --port 8193`` serves it on
  loopback, using the fixed id ``cs_test_local`` so the §E.2 smoke test can drive it
  with curl.
"""

import argparse
import re
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Only test-mode keys: the fake never claims to accept a live Stripe secret.
_AUTH_RE = re.compile(r"^Bearer (sk|rk)_test_[A-Za-z0-9]+$")


def paid_session(
    sid: str,
    robot: str,
    *,
    cents: int = 100,
    currency: str = "usd",
    livemode: bool = False,
    created: int | None = None,
    status: str = "complete",
    payment_status: str = "paid",
    gateway: str = "127.0.0.1:8192",
) -> dict:
    """A realistic Checkout Session body, the shape the gateway's confirm reads.

    ``created`` (the PaymentIntent's creation time, which the gateway anchors the lease
    ``exp`` to) defaults to now. ``sid`` is the Checkout Session id, e.g. ``cs_test_1``.
    ``gateway`` defaults to the test suite's gateway host (``GW_PORT``); sessions
    created through ``POST`` carry whatever ``metadata[gateway]`` the gateway sent.
    """
    return {
        "id": sid,
        "object": "checkout.session",
        "mode": "payment",
        "status": status,
        "payment_status": payment_status,
        "amount_total": cents,
        "currency": currency,
        "livemode": livemode,
        "metadata": {"robot": robot, "gateway": gateway},
        "payment_intent": {
            "id": f"pi_{sid}",
            "object": "payment_intent",
            "created": created if created is not None else int(time.time()),
        },
    }


@dataclass
class StripeState:
    """Everything the fake records, for tests to assert on and to pre-seed sessions."""

    created_forms: list[dict] = field(default_factory=list)
    retrieve_calls: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, dict] = field(default_factory=dict)
    fixed_id: str | None = None  # standalone mode: POST always returns this id
    _counter: int = field(default=0, repr=False)

    @property
    def next_id(self) -> str:
        if self.fixed_id:
            return self.fixed_id
        self._counter += 1
        return f"cs_test_{self._counter}"


def create_fake_stripe(state: StripeState) -> FastAPI:
    """Build a FastAPI app that fakes the two Stripe endpoints the gateway uses."""

    app = FastAPI(title="fake-stripe")

    @app.post("/v1/checkout/sessions")
    async def create_session(request: Request):
        auth = request.headers.get("Authorization", "")
        if not _AUTH_RE.match(auth):
            return JSONResponse(
                {"error": {"type": "invalid_request_error"}}, status_code=401
            )

        form = {k: str(v) for k, v in (await request.form()).items()}
        state.created_forms.append(form)

        sid = state.next_id
        robot = form.get("metadata[robot]", "")
        # "Paying" is just following the redirect — record a paid session now so the
        # confirm GET succeeds. Tests override this for the unpaid/mismatch cases.
        state.sessions[sid] = paid_session(
            sid, robot, gateway=form.get("metadata[gateway]", "")
        )

        success_url = form.get("success_url", "")
        return {
            "id": sid,
            "object": "checkout.session",
            "url": success_url.replace("{CHECKOUT_SESSION_ID}", sid),
        }

    @app.get("/v1/checkout/sessions/{sid}")
    async def retrieve_session(sid: str):
        state.retrieve_calls[sid] = state.retrieve_calls.get(sid, 0) + 1
        session = state.sessions.get(sid)
        if session is None:
            return JSONResponse(
                {"error": {"type": "invalid_request_error"}}, status_code=404
            )
        return session

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8193, help="Port to serve on (default: 8193)")
    args = parser.parse_args()

    state = StripeState(fixed_id="cs_test_local")
    app = create_fake_stripe(state)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
