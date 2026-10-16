"""Phase B: bounded backward dark-pool pagination tests. The UW client
is a tiny fake here (no real network, no real adapter) so every boundary
condition -- duplicate timestamps, non-advancing cursor, short pages that
are NOT proof of completeness, genuine session-start arrival -- is
exercised deterministically."""
from __future__ import annotations

import sqlite3
from datetime import date

from app.dark_pool_pagination import COMPLETE, INCOMPLETE, UNKNOWN, paginate_ticker_day
from app.ingestion import IngestionCatalog


class _FakeClient:
    """Returns pre-canned pages in order, one per call, regardless of
    the older_than it's given (the test controls exactly what each call
    returns so boundary conditions are deterministic)."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def darkpool_ticker(self, ticker, date=None, limit=500, older_than=None, **kwargs):
        self.calls.append(older_than)
        if not self.pages:
            return {"data": []}
        return self.pages.pop(0)


def _catalog(tmp_path):
    return IngestionCatalog(str(tmp_path / "pg.db"))


TRADING_DATE = date(2026, 10, 14)  # a real Wednesday
SESSION_START_ISO = "2026-10-14T13:30:00Z"  # 09:30 America/New_York in UTC (EDT, UTC-4)


def test_complete_when_pagination_reaches_real_session_start(tmp_path):
    client = _FakeClient([
        {"data": [{"id": "t3", "executed_at": "2026-10-14T19:00:00Z", "price": 10}]},
        {"data": [{"id": "t2", "executed_at": "2026-10-14T15:00:00Z", "price": 10}]},
        {"data": [{"id": "t1", "executed_at": SESSION_START_ISO, "price": 10}]},  # exactly session start
    ])
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=10)
    assert result["status"] == COMPLETE
    assert result["pages"] == 3
    assert result["trades_cataloged"] == 3


def test_never_infers_complete_from_a_short_page_alone(tmp_path):
    """A short/empty page before session start is reached must NOT be
    treated as complete -- only a genuine calendar-aware session-start
    arrival does that."""
    client = _FakeClient([
        {"data": [{"id": "t2", "executed_at": "2026-10-14T19:00:00Z", "price": 10}]},
        {"data": []},  # empty page, but we never verified reaching session start
    ])
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=10)
    assert result["status"] == UNKNOWN


def test_non_advancing_cursor_stops_and_marks_unknown_not_looping_forever(tmp_path):
    """Duplicate timestamps at a page boundary that cause the SAME
    older_than to repeat must stop pagination immediately, never loop,
    and the result is honestly UNKNOWN (not complete, not incomplete)."""
    same_ts = "2026-10-14T19:00:00Z"
    client = _FakeClient([
        {"data": [{"id": "t1", "executed_at": same_ts, "price": 10}]},
        {"data": [{"id": "t1-dup", "executed_at": same_ts, "price": 10}]},  # non-advancing
    ])
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=10)
    assert result["status"] == UNKNOWN
    assert result["pages"] == 2  # made it onto the second page before detecting the stall


def test_duplicate_timestamps_at_a_real_boundary_are_deduped_not_errors(tmp_path):
    """A genuinely overlapping page boundary (the same trade appears at
    the edge of two pages, same id) is expected and must dedupe via
    UNIQUE(source_id), not be treated as a non-advancing-cursor error --
    the cursor DOES move forward because the SECOND distinct trade in
    page 2 is strictly older."""
    client = _FakeClient([
        {"data": [
            {"id": "t2", "executed_at": "2026-10-14T19:00:00Z", "price": 10},
            {"id": "t1", "executed_at": "2026-10-14T18:00:00Z", "price": 10},
        ]},
        {"data": [
            {"id": "t1", "executed_at": "2026-10-14T18:00:00Z", "price": 10},  # boundary dupe
            {"id": "t0", "executed_at": SESSION_START_ISO, "price": 10},
        ]},
    ])
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=2, max_pages=10)
    assert result["status"] == COMPLETE
    conn = sqlite3.connect(cat.db_path)
    count = conn.execute("SELECT COUNT(*) FROM darkpool_observations WHERE ticker='SPY'").fetchone()[0]
    assert count == 3  # t0, t1, t2 -- t1's boundary duplicate was not double-counted
    conn.close()


def test_incomplete_when_max_pages_exhausted_before_session_start(tmp_path):
    pages = [
        {"data": [{"id": f"t{i}", "executed_at": f"2026-10-14T1{9-i}:00:00Z", "price": 10}]}
        for i in range(5)
    ]
    client = _FakeClient(pages)
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=5)
    assert result["status"] == INCOMPLETE


def test_weekend_date_is_unknown_not_complete_or_incomplete(tmp_path):
    client = _FakeClient([])
    cat = _catalog(tmp_path)
    saturday = date(2026, 10, 17)
    result = paginate_ticker_day(client, cat, "SPY", saturday)
    assert result["status"] == UNKNOWN
    assert result["pages"] == 0


def test_unavailable_response_marks_unknown(tmp_path):
    client = _FakeClient([{"status": "unauthorized", "ok": False}])
    cat = _catalog(tmp_path)
    result = paginate_ticker_day(client, cat, "SPY", TRADING_DATE)
    assert result["status"] == UNKNOWN


def test_coverage_status_is_recorded_as_a_distinct_row_per_ticker_date(tmp_path):
    client = _FakeClient([
        {"data": [{"id": "t1", "executed_at": SESSION_START_ISO, "price": 10}]},
    ])
    cat = _catalog(tmp_path)
    paginate_ticker_day(client, cat, "SPY", TRADING_DATE, page_limit=1)
    status = cat.get_coverage_status("SPY", TRADING_DATE.isoformat())
    assert status["status"] == COMPLETE
    assert status["session_start_verified"] is True


class TestUnscheduledClosureCannotBeMisclassifiedAsComplete:
    """Item 5 (pre-Phase-D follow-up): trading_calendar.py's hand-written
    NYSE holiday table has NO mechanism for an unscheduled closure (a
    historical full-market shutdown, a weather emergency, a technical
    outage day) -- those are not on any fixed annual pattern, so
    `tc.is_trading_day`/`session_info` genuinely, wrongly believe such a
    date is a completely normal trading day.

    VERIFIED ANSWER (read dark_pool_pagination.py's coverage-determination
    logic directly, not assumed): this safety net ALREADY EXISTS, and it
    lives in `paginate_ticker_day`'s own logic, not the calendar. The
    calendar being wrong about a date never matters here, because
    completeness is NEVER inferred merely from a day "being a trading
    day" -- it is *exclusively* inferred from a real, non-empty page whose
    oldest trade's timestamp is <= session_start (`reached_session_start`).
    On a date the market was actually fully closed (real UW pagination
    legitimately returns zero trades, since there were genuinely none to
    retrieve), the VERY FIRST page comes back empty. The existing
    `if not page_trades: status = COMPLETE if reached_session_start else
    UNKNOWN` branch cannot possibly set COMPLETE here, because
    `reached_session_start` can only ever become True after seeing at
    least one real, non-empty page first -- and there wasn't one. The
    result is honestly UNKNOWN, never a phantom 'complete' day with zero
    real trading.

    This class proves that property directly against a date
    trading_calendar.py genuinely, incorrectly believes is a normal
    trading day (not a weekend/known-holiday -- picked to be a real
    ordinary Tuesday with no calendar exception at all, i.e. exactly what
    an *unscheduled* closure would look like to this calendar), with zero
    observations available for the whole day. No code change was needed
    -- this is the existing, correct design, and this test is the
    regression coverage proving it, requested explicitly because it
    did not already exist in this suite."""

    UNSCHEDULED_CLOSURE_DATE = date(2026, 9, 15)  # an ordinary Tuesday; tc has no exception for it

    def test_calendar_genuinely_believes_this_is_a_normal_trading_day(self):
        """Sanity check on the premise itself: trading_calendar.py has no
        way to know this date is special, so it reports a normal full
        trading day -- this is the exact blind spot item 5 describes."""
        from app.core import trading_calendar as tc

        info = tc.session_info(self.UNSCHEDULED_CLOSURE_DATE)
        assert info.is_trading_day is True
        assert info.holiday_name is None
        assert tc.session_start_utc(self.UNSCHEDULED_CLOSURE_DATE) is not None

    def test_zero_real_observations_on_a_calendar_believed_trading_day_yields_unknown_not_complete(self, tmp_path):
        """The real failure mode this must guard against: pagination
        genuinely finds NO real dark-pool prints for a date the calendar
        says should have a full session (simulating an unscheduled full-
        market closure, or equally a genuine data gap/outage) -- this
        must NEVER silently become 'complete'."""
        client = _FakeClient([{"data": []}])  # the very first page is already empty
        cat = _catalog(tmp_path)
        result = paginate_ticker_day(client, cat, "SPY", self.UNSCHEDULED_CLOSURE_DATE, page_limit=500, max_pages=10)
        assert result["status"] == UNKNOWN
        assert result["status"] != COMPLETE

        status_row = cat.get_coverage_status("SPY", self.UNSCHEDULED_CLOSURE_DATE.isoformat())
        assert status_row["status"] == UNKNOWN
        assert status_row["session_start_verified"] is False

    def test_this_unknown_day_is_structurally_excluded_from_the_baseline_too(self, tmp_path):
        """Belt-and-suspenders across the two modules: confirms the
        UNKNOWN classification this test proves pagination produces is
        ALSO the exact status dark_pool_baseline.eligible_sessions (Phase
        C) filters out of the eligible historical-baseline set -- an
        unscheduled-closure day recorded this way can never silently
        contribute a phantom zero-activity 'normal session' data point to
        the baseline statistics either."""
        from app.core import dark_pool_baseline as dpb

        client = _FakeClient([{"data": []}])
        cat = _catalog(tmp_path)
        paginate_ticker_day(client, cat, "SPY", self.UNSCHEDULED_CLOSURE_DATE, page_limit=500, max_pages=10)

        coverage_rows = cat.list_coverage_status("SPY")
        eligible = dpb.eligible_sessions(coverage_rows)
        assert all(row["coverage_date"] != self.UNSCHEDULED_CLOSURE_DATE.isoformat() for row in eligible)


def test_interrupted_job_resumes_from_checkpoint_not_from_scratch(tmp_path):
    """Simulates a process restart mid-pagination: run one page, then
    construct a FRESH client+catalog-handle (same underlying db file)
    and resume -- the resumed run must continue from the checkpointed
    older_than cursor, not restart from "now"."""
    cat = _catalog(tmp_path)
    client1 = _FakeClient([
        {"data": [{"id": "t2", "executed_at": "2026-10-14T19:00:00Z", "price": 10}]},
    ])
    # Manually drive one page then simulate a crash: the job row is
    # left in status='running' with its checkpoint, no "done" was ever
    # written for it (paginate_ticker_day with max_pages=1 and no break
    # reaches the `else` branch -- INCOMPLETE -- but the job source_id
    # + checkpoint are what matter for resumability).
    result1 = paginate_ticker_day(client1, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=1)
    assert result1["status"] == INCOMPLETE
    job = cat.get_ingestion_job(result1["job_source_id"])
    assert job["detail"]["older_than"] == "2026-10-14T19:00:00Z"

    # "Restart": a brand-new paginate_ticker_day call for the same
    # ticker+date must find and resume that exact job/cursor.
    client2 = _FakeClient([
        {"data": [{"id": "t1", "executed_at": SESSION_START_ISO, "price": 10}]},
    ])
    result2 = paginate_ticker_day(client2, cat, "SPY", TRADING_DATE, page_limit=1, max_pages=10, resume=True)
    assert result2["resumed"] is True
    assert client2.calls[0] == "2026-10-14T19:00:00Z"  # resumed from the checkpoint, not None/"now"
    assert result2["status"] == COMPLETE

    conn = sqlite3.connect(cat.db_path)
    count = conn.execute("SELECT COUNT(*) FROM darkpool_observations WHERE ticker='SPY'").fetchone()[0]
    assert count == 2  # t1 + t2, never double-fetched/double-inserted across the "restart"
    conn.close()
