"""SUPERSEDED (signal-integrity remediation): the running app collects
session coverage through app/session_collection.py into the generic
`session_coverage` table, which is what the baseline gates read. This module
and the `darkpool_coverage_status` table it writes are kept only for their
existing tests/history; nothing in the app calls paginate_ticker_day (it
never had a caller in 05c47ca either -- audit B1).

Phase B: bounded, resumable backward pagination over
/api/darkpool/{ticker} using the `older_than`/`newer_than` params
live_uw_adapter.UWRestClient.darkpool_ticker already exposes (just not
looped until now).

Walks ONE trading day backward, page by page, cataloging every trade via
the shared ingestion layer (app/ingestion.py) -- duplicate timestamps at
a page boundary are EXPECTED and silently deduped by
darkpool_observations' UNIQUE(source_id) constraint, never treated as an
error. Classifies the day's coverage into exactly one of three states
(darkpool_coverage_status.status): 'complete' only when pagination
genuinely walked back to/through the session's real, calendar-aware
start time (app/core/trading_calendar.py) -- never inferred merely from
a short/empty page; 'unknown' when coverage cannot be confidently
established at all (non-advancing cursor, an empty very-first page, an
unavailable/erroring response); 'incomplete' when pagination made real
progress but exhausted its page budget before reaching session start.

Resumability: progress is checkpointed into `ingestion_jobs` after every
page via IngestionCatalog.update_ingestion_job -- see
`resume_or_start_job`. A caller that was interrupted mid-run (process
restart, crash) can look up the same job (by ticker+date) and continue
from its last `older_than` cursor instead of re-fetching/double-inserting
already-collected pages (dedup would also catch a re-fetch, but resuming
from the checkpoint avoids the wasted UW requests entirely).
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from typing import Optional

from .core import trading_calendar as tc
from .ingestion import IngestionCatalog

COMPLETE = "complete"
INCOMPLETE = "incomplete"
UNKNOWN = "unknown"

JOB_TYPE = "darkpool_day_pagination"


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _oldest_executed_at(trades: list) -> Optional[str]:
    times = [t.get("executed_at") for t in trades if t.get("executed_at")]
    return min(times) if times else None


def resume_or_start_job(catalog: IngestionCatalog, ticker: str, trading_date: date_cls, kind: str) -> tuple:
    """Returns (job_source_id, resumed: bool, older_than_checkpoint)."""
    match = {"ticker": ticker, "trading_date": trading_date.isoformat()}
    existing = catalog.find_resumable_job(JOB_TYPE, match)
    if existing is not None:
        return existing["source_id"], True, existing["detail"].get("older_than")
    detail = {**match, "older_than": None, "pages": 0}
    job_source_id = catalog.start_ingestion_job(JOB_TYPE, detail, kind=kind)
    return job_source_id, False, None


def paginate_ticker_day(
    client, catalog: IngestionCatalog, ticker: str, trading_date: date_cls,
    page_limit: int = 500, max_pages: int = 50, kind: str = "on_demand",
    resume: bool = True,
) -> dict:
    """Paginates ONE trading day for `ticker` backward from "now"
    (or from a checkpointed cursor when `resume=True` and an interrupted
    job for the same ticker+date is found). Returns:
        {"status": COMPLETE|INCOMPLETE|UNKNOWN, "pages": int,
         "trades_cataloged": int, "job_source_id": str, "resumed": bool}
    """
    session_start = tc.session_start_utc(trading_date)

    if resume:
        job_source_id, resumed, older_than = resume_or_start_job(catalog, ticker, trading_date, kind)
    else:
        job_source_id = catalog.start_ingestion_job(
            JOB_TYPE, {"ticker": ticker, "trading_date": trading_date.isoformat(), "older_than": None, "pages": 0},
            kind=kind,
        )
        resumed, older_than = False, None

    if session_start is None:
        catalog.update_ingestion_job(
            job_source_id, "done",
            {"ticker": ticker, "trading_date": trading_date.isoformat(), "coverage": UNKNOWN,
             "reason": "not a weekday (holiday calendar not modeled -- see trading_calendar.py)"},
        )
        catalog.record_coverage_status(ticker, trading_date.isoformat(), UNKNOWN, 0, False, kind=kind)
        return {"status": UNKNOWN, "pages": 0, "trades_cataloged": 0,
                "job_source_id": job_source_id, "resumed": resumed}

    trades_cataloged = 0
    pages = 0
    status = UNKNOWN
    reached_session_start = False
    first_page = True

    for _ in range(max_pages):
        body = client.darkpool_ticker(
            ticker, date=trading_date.isoformat(), limit=page_limit, older_than=older_than,
        )
        if not isinstance(body, dict) or body.get("status") in ("unavailable_on_tier", "unauthorized", "error"):
            status = UNKNOWN
            break

        page_trades = list(body.get("data") or [])
        pages += 1

        if not page_trades:
            # An empty page proves nothing about completeness on its
            # own -- ONLY treat the day as complete if we already
            # independently verified reaching session start on a prior
            # (non-empty) page.
            status = COMPLETE if reached_session_start else UNKNOWN
            break

        trades_cataloged += catalog.catalog_darkpool_observations(ticker, page_trades, kind=kind)

        oldest = _oldest_executed_at(page_trades)
        if oldest is None:
            status = UNKNOWN
            break

        oldest_dt = _parse_iso(oldest)
        if oldest_dt is not None and oldest_dt <= session_start:
            reached_session_start = True
            status = COMPLETE
            break

        if not first_page and older_than is not None and not (oldest < older_than):
            # Non-advancing cursor: the next request's older_than would
            # not move the window further back than this page already
            # covered. Stop immediately rather than loop forever or
            # silently re-fetch the same page -- coverage for this day
            # is honestly UNKNOWN, not complete and not incomplete.
            status = UNKNOWN
            break

        older_than = oldest
        first_page = False
        catalog.update_ingestion_job(
            job_source_id, "running",
            {"ticker": ticker, "trading_date": trading_date.isoformat(), "older_than": older_than, "pages": pages},
        )
    else:
        # Loop exhausted max_pages without hitting any break above --
        # genuine progress was made (we're walking backward), just not
        # all the way to session start within budget.
        status = INCOMPLETE

    final_detail = {
        "ticker": ticker, "trading_date": trading_date.isoformat(), "older_than": older_than,
        "pages": pages, "coverage": status,
    }
    # INCOMPLETE means there is genuinely more work left to do (budget
    # ran out before session start) -- the job is left 'running' with
    # its checkpoint so a later call (this phase's resumability
    # requirement) can find and continue it via find_resumable_job.
    # COMPLETE/UNKNOWN are terminal for this job: nothing more to do
    # (COMPLETE reached session start; UNKNOWN stopped on a condition
    # that re-running from scratch, not resuming, would be needed to
    # possibly resolve -- e.g. a non-advancing cursor or an
    # unavailable/erroring response).
    job_status = "running" if status == INCOMPLETE else "done"
    catalog.update_ingestion_job(job_source_id, job_status, final_detail)
    catalog.record_coverage_status(ticker, trading_date.isoformat(), status, pages, reached_session_start, kind=kind)
    return {"status": status, "pages": pages, "trades_cataloged": trades_cataloged,
            "job_source_id": job_source_id, "resumed": resumed}
