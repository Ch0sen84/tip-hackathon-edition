"""Audit-3 reproductions (7206583). A FAIL is a finding; the printed line carries the evidence.
Network-free: scheduler fakes only."""
from __future__ import annotations

from tests.test_audit_jobs_regressions import MARKET_OPEN, _Adapter
from tests.test_baseline_jobs import live  # noqa: F401 -- fixture reuse
from app.ingestion import IngestionCatalog
from app.scheduler import SchedulerRunner


# R3F-6: a ONE-TIME run (recurring mode Disabled) whose screener fails records and returns
# "... the next scheduled cycle retries" -- but nothing is scheduled: Disabled never retries.
def test_R3F6_one_time_failure_promises_a_retry_that_never_comes(tmp_path):
    db = str(tmp_path / "s.db")
    catalog = IngestionCatalog(db)
    bad = _Adapter([], unavailable=True)
    r = SchedulerRunner(db_path=db, get_adapter=lambda: bad, get_catalog=lambda: catalog,
                        run_ticker_scan=lambda a, t: {}, top_n=1, scan_interval_s=60, poll_interval_s=1)
    assert r.store.get_mode() == "disabled"
    res = r.run_once_with_depth("discovery_only", now=MARKET_OPEN)
    st = r.store.get_state()
    print(f"\nR3F-6 result.pause_reason={res['pause_reason']!r}\n      state.pause_reason={st['pause_reason']!r} "
          f"next_run_at={st['next_run_at']!r} mode={st['mode']!r}")
    # The UI shows this verbatim both in the one-time result line and in Market Intelligence's status.
    assert "next scheduled cycle retries" not in (res["pause_reason"] or ""), \
        "one-time run with recurring mode Disabled claims a scheduled retry"


# R3F-7: the Resume 428 (J2-1 fix) counts tickers that will never get another page. Only "pending"/"running"
# tickers receive work (baseline_jobs.py:262); the exclusion list names states that do not exist
# ("done", "cutoff", "target_reached") and misses "history_cutoff" / "no_more_work".
def test_R3F7_resume_budget_counts_finished_tickers(live):  # noqa: F811
    from fastapi.testclient import TestClient
    client = TestClient(live.app)
    store = live.get_baseline_job_store()
    job = store.create(["AAA", "BBB", "CCC"], max_pages_per_ticker=400, step_pages=10, requested_by="test")
    prog = job["progress"]
    prog["AAA"].update(state="history_cutoff", pages_used=12)
    prog["BBB"].update(state="no_more_work", pages_used=3)
    prog["CCC"].update(state="pending", pages_used=0)
    store.update(job["id"], progress=prog, status="paused")
    r = client.post(f"/api/baseline/jobs/{job['id']}/resume")
    d = r.json()["detail"]
    print(f"\nR3F-7 HTTP {r.status_code} remaining_pages={d.get('remaining_pages')} | {d.get('consequence')}")
    assert r.status_code == 428
    assert d["remaining_pages"] == 400, "only CCC can still receive pages (400); finished tickers are counted too"
