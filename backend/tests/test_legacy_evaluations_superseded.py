"""Upgrade safety: evaluations written by the pre-remediation engine are history,
never a current Live read (found by the Phase 3 Docker upgrade rehearsal).

Before: after upgrading a real 05c47ca volume, Live Market Intelligence and Ticker
Analysis served the latest legacy `signal_evaluations` row as the current read --
SPY/QQQ/NVDA/TSLA "BUILDING / medium" and PLTR "INVALIDATED" (the false put/call
invalidation), with `collection_complete` defaulting to True -- even though the new
Live lifecycle store was empty. Those verdicts came from the engine this remediation
replaced (no explicit gate status, failures read as zero activity, synthetic weight).

After: every row this build writes is stamped ENGINE_RULE_VERSION; current-read and
history-annotation queries only accept that version. Legacy rows stay untouched in
the table as an audit trail, and the ticker reads "not yet evaluated" with an honest
note until a real evaluation runs.
"""
import pytest
from fastapi.testclient import TestClient

from app import ingestion
from app.core import scheduler_freshness
from tests.uw_fakes import fake_live_adapter, ok

FLOW = [{"id": "a1", "ticker": "SPY", "type": "call", "created_at": "2026-10-16T14:00:00Z",
         "total_premium": 2_500_000, "total_ask_side_prem": 2_000_000, "total_bid_side_prem": 300_000,
         "has_sweep": True}]
DARKPOOL = [{"executed_at": "2026-10-16T14:00:00Z", "ticker": "SPY", "price": 700.0, "size": 100,
             "premium": 70_000, "tracking_id": 1, "volume": 1000}]
STATE = {"close": "701.00", "prev_close": "700.00"}

# Shape of a real 05c47ca evaluation (gates carry only metrics/passed; no collection block).
LEGACY_SCAN = {
    "ticker": "SPY", "state": "BUILDING", "previous_state": "NEW", "confidence": "medium",
    "confidence_reason": "2 independent signals", "signal_type": "dark_pool_accumulation",
    "transition_rule": "legacy_v1_rule", "qualifies": True, "qualifying_days_count": 3,
    "gates": {"dark_pool_anomaly": {"passed": True, "metrics": {}},
              "historical_percentile": {"passed": True, "metrics": {}},
              "data_quality": {"passed": True, "metrics": {}}},
    "gates_passed": ["dark_pool_anomaly", "historical_percentile"],
    "raw": {}, "stock_state": None, "price_move_pct": 0.4, "direction_heuristic": None,
}


@pytest.fixture
def live_app(app_main, monkeypatch):
    holder = {"adapter": fake_live_adapter({"flow-alerts": ok(FLOW), "/api/darkpool/": ok(DARKPOOL),
                                            "stock-state": ok(STATE)}, watchlist=["SPY"])}
    monkeypatch.setattr(app_main, "get_adapter", lambda: holder["adapter"])
    app_main.get_catalog().catalog_signal_evaluation("SPY", LEGACY_SCAN, engine_rule_version="v1",
                                                     retrieved_at="2026-10-17T23:04:01+00:00")
    return app_main


def test_legacy_evaluation_is_not_served_as_the_live_current_read(live_app):
    body = TestClient(live_app.app).get("/api/ticker/SPY").json()
    assert body["not_evaluated"] is True
    assert body["state"] is None and body["confidence"] is None and body["gates"] == {}
    assert body["eval_freshness"] == "unevaluated"
    assert body["superseded_evaluations"] == 1
    assert "previous version" in body["current_read"]


def test_radar_shows_a_legacy_only_ticker_as_not_evaluated(live_app):
    body = TestClient(live_app.app).get("/api/radar").json()
    entries = {e["ticker"]: e for e in body["highest"] + body["other"]}
    assert entries["SPY"]["state"] is None
    assert "BUILDING" not in str(entries["SPY"])


def test_scheduler_freshness_treats_legacy_only_history_as_unevaluated(live_app):
    result = scheduler_freshness.classify_candidate(live_app.get_catalog(), "SPY")
    assert result.state == scheduler_freshness.UNEVALUATED


def test_a_real_evaluation_replaces_the_legacy_read_and_legacy_rows_are_kept(live_app):
    m = live_app
    client = TestClient(m.app)
    fresh = client.post("/api/ticker/SPY/refresh", json={"confirm": True}).json()
    assert fresh["transition_rule"] != "legacy_v1_rule"
    cached = client.get("/api/ticker/SPY").json()
    assert cached["served_from_cache"] is True and cached.get("not_evaluated") is not True
    assert cached["state"] == fresh["state"]
    catalog = m.get_catalog()
    assert catalog.get_latest_signal_evaluation("SPY")["engine_rule_version"] == ingestion.ENGINE_RULE_VERSION
    assert catalog.get_latest_signal_evaluation("SPY", engine_rule_version="v1")["state"] == "BUILDING"
    assert catalog.count_superseded_signal_evaluations("SPY") == 1


def test_legacy_rows_never_annotate_new_lifecycle_history(live_app):
    m = live_app
    TestClient(m.app).post("/api/ticker/SPY/refresh", json={"confirm": True})
    rules = m.get_catalog().list_signal_evaluation_rules("SPY")
    assert rules and all(r["transition_rule"] != "legacy_v1_rule" for r in rules)
