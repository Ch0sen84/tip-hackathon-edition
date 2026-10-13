"""
Centralized signal-scoring engine -- ported from the production TIP app's
`signal_engine.py` (Phase 2 "single source of truth" for named gate
evaluation, confidence computation, and signal-lifecycle state transitions).

PORTING NOTES (read before touching thresholds):
  * `evaluate_gates`, `classify_confidence`, and `determine_transition` are
    ported VERBATIM in logic and threshold values (STRONG_RATIO=4.0,
    STRONG_Z=3.0, INVALIDATION_ADVERSE_PCT=-3.0, STABLE_MIN_QUALIFYING_DAYS=3).
  * The one functional change: the production `evaluate_gates` hard-imports
    `baselines.evaluate_percentile_gate(ticker, today_premium)`, which is
    Postgres-backed (real trailing-window historical percentile). This repo
    has no database of that kind, so `evaluate_gates` now accepts an
    OPTIONAL injectable `percentile_gate_fn` callable with the same
    signature. The default implementation, `synthetic_percentile_gate`
    below, is a SYNTHETIC/STATIC estimator -- NOT the production
    historical-percentile model -- documented inline where it's defined.
  * `log_gate_results`, `log_transition`, and `qualifying_days_count` are
    NOT ported here: they are Postgres persistence helpers in production.
    This app computes an equivalent `qualifying_days_count` from a small
    local SQLite log instead -- see `backend/app/storage.py`.
"""
from __future__ import annotations

from typing import Callable, Optional

GATE_NAMES = (
    "dark_pool_anomaly", "options_flow_anomaly", "historical_percentile",
    "persistence", "price_confirmation", "multi_signal_confirmation",
    "catalyst_identified", "data_quality",
)

# Confirmed-strength thresholds -- identical to production signal_engine.py.
STRONG_RATIO = 4.0
STRONG_Z = 3.0
INVALIDATION_ADVERSE_PCT = -3.0  # price move against expected direction, %
STABLE_MIN_QUALIFYING_DAYS = 3


def synthetic_percentile_gate(ticker: str, today_premium: float) -> dict:
    """SYNTHETIC baseline estimator -- NOT the production historical-percentile
    model (that one is in ~/.hermes/scripts/uw/baselines.py and computes a real
    trailing-window percentile from Postgres history). This hackathon build has
    no such history, so it estimates a plausible percentile from fixed static
    premium-size buckets. Good enough to exercise the gate/confidence/state
    logic end-to-end on demo data; NOT a claim about real historical rarity.
    """
    buckets = (
        (50_000_000, 0.995),
        (20_000_000, 0.98),
        (10_000_000, 0.95),
        (5_000_000, 0.90),
        (1_000_000, 0.75),
        (0, 0.50),
    )
    premium = float(today_premium or 0)
    percentile = next(p for threshold, p in buckets if premium >= threshold)
    return {
        "passed": percentile >= 0.95,
        "percentile": percentile,
        "window_days": 30,
        "synthetic": True,
        "ticker": ticker,
        "today_premium": premium,
        "note": "synthetic baseline, not the production historical-percentile model",
    }


PercentileGateFn = Callable[[str, float], dict]


# --- Gate evaluation --------------------------------------------------------

def evaluate_gates(ticker: str, *, tier1_hit: bool = False, tier1_bootstrap: bool = False,
                    tier1_ratio: Optional[float] = None, tier1_z: Optional[float] = None,
                    tier2_hit: bool = False, tier2_qualifying_prints: int = 0,
                    tier2_max_pct: float = 0.0, options_flow_hit: bool = False,
                    options_flow_metrics: Optional[dict] = None,
                    today_premium: Optional[float] = None,
                    is_new_qualifying_day: bool = False,
                    price_move_pct: Optional[float] = None,
                    direction_heuristic: Optional[str] = None,
                    catalyst_identified: bool = False,
                    api_errors: int = 0,
                    percentile_gate_fn: Optional[PercentileGateFn] = None) -> dict:
    """Evaluates every named gate for one ticker/scan. Returns
    {gate_name: {"passed": bool, "metrics": {...}}}. Pure function -- does
    not touch the DB or network. `percentile_gate_fn` defaults to the
    synthetic estimator above; inject a real one (e.g. a LiveUWAdapter-backed
    historical calculation) to change only that one gate's data source."""
    percentile_gate_fn = percentile_gate_fn or synthetic_percentile_gate
    gates = {}

    gates["dark_pool_anomaly"] = {
        "passed": bool(tier1_hit and not tier1_bootstrap),
        "metrics": {"ratio": tier1_ratio, "z_score": tier1_z, "bootstrap": tier1_bootstrap},
    }

    gates["options_flow_anomaly"] = {
        "passed": bool(options_flow_hit),
        "metrics": options_flow_metrics or {},
    }

    pct_gate = None
    if today_premium is not None:
        pct_gate = percentile_gate_fn(ticker, today_premium)
    gates["historical_percentile"] = {
        "passed": bool(pct_gate and pct_gate.get("passed")),
        "metrics": pct_gate or {"reason": "not evaluated (no today_premium supplied)"},
    }

    gates["persistence"] = {
        "passed": bool(tier2_hit or is_new_qualifying_day),
        "metrics": {"tier2_qualifying_prints": tier2_qualifying_prints,
                    "tier2_max_pct": tier2_max_pct, "is_new_qualifying_day": is_new_qualifying_day},
    }

    price_confirmed = None
    if price_move_pct is not None and direction_heuristic in ("bullish-lean", "bearish-lean"):
        expected_up = direction_heuristic == "bullish-lean"
        actual_up = price_move_pct > 0
        price_confirmed = (expected_up == actual_up)
    gates["price_confirmation"] = {
        "passed": bool(price_confirmed),
        "metrics": {"price_move_pct": price_move_pct, "direction_heuristic": direction_heuristic,
                    "confirmed": price_confirmed},
    }

    multi = sum(1 for g in ("dark_pool_anomaly", "options_flow_anomaly", "historical_percentile", "persistence")
                if gates[g]["passed"]) >= 2
    gates["multi_signal_confirmation"] = {
        "passed": multi,
        "metrics": {"contributing_gates": [g for g in
                    ("dark_pool_anomaly", "options_flow_anomaly", "historical_percentile", "persistence")
                    if gates[g]["passed"]]},
    }

    gates["catalyst_identified"] = {
        "passed": bool(catalyst_identified),
        "metrics": {"note": "not auto-computed -- set explicitly when a real catalyst is found"},
    }

    gates["data_quality"] = {
        "passed": api_errors == 0 and not tier1_bootstrap,
        "metrics": {"api_errors": api_errors, "bootstrap": tier1_bootstrap},
    }

    return gates


# --- Confidence --------------------------------------------------------------

def classify_confidence(gates: dict, is_watchlist: bool = False) -> tuple:
    """Returns (confidence, signal_type, reason, qualifies). Ported verbatim
    from production signal_engine.py."""
    if is_watchlist:
        return ("manual", "watchlist-manual",
                "Manually marked for tracking (named watchlist ticker)", True)

    dp = gates["dark_pool_anomaly"]
    of = gates["options_flow_anomaly"]  # noqa: F841 (kept for parity with production signature/readability)
    hp = gates["historical_percentile"]
    persist = gates["persistence"]
    multi = gates["multi_signal_confirmation"]

    if multi["passed"]:
        contributing = multi["metrics"]["contributing_gates"]
        conf = "high"
        reason = f"Multi-signal confirmation: {', '.join(contributing)}"
        return (conf, "multi-signal", reason, True)

    if dp["passed"]:
        ratio = dp["metrics"].get("ratio")
        z = dp["metrics"].get("z_score")
        strong = (ratio and ratio >= STRONG_RATIO) or (z and z >= STRONG_Z)
        conf = "high" if strong else "medium"
        reason = f"Dark-pool anomaly vs baseline: {ratio:.1f}x avg, z={z:.1f}" if ratio else "Dark-pool anomaly vs baseline"
        return (conf, "t1-baseline", reason, True)

    if hp["passed"]:
        m = hp["metrics"]
        conf = "high" if m.get("percentile", 0) >= 0.97 else "medium"
        reason = f"Historical percentile: {m.get('percentile', 0):.0%} in trailing {m.get('window_days')}d window"
        return (conf, "historical-percentile", reason, True)

    if persist["passed"] and persist["metrics"]["tier2_qualifying_prints"] > 0:
        n = persist["metrics"]["tier2_qualifying_prints"]
        pct = persist["metrics"]["tier2_max_pct"]
        conf = "high" if (n >= 3 or pct >= 0.90) else "medium"
        reason = f"{n} prints \u2265{pct:.0%} of that day's volume"
        return (conf, "t2-pctvol", reason, True)

    if gates["dark_pool_anomaly"]["metrics"].get("bootstrap"):
        return ("low", "t1-bootstrap", "Bootstrap-only, insufficient history -- not auto-qualified", False)

    return ("low", "none", "No qualifying gate passed", False)


# --- State machine ------------------------------------------------------------

def determine_transition(current_state: Optional[str], gates: dict,
                          qualifying_days_count: int, price_move_pct: Optional[float],
                          direction_heuristic: Optional[str]) -> tuple:
    """Pure, deterministic. Returns (new_state, rule_id). current_state is
    None for a brand-new ticker (no open qualification record yet). Ported
    verbatim from production signal_engine.py."""
    # Invalidation check first -- overrides everything else.
    if price_move_pct is not None and direction_heuristic in ("bullish-lean", "bearish-lean"):
        expected_up = direction_heuristic == "bullish-lean"
        actual_pct = price_move_pct if expected_up else -price_move_pct
        if actual_pct <= INVALIDATION_ADVERSE_PCT:
            return ("INVALIDATED", f"adverse_price_move<= {INVALIDATION_ADVERSE_PCT}%")

    any_gate_passed = any(g["passed"] for name, g in gates.items()
                           if name not in ("multi_signal_confirmation", "catalyst_identified", "data_quality"))

    if current_state is None:
        return ("NEW", "first_qualifying_gate")

    if current_state == "NEW":
        if gates["multi_signal_confirmation"]["passed"] or qualifying_days_count >= 2:
            return ("CONFIRMED", "multi_signal_or_persistence_on_first_reeval")
        return ("BUILDING", "single_signal_reevaluated")

    if current_state in ("BUILDING",):
        if gates["multi_signal_confirmation"]["passed"] or qualifying_days_count >= 2:
            return ("CONFIRMED", "multi_signal_or_2plus_qualifying_days")
        if not any_gate_passed:
            return ("FADING", "no_gate_passed_while_building")
        return ("BUILDING", "still_single_signal")

    if current_state == "CONFIRMED":
        if qualifying_days_count >= STABLE_MIN_QUALIFYING_DAYS and gates["price_confirmation"]["passed"]:
            return ("STABLE", f">= {STABLE_MIN_QUALIFYING_DAYS}_qualifying_days_and_price_confirmed")
        if not any_gate_passed:
            return ("FADING", "no_gate_passed_while_confirmed")
        return ("CONFIRMED", "still_confirmed")

    if current_state == "STABLE":
        if not any_gate_passed:
            return ("FADING", "no_gate_passed_while_stable")
        return ("STABLE", "still_stable")

    if current_state == "FADING":
        if gates["multi_signal_confirmation"]["passed"]:
            return ("CONFIRMED", "reheated_multi_signal")
        if any_gate_passed:
            return ("BUILDING", "reheated_single_signal")
        return ("FADING", "still_fading")

    if current_state == "INVALIDATED":
        return ("INVALIDATED", "terminal_state")

    return (current_state, "no_rule_matched_state_unchanged")
