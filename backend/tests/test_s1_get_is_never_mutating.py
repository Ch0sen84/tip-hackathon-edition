"""S1 security fix regression tests (highest-priority item this round).

Real finding: GET /api/radar?refresh=true performed a real, costly UW
scan with NO authentication and NO cross-origin protection -- a foreign
page can trigger this via a no-cors fetch/<img> request, which never
carries an Origin/Sec-Fetch-Site header this app could even inspect, so
no amount of auth-gating GET could have closed the hole. The only
structurally safe fix is removing the side effect from GET entirely:
GET /api/radar no longer accepts a `refresh` parameter at all (not
"ignores it", literally absent from the route signature), and the same
treatment was applied to GET /api/ticker/{ticker} (W2-b, same bug
class). POST /api/radar/refresh and POST /api/ticker/{ticker}/refresh
remain the ONLY routes that can trigger a real scan, both already
behind require_auth + a concurrency lock.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter


def _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist):
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


def test_get_radar_refresh_true_query_param_makes_zero_uw_calls(app_main, monkeypatch):
    """The exact real-world attack payload: GET /api/radar?refresh=true.
    Before the S1 fix this triggered a real scan for every watchlist
    ticker. The route no longer even has a `refresh` parameter, so this
    query string is inert -- FastAPI never binds it to anything."""
    tickers = ["AMD", "MSFT"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    client = TestClient(app_main.app)
    resp = client.get("/api/radar?refresh=true")
    assert resp.status_code == 200
    assert mock_session.get.call_count == 0, (
        "GET /api/radar?refresh=true made a real UW call -- this is exactly the S1 security "
        "regression (an unauthenticated, cross-origin-triggerable GET costing real money)."
    )
    assert resp.json()["served_from_cache"] is True


def test_get_radar_cross_site_sec_fetch_header_still_makes_zero_calls(app_main, monkeypatch):
    """Simulates the real attack vector: a foreign page's no-cors
    fetch/<img> request, which the browser tags Sec-Fetch-Site: cross-
    site (when it sends Fetch Metadata at all) and never carries a
    matching Origin. Confirms the fix holds structurally -- not because
    an Origin/Sec-Fetch-Site check rejected it (GET has none and never
    will, since a no-cors request doesn't let the page read the
    response either way), but because GET simply has no code path left
    that can trigger a live call, regardless of headers."""
    tickers = ["AMD", "MSFT"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    client = TestClient(app_main.app)
    resp = client.get(
        "/api/radar?refresh=true",
        headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"},
    )
    # A mismatched Origin is rejected by require_auth on MUTATING routes,
    # but GET /api/radar has no require_auth dependency at all (by design
    # -- it's a plain page-load read) and must still never call UW.
    assert mock_session.get.call_count == 0
    assert resp.status_code == 200


def test_get_ticker_analysis_refresh_query_param_is_inert(app_main, monkeypatch):
    """Same bug class, Ticker Analysis (W2-b): a GET must never accept a
    refresh trigger via query string either."""
    tickers = ["AMD"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/AMD?refresh=true")
    assert resp.status_code == 200
    assert mock_session.get.call_count == 0
    assert resp.json()["served_from_cache"] is True


def test_post_radar_refresh_still_works_and_is_the_sole_mutating_path(app_main, monkeypatch):
    """Proof point (d) from the brief: POST /api/radar/refresh still
    performs a real refresh correctly after the S1 fix."""
    tickers = ["AMD", "MSFT"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    resp = client.post("/api/radar/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["served_from_cache"] is False
    assert mock_session.get.call_count > 0


def test_post_radar_refresh_cross_origin_is_rejected(app_main, monkeypatch):
    """The sole mutating path remains properly authenticated/origin-
    checked -- a mismatched Origin on the POST route is rejected
    outright by require_auth, unweakened by the S1 fix."""
    client = TestClient(app_main.app)
    resp = client.post("/api/radar/refresh", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_no_other_get_route_in_main_accepts_a_refresh_or_similar_mutating_bool_param():
    """Structural audit (S1 requirement: audit EVERY other GET endpoint
    for the same class of bug). Walks the real FastAPI route table and
    asserts no GET route declares a bool/str query parameter literally
    named one of the known trigger-style names used elsewhere in this
    app for an explicit mutating action."""
    import inspect

    from app.main import app

    suspicious_names = {"refresh", "run", "trigger", "force", "scan", "execute"}
    offenders = []
    for route in app.router.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            sig = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue
        for name in sig.parameters:
            if name.lower() in suspicious_names:
                offenders.append((route.path, name))
    assert offenders == [], (
        f"GET route(s) with a mutating-looking query parameter found: {offenders}. "
        "Per the S1 fix, state-changing/costly logic must never be reachable from GET."
    )
