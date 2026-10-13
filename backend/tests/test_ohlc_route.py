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
