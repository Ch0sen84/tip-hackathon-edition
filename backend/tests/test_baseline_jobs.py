"""User-initiated historical baseline builds (Phase 4) through the real routes,
the real session-coverage collector and a fake UW endpoint that honours the
verified pagination contract (tests/uw_paginated.py)."""
import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import baseline_jobs, session_collection
from tests.uw_fakes import FakeResponse, ok
from tests.uw_paginated import PaginatedUW, prior_sessions, session_prints

EVIDENCE = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
STATE = ok({"close": "101.00", "prev_close": "100.00"})


def build_uw(history_sessions=26, **kw):
    rows = []
    for d in prior_sessions(EVIDENCE, history_sessions):
        rows += session_prints("ABCD", d, 10_000.0)
    rows += session_prints("ABCD", EVIDENCE, 10_000.0)
    return PaginatedUW(darkpool=rows, static={"stock-state": STATE, "flow-alerts": ok([])}, **kw)


JOB_STOP_TIMEOUT_S = 60


def stop_job_threads(m) -> None:
    """Cancel any active job of THIS test and wait until its runner thread has
    really stopped. Fails loudly instead of letting a job thread outlive the
    test (a leaked runner resolves the module globals get_catalog() /
    get_adapter() on its next step and would write into the NEXT test's DB)."""
    runner = m._baseline_job_runner
    store = m._baseline_job_store
    if store is not None:
        job = store.latest()
        if job and job["status"] in ("running", "pause_requested"):
            store.transition(job["id"], ("running", "pause_requested"), "cancel_requested")
    if runner is not None:
        runner.join(JOB_STOP_TIMEOUT_S)
        assert not runner.is_running(), "baseline job thread did not stop within the test"


@pytest.fixture
def live(app_main, monkeypatch, tmp_path):
    """Live-mode app with an ISOLATED FILE-BACKED database per test (production
    semantics: one SQLite connection per call). The suite default ':memory:'
    keeps ONE connection shared by every thread, which is not a supported
    concurrency model for a background job thread plus test-thread reads."""
    holder = {}

    def use(uw, watchlist=("ABCD",)):
        holder["uw"] = uw
        holder["adapter"] = uw.adapter(list(watchlist))
        return uw

    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "live_jobs.db"))
    monkeypatch.setattr(app_main, "_catalog", None)
    monkeypatch.setattr(app_main, "_store", None)
    monkeypatch.setattr(app_main, "get_adapter", lambda: holder["adapter"])
    monkeypatch.setattr(app_main, "_utcnow", lambda: AS_OF)
    monkeypatch.setattr(app_main, "_baseline_job_store", None)
    monkeypatch.setattr(app_main, "_baseline_job_runner", None)
    app_main.use_uw = use
    yield app_main
    stop_job_threads(app_main)          # cleanup only after the background worker has stopped


def run_job(m, client, **body):
    job = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 20, **body}).json()["job"]
    m.get_baseline_job_runner().join(JOB_STOP_TIMEOUT_S)
    assert not m.get_baseline_job_runner().is_running(), "job did not finish within the timeout"
    return m.get_baseline_job_store().get(job["id"])


def test_nothing_runs_until_the_user_starts_a_job_and_reads_make_no_requests(live):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    ov = client.get("/api/baseline/overview").json()
    assert ov["job"] is None and ov["tickers"][0]["datasets"]["darkpool"]["complete_prior_sessions"] == 0
    assert "0 of 20" in ov["tickers"][0]["datasets"]["darkpool"]["insufficient_reason"]
    client.get("/api/baseline/jobs/current")
    assert uw.calls == []
    assert live.get_baseline_job_runner().is_running() is False


def test_a_job_builds_the_baseline_in_bounded_steps_to_the_target(live):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "complete"
    assert all(d["complete_prior_sessions"] >= 20 for d in entry["datasets"].values())
    assert entry["steps"] >= 2 and entry["pages_used"] <= entry["steps"] * 20     # bounded per step
    provider_pages = [u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u]
    assert len(provider_pages) == job["pages_used"]                                 # accounting is exact
    ov = client.get("/api/baseline/overview").json()["tickers"][0]["datasets"]
    assert ov["darkpool"]["complete_prior_sessions"] >= 20 and ov["darkpool"]["insufficient_reason"].startswith("history is sufficient")
    b = client.get("/api/ticker/ABCD/baseline").json()["datasets"]["darkpool"]["baseline"]
    assert b["eligible_sessions"] >= 20


def test_pause_then_resume_never_refetches_verified_sessions(live, monkeypatch):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    real_step = live._baseline_job_step
    seen = {"n": 0}

    def step_then_pause(ticker, pages):
        r = real_step(ticker, pages)
        seen["n"] += 1
        if seen["n"] == 2:
            job = live.get_baseline_job_store().latest()
            live.get_baseline_job_store().transition(job["id"], ("running",), "pause_requested")
        return r

    monkeypatch.setattr(live, "_baseline_job_step", step_then_pause)
    live._baseline_job_runner = None
    job = run_job(live, client, max_pages_per_ticker=400)
    assert job["status"] == "paused"
    done_before = {c["session_date"] for c in live.get_catalog().list_session_coverage("darkpool", "ABCD")
                   if c["status"] == "complete"}
    assert done_before
    calls_before = len(uw.calls)
    assert client.post(f"/api/baseline/jobs/{job['id']}/resume").status_code == 200
    live.get_baseline_job_runner().join(60)
    assert live.get_baseline_job_store().get(job["id"])["status"] == "completed"
    later_dates = {(p or {}).get("date") for url, p in uw.calls[calls_before:] if "/api/darkpool/" in url}
    assert not (later_dates & done_before), "a verified-complete session was fetched again"


def test_restart_leaves_an_interrupted_job_paused_and_starts_nothing(live):
    uw = live.use_uw(build_uw())
    store = live.get_baseline_job_store()
    job = store.create(["ABCD"], 400, 20, requested_by="user")      # status running, no thread (simulated crash)
    store.recover_after_restart()
    after = store.get(job["id"])
    assert after["status"] == "paused" and "restart" in after["last_error"]
    assert uw.calls == [] and live.get_baseline_job_runner().is_running() is False


def test_cancel_takes_effect_between_steps(live, monkeypatch):
    live.use_uw(build_uw())
    client = TestClient(live.app)
    real_step = live._baseline_job_step

    def step_then_cancel(ticker, pages):
        r = real_step(ticker, pages)
        job = live.get_baseline_job_store().latest()
        client.post(f"/api/baseline/jobs/{job['id']}/cancel")
        return r

    monkeypatch.setattr(live, "_baseline_job_step", step_then_cancel)
    live._baseline_job_runner = None
    job = run_job(live, client, max_pages_per_ticker=400)
    assert job["status"] == "cancelled" and job["progress"]["ABCD"]["steps"] == 1


def test_provider_failure_pauses_the_job_and_records_the_error(live):
    live.use_uw(build_uw(fail={"/api/darkpool/": FakeResponse(503, {"error": "upstream"})}))
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    assert job["status"] == "paused" and "darkpool" in job["last_error"] and "error" in job["last_error"]
    assert not any(c["status"] == "complete" for c in live.get_catalog().list_session_coverage("darkpool", "ABCD"))


def test_history_cutoff_finishes_the_ticker_honestly(live):
    cutoff = prior_sessions(EVIDENCE, 8)[0]          # provider history starts 8 sessions back
    live.use_uw(build_uw(history_cutoff=cutoff))
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "history_cutoff"
    ds = client.get("/api/baseline/overview").json()["tickers"][0]["datasets"]["darkpool"]
    assert ds["complete_prior_sessions"] < 20 and ds["history_cutoff_before"] and "history ends" in ds["insufficient_reason"]


def test_one_active_job_live_only_and_auth_required(live, monkeypatch):
    live.use_uw(build_uw())
    client = TestClient(live.app)
    store = live.get_baseline_job_store()
    store.create(["ABCD"], 400, 20, requested_by="user")            # an active (running) job exists
    assert client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"]}).status_code == 409
    anon = TestClient(live.app)
    anon.headers.pop("X-TIP-Auth", None)
    assert anon.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"]}).status_code == 401
    assert anon.get("/api/baseline/overview").status_code == 401
    from app.adapters.synthetic_adapter import SyntheticDataAdapter
    monkeypatch.setattr(live, "get_adapter", lambda: SyntheticDataAdapter(live.settings.fixtures_dir))
    assert client.post("/api/baseline/jobs/1/cancel").status_code == 200    # cancel never needs Live
    assert client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"]}).status_code == 400


def test_job_and_interactive_refreshes_never_write_one_tickers_coverage_concurrently(live, monkeypatch, tmp_path):
    # File-backed DB, as in production: one connection per call. (The suite's
    # in-memory catalog shares ONE sqlite connection across threads, which is
    # not a supported concurrency model for this test.)
    live.use_uw(build_uw())             # the live fixture is already file-backed
    client = TestClient(live.app)
    active, peak, guard = {"n": 0}, {"n": 0}, threading.Lock()
    real = session_collection.update_session_coverage

    def tracked(*a, **k):
        with guard:
            active["n"] += 1
            peak["n"] = max(peak["n"], active["n"])
        try:
            return real(*a, **k)
        finally:
            with guard:
                active["n"] -= 1

    monkeypatch.setattr(session_collection, "update_session_coverage", tracked)
    job = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 400}).json()["job"]
    for _ in range(4):
        assert client.post("/api/ticker/ABCD/refresh").status_code in (200, 409)
    live.get_baseline_job_runner().join(60)
    final = live.get_baseline_job_store().get(job["id"])
    assert final["status"] == "completed", (final["last_error"], final["progress"])
    assert peak["n"] == 1


def test_default_step_bounds_requests_after_a_pause_to_ten(live, monkeypatch):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    real_step = live._baseline_job_step
    calls_at_pause = {}

    def step_then_pause(ticker, pages):
        if "n" not in calls_at_pause:                      # user clicks Pause while step 1 is in flight
            job = live.get_baseline_job_store().latest()
            client.post(f"/api/baseline/jobs/{job['id']}/pause")
            calls_at_pause["n"] = len(uw.calls)
        return real_step(ticker, pages)

    monkeypatch.setattr(live, "_baseline_job_step", step_then_pause)
    live._baseline_job_runner = None
    job = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"]}).json()["job"]
    assert job["step_pages"] == baseline_jobs.DEFAULT_STEP_PAGES == 10
    live.get_baseline_job_runner().join(60)
    final = live.get_baseline_job_store().get(job["id"])
    provider_after_pause = [u for u, _ in uw.calls[calls_at_pause["n"]:] if "/api/darkpool/" in u or "flow-alerts" in u]
    assert final["status"] == "paused" and len(provider_after_pause) <= 10


def test_leaving_live_mode_pauses_the_job_with_a_clear_reason_and_no_more_requests(live, monkeypatch):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    real_step = live._baseline_job_step
    from app.adapters.synthetic_adapter import SyntheticDataAdapter

    def step_then_leave_live(ticker, pages):
        r = real_step(ticker, pages)
        monkeypatch.setattr(live, "get_adapter", lambda: SyntheticDataAdapter(live.settings.fixtures_dir))
        return r

    monkeypatch.setattr(live, "_baseline_job_step", step_then_leave_live)
    live._baseline_job_runner = None
    job = run_job(live, client, max_pages_per_ticker=400)
    n = len(uw.calls)
    assert job["status"] == "paused" and "no longer in Live Mode" in job["last_error"]
    assert "internal error" not in job["last_error"]
    assert len(uw.calls) == n


def test_a_cutoff_found_by_an_earlier_job_is_still_labelled_as_a_cutoff(live):
    cutoff = prior_sessions(EVIDENCE, 8)[0]
    live.use_uw(build_uw(history_cutoff=cutoff))
    client = TestClient(live.app)
    first = run_job(live, client, max_pages_per_ticker=400)
    assert first["progress"]["ABCD"]["state"] == "history_cutoff"
    live._baseline_job_runner = None
    second = run_job(live, client, max_pages_per_ticker=400)      # nothing left to fetch this time
    assert second["status"] == "completed" and second["progress"]["ABCD"]["state"] == "history_cutoff"
