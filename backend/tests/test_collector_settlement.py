"""Collector settlement, provenance and reconciliation (Release 3, migration 11), verified against an
AUTHORITATIVE synthetic-provider ledger (tests/synthetic_ledger.LedgerUW): after every certification
the catalog must hold exactly the ledger's truth for the session as of that time -- not just a count.

Covers: late reports (before and after settlement), legacy 'complete' sessions from earlier collectors,
provider outages (backfilled later, or permanent), provider-empty sessions, later cancellations, auth
failures, the scan-budget island, fractional live edges, rejected-content retries, special closures.
All market rows are CONSTRUCTED."""
import random
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.core import coverage_policy as policy
from app.core import session_baseline, trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests.synthetic_ledger import LedgerUW, stored_keys
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, iso, prior_sessions, session_alerts, session_prints

T = "ABCD"
EVIDENCE = date(2026, 10, 16)
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
SETTLED = CLOSE + policy.SETTLE_LAG + timedelta(minutes=30)
NEXT_DAY = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "ledger.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def walk(uw, cat, now, d=DAY, ds="darkpool", pages=200, **kw):
    uw.now = now
    return sc.update_session_coverage(uw.adapter().client, cat, ds, T, d, now_utc=now, max_pages=pages, **kw)


def exact(uw, cat, as_of, d=DAY, ds="darkpool"):
    info = tc.session_info(d)
    o, c = info.session_open_et.astimezone(timezone.utc), info.session_close_et.astimezone(timezone.utc)
    return stored_keys(cat, ds, T, o, c) == uw.truth(ds, T, o, c, as_of)


def late(row, visible_from):
    return {**row, "_visible_from": visible_from}


def reconciliations(cat, d=DAY):
    return [r for r in cat.list_coverage_reconciliations(ticker=T) if r["session_date"] == d.isoformat()]


# --- late reports ------------------------------------------------------------------------------
def test_print_reported_late_inside_an_already_walked_span_is_caught_by_the_settled_walk(cat):
    rows = session_prints(T, DAY, 1000.0)
    late_row = late({**rows[0], "executed_at": iso(OPEN + timedelta(minutes=30, seconds=7)),
                     "tracking_id": "LATE-1"}, visible_from=CLOSE - timedelta(minutes=10))
    uw = LedgerUW(darkpool=rows + [late_row])
    u = walk(uw, cat, OPEN + timedelta(hours=2))                       # in session: partial, LATE-1 not visible
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "session_in_progress"
    u = walk(uw, cat, CLOSE + timedelta(minutes=5))                    # after close, before settlement
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "awaiting_settlement"
    assert not exact(uw, cat, CLOSE + timedelta(minutes=5))            # the old collector would certify this
    u = walk(uw, cat, SETTLED)                                         # settled: fresh full walk
    assert u.coverage["status"] == "complete" and u.coverage["certified_by"] == policy.CERTIFIER
    assert exact(uw, cat, SETTLED)
    rec = reconciliations(cat)
    assert [(r["trigger"], r["new_rows"], r["outcome"]) for r in rec] == [("unsettled", 1, "certified")]


def test_print_reported_after_settlement_is_found_by_an_operator_reconciliation(cat):
    rows = session_prints(T, DAY, 1000.0)
    late_row = late({**rows[0], "executed_at": iso(OPEN + timedelta(hours=1)), "tracking_id": "VERY-LATE"},
                    visible_from=NEXT_DAY - timedelta(hours=1))
    uw = LedgerUW(darkpool=rows + [late_row])
    u = walk(uw, cat, SETTLED)
    assert policy.is_certified(u.coverage) and exact(uw, cat, SETTLED)
    assert not exact(uw, cat, NEXT_DAY)                                 # documented residual: reported after settlement
    # an ordinary walk of a certified session makes no request
    calls = len(uw.calls)
    assert walk(uw, cat, NEXT_DAY).detail == "certified" and len(uw.calls) == calls
    # an under-budget reconciliation changes nothing and is logged as such
    u = walk(uw, cat, NEXT_DAY, pages=0 + 1, reverify="operator", preserve_on_incomplete=True)
    assert policy.is_certified(cat.get_session_coverage("darkpool", T, DAY.isoformat()))
    u = walk(uw, cat, NEXT_DAY, reverify="operator", preserve_on_incomplete=True)
    assert policy.is_certified(u.coverage) and exact(uw, cat, NEXT_DAY)
    outcomes = [(r["trigger"], r["outcome"], r["new_rows"]) for r in reconciliations(cat)]
    assert ("operator", "certified", 1) in outcomes


def test_reconciliation_is_refused_before_settlement(cat):
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0))
    u = walk(uw, cat, CLOSE + timedelta(minutes=30), reverify="operator", preserve_on_incomplete=True)
    assert "not settled" in u.detail and uw.calls == []


# --- legacy coverage (earlier collectors) ------------------------------------------------------------
def legacy_history(cat, uw, sessions, keep_fraction=0.9):
    """'complete' rows as an EARLIER release wrote them: no provenance, and missing some prints."""
    for d in sessions:
        info = tc.session_info(d)
        o, c = info.session_open_et.astimezone(timezone.utc), info.session_close_et.astimezone(timezone.utc)
        rows = uw.darkpool[T]
        mine = [r for r in rows if r["executed_at"][:10] == d.isoformat()]
        cat.catalog_darkpool_observations(T, mine[: int(len(mine) * keep_fraction)])
        cat.upsert_session_coverage("darkpool", T, d.isoformat(), "complete", o.isoformat(), c.isoformat(), 1, 1)


def test_legacy_complete_sessions_are_never_baseline_and_are_rewalked_by_an_approved_backfill(cat):
    sessions = prior_sessions(EVIDENCE, 22)
    uw = LedgerUW(darkpool=[r for d in sessions for r in session_prints(T, d, 1000.0)])
    legacy_history(cat, uw, sessions)
    base = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=NEXT_DAY)
    assert base.status == "insufficient" and base.eligible_sessions == 0
    assert base.coverage_summary == {"complete:legacy_uncertified": 22}
    res = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=400,
                               lookback_sessions=22, now_utc=NEXT_DAY)
    assert res["complete_prior_sessions"] == 22
    for d in sessions:
        assert policy.is_certified(cat.get_session_coverage("darkpool", T, d.isoformat()))
        assert exact(uw, cat, NEXT_DAY, d)
    recs = cat.list_coverage_reconciliations(ticker=T)
    assert len(recs) == 22 and {r["trigger"] for r in recs} == {"legacy"}
    assert all(r["new_rows"] > 0 for r in recs)                        # the missing prints were recovered
    base = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=NEXT_DAY)
    assert base.eligible_sessions == 22


# --- provider outages and empty sessions ---------------------------------------------------------------
def outage_rows(fixed_at=None, hidden=False):
    rows = session_prints(T, DAY, 1000.0, every_minutes=1)            # ~391 prints: ~1 per minute
    lo, hi = OPEN + timedelta(hours=2), OPEN + timedelta(hours=3)
    out = []
    for r in rows:
        ts = datetime.fromisoformat(r["executed_at"].replace("Z", "+00:00"))
        if lo <= ts < hi:
            r = {**r, "_hidden": True} if hidden else late(r, fixed_at)
        out.append(r)
    return out


def test_provider_outage_is_withheld_once_then_certified_when_the_provider_backfills(cat):
    uw = LedgerUW(darkpool=outage_rows(fixed_at=NEXT_DAY - timedelta(hours=2)))
    u = walk(uw, cat, SETTLED)
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "suspected_provider_gap"
    assert u.coverage["gap_flag"] == "suspected"
    u = walk(uw, cat, NEXT_DAY)
    assert policy.is_certified(u.coverage) and u.coverage["gap_flag"] is None
    assert exact(uw, cat, NEXT_DAY)
    rec = reconciliations(cat)
    assert rec[0]["trigger"] == "suspected_gap" and rec[0]["new_rows"] == 60 and rec[0]["outcome"] == "certified"


def test_a_permanent_gap_is_certified_only_after_a_second_walk_reproduces_it(cat):
    uw = LedgerUW(darkpool=outage_rows(hidden=True))
    assert walk(uw, cat, SETTLED).coverage["last_error"] == "suspected_provider_gap"
    u = walk(uw, cat, NEXT_DAY)
    assert policy.is_certified(u.coverage) and u.coverage["gap_flag"] == "confirmed"
    assert u.coverage["max_gap_s"] >= 3600 and exact(uw, cat, NEXT_DAY)


def history_with_empty_days(cat, uw_rows_per_day, empty_idx, n=22):
    days = prior_sessions(EVIDENCE, n)
    rows = [r for i, d in enumerate(days) if i not in empty_idx
            for r in session_prints(T, d, 1000.0, every_minutes=uw_rows_per_day)]
    uw = LedgerUW(darkpool=rows)
    for d in days:
        assert policy.is_certified(walk(uw, cat, NEXT_DAY + timedelta(days=1), d).coverage)
    walk(uw, cat, datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc), EVIDENCE)
    return session_baseline.compute(cat, "darkpool", T, EVIDENCE,
                                    as_of_utc=datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc))


def test_a_provider_empty_session_of_an_active_ticker_is_excluded_not_averaged_as_zero(cat):
    base = history_with_empty_days(cat, 5, empty_idx={3, 0})           # 79 prints/day; two empty days
    assert base.excluded_implausible_empty == 2
    assert all(h["value"] > 0 for h in base.history_sessions) and base.eligible_sessions == 20


def test_pre_listing_and_thin_ticker_zero_days_are_kept_as_genuine(cat):
    base = history_with_empty_days(cat, 30, empty_idx={3})             # 14 prints/day: thin ticker
    assert base.excluded_implausible_empty == 0
    assert any(h["value"] == 0 for h in base.history_sessions)


def test_a_quiet_ticker_with_no_activity_anywhere_is_certified_without_delay(cat):
    uw = LedgerUW(darkpool=[])
    assert policy.is_certified(walk(uw, cat, SETTLED).coverage)


# --- cancellations -------------------------------------------------------------------------------------
def test_a_cancellation_reported_after_certification_is_excluded_after_reconciliation(cat):
    rows = session_prints(T, DAY, 1000.0)
    rows[10] = {**rows[10], "_canceled_from": NEXT_DAY - timedelta(hours=1)}
    uw = LedgerUW(darkpool=rows)
    walk(uw, cat, SETTLED)
    assert exact(uw, cat, SETTLED)
    sig = cat.observation_window_signature("darkpool", T, OPEN, CLOSE)
    walk(uw, cat, NEXT_DAY, reverify="operator", preserve_on_incomplete=True)
    assert exact(uw, cat, NEXT_DAY)                                      # the canceled print no longer counts
    assert cat.observation_window_signature("darkpool", T, OPEN, CLOSE) != sig
    assert len(cat.observations_between("darkpool", T, OPEN, CLOSE)) == len(rows) - 1


# --- auth failures never become coverage (C-F1) --------------------------------------------------------
@pytest.mark.parametrize("resp", [FakeResponse(401, {"message": "Invalid API key"}),
                                  FakeResponse(403, {"message": "Access denied"})])
def test_an_auth_failure_writes_no_coverage_and_the_session_is_collected_after_recovery(cat, resp):
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0), fail={"/api/darkpool/": resp})
    u = walk(uw, cat, SETTLED)
    assert not u.ok and cat.get_session_coverage("darkpool", T, DAY.isoformat()) is None
    uw.fail = {}
    assert policy.is_certified(walk(uw, cat, NEXT_DAY).coverage)


def test_a_not_entitled_row_written_by_an_earlier_release_is_reprobed(cat):
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "unavailable", None, None, 1, 0, "not_entitled")
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0))
    assert policy.is_certified(walk(uw, cat, NEXT_DAY).coverage)


def test_the_history_cutoff_is_still_respected(cat):
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0), history_cutoff=EVIDENCE)
    u = walk(uw, cat, NEXT_DAY)
    assert u.coverage["status"] == "unavailable" and u.coverage["last_error"] == "provider_history_cutoff"
    calls = len(uw.calls)
    walk(uw, cat, NEXT_DAY)
    assert len(uw.calls) == calls                                         # not re-requested


# --- scan budget keeps the proven from-open interval (C-F8) -----------------------------------------
def test_a_small_scan_budget_never_discards_the_from_open_interval(cat):
    rows = session_prints(T, EVIDENCE, 100.0, every_minutes=0.01, end=time(11, 30))   # 100 prints per minute
    uw = LedgerUW(darkpool=rows)
    ev_open = datetime.combine(EVIDENCE, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
    t1 = ev_open + timedelta(hours=1)
    u = walk(uw, cat, t1, EVIDENCE, pages=60)
    assert u.coverage["covered_from_utc"].startswith(ev_open.strftime("%Y-%m-%dT%H:%M"))
    through1 = u.coverage["covered_through_utc"]
    t2 = t1 + timedelta(minutes=30)
    u = walk(uw, cat, t2, EVIDENCE, pages=4)                              # a scan: 4 pages, far behind
    assert u.coverage["covered_from_utc"].startswith(ev_open.strftime("%Y-%m-%dT%H:%M"))
    assert u.coverage["covered_through_utc"] == through1                  # proven interval kept
    assert u.coverage["island_from_utc"] and u.coverage["island_through_utc"]
    base = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=t2)
    assert "not verified from the open" not in base.reason
    for _ in range(10):                                                    # later walks fill the gap
        u = walk(uw, cat, t2, EVIDENCE, pages=4)
    assert u.coverage["island_from_utc"] is None
    assert u.coverage["covered_through_utc"] > through1
    info = tc.session_info(EVIDENCE)
    lo = info.session_open_et.astimezone(timezone.utc)
    hi = datetime.fromisoformat(u.coverage["covered_through_utc"]) - timedelta(microseconds=1)  # dark pool: exclusive
    stored = stored_keys(cat, "darkpool", T, lo, hi)
    assert uw.truth("darkpool", T, lo, hi, t2) <= stored


# --- edges --------------------------------------------------------------------------------------------
def test_the_dark_pool_live_edge_is_a_whole_second(cat):
    uw = LedgerUW(darkpool=session_prints(T, EVIDENCE, 1.0))
    now = datetime(2026, 10, 16, 15, 0, 0, 500000, tzinfo=timezone.utc)
    walk(uw, cat, now, EVIDENCE, pages=5)
    assert all("." not in p["older_than"] for u, p in uw.calls if "/darkpool/" in u)


def test_rejected_content_sessions_become_rewalkable_after_a_day(cat):
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "unknown", None, None, 1, 0, "bounds_not_honored")
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0))
    res = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=50,
                               lookback_sessions=1, now_utc=NEXT_DAY)
    touched = [s["session_date"] for s in res["sessions_touched"]]
    assert DAY.isoformat() not in touched                                 # rejected within the last day: skipped
    res = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=50,
                               lookback_sessions=1, now_utc=datetime.now(timezone.utc) + timedelta(days=2))
    assert DAY.isoformat() in [s["session_date"] for s in res["sessions_touched"]]


def test_unscheduled_market_closures_are_not_trading_days(monkeypatch):
    assert not tc.is_trading_day(date(2025, 1, 9)) and not tc.is_trading_day(date(2018, 12, 5))
    monkeypatch.setenv("TIP_EXTRA_MARKET_CLOSURES", "2026-10-14")
    assert not tc.is_trading_day(date(2026, 10, 14))


# --- property: certification always equals the ledger truth --------------------------------------
@pytest.mark.parametrize("seed", range(24))
def test_certified_sessions_always_hold_exactly_the_ledger_truth(cat, seed):
    rnd = random.Random(seed)
    ds = "darkpool" if seed % 2 == 0 else "options_flow"
    every = rnd.choice([0.2, 0.5, 1, 3])
    rows = (session_prints(T, DAY, 1000.0, every_minutes=every) if ds == "darkpool"
            else session_alerts(T, DAY, 5000.0, every_minutes=every))
    field = "executed_at" if ds == "darkpool" else "created_at"
    for r in rows:
        if rnd.random() < 0.05:                                            # 5% reported late (any time to next day)
            r["_visible_from"] = OPEN + timedelta(minutes=rnd.randint(0, 60 * 30))
    uw = LedgerUW(darkpool=rows if ds == "darkpool" else [], flow=rows if ds != "darkpool" else [])
    times = sorted(OPEN + timedelta(minutes=rnd.randint(30, 60 * 34)) for _ in range(rnd.randint(2, 6)))
    certified_at = None
    for now in times + [NEXT_DAY + timedelta(days=1)] * 30:
        u = walk(uw, cat, now, ds=ds, pages=rnd.choice([2, 4, 10, 60]))
        if policy.is_certified(u.coverage):
            certified_at = now
            break
    assert certified_at is not None and certified_at >= CLOSE + policy.SETTLE_LAG
    # The guarantee: every row visible when the certifying (post-settlement) walk BEGAN is stored, and
    # nothing outside the ledger is. A row reported after that start, inside a span the walk had already
    # passed, needs an operator reconciliation (documented residual; see the reconciliation tests).
    proof = datetime.fromisoformat(u.coverage["proof_started_utc"])
    assert proof >= CLOSE + policy.SETTLE_LAG
    stored = stored_keys(cat, ds, T, OPEN, CLOSE)
    assert uw.truth(ds, T, OPEN, CLOSE, proof) <= stored <= uw.truth(ds, T, OPEN, CLOSE, certified_at), (seed, every)
    assert {r[field] for r in rows}                                         # sanity
