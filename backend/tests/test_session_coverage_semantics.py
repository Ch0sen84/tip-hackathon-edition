"""Evidence-based session coverage on the LIVE-VERIFIED UW pagination contract
(Phase 2, docs/UW_PAGINATION_VERIFICATION.md). The fake (tests/uw_paginated.py)
reproduces the verified semantics: dark pool date-scoped, newer_than inclusive,
older_than exclusive; flow alerts both bounds inclusive; short page = window
exhausted; 403 historic_data_access_missing beyond the history cutoff.
All market rows here are CONSTRUCTED."""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.core import session_baseline, trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests.uw_paginated import NY, PaginatedUW, iso, prior_sessions, session_alerts, session_prints

EVIDENCE = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "cov.db"))


def cover(uw, cat, dataset, d, now=AS_OF, pages=20, ticker="ABCD"):
    return sc.update_session_coverage(uw.adapter().client, cat, dataset, ticker, d, now_utc=now, max_pages=pages)


def test_every_collector_request_is_a_bounded_window(cat):
    uw = PaginatedUW(darkpool=session_prints("ABCD", EVIDENCE, 1.0), flow=session_alerts("ABCD", EVIDENCE, 1.0))
    cover(uw, cat, "darkpool", EVIDENCE)
    cover(uw, cat, "options_flow", EVIDENCE)
    collector_calls = [p for u, p in uw.calls if "/darkpool/" in u or "flow-alerts" in u]
    assert collector_calls and all(p.get("newer_than") and p.get("older_than") for p in collector_calls)
    assert all(p.get("date") == EVIDENCE.isoformat() for u, p in uw.calls if "/darkpool/" in u)


def test_holiday_makes_no_request_and_records_nothing(cat):
    uw = PaginatedUW()
    thanksgiving = date(2026, 11, 26)
    u = cover(uw, cat, "darkpool", thanksgiving, now=datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc))
    assert u.detail == "not a trading session" and uw.calls == []
    assert cat.get_session_coverage("darkpool", "ABCD", "2026-11-26") is None


def test_early_close_session_is_complete_at_its_own_1pm_close(cat):
    day_after = date(2026, 11, 27)
    uw = PaginatedUW(darkpool=session_prints("ABCD", day_after, 1.0, end=time(13, 0)))
    u = cover(uw, cat, "darkpool", day_after, now=datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc))
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "awaiting_settlement"
    assert u.coverage["covered_through_utc"].startswith("2026-11-27T18:00")   # 13:00 ET
    u = cover(uw, cat, "darkpool", day_after, now=datetime(2026, 11, 27, 22, 0, tzinfo=timezone.utc))
    assert u.coverage["status"] == "complete"                  # settled: certified at its own 1pm close
    assert u.coverage["covered_through_utc"].startswith("2026-11-27T18:00")


def test_quiet_session_with_no_print_at_the_open_is_complete(cat):
    d = prior_sessions(EVIDENCE, 1)[0]
    uw = PaginatedUW(darkpool=session_prints("ABCD", d, 1.0, every_minutes=1, start=time(11, 7), end=time(11, 9)))
    u = cover(uw, cat, "darkpool", d)
    assert u.coverage["status"] == "complete" and u.coverage["rows_seen"] == 3


def test_session_with_zero_prints_is_complete_but_pre_listing_sessions_are_not_baseline(cat, no_gap_policy):
    """Verified: a bounded, date-scoped window answered with [] is exhausted.
    Sessions before a ticker's first observed activity (e.g. pre-IPO) are
    excluded from the baseline so a new listing cannot look anomalous."""
    sessions = prior_sessions(EVIDENCE, 25)
    listed = sessions[:20]                      # the 20 most recent prior sessions traded
    rows = [r for d in listed for r in session_prints("ABCD", d, 10_000.0)]
    rows += session_prints("ABCD", EVIDENCE, 100_000.0, end=time(12, 0))
    uw = PaginatedUW(darkpool=rows)
    for d in sessions:
        assert cover(uw, cat, "darkpool", d).coverage["status"] == "complete"
    cover(uw, cat, "darkpool", EVIDENCE)
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF)
    assert b.excluded_before_first_activity == 5 and b.eligible_sessions == 20
    assert b.status == "pass"


def test_provider_history_cutoff_is_unavailable_and_stops_the_backfill(cat):
    sessions = prior_sessions(EVIDENCE, 30)
    cutoff = sessions[9]        # only the 10 most recent prior sessions are entitled
    rows = [r for d in sessions for r in session_prints("ABCD", d, 10_000.0)]
    rows += session_prints("ABCD", EVIDENCE, 10_000.0, end=time(12, 0))
    uw = PaginatedUW(darkpool=rows, history_cutoff=cutoff)
    out = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=200,
                               lookback_sessions=30, now_utc=AS_OF)
    statuses = [(t["session_date"], t["coverage"], t["reason"]) for t in out["sessions_touched"]]
    assert statuses[-1][1:] == ("unavailable", "provider_history_cutoff")
    assert out["complete_prior_sessions"] == 10
    assert len(out["sessions_touched"]) == 12   # evidence + 10 complete + the first refused one; no more requests
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF)
    assert b.status == "insufficient" and "unavailable:provider_history_cutoff" in b.coverage_summary


def test_distinct_prints_sharing_a_tracking_id_are_all_counted(cat):
    """Verified live: tracking_id is NOT unique (500-row page, 432 ids).
    Same second, same size and price, different day-volume = two prints."""
    t = datetime.combine(EVIDENCE, time(10, 0), tzinfo=NY)
    rows = [{"ticker": "ABCD", "tracking_id": 42, "executed_at": iso(t), "price": "100.00", "size": 600,
             "premium": "60000.00", "volume": 1_000_000 + i, "canceled": False} for i in range(3)]
    uw = PaginatedUW(darkpool=rows)
    cover(uw, cat, "darkpool", EVIDENCE)
    obs = cat.observations_between("darkpool", "ABCD", t - timedelta(minutes=1), t + timedelta(minutes=1))
    assert len(obs) == 3
    cover(uw, cat, "darkpool", EVIDENCE, now=AS_OF + timedelta(minutes=30))     # re-fetch
    reformatted = [{**r, "price": "100.0", "premium": "60000.0"} for r in rows]  # same prints, other formatting
    cat.catalog_darkpool_observations("ABCD", reformatted)
    assert len(cat.observations_between("darkpool", "ABCD", t - timedelta(minutes=1), t + timedelta(minutes=1))) == 3


def test_dark_pool_exclusive_cursor_with_overlap_never_skips_a_boundary_second(cat):
    """Many prints in one second straddling a page boundary: the +1 s overlap
    re-reads that second (exclusive older_than) so none are skipped."""
    t = datetime.combine(EVIDENCE, time(10, 0), tzinfo=NY)
    burst = [{"ticker": "ABCD", "tracking_id": i, "executed_at": iso(t), "price": "1", "size": 1, "premium": "1",
              "volume": i} for i in range(300)]
    other = session_prints("ABCD", EVIDENCE, 1.0, every_minutes=0.02, start=time(10, 0, 1), end=time(10, 4))
    uw = PaginatedUW(darkpool=burst + other)
    u = cover(uw, cat, "darkpool", EVIDENCE)
    assert u.coverage["covered_from_utc"].startswith("2026-10-16T13:30")
    assert len(cat.observations_between("darkpool", "ABCD", t, t)) == 300


def test_flow_inclusive_cursor_stuck_on_one_timestamp_is_unknown_not_a_loop(cat):
    t = datetime.combine(EVIDENCE, time(10, 0), tzinfo=NY)
    same = [{"id": f"a{i}", "ticker": "ABCD", "created_at": t.isoformat(), "type": "call", "total_premium": 1,
             "total_ask_side_prem": 1, "total_bid_side_prem": 0} for i in range(450)]
    uw = PaginatedUW(flow=same)
    u = cover(uw, cat, "options_flow", EVIDENCE, pages=50)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "cursor_stuck"
    assert u.pages <= 3


def test_page_budget_is_a_hard_bound(cat):
    uw = PaginatedUW(darkpool=session_prints("ABCD", EVIDENCE, 1.0, every_minutes=0.01, end=time(12, 0)))
    u = cover(uw, cat, "darkpool", EVIDENCE, pages=3)
    assert u.pages == 3 and len(uw.calls) == 3
    assert u.coverage["status"] == "partial" and u.coverage["last_error"] == "page_budget_exhausted"


def test_current_session_is_partial_until_after_the_close(cat):
    uw = PaginatedUW(darkpool=session_prints("ABCD", EVIDENCE, 1.0))
    mid = cover(uw, cat, "darkpool", EVIDENCE, now=datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc))
    assert mid.coverage["status"] == "partial" and mid.coverage["last_error"] == "session_in_progress"
    after = cover(uw, cat, "darkpool", EVIDENCE, now=datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc))
    assert after.coverage["status"] == "partial" and after.coverage["last_error"] == "awaiting_settlement"
    settled = cover(uw, cat, "darkpool", EVIDENCE, now=datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc))
    assert settled.coverage["status"] == "complete" and settled.coverage["certified_by"]


def test_partial_sessions_are_never_baseline_eligible(cat):
    sessions = prior_sessions(EVIDENCE, 25)
    rows = [r for d in sessions for r in session_prints("ABCD", d, 10_000.0, every_minutes=0.05)]
    rows += session_prints("ABCD", EVIDENCE, 10_000.0, end=time(12, 0))
    uw = PaginatedUW(darkpool=rows)
    for d in sessions:
        cover(uw, cat, "darkpool", d, pages=2)        # ~16 pages needed per session -> partial
    cover(uw, cat, "darkpool", EVIDENCE)
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF)
    assert b.eligible_sessions == 0 and b.status == "insufficient"
    assert b.coverage_summary.get("partial:page_budget_exhausted") == 25


def test_canceled_prints_are_excluded_from_the_baseline_value(cat):
    rows = session_prints("ABCD", EVIDENCE, 10.0, end=time(10, 0))
    rows.append({"ticker": "ABCD", "tracking_id": "x", "executed_at": iso(datetime.combine(EVIDENCE, time(9, 45), tzinfo=NY)),
                 "price": "1", "size": 1, "premium": "99999999", "volume": 1, "canceled": True})
    uw = PaginatedUW(darkpool=rows)
    cover(uw, cat, "darkpool", EVIDENCE)
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF, min_sessions=0)
    assert b.value == pytest.approx(10.0 * len(rows[:-1]))
