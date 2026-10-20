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

Out-of-window pages (Phase 9 reliability)
  A page with a row outside the requested bounds (or, for dark pool, off the
  requested NY date) is rejected WHOLE and never stored. A sanitized
  diagnostic (request bounds, observed min/max timestamps, a few offending
  timestamps -- no prices, sizes, ids or credentials) is recorded in
  collection_diagnostics. The SAME window is then re-requested at most
  BOUNDS_RETRIES_PER_WINDOW times (each retry is a logical page against the
  same page budget). Only a response that passes every check is stored and
  advances the cursor, so a recovered walk proves coverage exactly as a clean
  one does; if every retry is also rejected the session stays UNKNOWN
  (bounds_not_honored / wrong_session_date), as before.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
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
# Re-requests of ONE rejected (out-of-window) page before the session is left
# unknown. 0 restores the pre-Phase-9 behaviour; capped at 3.
BOUNDS_RETRIES_PER_WINDOW = max(0, min(3, int(os.environ.get("TIP_BOUNDS_RETRIES_PER_WINDOW", "1"))))
BOUNDS_RETRY_DELAY_S = max(0.0, min(30.0, float(os.environ.get("TIP_BOUNDS_RETRY_DELAY_S", "1.0"))))
DIAGNOSTIC_SAMPLE_ROWS = 5
_sleep = time.sleep          # patched by tests
_log = logging.getLogger(__name__)


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

    def request_params(self, ticker: str, session_date: date, lo: datetime, hi: datetime) -> dict:
        """The exact query this collector sends (never includes credentials)."""
        if self.name == "darkpool":
            return {"date": session_date.isoformat(), "limit": self.page_limit,
                    "newer_than": _iso(lo), "older_than": _iso(hi)}
        return {"ticker_symbol": ticker, "limit": self.page_limit, "newer_than": _iso(lo), "older_than": _iso(hi)}

    def endpoint(self, ticker: str) -> str:
        return f"/api/darkpool/{ticker}" if self.name == "darkpool" else "/api/option-trades/flow-alerts"

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
    bounds_retries: int = 0            # re-requests of rejected out-of-window pages (Phase 9)
    bounds_recovered: int = 0          # of those, windows that then passed every check

    @property
    def ok(self) -> bool:
        return self.collection_status == COLLECTION_OK

    def summary(self) -> dict:
        return {"status": self.collection_status, "ok": self.ok, "retrieved_at": self.retrieved_at,
                "pages": self.pages, "rows_fetched": self.rows_fetched, "detail": self.detail,
                "coverage": self.coverage, "source": "unusual_whales",
                "bounds_retries": self.bounds_retries, "bounds_recovered": self.bounds_recovered}


def _classify_violation(v: dict, samples: list, prev_newest: Optional[datetime],
                        page_max: Optional[datetime]) -> tuple:
    """Best-effort label for a rejected page, from the sanitized facts only.
    Returns (classification, flags). A label is a HYPOTHESIS for a human to
    check against the recorded facts, never an automatic conclusion."""
    flags = []
    if v["out_of_order"]:
        flags.append("rows_not_newest_first")
    if any(x["fractional_seconds"] for x in samples):
        flags.append("fractional_second_timestamps")
    offs = [abs(x["delta_s"]) for x in samples if x["kind"] in ("above_upper", "below_lower")]
    if offs and all(o >= 3540 and (o % 3600 <= 60 or o % 3600 >= 3540) for o in offs):
        flags.append("whole_hour_offsets")
    if v["wrong_date"] and not (v["above_upper"] or v["at_upper"] or v["below_lower"]):
        return "date_filter_mismatch", flags
    if v["at_upper"] and not v["above_upper"] and not v["below_lower"]:
        if any(x["fractional_seconds"] for x in samples if x["kind"] == "at_upper"):
            return "timestamp_precision", flags
        return "cursor_boundary_inclusive", flags          # exclusive older_than treated as inclusive
    if "whole_hour_offsets" in flags:
        return "timezone_offset_suspected", flags
    if v["above_upper"] and not v["below_lower"]:
        if offs and max(offs) < 1.0:
            return "timestamp_precision", flags
        if prev_newest is not None and page_max is not None and page_max >= prev_newest:
            return "upper_bound_ignored", flags            # reaches back above rows this walk already stored
        return "upper_bound_violated", flags
    if v["below_lower"] and not (v["above_upper"] or v["at_upper"]):
        return "lower_bound_ignored", flags
    return "mixed_bounds_violation", flags


def _inspect_page(spec, data: list, ticker: str, session_date: date, lo: datetime, cursor: datetime) -> dict:
    """Validates a WHOLE page without storing anything: the accepted rows and
    times, or the rejection reason plus sanitized facts about the page."""
    rows, times, all_times, samples = [], [], [], []
    v = {"below_lower": 0, "at_upper": 0, "above_upper": 0, "wrong_date": 0, "unparseable": 0,
         "other_ticker": 0, "out_of_order": 0}
    reason, prev_ts = None, None
    for idx, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        own = raw.get("ticker") or raw.get("ticker_symbol")
        if own and str(own).upper() != ticker.upper():
            v["other_ticker"] += 1
            continue
        raw_ts = raw.get(spec.time_field)
        ts = _parse(raw_ts)
        if ts is None:
            v["unparseable"] += 1
            continue
        all_times.append(ts)
        if prev_ts is not None and ts > prev_ts:
            v["out_of_order"] += 1
        prev_ts = ts
        bad = None
        if ts < lo:
            bad = "below_lower"
        elif not spec.in_bounds(ts, lo, cursor):
            bad = "at_upper" if ts == cursor else "above_upper"
        if bad:
            reason = reason or "bounds_not_honored"
        elif spec.date_scoped and ts.astimezone(tc.NY_TZ).date() != session_date:
            bad = "wrong_date"
            reason = reason or "wrong_session_date"
        if bad:
            v[bad] += 1
            if len(samples) < DIAGNOSTIC_SAMPLE_ROWS:
                bound = lo if bad == "below_lower" else cursor
                samples.append({"index": idx, "raw": str(raw_ts)[:40], "utc": _iso(ts),
                                "ny_date": ts.astimezone(tc.NY_TZ).date().isoformat(), "kind": bad,
                                "delta_s": round((ts - bound).total_seconds(), 6),
                                "fractional_seconds": "." in str(raw_ts)})
            continue
        rows.append(spec.normalize(raw))
        times.append(ts)
    return {"rows": rows, "times": times, "all_times": all_times, "reason": reason, "violations": v,
            "samples": samples}


def _fetch_window(spec, client, catalog, ticker, session_date, lo, hi, budget, kind,
                  on_diagnostic=None, retries: Optional[int] = None) -> dict:
    """Pages newest-first inside [lo, hi]. Returns
       {"outcome": "exhausted" | "budget" | "unverifiable" | "failed" | "cutoff",
        "oldest": earliest row time seen, "pages", "rows", "reason", "status",
        "bounds_retries", "bounds_recovered"}.
    A rejected page is never stored; the same window is re-requested at most
    `retries` times (BOUNDS_RETRIES_PER_WINDOW), each against `budget`."""
    cursor, pages, n_rows, oldest, prev_oldest = hi, 0, 0, None, None
    retries_left = BOUNDS_RETRIES_PER_WINDOW if retries is None else max(0, retries)
    retried = recovered = 0
    attempt, window_page, newest_seen = 0, 0, None

    def done(outcome, reason, status=COLLECTION_OK):
        return {"outcome": outcome, "reason": reason, "pages": pages, "rows": n_rows, "oldest": oldest,
                "status": status, "bounds_retries": retried, "bounds_recovered": recovered}

    while pages < budget:
        body = spec.fetch(client, ticker, session_date, lo, cursor)
        pages += 1
        failure = LiveUWAdapter._failure_status(body)
        if failure:
            status, detail, code = failure
            detail_l = (detail or "").lower()
            if status == "unavailable_on_tier" and "historic" in detail_l:
                return done("cutoff", "provider_history_cutoff")
            if status in ("unavailable_on_tier", "unauthorized"):
                return done("cutoff", "not_entitled", status)
            return done("failed", detail, status)
        data = body.get("data")
        if not isinstance(data, list):
            return done("failed", "data is not a list", "malformed")
        page = _inspect_page(spec, data, ticker, session_date, lo, cursor)
        if page["reason"]:
            will_retry = retries_left > 0 and pages < budget
            if on_diagnostic is not None:
                at = page["all_times"]
                diag = {
                    "dataset": spec.name, "ticker": ticker, "session_date": session_date.isoformat(),
                    "reason": page["reason"], "endpoint": spec.endpoint(ticker),
                    "request": spec.request_params(ticker, session_date, lo, cursor),
                    "expected": {"lower": _iso(lo), "lower_inclusive": True, "upper": _iso(cursor),
                                 "upper_inclusive": spec.upper_inclusive, "window_upper": _iso(hi),
                                 "session_date_ny": session_date.isoformat() if spec.date_scoped else None},
                    "page_in_window": window_page + 1, "attempt": attempt + 1, "response_rows": len(data),
                    "page_limit": spec.page_limit, "provider_status": "http_200",
                    "observed": {"min": _iso(min(at)) if at else None, "max": _iso(max(at)) if at else None,
                                 "parsed_rows": len(at)},
                    "previous_page": {"oldest": _iso(prev_oldest) if prev_oldest else None,
                                      "newest_seen": _iso(newest_seen) if newest_seen else None},
                    "violations": page["violations"], "samples": page["samples"],
                    "action": "retry_same_window" if will_retry else "session_left_unknown",
                    "retries_left_after": retries_left - 1 if will_retry else retries_left,
                }
                diag["classification"], diag["flags"] = _classify_violation(
                    page["violations"], page["samples"], newest_seen, max(at) if at else None)
                on_diagnostic(diag)
            if will_retry:
                retries_left -= 1
                retried += 1
                attempt += 1
                if BOUNDS_RETRY_DELAY_S:
                    _sleep(BOUNDS_RETRY_DELAY_S)
                continue                     # same lo/cursor; nothing from the rejected page was stored
            return done("unverifiable", page["reason"])
        if attempt:
            recovered += 1
        attempt = 0
        window_page += 1
        rows, times = page["rows"], page["times"]
        if rows:
            try:
                spec.catalog(catalog, ticker, rows, kind)
            except Exception:  # noqa: BLE001 -- storage guard / write failure: cannot prove coverage
                return done("unverifiable", "catalog_write_failed")
            n_rows += len(rows)
            page_oldest = min(times)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            newest_seen = max(times) if newest_seen is None else max(newest_seen, max(times))
        if len(data) < spec.page_limit:
            # verified: a short (or empty) page exhausts the bounded window
            return done("exhausted", None)
        page_oldest = min(times) if times else None
        if page_oldest is None or (prev_oldest is not None and page_oldest >= prev_oldest):
            return done("unverifiable", "cursor_stuck")
        prev_oldest = page_oldest
        cursor = page_oldest + spec.cursor_step
        if cursor >= hi and pages > 1:  # cannot make progress inside this window
            return done("unverifiable", "cursor_stuck")
    return done("budget", "page_budget_exhausted")


def _diagnostic_sink(catalog, kind: str):
    """Per collection walk: logs one sanitized line per rejected page and
    persists the diagnostic (best effort -- never changes the outcome)."""
    walk_id = uuid.uuid4().hex[:12]

    def sink(diag: dict) -> None:
        diag = {**diag, "walk_id": walk_id, "collection_kind": kind}
        _log.warning("collector rejected an out-of-window page: %s %s %s reason=%s class=%s page=%s attempt=%s "
                     "action=%s observed=%s..%s requested=[%s, %s%s", diag["dataset"], diag["ticker"],
                     diag["session_date"], diag["reason"], diag["classification"], diag["page_in_window"],
                     diag["attempt"], diag["action"], diag["observed"]["min"], diag["observed"]["max"],
                     diag["expected"]["lower"], diag["expected"]["upper"],
                     "]" if diag["expected"]["upper_inclusive"] else ")")
        record = getattr(catalog, "record_collection_diagnostic", None)
        if record is None:
            return
        try:
            record(diag, kind=kind)
        except Exception:  # noqa: BLE001 -- diagnostics must never change a collection outcome
            _log.info("collection diagnostic not persisted", exc_info=True)
    return sink


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
    sink = _diagnostic_sink(catalog, kind)
    retry_stats = {"bounds_retries": 0, "bounds_recovered": 0}

    def counted(w):
        retry_stats["bounds_retries"] += w.get("bounds_retries", 0)
        retry_stats["bounds_recovered"] += w.get("bounds_recovered", 0)
        return w

    def result(*a, **k):
        return CoverageUpdate(*a, **k, **retry_stats)

    def store(cov_status, frm, thr, why):
        catalog.upsert_session_coverage(dataset, ticker, sd, cov_status, frm.isoformat() if frm else None,
                                        thr.isoformat() if thr else None, pages, rows, why, kind=kind)

    # 1) newest gap: (c_through, end], or the whole [open, end] if nothing usable yet
    top_lo = c_through if c_through is not None else open_utc
    if c_through is None or c_through < end:
        w = counted(_fetch_window(spec, client, catalog, ticker, session_date, top_lo, end, budget, kind, sink))
        pages += w["pages"]; rows += w["rows"]; budget -= w["pages"]
        if w["outcome"] == "failed":
            return result(dataset, ticker, sd, w["status"], cov or None, pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        if w["outcome"] == "cutoff":
            store("unavailable", None, None, w["reason"])
            return result(dataset, ticker, sd, w["status"], catalog.get_session_coverage(dataset, ticker, sd),
                          pages=pages, rows_fetched=rows, detail=w["reason"])
        if w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return result(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
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
        w = counted(_fetch_window(spec, client, catalog, ticker, session_date, open_utc, c_from, budget, kind, sink))
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
            return result(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                          pages=pages, rows_fetched=rows, detail=w["reason"])
        else:  # cutoff
            store("unavailable", None, None, w["reason"])
            return result(dataset, ticker, sd, w["status"], catalog.get_session_coverage(dataset, ticker, sd),
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
    return result(dataset, ticker, sd, status, catalog.get_session_coverage(dataset, ticker, sd),
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
                        "coverage": cov.get("status"), "reason": cov.get("last_error"), "pages": u.pages,
                        "bounds_retries": u.bounds_retries, "bounds_recovered": u.bounds_recovered})
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
