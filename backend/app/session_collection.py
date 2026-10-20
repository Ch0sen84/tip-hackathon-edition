"""Session-coverage collection for the real-baseline gates.

Signal-integrity remediation (audit B1/B8/B18; Phase 2: rebuilt on the
LIVE-VERIFIED Unusual Whales pagination contract).

What this module guarantees
---------------------------
For one dataset ('darkpool' | 'options_flow'), ticker and NYSE session, it
records the contiguous time interval it has PROVABLY collected
(ingestion.session_coverage) and why. It is called by every Live scan (small
page budget, evidence session) and by the bounded baseline backfill
(scheduler hook / explicit route).

Verified provider contract (10 bounded live requests, SPY, 2026-10-20 --
see docs/UW_PAGINATION_VERIFICATION.md)
  dark pool  /api/darkpool/{T}?date=D&newer_than=&older_than=&limit<=500
             date-scoped to NY date D; newest first; `newer_than` INCLUSIVE,
             `older_than` EXCLUSIVE; second-resolution timestamps; a query
             with nothing in range returns HTTP 200 + []; >= 30 sessions of
             history on Basic; a date outside entitlement is documented as
             403 historic_data_access_missing.
  flow       /api/option-trades/flow-alerts?ticker_symbol=T&newer_than=
             &older_than=&limit<=200
             newest first; BOTH bounds INCLUSIVE; microsecond timestamps;
             unique `id`; crosses sessions/weekends; no `date` parameter;
             without `newer_than` only 7 days before `older_than` (doc).
  For both: every returned row lay inside the requested bounds, and a page
  shorter than `limit` exhausted its window.

Coverage proof (no assumption that a print exists at the open)
  Each fetch asks for a BOUNDED window [lo, hi] (newer_than=lo, older_than=hi)
  and pages newest-first inside it. A page shorter than `limit` -- including
  an empty page -- proves the window exhausted (verified semantics). Every
  page is checked: rows inside the bounds, dark-pool rows on the requested
  session date, cursor advancing; any violation makes the session UNKNOWN.
  So a quiet or thinly traded session can be COMPLETE with few or zero rows,
  and a liquid session is COMPLETE only once every page has been walked.

Status vocabulary (session_coverage.status) with reason codes (last_error):
  complete     [open, close] provably collected
  partial      a contiguous sub-interval only -- reasons:
               session_in_progress | page_budget_exhausted
  unknown      coverage could not be established -- reasons:
               bounds_not_honored | wrong_session_date | cursor_stuck |
               catalog_write_failed
  unavailable  the provider refuses this session -- reasons:
               provider_history_cutoff | not_entitled
Transient provider failures (429/5xx/timeout/malformed) leave stored coverage
untouched and are reported as a failed COLLECTION, never as coverage.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from .adapters.base import COLLECTION_OK
from .adapters.live_uw_adapter import LiveUWAdapter, _normalize_flow_alert
from .core import trading_calendar as tc

REPORTING_LAG = timedelta(seconds=60)  # newest minute of an open session may still be reported late

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
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    time_field: str
    page_limit: int
    date_scoped: bool
    upper_inclusive: bool          # older_than inclusive? (verified per endpoint)
    cursor_step: timedelta         # how far ABOVE the page's oldest row the next cursor sits

    def fetch(self, client, ticker: str, session_date: date, lo: datetime, hi: datetime) -> dict:
        if self.name == "darkpool":
            return client.darkpool_ticker(ticker, date=session_date.isoformat(), limit=self.page_limit,
                                          newer_than=_iso(lo), older_than=_iso(hi))
        return client.flow_alerts(ticker=ticker, limit=self.page_limit, newer_than=_iso(lo), older_than=_iso(hi))

    def normalize(self, row: dict) -> dict:
        return _normalize_flow_alert(row) if self.name == "options_flow" else row

    def catalog(self, catalog, ticker: str, rows: list, kind: str) -> int:
        if self.name == "darkpool":
            return catalog.catalog_darkpool_observations(ticker, rows, kind=kind)
        return catalog.catalog_options_flow_observations(ticker, rows, kind=kind)

    def in_bounds(self, ts: datetime, lo: datetime, hi: datetime) -> bool:
        if ts < lo:
            return False
        return ts <= hi if self.upper_inclusive else ts < hi


DATASETS = {
    # dark pool: older_than EXCLUSIVE at second resolution -> next cursor is
    # oldest+1s so prints sharing the boundary second are re-read (deduped).
    "darkpool": DatasetSpec("darkpool", "executed_at", 500, True, False, timedelta(seconds=1)),
    # flow alerts: older_than INCLUSIVE (microseconds) -> cursor = oldest;
    # the boundary rows come back again and are deduplicated by `id`.
    "options_flow": DatasetSpec("options_flow", "created_at", 200, False, True, timedelta(0)),
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


def _fetch_window(spec, client, catalog, ticker, session_date, lo, hi, budget, kind) -> dict:
    """Pages newest-first inside [lo, hi]. Returns
       {"outcome": "exhausted" | "budget" | "unverifiable" | "failed" | "cutoff",
        "oldest": earliest row time seen, "pages", "rows", "reason", "status"}."""
    cursor, pages, n_rows, oldest, prev_oldest = hi, 0, 0, None, None
    while pages < budget:
        body = spec.fetch(client, ticker, session_date, lo, cursor)
        pages += 1
        failure = LiveUWAdapter._failure_status(body)
        if failure:
            status, detail, code = failure
            detail_l = (detail or "").lower()
            if status == "unavailable_on_tier" and "historic" in detail_l:
                return {"outcome": "cutoff", "reason": "provider_history_cutoff", "pages": pages, "rows": n_rows,
                        "oldest": oldest, "status": COLLECTION_OK}
            if status in ("unavailable_on_tier", "unauthorized"):
                return {"outcome": "cutoff", "reason": "not_entitled", "pages": pages, "rows": n_rows,
                        "oldest": oldest, "status": status}
            return {"outcome": "failed", "reason": detail, "pages": pages, "rows": n_rows, "oldest": oldest,
                    "status": status}
        data = body.get("data")
        if not isinstance(data, list):
            return {"outcome": "failed", "reason": "data is not a list", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "status": "malformed"}
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
            if not spec.in_bounds(ts, lo, cursor):
                return {"outcome": "unverifiable", "reason": "bounds_not_honored", "pages": pages,
                        "rows": n_rows, "oldest": oldest, "status": COLLECTION_OK}
            if spec.date_scoped and ts.astimezone(tc.NY_TZ).date() != session_date:
                return {"outcome": "unverifiable", "reason": "wrong_session_date", "pages": pages,
                        "rows": n_rows, "oldest": oldest, "status": COLLECTION_OK}
            rows.append(spec.normalize(raw))
            times.append(ts)
        if rows:
            try:
                spec.catalog(catalog, ticker, rows, kind)
            except Exception:  # noqa: BLE001 -- storage guard / write failure: cannot prove coverage
                return {"outcome": "unverifiable", "reason": "catalog_write_failed", "pages": pages,
                        "rows": n_rows, "oldest": oldest, "status": COLLECTION_OK}
            n_rows += len(rows)
            page_oldest = min(times)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
        if len(data) < spec.page_limit:
            # verified: a short (or empty) page exhausts the bounded window
            return {"outcome": "exhausted", "reason": None, "pages": pages, "rows": n_rows, "oldest": oldest,
                    "status": COLLECTION_OK}
        page_oldest = min(times) if times else None
        if page_oldest is None or (prev_oldest is not None and page_oldest >= prev_oldest):
            return {"outcome": "unverifiable", "reason": "cursor_stuck", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "status": COLLECTION_OK}
        prev_oldest = page_oldest
        cursor = page_oldest + spec.cursor_step
        if cursor >= hi and pages > 1:  # cannot make progress inside this window
            return {"outcome": "unverifiable", "reason": "cursor_stuck", "pages": pages, "rows": n_rows,
                    "oldest": oldest, "status": COLLECTION_OK}
    return {"outcome": "budget", "reason": "page_budget_exhausted", "pages": pages, "rows": n_rows,
            "oldest": oldest, "status": COLLECTION_OK}


def update_session_coverage(client, catalog, dataset: str, ticker: str, session_date: date,
                            now_utc: Optional[datetime] = None, max_pages: int = DEFAULT_SCAN_PAGES,
                            kind: str = "on_demand") -> CoverageUpdate:
    """Extend proven coverage of one session within `max_pages` requests.
    Never raises on provider behaviour; never claims unproven coverage."""
    spec = DATASETS[dataset]
    now_utc = now_utc or datetime.now(timezone.utc)
    sd = session_date.isoformat()
    info = tc.session_info(session_date)
    if not info.is_trading_day:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, None, detail="not a trading session")
    open_utc = info.session_open_et.astimezone(timezone.utc)
    close_utc = info.session_close_et.astimezone(timezone.utc)
    end = min(close_utc, now_utc - REPORTING_LAG)
    if end <= open_utc:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                              detail="session has not opened yet")

    cov = catalog.get_session_coverage(dataset, ticker, sd) or {}
    if cov.get("status") in ("unknown", "unavailable") and cov.get("last_error") in (
            "provider_history_cutoff", "not_entitled"):
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, cov, detail=cov.get("last_error"))
    usable = cov.get("status") in ("complete", "partial")
    c_from = _parse(cov.get("covered_from_utc")) if usable else None
    c_through = _parse(cov.get("covered_through_utc")) if usable else None

    budget, pages, rows = max_pages, 0, 0
    reason = None
    status = COLLECTION_OK

    def store(cov_status, frm, thr, why):
        catalog.upsert_session_coverage(dataset, ticker, sd, cov_status, frm.isoformat() if frm else None,
                                        thr.isoformat() if thr else None, pages, rows, why, kind=kind)

    # 1) newest gap: (c_through, end], or the whole [open, end] if nothing usable yet
    top_lo = c_through if c_through is not None else open_utc
    if c_through is None or c_through < end:
        w = _fetch_window(spec, client, catalog, ticker, session_date, top_lo, end, budget, kind)
        pages += w["pages"]; rows += w["rows"]; budget -= w["pages"]
        if w["outcome"] == "failed":
            return CoverageUpdate(dataset, ticker, sd, w["status"], cov or None, pages=pages, rows_fetched=rows,
                                  detail=w["reason"])
        if w["outcome"] == "cutoff":
            store("unavailable", None, None, w["reason"])
            return CoverageUpdate(dataset, ticker, sd, w["status"], catalog.get_session_coverage(dataset, ticker, sd),
                                  pages=pages, rows_fetched=rows, detail=w["reason"])
        if w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                                  pages=pages, rows_fetched=rows, detail=w["reason"])
        if w["outcome"] == "exhausted":
            if c_through is None:
                c_from, c_through = open_utc, end
            else:
                c_through = end
        else:  # budget: newest island [oldest_seen, end] (rows stay cataloged; older part re-walked later)
            if c_through is not None and w["oldest"] is not None and w["oldest"] <= c_through:
                c_through = end
            else:
                c_from, c_through = (w["oldest"] or end), end
            reason = w["reason"]

    # 2) backward gap: [open, c_from)
    if c_from is not None and c_from > open_utc and budget > 0:
        w = _fetch_window(spec, client, catalog, ticker, session_date, open_utc, c_from, budget, kind)
        pages += w["pages"]; rows += w["rows"]
        if w["outcome"] == "exhausted":
            c_from = open_utc
            reason = None
        elif w["outcome"] == "budget":
            c_from = min(c_from, w["oldest"]) if w["oldest"] else c_from
            reason = w["reason"]
        elif w["outcome"] == "failed":
            status, reason = w["status"], w["reason"]   # keep what was proven so far
        elif w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                                  pages=pages, rows_fetched=rows, detail=w["reason"])
        else:  # cutoff
            store("unavailable", None, None, w["reason"])
            return CoverageUpdate(dataset, ticker, sd, w["status"], catalog.get_session_coverage(dataset, ticker, sd),
                                  pages=pages, rows_fetched=rows, detail=w["reason"])
    elif c_from is not None and c_from > open_utc:
        reason = reason or "page_budget_exhausted"

    if c_from is not None and c_from <= open_utc and c_through is not None and c_through >= close_utc:
        cov_status, why = "complete", None
    else:
        cov_status = "partial"
        why = reason or ("session_in_progress" if c_through is not None and c_through < close_utc
                         else "page_budget_exhausted")
    store(cov_status, c_from, c_through, why)
    return CoverageUpdate(dataset, ticker, sd, status, catalog.get_session_coverage(dataset, ticker, sd),
                          pages=pages, rows_fetched=rows, detail=why)


def backfill_baseline(client, catalog, dataset: str, ticker: str, evidence_session: date,
                      max_pages: int = DEFAULT_BACKFILL_PAGES,
                      lookback_sessions: int = BACKFILL_LOOKBACK_SESSIONS,
                      kind: str = "background", now_utc: Optional[datetime] = None) -> dict:
    """Bounded backfill, spending at most `max_pages` UW requests: first
    extends the EVIDENCE session (the comparison needs it covered from the
    open), then completes prior sessions newest-first. Never fabricates
    coverage; stops on a provider failure or history cutoff."""
    now_utc = now_utc or datetime.now(timezone.utc)
    budget, results = max_pages, []

    def touch(d):
        nonlocal budget
        u = update_session_coverage(client, catalog, dataset, ticker, d, now_utc=now_utc, max_pages=budget, kind=kind)
        budget -= max(u.pages, 0)
        cov = u.coverage or {}
        results.append({"session_date": d.isoformat(), "status": u.collection_status,
                        "coverage": cov.get("status"), "reason": cov.get("last_error"), "pages": u.pages})
        return u, cov

    if tc.is_trading_day(evidence_session):
        cov = catalog.get_session_coverage(dataset, ticker, evidence_session.isoformat()) or {}
        if budget > 0 and cov.get("status") not in ("complete", "unavailable"):
            u, cov = touch(evidence_session)
            if not u.ok:
                return _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results)
    d = evidence_session
    for _ in range(lookback_sessions):
        d = tc.previous_trading_day(d)
        if budget <= 0:
            break
        cov = catalog.get_session_coverage(dataset, ticker, d.isoformat()) or {}
        if cov.get("status") in ("complete", "unavailable") or (
                cov.get("status") == "unknown" and cov.get("last_error") in ("bounds_not_honored", "wrong_session_date")):
            if cov.get("status") == "unavailable":
                break  # history cutoff: older sessions are unavailable too
            continue
        u, cov = touch(d)
        if not u.ok or cov.get("status") == "unavailable":
            break
    return _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results)


def _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results):
    complete = sum(1 for c in catalog.list_session_coverage(dataset, ticker)
                   if c["status"] == "complete" and c["session_date"] < evidence_session.isoformat())
    return {"dataset": dataset, "ticker": ticker, "pages_used": max_pages - max(budget, 0),
            "sessions_touched": results, "complete_prior_sessions": complete}
