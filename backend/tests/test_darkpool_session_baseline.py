"""Dark-pool session baseline: populated by normal Live execution, gated on
verified coverage, no look-ahead (signal-integrity remediation, audit B1/B18).

Replaces tests/test_item4_live_mode_baseline_http_integration.py, which
proved the baseline math by hand-inserting COMPLETE coverage rows -- a state
no shipped code path ever produced (the only writer had no caller). Here the
coverage is created by the real backfill route + session collector paging a
fake UW endpoint that honours UW's documented date/older_than/limit
semantics, then a real POST refresh evaluates the gate.

DATA PROVENANCE: the multi-week dark-pool histories below are CONSTRUCTED
(fixed-cadence prints with chosen premiums) because the recorded real
capture covers only two partially-sampled sessions -- see
test_recorded_real_capture_cannot_establish_a_baseline, which asserts that
truth instead of manufacturing a result from it.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core import session_baseline
from tests import recorded_uw
from tests.uw_fakes import FakeResponse, ok
from tests.uw_paginated import PaginatedUW, prior_sessions, session_prints

EVIDENCE = date(2026, 10, 16)                                  # Friday
AS_OF = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)    # 12:00 ET
STATE = ok({"close": "101.00", "prev_close": "100.00"})


def build_uw(history_sessions=25, evidence_premium=100_000.0, base_premium=10_000.0, ticker="ABCD",
             extra=None, evidence_kwargs=None):
    rows = []
    for d in prior_sessions(EVIDENCE, history_sessions):
        rows += session_prints(ticker, d, base_premium)
    rows += session_prints(ticker, EVIDENCE, evidence_premium, **(evidence_kwargs or {}))
    rows += extra or []
    return PaginatedUW(darkpool=rows, static={"stock-state": STATE, "flow-alerts": ok([])})


@pytest.fixture
def live(app_main, monkeypatch):
    holder = {}

    def use(uw, watchlist=("ABCD",)):
        holder["adapter"] = uw.adapter(list(watchlist))
        return holder["adapter"]

    monkeypatch.setattr(app_main, "get_adapter", lambda: holder["adapter"])
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    app_main.use_uw = use
    return app_main


def test_backfill_route_populates_complete_prior_sessions_through_the_collector(live):
    live.use_uw(build_uw())
    client = TestClient(live.app)
    before = client.get("/api/ticker/ABCD/baseline").json()
    assert before["datasets"]["darkpool"]["complete_prior_sessions"] == 0
    body = client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200").json()
    assert body["datasets"]["darkpool"]["complete_prior_sessions"] == 25
    touched = body["backfill"]["darkpool"]["sessions_touched"]
    # 25 constructed sessions are complete; older sessions with no data at all
    # are "unknown" (no activity vs no access cannot be told apart) -- never complete.
    assert sum(1 for t in touched if t["coverage"] == "complete") == 25
    assert all(t["coverage"] == "unknown" for t in touched if t["coverage"] != "complete")


def test_coverage_already_current_makes_no_request(live):
    uw = build_uw()
    live.use_uw(uw)
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/refresh")
    n = sum(1 for u, _ in uw.calls if "/api/darkpool/" in u)
    client.post("/api/ticker/ABCD/refresh")          # same instant: nothing new to cover
    assert sum(1 for u, _ in uw.calls if "/api/darkpool/" in u) == n


def test_refresh_after_backfill_evaluates_a_real_pass(live):
    live.use_uw(build_uw(evidence_premium=100_000.0))
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    body = client.post("/api/ticker/ABCD/refresh").json()
    dp = body["gates"]["dark_pool_anomaly"]
    assert dp["status"] == "pass", dp
    b = dp["metrics"]["baseline"]
    assert b["tier"] == "baseline_ready" and b["eligible_sessions"] == 25
    assert b["ratio"] == pytest.approx(10.0, rel=0.05) and b["percentile"] == 1.0
    assert b["window_start_utc"].startswith("2026-10-16T13:30")   # regular-session open
    assert body["collection"]["darkpool"]["coverage"]["covered_from_utc"].startswith("2026-10-16T13:30")
    assert body["confidence"] == "medium"                          # one independent category
    assert body["state"] == "NEW"


def test_normal_activity_fails_rather_than_passing(live):
    live.use_uw(build_uw(evidence_premium=10_000.0))
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    dp = client.post("/api/ticker/ABCD/refresh").json()["gates"]["dark_pool_anomaly"]
    assert dp["status"] == "fail"
    assert dp["metrics"]["baseline"]["ratio"] == pytest.approx(1.0, rel=0.05)


def test_too_few_prior_sessions_is_insufficient_not_fail(live):
    live.use_uw(build_uw(history_sessions=5))
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    body = client.post("/api/ticker/ABCD/refresh").json()
    dp = body["gates"]["dark_pool_anomaly"]
    assert dp["status"] == "insufficient"
    assert "insufficient history" in dp["metrics"]["baseline"]["reason"]


def test_without_any_backfill_a_single_refresh_is_insufficient(live):
    live.use_uw(build_uw())
    body = TestClient(live.app).post("/api/ticker/ABCD/refresh").json()
    assert body["gates"]["dark_pool_anomaly"]["status"] == "insufficient"
    assert body["confidence"] in ("insufficient", "low")


def test_partial_evidence_session_coverage_cannot_qualify(live):
    # one print every 3 seconds: ~3,600 prints by noon = 8 pages; the
    # interactive scan budget (4 pages) cannot reach the open in one scan
    uw = build_uw(evidence_kwargs={"every_minutes": 0.05})
    live.use_uw(uw)
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    body = client.post("/api/ticker/ABCD/refresh").json()
    dp = body["gates"]["dark_pool_anomaly"]
    assert dp["status"] == "insufficient"
    assert "not verified from the open" in dp["metrics"]["baseline"]["reason"]
    # a second refresh continues backward from where the first stopped
    body = client.post("/api/ticker/ABCD/refresh").json()
    assert body["gates"]["dark_pool_anomaly"]["status"] == "pass"


def test_future_sessions_are_never_used_as_history(live, monkeypatch):
    uw = build_uw(evidence_premium=100_000.0)
    live.use_uw(uw)
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    client.post("/api/ticker/ABCD/refresh")          # establishes evidence-session coverage
    cat = live.get_catalog()
    cat.upsert_session_coverage("darkpool", "ABCD", "2026-10-19", "complete",
                                "2026-10-19T13:30:00+00:00", "2026-10-19T20:00:00+00:00", 1, 1)
    cat.catalog_darkpool_observations("ABCD", session_prints("ABCD", date(2026, 10, 19), 10_000_000.0, tag="F"))
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF)
    assert "2026-10-19" not in [h["session_date"] for h in b.history_sessions]
    assert b.status == "pass"


def test_canceled_prints_are_excluded_and_tracking_id_dedupes(live):
    canceled = [{**r, "canceled": True, "tracking_id": r["tracking_id"] + "c", "premium": "50000000.00"}
                for r in session_prints("ABCD", EVIDENCE, 1.0, start=datetime(2026, 10, 16, 10, 0).time(),
                                        end=datetime(2026, 10, 16, 10, 5).time())]
    live.use_uw(build_uw(evidence_premium=10_000.0, extra=canceled))
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    dp = client.post("/api/ticker/ABCD/refresh").json()["gates"]["dark_pool_anomaly"]
    assert dp["status"] == "fail"   # the $50M canceled prints did not count
    cat = live.get_catalog()
    reformatted = [{**r, "premium": str(float(r["premium"]))} for r in session_prints("ABCD", EVIDENCE, 10_000.0)]
    cat.catalog_darkpool_observations("ABCD", reformatted)   # same prints, different number formatting
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF)
    assert b.ratio_vs_mean == pytest.approx(1.0, rel=0.05)


def test_provider_failure_during_collection_is_unavailable_and_keeps_coverage(live, monkeypatch):
    uw = build_uw()
    live.use_uw(uw)
    client = TestClient(live.app)
    client.post("/api/ticker/ABCD/baseline/backfill?max_pages=200")
    client.post("/api/ticker/ABCD/refresh")
    cov_before = live.get_catalog().get_session_coverage("darkpool", "ABCD", "2026-10-16")
    uw.fail["/api/darkpool/"] = FakeResponse(503, {"error": "upstream"})
    # five minutes later there is new data to collect, and UW fails
    from datetime import timedelta
    monkeypatch.setattr(live, "_utcnow", lambda: AS_OF + timedelta(minutes=5))
    body = client.post("/api/ticker/ABCD/refresh").json()
    assert body["gates"]["dark_pool_anomaly"]["status"] == "unavailable"
    assert body["transition_rule"] == "held_incomplete_collection"
    cov_after = live.get_catalog().get_session_coverage("darkpool", "ABCD", "2026-10-16")
    assert cov_after["covered_from_utc"] == cov_before["covered_from_utc"]


def test_pagination_cursor_not_honored_is_unknown_not_complete(live):
    class IgnoresCursor(PaginatedUW):
        @staticmethod
        def _page(rows, field, params):
            return sorted(rows, key=lambda r: r[field], reverse=True)[: int(params.get("limit") or 100)]

    uw = IgnoresCursor(darkpool=[r for d in prior_sessions(EVIDENCE, 3)
                                 for r in session_prints("ABCD", d, 10_000.0, every_minutes=0.5)],
                       static={"stock-state": STATE, "flow-alerts": ok([])})
    live.use_uw(uw)
    body = TestClient(live.app).post("/api/ticker/ABCD/baseline/backfill?max_pages=50").json()
    statuses = {c["status"] for c in body["datasets"]["darkpool"]["coverage"]}
    assert "complete" not in statuses


def test_demo_mode_has_no_session_baseline(app_main):
    body = TestClient(app_main.app).get("/api/ticker/FLUX/baseline").json()
    assert body["mode"] == "demo" and body["datasets"] == {}
    assert TestClient(app_main.app).post("/api/ticker/FLUX/baseline/backfill").status_code == 400


def test_recorded_real_capture_cannot_establish_a_baseline(tmp_path):
    """The real 2026-10-15..17 capture: thousands of real prints, but every
    scan saw only the newest page, so NO session has verified coverage.
    The honest result is insufficient -- asserted, not worked around."""
    data = recorded_uw.require()
    from app.ingestion import IngestionCatalog

    cat = IngestionCatalog(str(tmp_path / "rec.db"))
    nvda = [r for r in data["darkpool"].values() if r["ticker"] == "NVDA"]
    cat.catalog_darkpool_observations("NVDA", nvda)
    b = session_baseline.compute(cat, "darkpool", "NVDA", date(2026, 10, 16),
                                 as_of_utc=datetime(2026, 10, 16, 15, 0, 3, tzinfo=timezone.utc))
    assert len(nvda) > 2000
    assert b.status == "insufficient" and b.eligible_sessions == 0
    assert "never collected" in b.reason


def test_scheduler_backfill_hook_is_bounded_and_rotates(live, monkeypatch):
    adapter = live.use_uw(build_uw(), watchlist=("ABCD", "EFGH", "IJKL"))
    monkeypatch.setattr(live, "BACKFILL_TICKERS_PER_CYCLE", 2)
    first = live._scheduler_baseline_backfill(adapter)
    second = live._scheduler_baseline_backfill(adapter)
    assert len(first) == 2 and len(second) == 2
    assert set(first) | set(second) == {"ABCD", "EFGH", "IJKL"}
    for result in list(first.values()) + list(second.values()):
        assert result["darkpool"]["pages_used"] <= live.session_collection.DEFAULT_BACKFILL_PAGES
    assert live._scheduler_baseline_backfill(live.SyntheticDataAdapter(live.settings.fixtures_dir)) is None


def test_scheduler_runs_backfill_only_on_evaluating_cycles(tmp_path):
    from app.scheduler import SchedulerRunner

    calls = []

    class _Adapter:
        def discover_candidates(self, filters=None):
            return {"candidates": [], "snapshot_at": None}

    class _Catalog:
        def catalog_discovery_snapshot(self, **kw):
            return None

    runner = SchedulerRunner(db_path=str(tmp_path / "sched.db"), get_adapter=_Adapter, get_catalog=_Catalog,
                             run_ticker_scan=lambda a, t: {}, run_baseline_backfill=lambda a: calls.append(a) or {"ok": 1})
    runner.run_once_with_depth("discovery_only")
    assert calls == []
    out = runner.run_once_with_depth("discovery_plus_evaluation")
    assert len(calls) == 1
