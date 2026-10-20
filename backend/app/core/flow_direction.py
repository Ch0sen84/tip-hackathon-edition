"""Options-flow direction: what was OBSERVED vs what is INFERRED.

Signal-integrity remediation (audit B3 / E3 / E4). The previous heuristic
computed one ask-side share across calls AND puts combined, so aggressive
PUT buying was read as "bullish". On the captured PLTR 2026-10-16T15:02Z
payload that produced `bullish-lean` from put-dominated buying ($3.26M put
ask-side vs $1.58M call ask-side) and a false permanent INVALIDATED when the
stock fell -- a move that actually agreed with the observed flow.

OBSERVED (facts straight from UW flow-alert fields; nothing inferred):
  premium executed at/near the ask vs at/near the bid, split by option type:
      call_ask, call_bid, put_ask, put_bid
  plus premium that carried no side, premium in multi-leg alerts, and alerts
  whose option type is missing.

INFERRED (a heuristic, never a fact about intent):
  `lean` is derived ONLY from aggressor-side BUYING in single-leg alerts:
      call_buy_share = call_ask / (call_ask + put_ask)
      >= 0.65 -> "bullish-lean"  (call buying dominates the aggressive buying)
      <= 0.35 -> "bearish-lean"  (put buying dominates the aggressive buying)
      otherwise -> None (mixed)
  The 0.65 / 0.35 cut-offs are the same one-sided share this codebase already
  used (scan_service.OPTIONS_FLOW_ONE_SIDED_SHARE); they were not tuned on
  outcomes.

Why selling is reported but not used for the lean: bid-side premium (call
selling, put selling) has a much weaker and more ambiguous directional
reading -- covered calls, cash-secured puts, closing a long position, or
spread legs -- and UW flow alerts do not say whether a trade opened or closed
a position. Treating put selling as equivalent to call buying (or call
selling as equivalent to put buying) would overstate what the data shows.

No lean is produced (lean=None with an explicit `ambiguity` reason) when:
  * there is no aggressive single-leg buying at all,
  * less than half of the single-leg premium has a known side (the rest
    traded mid-market / unclassified, so the side split is not representative),
  * selling (bid-side) premium is the majority of sided premium -- a small
    amount of buying next to dominant selling is not a reliable lean,
  * buying is mixed (between the cut-offs).

A lean is CONTEXT for the price_confirmation / invalidation logic. A later
price move in the same direction is coincidence-consistent, not proof that
the inference was right; nothing here has been validated as predictive.
"""
from __future__ import annotations

from typing import Optional

LEAN_SHARE = 0.65
MIN_SIDED_SHARE = 0.5
BASIS = "aggressor-side (ask) buying in single-leg alerts: call premium vs put premium"


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _option_type(alert: dict) -> Optional[str]:
    t = str(alert.get("type") or "").strip().lower()
    if t in ("call", "put"):
        return t
    if alert.get("is_call") and not alert.get("is_put"):
        return "call"
    if alert.get("is_put") and not alert.get("is_call"):
        return "put"
    return None


def classify_flow(flow_alerts: list) -> dict:
    observed = {"call_ask": 0.0, "call_bid": 0.0, "put_ask": 0.0, "put_bid": 0.0,
                "unsided_single_leg": 0.0, "multileg_premium": 0.0, "unknown_type_premium": 0.0,
                "single_leg_premium": 0.0, "alerts": 0, "multileg_alerts": 0}
    for a in flow_alerts or []:
        if not isinstance(a, dict):
            continue
        observed["alerts"] += 1
        total = _f(a.get("total_premium"))
        if a.get("has_multileg"):
            observed["multileg_alerts"] += 1
            observed["multileg_premium"] += total
            continue
        kind = _option_type(a)
        if kind is None:
            observed["unknown_type_premium"] += total
            continue
        ask, bid = _f(a.get("total_ask_premium")), _f(a.get("total_bid_premium"))
        observed[f"{kind}_ask"] += ask
        observed[f"{kind}_bid"] += bid
        observed["single_leg_premium"] += total
        observed["unsided_single_leg"] += max(total - ask - bid, 0.0)

    buy = observed["call_ask"] + observed["put_ask"]
    sided = buy + observed["call_bid"] + observed["put_bid"]
    single = observed["single_leg_premium"]
    sided_share = (sided / single) if single > 0 else None
    call_buy_share = (observed["call_ask"] / buy) if buy > 0 else None

    lean: Optional[str] = None
    ambiguity: Optional[str] = None
    if buy <= 0:
        ambiguity = "no aggressive (ask-side) single-leg buying observed"
    elif sided_share is not None and sided_share < MIN_SIDED_SHARE:
        ambiguity = "most single-leg premium traded without a clear side"
    elif sided > 0 and buy / sided < 0.5:
        ambiguity = "selling (bid-side) premium dominates; its directional meaning is ambiguous"
    elif call_buy_share >= LEAN_SHARE:
        lean = "bullish-lean"
    elif call_buy_share <= 1 - LEAN_SHARE:
        lean = "bearish-lean"
    else:
        ambiguity = "call buying and put buying are mixed"

    return {
        "lean": lean,
        "ambiguity": ambiguity,
        "basis": BASIS,
        "call_buy_share": round(call_buy_share, 4) if call_buy_share is not None else None,
        "sided_share": round(sided_share, 4) if sided_share is not None else None,
        "observed": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in observed.items()},
        "inferred": True,
        "note": "inferred sentiment heuristic, not a statement of trader intent or a prediction",
    }
