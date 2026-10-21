"""Local Mode / Network Mode (Fetch Metadata auth redesign) tests.

Covers Chad's refinement items #1, #2, #3, #4, #6, #7 at the backend
level (real browser proof is in frontend/test_local_mode_real_browser.mjs
-- see that file + the FINAL REPORT for what TestClient genuinely cannot
simulate). Every test here uses only synthetic test state (isolated
tmp_path stores via the app_main fixture) -- never a real deployed
token/settings file.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client_no_token(app_main) -> TestClient:
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    return client


# --- (a) Local Mode: genuine same-origin mutating request, zero token ---


def test_local_mode_same_origin_mutation_succeeds_with_zero_token(app_main):
    assert app_main.LOCAL_MODE_ACTIVE is True  # default BIND_HOST=127.0.0.1 in tests
    client = _client_no_token(app_main)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 200
    assert resp.json()["watchlist"] == ["AAPL"]


def test_local_mode_same_origin_mutation_succeeds_even_with_matching_origin_present(app_main):
    from app.main import ALLOWED_ORIGIN

    client = _client_no_token(app_main)
    resp = client.post(
        "/api/settings/mode",
        json={"mode": "demo"},
        headers={"Sec-Fetch-Site": "same-origin", "Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 200


# --- (b) cross-site Sec-Fetch-Site rejected even with correct Host/Origin


def test_local_mode_cross_site_rejected_even_with_correct_origin(app_main):
    from app.main import ALLOWED_ORIGIN

    client = _client_no_token(app_main)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "cross-site", "Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 401  # falls through to the (unmet) token requirement


# --- (c) same-site (not same-origin) rejected -----------------------------


def test_local_mode_same_site_rejected(app_main):
    client = _client_no_token(app_main)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]}, headers={"Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 401


# --- (d) `none` Sec-Fetch-Site on a mutating route rejected ---------------
# (no narrow exception implemented for any mutating route -- see
# require_auth's docstring in app/main.py for why none was needed)


def test_local_mode_none_sec_fetch_site_rejected_on_mutating_route(app_main):
    client = _client_no_token(app_main)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]}, headers={"Sec-Fetch-Site": "none"})
    assert resp.status_code == 401


# --- (e) missing Fetch Metadata entirely falls back to requiring the token
# (fail-closed "unsupported browser" case) --------------------------------


def test_local_mode_missing_sec_fetch_site_entirely_requires_token(app_main):
    client = _client_no_token(app_main)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]})  # no Sec-Fetch-Site header at all
    assert resp.status_code == 401

    # ... and a valid token still works for this exact "unsupported
    # browser" shape, proving the fallback is genuinely usable, not a
    # dead end.
    client2 = TestClient(app_main.app)  # auto-injected valid token (conftest.py)
    resp2 = client2.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]})
    assert resp2.status_code == 200


# --- (f) forged/mismatched Origin rejected even when Sec-Fetch-Site looks
# correct -------------------------------------------------------------------


def test_forged_origin_rejected_even_with_same_origin_sec_fetch_site(app_main):
    client = _client_no_token(app_main)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
    assert "evil.example.com" in resp.text


def test_forged_origin_rejected_with_valid_token_too(app_main):
    """Refinement #2: Origin validation is NOT a replacement/condition on
    the token check -- a forged Origin is rejected even for a client that
    ALSO has a perfectly valid X-TIP-Auth token."""
    client = TestClient(app_main.app)  # has the valid auto-injected token
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403


# --- (g) NO Origin header at all + non-same-origin Sec-Fetch-Site is still
# rejected -- missing Origin never substitutes for proof ------------------


def test_missing_origin_does_not_substitute_for_proof_with_cross_site(app_main):
    client = _client_no_token(app_main)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "cross-site"},  # and deliberately NO Origin header
    )
    assert resp.status_code == 401


def test_missing_origin_and_missing_sec_fetch_site_is_still_rejected(app_main):
    """The purest 'no evidence at all' case -- neither header present,
    no token. Must be rejected, not treated as implicitly trustworthy."""
    client = _client_no_token(app_main)
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]})
    assert resp.status_code == 401


# --- (i) every sensitive route from item #6, individually, in Local Mode -


SENSITIVE_MUTATING_REQUESTS = [
    ("put", "/api/settings/watchlist", {"watchlist": ["AAPL"]}),
    ("post", "/api/settings/mode", {"mode": "demo"}),
    ("post", "/api/settings/uw-key", {"api_key": "uwtest_sk_live_FAKEFAKE1234567890ABCDEF"}),
    ("post", "/api/settings/uw-key/test", {}),
    ("delete", "/api/settings/uw-key", None),
    ("post", "/api/settings/ai-key", {"provider": "anthropic", "api_key": "fake-not-real"}),
    ("delete", "/api/settings/ai-key", None),
    ("post", "/api/scheduler/run-once", {"confirm": True}),
    ("post", "/api/scheduler/mode", {"mode": "dry_run", "confirm": "dry_run"}),
    ("post", "/api/radar/discover", {"limit": 5}),
    ("post", "/api/investigate/FLUX", None),
]


def _send(client, method, path, body):
    fn = getattr(client, method)
    if body is None:
        return fn(path)
    return fn(path, json=body)


def test_every_sensitive_route_local_mode_same_origin_bypasses_token(app_main):
    assert app_main.LOCAL_MODE_ACTIVE is True
    for method, path, body in SENSITIVE_MUTATING_REQUESTS:
        client = _client_no_token(app_main)
        client.headers["Sec-Fetch-Site"] = "same-origin"
        resp = _send(client, method, path, body)
        assert resp.status_code != 401, f"{method.upper()} {path} unexpectedly required a token in Local Mode"


def test_every_sensitive_route_local_mode_none_sec_fetch_site_still_requires_token(app_main):
    assert app_main.LOCAL_MODE_ACTIVE is True
    for method, path, body in SENSITIVE_MUTATING_REQUESTS:
        client = _client_no_token(app_main)
        client.headers["Sec-Fetch-Site"] = "none"
        resp = _send(client, method, path, body)
        assert resp.status_code == 401, f"{method.upper()} {path} unexpectedly bypassed the token with Sec-Fetch-Site: none"


def test_every_sensitive_route_local_mode_cross_site_still_requires_token(app_main):
    for method, path, body in SENSITIVE_MUTATING_REQUESTS:
        client = _client_no_token(app_main)
        client.headers["Sec-Fetch-Site"] = "cross-site"
        resp = _send(client, method, path, body)
        assert resp.status_code == 401, f"{method.upper()} {path} unexpectedly bypassed the token with Sec-Fetch-Site: cross-site"


# --- /api/auth/mode indicator route ---------------------------------------


def test_auth_mode_route_reports_local_when_bind_host_loopback(app_main):
    client = _client_no_token(app_main)
    resp = client.get("/api/auth/mode")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "local", "local_mode_active": True}


def test_auth_mode_route_is_unauthenticated(app_main):
    """Reachable before any token exists -- a brand-new install with no
    auth state at all must still be able to call this."""
    client = _client_no_token(app_main)
    client.headers.pop("Sec-Fetch-Site", None)
    resp = client.get("/api/auth/mode")
    assert resp.status_code == 200
