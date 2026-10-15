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
    # REAL UW shape (confirmed live, 2026-10-15): close/prev_close are
    # strings and there is no last_price/change_percent field at all --
    # the adapter must derive/normalize them, never pass the raw dict
    # through unmodified (see live_uw_adapter._normalize_stock_state).
    session = _mock_session(200, {"data": {"close": "450.10", "prev_close": "445.00"}})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["last_price"] == 450.10
    assert round(state["change_percent"], 4) == round((450.10 - 445.00) / 445.00 * 100, 4)


def test_adapter_get_stock_state_missing_prev_close_never_fabricates_change_pct():
    session = _mock_session(200, {"data": {"close": "99.50"}})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["SPY"], client=client)
    state = adapter.get_stock_state("SPY")
    assert state["last_price"] == 99.50
    assert state["change_percent"] is None  # no prev_close -> honestly unknown, never guessed


def test_adapter_universe_is_the_configured_watchlist():
    adapter = LiveUWAdapter(api_key="k", watchlist=["spy", "qqq"], client=MagicMock())
    assert adapter.get_universe() == ["SPY", "QQQ"]


def test_health_makes_zero_external_calls_and_reports_key_configured():
    """REAL BUG FIX (2026-10-15, Priority 4): health() used to call
    client.stock_state("SPY") -- a real outbound UW request -- on every
    /api/health hit. Assert the session's .get is NEVER invoked at all."""
    session = MagicMock()
    client = UWRestClient("bad-key", session=session)
    adapter = LiveUWAdapter(api_key="bad-key", watchlist=["SPY"], client=client)
    h = adapter.health()
    session.get.assert_not_called()
    assert h["api_key_configured"] is True
    assert h["probe_ok"] is None  # structurally not probed, never faked as True/False


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


# ---------------------------------------------------------------------------
# #36 (2026-10-15 round 2): the Evidence Detail table reported ZERO call/put
# activity because it read is_call/is_put booleans that don't exist on real
# UW flow-alert rows -- the real field is `type` ("call"/"put"). These
# tests exercise _normalize_flow_alert through the real adapter method
# (get_flow_alerts), with CONSTRUCTED (not real-response) fixtures covering
# the required matrix: missing type, malformed/unexpected values,
# numeric-string premiums, and a real call+put mix.
# ---------------------------------------------------------------------------

def test_flow_alert_type_call_normalizes_to_is_call():
    raw = {"type": "call", "total_premium": "1500000", "total_ask_side_prem": "1400000",
           "total_bid_side_prem": "100000"}
    session = _mock_session(200, {"data": [raw]})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    alerts = adapter.get_flow_alerts("NVDA")
    assert len(alerts) == 1
    assert alerts[0]["is_call"] is True
    assert alerts[0]["is_put"] is False
    # numeric-string premium passed through untouched for scan_service to coerce
    assert alerts[0]["total_premium"] == "1500000"
    assert alerts[0]["total_ask_premium"] == "1400000"


def test_flow_alert_type_put_case_insensitive_and_whitespace_tolerant():
    raw = {"type": " PUT ", "total_premium": 900000}
    session = _mock_session(200, {"data": [raw]})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    alerts = adapter.get_flow_alerts("NVDA")
    assert alerts[0]["is_put"] is True
    assert alerts[0]["is_call"] is False


def test_flow_alert_missing_type_field_is_neither_call_nor_put_no_crash():
    raw = {"total_premium": 500000}  # no "type" key at all
    session = _mock_session(200, {"data": [raw]})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    alerts = adapter.get_flow_alerts("NVDA")
    assert alerts[0]["is_call"] is False
    assert alerts[0]["is_put"] is False


def test_flow_alert_malformed_type_value_is_neither_call_nor_put_no_crash():
    for bad_type in (None, "", 123, "straddle", "cal", "putt"):
        raw = {"type": bad_type, "total_premium": 1}
        session = _mock_session(200, {"data": [raw]})
        client = UWRestClient("fake-key", session=session)
        adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
        alerts = adapter.get_flow_alerts("NVDA")
        assert alerts[0]["is_call"] is False, f"bad_type={bad_type!r}"
        assert alerts[0]["is_put"] is False, f"bad_type={bad_type!r}"


def test_flow_alert_mix_of_calls_and_puts_aggregate_correctly_via_scan_service():
    """End-to-end: normalized alerts feed scan_service's real aggregation
    (the same function the Evidence Detail table's numbers are derived
    from upstream) and produce a correct non-zero split."""
    from app.core.scan_service import _options_flow_metrics

    raw_rows = [
        {"type": "call", "total_premium": "2000000", "total_ask_side_prem": "1900000",
         "total_bid_side_prem": "100000"},
        {"type": "PUT", "total_premium": 500000, "total_ask_side_prem": 100000,
         "total_bid_side_prem": 400000},
        {"type": "call", "total_premium": "750000"},
    ]
    session = _mock_session(200, {"data": raw_rows})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    alerts = adapter.get_flow_alerts("NVDA")
    calls = [a for a in alerts if a["is_call"]]
    puts = [a for a in alerts if a["is_put"]]
    assert len(calls) == 2
    assert len(puts) == 1
    metrics = _options_flow_metrics(alerts)
    assert metrics["metrics"]["total_premium"] == 2000000 + 500000 + 750000


def test_flow_alert_explicit_is_call_is_put_from_fixtures_is_never_overridden():
    """Demo/synthetic fixtures already set is_call/is_put directly (no
    `type` field at all) -- the live-adapter fix must never clobber that
    when it happens to pass through this normalizer."""
    raw = {"is_call": True, "is_put": False, "total_premium": 1}
    session = _mock_session(200, {"data": [raw]})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    alerts = adapter.get_flow_alerts("NVDA")
    assert alerts[0]["is_call"] is True
    assert alerts[0]["is_put"] is False


def test_adapter_get_ohlc_forwards_window_through_to_the_client():
    session = _mock_session(200, {"data": []})
    client = UWRestClient("fake-key", session=session)
    adapter = LiveUWAdapter(api_key="fake-key", watchlist=["NVDA"], client=client)
    adapter.get_ohlc("NVDA", timeframe="1d", limit=84, window="3M")
    _, kwargs = session.get.call_args
    assert kwargs.get("params", {}).get("timeframe") == "3M"
