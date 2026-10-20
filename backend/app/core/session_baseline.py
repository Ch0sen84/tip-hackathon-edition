"""Session-aware historical baseline for one dataset ('darkpool' premium or
'options_flow' alert premium), computed only from VERIFIED coverage.

Signal-integrity remediation (audit B1/B8/B18). Differences from the Phase C
`dark_pool_baseline.compute_baseline_for_ticker` it supersedes at runtime:

  * Today's (the evidence session's) value is only valid when coverage of
    that session is verified contiguous from the regular-session OPEN
    (ingestion.session_coverage). Phase C treated "no coverage row at all,
    but inside session hours" as valid, which let a partial single page be
    compared against full history.
  * The comparison window ends at the verified covered point
    (min(covered_through, session close)), not at wall-clock "now", and every
    historical session is cut at the same elapsed time OR its own close
    (early-close days), regular session only -- after-hours prints are not
    compared against regular-hours history.
  * No look-ahead: only COMPLETE sessions strictly BEFORE the evidence
    session are eligible (the Phase C wrapper also used later dates when
    replaying history), at most MAX_HISTORY_SESSIONS most recent.
  * Observations are grouped by NYSE session window (not UTC calendar date),
    dark-pool prints are deduplicated by UW tracking_id and canceled prints
    excluded (ingestion.observations_between).

Statistical test (unchanged from Phase C, not tuned on outcomes): the
evidence session's cumulative value must be >= RATIO_THRESHOLD x the
historical mean AND at or above the PERCENTILE_THRESHOLD rank of the
historical same-elapsed cumulatives, with >= BASELINE_READY_MIN eligible
sessions. Fewer sessions, or unverified coverage, is "insufficient" -- never
a pass and never a fail.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import trading_calendar as tc
from .dark_pool_baseline import (
    BASELINE_READY_MIN, PERCENTILE_THRESHOLD, RATIO_THRESHOLD, STRONG_MIN, classify_tier, percentile_rank,
    TIER_PRELIMINARY,
)

MAX_HISTORY_SESSIONS = 60
VALUE_KEYS = {"darkpool": "premium", "options_flow": "total_premium"}


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class SessionBaseline:
    dataset: str
    ticker: str
    evidence_session: str
    status: str                      # pass | fail | insufficient
    reason: str
    tier: str
    eligible_sessions: int
    window_start_utc: Optional[str] = None
    window_end_utc: Optional[str] = None
    elapsed_minutes: Optional[float] = None
    value: Optional[float] = None
    observations: int = 0
    historical_mean: Optional[float] = None
    historical_stdev: Optional[float] = None
    ratio_vs_mean: Optional[float] = None
    z_score: Optional[float] = None
    percentile: Optional[float] = None
    history_sessions: list = field(default_factory=list)
    evidence_coverage: Optional[dict] = None
    coverage_summary: dict = field(default_factory=dict)
    excluded_before_first_activity: int = 0

    def as_metrics(self) -> dict:
        return {
            "method": "time_of_day_cumulative_vs_prior_complete_sessions",
            "dataset": self.dataset, "evidence_session": self.evidence_session,
            "status": self.status, "reason": self.reason, "tier": self.tier,
            "eligible_sessions": self.eligible_sessions, "required_sessions": BASELINE_READY_MIN,
            "window_start_utc": self.window_start_utc, "window_end_utc": self.window_end_utc,
            "elapsed_minutes": self.elapsed_minutes, "value": self.value, "observations": self.observations,
            "historical_mean": self.historical_mean, "historical_stdev": self.historical_stdev,
            "ratio": self.ratio_vs_mean, "z_score": self.z_score, "percentile": self.percentile,
            "thresholds": {"ratio": RATIO_THRESHOLD, "percentile": PERCENTILE_THRESHOLD},
            "history_sessions": self.history_sessions,
            "evidence_coverage": self.evidence_coverage,
            "coverage_summary": self.coverage_summary,
            "excluded_before_first_activity": self.excluded_before_first_activity,
        }

    def as_dark_pool_gate_inputs(self) -> dict:
        return {"tier1_hit": self.status == "pass", "tier1_bootstrap": self.status == "insufficient",
                "tier1_ratio": self.ratio_vs_mean, "tier1_z": self.z_score,
                "total_premium": self.value or 0.0, "baseline": self.as_metrics()}


def compute(catalog, dataset: str, ticker: str, evidence_session: date,
            as_of_utc: Optional[datetime] = None, min_sessions: int = BASELINE_READY_MIN) -> SessionBaseline:
    """`min_sessions` exists ONLY so tests can exercise the comparison branch
    on short recorded histories; the app always uses BASELINE_READY_MIN."""
    as_of_utc = as_of_utc or datetime.now(timezone.utc)
    value_key = VALUE_KEYS[dataset]
    sd = evidence_session.isoformat()
    info = tc.session_info(evidence_session)
    all_cov = catalog.list_session_coverage(dataset, ticker)
    prior = [c for c in all_cov if c["session_date"] < sd]
    summary: dict = {}
    for c in prior:
        k = c["status"] + (f":{c['last_error']}" if c.get("last_error") else "")
        summary[k] = summary.get(k, 0) + 1
    eligible = [c for c in prior if c["status"] == "complete"]
    eligible = eligible[-MAX_HISTORY_SESSIONS:]
    tier = classify_tier(len(eligible)) if min_sessions == BASELINE_READY_MIN else (
        TIER_PRELIMINARY if len(eligible) < min_sessions else "baseline_ready")
    base = SessionBaseline(dataset, ticker, sd, "insufficient", "", tier, len(eligible))
    base.coverage_summary = summary
    if not info.is_trading_day:
        base.reason = "evidence session is not a trading day"
        return base
    open_utc = info.session_open_et.astimezone(timezone.utc)
    close_utc = info.session_close_et.astimezone(timezone.utc)
    cov = next((c for c in all_cov if c["session_date"] == sd), None)
    base.evidence_coverage = cov
    c_from, c_through = (_parse(cov.get("covered_from_utc")), _parse(cov.get("covered_through_utc"))) if cov else (None, None)
    if not cov or cov["status"] == "unknown" or c_from is None or c_through is None or c_from > open_utc:
        base.reason = ("evidence-session coverage is not verified from the open"
                       + (f" ({cov['status']}: {cov.get('last_error')})" if cov else " (never collected)"))
        return base
    window_end = min(c_through, close_utc, as_of_utc)
    if window_end <= open_utc:
        base.reason = "no regular-session time covered yet"
        return base
    elapsed = window_end - open_utc
    obs = catalog.observations_between(dataset, ticker, open_utc, window_end)
    base.window_start_utc, base.window_end_utc = open_utc.isoformat(), window_end.isoformat()
    base.elapsed_minutes = round(elapsed.total_seconds() / 60, 1)
    base.value = sum(_f(o.get(value_key)) for o in obs)
    base.observations = len(obs)

    hist_rows = []
    for c in eligible:
        hinfo = tc.session_info(date.fromisoformat(c["session_date"]))
        if not hinfo.is_trading_day:
            continue
        h_open = hinfo.session_open_et.astimezone(timezone.utc)
        h_close = hinfo.session_close_et.astimezone(timezone.utc)
        cutoff = min(h_open + elapsed, h_close)
        h_obs = catalog.observations_between(dataset, ticker, h_open, cutoff)
        v = sum(_f(o.get(value_key)) for o in h_obs)
        full_day = catalog.observations_between(dataset, ticker, h_open, h_close) if not h_obs else h_obs
        hist_rows.append({"session_date": c["session_date"], "value": v, "observations": len(h_obs),
                          "active": bool(full_day)})
    # A verified-empty session BEFORE the ticker's first observed activity
    # (e.g. pre-listing) is not a genuine "zero activity" baseline day.
    first_active = next((i for i, r in enumerate(hist_rows) if r["active"]), len(hist_rows))
    base.excluded_before_first_activity = first_active
    hist_rows = hist_rows[first_active:]
    base.history_sessions = [{k: r[k] for k in ("session_date", "value", "observations")} for r in hist_rows]
    hist_values = [r["value"] for r in hist_rows]
    base.eligible_sessions = len(hist_values)
    base.tier = classify_tier(len(hist_values)) if min_sessions == BASELINE_READY_MIN else (
        TIER_PRELIMINARY if len(hist_values) < min_sessions else "baseline_ready")

    if not hist_values or len(hist_values) < min_sessions:
        base.reason = (f"only {len(hist_values)} complete prior sessions (need >= {min_sessions}) -- "
                       "insufficient history, not evidence either way")
        return base
    mean = statistics.mean(hist_values)
    stdev = statistics.pstdev(hist_values) if len(hist_values) >= 2 else 0.0
    base.historical_mean, base.historical_stdev = mean, stdev
    base.ratio_vs_mean = (base.value / mean) if mean > 0 else None
    base.z_score = ((base.value - mean) / stdev) if stdev > 0 else None
    base.percentile = percentile_rank(hist_values, base.value)
    qualifies = (base.ratio_vs_mean is not None and base.ratio_vs_mean >= RATIO_THRESHOLD
                 and base.percentile is not None and base.percentile >= PERCENTILE_THRESHOLD)
    base.status = "pass" if qualifies else "fail"
    base.reason = (f"{base.value:,.0f} over the first {base.elapsed_minutes:g} min vs mean {mean:,.0f} of "
                   f"{len(hist_values)} prior complete sessions (ratio "
                   f"{base.ratio_vs_mean if base.ratio_vs_mean is not None else float('nan'):.2f}x, "
                   f"percentile {base.percentile:.2f})")
    return base
