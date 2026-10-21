"""Missing data is never negative evidence (signal-integrity remediation,
audit B4 / E9).

Before: LiveUWAdapter turned every UW failure into `[]`, _run_scan never
passed api_errors, and a 503 on flow-alerts produced exactly the same
"no flow alerts this scan" gate -- and the same BUILDING -> FADING demotion --
as a genuinely quiet tape.

After: each source carries an explicit collection status; a failed source is
"unavailable", confidence is "insufficient", lifecycle state is held, no
lifecycle history row is written, and freshness reads "collection_failed".
"""
import pytest
from fastapi.testclient import TestClient

from tests.uw_fakes import FakeResponse, Timeout, fake_live_adapter, ok

FLOW = [{"id": "a1", "ticker": "SPY", "type": "call", "created_at": "2026-10-16T14:00:00Z",
         "total_premium": 2_500_000, "total_ask_side_prem": 2_000_000, "total_bid_side_prem": 300_000,
         "has_sweep": True}]
DARKPOOL = [{"executed_at": "2026-10-16T14:00:00Z", "ticker": "SPY", "price": 700.0, "size": 100, "premium": 70_000}]
STATE = {"close": "701.00", "prev_close": "700.00"}


def routes(flow=None, dp=None, state=None):
    return {
        "flow-alerts": flow if flow is not None else ok(FLOW),
        "/api/darkpool/": dp if dp is not None else ok(DARKPOOL),
        "stock-state": state if state is not None else ok(STATE),
    }


FAILURES = {
    "http_503": (FakeResponse(503, {"error": "upstream"}), "error"),
    "http_500": (FakeResponse(500, {"error": "boom"}), "error"),
    "http_429": (FakeResponse(429, {"error": "slow down"}), "rate_limited"),
    "timeout": (Timeout(), "error"),
    "malformed_json": (FakeResponse(200, raw_text="<html>not json</html>"), "malformed"),
    "data_not_a_list": (FakeResponse(200, {"data": {"oops": 1}}), "malformed"),
    "unauthorized_401": (FakeResponse(401, {"error": "bad key"}), "unauthorized"),
    "forbidden_403": (FakeResponse(403, {"error": "tier"}), "unavailable_on_tier"),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_every_failure_mode_is_reported_as_a_failure_not_an_empty_answer(name):
    response, expected = FAILURES[name]
    adapter = fake_live_adapter(routes(flow=response))
    result = adapter.collect_flow_alerts("SPY")
    assert result["status"] == expected
    assert result["ok"] is False
    assert result["rows"] == []


def test_genuinely_empty_answer_is_ok_with_zero_rows():
    result = fake_live_adapter(routes(flow=ok([]))).collect_flow_alerts("SPY")
    assert result["status"] == "ok" and result["rows"] == []


def test_rows_for_a_different_ticker_are_dropped_and_counted():
    mixed = FLOW + [{**FLOW[0], "id": "x9", "ticker": "AAPL"}]
    result = fake_live_adapter(routes(flow=ok(mixed))).collect_flow_alerts("SPY")
    assert [r["id"] for r in result["rows"]] == ["a1"]
    assert result["dropped_rows"] == 1


def test_retries_stay_bounded_on_a_persistent_5xx():
    adapter = fake_live_adapter(routes(flow=FakeResponse(503, {"error": "upstream"})), max_retries=2)
    adapter.collect_flow_alerts("SPY")
    flow_calls = [u for u, _ in adapter.fake_session.calls if "flow-alerts" in u]
    assert len(flow_calls) == 3  # first attempt + 2 bounded retries, then give up


@pytest.fixture
def live_app(app_main, monkeypatch):
    holder = {}

    def use(r):
        holder["adapter"] = fake_live_adapter(r, watchlist=["SPY"])

    monkeypatch.setattr(app_main, "get_adapter", lambda: holder["adapter"])
    app_main.use_routes = use
    return app_main


def _seed_building(m):
    store = m.get_store("live")
    store.record_scan("SPY", True, "NEW", scan_date="2026-10-14")
    store.record_scan("SPY", True, "BUILDING", scan_date="2026-10-15")
    return store


def test_outage_holds_state_and_writes_no_lifecycle_history(live_app):
    m = live_app
    store = _seed_building(m)
    history_before = store.history("SPY")
    days_before = store.qualifying_days_count("SPY")
    m.use_routes(routes(flow=FakeResponse(503, {"error": "upstream"})))

    body = TestClient(m.app).post("/api/ticker/SPY/refresh", json={"confirm": True}).json()

    assert body["state"] == "BUILDING"
    assert body["transition_rule"] == "held_incomplete_collection"
    assert body["confidence"] == "insufficient"
    assert body["collection_complete"] is False
    assert body["collection"]["options_flow"]["status"] == "error"
    assert body["gates"]["options_flow_anomaly"]["status"] == "unavailable"
    assert body["gates"]["data_quality"]["passed"] is False
    assert body["eval_freshness"] == "collection_failed"
    assert store.history("SPY") == history_before
    assert store.qualifying_days_count("SPY") == days_before
    assert store.current_state("SPY") == "BUILDING"


def test_outage_and_genuine_empty_answer_are_no_longer_identical(live_app):
    m = live_app
    client = TestClient(m.app)
    _seed_building(m)
    m.use_routes(routes(flow=FakeResponse(503, {"error": "upstream"}), dp=ok([])))
    outage = client.post("/api/ticker/SPY/refresh", json={"confirm": True}).json()
    m.use_routes(routes(flow=ok([]), dp=ok([])))
    quiet = client.post("/api/ticker/SPY/refresh", json={"confirm": True}).json()
    assert outage["gates"]["options_flow_anomaly"]["status"] == "unavailable"
    assert outage["transition_rule"] == "held_incomplete_collection"
    assert outage["collection_complete"] is False
    # A complete, empty answer IS collected data: the gate is evaluated by
    # its method (here: insufficient baseline history on a fresh install),
    # never reported as unavailable.
    assert quiet["collection_complete"] is True
    assert quiet["gates"]["options_flow_anomaly"]["status"] == "insufficient"
    assert quiet["transition_rule"] == "held_insufficient_evidence"


def test_failed_first_scan_creates_no_lifecycle_record(live_app):
    m = live_app
    m.use_routes(routes(dp=FakeResponse(500, {"error": "x"})))
    body = TestClient(m.app).post("/api/ticker/SPY/refresh", json={"confirm": True}).json()
    assert body["state"] is None
    assert m.get_store("live").current_state("SPY") is None
    assert m.get_store("live").history("SPY") == []


def test_cached_view_after_outage_reports_failure_and_last_successful_collection(live_app):
    m = live_app
    client = TestClient(m.app)
    m.use_routes(routes())
    good = client.post("/api/ticker/SPY/refresh", json={"confirm": True}).json()
    assert good["collection_complete"] is True and good["eval_freshness"] == "current"
    first_success = good["last_successful_collection_at"]
    assert first_success

    m.use_routes(routes(state=FakeResponse(502, {"error": "gateway"})))
    client.post("/api/ticker/SPY/refresh", json={"confirm": True})
    cached = client.get("/api/ticker/SPY").json()
    assert cached["served_from_cache"] is True
    assert cached["eval_freshness"] == "collection_failed"
    assert cached["last_successful_collection_at"] == first_success
    assert cached["last_evaluated_at"] > first_success


def test_radar_refresh_marks_failed_ticker_as_collection_failed_not_current(live_app):
    m = live_app
    m.use_routes(routes(flow=FakeResponse(429, {"error": "slow"})))
    body = TestClient(m.app).post("/api/radar/refresh", json={"confirm": True}).json()
    entries = {e["ticker"]: e for e in body["highest"] + body["other"]}
    assert entries["SPY"]["eval_freshness"] == "collection_failed"
    assert entries["SPY"]["collection"]["options_flow"]["status"] == "rate_limited"


def test_chart_refresh_discloses_failed_marker_source(live_app):
    m = live_app
    r = routes(flow=FakeResponse(503, {"error": "upstream"}))
    r["/ohlc/"] = ok([])
    m.use_routes(r)
    body = TestClient(m.app).post("/api/ticker/SPY/ohlc/refresh").json()
    assert body["marker_sources"]["origin"] == "live_fetch"
    assert body["marker_sources"]["options_flow"]["status"] == "error"
    assert body["marker_sources"]["darkpool"]["status"] == "ok"
    cached = TestClient(m.app).get("/api/ticker/SPY/ohlc").json()
    assert cached["marker_sources"]["origin"] == "catalog"
