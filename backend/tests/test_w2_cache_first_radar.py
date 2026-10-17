"""W2 (highest-priority item this round): real bug was "an ordinary page
load with the scheduler Disabled made 15 real UW API calls for 5
watchlist tickers" (3 calls/ticker -- flow alerts, dark-pool trades,
stock state -- times 5 tickers). Fix: GET /api/radar is cache-first for
a real LiveUWAdapter; a real live fetch only happens via the explicit
POST /api/radar/refresh action.

THE SINGLE MOST IMPORTANT TEST IN THIS FILE is
test_ordinary_page_load_makes_exactly_zero_uw_calls below: starts the
real FastAPI app via TestClient (not a unit-level mock of _run_scan),
seeds real cached signal_evaluations rows through the catalog, swaps
LiveUWAdapter's underlying requests.Session for a MagicMock that would
record every call(), drives a real GET /api/radar, and asserts that
mock's call_count is exactly 0 afterward -- not "fewer calls", not
"approximately none", exactly zero.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter


def _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist):
    """Builds a REAL LiveUWAdapter (same class production code path uses)
    whose UWRestClient.session is a MagicMock -- any call reaching
    requests.Session.get would be recorded on this mock, so asserting
    mock.get.call_count == 0 is a genuine proof of zero real HTTP calls,
    not an assumption about internal call graph."""
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


def _seed_cached_evaluation(app_main, monkeypatch, ticker: str, state: str = "BUILDING"):
    """Writes one real signal_evaluations row through the SAME catalog
    the production code reads from (IngestionCatalog.catalog_signal_
    evaluation) -- this is "the existing SQLite catalog already has
    data", never a fabricated shortcut."""
    catalog = app_main.get_catalog()
    scan = {
        "ticker": ticker, "state": state, "confidence": "medium", "signal_type": "dark_pool_anomaly",
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


@pytest.fixture
def watchlist_tickers():
    return ["AMD", "MSFT", "NVDA", "TSLA", "SPY"]  # 5 tickers, matches the reported bug exactly


def test_ordinary_page_load_makes_exactly_zero_uw_calls(app_main, monkeypatch, watchlist_tickers):
    """THE single most important test in this round (per Chad's brief):
    seed cached data for every watchlist ticker, drive a real GET
    /api/radar through the real app, assert the mock UW session's
    call_count is exactly 0 -- not reduced, not mostly-zero, exactly 0."""
    app_main._settings_store.set_watchlist(watchlist_tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist_tickers)
    for t in watchlist_tickers:
        _seed_cached_evaluation(app_main, monkeypatch, t)

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["served_from_cache"] is True
    assert body["scanned"] == len(watchlist_tickers)

    assert mock_session.get.call_count == 0, (
        f"Ordinary GET /api/radar made {mock_session.get.call_count} real UW HTTP call(s) -- "
        "this is exactly the W2 bug (15 calls for 5 watchlist tickers on a plain page load)."
    )


def test_ordinary_page_load_still_zero_calls_with_no_cache_at_all(app_main, monkeypatch, watchlist_tickers):
    """Even a brand-new watchlist with NOTHING cataloged yet must never
    fall back to a live fetch on a plain GET -- tickers render as
    'unevaluated', never trigger a surprise live call."""
    app_main._settings_store.set_watchlist(watchlist_tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist_tickers)

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # UX remediation item 2 (2026-10-16): never-evaluated tickers must be
    # shown, not hidden -- "highest" correctly stays empty (nothing here
    # has ever qualified), but every watchlist ticker now appears in
    # "other", honestly labeled "Not yet evaluated" rather than silently
    # dropped by tip_evidence.present()'s qualifying filter.
    assert body["highest"] == []
    assert {e["ticker"] for e in body["other"]} == set(watchlist_tickers)
    for e in body["other"]:
        assert e["headline"] == "Not yet evaluated"
    assert body["scanned"] == len(watchlist_tickers)
    assert body["served_from_cache"] is True
    assert mock_session.get.call_count == 0
    # D4/D5 fix (ux-nav-watchlist-reliability round 9): a genuinely
    # never-evaluated ticker must get an honest None state -- NOT the
    # literal string "NEW", which is a real signal_engine lifecycle
    # state implying an evaluation actually happened. Also explicitly
    # marked not_evaluated so the frontend never counts it as a
    # qualifying signal just because it lives in the "other" list.
    for e in body["other"]:
        assert e["state"] is None, f"never-evaluated {e['ticker']} must not be labeled a real state, got {e['state']!r}"
        assert e["not_evaluated"] is True
    # watchlist_size/evaluated_count must distinguish total watchlist
    # size from how many were ACTUALLY evaluated -- here, zero.
    assert body["watchlist_size"] == len(watchlist_tickers)
    assert body["evaluated_count"] == 0


def test_never_evaluated_ticker_with_a_real_prior_lifecycle_state_is_unaffected(app_main, monkeypatch, watchlist_tickers):
    """D4/D5 fix must only change the GENUINELY-never-evaluated case.
    A ticker whose evaluation cache row doesn't exist (e.g. evicted) but
    which DOES have a real prior lifecycle record in QualifyingDaysStore
    (store.current_state returns a real value) must keep showing that
    real state honestly -- never forced to None either."""
    app_main._settings_store.set_watchlist(watchlist_tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist_tickers)
    ticker = watchlist_tickers[0]
    # Give this ticker a real lifecycle record without a cataloged scan.
    app_main.get_store().record_scan(ticker, any_gate_passed=True, state="BUILDING")

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    entry = next(e for e in (body["highest"] + body["other"]) if e["ticker"] == ticker)
    assert entry["state"] == "BUILDING"
    assert not entry.get("not_evaluated")


def test_repeated_ordinary_loads_navigate_back_and_auth_state_change_stay_zero(app_main, monkeypatch, watchlist_tickers):
    """Simulates open -> refresh -> navigate away and back -> reload
    (as if auth state changed) -- all plain GETs, never a live call."""
    app_main._settings_store.set_watchlist(watchlist_tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist_tickers)
    for t in watchlist_tickers:
        _seed_cached_evaluation(app_main, monkeypatch, t)
    client = TestClient(app_main.app)
    for _ in range(5):
        resp = client.get("/api/radar")
        assert resp.status_code == 200
    assert mock_session.get.call_count == 0


def test_explicit_refresh_data_action_does_make_real_calls(app_main, monkeypatch, watchlist_tickers):
    """The explicit manual action (POST /api/radar/refresh) is the ONE
    path allowed to perform a real fetch -- this proves it actually
    does (contrast with the zero-calls tests above)."""
    app_main._settings_store.set_watchlist(watchlist_tickers[:2])  # keep it small/fast
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, watchlist_tickers[:2])
    mock_session.get.return_value = MagicMock(status_code=200, ok=True, text="{}", headers={},
                                                json=MagicMock(return_value={"data": []}))
    client = TestClient(app_main.app)
    resp = client.post("/api/radar/refresh")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["served_from_cache"] is False
    assert mock_session.get.call_count > 0, "Explicit Refresh Data must make real UW calls -- that's its whole purpose."


def test_duplicate_concurrent_refresh_is_rejected_cleanly(app_main, monkeypatch, watchlist_tickers):
    """A second Refresh Data click while one is in flight must get a
    clean 409, never silently double-fire or queue."""
    import app.main as app_module
    app_module._radar_refresh_lock.acquire()  # simulate a refresh already in progress
    try:
        client = TestClient(app_main.app)
        resp = client.post("/api/radar/refresh")
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]
    finally:
        app_module._radar_refresh_lock.release()


def test_refresh_requires_auth_same_path_as_every_other_mutating_route(app_main, monkeypatch):
    """Never bypasses auth -- same require_auth dependency as every
    other mutating route. In Network Mode (non-loopback), a missing/
    invalid X-TIP-Auth token must still 401."""
    import app.main as app_module
    monkeypatch.setattr(app_module, "LOCAL_MODE_ACTIVE", False)
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)  # TestClient auto-injects a valid token by default
    resp = client.post("/api/radar/refresh")
    assert resp.status_code == 401


def test_cached_eval_freshness_distinguishes_current_stale_and_unevaluated(app_main, monkeypatch):
    """eval_freshness is structurally distinct from signal lifecycle
    `state` and `confidence` -- never conflated. Verifies all three
    states render distinctly in one real /api/radar response."""
    tickers = ["CURW", "STLW", "NVRW"]
    app_main._settings_store.set_watchlist(tickers)
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, tickers)
    _seed_cached_evaluation(app_main, monkeypatch, "CURW")  # current (just cataloged, retrieved_at=now)

    # STLW: cataloged, but stale (retrieved_at far in the past)
    catalog = app_main.get_catalog()
    stale_scan = {
        "ticker": "STLW", "state": "BUILDING", "confidence": "medium", "signal_type": "dark_pool_anomaly",
        "gates_passed": ["dark_pool_anomaly"],
        "gates": {
            "dark_pool_anomaly": {"passed": True, "metrics": {"ratio": 2.0}},
            "options_flow_anomaly": {"passed": False, "metrics": {}},
            "price_move": {"passed": False, "metrics": {}},
            "catalyst_identified": {"passed": False, "metrics": {}},
        },
        "stock_state": {"last_price": 50.0, "change_percent": 0.1},
        "raw": {"flow_alerts": [], "darkpool_trades": []},
    }
    catalog.catalog_signal_evaluation("STLW", stale_scan, kind="on_demand", retrieved_at="2020-01-01T00:00:00+00:00")
    # NVRW: never cataloged at all -> unevaluated

    client = TestClient(app_main.app)
    resp = client.get("/api/radar")
    body = resp.json()
    by_ticker = {e["ticker"]: e for e in body["highest"] + body["other"]}
    # UX remediation item 2 (2026-10-16): NVRW was never cataloged at
    # all -- it must still be SHOWN (in "other", honestly labeled), not
    # silently dropped just because it never qualified either.
    assert "NVRW" in by_ticker
    assert by_ticker["NVRW"]["headline"] == "Not yet evaluated"
    assert body["scanned"] == 3
    assert by_ticker["CURW"]["eval_freshness"] == "current"
    assert by_ticker["STLW"]["eval_freshness"] == "stale"
    assert mock_session.get.call_count == 0
