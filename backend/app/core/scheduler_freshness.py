"""Phase D: freshness classification for a market-wide Radar candidate,
plus the dedup rule for watchlist-sourced vs discovery-sourced
evaluations of the same ticker.

THREE STATES, defined precisely (exact boundary matters -- see tests):
  * "current"      -- a signal_evaluations row exists for this ticker
                       and its `retrieved_at` age is <= the configured
                       freshness window.
  * "stale"         -- a signal_evaluations row exists but its age is
                       STRICTLY GREATER than the window.
  * "unevaluated"   -- no signal_evaluations row exists for this ticker
                       at all yet (a pure discovery candidate).

WINDOW DEFAULT: 20 minutes (TIP_EVAL_FRESHNESS_WINDOW_S, default 1200).
Justification: the Phase D scheduler's own default scan interval is 15
minutes (see scheduler.py) and UW stock-state/flow-alert data itself
has no sub-minute-granularity guarantee worth chasing for a discovery
candidate (unlike a direct Ticker Analysis lookup, which always forces
a fresh fetch regardless of this window). 20 minutes is comfortably
longer than one scheduler cycle (so a candidate evaluated last cycle
still reads "current" when the next cycle's ranking runs) while still
being short enough that an evaluation from hours ago is correctly
flagged stale rather than presented as if it just happened.

DEDUP: this module does NOT maintain a second store of "the de-duped
evaluation" -- that would be exactly the kind of second observation-
storage mechanism Chad's spec forbids. Instead, both the watchlist-
scan path (app/main.py's existing _run_scan) and the discovery/
scheduler path write into the SAME `signal_evaluations` table via the
SAME app/ingestion.py `IngestionCatalog.catalog_signal_evaluation`
call -- there has never been a second table. "Most recent evaluation
wins regardless of source" is therefore just
`IngestionCatalog.get_latest_signal_evaluation(ticker)`: one query,
`ORDER BY retrieved_at DESC LIMIT 1`, with no source-path distinction
to even mix up. See backend/tests/test_scheduler_freshness.py's
test_dedup_prefers_most_recent_regardless_of_source for the end-to-end
proof (two evaluations, different simulated sources, same ticker, same
day -- exactly one survives as "the" evaluation).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

CURRENT = "current"
STALE = "stale"
UNEVALUATED = "unevaluated"

DEFAULT_FRESHNESS_WINDOW_S = 20 * 60  # 20 minutes -- see module docstring


def freshness_window_s() -> int:
    try:
        return int(os.environ.get("TIP_EVAL_FRESHNESS_WINDOW_S", DEFAULT_FRESHNESS_WINDOW_S))
    except (TypeError, ValueError):
        return DEFAULT_FRESHNESS_WINDOW_S


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class FreshnessResult:
    ticker: str
    state: str  # current | stale | unevaluated
    evaluation: Optional[dict]
    age_s: Optional[float]
    window_s: int


def classify_freshness(latest_evaluation: Optional[dict], ticker: str,
                        now: Optional[datetime] = None, window_s: Optional[int] = None) -> FreshnessResult:
    """Pure function: given the single most-recent signal_evaluations row
    already read for `ticker` (or None), decide current/stale/
    unevaluated. `now`/`window_s` are injectable for exact-boundary
    tests; default to the real clock and the configured env window."""
    now = now or datetime.now(timezone.utc)
    window_s = freshness_window_s() if window_s is None else window_s
    if latest_evaluation is None:
        return FreshnessResult(ticker=ticker, state=UNEVALUATED, evaluation=None, age_s=None, window_s=window_s)
    retrieved_at = latest_evaluation.get("retrieved_at")
    if not retrieved_at:
        return FreshnessResult(ticker=ticker, state=UNEVALUATED, evaluation=None, age_s=None, window_s=window_s)
    try:
        ts = _parse_iso(retrieved_at)
    except ValueError:
        return FreshnessResult(ticker=ticker, state=UNEVALUATED, evaluation=None, age_s=None, window_s=window_s)
    age_s = (now - ts).total_seconds()
    # Exact boundary: age_s == window_s is still CURRENT (<=), one tick
    # over is STALE (>) -- see test_scheduler_freshness.py's boundary
    # tests for both sides of this exact inequality.
    state = CURRENT if age_s <= window_s else STALE
    return FreshnessResult(ticker=ticker, state=state, evaluation=latest_evaluation, age_s=age_s, window_s=window_s)


def classify_candidate(catalog, ticker: str, now: Optional[datetime] = None,
                        window_s: Optional[int] = None) -> FreshnessResult:
    """Convenience wrapper: reads the real latest evaluation for `ticker`
    from the shared IngestionCatalog (the ONE place both the watchlist
    and discovery paths write to) and classifies it. This is the single
    function the Market-wide Radar route calls per candidate."""
    latest = catalog.get_latest_signal_evaluation(ticker)
    return classify_freshness(latest, ticker, now=now, window_s=window_s)
