"""REAL BUG FIX regression guard (2026-10-14): /api/ticker/{ticker} and
/api/ticker/{ticker}/ohlc used to hard-reject ANY ticker not already in
adapter.get_universe() -- for LiveUWAdapter that is exactly
TIP_LIVE_WATCHLIST (a short fixed list), so a live user could never
analyze a real ticker outside that list, contradicting the advertised
"arbitrary ticker analysis" capability. Fixed: the fixed-universe check
now applies ONLY to the demo/synthetic adapter; Live Mode instead
format-validates the symbol and lets the real UW call be the source of
truth. These tests mock the adapter (no real network) but exercise the
REAL route logic end to end via TestClient, same pattern as
test_ohlc_route.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient


def _live_client(app_main, watchlist=("SPY", "QQQ")):
    """Force app.main's module-level adapter cache to a real LiveUWAdapter
    (mocked UWRestClient underneath, no real network) with a SHORT
    watchlist, so a ticker outside it is unambiguously "arbitrary"."""
    app_main._settings_store.set_mode("live")
    session = MagicMock()
    ok_resp = MagicMock(ok=True, status_code=200, text="{}")
    ok_resp.json.return_value = {"data": {"last_price": 123.45}}
    session.get.return_value = ok_resp
    client_ = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=list(watchlist), client=client_)
    app_main._adapter = adapter
    app_main._adapter_sig = ("live", "fake-key")
    return TestClient(app_main.app)


def test_live_mode_accepts_a_real_ticker_outside_the_watchlist(app_main):
    """The actual bug: NVDA-equivalent case -- a valid-format ticker NOT
    in TIP_LIVE_WATCHLIST must still be analyzable in Live Mode."""
    client = _live_client(app_main, watchlist=("SPY", "QQQ"))
    resp = client.get("/api/ticker/NVDA")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "NVDA"


def test_live_mode_ohlc_also_accepts_a_ticker_outside_the_watchlist(app_main):
    client = _live_client(app_main, watchlist=("SPY", "QQQ"))
    resp = client.get("/api/ticker/NVDA/ohlc")
    assert resp.status_code == 200, resp.text


def test_live_mode_rejects_a_format_invalid_ticker_with_400_not_a_crash(app_main):
    client = _live_client(app_main)
    resp = client.get("/api/ticker/NOT-A-TICKER!!")
    assert resp.status_code == 400
    resp2 = client.get("/api/ticker/NOT-A-TICKER!!/ohlc")
    assert resp2.status_code == 400


def test_demo_mode_still_enforces_the_fixed_scenario_universe(app_main):
    """The fix must NOT loosen demo mode -- fixtures genuinely only exist
    for the 11 named scenarios there, so this 404 behavior must remain."""
    app_main._settings_store.set_mode("demo")
    app_main._adapter = None
    app_main._adapter_sig = None
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/NVDA")  # NVDA is not one of the 11 synthetic scenarios
    assert resp.status_code == 404
    assert "demo scenario" in resp.json()["detail"]
