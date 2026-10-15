"""LiveUWAdapter tests use a mocked requests.Session -- no real network
calls to the UW API are ever made here."""
from unittest.mock import MagicMock

from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient


def _mock_session(status_code, json_body=None, text_body=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.text = text_body
    resp.json.return_value = json_body if json_body is not None else {}
    session = MagicMock()
    session.get.return_value = resp
    return session


def test_successful_call_returns_parsed_json():
    session = _mock_session(200, {"data": [{"ticker": "SPY"}]})
    client = UWRestClient("fake-key", session=session)
    body = client.stock_state("SPY")
    assert body == {"data": [{"ticker": "SPY"}]}
    session.get.assert_called_once()


def test_401_returns_unauthorized_structure_not_exception():
    session = _mock_session(401, text_body='{"code":"invalid_key"}')
    client = UWRestClient("bad-key", session=session)
    body = client.stock_info("SPY")
    assert body["status"] == "unauthorized"
    assert body["status_code"] == 401
    assert "ok" in body and body["ok"] is False


def test_403_returns_unavailable_on_tier_structure_not_exception():
    session = _mock_session(403, text_body='{"code":"futures_access_required"}')
    client = UWRestClient("fake-key", session=session)
    body = client.get("/api/futures/contracts")
    assert body["status"] == "unavailable_on_tier"
    assert body["status_code"] == 403


def test_missing_api_key_never_makes_a_request():
    session = MagicMock()
    client = UWRestClient("", session=session)
    body = client.stock_state("SPY")
    assert body["status"] == "unauthorized"
    session.get.assert_not_called()


def test_network_exception_is_caught_not_raised():
    import requests
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("boom")
    client = UWRestClient("fake-key", session=session)
    body = client.stock_state("SPY")
    assert body["status"] == "error"


def test_adapter_get_flow_alerts_returns_empty_list_on_403_no_crash():
    session = _mock_session(403, text_body="forbidden")
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    alerts = adapter.get_flow_alerts("SPY")
    assert alerts == []


def test_adapter_get_darkpool_trades_returns_empty_list_on_401_no_crash():
    session = _mock_session(401, text_body="unauthorized")
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", watchlist=["SPY"], client=client)
    trades = adapter.get_darkpool_trades("SPY")
    assert trades == []


def test_adapter_get_stock_state_marks_unavailable_without_crashing():
    session = _mock_session(403, text_body="forbidden")
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["unavailable"] is True


def test_adapter_get_stock_state_returns_data_on_success():
    session = _mock_session(200, {"data": {"last_price": 450.1, "change_percent": 1.0}})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["last_price"] == 450.1


def test_adapter_universe_is_the_configured_watchlist():
    adapter = LiveUWAdapter(api_key="k", watchlist=["spy", "qqq"], client=MagicMock())
    assert adapter.get_universe() == ["SPY", "QQQ"]


def test_health_reports_probe_failure_without_crashing():
    session = _mock_session(401, text_body="unauthorized")
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", watchlist=["SPY"], client=client)
    h = adapter.health()
    assert h["probe_ok"] is False


# ---------------------------------------------------------------------------
# Phase B final verification (2026-10-15): UW's real OHLC endpoint has its
# OWN "timeframe" query parameter (date-window selector, e.g. "1Y"/"2M",
# default "1Y") that this adapter never sent -- it only ever sent `limit`,
# so every request silently stayed bound to UW's default 1-year window no
# matter how large `limit` was. Fixed by adding `window`, forwarded as
# UW's real "timeframe" query key. These tests lock in the fix at the
# actual HTTP-request level (session.get's real call args), not just the
# adapter's return value, so a regression here is caught immediately.
# ---------------------------------------------------------------------------

def test_stock_ohlc_sends_uw_timeframe_query_param_when_window_given():
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    client.stock_ohlc("NVDA", timeframe="1d", limit=84, window="1Y")
    _, kwargs = session.get.call_args
    sent_params = kwargs.get("params", {})
    assert sent_params.get("timeframe") == "1Y"
    assert sent_params.get("limit") == 84


def test_stock_ohlc_omits_timeframe_query_param_when_window_is_none():
    """Backward compatibility: any caller that never asks for a window
    (e.g. the marker-building/legacy limit-only path) gets the exact
    same request shape as before this fix -- no new query param."""
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    client.stock_ohlc("NVDA", timeframe="1d", limit=180)
    _, kwargs = session.get.call_args
    sent_params = kwargs.get("params", {})
    assert "timeframe" not in sent_params
    assert sent_params.get("limit") == 180


def test_adapter_get_ohlc_overfetches_raw_rows_for_daily_bars():
    """2026-10-15 session-handling fix: requesting 84 desired daily bars
    must ask UW for MORE than 84 raw rows (3 sessions/date), not 84 --
    verified at the real session.get() call args."""
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    adapter.get_ohlc("NVDA", timeframe="1d", limit=84, window="1Y")
    _, kwargs = session.get.call_args
    requested_limit = kwargs.get("params", {}).get("limit")
    assert requested_limit > 84
    assert requested_limit >= 84 * 3


def test_adapter_get_ohlc_does_not_overfetch_for_intraday_bars():
    """Intraday candle sizes (5m etc.) need no over-fetch -- each bucket
    already has a distinct timestamp, no per-date session collision."""
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    adapter.get_ohlc("NVDA", timeframe="5m", limit=200, window="1D")
    _, kwargs = session.get.call_args
    assert kwargs.get("params", {}).get("limit") == 200


def test_adapter_get_ohlc_selects_regular_session_and_reports_coverage_metadata():
    """End-to-end through the real adapter: 3 raw session rows for one
    date in, 1 regular-session candle out, with honest raw/retained
    counts in the response (Chad's item #4 coverage-disclosure ask)."""
    raw_rows = [
        {"date": "2026-10-14", "market_time": "pr", "open": 229.7, "high": 230, "low": 229,
         "close": 225.65, "volume": 2_675_512},
        {"date": "2026-10-14", "market_time": "po", "open": 221.51, "high": 222, "low": 221,
         "close": 217.89, "volume": 24_546_863},
        {"date": "2026-10-14", "market_time": "r", "open": 225.53, "high": 226, "low": 221,
         "close": 221.3, "volume": 118_730_084},
    ]
    session = _mock_session(200, {"data": raw_rows})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    result = adapter.get_ohlc("NVDA", timeframe="1d", limit=1, window="1M")
    assert len(result["candles"]) == 1
    assert result["candles"][0]["o"] == 225.53  # regular-session OHLC, not pre/post
    assert result["candles"][0]["v"] == 118_730_084 - 2_675_512  # regular-only volume
    assert result["raw_records_returned"] == 3
    assert result["regular_session_records_retained"] == 1
    assert result["raw_records_requested"] > 1  # over-fetched, never exactly the desired count


def test_adapter_get_ohlc_forwards_window_through_to_the_client():
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    adapter.get_ohlc("NVDA", timeframe="1d", limit=84, window="3M")
    _, kwargs = session.get.call_args
    assert kwargs.get("params", {}).get("timeframe") == "3M"
