"""Glue between raw UW-shaped adapter data and the pure signal_engine /
tip_evidence logic. Still DB-free and network-free: this module only
transforms dicts -> gate inputs -> gate results -> presentation dicts. The
only "memory" it needs (current lifecycle state, qualifying_days_count) is
passed in by the caller (backend/app/main.py), which gets it from
backend/app/storage.py (a small local SQLite log -- see that module's
docstring for why SQLite instead of Postgres).

This is a DESIGN DECISION made for this hackathon port: production TIP
computes tier1/tier2/options-flow hit booleans deep inside flow_tracker.py /
midday_scan.py by comparing against Postgres-stored historical baselines
collected over weeks. This app has no such history, so this module derives
the same *shape* of inputs directly and transparently from one scan's worth
of flow-alert / dark-pool data (synthetic or live), using simple, documented
rules below. Swap these heuristics out first if adapting this for anything
beyond a demo.
"""
from __future__ import annotations

from typing import Optional

from dataclasses import asdict
from datetime import date

from . import flow_direction, lifecycle, signal_engine as se

DARK_POOL_RATIO_HIT = 3.0  # matches tip_evidence.DP_STRONG_RATIO
OPTIONS_FLOW_PREMIUM_MIN = 1_000_000
OPTIONS_FLOW_ONE_SIDED_SHARE = 0.65


def _darkpool_metrics(darkpool_trades: list) -> dict:
    """Aggregate a batch of UW-shaped dark pool trades into tier1-style
    inputs. Each trade dict is expected to look like a real UW darkpool
    record: {"premium": float, "size": int, "price": float, ...}. The
    `avg_premium_baseline` key (if present on any trade, or passed in
    separately) stands in for the trailing average production would pull
    from Postgres; fixtures carry it explicitly per DESIGN DECISION above."""
    total_premium = sum(float(t.get("premium") or 0) for t in darkpool_trades)
    baseline = None
    for t in darkpool_trades:
        if t.get("avg_premium_baseline") is not None:
            baseline = float(t["avg_premium_baseline"])
            break
    if not darkpool_trades:
        return {"tier1_hit": False, "tier1_bootstrap": True, "tier1_ratio": None,
                "tier1_z": None, "total_premium": 0.0}
    if baseline is None or baseline <= 0:
        return {"tier1_hit": False, "tier1_bootstrap": True, "tier1_ratio": None,
                "tier1_z": None, "total_premium": total_premium}
    ratio = total_premium / baseline
    # simple synthetic z: treat baseline as mean, 25% of baseline as stdev proxy.
    stdev = max(baseline * 0.25, 1.0)
    z = (total_premium - baseline) / stdev
    return {
        "tier1_hit": ratio >= DARK_POOL_RATIO_HIT,
        "tier1_bootstrap": False,
        "tier1_ratio": round(ratio, 3),
        "tier1_z": round(z, 3),
        "total_premium": total_premium,
    }


def _options_flow_metrics(flow_alerts: list) -> dict:
    """Aggregate UW flow-alerts (field names matching the real UW
    option-trades/flow-alerts response: total_premium, total_ask_premium,
    total_bid_premium, has_sweep, has_multileg) into an options_flow_hit
    boolean plus the metrics dict the gate records."""
    if not flow_alerts:
        return {"options_flow_hit": False, "metrics": {"reason": "no flow alerts this scan"},
                "today_premium_from_alerts": 0.0}
    total_premium = sum(float(a.get("total_premium") or 0) for a in flow_alerts)
    ask_premium = sum(float(a.get("total_ask_premium") or 0) for a in flow_alerts)
    bid_premium = sum(float(a.get("total_bid_premium") or 0) for a in flow_alerts)
    any_sweep = any(bool(a.get("has_sweep")) for a in flow_alerts)
    any_multileg = any(bool(a.get("has_multileg")) for a in flow_alerts)
    side_total = ask_premium + bid_premium
    ask_share = (ask_premium / side_total) if side_total else None
    one_sided = ask_share is not None and (ask_share >= OPTIONS_FLOW_ONE_SIDED_SHARE
                                            or ask_share <= 1 - OPTIONS_FLOW_ONE_SIDED_SHARE)
    hit = total_premium >= OPTIONS_FLOW_PREMIUM_MIN and (any_sweep or one_sided)
    return {
        "options_flow_hit": hit,
        "metrics": {
            "total_premium": total_premium,
            "total_ask_premium": ask_premium,
            "total_bid_premium": bid_premium,
            "ask_share": round(ask_share, 3) if ask_share is not None else None,
            "has_sweep": any_sweep,
            "has_multileg": any_multileg,
        },
        "today_premium_from_alerts": total_premium,
    }


def _direction_heuristic(flow_alerts: list) -> Optional[str]:
    """Inferred lean from call-vs-put aggressor BUYING only -- see
    core/flow_direction.py for the observed/inferred split and why selling
    and multi-leg flow are not used. None when ambiguous."""
    return flow_direction.classify_flow(flow_alerts)["lean"]


def build_gate_inputs(ticker: str, flow_alerts: list, darkpool_trades: list,
                       price_move_pct: Optional[float] = None,
                       is_new_qualifying_day: bool = False,
                       api_errors: int = 0,
                       catalyst_identified: bool = False,
                       catalog_dark_pool_baseline: Optional[dict] = None,
                       mode: str = "demo") -> dict:
    """Pure transform: raw UW-shaped lists -> the keyword arguments
    signal_engine.evaluate_gates expects. Exposed separately from
    evaluate_ticker_scan so tests can assert on the intermediate shape.

    Phase C, ADDITIVE ONLY: `catalog_dark_pool_baseline`, when supplied,
    is a dict already shaped exactly like `_darkpool_metrics`'s return
    value (tier1_hit/tier1_bootstrap/tier1_ratio/tier1_z/total_premium) --
    see app/core/dark_pool_baseline.BaselineResult.as_gate_inputs(), which
    produces precisely that shape from a real historical-baseline
    computation over the Phase B catalog tables. When it is None (every
    existing caller, and still the only path for the synthetic/demo
    adapter, which is never cataloged), behavior is BYTE-FOR-BYTE
    unchanged from before this phase: the old `_darkpool_metrics` heuristic
    over `darkpool_trades`' own `avg_premium_baseline` field runs exactly
    as it always has. This is the one new input Live Mode scans can now
    supply for the dark_pool_anomaly gate -- signal_engine.evaluate_gates
    itself is untouched; this only changes what gets passed INTO its
    existing tier1_* keyword arguments."""
    # Live Mode NEVER uses the fixture-style avg_premium_baseline path: a real
    # dark-pool baseline comes only from the cataloged session history
    # (dark_pool_baseline.py). Without one the gate is "insufficient".
    if catalog_dark_pool_baseline is not None:
        dp = catalog_dark_pool_baseline
        dp_source = "uw_darkpool_session_baseline"
    elif mode == "live":
        dp = {"tier1_hit": False, "tier1_bootstrap": True, "tier1_ratio": None, "tier1_z": None,
              "total_premium": sum(float(t.get("premium") or 0) for t in darkpool_trades or []),
              "reason": "no session baseline available"}
        dp_source = "uw_darkpool_session_baseline"
    else:
        dp = _darkpool_metrics(darkpool_trades or [])
        dp_source = "demo_fixture_baseline"
    of = _options_flow_metrics(flow_alerts or [])
    direction_detail = flow_direction.classify_flow(flow_alerts or [])
    direction = direction_detail["lean"]
    today_premium = (dp.get("total_premium") or 0.0) + of["today_premium_from_alerts"]
    return dict(
        ticker=ticker,
        tier1_hit=dp["tier1_hit"], tier1_bootstrap=dp["tier1_bootstrap"],
        tier1_ratio=dp["tier1_ratio"], tier1_z=dp["tier1_z"],
        tier2_hit=False, tier2_qualifying_prints=0, tier2_max_pct=0.0,
        options_flow_hit=of["options_flow_hit"], options_flow_metrics=of["metrics"],
        today_premium=today_premium if today_premium > 0 else None,
        is_new_qualifying_day=is_new_qualifying_day,
        price_move_pct=price_move_pct, direction_heuristic=direction,
        catalyst_identified=catalyst_identified, api_errors=api_errors,
        direction_detail=direction_detail,
        mode=mode,
        dark_pool_source=dp_source,
        dark_pool_metrics={k: v for k, v in dp.items()
                           if k not in ("tier1_hit", "tier1_bootstrap", "tier1_ratio", "tier1_z")},
        options_flow_source=("demo_fixture_snapshot_threshold" if mode != "live"
                             else "uw_flow_alerts_snapshot_threshold"),
    )


def evaluate_ticker_scan(ticker: str, flow_alerts: list, darkpool_trades: list,
                          current_state: Optional[str], qualifying_days_count: int,
                          price_move_pct: Optional[float] = None,
                          is_new_qualifying_day: bool = False,
                          api_errors: int = 0,
                          catalyst_identified: bool = False,
                          percentile_gate_fn=None,
                          catalog_dark_pool_baseline: Optional[dict] = None,
                          collection_status: Optional[dict] = None,
                          mode: str = "demo",
                          lifecycle_context: Optional["lifecycle.LifecycleContext"] = None,
                          session_date: Optional[date] = None,
                          price_last: Optional[float] = None,
                          evidence_fingerprint: Optional[str] = None) -> dict:
    """End-to-end: raw adapter data -> gates -> confidence -> state
    transition. Returns everything the API layer needs to render both
    Market Radar and Ticker Analysis for one ticker.

    `catalog_dark_pool_baseline` is passed straight through to
    `build_gate_inputs` -- see its docstring; omitting it (the default)
    reproduces pre-Phase-C behavior exactly."""
    inputs = build_gate_inputs(ticker, flow_alerts, darkpool_trades,
                                price_move_pct=price_move_pct,
                                is_new_qualifying_day=is_new_qualifying_day,
                                api_errors=api_errors,
                                catalyst_identified=catalyst_identified,
                                catalog_dark_pool_baseline=catalog_dark_pool_baseline,
                                mode=mode)
    # Collection status (audit B4/E9): a source that could not be collected
    # is "unavailable", never an empty-but-successful observation.
    collection_status = dict(collection_status or {})
    failed = sorted(k for k, v in collection_status.items() if v != "ok")
    collection_complete = not failed
    if failed:
        inputs["api_errors"] = max(inputs.get("api_errors") or 0, len(failed))
    if collection_status.get("stock_state", "ok") != "ok":
        inputs["price_move_pct"] = None
        price_move_pct = None
    direction = inputs["direction_heuristic"]
    gates = se.evaluate_gates(
        percentile_gate_fn=percentile_gate_fn,
        options_flow_available=collection_status.get("options_flow", "ok") == "ok",
        darkpool_available=collection_status.get("darkpool", "ok") == "ok",
        collection_status=collection_status,
        **inputs)
    confidence, signal_type, reason, qualifies = se.classify_confidence(
        gates, collection_complete=collection_complete)
    # Invalidation uses the inferred lean only when it comes from options
    # flow that actually qualified this scan.
    lifecycle_direction = direction if gates["options_flow_anomaly"]["passed"] else None
    lifecycle_detail = None
    if lifecycle_context is not None:
        # Episode-scoped, trading-session-counted lifecycle (core/lifecycle.py).
        decision = lifecycle.decide(
            lifecycle_context, gates, session_date=session_date or date.today(),
            collection_complete=collection_complete, lean=lifecycle_direction,
            price_last=price_last, price_move_pct=price_move_pct,
            fingerprint=evidence_fingerprint)
        current_state = decision.state_before
        new_state, rule_id = decision.state_after, decision.rule
        qualifying_days_count = decision.qualifying_sessions
        gates["persistence"] = se.persistence_context_gate(decision.qualifying_sessions)
        lifecycle_detail = asdict(decision)
    else:
        new_state, rule_id = se.determine_transition(
            current_state, gates, qualifying_days_count, price_move_pct, lifecycle_direction,
            collection_complete=collection_complete)
    # Display list: real evidence that passed (independent + corroborating
    # price_confirmation). Zero-weight gates (synthetic percentile,
    # persistence) never appear here. Lifecycle bookkeeping uses
    # independent_gates_passed only.
    gates_passed = [name for name, g in gates.items() if g["passed"]
                    and name not in ("multi_signal_confirmation", "catalyst_identified", "data_quality",
                                     "historical_percentile", "persistence")]
    independent_passed = se.independent_gates_passed(gates)
    return {
        "ticker": ticker,
        "gates": gates,
        "gates_passed": gates_passed,
        "independent_gates_passed": independent_passed,
        "mode": mode,
        "confidence": confidence,
        "signal_type": signal_type,
        "confidence_reason": reason,
        "qualifies": qualifies,
        "state": new_state,
        "previous_state": current_state,
        "transition_rule": rule_id,
        "qualifying_days_count": qualifying_days_count,
        "direction_heuristic": direction,
        "direction_detail": inputs.get("direction_detail"),
        "price_move_pct": price_move_pct,
        "collection_complete": collection_complete,
        "collection_failed_sources": failed,
        "lifecycle": lifecycle_detail,
    }
