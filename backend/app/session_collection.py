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
  complete     [open, close] provably collected AND certified (core/coverage_policy):
               walked entirely after close + settlement lag, gap check passed
  partial      a contiguous sub-interval only, or a full interval not yet
               certifiable -- reasons: session_in_progress | page_budget_exhausted |
               awaiting_settlement | suspected_provider_gap
  unknown      coverage could not be established -- reasons:
               invalid_rows | bounds_not_honored | wrong_session_date |
               rows_out_of_order | cursor_stuck
  (a LOCAL catalog write failure is not a coverage finding: the collection
   reports status "error" with detail catalog_write_failed and the stored
   coverage is kept unchanged -- PE-P4)
  unavailable  the provider refuses this session -- reasons:
               provider_history_cutoff | not_entitled
Transient provider failures (429/5xx/timeout/malformed) leave stored coverage
untouched and are reported as a failed COLLECTION, never as coverage.

Rejected pages (Phase 9 reliability)
  Every row of a page is validated before ANY row is stored. One invalid row
  rejects the WHOLE page -- nothing from it is stored and a short invalid page
  never proves a window exhausted:
    invalid_rows        a row that is not an object, carries another ticker,
                        or has a missing / unparseable timestamp
    bounds_not_honored  a row outside the requested bounds
    wrong_session_date  (dark pool) a row off the requested NY date
    rows_out_of_order   rows not newest-first (the contract the cursor relies
                        on: a full page must be the NEWEST rows of the window)
  A sanitized diagnostic (request bounds, observed min/max timestamps,
  violation counts, a few offending timestamps -- no prices, sizes, ids, raw
  rows or credentials) is recorded in collection_diagnostics. The SAME window
  is then re-requested at most BOUNDS_RETRIES_PER_WINDOW times; each retry is
  a logical page against the same page budget, and the allowance is per page:
  it resets only when an accepted page advances the cursor. Only a response
  that passes every check is stored and advances the cursor, so a recovered
  walk proves coverage exactly as a clean one does; if every retry for a page
  is rejected the session stays UNKNOWN with that reason.

Resuming an interrupted walk (page budget)
  When a walk stops on its page budget, the proven island starts at the bound
  the NEXT request would have used (`resume_from`), not at the oldest row
  seen: for dark pool (exclusive older_than, second resolution) that is the
  oldest second + 1 s, because prints sharing the oldest second may not all
  have fit on the last page. The resumed backward window [open, resume_from)
  therefore re-reads that second (deduplicated) instead of skipping it. For
  flow (inclusive older_than) resume_from is the oldest instant itself and the
  resumed window [open, resume_from] re-reads it the same way.

Settlement and provenance (Release 3, migration 11; core/coverage_policy.py)
  An interval proven while the session was trading only extends FORWARD from
  its checkpoint, so a print reported late -- after the walk had passed its
  timestamp -- would never be fetched. A session is therefore certified only when
  its whole [open, close] was walked at or after close + SETTLE_LAG
  (`proof_started_utc` = earliest retrieval time of the contributing walks).
  After settlement an earlier interval (walked before settlement, certified by
  an earlier release, or withheld for a suspected gap) is not extended: it is
  re-walked from scratch (rows are deduplicated; nothing is deleted or edited)
  and the completed re-walk is logged in coverage_reconciliations with the
  number of rows the earlier collection had missed. A full interval whose
  longest row-free gap is implausible for the session's own activity rate is
  withheld once (`suspected_provider_gap`) and certified only if a second,
  independent walk reproduces the same gap (gap_flag 'confirmed').
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
from .core import coverage_policy as policy
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
# unknown reasons meaning the provider returned unusable content for the
# session: backfills skip such sessions instead of re-walking them every step
REJECTED_CONTENT_REASONS = ("invalid_rows", "bounds_not_honored", "wrong_session_date", "rows_out_of_order")
REJECTED_RETRY_AFTER = timedelta(hours=24)
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
    if v["non_object"] or v["other_ticker"] or v["unparseable"]:
        return "invalid_rows", flags
    samples = [x for x in samples if "delta_s" in x]
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
    if v["out_of_order"] and not (v["above_upper"] or v["at_upper"] or v["below_lower"] or v["wrong_date"]):
        return "rows_out_of_order", flags
    return "mixed_bounds_violation", flags


def _inspect_page(spec, data: list, ticker: str, session_date: date, lo: datetime, cursor: datetime) -> dict:
    """Validates a WHOLE page without storing anything: the accepted rows and
    times, or the rejection reason plus sanitized facts about the page. Any
    invalid row rejects the page (reason priority: invalid_rows, then
    bounds_not_honored, then wrong_session_date, then rows_out_of_order)."""
    rows, times, all_times, samples = [], [], [], []
    v = {"below_lower": 0, "at_upper": 0, "above_upper": 0, "wrong_date": 0, "unparseable": 0,
         "other_ticker": 0, "non_object": 0, "out_of_order": 0}
    prev_ts = None

    def invalid(idx, kind, value_type):
        v[kind] += 1
        if len(samples) < DIAGNOSTIC_SAMPLE_ROWS:     # position and type only -- never the row content
            samples.append({"index": idx, "kind": kind, "value_type": value_type})

    for idx, raw in enumerate(data):
        if not isinstance(raw, dict):
            invalid(idx, "non_object", type(raw).__name__)
            continue
        own = raw.get("ticker") or raw.get("ticker_symbol")
        if own and str(own).upper() != ticker.upper():
            invalid(idx, "other_ticker", type(own).__name__)
            continue
        raw_ts = raw.get(spec.time_field)
        ts = _parse(raw_ts)
        if ts is None:
            invalid(idx, "unparseable", type(raw_ts).__name__)
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
        if not bad and spec.date_scoped and ts.astimezone(tc.NY_TZ).date() != session_date:
            bad = "wrong_date"
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
    if v["non_object"] or v["other_ticker"] or v["unparseable"]:
        reason = "invalid_rows"
    elif v["below_lower"] or v["at_upper"] or v["above_upper"]:
        reason = "bounds_not_honored"
    elif v["wrong_date"]:
        reason = "wrong_session_date"
    elif v["out_of_order"]:
        reason = "rows_out_of_order"
    else:
        reason = None
    if reason:
        rows, times = [], []                          # nothing from a rejected page may be stored
    return {"rows": rows, "times": times, "all_times": all_times, "reason": reason, "violations": v,
            "samples": samples}


def _fetch_window(spec, client, catalog, ticker, session_date, lo, hi, budget, kind,
                  on_diagnostic=None, retries: Optional[int] = None) -> dict:
    """Pages newest-first inside [lo, hi]. Returns
       {"outcome": "exhausted" | "budget" | "unverifiable" | "failed" | "cutoff",
        "oldest": earliest row time seen, "pages", "rows", "reason", "status",
        "bounds_retries", "bounds_recovered",
        "resume_from": for "budget" only, the upper bound the next request would
                       have used. Dark pool (exclusive, 1 s): every row AT OR ABOVE
                       it is proven. Flow (inclusive): every row ABOVE it is proven;
                       rows exactly at it are re-read by the resumed window}.
    A rejected page is never stored; the same window is re-requested at most
    `retries` times (BOUNDS_RETRIES_PER_WINDOW) PER PAGE, each against `budget`.
    The allowance resets only when an accepted page advances the cursor."""
    cursor, pages, n_rows, oldest, prev_oldest = hi, 0, 0, None, None
    per_page = BOUNDS_RETRIES_PER_WINDOW if retries is None else max(0, retries)
    retries_left = per_page
    retried = recovered = 0
    attempt, window_page, newest_seen = 0, 0, None

    def done(outcome, reason, status=COLLECTION_OK):
        return {"outcome": outcome, "reason": reason, "pages": pages, "rows": n_rows, "oldest": oldest,
                "status": status, "bounds_retries": retried, "bounds_recovered": recovered,
                "resume_from": cursor if outcome == "budget" and window_page else None}

    while pages < budget:
        body = spec.fetch(client, ticker, session_date, lo, cursor)
        pages += 1
        failure = LiveUWAdapter._failure_status(body)
        if failure:
            status, detail, code = failure
            detail_l = (detail or "").lower()
            if status == "unavailable_on_tier" and "historic" in detail_l:
                return done("cutoff", "provider_history_cutoff")
            # 401 / missing key / any other 403: a property of the CREDENTIAL or account at this moment,
            # never of the session -- a failed collection that writes no coverage (key rotations, lapses
            # and WAF blocks must not turn into permanently lost history)
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
                    "action": ("retry_same_window" if will_retry else
                               "left_for_a_later_walk" if retries_left > 0 else "session_left_unknown"),
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
            if retries_left > 0:             # retry allowance unused only because the page budget ran out:
                return done("budget", "page_budget_exhausted")   # not evidence of bad content
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
        retries_left = per_page                     # a new page: its own retry allowance
        if cursor >= hi and pages > 1:  # cannot make progress inside this window
            return done("unverifiable", "cursor_stuck")
    return done("budget", "page_budget_exhausted")


def _diagnostic_sink(catalog, kind: str):
    """Per collection walk: logs one sanitized line per rejected page and
    persists the diagnostic (best effort -- never changes the outcome)."""
    walk_id = uuid.uuid4().hex[:12]

    def sink(diag: dict) -> None:
        diag = {**diag, "walk_id": walk_id, "collection_kind": kind}
        _log.warning("collector rejected a page: %s %s %s reason=%s class=%s page=%s attempt=%s "
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


def _session_times(catalog, dataset, ticker, open_utc, close_utc) -> list:
    return [_parse(o.get("_event_time_utc")) for o in catalog.observations_between(dataset, ticker, open_utc, close_utc)]


def _floor_second(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def update_session_coverage(client, catalog, dataset: str, ticker: str, session_date: date,
                            now_utc: Optional[datetime] = None, max_pages: int = DEFAULT_SCAN_PAGES,
                            kind: str = "on_demand", reverify: Optional[str] = None,
                            preserve_on_incomplete: bool = False) -> CoverageUpdate:
    """Extend proven coverage of one session within `max_pages` requests.
    Never raises on provider behaviour; never claims unproven coverage.

    `reverify` (e.g. "operator") forces a fresh post-settlement walk even of a
    certified session; with `preserve_on_incomplete` the stored coverage is
    only replaced if that walk certifies the session (an unfinished or failed
    reconciliation leaves the existing row untouched)."""
    spec = DATASETS[dataset]
    now_utc = now_utc or datetime.now(timezone.utc)
    sd = session_date.isoformat()
    info = tc.session_info(session_date)
    if not info.is_trading_day:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, None, detail="not a trading session")
    open_utc = info.session_open_et.astimezone(timezone.utc)
    close_utc = info.session_close_et.astimezone(timezone.utc)
    settle_at = close_utc + policy.SETTLE_LAG
    end = min(close_utc, now_utc - REPORTING_LAG)
    if dataset == "darkpool":
        end = _floor_second(end)              # second-resolution endpoint: never send fractional bounds
    if end <= open_utc:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, catalog.get_session_coverage(dataset, ticker, sd),
                              detail="session has not opened yet")

    cov = catalog.get_session_coverage(dataset, ticker, sd) or {}
    if max_pages <= 0:                    # no budget: no request and no coverage write (keeps the checkpoint)
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, cov or None, detail="no page budget")
    if cov.get("status") in ("unknown", "unavailable") and cov.get("last_error") == "provider_history_cutoff":
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, cov, detail=cov.get("last_error"))
    if reverify and now_utc < settle_at:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, cov or None,
                              detail="session not settled yet; reconciliation refused")
    if policy.is_certified(cov) and not reverify:
        return CoverageUpdate(dataset, ticker, sd, COLLECTION_OK, cov, detail="certified")
    usable = cov.get("status") in ("complete", "partial")
    c_from = _parse(cov.get("covered_from_utc")) if usable else None
    c_through = _parse(cov.get("covered_through_utc")) if usable else None
    proof = _parse(cov.get("proof_started_utc")) if usable else None
    i_from = _parse(cov.get("island_from_utc")) if usable else None
    i_through = _parse(cov.get("island_through_utc")) if usable else None
    if i_from is None or i_through is None or c_through is None:
        i_from = i_through = None
    carried = {"reverify_trigger": cov.get("reverify_trigger") if usable else None,
               "rows_before_reverify": cov.get("rows_before_reverify") if usable else None,
               "gap_flag": cov.get("gap_flag") if usable else None}

    # after settlement an interval is only extended if it was itself walked after settlement
    trigger = reverify
    if trigger is None and usable and now_utc >= settle_at:
        if policy.is_legacy_complete(cov):
            trigger = "legacy"
        elif cov.get("last_error") == "suspected_provider_gap":
            trigger = "suspected_gap"
        elif proof is None or proof < settle_at:
            trigger = "unsettled"
    if trigger is not None:
        c_from = c_through = i_from = i_through = None
        proof = now_utc
        carried = {"reverify_trigger": trigger,
                   "rows_before_reverify": len(_session_times(catalog, dataset, ticker, open_utc, close_utc)),
                   "gap_flag": carried["gap_flag"] if trigger in ("suspected_gap", "operator") else None}
    elif proof is None:
        proof = now_utc

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

    def reconciliation(outcome, rows_after=None, detail=None):
        if carried["reverify_trigger"]:
            catalog.record_coverage_reconciliation(
                dataset, ticker, sd, carried["reverify_trigger"], carried["rows_before_reverify"], rows_after,
                outcome, policy.CERTIFIER if outcome == "certified" else None, detail, kind=kind)

    def store(cov_status, frm, thr, why, certification=None):
        if preserve_on_incomplete and cov_status != "complete":
            reconciliation("not_certified_left_unchanged", detail={"status": cov_status, "reason": why})
            return
        keep = cov_status in ("complete", "partial")
        island = keep and cov_status == "partial" and i_from is not None and i_through is not None
        prov = {"proof_started_utc": proof.isoformat() if proof and keep else None,
                "island_from_utc": i_from.isoformat() if island else None,
                "island_through_utc": i_through.isoformat() if island else None,
                **(carried if keep else {}), **(certification or {})}
        catalog.upsert_session_coverage(dataset, ticker, sd, cov_status, frm.isoformat() if frm else None,
                                        thr.isoformat() if thr else None, pages, rows, why, kind=kind,
                                        provenance=prov)

    def current():
        return catalog.get_session_coverage(dataset, ticker, sd)

    # 1) newest gap: (top of proven data, end], or the whole [open, end] if nothing usable yet
    upper = i_through if i_through is not None else c_through
    top_lo = upper if upper is not None else open_utc
    # [open, close] is INCLUSIVE (the baseline reads event_time <= close): at the close, an exclusive
    # upper bound (dark pool) must sit one cursor step above it or prints stamped at the close are lost
    top_hi = end + spec.cursor_step if end >= close_utc else end
    if upper is None or upper < end:
        w = counted(_fetch_window(spec, client, catalog, ticker, session_date, top_lo, top_hi, budget, kind, sink))
        pages += w["pages"]; rows += w["rows"]; budget -= w["pages"]
        if w["outcome"] == "failed":
            if preserve_on_incomplete:
                reconciliation("not_certified_left_unchanged", detail={"status": w["status"], "reason": w["reason"]})
            return result(dataset, ticker, sd, w["status"], cov or None, pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        if w["outcome"] == "cutoff":
            store("unavailable", None, None, w["reason"])
            return result(dataset, ticker, sd, w["status"], current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        if w["outcome"] == "unverifiable" and w["reason"] == "catalog_write_failed":
            return result(dataset, ticker, sd, "error", cov or None, pages=pages, rows_fetched=rows,
                          detail="catalog_write_failed: local storage refused the rows; proven coverage kept")
        if w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return result(dataset, ticker, sd, COLLECTION_OK, current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        if w["outcome"] == "exhausted":
            if c_through is None:
                c_from, c_through = open_utc, end
            elif i_through is not None:
                i_through = end                          # the island now reaches the live edge
            else:
                c_through = end
        else:  # budget: newest island [resume_from, end] (rows stay cataloged; the gap below is filled later)
            resume = w["resume_from"]
            if resume is None:                           # defensive: no accepted page, nothing new proven
                if preserve_on_incomplete:
                    reconciliation("not_certified_left_unchanged", detail={"reason": w["reason"]})
                return result(dataset, ticker, sd, COLLECTION_OK, cov or None, pages=pages, rows_fetched=rows,
                              detail=w["reason"])
            if c_through is None:
                c_from, c_through = resume, end          # nothing proven yet: the island is the interval
            elif i_through is not None and resume <= i_through:
                i_through = end                          # reaches the existing island: it grows upward
            elif i_through is None and resume <= c_through:
                c_through = end                          # reaches the proven interval: contiguous
            else:
                # C-F8: never discard the proven (from-open) interval; remember ONE island above it.
                # An older island is dropped (its rows stay cataloged and are re-read by the gap fill).
                i_from, i_through = resume, end
            reason = w["reason"]

    # 1b) gap between the proven interval and the island: [c_through, i_from)
    if i_from is not None and c_through is not None and budget > 0:
        w = counted(_fetch_window(spec, client, catalog, ticker, session_date, c_through, i_from, budget, kind, sink))
        pages += w["pages"]; rows += w["rows"]; budget -= w["pages"]
        if w["outcome"] == "exhausted":
            c_through, i_from, i_through = i_through, None, None
        elif w["outcome"] == "budget":
            if w["resume_from"] is not None:
                i_from = w["resume_from"] if w["resume_from"] > c_through else i_from
                if i_from <= c_through:
                    c_through, i_from, i_through = i_through, None, None
            reason = w["reason"]
        elif w["outcome"] == "failed" or (w["outcome"] == "unverifiable" and w["reason"] == "catalog_write_failed"):
            status, reason = (w["status"], w["reason"]) if w["outcome"] == "failed" else ("error", w["reason"])
        elif w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return result(dataset, ticker, sd, COLLECTION_OK, current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        else:
            store("unavailable", None, None, w["reason"])
            return result(dataset, ticker, sd, w["status"], current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
    elif i_from is not None:
        reason = reason or "page_budget_exhausted"

    # 2) backward gap: [open, c_from)
    if c_from is not None and c_from > open_utc and budget > 0:
        w = counted(_fetch_window(spec, client, catalog, ticker, session_date, open_utc, c_from, budget, kind, sink))
        pages += w["pages"]; rows += w["rows"]
        if w["outcome"] == "exhausted":
            c_from = open_utc
            reason = None
        elif w["outcome"] == "budget":
            c_from = min(c_from, w["resume_from"]) if w["resume_from"] else c_from
            reason = w["reason"]
        elif w["outcome"] == "failed" or (w["outcome"] == "unverifiable" and w["reason"] == "catalog_write_failed"):
            # keep what was proven so far (a local write failure is not evidence about the provider)
            status, reason = (w["status"], w["reason"]) if w["outcome"] == "failed" else ("error", w["reason"])
        elif w["outcome"] == "unverifiable":
            store("unknown", None, None, w["reason"])
            return result(dataset, ticker, sd, COLLECTION_OK, current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
        else:  # cutoff
            store("unavailable", None, None, w["reason"])
            return result(dataset, ticker, sd, w["status"], current(), pages=pages, rows_fetched=rows,
                          detail=w["reason"])
    elif c_from is not None and c_from > open_utc:
        reason = reason or "page_budget_exhausted"

    certification = None
    if c_from is not None and c_from <= open_utc and c_through is not None and c_through >= close_utc:
        if proof < settle_at:
            cov_status, why = "partial", "awaiting_settlement"
        else:
            times = _session_times(catalog, dataset, ticker, open_utc, close_utc)
            max_gap, implausible = policy.gap_assessment(times, open_utc, close_utc)
            if implausible and carried["gap_flag"] not in ("suspected", "confirmed"):
                cov_status, why = "partial", "suspected_provider_gap"
                reconciliation("suspected_provider_gap", len(times), {"max_gap_s": max_gap})
                carried.update(gap_flag="suspected", reverify_trigger=None, rows_before_reverify=None)
            else:
                cov_status, why = "complete", None
                reconciliation("certified", len(times), {"max_gap_s": max_gap})
                certification = {"certified_by": policy.CERTIFIER, "certified_at_utc": now_utc.isoformat(),
                                 "session_rows": len(times), "max_gap_s": max_gap,
                                 "gap_flag": "confirmed" if implausible else None,
                                 "reverify_trigger": None, "rows_before_reverify": None}
    else:
        cov_status = "partial"
        why = reason or ("session_in_progress" if c_through is not None and c_through < close_utc
                         else "page_budget_exhausted")
    store(cov_status, c_from, c_through, why, certification)
    return result(dataset, ticker, sd, status, current(), pages=pages, rows_fetched=rows, detail=why)


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
        if budget > 0 and not policy.is_certified(cov) and cov.get("status") != "unavailable":
            u, cov = touch(evidence_session)
            if not u.ok:
                return _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results)
    d = evidence_session
    for _ in range(lookback_sessions):
        d = tc.previous_trading_day(d)
        if budget <= 0:
            break
        cov = catalog.get_session_coverage(dataset, ticker, d.isoformat()) or {}
        if policy.is_certified(cov) or cov.get("status") == "unavailable" or (
                cov.get("status") == "unknown" and cov.get("last_error") in REJECTED_CONTENT_REASONS
                and not _rejection_expired(cov, now_utc)):
            if cov.get("status") == "unavailable":
                break  # history cutoff: older sessions are unavailable too
            continue
        u, cov = touch(d)
        if not u.ok or cov.get("status") == "unavailable":
            break
    return _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results)


def _rejection_expired(cov: dict, now_utc: datetime) -> bool:
    """A session left unknown for rejected provider content is skipped by backfills, but only for
    REJECTED_RETRY_AFTER: a transient provider fault must not exclude it forever (C-F7)."""
    at = _parse(cov.get("retrieved_at"))
    return at is None or now_utc - at >= REJECTED_RETRY_AFTER


def _backfill_result(catalog, dataset, ticker, evidence_session, max_pages, budget, results):
    complete = sum(1 for c in catalog.list_session_coverage(dataset, ticker)
                   if policy.is_certified(c) and c["session_date"] < evidence_session.isoformat())
    return {"dataset": dataset, "ticker": ticker, "pages_used": max_pages - max(budget, 0),
            "sessions_touched": results, "complete_prior_sessions": complete}
