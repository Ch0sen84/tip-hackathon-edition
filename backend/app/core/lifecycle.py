"""Episode-scoped, trading-session-counted signal lifecycle.

Signal-integrity remediation (audit B5/B6/B7, E6/E7/E8). The previous model
kept one all-time `ticker_scan_log` per ticker, counted *server UTC calendar
dates* with any gate passing (including the synthetic one), never reset that
count, treated INVALIDATED as a permanent lock on the ticker, and let repeated
scans of the same data advance states -- the captured SPY history reached
STABLE eligibility from a Saturday re-read of Friday's data.

Model
-----
* A **qualification episode** opens on the first complete scan with
  qualifying INDEPENDENT evidence and ends when it is INVALIDATED or expires.
  All counts below are per episode, never all-time.
* Evidence is attributed to an **NYSE trading session**
  (`trading_calendar.evidence_session_date`): weekend / holiday / pre-open
  scans belong to the previous session.
* A session counts toward the episode only once, however many scans it saw.
* **Identical evidence** (same set of observation ids) already recorded for
  an EARLIER session of the episode is treated as a stale re-read: the scan
  is held -- no promotion, no demotion. (Demo fixtures are static, so Demo
  scans carry no fingerprint and skip this check.)
* Decisions are only taken on complete, evaluable evidence: a failed
  collection, or a scan where no independent gate could be evaluated
  (missing baseline history), holds the current state.

Transitions within an open episode
----------------------------------
  NEW       -> CONFIRMED  two independent categories on this scan
            -> BUILDING   qualifying evidence on a LATER session than the
                          one that opened the episode
            -> NEW        otherwise (repeated scans of the opening session
                          never advance it)
  BUILDING  -> CONFIRMED  two independent categories
            -> BUILDING   one independent category
            -> FADING     evaluated, none passed
  CONFIRMED -> STABLE     still two categories, price corroborates, and
                          two-category confirmation on >= 3 distinct
                          sessions of this episode
            -> CONFIRMED / BUILDING (demoted) / FADING
  STABLE    -> STABLE / BUILDING (demoted) / FADING
  FADING    -> CONFIRMED / BUILDING (reheated) / FADING
  any open  -> INVALIDATED  the inferred lean of qualifying options flow is
                          contradicted by a >= 3% adverse price move measured
                          from the episode's opening reference price (or the
                          day's change when no reference price was captured).
                          INVALIDATED closes the episode.

Episodes
--------
* Expiry: an open episode whose last qualifying session is more than
  EPISODE_MAX_GAP_SESSIONS trading sessions old is closed ("expired") before
  the current scan is evaluated, so stale state cannot persist indefinitely.
* After INVALIDATED, a NEW episode may open only on a LATER trading session
  than the invalidation (never by silently reviving the old one); its first
  transition rule says so explicitly.

Nothing here implies predictive validity: states describe how evidence has
accumulated, not what price will do.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Optional

from . import signal_engine as se
from . import trading_calendar as tc

EPISODE_MAX_GAP_SESSIONS = 5
STABLE_MIN_CONFIRMED_SESSIONS = se.STABLE_MIN_QUALIFYING_DAYS
INVALIDATION_ADVERSE_PCT = se.INVALIDATION_ADVERSE_PCT


@dataclass(frozen=True)
class LifecycleContext:
    episode_id: Optional[int] = None
    state: Optional[str] = None                 # state of the OPEN episode, None if none open
    opened_session: Optional[str] = None
    reference_price: Optional[float] = None
    qualifying_sessions: tuple = ()             # ISO dates with qualifying evidence (this episode)
    confirmed_sessions: tuple = ()              # ISO dates with two-category confirmation
    last_qualifying_session: Optional[str] = None
    earlier_fingerprints: tuple = ()            # evidence fingerprints from EARLIER sessions
    last_closed_reason: Optional[str] = None    # of the most recent closed episode
    last_closed_session: Optional[str] = None
    display_state: Optional[str] = None         # shown when no episode is open (INVALIDATED / None)


@dataclass
class LifecycleDecision:
    state_before: Optional[str]
    state_after: Optional[str]
    rule: str
    action: str                                 # hold | none | open | update | close_invalidated
    session_date: str
    qualifying: bool = False
    confirmed: bool = False
    fingerprint: Optional[str] = None
    expire_open_episode: bool = False
    reference_price: Optional[float] = None
    qualifying_sessions: int = 0
    confirmed_sessions: int = 0
    write_scan_row: bool = True
    detail: dict = field(default_factory=dict)


def evidence_fingerprint(flow_alerts: list, darkpool_trades: list) -> Optional[str]:
    """Stable identity of the observation set a scan evaluated."""
    keys = []
    for a in flow_alerts or []:
        k = a.get("id") or a.get("alert_id")
        keys.append(f"f:{k}" if k else "f:" + "|".join(str(a.get(x)) for x in ("created_at", "total_premium", "type")))
    for t in darkpool_trades or []:
        k = t.get("tracking_id") or t.get("id")
        keys.append(f"d:{k}" if k else "d:" + "|".join(str(t.get(x)) for x in ("executed_at", "price", "size")))
    if not keys:
        return None
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:32]


def _move_since(reference: Optional[float], last: Optional[float], daily: Optional[float]):
    if reference and last:
        return (last - reference) / reference * 100.0, "since_episode_open"
    if daily is not None:
        return daily, "daily_change"
    return None, None


def decide(ctx: LifecycleContext, gates: dict, *, session_date: date_cls, collection_complete: bool,
           lean: Optional[str], price_last: Optional[float], price_move_pct: Optional[float],
           fingerprint: Optional[str]) -> LifecycleDecision:
    sd = session_date.isoformat()
    shown_before = ctx.state if ctx.state is not None else ctx.display_state

    def out(state_after, rule, action, **kw):
        return LifecycleDecision(state_before=shown_before, state_after=state_after, rule=rule,
                                 action=action, session_date=sd, fingerprint=fingerprint, **kw)

    if not collection_complete:
        return out(shown_before, "held_incomplete_collection", "hold", write_scan_row=False)
    if not se.independent_gates_evaluable(gates):
        return out(shown_before, "held_insufficient_evidence", "hold",
                   qualifying_sessions=len(ctx.qualifying_sessions),
                   confirmed_sessions=len(ctx.confirmed_sessions))

    independent = se.independent_gates_passed(gates)
    any_pass = bool(independent)
    multi = bool(gates["multi_signal_confirmation"]["passed"])
    duplicate_of_earlier_session = fingerprint is not None and fingerprint in ctx.earlier_fingerprints

    state = ctx.state
    expire = False
    if state is not None and ctx.last_qualifying_session:
        gap = tc.trading_sessions_between(date_cls.fromisoformat(ctx.last_qualifying_session), session_date)
        if gap > EPISODE_MAX_GAP_SESSIONS:
            expire, state = True, None

    if state is not None and duplicate_of_earlier_session:
        return out(state, "held_identical_evidence_from_an_earlier_session", "hold",
                   qualifying_sessions=len(ctx.qualifying_sessions),
                   confirmed_sessions=len(ctx.confirmed_sessions))

    qualifying = any_pass
    confirmed = multi

    # --- no open episode -------------------------------------------------
    if state is None:
        display = None if expire else ctx.display_state
        if not qualifying:
            rule = "episode_expired_no_recent_evidence" if expire else (
                "no_qualifying_evidence_yet" if display is None else "no_qualifying_evidence")
            return out(display, rule, "none", expire_open_episode=expire)
        if ctx.last_closed_reason == "invalidated" and ctx.last_closed_session == sd and not expire:
            return out("INVALIDATED", "invalidated_this_session_no_reopen", "none")
        new_state = "CONFIRMED" if confirmed else "NEW"
        if ctx.last_closed_reason == "invalidated" and not expire:
            rule = "new_episode_after_invalidation"
        elif expire:
            rule = "new_episode_after_expiry"
        else:
            rule = ("opened_confirmed_two_independent_categories" if confirmed else "first_qualifying_gate")
        return out(new_state, rule, "open", qualifying=True, confirmed=confirmed,
                   expire_open_episode=expire, reference_price=price_last,
                   qualifying_sessions=1, confirmed_sessions=1 if confirmed else 0)

    # --- open episode: invalidation first ---------------------------------
    move, basis = _move_since(ctx.reference_price, price_last, price_move_pct)
    if lean in ("bullish-lean", "bearish-lean") and move is not None:
        adverse = move if lean == "bullish-lean" else -move
        if adverse <= INVALIDATION_ADVERSE_PCT:
            return out("INVALIDATED", f"adverse_price_move_{basis}<= {INVALIDATION_ADVERSE_PCT}%",
                       "close_invalidated", qualifying=False, confirmed=False,
                       qualifying_sessions=len(ctx.qualifying_sessions),
                       confirmed_sessions=len(ctx.confirmed_sessions),
                       detail={"move_pct": round(move, 3), "basis": basis, "lean": lean})

    q_sessions = set(ctx.qualifying_sessions) | ({sd} if qualifying else set())
    c_sessions = set(ctx.confirmed_sessions) | ({sd} if confirmed else set())
    kw = dict(qualifying=qualifying, confirmed=confirmed,
              qualifying_sessions=len(q_sessions), confirmed_sessions=len(c_sessions))

    if state == "NEW":
        if multi:
            return out("CONFIRMED", "multi_signal_confirmation_two_independent_categories", "update", **kw)
        if any_pass:
            if sd != ctx.opened_session:
                return out("BUILDING", "qualifying_evidence_on_a_later_session", "update", **kw)
            return out("NEW", "same_session_no_advance", "update", **kw)
        return out("NEW", "no_gate_passed_remains_new", "update", **kw)

    if state == "BUILDING":
        if multi:
            return out("CONFIRMED", "multi_signal_confirmation_two_independent_categories", "update", **kw)
        if any_pass:
            return out("BUILDING", "still_single_signal", "update", **kw)
        return out("FADING", "no_gate_passed_while_building", "update", **kw)

    if state == "CONFIRMED":
        if multi and gates["price_confirmation"]["passed"] and len(c_sessions) >= STABLE_MIN_CONFIRMED_SESSIONS:
            return out("STABLE", f"confirmed_on_{STABLE_MIN_CONFIRMED_SESSIONS}_distinct_sessions_and_price_corroborates",
                       "update", **kw)
        if multi:
            return out("CONFIRMED", "still_confirmed", "update", **kw)
        if any_pass:
            return out("BUILDING", "demoted_lost_multi_signal_confirmation", "update", **kw)
        return out("FADING", "no_gate_passed_while_confirmed", "update", **kw)

    if state == "STABLE":
        if multi:
            return out("STABLE", "still_stable", "update", **kw)
        if any_pass:
            return out("BUILDING", "demoted_lost_multi_signal_confirmation", "update", **kw)
        return out("FADING", "no_gate_passed_while_stable", "update", **kw)

    if state == "FADING":
        if multi:
            return out("CONFIRMED", "reheated_multi_signal", "update", **kw)
        if any_pass:
            return out("BUILDING", "reheated_single_signal", "update", **kw)
        return out("FADING", "still_fading", "update", **kw)

    return out(state, "no_rule_matched_state_unchanged", "update", **kw)
