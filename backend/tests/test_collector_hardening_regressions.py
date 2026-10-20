"""Hardening-pass regressions (found on 3960eae by adversarial review; all but
D6 also present on 147e315). Each test is the minimal reproduction.

D1  a budget-interrupted dark-pool walk, resumed later, skipped the prints of
    the boundary second (island started at the oldest row instead of the
    next cursor) and still certified the session complete
D2  a full page that is not newest-first (the cursor contract) was accepted;
    a provider returning oldest-first pages certified 500 of 3000 prints
D3  zero/negative budgets became requests (route, job) and
    update_session_coverage(max_pages=0) overwrote a valid checkpoint
D4  pages spent by a job step that raised were never charged, so a resume
    could exceed max_pages_per_ticker
D5  dark-pool prints stamped exactly at the close were never requested
    (exclusive older_than = close) yet the session was certified complete
D6  HTTP attempts made by a job step that raised were not reported
Constructed data only."""
import os
import tempfile
from datetime import date, datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.ingestion import IngestionCatalog, darkpool_print_key
from tests.test_baseline_jobs import build_uw, live  # noqa: F401 (fixture)
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, PaginatedUW, _ts, iso, session_alerts, session_prints

DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 16, 16, tzinfo=timezone.utc)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "h.db"))


def per_second(k, n):
    out, t, i = [], OPEN, 0
    while len(out) < n:
        for j in range(k):
            out.append({"ticker": "ABCD", "executed_at": iso(t), "price": "1", "size": 1 + j, "premium": "1",
                        "tracking_id": f"p{i}", "volume": i})
            i += 1
        t += timedelta(seconds=2)
    return out


def keys(cat):
    return {darkpool_print_key("ABCD", r) for r in cat.observations_between("darkpool", "ABCD", OPEN, CLOSE)}


def walk(uw, cat, pages, dataset="darkpool"):
    return sc.update_session_coverage(uw.adapter().client, cat, dataset, "ABCD", DAY, now_utc=NOW, max_pages=pages)


@pytest.mark.parametrize("budgets", [[1, 50], [1, 1, 1, 50], [2, 3, 50], [3, 1, 2, 1, 50]])
def test_d1_resumed_dark_pool_walk_never_skips_the_boundary_second(cat, budgets):
    rows = per_second(3, 2000)                        # 500-row pages end mid-second
    uw = PaginatedUW(darkpool=rows)
    first = walk(uw, cat, budgets[0])
    assert first.coverage["status"] == "partial"
    if budgets[0] == 1:                               # the island starts at the NEXT cursor, not the oldest row
        oldest_on_page = min(_ts(r["executed_at"]) for r in sorted(rows, key=lambda r: r["executed_at"], reverse=True)[:500])
        assert _ts(first.coverage["covered_from_utc"]) == oldest_on_page + timedelta(seconds=1)
    for b in budgets[1:]:
        u = walk(uw, cat, b)
    assert u.coverage["status"] == "complete"
    assert keys(cat) == {darkpool_print_key("ABCD", r) for r in rows}


def test_d1_flow_resume_rereads_the_oldest_instant(cat):
    alerts = []
    for a in session_alerts("ABCD", DAY, 1.0, every_minutes=1):
        alerts += [{**a, "id": f"{a['id']}-{j}"} for j in range(3)]        # 3 alerts share each instant
    uw = PaginatedUW(flow=alerts)
    first = walk(uw, cat, 1, "options_flow")
    assert first.coverage["status"] == "partial"
    while True:
        u = walk(uw, cat, 50, "options_flow")
        if u.coverage["status"] != "partial":
            break
    assert u.coverage["status"] == "complete"
    got = {r["id"] for r in cat.observations_between("options_flow", "ABCD", OPEN, CLOSE)}
    assert got == {a["id"] for a in alerts}


class OldestFirst(PaginatedUW):
    def get(self, url, headers=None, params=None, timeout=None):
        r = super().get(url, headers, params, timeout)
        if "/api/darkpool/" in url and r.status_code == 200:
            p = dict(params)
            rows = [x for x in self.darkpool["ABCD"] if _ts(p["newer_than"]) <= _ts(x["executed_at"]) < _ts(p["older_than"])]
            rows.sort(key=lambda x: x["executed_at"])
            return FakeResponse(200, {"data": rows[:int(p["limit"])]})
        return r


def test_d2_pages_that_are_not_newest_first_are_rejected(cat):
    rows = [{"ticker": "ABCD", "executed_at": iso(OPEN + timedelta(seconds=7 * i)), "price": "1", "size": 1,
             "premium": "1", "tracking_id": f"p{i}", "volume": i} for i in range(3000)]
    u = walk(OldestFirst(darkpool=rows), cat, 50)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "rows_out_of_order"
    assert keys(cat) == set()
    [d] = cat.list_collection_diagnostics("darkpool", "ABCD")[:1]
    assert d["classification"] == "rows_out_of_order" and d["violations"]["out_of_order"] > 0


def test_d2_equal_timestamps_in_any_order_are_not_out_of_order(cat):
    rows = per_second(31, 2000)                       # many prints per second: equal, not increasing
    u = walk(PaginatedUW(darkpool=rows), cat, 50)
    assert u.coverage["status"] == "complete" and keys(cat) == {darkpool_print_key("ABCD", r) for r in rows}


def test_d3_zero_budget_makes_no_request_and_keeps_the_checkpoint(cat):
    rows = per_second(1, 1500)
    uw = PaginatedUW(darkpool=rows)
    walk(uw, cat, 1)
    before = cat.get_session_coverage("darkpool", "ABCD", DAY.isoformat())
    n = len(uw.calls)
    for budget in (0, -1):
        u = walk(uw, cat, budget)
        assert u.pages == 0 and len(uw.calls) == n
    after = cat.get_session_coverage("darkpool", "ABCD", DAY.isoformat())
    assert after == before                            # unchanged (incl. retrieved_at)


def test_d3_routes_refuse_budgets_below_one_without_a_request(live):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    for mp in (0, -5):
        assert client.post(f"/api/ticker/ABCD/baseline/backfill?max_pages={mp}").status_code == 400
    assert client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "max_pages_per_ticker": 0}).status_code == 400
    assert not [u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u]
    assert live.get_baseline_job_store().latest() is None


def test_d4_d6_pages_and_attempts_of_a_failing_step_are_charged(live, monkeypatch):
    uw = live.use_uw(build_uw())
    cat = live.get_catalog()
    real, n = cat.upsert_session_coverage, {"i": 0}

    def flaky(*a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise RuntimeError("storage guard: disk below minimum")
        return real(*a, **k)
    monkeypatch.setattr(cat, "upsert_session_coverage", flaky)
    client = TestClient(live.app)
    job = client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 20}).json()["job"]
    live.get_baseline_job_runner().join(30)
    store = live.get_baseline_job_store()
    j = store.get(job["id"])
    provider = lambda: len([u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u])
    assert j["status"] == "paused" and "internal error" in j["last_error"]
    assert j["pages_used"] == 10 and j["progress"]["ABCD"]["reserved_pages"] == 10     # charged before requests
    assert j["progress"]["ABCD"]["http_attempts"] == provider() == 3                     # D6
    client.post(f"/api/baseline/jobs/{job['id']}/resume")
    live.get_baseline_job_runner().join(30)
    j = store.get(job["id"])
    assert j["pages_used"] <= 20 and provider() <= 20                                   # D4 (was 23)
    assert j["progress"]["ABCD"]["http_attempts"] == provider()


def test_d5_a_print_at_the_close_is_collected(cat):
    rows = session_prints("ABCD", DAY, 1.0, every_minutes=5)
    assert _ts(rows[-1]["executed_at"]) == CLOSE
    uw = PaginatedUW(darkpool=rows)
    u = walk(uw, cat, 10)
    assert u.coverage["status"] == "complete" and uw.calls[0][1]["older_than"] == iso(CLOSE + timedelta(seconds=1))
    assert keys(cat) == {darkpool_print_key("ABCD", r) for r in rows}
    after_close = session_prints("ABCD", DAY, 1.0, every_minutes=1, start=time(16, 0, 1), end=time(16, 5))
    cat2 = IngestionCatalog(str(cat.db_path) + ".2")
    walk(PaginatedUW(darkpool=rows + after_close), cat2, 10)
    assert {darkpool_print_key("ABCD", r) for r in cat2.observations_between(
        "darkpool", "ABCD", OPEN, CLOSE + timedelta(hours=1))} == {darkpool_print_key("ABCD", r) for r in rows}


def test_d5_in_progress_sessions_keep_the_exclusive_live_edge(cat):
    live_now = datetime.combine(DAY, time(12, 0), tzinfo=NY).astimezone(timezone.utc)
    uw = PaginatedUW(darkpool=session_prints("ABCD", DAY, 1.0, every_minutes=1))
    u = sc.update_session_coverage(uw.adapter().client, cat, "darkpool", "ABCD", DAY, now_utc=live_now, max_pages=10)
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "session_in_progress"
    assert uw.calls[0][1]["older_than"] == iso(live_now - sc.REPORTING_LAG)


def test_telemetry_meter_isolates_concurrent_threads():
    """The per-thread meter counts only the calling thread's requests, even when
    another thread uses the SAME client concurrently (job step vs a refresh)."""
    import threading
    uw = PaginatedUW(darkpool=session_prints("ABCD", DAY, 1.0, every_minutes=1))
    client = uw.adapter().client
    results, barrier = {}, threading.Barrier(2)

    def worker(name, n):
        with client.telemetry.meter() as m:
            barrier.wait()
            for _ in range(n):
                client.darkpool_ticker("ABCD", date=DAY.isoformat(), limit=5,
                                       newer_than=iso(OPEN), older_than=iso(CLOSE))
            results[name] = dict(m)
    ts = [threading.Thread(target=worker, args=("a", 37)), threading.Thread(target=worker, args=("b", 11))]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert results["a"] == {"logical_requests": 37, "http_attempts": 37}
    assert results["b"] == {"logical_requests": 11, "http_attempts": 11}
    with client.telemetry.meter() as outer:
        with client.telemetry.meter() as inner:
            client.darkpool_ticker("ABCD", date=DAY.isoformat(), limit=5, newer_than=iso(OPEN), older_than=iso(CLOSE))
        client.darkpool_ticker("ABCD", date=DAY.isoformat(), limit=5, newer_than=iso(OPEN), older_than=iso(CLOSE))
    assert inner["logical_requests"] == 1 and outer["logical_requests"] == 2


def test_d4_leaving_live_mode_releases_the_unspent_reservation(live, monkeypatch):
    uw = live.use_uw(build_uw())
    client = TestClient(live.app)
    real_step = live._baseline_job_step
    calls = {"n": 0}

    def step(ticker, pages, datasets=None):
        calls["n"] += 1
        if calls["n"] == 2:
            from app import baseline_jobs
            raise baseline_jobs.JobBlocked("paused: the app is no longer in Live Mode")
        return real_step(ticker, pages, datasets=datasets) if datasets else real_step(ticker, pages)
    monkeypatch.setattr(live, "_baseline_job_step", step)
    live._baseline_job_runner = None
    job = client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 40}).json()["job"]
    live.get_baseline_job_runner().join(30)
    j = live.get_baseline_job_store().get(job["id"])
    provider = len([u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u])
    assert j["status"] == "paused" and "Live Mode" in j["last_error"]
    assert j["pages_used"] == provider == j["progress"]["ABCD"]["pages_used"]       # step 2's 10 pages released
    assert "reserved_pages" not in j["progress"]["ABCD"]
