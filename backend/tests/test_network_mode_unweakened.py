"""Refinement #4: Network Mode must be provably unweakened by the Local
Mode redesign. This file never touches/relaxes a single pre-existing
Phase A / QA-remediation D1 test (those stay in test_auth.py /
test_pairing_bootstrap.py / test_cors_host_hardening.py, completely
unmodified -- re-run as part of the same `pytest` invocation as this
file, see FINAL REPORT for the full before/after count).

This file adds the ONE new proof Chad's brief calls for: with
LOCAL_MODE_ACTIVE forced off (simulating a real non-loopback BIND_HOST
deployment), the new Local-Mode-only bypass path is genuinely inert --
a request that presents ONLY Sec-Fetch-Site: same-origin, with no
token, is rejected exactly like it always was.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client_no_token(app_main) -> TestClient:
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    return client


def test_network_mode_bypass_path_is_inert_without_a_token(app_main, monkeypatch):
    """The exact proof Chad's brief names: force Network Mode (as if
    BIND_HOST were a real non-loopback value) and confirm that
    Sec-Fetch-Site: same-origin ALONE, even with a perfectly matching
    Origin, is NOT sufficient -- the token is still required, end to
    end, through the real require_auth dependency every sensitive route
    uses."""
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    from app.main import ALLOWED_ORIGIN

    client = _client_no_token(app_main)
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Sec-Fetch-Site": "same-origin", "Origin": ALLOWED_ORIGIN},
    )
    assert resp.status_code == 401
    assert "X-TIP-Auth" in resp.json()["detail"]


def test_network_mode_still_works_normally_with_a_valid_token(app_main, monkeypatch):
    """Sanity companion to the test above: forcing Network Mode must not
    break the actual token path either -- a valid token still succeeds,
    exactly as before this round's changes."""
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    client = TestClient(app_main.app)  # auto-injected valid token
    resp = client.put("/api/settings/watchlist", json={"watchlist": ["AAPL"]})
    assert resp.status_code == 200


def test_network_mode_every_sensitive_route_rejects_sec_fetch_site_alone(app_main, monkeypatch):
    """Item #6's full sensitive-route list, re-audited specifically for
    the inert-bypass proof -- not just the one watchlist route above."""
    from app.main import ALLOWED_ORIGIN
    from .test_local_mode_fetch_metadata import SENSITIVE_MUTATING_REQUESTS, _send

    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    for method, path, body in SENSITIVE_MUTATING_REQUESTS:
        client = _client_no_token(app_main)
        client.headers["Sec-Fetch-Site"] = "same-origin"
        client.headers["Origin"] = ALLOWED_ORIGIN
        resp = _send(client, method, path, body)
        assert resp.status_code == 401, (
            f"{method.upper()} {path} unexpectedly accepted Sec-Fetch-Site alone in Network Mode"
        )


def test_network_mode_origin_validation_still_applies(app_main, monkeypatch):
    """Refinement #2 explicitly applies in BOTH modes -- confirm a
    forged Origin is rejected in Network Mode too, even with a valid
    token (Origin validation runs before the token check)."""
    monkeypatch.setattr(app_main, "LOCAL_MODE_ACTIVE", False)
    client = TestClient(app_main.app)  # valid token
    resp = client.put(
        "/api/settings/watchlist",
        json={"watchlist": ["AAPL"]},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
