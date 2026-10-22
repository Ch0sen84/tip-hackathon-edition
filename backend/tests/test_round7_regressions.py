"""Round 7 (reviewer reproductions, kept as regression tests) -- AUDIT7: adversarial verification of the v9 live-qualification commits on top of 7a6a62c
(ac466d3 F-1 backfill order, 27fbf3b chart cap, a6cfe95 dark-pool boundary drop, c3c8c68 docs).

Every test asserts the CORRECT behaviour: a failing test is a finding (see C:/Users/Chad/audit7-report.md).
Constructed data only; no network."""
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.core import trading_calendar as tc
from app.ingestion import IngestionCatalog, darkpool_print_key
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, live, run_job  # noqa: F401 (fixture)
from tests.test_collection_status import live_app, routes  # noqa: F401 (fixture)
from tests.uw_fakes import FakeResponse, ok
from tests.uw_paginated import NY, PaginatedUW, _ts, iso, prior_sessions, session_alerts, session_prints

REPO = Path(__file__).resolve().parents[2]
OPEN_EV = datetime.combine(EVIDENCE, time(9, 30), tzinfo=NY).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def _cat(tmp_path, name="a7.db"):
    return IngestionCatalog(str(tmp_path / name))


def dense(d, start=time(9, 30), end=time(16, 0), step_s=3, tag="d"):
    """One dark-pool print every `step_s` seconds (500-row pages = 25 minutes at 3 s)."""
    out, t, i = [], datetime.combine(d, start, tzinfo=NY), 0
    stop = datetime.combine(d, end, tzinfo=NY)
    while t <= stop:
        out.append({"ticker": "ABCD", "executed_at": iso(t), "price": "100.00", "size": 100 + (i % 7),
                    "premium": "10000.00", "tracking_id": f"{tag}{d.isoformat()}-{i}", "volume": i,
                    "canceled": False})
        t += timedelta(seconds=step_s)
        i += 1
    return out


def ev_dp_calls(uw, start=0):
    return [p for u, p in uw.calls[start:] if "/api/darkpool/" in u and p.get("date") == EVIDENCE.isoformat()]


def prior_dp_calls(uw, start=0):
    return [p for u, p in uw.calls[start:] if "/api/darkpool/" in u and p.get("date") != EVIDENCE.isoformat()]


# =====================================================================================================
# 1. ac466d3 (F-1): backfill order and the job's no_more_work / waiting_now judgement
# =====================================================================================================
class DeepRejectUW(PaginatedUW):
    """The evidence session's dark-pool pages are clean until the cursor reaches `deep`; from there every
    response carries one row above the cursor (persistent provider fault deep in the session)."""

    def __init__(self, *a, deep=None, **k):
        super().__init__(*a, **k)
        self.deep = deep

    def get(self, url, headers=None, params=None, timeout=None):
        resp = super().get(url, headers, params, timeout)
        p = dict(params or {})
        if ("/api/darkpool/" in url and p.get("date") == EVIDENCE.isoformat() and resp.status_code == 200
                and _ts(p["older_than"]) <= self.deep):
            hi = _ts(p["older_than"])
            bad = {"ticker": "ABCD", "tracking_id": "BAD", "executed_at": iso(hi + timedelta(seconds=30)),
                   "price": "1", "size": 1, "premium": "1", "canceled": False}
            return FakeResponse(200, {"data": [bad] + list(resp.json()["data"])[:-1]})
        return resp


def test_r7_1_an_evidence_session_that_cannot_be_proven_does_not_starve_history_forever(tmp_path):
    """F-1 claims step (1) 'catch-up is finite'. An evidence session whose walk is rejected deep in the
    session goes unknown, is re-walked from scratch next call, and so on: history only gets crumbs."""
    deep = datetime.combine(EVIDENCE, time(10, 0), tzinfo=NY).astimezone(timezone.utc)
    rows = dense(EVIDENCE, end=time(12, 0))                                 # ~6 pages to the open
    for d in prior_sessions(EVIDENCE, 8):
        rows += session_prints("ABCD", d, 10_000.0)                         # 1 page each
    uw = DeepRejectUW(darkpool=rows, deep=deep)
    cat, client = _cat(tmp_path), uw.adapter(["ABCD"]).client
    prior_pages = 0
    for k in range(8):                                                      # 8 steps x 4 pages = 32 pages
        r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=4, lookback_sessions=8,
                                 now_utc=AS_OF + timedelta(seconds=k))
        prior_pages += r["prior_pages_used"]
    ev = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert ev["status"] == "unknown"                                       # the evidence session is unprovable
    # correct: once the evidence walk is known to fail, history gets the budget (8 sessions need 8 pages)
    assert r["complete_prior_sessions"] == 8 and prior_pages >= 8, (r["complete_prior_sessions"], prior_pages,
                                                                     len(ev_dp_calls(uw)))


def test_step1_consuming_the_whole_budget_never_reads_as_no_more_work(tmp_path):
    rows = dense(EVIDENCE, end=time(12, 0)) + sum((session_prints("ABCD", d, 1e4) for d in prior_sessions(EVIDENCE, 3)), [])
    uw = PaginatedUW(darkpool=rows)
    cat, client = _cat(tmp_path), uw.adapter(["ABCD"]).client
    r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=3, lookback_sessions=3, now_utc=AS_OF)
    assert r["pages_used"] == 3 and r["prior_budget"] == 0 and r["prior_pages_used"] == 0
    assert not prior_dp_calls(uw)
    # the job's rule: no_more_work only when the prior loop HAD pages and used none
    prior_had_pages = int(r.get("prior_budget", 3) or 0) > 0
    assert not (prior_had_pages and r["prior_pages_used"] == 0)


def test_provider_failure_in_step1_stops_everything_and_reports_the_failure(tmp_path):
    rows = dense(EVIDENCE, end=time(12, 0)) + sum((session_prints("ABCD", d, 1e4) for d in prior_sessions(EVIDENCE, 3)), [])
    uw = PaginatedUW(darkpool=rows)
    real = uw.get

    def failing(url, headers=None, params=None, timeout=None):
        if "/api/darkpool/" in url and (params or {}).get("date") == EVIDENCE.isoformat():
            uw.calls.append((url, dict(params or {})))
            return FakeResponse(500, {"message": "provider error"})
        return real(url, headers, params, timeout)

    uw.get = failing
    cat, client = _cat(tmp_path), uw.adapter(["ABCD"]).client
    r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=3, now_utc=AS_OF)
    assert r["prior_budget"] == 0 and not prior_dp_calls(uw)
    assert r["sessions_touched"][0]["status"] != "ok"                     # the job sees `failed` and pauses
    assert len(ev_dp_calls(uw)) == 1                                       # no (3) after a failure in (1)


def test_history_cutoff_breaks_without_failure_and_never_overspends(tmp_path):
    priors = prior_sessions(EVIDENCE, 6)
    rows = session_prints("ABCD", EVIDENCE, 1e4) + sum((session_prints("ABCD", d, 1e4) for d in priors), [])
    uw = PaginatedUW(darkpool=rows, history_cutoff=priors[2])               # sessions older than priors[2] refused
    cat, client = _cat(tmp_path), uw.adapter(["ABCD"]).client
    r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=40, lookback_sessions=6, now_utc=AS_OF)
    assert r["pages_used"] == len([u for u, _ in uw.calls if "/api/darkpool/" in u]) <= 40
    assert all(t["status"] == "ok" for t in r["sessions_touched"] if t["coverage"] != "unavailable")
    r2 = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=40, lookback_sessions=6,
                              now_utc=AS_OF + timedelta(minutes=1))
    assert r2["prior_budget"] > 0 and r2["prior_pages_used"] == 0           # nothing more: honest no_more_work


def test_r7_4_step3_extension_matches_the_readme(tmp_path, no_gap_policy):
    """R7-4 resolved by correcting the README: step (3) extends the current session with the pages LEFT in the
    step (never before history, never beyond the step's allocation)."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "at most one page per dataset per step" not in readme
    assert "extends the current session only with the pages left in the" in readme
    rows = dense(EVIDENCE, end=time(12, 0)) + session_prints("ABCD", prior_sessions(EVIDENCE, 1)[0], 1e4)
    uw = PaginatedUW(darkpool=rows)
    cat, client = _cat(tmp_path), uw.adapter(["ABCD"]).client
    early = datetime.combine(EVIDENCE, time(10, 1), tzinfo=NY).astimezone(timezone.utc)
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=10, lookback_sessions=1, now_utc=early)
    assert sc._evidence_from_open(cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat()), EVIDENCE)
    before = len(uw.calls)
    res = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=10, lookback_sessions=1,
                               now_utc=AS_OF)
    assert len(ev_dp_calls(uw, before)) <= 10 - res["prior_pages_used"]       # only the leftover
    assert res["pages_used"] <= 10


def test_market_hours_job_with_a_long_evidence_catch_up_still_completes_and_covers_from_the_open(live, monkeypatch):
    rows, alerts = dense(EVIDENCE, end=time(12, 0)), []
    for d in prior_sessions(EVIDENCE, 22):
        rows += session_prints("ABCD", d, 10_000.0)
        alerts += session_alerts("ABCD", d, 50_000.0)
    alerts += session_alerts("ABCD", EVIDENCE, 50_000.0, end=time(11, 55))
    uw = live.use_uw(PaginatedUW(darkpool=rows, flow=alerts, static={"stock-state": STATE}))
    job = run_job(live, TestClient(live.app), max_pages_per_ticker=400, step_pages=10)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "complete", (job["status"], entry["state"])
    for ds in ("darkpool", "options_flow"):
        ev = live.get_catalog().get_session_coverage(ds, "ABCD", EVIDENCE.isoformat())
        assert ev and datetime.fromisoformat(ev["covered_from_utc"]) <= OPEN_EV, ds
    assert len([u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u]) == job["pages_used"] <= 400


# =====================================================================================================
# 2. a6cfe95: dark-pool rows stamped at the exclusive older_than bound are dropped
# =====================================================================================================
class InclusiveAtBoundUW(PaginatedUW):
    """Provider bug model A: the exclusive older_than is applied as <= at whole-second resolution."""

    @staticmethod
    def _page(rows, field, params, upper_inclusive=False):
        return PaginatedUW._page(rows, field, params, upper_inclusive=True)


class RoundingUW(PaginatedUW):
    """Provider bug model B: prints carry sub-second true times (`_true`); bounds compare the TRUE time, the
    response shows executed_at ROUNDED to the nearest whole second. A print at true 15:58:59.7 is returned
    for older_than=15:59:00 and displayed '15:59:00' -- exactly at the bound."""

    @staticmethod
    def _page(rows, field, params, upper_inclusive=False):
        lo, hi = _ts(params["newer_than"]), _ts(params["older_than"])
        sel = sorted((r for r in rows if lo <= r["_true"] < hi), key=lambda r: r["_true"], reverse=True)
        out = []
        for r in sel[: int(params.get("limit") or 100)]:
            shown = (r["_true"] + timedelta(milliseconds=500)).replace(microsecond=0)
            out.append({k: v for k, v in r.items() if k != "_true"} | {"executed_at": iso(shown)})
        return out


def _rounding_rows(d, true_times):
    out = []
    for i, t in enumerate(true_times):
        shown = (t + timedelta(milliseconds=500)).replace(microsecond=0)
        out.append({"ticker": "ABCD", "_true": t, "executed_at": iso(shown), "price": "100.00", "size": 100,
                    "premium": "10000.00", "tracking_id": f"r{i}", "volume": i, "canceled": False})
    return out


def _stored(cat, d):
    o = datetime.combine(d, time(0, 0), tzinfo=NY).astimezone(timezone.utc)
    return {darkpool_print_key("ABCD", r) for r in cat.observations_between("darkpool", "ABCD", o, o + timedelta(days=1))}


def _keys(rows):
    return {darkpool_print_key("ABCD", {k: v for k, v in r.items() if k != "_true"}) for r in rows}


def test_boundary_drop_sound_across_budget_split_settled_walks_model_a(tmp_path, no_gap_policy):
    day = prior_sessions(EVIDENCE, 1)[0]
    rows = []
    t = datetime.combine(day, time(9, 30), tzinfo=NY)
    while len(rows) < 7800:                              # 31 prints share every second: page breaks split seconds
        for k in range(31):
            rows.append({"ticker": "ABCD", "executed_at": iso(t), "price": "100.00", "size": 100 + k,
                         "premium": "1", "tracking_id": f"p{len(rows)}", "volume": len(rows), "canceled": False})
        t += timedelta(seconds=1)
    uw = InclusiveAtBoundUW(darkpool=rows)
    cat, client = _cat(tmp_path), uw.adapter().client
    for k in range(10):                                  # 3 pages per call: island, then resumed backward walks
        u = sc.update_session_coverage(client, cat, "darkpool", "ABCD", day, now_utc=AS_OF + timedelta(seconds=k),
                                       max_pages=3)
        if u.coverage and u.coverage["status"] == "complete":
            break
    assert u.coverage["status"] == "complete"
    assert _stored(cat, day) == _keys(rows)


def test_boundary_drop_sound_for_a_settled_walk_model_b(tmp_path, no_gap_policy):
    day = prior_sessions(EVIDENCE, 1)[0]
    base = datetime.combine(day, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
    trues = [base + timedelta(milliseconds=700 + 333 * i) for i in range(4000)]
    rows = _rounding_rows(day, trues)
    uw = RoundingUW(darkpool=rows)
    cat, client = _cat(tmp_path), uw.adapter().client
    for k in range(10):
        u = sc.update_session_coverage(client, cat, "darkpool", "ABCD", day, now_utc=AS_OF + timedelta(seconds=k),
                                       max_pages=3)
        if u.coverage and u.coverage["status"] == "complete":
            break
    assert u.coverage["status"] == "complete"
    assert _stored(cat, day) == _keys(rows)


def test_r7_2_in_progress_seam_loses_a_print_under_the_rounding_model(tmp_path):
    """The claim 'on a first page the boundary second is past the window' holds only if the provider row's
    true time is >= the bound. Under model B, a print at true end-0.3 s is returned on walk 1 (displayed at
    the bound -> dropped) and excluded from walk 2 (newer_than=end compares the true time): never stored,
    while the evidence session is reported covered from the open through later walks."""
    now1 = datetime.combine(EVIDENCE, time(11, 0), tzinfo=NY).astimezone(timezone.utc)
    end1 = (now1 - sc.REPORTING_LAG).replace(microsecond=0)
    trues = [OPEN_EV + timedelta(minutes=m, milliseconds=100) for m in range(0, 80, 5)]
    trues += [end1 - timedelta(milliseconds=300)]                           # the seam print
    trues += [end1 + timedelta(minutes=m, milliseconds=100) for m in range(1, 30, 5)]
    rows = _rounding_rows(EVIDENCE, trues)
    uw = RoundingUW(darkpool=rows)
    cat, client = _cat(tmp_path), uw.adapter().client
    sc.update_session_coverage(client, cat, "darkpool", "ABCD", EVIDENCE, now_utc=now1, max_pages=4, settle=False)
    now2 = now1 + timedelta(minutes=40)
    u = sc.update_session_coverage(client, cat, "darkpool", "ABCD", EVIDENCE, now_utc=now2, max_pages=4, settle=False)
    assert u.coverage["status"] == "partial" and sc._evidence_from_open(u.coverage, EVIDENCE)
    thr = datetime.fromisoformat(u.coverage["covered_through_utc"])
    expected = _keys([r for r in rows if r["_true"] < thr])
    assert _stored(cat, EVIDENCE) == expected, len(expected - _stored(cat, EVIDENCE))


def test_a_full_page_made_only_of_boundary_rows_never_certifies(tmp_path, no_gap_policy):
    day = prior_sessions(EVIDENCE, 1)[0]
    close = datetime.combine(day, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
    rows = [{"ticker": "ABCD", "executed_at": iso(close + timedelta(seconds=1)), "price": "1", "size": 1,
             "premium": "1", "tracking_id": f"b{i}", "volume": i, "canceled": False} for i in range(500)]
    rows += session_prints("ABCD", day, 1e4)
    uw = InclusiveAtBoundUW(darkpool=rows)
    u = sc.update_session_coverage(uw.adapter().client, _cat(tmp_path), "darkpool", "ABCD", day, now_utc=AS_OF,
                                   max_pages=10)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "cursor_stuck"


def test_flow_rows_at_the_inclusive_bound_are_kept():
    flow = sc.DATASETS["options_flow"]
    lo = datetime(2026, 10, 15, 13, 30, tzinfo=timezone.utc)
    cur = lo + timedelta(minutes=10)
    row = {"id": "x", "ticker": "ABCD", "created_at": iso(cur), "type": "call", "total_premium": 1}
    page = sc._inspect_page(flow, [row], "ABCD", lo.date(), lo, cur)
    assert page["reason"] is None and len(page["rows"]) == 1 and page["violations"]["boundary_dropped"] == 0


# =====================================================================================================
# 3. 27fbf3b: newest-N cached chart markers
# =====================================================================================================
T0 = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def _dp(i, canceled=False):
    return {"ticker": "SPY", "executed_at": iso(T0 + timedelta(seconds=i)), "price": "500.00", "size": 10,
            "premium": "5000.00", "tracking_id": f"t{i}", "volume": i, "canceled": canceled}


def test_canceled_prints_in_the_newest_window_are_never_drawn_live(live_app, monkeypatch):
    m = live_app
    m.use_routes(routes())
    monkeypatch.setattr(m, "CHART_MARKERS_PER_KIND", 50)
    cat = m.get_catalog()
    cat.catalog_darkpool_observations("SPY", [_dp(i) for i in range(120)])
    # cancel the newest print and the print at the window boundary; reinstate one canceled earlier
    cat.catalog_darkpool_observations("SPY", [_dp(119, True), _dp(70, True), _dp(75, True)])
    cat.catalog_darkpool_observations("SPY", [_dp(75, False)])
    body = TestClient(m.app).get("/api/ticker/SPY/ohlc").json()
    times = {x["time"] for x in body["markers"] if x["kind"] == "darkpool_trade"}
    assert iso(T0 + timedelta(seconds=119)) not in times and iso(T0 + timedelta(seconds=70)) not in times
    assert iso(T0 + timedelta(seconds=75)) in times
    assert body["marker_sources"]["darkpool"]["total"] == 120              # version rows not counted


def test_r7_3_truncated_flag_is_not_raised_by_canceled_prints(live_app):
    """Small catalog, nothing truncated, one print canceled: total (120 originals incl. the canceled one)
    vs row_count (current, non-canceled prints) reports `truncated: true`, and the caption then says older
    observations 'are not drawn' and 'are used by the baselines'."""
    m = live_app
    m.use_routes(routes())
    cat = m.get_catalog()
    cat.catalog_darkpool_observations("SPY", [_dp(i) for i in range(10)])
    cat.catalog_darkpool_observations("SPY", [_dp(3, True)])
    ms = TestClient(m.app).get("/api/ticker/SPY/ohlc").json()["marker_sources"]["darkpool"]
    assert ms["row_count"] == 9
    assert ms["truncated"] is False, ms


def test_chart_marker_env_clamp():
    import os
    code = ("import app.main as m; print(m.CHART_MARKERS_PER_KIND)")
    for val, want in (("0", "1"), ("-5", "1"), ("999999", "5000"), ("250", "250")):
        env = {**os.environ, "TIP_MODE": "demo", "TIP_CHART_MARKERS_PER_KIND": val}
        env.pop("UW_API_KEY", None)
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO / "backend"), env=env, capture_output=True,
                             text=True, timeout=120)
        assert out.stdout.strip().splitlines()[-1] == want, (val, out.stdout[-200:], out.stderr[-400:])


# =====================================================================================================
# 4. c3c8c68: README / in-app copy
# =====================================================================================================
def test_doclint_reports_zero():
    out = subprocess.run([sys.executable, "tools/recovery/doclint.py"], cwd=str(REPO), capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0 and "0 finding(s)" in (out.stdout + out.stderr), (out.stdout + out.stderr)[-500:]


def test_r7_5_the_readme_confirmation_claim_matches_the_routes(live_app):
    """R7-5 resolved by correcting the README: Scan and Load Chart Data make a few bounded requests on click
    (no confirmation, as before); the README no longer claims every spending action asks first."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "Every action that spends Unusual Whales or AI requests asks for confirmation first" not in readme
    assert "Scan, Load Chart Data and the Settings connection" in readme
    m = live_app
    m.use_routes({**routes(), "screener": ok([]), "ohlc": ok([])})
    client = TestClient(m.app)
    session = m.get_adapter().fake_session
    spent = {}
    for path, body in (("/api/radar/discover", {}), ("/api/ticker/SPY/ohlc/refresh", None)):
        before = len(session.calls)
        r = client.post(path, json=body) if body is not None else client.post(path)
        spent[path] = (r.status_code, len(session.calls) - before)
    assert all(code == 200 and 1 <= n <= 3 for code, n in spent.values()), spent      # bounded, as documented
