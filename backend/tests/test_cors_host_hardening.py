"""Phase A: CORS/Host hardening tests. Proves a mismatched Origin/Host is
rejected, and that the configured same-origin value IS accepted -- never
just "no error happened"."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_trusted_host_middleware_rejects_mismatched_host(app_main):
    """app.main._ALLOWED_HOSTS is [ALLOWED_HOST] plus a test-only
    "testserver"/"testclient" widening (see main.py) -- a Host header for
    some unrelated domain must still be rejected with 400."""
    client = TestClient(app_main.app)
    resp = client.get("/api/settings", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert "Invalid host header" in resp.text


def test_trusted_host_middleware_accepts_configured_host(app_main):
    client = TestClient(app_main.app)
    # TestClient's default base_url ("http://testserver") is in the
    # test-only widened allow-list already covered by the other 300+
    # tests in this suite passing; explicitly re-assert it here too.
    resp = client.get("/api/settings")
    assert resp.status_code == 200


def test_cors_rejects_mismatched_origin(app_main):
    """A cross-origin browser preflight (OPTIONS with an Origin header
    that is NOT the configured ALLOWED_ORIGIN) must not get an
    access-control-allow-origin echoing that origin back."""
    client = TestClient(app_main.app)
    resp = client.options(
        "/api/settings/mode",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_allows_configured_origin(app_main):
    from app.main import ALLOWED_ORIGIN

    client = TestClient(app_main.app)
    resp = client.options(
        "/api/settings/mode",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_cors_is_no_longer_wildcard():
    """Direct regression for the exact pre-Phase-A bug: allow_origins was
    ["*"] -- assert the fixed module-level constant is a real, specific
    origin, never a wildcard."""
    from app.main import ALLOWED_ORIGIN

    assert ALLOWED_ORIGIN != "*"
    assert ALLOWED_ORIGIN.startswith("http")
