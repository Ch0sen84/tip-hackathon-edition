"""UX round (2026-10-16 round 5), item 1/2 validation requirement: the
new Market Intelligence "View Details" action must reuse the EXISTING
W2-b cache-first GET /api/ticker/{ticker} route (same navigateToTicker()
path Market Radar's own Investigate buttons already use) -- never a new
route, and never a real UW call just from viewing/navigating. Exercises
the exact round trip Chad asked be real-browser verified too: open
Market Intelligence -> View Details on a cataloged candidate -> navigate
away -> back, all as plain GETs against a real (mocked-session)
LiveUWAdapter, same zero-call assertion pattern
test_w2b_cache_first_ticker_analysis.py already established.
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


def test_market_intel_view_details_then_navigate_away_and_back_makes_zero_uw_calls(app_main, monkeypatch):
    ticker = "NVDA"
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    _seed_cached_evaluation(app_main, ticker)
    catalog = app_main.get_catalog()
    catalog.catalog_discovery_snapshot(
        filters={}, candidates=[{"ticker": ticker, "net_premium": 1_000_000, "is_index": False}],
        snapshot_at="2026-10-16T12:00:00+00:00",
    )
    client = TestClient(app_main.app)

    # 1. Open Market Intelligence (renders the new "View Details" row).
    mw = client.get("/api/radar/market-wide")
    assert mw.status_code == 200
    assert any(c["ticker"] == ticker for c in mw.json()["candidates"])

    # 2. Click "View Details" -> GET /api/ticker/{ticker} (cache-first,
    # the SAME route/adapter-call-shape W2-b already fixed -- the new
    # button is a presentation-layer reuse, not a new backend path).
    detail = client.get(f"/api/ticker/{ticker}")
    assert detail.status_code == 200
    assert detail.json()["served_from_cache"] is True

    # 3. Navigate away (Market Intelligence again -- the "back" leg).
    mw2 = client.get("/api/radar/market-wide")
    assert mw2.status_code == 200

    # 4. Back to Ticker Analysis (another plain GET, same route).
    detail2 = client.get(f"/api/ticker/{ticker}")
    assert detail2.status_code == 200
    assert detail2.json()["served_from_cache"] is True

    assert mock_session.get.call_count == 0, (
        f"Market Intelligence -> View Details -> away -> back made "
        f"{mock_session.get.call_count} real UW call(s) -- ordinary navigation "
        "through the new feature must never cost a live call."
    )
