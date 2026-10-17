"""P2 regression tests (ux-nav-watchlist-reliability round 10): chart
range-switching coverage honesty.

Real finding (Claude's Windows Docker Desktop retest): switching a
chart's selected range from 3M to 1Y silently reused the same ~65
cached bars without being honest that the longer range wasn't actually
covered. _ticker_ohlc_impl already computed `range_fully_satisfied`/
`available_span_days`/`earliest_candle`/`latest_candle` from the REAL
cached data (never assumed from the request) -- this file locks that
cache-first coverage-detection contract down across a realistic
3M -> 1Y -> 1D -> 3M range-switch sequence, confirming:

  * the GET route never makes a live UW call merely from switching
    ranges (S1-b's cache-only invariant, re-verified here specifically
    across a MULTI-range sequence, not just a single range);
  * a longer range than what's cached is reported as NOT fully
    satisfied, with the real cached bar count/span -- never implying
    full coverage it doesn't have;
  * switching to a DIFFERENT timeframe (1D's 5m bars) that has never
    been cached reports an honest empty/unavailable state, not a
    crash or a reused daily dataset;
  * switching BACK to a previously-POST-refreshed range re-reads the
    exact same real cached bars, still zero UW calls.
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


def _mock_ok_response(payload):
    return MagicMock(status_code=200, ok=True, text="{}", headers={}, json=MagicMock(return_value=payload))


def _daily_candles(n, start=1):
    """n real daily bars, chronological, distinct VALID calendar dates
    (simple day-increment, not a trading calendar -- this test only
    cares about distinct real dates for available_span_days math)."""
    from datetime import date, timedelta
    base = date(2026, 1, 1)
    out = []
    for offset in range(start - 1, start - 1 + n):
        d = base + timedelta(days=offset)
        out.append({"t": d.isoformat(),
                    "o": 100.0 + offset, "h": 101.0 + offset, "l": 99.0 + offset, "c": 100.5 + offset, "v": 1000 + offset})
    return out


def test_3m_to_1y_to_1d_and_back_to_3m_range_switch_sequence(app_main, monkeypatch):
    ticker = "AMD"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])

    # Real UW response: 65 daily bars (exactly 3M's worth), never more --
    # this is the realistic "UW only has 3 months of real history for
    # this ticker right now" case the bug report describes.
    cached_3m_candles = _daily_candles(65)
    monkeypatch.setattr(
        type(adapter), "get_ohlc",
        lambda self, ticker, timeframe="1d", limit=180, window=None: {
            "candles": cached_3m_candles, "source": "live", "negative_volume_bars_clamped": 0,
            "unavailable": False, "reason": None,
        },
    )
    mock_session.get.return_value = _mock_ok_response({"data": []})
    client = TestClient(app_main.app)

    # Step 1: explicit POST refresh for 3M -- the ONLY real UW call in
    # this whole sequence.
    r1 = client.post(f"/api/ticker/{ticker}/ohlc/refresh?range=3M")
    assert r1.status_code == 200, r1.text
    assert r1.json()["range_fully_satisfied"] is True
    calls_after_3m_refresh = mock_session.get.call_count
    assert calls_after_3m_refresh > 0

    # Step 2: switch to 1Y via plain GET (what the frontend's range
    # buttons always use) -- zero additional UW calls (cache-only), and
    # the response must HONESTLY say 1Y is NOT fully covered by the 65
    # cached bars, carrying the real cached bounds (never inventing/
    # padding candles to look like a full year).
    r2 = client.get(f"/api/ticker/{ticker}/ohlc?range=1Y")
    assert r2.status_code == 200
    body2 = r2.json()
    assert mock_session.get.call_count == calls_after_3m_refresh, (
        "switching range from 3M to 1Y via GET made a real UW call -- range-switch must stay cache-only"
    )
    assert body2["served_from_cache"] is True
    assert body2["candle_count"] == 65
    assert body2["range_fully_satisfied"] is False, "1Y must not be reported as fully covered by 65 cached (3M-sized) bars"
    assert body2["earliest_candle"] == cached_3m_candles[0]["t"]
    assert body2["latest_candle"] == cached_3m_candles[-1]["t"]
    assert body2["available_span_days"] is not None

    # Step 3: switch to 1D (intraday, a DIFFERENT timeframe -- 5m bars
    # that have never been cached at all). Must be an honest empty/
    # unavailable state, never a crash, never silently reusing the
    # daily candles under a different label, and still zero UW calls.
    r3 = client.get(f"/api/ticker/{ticker}/ohlc?range=1D")
    assert r3.status_code == 200
    body3 = r3.json()
    assert mock_session.get.call_count == calls_after_3m_refresh, (
        "switching range to the never-cached 1D/5m timeframe made a real UW call"
    )
    assert body3["unavailable"] is True
    assert body3["candle_count"] == 0
    assert body3["candles"] == []

    # Step 4: switch BACK to 3M (the earlier POST-refreshed, real-world
    # "previously-cached range") -- must re-read the exact same real
    # cached bars, fully satisfied, still zero additional UW calls.
    r4 = client.get(f"/api/ticker/{ticker}/ohlc?range=3M")
    assert r4.status_code == 200
    body4 = r4.json()
    assert mock_session.get.call_count == calls_after_3m_refresh, (
        "switching back to a previously-cached range made a real UW call"
    )
    assert body4["served_from_cache"] is True
    assert body4["range_fully_satisfied"] is True
    assert body4["candle_count"] == 65
    assert [c["t"] for c in body4["candles"]] == [c["t"] for c in cached_3m_candles]


def test_load_more_refresh_route_can_genuinely_extend_coverage_for_a_longer_range(app_main, monkeypatch):
    """The ONLY way the 1Y gap from the test above may legitimately be
    closed is the explicit, authenticated POST refresh -- this proves
    that path still genuinely fetches/persists more real data (never a
    silent auto-upgrade from the GET route), and that a subsequent
    cache-only GET for 1Y then correctly reports full coverage from the
    real newly-cataloged bars.
    """
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter_with_mock_session(app_main, monkeypatch, [ticker])
    calls = {"n": 0}

    def fake_get_ohlc(self, ticker, timeframe="1d", limit=180, window=None):
        calls["n"] += 1
        # First call (3M refresh): 65 bars. Second call (1Y refresh):
        # UW genuinely has a fuller year of real history this time.
        n = 65 if calls["n"] == 1 else 252
        return {
            "candles": _daily_candles(n), "source": "live", "negative_volume_bars_clamped": 0,
            "unavailable": False, "reason": None,
        }

    monkeypatch.setattr(type(adapter), "get_ohlc", fake_get_ohlc)
    mock_session.get.return_value = _mock_ok_response({"data": []})
    client = TestClient(app_main.app)

    r1 = client.post(f"/api/ticker/{ticker}/ohlc/refresh?range=3M")
    assert r1.status_code == 200
    r2 = client.get(f"/api/ticker/{ticker}/ohlc?range=1Y")
    assert r2.json()["range_fully_satisfied"] is False

    # Real "Load more" action: POST refresh for the actually-selected
    # longer range.
    r3 = client.post(f"/api/ticker/{ticker}/ohlc/refresh?range=1Y")
    assert r3.status_code == 200, r3.text
    assert r3.json()["range_fully_satisfied"] is True
    assert r3.json()["candle_count"] == 252
    assert calls["n"] == 2, "Load More must trigger exactly one real fetch, not more"

    # A subsequent plain GET for 1Y now honestly reports full coverage
    # from the real cataloged data -- still zero further UW calls.
    calls_before_final_get = mock_session.get.call_count
    r4 = client.get(f"/api/ticker/{ticker}/ohlc?range=1Y")
    assert r4.json()["range_fully_satisfied"] is True
    assert r4.json()["candle_count"] == 252
    assert mock_session.get.call_count == calls_before_final_get
