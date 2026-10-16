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
