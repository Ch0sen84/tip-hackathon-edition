"""options_flow_anomaly in Live: session baseline instead of a snapshot
threshold (signal-integrity remediation, audit B8 / E2).

05c47ca's rule -- >= $1M premium across the newest 100 alerts plus any
sweep -- passed on 41/41 captured Live scans and on every sliding 100-alert
window of the 2,925 captured alerts: it could not tell normal activity from
an anomaly. Live now compares the session's cumulative UW flow-alert premium
(verified from the open) with the same elapsed slice of >= 20 prior complete
sessions, exactly like the dark-pool gate.

DATA PROVENANCE: multi-week histories below are CONSTRUCTED (fixed-cadence
alerts with chosen premiums); the recorded real capture is used where it can
honestly answer the question (it has no verified session coverage, so the
correct Live answer on it is "insufficient").
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core import scan_service, session_baseline, trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests import recorded_uw
from tests.uw_fakes import ok
from tests.uw_paginated import PaginatedUW, prior_sessions, session_alerts, session_prints

EVIDENCE = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
STATE = ok({"close": "101.00", "prev_close": "100.00"})


def history(ticker="ABCD", sessions=25, alert_premium=100_000.0, evidence_alert_premium=100_000.0,
            dp_premium=10_000.0, evidence_dp_premium=10_000.0, evidence_days=(EVIDENCE,)):
    flow, dp = [], []
    for d in prior_sessions(EVIDENCE, sessions):
        flow += session_alerts(ticker, d, alert_premium)
        dp += session_prints(ticker, d, dp_premium)
    for d in evidence_days:
        flow += session_alerts(ticker, d, evidence_alert_premium)
        dp += session_prints(ticker, d, evidence_dp_premium)
    return PaginatedUW(darkpool=dp, flow=flow, static={"stock-state": STATE})


@pytest.fixture
def live(app_main, monkeypatch):
    holder = {"now": AS_OF}

    def use(uw, watchlist=("ABCD",)):
        holder["adapter"] = uw.adapter(list(watchlist))

    monkeypatch.setattr(app_main, "get_adapter", lambda: holder["adapter"])
    monkeypatch.setattr(app_main, "_utcnow", lambda: holder["now"])
    app_main.use_uw = use
    app_main.clock = holder
    return app_main


def refresh(m, ticker="ABCD"):
    return TestClient(m.app).post(f"/api/ticker/{ticker}/refresh", json={"confirm": True}).json()


def backfill(m, ticker="ABCD"):
    return TestClient(m.app).post(f"/api/ticker/{ticker}/baseline/backfill?confirm=true&max_pages=200").json()


def test_recorded_real_scans_no_longer_pass_by_default(tmp_path):
    """Every one of the 41 captured scans passed options_flow_anomaly in
    05c47ca. Without verified session history, Live now says insufficient."""
    data = recorded_uw.require()
    cat = IngestionCatalog(str(tmp_path / "rec.db"))
    for a in data["flow_alerts"].values():
        cat.catalog_options_flow_observations(a["ticker"], [a])
    for scan in recorded_uw.scans():
        as_of = datetime.fromisoformat(scan["retrieved_at"])
        b = session_baseline.compute(cat, "options_flow", scan["ticker"], tc.evidence_session_date(as_of),
                                     as_of_utc=as_of)
        r = scan_service.evaluate_ticker_scan(
            scan["ticker"], scan["flow_alerts"], [], None, 0, mode="live",
            options_flow_baseline={"status": b.status, "metrics": b.as_metrics()})
        assert scan["captured_engine"]["gates_passed"][0] == "options_flow_anomaly"   # what 05c47ca said
        assert r["gates"]["options_flow_anomaly"]["status"] == "insufficient"
        assert r["gates"]["options_flow_anomaly"]["passed"] is False


def test_demo_still_uses_the_labeled_fixture_snapshot_rule():
    r = scan_service.evaluate_ticker_scan("X", session_alerts("X", EVIDENCE, 2_000_000.0)[:3], [], None, 0,
                                          mode="demo")
    g = r["gates"]["options_flow_anomaly"]
    assert g["status"] == "pass" and g["source"] == "demo_fixture_snapshot_threshold"


def test_normal_options_activity_fails(live):
    live.use_uw(history(evidence_alert_premium=100_000.0))
    backfill(live)
    g = refresh(live)["gates"]["options_flow_anomaly"]
    assert g["status"] == "fail"
    assert g["metrics"]["baseline"]["ratio"] == pytest.approx(1.0, rel=0.05)


def test_unusually_heavy_options_activity_passes(live):
    live.use_uw(history(evidence_alert_premium=500_000.0))
    backfill(live)
    body = refresh(live)
    g = body["gates"]["options_flow_anomaly"]
    assert g["status"] == "pass", g
    # all 25: bounded windows prove a session exhausted without needing a
    # pre-open alert (verified UW semantics), so even the oldest constructed
    # session -- first alert 09:35 -- is complete
    assert g["metrics"]["baseline"]["eligible_sessions"] == 25
    assert body["collection"]["options_flow"]["coverage"]["covered_from_utc"].startswith("2026-10-16T13:30")
    assert body["confidence"] == "medium"


def test_short_history_is_insufficient(live):
    live.use_uw(history(sessions=4, evidence_alert_premium=500_000.0))
    backfill(live)
    assert refresh(live)["gates"]["options_flow_anomaly"]["status"] == "insufficient"


def test_full_pipeline_can_earn_high_confirmed_and_stable_on_genuine_multi_session_evidence(live):
    """End-to-end reachability proof on CONSTRUCTED histories: both
    independent categories genuinely anomalous vs their own baselines on
    three distinct trading sessions -> CONFIRMED, then STABLE on the 3rd
    session (price corroborating the call-buying lean)."""
    d1 = EVIDENCE
    d2 = tc.next_trading_day(d1)
    d3 = tc.next_trading_day(d2)
    live.use_uw(history(evidence_alert_premium=500_000.0, evidence_dp_premium=100_000.0,
                        evidence_days=(d1, d2, d3)))
    backfill(live)
    first = refresh(live)
    assert first["confidence"] == "high"
    assert first["state"] == "CONFIRMED" and first["transition_rule"] == "opened_confirmed_two_independent_categories"
    assert first["gates"]["multi_signal_confirmation"]["metrics"]["contributing_gates"] == [
        "dark_pool_anomaly", "options_flow_anomaly"]
    again = refresh(live)                                   # same instant, same session
    assert again["state"] == "CONFIRMED"
    states = []
    for d in (d2, d3):
        live.clock["now"] = datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone.utc)
        states.append(refresh(live)["state"])
    assert states == ["CONFIRMED", "STABLE"]
    assert live.get_store("live").qualifying_days_count("ABCD") == 3


def test_one_anomalous_category_alone_stays_medium_and_never_confirms(live):
    live.use_uw(history(evidence_alert_premium=500_000.0, evidence_dp_premium=10_000.0))
    backfill(live)
    body = refresh(live)
    assert body["gates"]["dark_pool_anomaly"]["status"] == "fail"
    assert body["confidence"] == "medium" and body["state"] == "NEW"


def test_options_coverage_reaches_the_open_by_paging_into_the_prior_session(live):
    live.use_uw(history())
    refresh(live)
    cov = live.get_catalog().get_session_coverage("options_flow", "ABCD", "2026-10-16")
    assert cov["covered_from_utc"].startswith("2026-10-16T13:30")
