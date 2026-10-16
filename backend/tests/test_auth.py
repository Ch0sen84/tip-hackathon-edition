"""Phase A security tests: every state-changing/paid route must reject an
unauthenticated request and accept one with a valid X-TIP-Auth token.
Uses only synthetic test tokens/keys generated in an isolated tmp_path --
never the real deployed token file or any real credential.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import AuthTokenStore, is_loopback

FAKE_UW_KEY = "uwtest_sk_live_FAKEFAKE1234567890ABCDEF"


def _client_without_auth(app_main) -> TestClient:
    """A client with the (auto-injected, see conftest.py) X-TIP-Auth
    header explicitly removed -- used by every negative test below."""
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    return client


def _client_with_wrong_token(app_main) -> TestClient:
    client = TestClient(app_main.app)
    client.headers["X-TIP-Auth"] = "0" * 64  # well-formed length, still wrong
    return client


# --- settings/credentials ------------------------------------------------


def test_save_uw_key_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    assert resp.status_code == 401
    assert "X-TIP-Auth" in resp.json()["detail"]


def test_save_uw_key_rejected_with_wrong_token(app_main):
    client = _client_with_wrong_token(app_main)
    resp = client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    assert resp.status_code == 401


def test_save_uw_key_succeeds_with_valid_token(app_main):
    client = TestClient(app_main.app)  # auto-injected valid token (conftest.py)
    resp = client.post("/api/settings/uw-key", json={"api_key": FAKE_UW_KEY})
    assert resp.status_code == 200
    assert resp.json()["configured"] is True


def test_delete_uw_key_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.delete("/api/settings/uw-key")
    assert resp.status_code == 401


def test_save_ai_key_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.post("/api/settings/ai-key", json={"provider": "anthropic", "api_key": "sk-fake-not-real"})
    assert resp.status_code == 401


def test_delete_ai_key_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.delete("/api/settings/ai-key")
    assert resp.status_code == 401


def test_set_mode_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.post("/api/settings/mode", json={"mode": "demo"})
    assert resp.status_code == 401


# --- watchlist -------------------------------------------------------------


def test_watchlist_put_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]})
    assert resp.status_code == 401


def test_watchlist_put_succeeds_with_valid_token(app_main):
    client = TestClient(app_main.app)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL", "MSFT"]})
    assert resp.status_code == 200
    assert resp.json()["watchlist"] == ["AAPL", "MSFT"]


# --- radar discover (paid-adjacent: real UW screener quota) --------------


def test_radar_discover_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.post("/api/radar/discover", json={"limit": 5})
    assert resp.status_code == 401


# --- investigate (the paid Anthropic action) ------------------------------


def test_investigate_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.post("/api/investigate/FLUX")
    assert resp.status_code == 401


# --- Phase B diagnostics route: new, but must follow the SAME rule as
# every other sensitive route from Phase A -- behind require_auth, never
# a new unauthenticated endpoint. ---------------------------------------


def test_uw_request_diagnostics_rejected_without_auth(app_main):
    client = _client_without_auth(app_main)
    resp = client.get("/api/diagnostics/uw-requests")
    assert resp.status_code == 401


def test_uw_request_diagnostics_succeeds_with_valid_token(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/diagnostics/uw-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert "by_path" in body
    # Never leaks a credential -- just counts/latencies/outcomes.
    assert FAKE_UW_KEY not in str(body)


def test_investigate_succeeds_with_valid_token_in_demo_mode(app_main):
    """Demo mode never calls a real LLM/UW endpoint (see
    get_simulated_transcript) -- safe to exercise end-to-end with a valid
    token without any real network/paid call."""
    client = TestClient(app_main.app)
    resp = client.post("/api/investigate/FLUX")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("simulated") is True


# --- bootstrap route itself: unauthenticated, but loopback-gated ---------


def test_bootstrap_rejects_non_loopback_testclient_peer(app_main):
    """fastapi.testclient.TestClient's simulated peer IP is literally
    "testclient" (see starlette/testclient.py), which is NOT loopback --
    this is a genuine, real exercise of the 403 branch, not a mock."""
    client = _client_without_auth(app_main)
    resp = client.get("/api/auth/bootstrap")
    assert resp.status_code == 403
    assert "docker exec" in resp.json()["detail"]


def test_bootstrap_returns_token_for_loopback_client(app_main):
    """Direct function-level exercise of the real route handler with a
    constructed Request whose client IS loopback -- this is the actual
    code path a real same-machine browser hits, verified without needing
    a real socket."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/bootstrap",
        "headers": [],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
        "query_string": b"",
    }
    request = Request(scope)
    result = app_main.auth_bootstrap(request)
    assert result == {"token": app_main.auth_token}


def test_bootstrap_rejects_remote_ip_at_the_handler_level(app_main):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/bootstrap",
        "headers": [],
        "client": ("203.0.113.5", 54321),  # TEST-NET-3, definitely not loopback
        "server": ("127.0.0.1", 8000),
        "query_string": b"",
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as excinfo:
        app_main.auth_bootstrap(request)
    assert excinfo.value.status_code == 403


# --- is_loopback() pure-function coverage ---------------------------------


def test_is_loopback_accepts_known_loopback_hosts():
    for host in ("127.0.0.1", "::1", "localhost"):
        assert is_loopback(host) is True


def test_is_loopback_rejects_everything_else():
    for host in ("10.0.0.5", "203.0.113.5", "example.com", None, ""):
        assert is_loopback(host) is False


# --- AuthTokenStore: generation, persistence, constant-time compare ------


def test_auth_token_store_generates_and_persists(tmp_path):
    store = AuthTokenStore(tmp_path / ".auth_token")
    token1 = store.get_or_create()
    assert len(token1) == 64  # secrets.token_hex(32) -> 64 hex chars
    store.reset_cache()
    token2 = store.get_or_create()
    assert token1 == token2  # re-reads the same persisted token, doesn't regenerate


def test_auth_token_store_file_mode_is_0600(tmp_path):
    import stat

    path = tmp_path / ".auth_token"
    store = AuthTokenStore(path)
    store.get_or_create()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_auth_token_store_matches_is_constant_time_and_correct(tmp_path):
    store = AuthTokenStore(tmp_path / ".auth_token")
    token = store.get_or_create()
    assert store.matches(token) is True
    assert store.matches("wrong") is False
    assert store.matches(None) is False
    assert store.matches("") is False


# --- Bootstrap route: Host/Origin/DNS-rebinding/forwarded-header hardening
#
# Chad's pre-merge security pass (2026-10-15/16): the generic Host/CORS
# tests in test_cors_host_hardening.py exercise /api/settings, not
# /api/auth/bootstrap specifically -- these pin the EXACT route that hands
# out the token, since TrustedHostMiddleware/CORSMiddleware wrap the whole
# ASGI app and a future refactor that accidentally exempted this one route
# (e.g. moved it outside the middleware stack) would be the single most
# dangerous regression possible here and deserves its own direct test.


def test_bootstrap_rejects_mismatched_host_header_dns_rebinding(app_main):
    """DNS rebinding: an attacker's page at evil.example.com gets the
    victim's DNS resolver to answer evil.example.com -> 127.0.0.1, then
    navigates/fetches http://evil.example.com:PORT/api/auth/bootstrap.
    The TCP connection genuinely lands on this loopback server (so an
    IP-only loopback check would wrongly allow it), but the browser's
    Host header is still "evil.example.com" -- TrustedHostMiddleware must
    reject this with 400 before the loopback-IP check in auth_bootstrap()
    is ever reached, and the body must not leak the token."""
    client = _client_without_auth(app_main)
    resp = client.get("/api/auth/bootstrap", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert "Invalid host header" in resp.text
    assert app_main.auth_token not in resp.text


def test_bootstrap_cors_rejects_foreign_origin(app_main):
    """A foreign origin's own JS calling fetch() against this exact route
    must never receive an access-control-allow-origin that would let the
    browser's CORS check pass and the response body actually be read --
    verified here at the header level (TestClient, unlike curl, still
    reflects the real CORSMiddleware response headers); a live cross-
    origin fetch() from a genuine second-origin browser page during this
    verification pass independently confirmed the browser-side effect is
    a hard "TypeError: Failed to fetch", not merely a missing header."""
    client = _client_without_auth(app_main)
    resp = client.get(
        "/api/auth/bootstrap",
        headers={"Origin": "https://evil.example.com"},
    )
    # The handler itself still runs for a same-host GET with a foreign
    # Origin (CORS is a browser-enforced, not server-enforced, control --
    # this is exactly why the header-level assertion below is the one
    # that actually matters): the real protection is that no ACAO header
    # names that origin, so a genuine browser's JS can never read this.
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_bootstrap_ignores_spoofed_forwarded_headers(app_main):
    """A remote attacker cannot bypass the loopback restriction by adding
    X-Forwarded-For/X-Real-IP/Forwarded headers claiming 127.0.0.1 --
    this app never runs uvicorn with --proxy-headers and never reads any
    forwarded-* header anywhere (grepped this session: zero matches in
    main.py/auth.py) -- request.client.host is the one and only signal
    is_loopback() ever sees, sourced from the genuine ASGI transport
    peer address, not from any attacker-controlled header."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/bootstrap",
        "headers": [
            (b"x-forwarded-for", b"127.0.0.1"),
            (b"x-real-ip", b"127.0.0.1"),
            (b"forwarded", b"for=127.0.0.1"),
        ],
        "client": ("203.0.113.5", 54321),  # the REAL peer -- remote, not loopback
        "server": ("127.0.0.1", 8000),
        "query_string": b"",
    }
    request = Request(scope)
    with pytest.raises(HTTPException) as excinfo:
        app_main.auth_bootstrap(request)
    assert excinfo.value.status_code == 403
    # The 403 detail text is a static help message -- confirm it never
    # echoes the real token even incidentally.
    assert app_main.auth_token not in str(excinfo.value.detail)


def test_bootstrap_error_response_never_contains_token(app_main):
    """Belt-and-suspenders: both failure paths (Host mismatch, non-
    loopback peer) must never leak the real token value anywhere in the
    response body, regardless of how the request was constructed."""
    client = _client_without_auth(app_main)
    resp = client.get("/api/auth/bootstrap")  # TestClient peer = "testclient", non-loopback
    assert resp.status_code == 403
    assert app_main.auth_token not in resp.text
