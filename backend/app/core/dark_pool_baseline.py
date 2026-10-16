"""Phase C: genuine historical dark-pool baseline methodology.

Operates PURELY on data already sitting in the Phase B catalog tables
(`darkpool_observations`, `darkpool_coverage_status`) -- this module never
fetches anything itself, never starts a backfill, and makes zero live UW
calls. Callers (see `compute_baseline_for_ticker`) hand it rows they
already read out of those tables; the pure-function core
(`compute_baseline`) doesn't even know SQLite exists.

THE TWO HARD REQUIREMENTS THIS MODULE EXISTS TO GET RIGHT (Chad's stated
priority, quoted in the phase brief):

  1. "Session counts alone must never guarantee an anomaly classification."
     A tier (PRELIMINARY/BASELINE_READY/STRONG) is necessary but never
     sufficient. The actual comparison is TIME-OF-DAY cumulative activity:
     today's dark-pool premium accumulated from session open up to the
     CURRENT elapsed time since open is compared against the SAME elapsed-
     time cumulative slice of every eligible historical session -- never
     whole-day totals against whole-day totals. See `cumulative_through_elapsed`
     and `compute_baseline` below; see `tests/test_dark_pool_baseline.py`'s
     `TestTimeOfDayVsWholeDay` for the test that would fail if someone
     sloppily compared whole-day sums instead.

  2. "Incomplete coverage cannot qualify." A session whose
     `darkpool_coverage_status.status` is anything other than exactly
     'complete' (i.e. 'incomplete' or 'unknown') is EXCLUDED from the
     eligible baseline set structurally, before any statistic is computed
     -- see `eligible_sessions`, the ONLY function in this module allowed
     to build that set. Separately, TODAY's own partial-session data must
     itself come from a session that is either already 'complete' or
     currently, genuinely in progress (never 'incomplete'/'unknown') --
     see `_today_session_valid`. Both checks feed into
     `BaselineResult.anomaly_qualifies`, and `as_gate_inputs()` additionally
     sets `tier1_bootstrap=True` whenever either check fails, which reuses
     signal_engine.evaluate_gates' EXISTING bootstrap semantics
     (`passed = tier1_hit and not tier1_bootstrap`) to make this hold with
     ZERO changes to signal_engine.py itself.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional, Sequence

from . import trading_calendar as tc
from ..dark_pool_pagination import COMPLETE, INCOMPLETE, UNKNOWN

# --- tiers -------------------------------------------------------------------

TIER_PRELIMINARY = "preliminary"       # < 20 eligible (complete) sessions
TIER_BASELINE_READY = "baseline_ready"  # 20-59 eligible sessions
TIER_STRONG = "strong"                  # >= 60 eligible sessions

BASELINE_READY_MIN = 20
STRONG_MIN = 60

# The statistical test: BOTH must hold (ratio-vs-mean AND percentile rank)
# -- matching the general "ratio + rarity" shape production TIP's own
# baseline check uses (per this session's architecture report), reimplemented
# independently here against this repo's own catalog schema, not copied.
RATIO_THRESHOLD = 3.0       # mirrors scan_service.DARK_POOL_RATIO_HIT
PERCENTILE_THRESHOLD = 0.95  # today's time-of-day cumulative must be >= 95th pct of history


def classify_tier(eligible_session_count: int) -> str:
    """Session count -> tier. This function ALONE decides the tier; it
    never also decides whether an anomaly qualifies (see
    `BaselineResult.anomaly_qualifies`, which additionally requires the
    time-of-day statistical test to pass -- count is necessary, never
    sufficient)."""
    if eligible_session_count >= STRONG_MIN:
        return TIER_STRONG
    if eligible_session_count >= BASELINE_READY_MIN:
        return TIER_BASELINE_READY
    return TIER_PRELIMINARY


def eligible_sessions(coverage_rows: Sequence[dict]) -> list:
    """THE ONLY function in this module allowed to build the eligible
    baseline set. Structurally excludes anything whose status isn't
    EXACTLY 'complete' -- 'incomplete' and 'unknown' rows are dropped here,
    before any caller ever sees them, so there is no later code path that
    could accidentally include them in a statistic. `compute_baseline`
    below calls this and nothing else to build its working set."""
    return [row for row in coverage_rows if row.get("status") == COMPLETE]


# --- time-of-day cumulative comparison (the core correctness property) -----

def _parse_event_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def cumulative_through_elapsed(
    observations: Sequence[dict], session_open: datetime, elapsed: timedelta,
    value_key: str = "premium",
) -> float:
    """Sums `value_key` over every observation whose event_time falls
    between `session_open` and `session_open + elapsed` (inclusive) --
    i.e. the SAME elapsed-time-since-open window on whatever session
    `observations` belongs to. This is the one function that makes the
    "time-of-day, not whole-day" requirement real: callers MUST pass the
    same `elapsed` for today and for every historical session, derived
    from the real `session_open_et`/`session_start_utc` (trading_calendar),
    never a hardcoded wall-clock hour."""
    if elapsed < timedelta(0):
        elapsed = timedelta(0)
    cutoff = session_open + elapsed
    total = 0.0
    for obs in observations:
        ts = _parse_event_time(obs.get("event_time"))
        if ts is None:
            continue
        if session_open <= ts <= cutoff:
            total += float(obs.get(value_key) or 0)
    return total


def percentile_rank(historical_values: Sequence[float], value: float) -> Optional[float]:
    """Fraction of historical values <= `value` -- 1.0 means today's
    time-of-day cumulative is at or above every eligible historical
    session's same-elapsed-time cumulative; 0.0 means it's at or below
    all of them. None when there is no historical data at all (can't rank
    against an empty set)."""
    if not historical_values:
        return None
    n = len(historical_values)
    at_or_below = sum(1 for v in historical_values if v <= value)
    return at_or_below / n


def _today_session_valid(
    today_coverage_status: Optional[str],
    today_open: Optional[datetime], today_close: Optional[datetime],
    as_of_utc: datetime,
) -> bool:
    """Today's own data must be attributable to a session that is either
    already verified COMPLETE, or genuinely still in progress right now
    (within real session hours, with no coverage_status row recorded for
    it yet at all) -- never a session flagged INCOMPLETE or UNKNOWN,
    regardless of how extreme today's raw numbers look."""
    if today_coverage_status == COMPLETE:
        return True
    if today_coverage_status in (INCOMPLETE, UNKNOWN):
        return False
    # No coverage_status row for today at all. Only "in progress" -- i.e.
    # genuinely inside today's real trading session right now -- counts;
    # a date with no row simply because it isn't a trading day at all, or
    # because `as_of_utc` is before/after the session, is NOT valid.
    if today_open is None or today_close is None:
        return False
    return today_open <= as_of_utc <= today_close


@dataclass(frozen=True)
class BaselineResult:
    ticker: str
    as_of_utc: datetime
    tier: str
    eligible_session_count: int
    today_coverage_status: Optional[str]
    today_session_valid: bool
    today_cumulative: float
    historical_cumulatives: tuple = field(default_factory=tuple)
    historical_mean: Optional[float] = None
    historical_stdev: Optional[float] = None
    ratio_vs_mean: Optional[float] = None
    z_score: Optional[float] = None
    percentile: Optional[float] = None
    anomaly_qualifies: bool = False
    reason: str = ""

    def as_gate_inputs(self) -> dict:
        """Shapes this result into the EXACT keyword-argument shape
        `backend/app/core/signal_engine.evaluate_gates` already expects
        for its dark_pool_anomaly gate (`tier1_hit`, `tier1_bootstrap`,
        `tier1_ratio`, `tier1_z`) -- the same shape
        `scan_service._darkpool_metrics` has always produced. Nothing in
        signal_engine.py changes; this is the "supply a real value through
        the existing interface" integration point.

        `tier1_bootstrap=True` whenever either hard requirement fails
        (insufficient tier OR today's session not valid) -- deliberately
        REDUNDANT with `anomaly_qualifies` already being False in that
        case (so `tier1_hit` would be False too), because
        evaluate_gates' `data_quality` gate and classify_confidence's
        "bootstrap-only, insufficient history" low-confidence branch both
        key off `tier1_bootstrap` specifically, not just `tier1_hit`."""
        structurally_insufficient = self.tier == TIER_PRELIMINARY or not self.today_session_valid
        return {
            "tier1_hit": bool(self.anomaly_qualifies),
            "tier1_bootstrap": bool(structurally_insufficient),
            "tier1_ratio": self.ratio_vs_mean,
            "tier1_z": self.z_score,
            "total_premium": self.today_cumulative,
        }


def compute_baseline(
    ticker: str,
    today_date: date_cls,
    as_of_utc: datetime,
    today_coverage_status: Optional[str],
    today_observations: Sequence[dict],
    historical_coverage_rows: Sequence[dict],
    historical_observations_by_date: dict,
    value_key: str = "premium",
) -> BaselineResult:
    """Pure function, no DB/network access. All the data this needs is
    passed in by the caller (see `compute_baseline_for_ticker` for the
    thin IO-aware wrapper that actually reads the catalog tables).

      * `historical_coverage_rows`: darkpool_coverage_status rows for this
        ticker, EXCLUDING today's own row (any status is fine here --
        filtering to COMPLETE-only happens inside via `eligible_sessions`,
        the one function allowed to do that).
      * `historical_observations_by_date`: {"YYYY-MM-DD": [observation, ...]}
        for every date referenced above, each observation a dict with at
        least "event_time" (ISO8601) and `value_key` (e.g. "premium").
    """
    eligible = eligible_sessions(historical_coverage_rows)
    tier = classify_tier(len(eligible))

    today_info = tc.session_info(today_date)
    today_open = today_info.session_open_et.astimezone(timezone.utc) if today_info.is_trading_day else None
    today_close = today_info.session_close_et.astimezone(timezone.utc) if today_info.is_trading_day else None
    today_valid = _today_session_valid(today_coverage_status, today_open, today_close, as_of_utc)

    if today_open is None:
        elapsed = timedelta(0)
        today_cumulative = 0.0
    else:
        elapsed = max(as_of_utc - today_open, timedelta(0))
        today_cumulative = cumulative_through_elapsed(today_observations, today_open, elapsed, value_key)

    historical_cumulatives = []
    for row in eligible:
        coverage_date = row.get("coverage_date")
        if not coverage_date:
            continue
        hist_date = date_cls.fromisoformat(coverage_date)
        hist_info = tc.session_info(hist_date)
        if not hist_info.is_trading_day:
            # Defensive only -- a row marked COMPLETE should always be a
            # real trading day, but never silently include a non-trading
            # date's observations in the baseline if this ever happens.
            continue
        hist_open = hist_info.session_open_et.astimezone(timezone.utc)
        obs = historical_observations_by_date.get(coverage_date, [])
        historical_cumulatives.append(cumulative_through_elapsed(obs, hist_open, elapsed, value_key))

    mean = statistics.mean(historical_cumulatives) if historical_cumulatives else None
    stdev = statistics.pstdev(historical_cumulatives) if len(historical_cumulatives) >= 2 else 0.0
    ratio = (today_cumulative / mean) if (mean is not None and mean > 0) else None
    z = ((today_cumulative - mean) / stdev) if (mean is not None and stdev > 0) else None
    pct = percentile_rank(historical_cumulatives, today_cumulative)

    tier_qualifies = tier != TIER_PRELIMINARY
    stats_qualify = (
        ratio is not None and ratio >= RATIO_THRESHOLD
        and pct is not None and pct >= PERCENTILE_THRESHOLD
    )
    anomaly_qualifies = bool(tier_qualifies and today_valid and stats_qualify)

    if not tier_qualifies:
        reason = f"PRELIMINARY tier ({len(eligible)} eligible complete sessions, need >= {BASELINE_READY_MIN}) -- descriptive only, cannot gate anything"
    elif not today_valid:
        reason = f"today's own coverage status ({today_coverage_status!r}) is not complete/in-progress -- cannot qualify regardless of magnitude"
    elif not stats_qualify:
        reason = "time-of-day cumulative did not clear both the ratio-vs-mean and percentile-rank thresholds"
    else:
        reason = (f"{tier} tier, time-of-day cumulative ratio={ratio:.2f}x mean, "
                  f"percentile={pct:.3f} -- qualifies")

    return BaselineResult(
        ticker=ticker, as_of_utc=as_of_utc, tier=tier,
        eligible_session_count=len(eligible),
        today_coverage_status=today_coverage_status, today_session_valid=today_valid,
        today_cumulative=today_cumulative, historical_cumulatives=tuple(historical_cumulatives),
        historical_mean=mean, historical_stdev=stdev, ratio_vs_mean=ratio, z_score=z,
        percentile=pct, anomaly_qualifies=anomaly_qualifies, reason=reason,
    )


# --- IO-aware convenience wrapper (reads the catalog, no network calls) ----

def compute_baseline_for_ticker(catalog, ticker: str, as_of_utc: Optional[datetime] = None,
                                 value_key: str = "premium") -> BaselineResult:
    """Thin wrapper: reads already-cataloged `darkpool_coverage_status` /
    `darkpool_observations` rows for `ticker` off `catalog` (an
    `IngestionCatalog`, see app/ingestion.py's Phase C read methods) and
    feeds them to the pure `compute_baseline` above. Never fetches
    anything from UW -- `catalog` is a local SQLite read, nothing else."""
    as_of_utc = as_of_utc or datetime.now(timezone.utc)
    today_date = as_of_utc.astimezone(tc.NY_TZ).date()
    today_str = today_date.isoformat()

    coverage_rows = catalog.list_coverage_status(ticker)
    today_coverage_status = None
    historical_coverage_rows = []
    for row in coverage_rows:
        if row.get("coverage_date") == today_str:
            today_coverage_status = row.get("status")
        else:
            historical_coverage_rows.append(row)

    observations_by_date = catalog.list_darkpool_observations_by_date(ticker)
    today_observations = observations_by_date.get(today_str, [])

    return compute_baseline(
        ticker=ticker, today_date=today_date, as_of_utc=as_of_utc,
        today_coverage_status=today_coverage_status, today_observations=today_observations,
        historical_coverage_rows=historical_coverage_rows,
        historical_observations_by_date=observations_by_date,
        value_key=value_key,
    )
