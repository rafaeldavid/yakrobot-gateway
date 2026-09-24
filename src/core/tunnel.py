"""Expose the MCP gateway over a public tunnel — ngrok or Cloudflare Tunnel.

Provider is chosen via ``start_tunnel(provider=...)`` or the ``TUNNEL_PROVIDER`` env var
(default ``ngrok``):

- ``ngrok``      — pyngrok on a static free domain. Needs ``NGROK_AUTHTOKEN`` +
  ``NGROK_DOMAIN``.
- ``cloudflare`` — the ``cloudflared`` binary (install separately:
  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/).
  Two modes:
    * **stable** — set ``CLOUDFLARE_TUNNEL_TOKEN`` (+ ``CLOUDFLARE_DOMAIN`` for the printed
      URL) to run a named, dashboard-managed tunnel on your own hostname.
    * **quick**  — no token → an ephemeral ``https://<random>.trycloudflare.com`` URL, no
      account required.
"""

import atexit
import os
import re
import shutil
import subprocess
import threading
from queue import Empty, Queue

_CLOUDFLARE_INSTALL = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)
_TRYCLOUDFLARE_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def start_tunnel(port: int = 8000, provider: str | None = None) -> str:
    """Open a public tunnel to ``port`` and return the public HTTPS URL.

    ``provider`` is ``"ngrok"`` or ``"cloudflare"`` (defaults to the ``TUNNEL_PROVIDER``
    env var, then ``"ngrok"``).
    """
    provider = (provider or os.getenv("TUNNEL_PROVIDER") or "ngrok").lower()
    if provider == "ngrok":
        return _start_ngrok(port)
    if provider == "cloudflare":
        return _start_cloudflare(port)
    raise RuntimeError(
        f"Unknown tunnel provider {provider!r} (expected 'ngrok' or 'cloudflare')"
    )


def _start_ngrok(port: int) -> str:
    """Open an ngrok tunnel on a static domain. Needs NGROK_AUTHTOKEN + NGROK_DOMAIN."""
    from pyngrok import ngrok

    auth_token = os.getenv("NGROK_AUTHTOKEN")
    domain = os.getenv("NGROK_DOMAIN")
    if not auth_token:
        raise RuntimeError("NGROK_AUTHTOKEN not set in .env")
    if not domain:
        raise RuntimeError(
            "NGROK_DOMAIN not set in .env (claim at https://dashboard.ngrok.com/domains)"
        )

    ngrok.set_auth_token(auth_token)
    ngrok.connect(addr=str(port), proto="http", hostname=domain)
    return f"https://{domain}"


def _start_cloudflare(port: int) -> str:
    """Open a Cloudflare Tunnel via the ``cloudflared`` binary.

    Named (stable) tunnel if CLOUDFLARE_TUNNEL_TOKEN is set; otherwise an ephemeral
    quick tunnel on a ``*.trycloudflare.com`` URL.
    """
    if shutil.which("cloudflared") is None:
        raise RuntimeError(f"cloudflared not found on PATH — install it: {_CLOUDFLARE_INSTALL}")

    token = os.getenv("CLOUDFLARE_TUNNEL_TOKEN")
    if token:
        domain = os.getenv("CLOUDFLARE_DOMAIN")
        if not domain:
            raise RuntimeError(
                "CLOUDFLARE_DOMAIN not set — the public hostname mapped to this tunnel in "
                f"the Cloudflare dashboard (its ingress must point at http://127.0.0.1:{port} "
                f"— not localhost, see _start_cloudflare below)"
            )
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "run", "--token", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _terminate_on_exit(proc)
        return f"https://{domain}"

    # Quick tunnel — no account; cloudflared prints an ephemeral URL to stderr.
    #
    # 127.0.0.1, not localhost: on macOS (and any dual-stack host whose resolver
    # prefers IPv6) `localhost` resolves to ::1 first, while uvicorn's default
    # --host binds IPv4 only. cloudflared then dials a dead address and the edge
    # answers 404 with the request never reaching the gateway — a failure that
    # looks like a broken tunnel rather than a wrong origin, because cloudflared
    # still reports "Registered tunnel connection" and looks entirely healthy.
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _terminate_on_exit(proc)
    return _await_trycloudflare_url(proc)


def _await_trycloudflare_url(proc: subprocess.Popen, timeout: float = 30.0) -> str:
    """Scan cloudflared's stderr for the quick-tunnel URL, then keep draining the pipe."""
    found: "Queue[str | None]" = Queue()

    def _scan() -> None:
        url_seen = False
        for line in proc.stderr:  # keep reading so cloudflared's stderr pipe never blocks
            if not url_seen:
                match = _TRYCLOUDFLARE_RE.search(line)
                if match:
                    url_seen = True
                    found.put(match.group(0))
        if not url_seen:
            found.put(None)  # stream ended before a URL appeared

    threading.Thread(target=_scan, daemon=True).start()
    try:
        url = found.get(timeout=timeout)
    except Empty:
        proc.terminate()
        raise RuntimeError(f"cloudflared did not report a quick-tunnel URL within {timeout:.0f}s")
    if url is None:
        raise RuntimeError("cloudflared exited before reporting a tunnel URL")
    return url


def _terminate_on_exit(proc: subprocess.Popen) -> None:
    """Tear the cloudflared child down when this process exits."""

    def _kill() -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    atexit.register(_kill)
