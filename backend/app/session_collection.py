"""Session-coverage collection for the real-baseline gates.

Signal-integrity remediation (audit B1/B8/B18). In 05c47ca the dark-pool
baseline could never become ready in Live Mode: the only writer of coverage
status (dark_pool_pagination.paginate_ticker_day) had no caller, every scan
read just the newest single page (500 prints / 100 alerts), and the captured
Live database held 12,356 prints and zero coverage rows.

This module is the one place that pages UW backward through an NYSE trading
session and records -- per dataset ('darkpool' | 'options_flow'), ticker
and session -- the contiguous interval it has VERIFIABLY collected
(ingestion.session_coverage). It is called by:

  * every Live scan, for the evidence session, with a small interactive page
    budget (top-up of the newest data, then backward extension), and
  * the bounded baseline backfill (backfill_baseline) that the background
    scheduler and the explicit POST /api/ticker/{t}/baseline/backfill route
    run for earlier sessions.

Verification -- coverage is only claimed for what was proven:
  * every page must respect the cursor (no row newer than `older_than`),
    dark-pool pages must belong to the requested session date, and the
    cursor must advance; otherwise coverage is 'unknown' and the reason is
    recorded. (UW's `older_than` on flow-alerts is documented but has not
    been live-verified by this project -- this check is what makes relying
    on it safe.)
  * an empty FIRST page proves nothing (no data vs. no access) -> 'unknown'.
  * only a page containing a row at or before the session open proves the
    interval reaches the open; running out of data first (an empty later
    page) leaves the session "partial" -- a provider history-depth limit
    looks identical to "no earlier activity".
  * a provider failure leaves the existing coverage untouched and is
    reported as a failed collection, never as an empty session.

The interval only ever grows contiguously. If a top-up cannot reach the
previously covered interval within its page budget, the newer island
replaces it (rows stay cataloged and deduplicated; the older part will be
re-walked), so a gap can never be counted as covered.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .adapters.base import COLLECTION_OK
from .adapters.live_uw_adapter import LiveUWAdapter, _normalize_flow_alert
from .core import trading_calendar as tc

REPORTING_LAG = timedelta(seconds=60)  # newest minute may still be reported late
CURSOR_OVERLAP = timedelta(seconds=1)   # UW timestamps are second-resolution

DEFAULT_SCAN_PAGES = int(os.environ.get("TIP_SCAN_COVERAGE_PAGES", "4"))
DEFAULT_BACKFILL_PAGES = int(os.environ.get("TIP_BASELINE_BACKFILL_PAGES", "60"))
BACKFILL_LOOKBACK_SESSIONS = int(os.environ.get("TIP_BASELINE_LOOKBACK_SESSIONS", "30"))


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    time_field: str
    page_limit: int
    date_scoped: bool

    def fetch(self, client, ticker: str, session_date: date, older_than: datetime) -> dict:
        if self.name == "darkpool":
            return client.darkpool_ticker(ticker, date=session_date.isoformat(), limit=self.page_limit,
                                          older_than=_iso(older_than))
        return client.flow_alerts(ticker=ticker, limit=self.page_limit, older_than=_iso(older_than))

    def normalize(self, row: dict) -> dict:
        return _normalize_flow_alert(row) if self.name == "options_flow" else row

    def catalog(self, catalog, ticker: str, rows: list, kind: str) -> int:
        if self.name == "darkpool":
            return catalog.catalog_darkpool_observations(ticker, rows, kind=kind)
        return catalog.catalog_options_flow_observations(ticker, rows, kind=kind)


DATASETS = {
    "darkpool": DatasetSpec("darkpool", "executed_at", 500, True),
    "options_flow": DatasetSpec("options_flow", "created_at", 200, False),
}


@dataclass
class CoverageUpdate:
    dataset: str
    ticker: str
    session_date: str
    collection_status: str            # ok | error | rate_limited | ... (adapters/base.py vocabulary)
    coverage: Optional[dict]
    pages: int = 0
    rows_fetched: int = 0
    detail: Optional[str] = None
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def ok(self) -> bool:
        return self.collection_status == COLLECTION_OK

    def summary(self) -> dict:
        return {"status": self.collection_status, "ok": self.ok, "retrieved_at": self.retrieved_at,
                "pages": self.pages, "rows_fetched": self.rows_fetched, "detail": self.detail,
                "coverage": self.coverage, "source": "unusual_whales"}


def _walk(spec, client, catalog, ticker, session_date, start_cursor, stop_at, open_utc, budget, kind):
    """Pages backward from `start_cursor` until a page reaches `stop_at`
    (or the session open). Returns dict(status, pages, rows, oldest,
    reached, bottom, error)."""
    cursor, pages, n_rows = start_cursor, 0, 0
    oldest, prev_oldest = None, None
    while pages < budget:
        body = spec.fetch(client, ticker, session_date, cursor)
        pages += 1
        failure = LiveUWAdapter._failure_status(body)
        if failure:
            return {"status": failure[0], "error": failure[1], "pages": pages, "rows": n_rows,
                    "oldest": oldest, "reached": False, "bottom": False}
        data = body.get("data")
        if not isinstance(data, list):
            return {"status": "malformed", "error": "data is not a list", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "reached": False, "bottom": False}
        rows, times = [], []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            own = raw.get("ticker") or raw.get("ticker_symbol")
            if own and str(own).upper() != ticker.upper():
                continue
            ts = _parse(raw.get(spec.time_field))
            if ts is None:
                continue
            if ts > cursor + CURSOR_OVERLAP:
                return {"status": "ok", "error": "pagination cursor not honored by provider", "pages": pages,
                        "rows": n_rows, "oldest": oldest, "reached": False, "bottom": False, "unverifiable": True}
            if spec.date_scoped and ts.astimezone(tc.NY_TZ).date() != session_date:
                return {"status": "ok", "error": "provider returned rows outside the requested session date",
                        "pages": pages, "rows": n_rows, "oldest": oldest, "reached": False, "bottom": False,
                        "unverifiable": True}
            rows.append(spec.normalize(raw))
            times.append(ts)
        if not rows:
            if pages == 1 and oldest is None:
                return {"status": "ok", "error": "first page empty: cannot distinguish no activity from no access",
                        "pages": pages, "rows": 0, "oldest": None, "reached": False, "bottom": False,
                        "unverifiable": True}
            # Ran out of data before seeing a row at/before the open: this
            # does NOT prove coverage from the open (history-depth limits look
            # identical to "no earlier activity").
            return {"status": "ok", "error": "no data at or before the session open was returned; "
                    "coverage from the open is unverified", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "reached": False, "bottom": True}
        try:
            spec.catalog(catalog, ticker, rows, kind)
        except Exception as e:  # noqa: BLE001 -- storage guard / write failure: cannot prove coverage
            return {"status": "ok", "error": f"catalog write failed: {type(e).__name__}", "pages": pages,
                    "rows": n_rows, "oldest": oldest, "reached": False, "bottom": False, "unverifiable": True}
        n_rows += len(rows)
        page_oldest = min(times)
        oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        if page_oldest <= stop_at or page_oldest <= open_utc:
            return {"status": "ok", "error": None, "pages": pages, "rows": n_rows, "oldest": oldest,
                    "reached": True, "bottom": False}
        if prev_oldest is not None and page_oldest >= prev_oldest:
            if len(data) < spec.page_limit:
                # A non-full page with nothing older than the boundary second:
                # the provider has no earlier data. Same as running out of
                # data -- coverage from the open stays unverified (partial).
                return {"status": "ok", "error": "no data at or before the session open was returned; "
                        "coverage from the open is unverified", "pages": pages, "rows": n_rows,
                        "oldest": oldest, "reached": False, "bottom": True}
            # A FULL page that cannot move past one second: genuinely stuck.
            return {"status": "ok", "error": "pagination cursor did not advance", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "reached": False, "bottom": False, "unverifiable": True}
        prev_oldest = page_oldest
        # +1s overlap: rows sharing the boundary second are re-read (and
        # deduplicated) rather than skipped.
        cursor = page_oldest + CURSOR_OVERLAP
    return {"status": "ok", "error": "page budget exhausted", "pages": pages, "rows": n_rows,
            "oldest": oldest, "reached": False, "bottom": False}


def update_session_coverage(client, catalog, dataset: str, ticker: str, session_date: date,
                            now_utc: Optional[datetime] = None, max_pages: int = DEFAULT_SCAN_PAGES,
                            kind: str = "on_demand") -> CoverageUpdate:
    """Extend verified coverage of one session. Never raises."""
    spec = DATASETS[dataset]
    now_utc = now_utc or datetime.now(timezone.utc)
    sd = session_date.isoformat()
    info = tc.session_info(session_date)
    if not info.is_trading_day:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, None, detail="not a trading session")
    open_utc = info.session_open_et.astimezone(timezone.utc)
    close_utc = info.session_close_et.astimezone(timezone.utc)
    target_end = min(close_utc, now_utc - REPORTING_LAG)
    if target_end <= open_utc:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                              detail="session has not opened yet")

    cov = catalog.get_session_coverage(dataset, ticker, sd) or {}
    c_from = _parse(cov.get("covered_from_utc")) if cov.get("status") != "unknown" else None
    c_through = _parse(cov.get("covered_through_utc")) if cov.get("status") != "unknown" else None
    budget, pages, rows, error, status = max_pages, 0, 0, None, COLLECTION_OK
    unverifiable = False

    # 1) top-up: contiguous coverage up to target_end
    if c_through is None or c_through < target_end:
        stop_at = c_through if c_through is not None else open_utc
        w = _walk(spec, client, catalog, ticker, session_date, target_end + CURSOR_OVERLAP, stop_at,
                  open_utc, budget, kind)
        pages += w["pages"]; rows += w["rows"]; budget -= w["pages"]
        status, error = w["status"], w.get("error")
        unverifiable = bool(w.get("unverifiable"))
        if status != COLLECTION_OK:
            return CoverageUpdate(dataset, ticker, sd, status, cov or None, pages=pages, rows_fetched=rows,
                                  detail=error)
        if unverifiable:
            catalog.upsert_session_coverage(dataset, ticker, sd, "unknown", None, None, pages, rows, error, kind=kind)
            return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK,
                                  catalog.get_session_coverage(dataset, ticker, sd), pages=pages,
                                  rows_fetched=rows, detail=error)
        if w["reached"] and w["oldest"] is not None and w["oldest"] <= open_utc:
            c_from, c_through = open_utc, target_end        # walked back past the open
        elif w["reached"] and c_through is not None:
            c_through = target_end                          # joined the existing interval
        elif w["oldest"] is not None:                       # budget/data ran out first: new island
            c_from, c_through = w["oldest"], target_end
            error = error or w.get("error")

    # 2) backward extension toward the open
    if c_from is not None and c_from > open_utc and budget > 0:
        w = _walk(spec, client, catalog, ticker, session_date, c_from + CURSOR_OVERLAP, open_utc,
                  open_utc, budget, kind)
        pages += w["pages"]; rows += w["rows"]
        if w["status"] != COLLECTION_OK:
            error = w.get("error")  # top-up succeeded; keep what was proven, report the failure
            status = w["status"]
        elif w.get("unverifiable"):
            error = w.get("error")
        elif w["reached"] and w["oldest"] is not None and w["oldest"] <= open_utc:
            c_from = open_utc
        elif w["oldest"] is not None:
            c_from = min(c_from, w["oldest"])
            error = w.get("error")

    complete = c_from is not None and c_from <= open_utc and c_through is not None and c_through >= close_utc
    cov_status = "complete" if complete else "partial"
    catalog.upsert_session_coverage(dataset, ticker, sd, cov_status, c_from.isoformat() if c_from else None,
                                    c_through.isoformat() if c_through else None, pages, rows, error, kind=kind)
    return CoverageUpdate(dataset, ticker, sd, status, catalog.get_session_coverage(dataset, ticker, sd),
                          pages=pages, rows_fetched=rows, detail=error)


def backfill_baseline(client, catalog, dataset: str, ticker: str, evidence_session: date,
                      max_pages: int = DEFAULT_BACKFILL_PAGES,
                      lookback_sessions: int = BACKFILL_LOOKBACK_SESSIONS,
                      kind: str = "background", now_utc: Optional[datetime] = None) -> dict:
    """Bounded backfill of COMPLETE coverage for sessions before
    `evidence_session` (newest first), spending at most `max_pages` UW
    requests. Never raises; never fabricates coverage."""
    now_utc = now_utc or datetime.now(timezone.utc)
    budget, d, results = max_pages, evidence_session, []
    for _ in range(lookback_sessions):
        d = tc.previous_trading_day(d)
        if budget <= 0:
            break
        cov = catalog.get_session_coverage(dataset, ticker, d.isoformat())
        if cov and cov["status"] in ("complete", "unknown"):
            continue
        u = update_session_coverage(client, catalog, dataset, ticker, d, now_utc=now_utc, max_pages=budget, kind=kind)
        budget -= max(u.pages, 1)
        results.append({"session_date": d.isoformat(), "status": u.collection_status,
                        "coverage": (u.coverage or {}).get("status"), "pages": u.pages, "detail": u.detail})
        if not u.ok:
            break  # provider failure: stop spending budget
    complete = sum(1 for c in catalog.list_session_coverage(dataset, ticker)
                   if c["status"] == "complete" and c["session_date"] < evidence_session.isoformat())
    return {"dataset": dataset, "ticker": ticker, "pages_used": max_pages - max(budget, 0),
            "sessions_touched": results, "complete_prior_sessions": complete}
