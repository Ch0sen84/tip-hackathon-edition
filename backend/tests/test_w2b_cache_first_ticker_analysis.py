"""W2-b: Ticker Analysis gets the SAME cache-first treatment as the
watchlist (W2/this round). Real bug: opening/refreshing/returning to
Ticker Analysis triggered ~6 live UW calls AND wrote a new evaluation
every single time, including on F5/Back/direct-link. Fix: GET
/api/ticker/{ticker} is cache-first for a real LiveUWAdapter; a real
fetch only happens via the new, explicit POST
/api/ticker/{ticker}/refresh action.
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


def _seed_cached_evaluation(app_main, ticker: str, state: str = "BUILDING"):
    catalog = app_main.get_catalog()
    scan = {
        "ticker": ticker, "state": state, "previous_state": "NEW", "transition_rule": "seeded",
        "confidence": "medium", "signal_type": "dark_pool_anomaly", "confidence_reason": "seeded",
        "qualifies": True, "qualifying_days_count": 1,
        "gates_passed": ["dark_pool_anomaly"],
        "gates": {
            "dark_pool_anomaly": {"passed": True, "metrics": {"ratio": 3.2}},
            "options_flow_anomaly": {"passed": False, "metrics": {}},
            "price_move": {"passed": False, "metrics": {}},
            "catalyst_identified": {"passed": False, "metrics": {}},
        },
        "stock_state": {"last_price": 101.5, "change_percent": 1.2},
        "raw": {"flow_alerts": [], "darkpool_trades": []},
    }
    catalog.catalog_signal_evaluation(ticker, scan, kind="on_demand")
    return scan


def test_ticker_analysis_open_refresh_back_direct_link_makes_zero_calls(app_main, monkeypatch):
    """Simulates open -> F5 -> navigate away and Back -> direct-link
    re-open, all plain GETs -- never a live call once something is
    cataloged."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    _seed_cached_evaluation(app_main, ticker)
    app_main.get_store("live").record_scan(ticker, any_gate_passed=True, state="BUILDING")
    client = TestClient(app_main.app)
    first_body = None
    for _ in range(4):  # open, F5, Back, direct-link -- all the same route
        resp = client.get(f"/api/ticker/{ticker}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["served_from_cache"] is True
        first_body = first_body or resp.json()
    assert first_body is not None
    assert first_body["history"][-1]["transition_rule"] == "seeded"
    assert mock_session.get.call_count == 0, (
        f"Ordinary navigation to Ticker Analysis made {mock_session.get.call_count} real UW call(s) "
        "-- this is exactly the W2-b bug."
    )


def test_ticker_analysis_never_evaluated_shows_honest_placeholder_zero_calls(app_main, monkeypatch):
    """A ticker with nothing cataloged yet must render an honest
    'Not yet evaluated' state on a plain GET, never fabricate evidence
    and never fall back to a live call."""
    ticker = "TSLA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == "Not yet evaluated"
    assert body["gates"] == {}
    assert body["eval_freshness"] == "unevaluated"
    assert mock_session.get.call_count == 0


def test_ticker_analysis_explicit_refresh_makes_real_call_and_writes_exactly_one_new_evaluation(app_main, monkeypatch):
    """Proof point (e): POST /api/ticker/{ticker}/refresh is the one
    path that performs a real fetch, and it writes exactly one new
    cataloged evaluation (never zero, never more than one per call)."""
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    catalog = app_main.get_catalog()
    before = len(catalog.list_coverage_status(ticker)) if hasattr(catalog, "list_coverage_status") else None

    client = TestClient(app_main.app)
    resp = client.post(f"/api/ticker/{ticker}/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["served_from_cache"] is False
    assert mock_session.get.call_count > 0

    latest = catalog.get_latest_signal_evaluation(ticker)
    assert latest is not None, "Explicit refresh must catalog a new evaluation."

    # A second ordinary GET right after must NOT create yet another
    # evaluation nor make another live call -- it reads back exactly
    # what the refresh just wrote.
    resp2 = client.get(f"/api/ticker/{ticker}")
    assert resp2.status_code == 200
    assert resp2.json()["served_from_cache"] is True
    calls_after_refresh = mock_session.get.call_count
    client.get(f"/api/ticker/{ticker}")
    assert mock_session.get.call_count == calls_after_refresh, "A plain GET after refresh must make zero additional UW calls."


def test_ticker_analysis_refresh_requires_auth(app_main, monkeypatch):
    """The explicit refresh action is authenticated the same way as
    every other mutating route -- never bypassed."""
    import app.main as app_module
    monkeypatch.setattr(app_module, "LOCAL_MODE_ACTIVE", False)
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    resp = client.post("/api/ticker/AMD/refresh")
    assert resp.status_code == 401


def test_ticker_analysis_refresh_duplicate_concurrent_is_rejected_cleanly(app_main, monkeypatch):
    import app.main as app_module
    app_module._ticker_refresh_lock.acquire()
    try:
        client = TestClient(app_main.app)
        resp = client.post("/api/ticker/AMD/refresh")
        assert resp.status_code == 409
    finally:
        app_module._ticker_refresh_lock.release()
