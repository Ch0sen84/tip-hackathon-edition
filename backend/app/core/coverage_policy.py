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
requirement, is never touched by evidence collection, and is re-walked from scratch
the next time an approved baseline job or backfill reaches it (a settled re-walk
records its progress in the row; no observation is ever deleted -- see below).
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

# Gap check (C2-1, R3B-1/2, R4-1/2/3). A false positive only delays certification (the session waits
# GAP_CONFIRM_AFTER, then a second settled walk certifies it as "confirmed"); a false negative certifies
# incomplete history. Every gap of at least GAP_MIN empty minutes is judged against the activity right AROUND it
# (GAP_LOCAL_WINDOW minutes on each side; where both sides exist the QUIETER one, so a change of regime -- a busy
# opening, then a quiet day -- is not an outage), never against a whole-session average that the U-shaped
# intraday profile, a lunch lull or a closing cross would distort. Two independent-event models:
#   (a) events: a run of consecutive active minutes is ONE event (a cluster of prints counts once); the local
#       event rate predicts the events the gap should have held;
#   (b) rows, for a ticker saturated around the gap (>= GAP_SATURATED_SHARE of those minutes active): the local
#       row rate, capped per minute against bursts, predicts the rows the gap should have held.
# A model flags the gap when its prediction is >= ln(the session's number of opportunities -- events, or capped
# rows) + GAP_SIGNIFICANCE: under a Poisson model the chance that ANY natural gap of the session reaches that is
# about exp(-GAP_SIGNIFICANCE) (~0.4%), however many gaps the session has. (c) A gap spanning more than half of
# the session with >= GAP_MIN_ROWS rows is implausible outright. A gap whose quieter side holds fewer than
# GAP_MIN_SIDE_EVENTS events is not judged by (a): so thin a context cannot tell an outage from a quiet day. Measured operating characteristics: docs/SIGNAL_INTEGRITY_RULES.md section 4.
GAP_MIN = timedelta(minutes=_env_minutes("TIP_COVERAGE_GAP_MIN_MINUTES", 5, 2, 390))
GAP_LOCAL_WINDOW_MINUTES = 30
GAP_MIN_CONTEXT_MINUTES = 5           # a side with fewer observable minutes gives no local rate
GAP_SATURATED_SHARE = 0.7
GAP_ROW_CAP_PER_MINUTE = 10
GAP_SIGNIFICANCE = 5.5
GAP_MIN_ROWS = 50                     # rule (c)
GAP_MIN_SIDE_EVENTS = 3               # a side with fewer events gives no reliable local rate (no judgement)
GAP_EXPECTED_ROWS = GAP_SIGNIFICANCE  # the switch tests use to disable the check (no_gap_policy sets inf)

# C2-2: a suspected gap is confirmed (certified as genuine) only by a walk at least this long after the
# walk that suspected it, so a provider backfill has time to arrive -- never by an immediate re-click.
GAP_CONFIRM_AFTER = timedelta(hours=_env_minutes("TIP_COVERAGE_GAP_CONFIRM_HOURS", 12, 1, 72))
# Baseline plausibility (core/session_baseline): a certified session with ZERO rows lying between active
# sessions of a ticker whose active sessions have a median of at least this many rows is excluded from
# the baseline (a provider-side outage or an unscheduled closure), never averaged in as zero. Below the
# threshold (thinly traded tickers) a zero day is a genuine zero.
EMPTY_SESSION_MIN_MEDIAN_ROWS = 20
# R5-7: for such an active ticker, a session with fewer rows than this share of its median active session (an
# outage the gap check cannot see, e.g. a near-total loss) is excluded too -- a genuine day that quiet is rare,
# and skipping it only shortens the history; averaging it in as real activity would distort the baseline.
IMPLAUSIBLY_SMALL_SHARE = 0.10
IMPLAUSIBLY_SMALL_MIN_SESSIONS = 5   # a median needs a real history behind it


def is_certified(cov) -> bool:
    return bool(cov) and cov.get("status") == "complete" and cov.get("certified_by") in ACCEPTED_CERTIFIERS


# Legacy rows (R3B-8, documented trade-off): a legacy 'complete' row is never certified, never counted, and
# never touched by evidence collection or by a walk before settlement. A SETTLED re-walk of it by an approved
# job or backfill records its progress in the same row (status partial, reverify_trigger 'legacy') because a
# busy session needs many job steps; no observation is ever deleted, and the row was not counted before
# either. The re-walk's start and its outcome are kept in the row's provenance and in coverage_reconciliations.
def is_legacy_complete(cov) -> bool:
    return bool(cov) and cov.get("status") == "complete" and cov.get("certified_by") not in ACCEPTED_CERTIFIERS


def status_key(cov) -> str:
    """Summary key used by coverage overviews: 'complete' only for certified rows."""
    if is_legacy_complete(cov):
        return "complete:legacy_uncertified"
    return cov["status"] + (f":{cov['last_error']}" if cov.get("last_error") else "")


def gap_assessment(event_times, open_utc, close_utc) -> tuple:
    """(max_gap_seconds, implausible) for UTC event times inside [open, close] (see the gap check above)."""
    import math

    span = (close_utc - open_utc).total_seconds()
    if span <= 0:
        return 0.0, False
    points = [open_utc] + sorted(event_times) + [close_utc]
    max_gap = max((b - a).total_seconds() for a, b in zip(points, points[1:]))
    if GAP_EXPECTED_ROWS == float("inf") or not event_times:
        return max_gap, False
    n_min = max(1, int(-(-span // 60)))                      # session minutes 0 .. n_min-1
    counts: dict = {}
    for t in event_times:
        m = min(n_min - 1, max(0, int((t - open_utc).total_seconds() // 60)))
        counts[m] = counts.get(m, 0) + 1
    active = sorted(counts)
    starts = {m for m in active if (m - 1) not in counts}    # first minute of each run = one event
    capped = {m: min(c, GAP_ROW_CAP_PER_MINUTE) for m, c in counts.items()}
    t_events = math.log(max(2, len(starts))) + GAP_SIGNIFICANCE
    t_rows = math.log(max(2, sum(capped.values()))) + GAP_SIGNIFICANCE
    gap_min = GAP_MIN.total_seconds() / 60.0
    win = GAP_LOCAL_WINDOW_MINUTES
    marks = [-1] + active + [n_min]
    for a, b in zip(marks, marks[1:]):
        g = b - a - 1                                        # empty minutes a+1 .. b-1
        if g < gap_min:
            continue
        if g >= n_min / 2 and len(event_times) >= GAP_MIN_ROWS:
            return max_gap, True                             # (c)
        sides = [side for side in (list(range(max(0, a - win + 1), a + 1)), list(range(b, min(n_min, b + win))))
                 if len(side) >= GAP_MIN_CONTEXT_MINUTES]
        if not sides:
            continue
        # R5-1: each side is judged with the model that fits IT -- events where it has enough of them, capped rows
        # where it is saturated (a busy opening or closing stretch is one long run: few events, many rows) --
        # as the ratio of its prediction to the significance bound. A side with neither (too thin to judge) counts
        # as quiet (0). The gap is implausible when even the QUIETER side's judgement reaches the bound.
        judgements = []
        for side in sides:
            n_ev = sum(1 for m in side if m in starts)
            share = sum(1 for m in side if m in counts) / len(side)
            ratio = 0.0
            if n_ev >= GAP_MIN_SIDE_EVENTS:
                ratio = max(ratio, n_ev / len(side) * g / t_events)                                # (a)
            if share >= GAP_SATURATED_SHARE:
                ratio = max(ratio, sum(capped.get(m, 0) for m in side) / len(side) * g / t_rows)    # (b)
            judgements.append(ratio)
        if min(judgements) >= 1.0:
            return max_gap, True
    return max_gap, False


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
        floor = (max(1, IMPLAUSIBLY_SMALL_SHARE * active[len(active) // 2])
                 if len(active) >= IMPLAUSIBLY_SMALL_MIN_SESSIONS else 1)
        rows = [n for n in rows if n >= floor]
    return len(rows)
