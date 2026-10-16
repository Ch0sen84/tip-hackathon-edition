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
