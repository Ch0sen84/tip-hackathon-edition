"""R8-3 (chart-data-integrity-fixes round 9, 2026-10-18): regression
tests for the "latest_bar_is_final" disclosure field GET
/api/ticker/{ticker}/ohlc now returns, so the frontend can show a
refresh affordance even when a range is otherwise fully cached.

Real-data QA finding this fixes: when a daily-bar range is fully
served from cache, there was previously NO way for the frontend to
know the trailing bar might still be non-final (mid-session/within
the R8-2 grace window) -- so no refresh affordance ever appeared, even
though a user-triggered refresh would have pulled in the real final
close. The route now surfaces the backend's own existing finality
determination (IngestionCatalog.get_latest_daily_bar_final_status /
_bar_is_final) directly in its response, rather than making the
frontend guess from wall-clock time.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.adapters.live_uw_adapter import LiveUWAdapter
from app.core.trading_calendar import session_end_utc
from datetime import date, timedelta


def _wire_live_adapter(app_main, monkeypatch, watchlist):
    mock_session = MagicMock()
    adapter = LiveUWAdapter(api_key="fake-key", base_url="https://api.unusualwhales.com",
                             client_api_id="fake", watchlist=list(watchlist))
    adapter.client.session = mock_session
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    return adapter, mock_session


_TRADING_DATE = date(2026, 10, 14)
_CLOSE_UTC = session_end_utc(_TRADING_DATE)
assert _CLOSE_UTC is not None
_WITHIN_GRACE = (_CLOSE_UTC + timedelta(minutes=5)).isoformat()
_PAST_GRACE = (_CLOSE_UTC + timedelta(minutes=25)).isoformat()


def test_get_ohlc_reports_latest_bar_not_final_within_grace_window(app_main, monkeypatch):
    """A daily bar cataloged within the R8-2 grace window must be
    reported as latest_bar_is_final: false -- the refresh affordance
    this round adds depends on this being accurate, not guessed."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter(app_main, monkeypatch, [ticker])
    catalog = app_main.get_catalog()
    bar = [{"t": _TRADING_DATE.isoformat(), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}]
    catalog.catalog_price_bars(ticker, "1d", bar, retrieved_at=_WITHIN_GRACE)

    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["served_from_cache"] is True
    assert body["latest_bar_is_final"] is False


def test_get_ohlc_reports_latest_bar_final_past_grace_window(app_main, monkeypatch):
    """Once genuinely past the grace period, the SAME field must flip
    to true -- the frontend refresh affordance must disappear once a
    real refresh has already captured the final bar."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter(app_main, monkeypatch, [ticker])
    catalog = app_main.get_catalog()
    bar = [{"t": _TRADING_DATE.isoformat(), "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.5, "v": 1000}]
    catalog.catalog_price_bars(ticker, "1d", bar, retrieved_at=_PAST_GRACE)

    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_bar_is_final"] is True


def test_get_ohlc_latest_bar_is_final_none_when_nothing_cataloged(app_main, monkeypatch):
    """An empty cache has nothing to assess finality for -- must report
    None, never guess True or False."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter(app_main, monkeypatch, [ticker])

    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc")
    assert resp.status_code == 200
    assert resp.json()["latest_bar_is_final"] is None


def test_get_ohlc_latest_bar_is_final_none_for_intraday_1d_range(app_main, monkeypatch):
    """1D (intraday, 5m bars) never gets a price_bar_current pointer
    row at all (catalog_price_bars's daily-only scope) -- the field
    must be None, not a misleading guess, so the frontend never shows
    the daily-bar refresh affordance on an intraday chart."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    _wire_live_adapter(app_main, monkeypatch, [ticker])
    catalog = app_main.get_catalog()
    intraday_bar = [{"t": "2026-10-14T14:30:00+00:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]
    catalog.catalog_price_bars(ticker, "5m", intraday_bar)

    client = TestClient(app_main.app)
    resp = client.get(f"/api/ticker/{ticker}/ohlc?range=1D")
    assert resp.status_code == 200
    assert resp.json()["latest_bar_is_final"] is None


def test_scoped_refresh_does_not_request_the_full_range_bar_count(app_main, monkeypatch):
    """R8-3 scoping requirement: the trailing-bar refresh affordance
    must call POST /ohlc/refresh WITHOUT the wide `range` the chart is
    currently showing -- a small `limit` only, so UW is never asked to
    re-fetch an entire 3M/6M/1Y window just to pick up one incomplete
    trailing bar. This test calls the refresh route the way the new
    frontend affordance does (limit=5, no range) and asserts UW's
    stock_ohlc is invoked with that small limit, never the big one."""
    ticker = "NVDA"
    app_main._settings_store.set_watchlist([ticker])
    adapter, mock_session = _wire_live_adapter(app_main, monkeypatch, [ticker])
    mock_session.get.return_value = MagicMock(
        status_code=200, ok=True, text="{}", headers={},
        json=MagicMock(return_value={"data": []}),
    )
    client = TestClient(app_main.app)
    resp = client.post(f"/api/ticker/{ticker}/ohlc/refresh?limit=5", headers={"X-TIP-Auth": app_main.auth_token})
    assert resp.status_code == 200
    assert mock_session.get.call_count >= 1
    called_params = mock_session.get.call_args_list[0].kwargs.get("params", {})
    # live_uw_adapter.get_ohlc() oversamples daily bars 4x internally
    # (raw_limit_for_trading_days -- UW returns up to 3 session records
    # per date: pr/r/po, see candles.py's own module docstring) before
    # trimming back down to the desired `limit` -- that pre-existing
    # mechanism is untouched here. What THIS test actually proves is the
    # scoping requirement: a small desired limit (5) reaches UW as a
    # small raw request (20 = 5*4), nowhere close to a full 3M (65) /
    # 6M (130) / 1Y (252) trading-day count's raw request (260-1008).
    assert called_params.get("limit") == 20, f"expected the SCOPED oversampled limit (5*4=20) to reach UW, got params={called_params}"
    assert called_params.get("limit") < 65 * 4, "a scoped trailing-bar refresh must never approach a full-range raw request size"
    assert "timeframe" not in called_params or called_params.get("timeframe") is None, (
        "no `window`/range should be forwarded for a scoped trailing-bar refresh -- "
        f"got params={called_params}"
    )
