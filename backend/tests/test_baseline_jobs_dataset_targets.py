"""Phase 9: a baseline job stops collecting a dataset once it has its own
`target_sessions` complete prior sessions and spends later steps on the
dataset still short of it. This changes WHICH PAGES the job requests -- never
which sessions count, the lookback, the 20-session requirement, signal
qualification or lifecycle -- and it never stops an explicitly requested scan
from collecting current-session evidence. Constructed data only."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import session_collection
from app.core import dark_pool_baseline
from app.core import session_baseline
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, live, run_job  # noqa: F401 (fixture)
from tests.uw_paginated import PaginatedUW, prior_sessions, session_alerts, session_prints


def heavy_flow_uw(history=30):
    """Dark pool: 2 pages per session. Options flow: ~4-5 pages per session,
    so dark pool reaches 20 sessions well before options flow does."""
    dp, fl = [], []
    for d in prior_sessions(EVIDENCE, history):
        dp += session_prints("ABCD", d, 10_000.0, every_minutes=0.5)
        fl += session_alerts("ABCD", d, 1_000.0, every_minutes=0.5)
    dp += session_prints("ABCD", EVIDENCE, 10_000.0)
    fl += session_alerts("ABCD", EVIDENCE, 1_000.0, every_minutes=1)
    return PaginatedUW(darkpool=dp, flow=fl, static={"stock-state": STATE})


def complete_prior(m, dataset):
    return sum(1 for c in m.get_catalog().list_session_coverage(dataset, "ABCD")
               if c["status"] == "complete" and c["session_date"] < EVIDENCE.isoformat())


def test_each_dataset_stops_at_its_own_target_and_the_other_continues(live, monkeypatch):
    uw = live.use_uw(heavy_flow_uw())
    client = TestClient(live.app)
    real_step, steps = live._baseline_job_step, []

    def recording_step(ticker, pages, datasets=None):
        before = len(uw.calls)
        r = real_step(ticker, pages, datasets=datasets)
        steps.append((datasets, [u for u, _ in uw.calls[before:]]))
        return r

    monkeypatch.setattr(live, "_baseline_job_step", recording_step)
    live._baseline_job_runner = None
    job = run_job(live, client, max_pages_per_ticker=1000)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "complete"
    assert complete_prior(live, "darkpool") >= 20 and complete_prior(live, "options_flow") >= 20

    first_flow_only = next(i for i, (ds, _) in enumerate(steps) if ds == ["options_flow"])
    met_at = complete_prior(live, "darkpool")
    assert all(ds is None for ds, _ in steps[:first_flow_only])
    for ds, urls in steps[first_flow_only:]:
        assert ds == ["options_flow"] and not [u for u in urls if "/api/darkpool/" in u]
    # dark pool stopped once met (within one step): it did not walk the whole 30-session lookback
    assert 20 <= met_at <= 25
    assert entry["datasets"]["darkpool"]["target_met"] is True
    assert entry["datasets"]["darkpool"]["complete_prior_sessions"] == met_at
    # the flow-only steps got the whole step budget, and accounting stays exact
    flow_only_pages = [len(urls) for _, urls in steps[first_flow_only:]]
    assert max(flow_only_pages) > 10
    provider = [u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u]
    assert len(provider) == job["pages_used"]


def test_dataset_targets_change_no_eligibility_or_signal_rule(live):
    live.use_uw(heavy_flow_uw())
    client = TestClient(live.app)
    run_job(live, client, max_pages_per_ticker=1000)
    assert dark_pool_baseline.BASELINE_READY_MIN == 20
    assert session_collection.BACKFILL_LOOKBACK_SESSIONS == 30
    for dataset in ("darkpool", "options_flow"):
        b = session_baseline.compute(live.get_catalog(), dataset, "ABCD", EVIDENCE, as_of_utc=AS_OF)
        assert b.as_metrics()["required_sessions"] == 20 and b.eligible_sessions == complete_prior(live, dataset)
        assert b.status != "insufficient"
    with live.get_catalog()._conn() as conn:
        for table in ("signal_evaluations", "signal_transitions", "lifecycle_episodes", "lifecycle_scans",
                      "lifecycle_session_evidence", "ai_investigations"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert client.get("/api/scheduler/status").json()["mode"] == "disabled"


def test_a_stopped_dataset_still_collects_current_session_evidence_on_request(live):
    """Freshness: the job no longer touches dark pool once it has 20 sessions,
    but a scan/refresh of the ticker still extends today's dark-pool coverage."""
    uw = live.use_uw(heavy_flow_uw())
    client = TestClient(live.app)
    run_job(live, client, max_pages_per_ticker=1000)
    ev_before = live.get_catalog().get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    later = AS_OF + timedelta(hours=1)
    calls_before = len(uw.calls)
    out = live._collect_session_dataset(live.get_adapter(), "darkpool", "ABCD", EVIDENCE, later, "on_demand")
    new = [p for u, p in uw.calls[calls_before:] if "/api/darkpool/" in u]
    assert new and all(p["date"] == EVIDENCE.isoformat() for p in new)
    ev_after = out["coverage"]
    assert ev_after["status"] == "partial" and ev_after["last_error"] == "session_in_progress"
    assert ev_after["covered_through_utc"] > ev_before["covered_through_utc"]


def test_resume_after_restart_keeps_per_dataset_targets(live):
    """A job checkpoint that already records dark pool at target resumes with
    options flow only (the saved progress is the checkpoint)."""
    uw = live.use_uw(heavy_flow_uw())
    store = live.get_baseline_job_store()
    job = store.create(["ABCD"], 1000, 20, requested_by="user")
    progress = job["progress"]
    progress["ABCD"]["datasets"] = {
        "darkpool": {"complete_prior_sessions": 20, "history_cutoff": False, "last_pages_used": 5},
        "options_flow": {"complete_prior_sessions": 3, "history_cutoff": False, "last_pages_used": 5}}
    store.update(job["id"], progress=progress)
    store.recover_after_restart()
    assert store.get(job["id"])["status"] == "paused"
    client = TestClient(live.app)
    assert client.post(f"/api/baseline/jobs/{job['id']}/resume", json={"confirm": True}).status_code == 200
    live.get_baseline_job_runner().join(60)
    final = store.get(job["id"])
    assert final["status"] == "completed"
    assert not [u for u, _ in uw.calls if "/api/darkpool/" in u]
    assert complete_prior(live, "options_flow") >= 20


def test_bounds_retries_are_reported_in_job_progress_and_the_read_only_route(live, monkeypatch):
    monkeypatch.setattr(session_collection, "_sleep", lambda _s: None)
    uw = live.use_uw(heavy_flow_uw())
    real_get, n = uw.get, {"i": 0}

    def get(url, headers=None, params=None, timeout=None):
        resp = real_get(url, headers, params, timeout)
        if "/api/darkpool/" in url:
            n["i"] += 1
            if n["i"] == 3:                                    # one out-of-window print, once
                from datetime import timedelta
                from tests.uw_fakes import FakeResponse
                from tests.uw_paginated import _ts, iso
                # 30 s beyond the exclusive bound (a whole-second row AT the bound is dropped, not rejected)
                bad = {"ticker": "ABCD", "tracking_id": "x",
                       "executed_at": iso(_ts(params["older_than"]) + timedelta(seconds=30)),
                       "price": "1", "size": 1, "premium": "1", "canceled": False}
                return FakeResponse(200, {"data": [bad] + resp.json()["data"]})
        return resp
    uw.get = get
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=1000)
    ds = job["progress"]["ABCD"]["datasets"]["darkpool"]
    assert ds["bounds_retries"] == 1 and ds["bounds_recovered"] == 1
    calls = len(uw.calls)
    body = client.get("/api/diagnostics/collection-bounds?ticker=abcd&dataset=darkpool").json()
    assert body["count"] == 1 and body["diagnostics"][0]["classification"] == "upper_bound_violated"
    assert body["retries_per_window"] == session_collection.BOUNDS_RETRIES_PER_WINDOW
    assert len(uw.calls) == calls                                   # read-only: no provider request
    assert client.get("/api/diagnostics/collection-bounds?dataset=nope").status_code == 400
    anon = TestClient(live.app)
    anon.headers.pop("X-TIP-Auth", None)
    assert anon.get("/api/diagnostics/collection-bounds").status_code == 401
    assert not any(c["status"] == "unknown" for c in live.get_catalog().list_session_coverage("darkpool", "ABCD"))
