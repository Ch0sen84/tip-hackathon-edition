"""Round-3 backend review (R3B-1..R3B-10), converted from the reviewer's reproductions into regression
tests asserting the corrected behaviour (R3B-8: the documented guarantee).

AUDIT3 (independent adversarial review of 7206583 / 5846158, backend round-2 fixes).

Every test asserts the CORRECT behaviour; a FAILING test reproduces a finding (R3B-n).
All market rows are CONSTRUCTED; no network (tests fakes only)."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.core import coverage_policy as policy
from app.core import trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests.synthetic_ledger import LedgerUW
from tests.uw_paginated import NY, _ts, iso, prior_sessions, session_prints

T = "ABCD"
EVIDENCE = date(2026, 10, 16)
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
SETTLED = CLOSE + policy.SETTLE_LAG + timedelta(minutes=30)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "audit3.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def walk(uw, cat, now, d=DAY, ds="darkpool", pages=200, **kw):
    uw.now = now
    return sc.update_session_coverage(uw.adapter().client, cat, ds, T, d, now_utc=now, max_pages=pages, **kw)


def _print(t, i, tag="X"):
    return {"ticker": T, "executed_at": iso(t), "price": "100.00", "size": 100 + i, "premium": "1000.00",
            "tracking_id": f"{tag}-{i}", "canceled": False}


# ------------------------------------------------------------------------------------------------
# R3B-1  regression from the C2-1 fix: an outage covering all but < 1 h of the session is never
#        tested (GAP_MIN_OBSERVED_S guard breaks out of the k-loop at k=1) -> certified complete
# ------------------------------------------------------------------------------------------------
def test_r3b_1_outage_of_most_of_the_session_is_not_certified(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=0.05)             # a print every 3 s (~7.8k)
    cut = OPEN + timedelta(minutes=45)
    rows = [{**r, "_hidden": True} if _ts(r["executed_at"]) >= cut else r for r in rows]   # outage 10:15-16:00
    uw = LedgerUW(darkpool=rows)
    u = walk(uw, cat, SETTLED)
    assert not policy.is_certified(u.coverage), (
        f"certified with a 5h45m hole after ~900 prints in 45 min; max_gap_s={u.coverage.get('max_gap_s')}")


def test_r3b_1b_gap_assessment_direct(cat):
    times = [OPEN + timedelta(seconds=3 * i) for i in range(900)]          # 45 min of 1 print / 3 s
    assert policy.gap_assessment(times, OPEN, CLOSE)[1], "5h45m without a row after 900 rows in 45 min"


# ------------------------------------------------------------------------------------------------
# R3B-2  C2-1 false positive: a GENUINE open/close-clustered session (busy open, a print every 30 min
#        midday, closing-cross burst) is 'suspected_provider_gap' (needs 12 h + another full walk)
# ------------------------------------------------------------------------------------------------
def test_r3b_2_clustered_genuine_session_is_certified_on_its_settled_walk(cat):
    rows = [_print(OPEN + timedelta(seconds=30 * i), i, "O") for i in range(60)]             # 9:30-10:00
    rows += [_print(OPEN + timedelta(minutes=30 * k), k, "M") for k in range(2, 13)]           # every 30 min
    rows += [_print(CLOSE - timedelta(seconds=120 - i // 2), i, "C") for i in range(200)]      # closing burst
    uw = LedgerUW(darkpool=rows)
    u = walk(uw, cat, SETTLED)
    assert policy.is_certified(u.coverage), (
        f"genuine clustered session not certified: {u.coverage['status']}:{u.coverage['last_error']}")


# ------------------------------------------------------------------------------------------------
# R3B-3  a suspected session is fully re-walked by EVERY backfill / job step inside the 12 h window
#        (a walk inside the window can never confirm it): budget burnt on the same session
# ------------------------------------------------------------------------------------------------
def test_r3b_3_suspected_session_is_not_rewalked_by_back_to_back_backfills(cat):
    days = prior_sessions(EVIDENCE, 2)
    rows = []
    for d in days:
        o = datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
        lo, hi = o + timedelta(hours=2), o + timedelta(hours=3)
        rows += [r for r in session_prints(T, d, 1000.0, every_minutes=0.05, tag=d.isoformat())
                 if not (lo <= _ts(r["executed_at"]) < hi)]                  # genuine 1 h hole, liquid ticker
    uw = LedgerUW(darkpool=rows)
    now = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
    uw.now = now
    sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=200,
                         lookback_sessions=2, now_utc=now)
    for d in days:
        assert cat.get_session_coverage("darkpool", T, d.isoformat())["last_error"] == "suspected_provider_gap"
    n = len(uw.calls)
    later = now + timedelta(minutes=1)
    uw.now = later
    sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=200,
                         lookback_sessions=2, now_utc=later)
    rewalk = [p for _u, p in uw.calls[n:] if p.get("date") in {d.isoformat() for d in days}]
    assert len(rewalk) == 0, f"{len(rewalk)} pages re-walking suspected sessions 1 minute after suspicion"


# ------------------------------------------------------------------------------------------------
# R3B-4  C2-3/P2-5: a print FIRST stored canceled and later reinstated is served by _iter_window (via
#        the #reinstated marker's data) but dropped by get_cached_darkpool_observations and
#        list_darkpool_observations_by_date (they drop marker rows AND rows whose own data is canceled)
# ------------------------------------------------------------------------------------------------
def test_r3b_4_reinstated_print_first_seen_canceled_is_served_by_every_reader(cat):
    p = _print(OPEN + timedelta(hours=1), 1, "RE")
    other = _print(OPEN + timedelta(hours=2), 2, "OK")
    cat.catalog_darkpool_observations(T, [{**p, "canceled": True}, other], retrieved_at="2026-10-15T15:00:00+00:00")
    cat.catalog_darkpool_observations(T, [p], retrieved_at="2026-10-16T15:00:00+00:00")     # reversal
    window = cat.observations_between("darkpool", T, OPEN, CLOSE)
    assert len(window) == 2                                              # the baseline reader serves it
    cached = cat.get_cached_darkpool_observations(T)
    by_date = sum(len(v) for v in cat.list_darkpool_observations_by_date(T).values())
    assert len(cached) == 2 and by_date == 2, f"window={len(window)} cached={len(cached)} by_date={by_date}"


# ------------------------------------------------------------------------------------------------
# R3B-5 (INFO)  S2-2/S2-3: the backfill route's confirm is a lax query bool ("yes", "on", "1" accepted)
# R3B-6 (INFO)  S2-2: the resume 428 overstates the remaining budget (counts tickers the job will never
#               step again: no_more_work / history_cutoff -- the state names it excludes do not exist)
# ------------------------------------------------------------------------------------------------
from tests.test_round2_security_regressions import _authed, live_m  # noqa: E402,F401  (fixture reuse)


@pytest.mark.parametrize("value", ["yes", "on", "1"])
def test_r3b_5_backfill_confirm_is_a_strict_boolean(live_m, monkeypatch, value):  # noqa: F811
    calls = []
    monkeypatch.setattr(live_m, "_run_baseline_backfill", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(live_m, "_baseline_report", lambda *a, **k: {})
    r = _authed(live_m).post(f"/api/ticker/ABCD/baseline/backfill?max_pages=200&confirm={value}")
    assert r.status_code in (422, 428) and not calls, (value, r.status_code)


def test_r3b_6_resume_states_the_budget_the_job_can_still_spend(live_m):  # noqa: F811
    store = live_m.get_baseline_job_store()
    job = store.create(["AAA", "BBB", "CCC"], 100, 20, requested_by="user", target_sessions=20)
    prog = job["progress"]
    prog["AAA"].update(state="no_more_work", pages_used=10)
    prog["BBB"].update(state="history_cutoff", pages_used=30)
    store.update(job["id"], progress=prog)
    store.transition(job["id"], ("running",), "paused")
    r = _authed(live_m).post(f"/api/baseline/jobs/{job['id']}/resume")
    assert r.status_code == 428
    assert r.json()["detail"]["remaining_pages"] == 100, r.json()["detail"]


# ------------------------------------------------------------------------------------------------
# R3B-7  C2-2 fails OPEN: a row flagged gap_flag='suspected' with no logged suspicion (rows written by
#        the schema-11 code before 5846158 never logged a first-walk suspicion; or the suspicion fell out
#        of the 1000-record window) is CONFIRMED by the very next walk, with no waiting period
# ------------------------------------------------------------------------------------------------
def test_r3b_7_suspected_row_without_a_logged_suspicion_is_not_confirmed_immediately(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=1)
    lo = OPEN + timedelta(hours=2)
    rows = [{**r, "_visible_from": SETTLED + timedelta(hours=6)} if lo <= _ts(r["executed_at"]) < lo + timedelta(hours=1)
            else r for r in rows]                                       # transient outage, backfilled 6 h later
    uw = LedgerUW(darkpool=rows)
    uw.now = SETTLED
    cat.catalog_darkpool_observations(T, [{k: v for k, v in r.items() if not k.startswith("_")}
                                          for r in rows if not r.get("_visible_from")])
    # the row a pre-5846158 schema-11 walk left: suspected, proof after settlement, nothing logged
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "partial", OPEN.isoformat(), CLOSE.isoformat(),
                                3, 330, "suspected_provider_gap",
                                provenance={"proof_started_utc": SETTLED.isoformat(), "gap_flag": "suspected"})
    u = walk(uw, cat, SETTLED + timedelta(minutes=5))
    assert not policy.is_certified(u.coverage), "suspected gap confirmed 5 minutes later (no suspicion on record)"


# ------------------------------------------------------------------------------------------------
# R3B-8 (LOW)  C2-6 only partly fixed: a settled but UNFINISHED legacy re-walk (budget) still rewrites the
#              legacy row in place (coverage_policy docstring: "never modified in place")
# ------------------------------------------------------------------------------------------------
def test_r3b_8_unfinished_legacy_rewalk_leaves_the_legacy_row_untouched(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=0.05)
    uw = LedgerUW(darkpool=rows)
    cat.catalog_darkpool_observations(T, rows[:-5])
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "complete", OPEN.isoformat(), CLOSE.isoformat(), 1, 1)
    before = len(cat.observations_between("darkpool", T, OPEN, CLOSE))
    walk(uw, cat, SETTLED, pages=2)                                     # 16 pages needed, 2 allowed
    cov = cat.get_session_coverage("darkpool", T, DAY.isoformat())
    # Resolved as a documented trade-off (coverage_policy, R3B-8): a settled legacy re-walk records its
    # progress in the row (a busy session needs many job steps). The guarantees that matter hold: nothing is
    # deleted, nothing is certified or counted early, and the legacy origin stays on record.
    assert len(cat.observations_between("darkpool", T, OPEN, CLOSE)) >= before
    assert not policy.is_certified(cov) and cov["reverify_trigger"] == "legacy"
    u = walk(uw, cat, SETTLED + timedelta(minutes=5))                    # the re-walk continues and certifies
    assert policy.is_certified(u.coverage)
    rec = [r for r in cat.list_coverage_reconciliations(ticker=T) if r["outcome"] == "certified"]
    assert rec and rec[0]["trigger"] == "legacy"


# ------------------------------------------------------------------------------------------------
# R3B-9  P2-4 not fixed in the JOB path: _baseline_job_step reports history_cutoff for ANY 'unavailable'
#        row, so a legacy `unavailable / not_entitled` row (an old auth failure) still marks the dataset
#        target as met and ends the ticker as "history_cutoff" before the backfill ever reaches it
# ------------------------------------------------------------------------------------------------
def test_r3b_9_legacy_not_entitled_row_does_not_end_a_job_as_history_cutoff(live_m, monkeypatch):  # noqa: F811
    from app.baseline_jobs import BaselineJobRunner
    cat_ = live_m.get_catalog()
    old = prior_sessions(EVIDENCE, 15)[-1]
    for ds in ("darkpool", "options_flow"):
        cat_.upsert_session_coverage(ds, "ABCD", old.isoformat(), "unavailable", None, None, 1, 0, "not_entitled")
    fake = {"pages_used": 10, "sessions_touched": [], "complete_prior_sessions": 3}
    monkeypatch.setattr(live_m, "_run_baseline_backfill", lambda *a, **k: {"darkpool": dict(fake),
                                                                           "options_flow": dict(fake)})
    result = live_m._baseline_job_step("ABCD", 20)
    entry = {"datasets": {ds: {"complete_prior_sessions": 3, "history_cutoff": r["history_cutoff"]}
                          for ds, r in result.items()}}
    assert not any(r["history_cutoff"] for r in result.values()), result
    assert BaselineJobRunner._ticker_done(entry, 20) is None


@pytest.mark.parametrize("step", [7, 10, 14])
def test_r3b_3b_job_steps_never_get_past_a_suspected_session(cat, step):
    """Job-like loop (backfill steps of `step` pages, back to back): the newest prior session has a genuine
    gap; once suspected, every step restarts its full re-walk (trigger suspected_gap), so the two older
    clean sessions are never reached (step 7 and 14: the re-walk takes exactly 14 pages, nothing is left over)."""
    days = prior_sessions(EVIDENCE, 3)
    rows = []
    for d in days:
        o = datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
        lo, hi = o + timedelta(hours=2), o + timedelta(hours=3)
        rows += [r for r in session_prints(T, d, 1000.0, every_minutes=0.05, tag=d.isoformat())
                 if d != days[0] or not (lo <= _ts(r["executed_at"]) < hi)]
    uw = LedgerUW(darkpool=rows)
    now = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc)
    spent = 0
    for i in range(20):
        t = now + timedelta(minutes=i)
        uw.now = t
        spent += sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=step,
                                      lookback_sessions=3, now_utc=t)["pages_used"]
    certified = [d for d in days if policy.is_certified(cat.get_session_coverage("darkpool", T, d.isoformat()))]
    assert set(days[1:]) <= set(certified), (
        f"{spent} pages spent; certified {certified}; newest: "
        f"{cat.get_session_coverage('darkpool', T, days[0].isoformat())['last_error']}")
