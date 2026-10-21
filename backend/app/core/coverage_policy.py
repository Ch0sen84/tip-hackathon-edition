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

# Gap check: an interval without any row is "implausible" when it is at least
# GAP_MIN long AND the session's own average rate predicts at least
# GAP_EXPECTED_ROWS rows in it (P(no row) ~ exp(-GAP_EXPECTED_ROWS) under a
# Poisson model, tiny even allowing for intraday seasonality).
GAP_MIN = timedelta(minutes=_env_minutes("TIP_COVERAGE_GAP_MIN_MINUTES", 20, 5, 390))
GAP_EXPECTED_ROWS = 50.0
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
    max_gap = max((b - a).total_seconds() for a, b in zip(points, points[1:]))
    rate = len(event_times) / span
    implausible = max_gap >= GAP_MIN.total_seconds() and rate * max_gap >= GAP_EXPECTED_ROWS
    return max_gap, implausible
