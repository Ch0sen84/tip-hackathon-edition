"""Round-2 jobs/scheduler review (J2-1..J2-8), converted from the reviewer's reproductions into regression
tests asserting the corrected behaviour.

Audit-2 (jobs / scheduler / confirmation contract) reproductions against 49e241c..339c84f.

Each test asserts the behaviour the confirmation contract / scheduler copy promises; a FAIL is a
finding (the printed line carries the evidence). Network-free: PaginatedUW / fakes only."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app import baseline_jobs
from app.ingestion import IngestionCatalog
from app.scheduler import SchedulerRunner
from tests.test_audit_jobs_regressions import MARKET_OPEN, _Adapter
from tests.test_baseline_jobs import build_uw, live, stop_job_threads  # noqa: F401 -- fixture reuse


def _uw_calls(uw):
    return len(uw.calls)


# J2-1: Resume restarts provider spending with no 428 (the UI's apiPostConfirmed never gets to ask).
def test_J2_1_resume_spends_without_server_confirmation(live, monkeypatch):
    uw = live.use_uw(build_uw())
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
    job = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 2000}).json()["job"]
    live.get_baseline_job_runner().join(30)
    assert store.get(job["id"])["status"] == "paused"
    before = _uw_calls(uw)
    r = client.post(f"/api/baseline/jobs/{job['id']}/resume")          # no body, no confirm
    live.get_baseline_job_runner().join(30)
    after = _uw_calls(uw)
    print(f"\nJ2-1 resume without confirm -> HTTP {r.status_code}; UW requests made by the resume: {after - before}; "
          f"remaining job budget: {2000 - store.get(job['id'])['progress']['ABCD']['pages_used']} pages")
    assert r.status_code == 428, "resume re-arms up to the remaining page budget without any confirmation"


# J2-2: the per-ticker historical backfill route spends up to 60 (cap) pages with no confirmation.
def test_J2_2_backfill_route_spends_without_confirmation(live):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    r = client.post("/api/ticker/ABCD/baseline/backfill")
    print(f"\nJ2-2 POST /baseline/backfill (no confirm) -> HTTP {r.status_code}; UW requests: {_uw_calls(uw)}")
    assert r.status_code == 428, "historical backfill route makes provider requests without the 428 contract"


# J2-3: Live single-ticker / watchlist refresh: the budget confirmation exists only in the browser.
def test_J2_3_live_refresh_routes_have_no_server_confirmation(live):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    r1 = client.post("/api/ticker/ABCD/refresh", json={})
    n1 = _uw_calls(uw)
    r2 = client.post("/api/radar/refresh", json={})
    n2 = _uw_calls(uw) - n1
    print(f"\nJ2-3 /ticker/ABCD/refresh -> {r1.status_code} ({n1} UW requests); /radar/refresh -> {r2.status_code} "
          f"({n2} UW requests); neither asked for confirmation")
    assert r1.status_code == 428 or r2.status_code == 428, "Live refreshes spend with no server-side confirmation"


# J2-4: "Paused: discovery unavailable" is only a label -- the recurring cycle keeps firing every interval.
def test_J2_4_discovery_unavailable_pause_does_not_pause(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    bad = _Adapter([], unavailable=True)
    r = SchedulerRunner(db_path=db, get_adapter=lambda: bad, get_catalog=lambda: catalog,
                        run_ticker_scan=lambda a, t: {}, top_n=1, scan_interval_s=60, poll_interval_s=1)
    r.store.set_mode("enabled")
    r.run_one_cycle_if_due(now=MARKET_OPEN)
    s1 = r.store.get_state()
    r.run_one_cycle_if_due(now=MARKET_OPEN + timedelta(seconds=61))
    r.run_one_cycle_if_due(now=MARKET_OPEN + timedelta(seconds=122))
    s3 = r.store.get_state()
    print(f"\nJ2-4 pause_reason={s1['pause_reason']!r} next_run_at={s1['next_run_at']} | screener calls after 3 "
          f"intervals: {bad.discover_calls} | mode still {s3['mode']!r}")
    # Resolved by honest wording (the reviewer's first fix option): a failed screener retries once per interval
    # (bounded: 1 request per cycle), and the status no longer claims anything is paused or shows a raw dict.
    assert bad.discover_calls == 3
    reason = s1["pause_reason"] or ""
    assert reason.startswith("last cycle failed") and "Paused" not in reason and "{" not in reason
    assert s3["mode"] == "enabled"


# J2-5: lazily created job store/runner singletons are not created under a lock: two concurrent first
# requests can end up with two BaselineJobRunner objects, so one job thread is invisible to the other.
def test_J2_5_job_runner_singleton_race(live, monkeypatch):
    live.use_uw(build_uw())
    real_init = baseline_jobs.BaselineJobRunner.__init__
    gate = threading.Barrier(2, timeout=5)

    def slow_init(self, *a, **k):
        real_init(self, *a, **k)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass

    monkeypatch.setattr(baseline_jobs.BaselineJobRunner, "__init__", slow_init)
    got = []
    ts = [threading.Thread(target=lambda: got.append(live.get_baseline_job_runner())) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    distinct = len({id(x) for x in got})
    print(f"\nJ2-5 concurrent first get_baseline_job_runner() -> {distinct} distinct runner object(s)")
    assert distinct == 1, "two runners: is_running()/reconcile_orphans() of one cannot see the other's thread"


# J2-6: the baseline-panel copy and step semantics: a job step is not interruptible; time a cancel takes
# with Retry-After at the new cap (60 s) x retries. Informational -- asserts the cap is respected.
def test_J2_6_retry_after_cap_bounds_one_page():
    from app.adapters.live_uw_adapter import MAX_RETRY_AFTER_S, RequestTelemetry, UWRestClient
    from tests.uw_fakes import FakeResponse, FakeUWSession
    slept = []
    s = FakeUWSession({"/api/stock/": [FakeResponse(429, {}, headers={"Retry-After": "60"})] * 3
                       + [FakeResponse(200, {"data": {}})]})
    c = UWRestClient("k", base_url="http://127.0.0.1:9", session=s, sleep_fn=slept.append,
                     jitter_fn=lambda: 0.0, telemetry=RequestTelemetry(), max_retries=3)
    c.stock_state("ABCD")
    print(f"\nJ2-6 one page with Retry-After=60 x3: slept {sum(slept):.0f}s (cap {MAX_RETRY_AFTER_S}s per wait); "
          f"a 10-page job step can therefore hold _baseline_backfill_lock + the ticker's _collection_lock "
          f"for ~{10 * sum(slept) / 60:.0f} min")
    assert all(x <= MAX_RETRY_AFTER_S for x in slept)


# J2-7 (sound check): historical backfill runs only on a recurring enabled cycle -- never from the
# legacy no-depth run-once, never from run_once_with_depth.
def test_J2_7_backfill_only_on_recurring_cycles(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    rows = [{"ticker": "AAA", "net_premium": "5000000", "evaluate_supported": True}]
    calls = []
    r = SchedulerRunner(db_path=db, get_adapter=lambda: _Adapter(rows), get_catalog=lambda: catalog,
                        run_ticker_scan=lambda a, t: {}, run_baseline_backfill=lambda a: calls.append(1) or {},
                        top_n=1, scan_interval_s=60, poll_interval_s=1)
    r.store.set_mode("enabled")
    r.run_once(now=MARKET_OPEN)
    legacy = len(calls)
    r.run_once_with_depth("discovery_plus_evaluation", now=MARKET_OPEN)
    depth = len(calls) - legacy
    r.store.record_run_finished(candidates_evaluated=0, next_run_at=None)
    r.run_one_cycle_if_due(now=MARKET_OPEN + timedelta(minutes=10))
    recurring = len(calls) - legacy - depth
    print(f"\nJ2-7 backfills: legacy run-once={legacy} depth run-once={depth} recurring cycle={recurring}")
    assert (legacy, depth, recurring) == (0, 0, 1)


# J2-8 (sound check): disabling during a recurring cycle stops before the next unit of provider work.
def test_J2_8_disable_mid_cycle_aborts_before_next_ticker(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    rows = [{"ticker": t, "net_premium": "5000000", "evaluate_supported": True} for t in ("AAA", "BBB", "CCC")]
    scans, holder = [], {}

    def scan(a, t):
        scans.append(t)
        holder["r"].store.set_mode("disabled", actor="user")
        return {}

    r = SchedulerRunner(db_path=db, get_adapter=lambda: _Adapter(rows), get_catalog=lambda: catalog,
                        run_ticker_scan=scan, run_baseline_backfill=lambda a: scans.append("BACKFILL") or {},
                        top_n=3, scan_interval_s=60, poll_interval_s=1)
    holder["r"] = r
    r.store.set_mode("enabled")
    out = r.run_one_cycle_if_due(now=MARKET_OPEN)
    print(f"\nJ2-8 work after disable: {scans[1:]} pause_reason={out['pause_reason']!r}")
    assert scans == ["AAA"]
