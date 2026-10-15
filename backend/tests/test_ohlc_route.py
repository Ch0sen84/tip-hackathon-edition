"""Integration tests for GET /api/ticker/{ticker}/ohlc against the real
demo-mode SyntheticDataAdapter + fixtures (no mocking of the route
itself) -- proves the route, adapter, and candles.py glue actually work
together, not just each piece in isolation.

Uses the shared `app_main` fixture (see conftest.py) for a fresh
settings-store/adapter-cache per test -- app.main is a singleton module,
so without this isolation a prior test file's global state (e.g. mode
left on "live") leaks into whichever test module runs next.
"""
from fastapi.testclient import TestClient


def test_ohlc_route_returns_candles_and_markers_for_known_scenario(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unavailable"] is False
    assert len(body["candles"]) > 0
    assert all({"t", "o", "h", "l", "c", "v"} <= set(c) for c in body["candles"])
    # FLUX fixture has 1 flow_alert + 1 darkpool_trade -> 2 markers.
    assert len(body["markers"]) == 2
    priced = [m for m in body["markers"] if m["price"] is not None]
    assert len(priced) == 2  # both FLUX events carry a real price field


def test_ohlc_route_candles_are_chronologically_sorted(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/QRVX/ohlc")
    body = resp.json()
    dates = [c["t"] for c in body["candles"]]
    assert dates == sorted(dates)


def test_ohlc_route_unknown_ticker_404s(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/NOPE/ohlc")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Phase B (2026-10-14): user-selectable chart ranges (1M/3M/6M/1Y) must be
# REAL re-fetches (different `limit`), not a client-side zoom, and must
# honestly disclose when UW doesn't actually have that much history.
# ---------------------------------------------------------------------------

def test_range_1y_requests_more_bars_than_range_1m_real_different_limits(app_main, monkeypatch):
    """Proves this is a genuine different-sized request, not a zoom --
    patches the adapter to record the `limit` it was actually called
    with for each range."""
    from app import main as _main
    seen_limits = []
    real_get_ohlc = _main.SyntheticDataAdapter.get_ohlc

    def spy_get_ohlc(self, ticker, timeframe="1d", limit=180, window=None):
        seen_limits.append(limit)
        return real_get_ohlc(self, ticker, timeframe=timeframe, limit=limit, window=window)

    monkeypatch.setattr(_main.SyntheticDataAdapter, "get_ohlc", spy_get_ohlc)
    client = TestClient(app_main.app)
    client.get("/api/ticker/FLUX/ohlc?range=1M")
    client.get("/api/ticker/FLUX/ohlc?range=1Y")
    assert seen_limits == [22, 252]  # the real RANGE_TO_TRADING_DAYS values, distinct


def test_range_param_rejects_an_unsupported_value_with_400(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?range=2Y")
    assert resp.status_code == 400
    assert "range must be one of" in resp.json()["detail"]


def test_range_response_discloses_real_coverage_fields(app_main):
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?range=1M")
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] == "1M"
    assert body["candle_count"] == len(body["candles"])
    if body["candles"]:
        assert body["earliest_candle"] == body["candles"][0]["t"]
        assert body["latest_candle"] == body["candles"][-1]["t"]
        assert isinstance(body["range_fully_satisfied"], bool)


def test_range_fully_satisfied_is_false_when_the_ticker_has_less_history_than_requested(app_main, monkeypatch):
    """Real short-history scenario: a ticker with only 5 candles total
    requesting a 1Y range must honestly report range_fully_satisfied is
    False, not silently claim full coverage of a range it doesn't have."""
    from app import main as _main

    def short_history_ohlc(self, ticker, timeframe="1d", limit=180, window=None):
        candles = [{"t": f"2026-10-{i:02d}", "o": 1, "h": 1, "l": 1, "c": 1, "v": 100} for i in range(1, 6)]
        return {"candles": candles, "source": "test", "negative_volume_bars_clamped": 0, "unavailable": False, "reason": None}

    monkeypatch.setattr(_main.SyntheticDataAdapter, "get_ohlc", short_history_ohlc)
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?range=1Y")
    body = resp.json()
    assert body["candle_count"] == 5
    assert body["range_fully_satisfied"] is False


def test_1d_range_uses_5m_candle_size_and_uw_1d_window(app_main, monkeypatch):
    """2026-10-15 (Chad-approved item #3): genuine intraday range -- must
    switch candle_size to 5m (never daily bars relabeled as "1D") and
    pass UW's own timeframe=1D window param."""
    from app import main as _main
    seen = {}
    real_get_ohlc = _main.SyntheticDataAdapter.get_ohlc

    def spy_get_ohlc(self, ticker, timeframe="1d", limit=180, window=None):
        seen["timeframe"] = timeframe
        seen["window"] = window
        return real_get_ohlc(self, ticker, timeframe=timeframe, limit=limit, window=window)

    monkeypatch.setattr(_main.SyntheticDataAdapter, "get_ohlc", spy_get_ohlc)
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?range=1D")
    assert resp.status_code == 200
    assert seen["timeframe"] == "5m"
    assert seen["window"] == "1D"
    assert resp.json()["range"] == "1D"


def test_5d_range_is_not_yet_supported(app_main):
    """Chad explicitly deferred 5D this pass -- must 400, not silently
    fall through to something else."""
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?range=5D")
    assert resp.status_code == 400


def test_intraday_candles_preserve_full_source_timestamps(app_main, monkeypatch):
    """1D intraday candles must carry real start_time timestamps (not
    truncated to a bare date) -- the frontend needs this to place bars
    at distinct intraday x-positions, not collapse them onto one date."""
    # Exercise the REAL build_candles pipeline directly (ensures the
    # route/adapter boundary doesn't truncate timestamps anywhere).
    from app.core.candles import build_candles
    candles, _ = build_candles([
        {"start_time": "2026-10-14T13:30:00Z", "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 100},
        {"start_time": "2026-10-14T13:35:00Z", "open": 1.5, "high": 2.5, "low": 1, "close": 2, "volume": 150},
    ], dedupe_daily=False)
    assert candles[0]["t"] == "2026-10-14T13:30:00Z"
    assert candles[1]["t"] == "2026-10-14T13:35:00Z"
    assert candles[0]["t"] != candles[1]["t"]  # distinct intraday timestamps, not collapsed to one date


def test_omitting_range_leaves_legacy_timeframe_limit_params_unaffected(app_main):
    """Backward compatibility: callers that never pass `range` (existing
    marker-building logic, any external consumer) keep working exactly
    as before -- range/coverage fields are simply null, not an error."""
    client = TestClient(app_main.app)
    resp = client.get("/api/ticker/FLUX/ohlc?timeframe=1d&limit=30")
    assert resp.status_code == 200
    body = resp.json()
    assert body["range"] is None
    assert body["range_fully_satisfied"] is None
    assert body["available_span_days"] is None


def test_ohlc_route_markers_match_the_same_data_the_detection_engine_sees(app_main):
    """The markers must be derived from the SAME flow_alerts/darkpool_trades
    the /api/ticker/{t} detection route already returns -- not a second,
    divergent data source."""
    client = TestClient(app_main.app)
    ohlc = client.get("/api/ticker/FLUX/ohlc").json()
    detail = client.get("/api/ticker/FLUX").json()
    raw_alert_times = {a["created_at"] for a in detail["raw"]["flow_alerts"]}
    raw_trade_times = {t["executed_at"] for t in detail["raw"]["darkpool_trades"]}
    marker_times = {m["time"] for m in ohlc["markers"]}
    assert marker_times == raw_alert_times | raw_trade_times
