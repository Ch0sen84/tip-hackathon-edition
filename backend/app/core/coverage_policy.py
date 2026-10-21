"""Which session-coverage rows may be used as baseline history (Release 3).

A session_coverage row with status 'complete' says that [open, close] was walked
without a gap. Since migration 11 that alone is NOT enough to use the session as
history: the row must also be CERTIFIED by this release's rules (`certified_by`
in ACCEPTED_CERTIFIERS), which means

  * settlement: the whole [open, close] was walked at or after
    close + SETTLE_LAG, so prints reported late (after the in-session walk had
    passed their timestamp) are included; a walk made while the session was
    still trading can never certify it;
  * gap check: no implausibly long interval without a row for this session's
    own activity rate, or the same gap reproduced by a second, independent
    post-settlement walk (gap_flag 'confirmed').

Every 'complete' row written by an earlier release has certified_by = NULL. It
is reported as legacy/uncertified, is never counted towards the 20-session
requirement, and is re-walked from scratch (never modified in place) the next
time an approved baseline job or backfill reaches it.
"""
from __future__ import annotations

import os
from datetime import timedelta

CERTIFIER = "settled-v1"
ACCEPTED_CERTIFIERS = frozenset({CERTIFIER})


def _env_minutes(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


# Late-report allowance after the regular-session close. Not a verified provider
# contract (no live measurement of UW reporting latency was authorized); a
# conservative operating choice. Prints reported after it are only caught by an
# explicit reconciliation walk.
SETTLE_LAG = timedelta(minutes=_env_minutes("TIP_COVERAGE_SETTLE_MINUTES", 180, 30, 1440))

# Gap check (C2-1): an interval without any row is "implausible" when it is at least GAP_MIN long AND the
# session's rate OUTSIDE its long gaps predicts at least GAP_EXPECTED_ROWS rows in it -- for one gap, or
# summed over all long gaps (several medium outages). P(no row) ~ exp(-25) under a Poisson model, still
# negligible allowing for intraday seasonality (a midday lull is a few times quieter than the average).
# The rate excludes the gaps themselves, so an outage cannot hide itself by lowering the average.
GAP_MIN = timedelta(minutes=_env_minutes("TIP_COVERAGE_GAP_MIN_MINUTES", 5, 2, 390))
GAP_EXPECTED_ROWS = 25.0
GAP_MIN_ROWS = 50                     # a rate needs at least this many rows behind it
GAP_MIN_OBSERVED_S = 3600.0           # ... observed over at least an hour outside the tested gaps
# C2-2: a suspected gap is confirmed (certified as genuine) only by a walk at least this long after the
# walk that suspected it, so a provider backfill has time to arrive -- never by an immediate re-click.
GAP_CONFIRM_AFTER = timedelta(hours=_env_minutes("TIP_COVERAGE_GAP_CONFIRM_HOURS", 12, 1, 72))
# Baseline plausibility (core/session_baseline): a certified session with ZERO rows lying between active
# sessions of a ticker whose active sessions have a median of at least this many rows is excluded from
# the baseline (a provider-side outage or an unscheduled closure), never averaged in as zero. Below the
# threshold (thinly traded tickers) a zero day is a genuine zero.
EMPTY_SESSION_MIN_MEDIAN_ROWS = 20


def is_certified(cov) -> bool:
    return bool(cov) and cov.get("status") == "complete" and cov.get("certified_by") in ACCEPTED_CERTIFIERS


def is_legacy_complete(cov) -> bool:
    return bool(cov) and cov.get("status") == "complete" and cov.get("certified_by") not in ACCEPTED_CERTIFIERS


def status_key(cov) -> str:
    """Summary key used by coverage overviews: 'complete' only for certified rows."""
    if is_legacy_complete(cov):
        return "complete:legacy_uncertified"
    return cov["status"] + (f":{cov['last_error']}" if cov.get("last_error") else "")


def gap_assessment(event_times, open_utc, close_utc) -> tuple:
    """(max_gap_seconds, implausible) for sorted UTC event times inside [open, close]."""
    span = (close_utc - open_utc).total_seconds()
    if span <= 0:
        return 0.0, False
    points = [open_utc] + sorted(event_times) + [close_utc]
    gaps = sorted(((b - a).total_seconds() for a, b in zip(points, points[1:])), reverse=True)
    max_gap = gaps[0]
    if len(event_times) < GAP_MIN_ROWS:
        return max_gap, False          # too few rows to estimate a rate: a thin ticker's outage is undetectable
    # Test the k largest gaps (k = 1..5) against the rate observed OUTSIDE those k gaps: one long outage, or
    # a few medium ones together. Regular sparse trading (every gap similar) never qualifies, because the
    # rate outside k typical gaps predicts about k rows in them.
    implausible = False
    for k in range(1, min(5, len(gaps)) + 1):
        top = gaps[:k]
        if top[-1] < GAP_MIN.total_seconds():
            break
        outside = span - sum(top)
        if outside < GAP_MIN_OBSERVED_S:
            break                      # the rate would be extrapolated from too short a stretch of trading
        rate = len(event_times) / outside
        if rate * sum(top) >= GAP_EXPECTED_ROWS * (1 if k == 1 else 2):
            implausible = True
            break
    return max_gap, implausible


def usable_prior_count(covs, before_iso: str, max_history: int = 60) -> int:
    """C2-5: how many prior sessions the baseline will actually use, by the SAME rules as
    core/session_baseline.compute (certified, trading day, newest `max_history`, none before the first
    activity, implausibly empty sessions of an active ticker excluded) -- from the row counts recorded at
    certification. Job targets and the coverage overview use this, so 'sufficient' means the baseline has it."""
    from . import trading_calendar as tc
    from datetime import date as _date

    cert = [c for c in sorted(covs, key=lambda c: c["session_date"])
            if c["session_date"] < before_iso and is_certified(c)][-max_history:]
    rows = [int(c.get("session_rows") or 0) for c in cert if tc.is_trading_day(_date.fromisoformat(c["session_date"]))]
    first = next((i for i, n in enumerate(rows) if n > 0), len(rows))
    rows = rows[first:]
    active = sorted(n for n in rows if n > 0)
    if active and active[len(active) // 2] >= EMPTY_SESSION_MIN_MEDIAN_ROWS:
        rows = [n for n in rows if n > 0]
    return len(rows)
