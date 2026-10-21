"""Regression tests from the independent jobs / scheduler / provider-request audit (J1-J4, S1-S4,
P1-P4, G1-G2). Each test asserts the CORRECT behaviour: all except the G1 sweep (no provider request
from any GET route, already true) failed on 345decb. Network-free: fakes only."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import baseline_jobs
from app.adapters.live_uw_adapter import CircuitBreaker, RequestTelemetry, UWRestClient
from app.ingestion import IngestionCatalog
from app.scheduler import SchedulerRunner
from tests.test_baseline_jobs import build_uw, live, stop_job_threads  # noqa: F401 -- fixture reuse
from tests.uw_fakes import FakeResponse, FakeUWSession


# --------------------------------------------------------------------------
# J1: a job created (or resumed) while the previous job's runner thread is
# still finishing is persisted as 'running' but NO thread ever runs it.
# --------------------------------------------------------------------------

def _linger_after(store, to_status, reached, release):
    real = store.transition

    def transition(job_id, allowed_from, to):
        out = real(job_id, allowed_from, to)
        if to == to_status and out is not None and threading.current_thread().name.startswith("baseline-job-"):
            reached.set()
            release.wait(30)            # the runner thread is still alive here (about to return)
        return out

    store.transition = transition


def test_J1_create_while_previous_thread_finishing_orphans_a_running_job(live):
    live.use_uw(build_uw())
    client = TestClient(live.app)
    store = live.get_baseline_job_store()
    reached, release = threading.Event(), threading.Event()
    _linger_after(store, "completed", reached, release)
    try:
        a = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 200,
                                                    "max_pages_per_ticker": 2000}).json()["job"]
        assert reached.wait(60), "job A never completed"
        assert store.get(a["id"])["status"] == "completed"
        r = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 20})
        b = r.json()["job"]
        print("\nJ1 create B ->", r.status_code, b["status"])
    finally:
        release.set()
    live.get_baseline_job_runner().join(30)
    b_after = store.get(b["id"])
    alive = live.get_baseline_job_runner().is_running()
    print("J1 job B after A's thread exited:", b_after["status"], "thread alive:", alive,
          "steps:", b_after["progress"]["ABCD"]["steps"])
    # third create is now refused forever; pause/cancel are never honoured
    r3 = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 20})
    rc = client.post(f"/api/baseline/jobs/{b['id']}/cancel")
    print("J1 create C ->", r3.status_code, r3.json().get("detail"), "| cancel B ->", rc.status_code,
          store.get(b["id"])["status"])
    # cleanup for the fixture: nobody will ever move cancel_requested -> cancelled
    store.transition(b["id"], ("cancel_requested", "running"), "cancelled")
    assert b_after["status"] != "running" or alive, "job B is 'running' with no runner thread (orphaned)"


def test_J2_resume_while_thread_finishing_leaves_status_running_without_thread(live, monkeypatch):
    live.use_uw(build_uw())
    client = TestClient(live.app)
    store = live.get_baseline_job_store()
    real_step = live._baseline_job_step

    def step_then_pause(ticker, pages, **kw):
        r = real_step(ticker, pages, **kw)
        j = store.latest()
        store.transition(j["id"], ("running",), "pause_requested")
        return r

    monkeypatch.setattr(live, "_baseline_job_step", step_then_pause)
    live._baseline_job_runner = None
    reached, release = threading.Event(), threading.Event()
    _linger_after(store, "paused", reached, release)
    try:
        a = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 20}).json()["job"]
        assert reached.wait(60)
        assert store.get(a["id"])["status"] == "paused"
        r = client.post(f"/api/baseline/jobs/{a['id']}/resume", json={"confirm": True})
        print("\nJ2 resume ->", r.status_code, r.json().get("detail"), "| status now:", store.get(a["id"])["status"])
    finally:
        release.set()
    live.get_baseline_job_runner().join(30)
    after = store.get(a["id"])
    alive = live.get_baseline_job_runner().is_running()
    print("J2 after thread exit:", after["status"], "thread alive:", alive)
    store.transition(a["id"], ("running", "pause_requested", "cancel_requested"), "cancelled")
    assert not (r.status_code == 409 and after["status"] == "running" and not alive), \
        "resume was rejected (409) yet the job is left 'running' with no thread"


# --------------------------------------------------------------------------
# J3: a Cancel requested during a step that then raises is silently turned
# into 'paused' (resumable) -- the user's cancel is lost.
# --------------------------------------------------------------------------

class _MemStore:
    """Minimal in-memory BaselineJobStore stand-in (same API the runner uses)."""

    def __init__(self, job):
        self.job = job
        self.fail_updates = False

    def get(self, job_id):
        import copy
        return copy.deepcopy(self.job)

    def update(self, job_id, **fields):
        if self.fail_updates:
            raise RuntimeError("database is locked")
        import copy
        self.job.update(copy.deepcopy(fields))

    def transition(self, job_id, allowed_from, to):
        if self.job["status"] in allowed_from:
            self.job["status"] = to
            return self.get(job_id)
        return None


def _job():
    return {"id": 1, "status": "running", "tickers": ["ABCD"], "max_pages_per_ticker": 100,
            "step_pages": 10, "pages_used": 0, "target_sessions": 20, "last_error": None,
            "progress": {"ABCD": {"state": "pending", "pages_used": 0, "steps": 0, "datasets": {},
                                  "last_error": None}}}


def test_J3_cancel_requested_during_a_failing_step_becomes_paused(live):
    store = _MemStore(_job())

    def step(ticker, pages, **kw):
        store.job["status"] = "cancel_requested"     # user clicks Cancel while the step runs
        raise RuntimeError("boom mid-step")

    runner = baseline_jobs.BaselineJobRunner(store, step)
    runner._run(1)
    print("\nJ3 final status:", store.job["status"], "| last_error:", store.job["last_error"])
    assert store.job["status"] == "cancelled", "a requested cancel was overwritten to a resumable 'paused'"


def test_J4_db_error_while_recording_failure_leaves_job_running_without_thread(live):
    store = _MemStore(_job())

    def step(ticker, pages, **kw):
        store.fail_updates = True                    # DB becomes unwritable (locked / disk) mid-step
        raise RuntimeError("provider exploded")

    runner = baseline_jobs.BaselineJobRunner(store, step)
    t = threading.Thread(target=runner._run, args=(1,))
    t.start(); t.join(10)
    print("\nJ4 status after the runner thread died:", store.job["status"])
    assert store.job["status"] != "running", "job left 'running' with no thread"


# --------------------------------------------------------------------------
# S1: switching the scheduler to 'disabled' does not stop a cycle already
# in progress -- it keeps evaluating the remaining top_n tickers and runs
# the baseline backfill afterwards.
# --------------------------------------------------------------------------

MARKET_OPEN = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


class _Adapter:
    def __init__(self, rows, unavailable=False):
        self.rows, self.unavailable, self.discover_calls = rows, unavailable, 0

    def discover_candidates(self, filters=None):
        self.discover_calls += 1
        if self.unavailable:
            return {"candidates": [], "snapshot_at": "2026-10-14T15:00:00+00:00", "screener_requests_used": 1,
                    "source": "fake", "unavailable": True,
                    "reason": {"status": "error", "status_code": 429, "detail": "rate limited"}}
        return {"candidates": self.rows, "snapshot_at": "2026-10-14T15:00:00+00:00", "screener_requests_used": 1,
                "source": "fake", "unavailable": False, "reason": None}

    def get_universe(self):
        return []


def test_S1_disabling_mid_cycle_does_not_stop_evaluation_or_backfill(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    rows = [{"ticker": t, "net_premium": str(1_000_000 * (9 - i))} for i, t in enumerate("ABCDE")]
    scans, backfills = [], []
    holder = {}

    def scan(adapter, ticker):
        scans.append(ticker)
        if len(scans) == 1:
            holder["r"].store.set_mode("disabled", actor="user")    # user clicks Disable now
        return {}

    r = SchedulerRunner(db_path=db, get_adapter=lambda: _Adapter(rows), get_catalog=lambda: catalog,
                        run_ticker_scan=scan, run_baseline_backfill=lambda a: backfills.append(1) or {},
                        top_n=5, lease_ttl_s=90, poll_interval_s=1)
    holder["r"] = r
    r.store.set_mode("enabled")
    out = r.run_one_cycle_if_due(now=MARKET_OPEN)
    print("\nS1 scans after disable:", scans[1:], "| backfill runs:", len(backfills), "| mode:", r.store.get_mode())
    assert scans == scans[:1] and not backfills, "provider work continued after mode was set to 'disabled'"


# --------------------------------------------------------------------------
# S2: the lease is never renewed while a cycle runs, so a cycle longer than
# the TTL lets a second worker run a concurrent cycle; the first then keeps
# writing state after losing the lease.
# --------------------------------------------------------------------------

def test_S2_long_cycle_outlives_its_lease_and_a_second_worker_runs_concurrently(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    rows = [{"ticker": "AAA", "net_premium": "5000000"}]
    concurrent = {"max": 0, "now": 0}
    lock = threading.Lock()
    second = {}

    def scan(adapter, ticker):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        try:
            if "go" not in second:
                second["go"] = True
                # 100 s into A's cycle (TTL 90 s): worker B (another process on the same DB)
                second["out"] = b.run_once_with_depth("discovery_plus_evaluation",
                                                      now=MARKET_OPEN + timedelta(seconds=100))
        finally:
            with lock:
                concurrent["now"] -= 1
        return {}

    mk = lambda wid: SchedulerRunner(db_path=db, get_adapter=lambda: _Adapter(rows), get_catalog=lambda: catalog,
                                     run_ticker_scan=scan, worker_id=wid, top_n=1, lease_ttl_s=90,
                                     poll_interval_s=1)
    a, b = mk("worker-A"), mk("worker-B")
    a.store.set_mode("enabled")
    a.run_one_cycle_if_due(now=MARKET_OPEN)
    print("\nS2 worker B result while A mid-cycle:", second.get("out"), "| max concurrent scans:", concurrent["max"],
          "| lease now held by:", a.store.get_lease()["worker_id"])
    assert concurrent["max"] == 1, "two workers ran per-ticker evaluation concurrently"


# --------------------------------------------------------------------------
# S3: a failed screener request (429/5xx/circuit open) is recorded as a
# successful cycle and cataloged as an EMPTY market snapshot that replaces
# the last good one on Market-wide Radar.
# --------------------------------------------------------------------------

def test_S3_failed_discovery_reported_as_success_and_empty_snapshot_cataloged(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    good = _Adapter([{"ticker": "NVDA", "net_premium": "5000000"}])
    bad = _Adapter([], unavailable=True)
    cur = {"a": good}
    r = SchedulerRunner(db_path=db, get_adapter=lambda: cur["a"], get_catalog=lambda: catalog,
                        run_ticker_scan=lambda a, t: {}, top_n=1, scan_interval_s=60, poll_interval_s=1)
    r.store.set_mode("dry_run")
    r.run_one_cycle_if_due(now=MARKET_OPEN)
    cur["a"] = bad
    bad_snapshot_time = "2026-10-14T15:05:00+00:00"
    bad.discover_candidates = (lambda f=None, _orig=bad.discover_candidates: {**_orig(f), "snapshot_at": bad_snapshot_time})
    out = r.run_one_cycle_if_due(now=MARKET_OPEN + timedelta(minutes=5))
    state = r.store.get_state()
    with catalog._conn() as conn:
        latest = conn.execute("SELECT event_time, candidate_count FROM discovery_snapshots "
                              "ORDER BY event_time DESC LIMIT 1").fetchone()
    print("\nS3 cycle outcome:", out, "| pause_reason:", state["pause_reason"], "| latest snapshot:", latest)
    assert out.get("pause_reason") or state["pause_reason"], "a failed screener call was reported as a clean cycle"
    assert latest[1] != 0, "the failed call was cataloged as an empty market snapshot (hides last good one)"


# --------------------------------------------------------------------------
# P1: Retry-After is honoured without any upper bound.
# P2: a 200 with an unparseable body is counted as a provider 'success'.
# P3: the circuit breaker is keyed by full path (incl. ticker), so a
#     provider-wide 429 storm is never tripped across tickers.
# --------------------------------------------------------------------------

def _client(session, **kw):
    slept = []
    c = UWRestClient("k", base_url="http://127.0.0.1:9", session=session, sleep_fn=slept.append,
                     jitter_fn=lambda: 0.0, telemetry=RequestTelemetry(), **kw)
    return c, slept


def test_P1_retry_after_is_unbounded():
    s = FakeUWSession({"/api/stock/": [FakeResponse(429, {"e": 1}, headers={"Retry-After": "86400"}),
                                       FakeResponse(200, {"data": {}})]})
    c, slept = _client(s, max_retries=2)
    body = c.stock_state("ABCD")
    print("\nP1 slept seconds before retry:", slept)
    # fixed: a Retry-After above MAX_RETRY_AFTER_S is never slept on; the call fails fast as rate limited
    assert all(x <= 120 for x in slept), "a single Retry-After header parked the calling thread for a day"
    assert slept == [] and body.get("ok") is False and len(s.calls) == 1


def test_P2_unparseable_200_body_is_recorded_as_success():
    s = FakeUWSession({"/api/stock/": FakeResponse(200, raw_text="<html>gateway</html>")})
    c, _ = _client(s, max_retries=0)
    body = c.stock_state("ABCD")
    summ = c.telemetry.summary()["/api/stock/ABCD/stock-state"]
    print("\nP2 body status:", body.get("status"), "| telemetry:", summ["by_outcome"], "success:", summ["success"])
    assert summ["success"] == 0, "malformed body counted as a successful provider request"


def test_P3_circuit_breaker_never_trips_on_a_provider_wide_429_storm():
    s = FakeUWSession({"/api/stock/": FakeResponse(429, {"e": 1})})
    c, _ = _client(s, max_retries=2, circuit_breaker=CircuitBreaker(threshold=5, cooldown_s=30))
    for i in range(20):
        c.stock_state(f"T{i:02d}")
    print("\nP3 HTTP attempts sent during a provider-wide 429 storm (20 tickers):", len(s.calls))
    assert len(s.calls) <= 5 * 3, "no breaker ever opened: every ticker paid full retries against a 429 wall"


# --------------------------------------------------------------------------
# G1: sweep every GET route in Live Mode (empty catalog, then after one
# explicit refresh) -- zero UW HTTP requests expected.
# --------------------------------------------------------------------------

def test_G1_every_get_route_makes_zero_uw_requests(app_main, monkeypatch, tmp_path):
    from tests.uw_fakes import fake_live_adapter, ok
    monkeypatch.setattr(app_main.settings, "db_path", str(tmp_path / "g.db"))
    app_main._catalog = None
    app_main._store = None
    app_main._baseline_job_store = None
    app_main._baseline_job_runner = None
    app_main._reset_scheduler_runner()
    adapter = fake_live_adapter({"stock-state": ok({"close": "10", "prev_close": "9"}),
                                 "flow-alerts": ok([]), "/api/darkpool/": ok([]),
                                 "/ohlc/": ok([]), "screener": ok([])}, watchlist=["ABCD"])
    monkeypatch.setattr(app_main, "get_adapter", lambda: adapter)
    app_main._settings_store.set_watchlist(["ABCD"])
    client = TestClient(app_main.app)
    gets = ["/api/health", "/api/mode", "/api/auth/mode", "/api/settings", "/api/settings/watchlist",
            "/api/diagnostics/collection-bounds", "/api/diagnostics/uw-requests", "/api/scheduler/status",
            "/api/radar/market-wide", "/api/settings/version", "/api/radar", "/api/ticker/ABCD/baseline",
            "/api/baseline/overview", "/api/baseline/jobs/current", "/api/ticker/ABCD",
            "/api/ticker/ABCD/ohlc", "/api/ticker/ABCD/ohlc?range=1D", "/api/ticker/ABCD/ohlc?range=1Y",
            "/api/ticker/MSFT", "/api/ticker/MSFT/ohlc", "/api/investigate/availability", "/api/scenarios", "/"]
    offenders = {}
    for phase in ("empty", "after_refresh"):
        if phase == "after_refresh":
            rr = client.post("/api/ticker/ABCD/refresh", json={"confirm": True})
            print("G1 refresh status", rr.status_code, "calls", len(adapter.fake_session.calls))
        for g in gets:
            before = len(adapter.fake_session.calls)
            client.get(g)
            n = len(adapter.fake_session.calls) - before
            if n:
                offenders[f"{phase}:{g}"] = n
    print("\nG1 GET routes that made UW requests:", offenders)
    assert not offenders


def test_G2_unauthenticated_cross_site_get_triggers_outbound_github_call(app_main, monkeypatch):
    from app import update_check
    calls = []

    class _R:
        status_code = 200
        def json(self):
            return {"status": "identical", "commits": [], "ahead_by": 0}

    monkeypatch.setattr(update_check.requests, "get", lambda *a, **k: calls.append(a) or _R())
    monkeypatch.setattr(app_main._version, "get_build_info", lambda: {"commit": "a" * 40})
    client = TestClient(app_main.app)
    client.headers.pop("X-TIP-Auth", None)
    for _ in range(5):
        r = client.get("/api/settings/update-check", headers={"Sec-Fetch-Site": "cross-site"})
    print("\nG2 status:", r.status_code, "| outbound GitHub requests from 5 unauthenticated cross-site GETs:", len(calls))
    assert len(calls) <= 1


def test_S4_persisted_mode_makes_provider_requests_at_startup_without_user_action(app_main, monkeypatch, tmp_path):
    import time
    db = str(tmp_path / "boot.db")
    monkeypatch.setattr(app_main.settings, "db_path", db)
    app_main._catalog = None
    catalog = IngestionCatalog(db)
    adapter = _Adapter([{"ticker": "NVDA", "net_premium": "5000000"}])
    runner = SchedulerRunner(db_path=db, get_adapter=lambda: adapter, get_catalog=lambda: catalog,
                             run_ticker_scan=lambda a, t: {}, top_n=1, poll_interval_s=1,
                             now_fn=lambda: MARKET_OPEN)
    runner.store.set_mode("dry_run")           # armed in a PREVIOUS process lifetime
    monkeypatch.setattr(app_main, "_scheduler_runner", runner)
    monkeypatch.setattr(app_main, "get_scheduler_runner", lambda: runner)
    with TestClient(app_main.app):             # process start; no request is made by anyone
        deadline = time.time() + 5
        while adapter.discover_calls == 0 and time.time() < deadline:
            time.sleep(0.05)
    print("\nS4 screener requests issued by startup alone:", adapter.discover_calls)
    assert adapter.discover_calls == 0


def test_P4_network_error_backoff_sleeps_while_holding_a_concurrency_slot():
    import requests as _rq
    from app.core import uw_concurrency
    uw_concurrency.reset_shared_gate_for_tests()

    class _S:
        def get(self, *a, **k):
            raise _rq.exceptions.ConnectionError("reset")

    held = []
    c = UWRestClient("k", base_url="http://127.0.0.1:9", session=_S(), max_retries=2, jitter_fn=lambda: 0.0,
                     sleep_fn=lambda s: held.append(c._gate.interactive_in_flight + c._gate.background_in_flight),
                     max_concurrent_requests=2)
    c.stock_state("ABCD")
    print("\nP4 in-flight slots held during each backoff sleep:", held)
    uw_concurrency.reset_shared_gate_for_tests()
    assert all(h == 0 for h in held)
