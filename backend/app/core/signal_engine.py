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

SIGNAL-INTEGRITY RULE CHANGE (2026-10-15, Chad-approved, read before touching
confirmation/confidence logic again):

  1. `historical_percentile` (the SYNTHETIC static premium-size-bucket
     estimator -- see `synthetic_percentile_gate` below) has ZERO weight in
     any state-machine or confidence decision now. It is still computed and
     still returned in the `gates` dict every scan (so the UI can keep
     showing it as an honestly-labeled, non-counting descriptive/demo-only
     indicator -- see `tip_evidence.py`'s `synthetic_baseline` family,
     already correct as of commit 7dafc8f), but it no longer contributes to
     `multi_signal_confirmation`'s tally, to `classify_confidence`'s tiers,
     or to any lifecycle transition. A ticker whose ONLY passing gate is
     this synthetic one can never reach "high" confidence or CONFIRMED.

  2. CONFIRMED (and HIGH confidence) now require >= 2 of the following
     `INDEPENDENT_SIGNAL_GATES` to have genuinely passed on the SAME scan:
       - dark_pool_anomaly     (source: real UW dark-pool trade records)
       - options_flow_anomaly  (source: real UW option-trades/flow-alerts)
       - catalyst_identified   (source: a manually-verified real catalyst --
                                 never auto-computed from any UW feed at all,
                                 see adapters/base.py)
     `price_confirmation`, `persistence`, and `historical_percentile` are
     DELIBERATELY EXCLUDED from this independence list:
       - `historical_percentile` per rule 1 above (it's synthetic, not a
         real data source at all).
       - `persistence` is excluded because, in THIS app's build, it is
         satisfied by `tier2_hit` (hardcoded False everywhere -- tier2 was
         never ported, see scan_service.py) OR `is_new_qualifying_day`,
         which is just "today's calendar date differs from a previously
         logged qualifying date" -- a fact about WHEN a scan happened, not
         an independently-sourced piece of market evidence. Counting it as
         a second "category" would let the exact same single real anomaly
         (e.g. one options-flow hit) qualify as "2 independent categories"
         merely by being seen on two different calendar dates, which is the
         specific loophole this rule change closes. `persistence` remains a
         visible gate (and still contributes to `any_gate_passed` for
         BUILDING/FADING bookkeeping below) but never counts toward the
         2-category CONFIRMED/HIGH bar.
       - `price_confirmation` is excluded on careful analysis, NOT a rubber
         stamp: its own "passed" boolean is gated on `direction_heuristic`,
         which `scan_service._direction_heuristic` computes from the SAME
         options-flow ask/bid-side premium split that `options_flow_anomaly`
         itself reads (see scan_service.build_gate_inputs). price_confirmation
         measures a genuinely different instrument (real stock price/OHLC,
         not options premium), so it IS real additional evidence and still
         counts toward `any_gate_passed` / the STABLE transition's price
         check below -- but it can only ever confirm or contradict a
         direction that `options_flow_anomaly`'s own feed produced. Treating
         "options_flow_anomaly passed" + "price_confirmation passed" as 2
         INDEPENDENT categories would, in practice, let a single options-flow
         anomaly with a consistent price reaction reach CONFIRMED by itself
         -- exactly the "single genuine options-flow anomaly alone... must
         result in observed/candidate, NOT CONFIRMED" case this rule change
         is meant to prevent. price_confirmation therefore corroborates
         existing evidence (and can flag CONFLICTING evidence when it fails)
         but is not itself counted as one of the 2 required independent
         categories. Reconsider this if a future gate's direction input is
         ever sourced from something other than the options-flow feed.
     If a future gate is added that derives its "passed" boolean from the
     same raw API call as an existing INDEPENDENT_SIGNAL_GATES member (e.g.
     two different thresholds over the same flow-alerts batch), it must NOT
     be added to INDEPENDENT_SIGNAL_GATES either.

  3. A SINGLE real gate passing (even a strong dark-pool ratio, even options
     flow with a sweep) now caps at "medium" confidence and a non-CONFIRMED
     lifecycle state (NEW/BUILDING/"observed") -- never "high"/CONFIRMED,
     regardless of how many calendar days it has been seen on. Only genuine
     multi-category corroboration on the CURRENT scan reaches CONFIRMED.

  4. `determine_transition` no longer has a `qualifying_days_count >= 2`
     bypass to CONFIRMED (the old code's "OR persistence" path -- removed,
     see rule 2's loophole note). It also now RE-EVALUATES every scan: a
     ticker already sitting in CONFIRMED or STABLE from a PRIOR scan (under
     the old rules, or simply because evidence faded) is demoted to
     BUILDING the moment `multi_signal_confirmation` no longer passes on the
     current scan (demoted further to FADING only when no gate passes at
     all). Existing `ticker_scan_log` / `ticker_current_state` rows are
     never deleted or rewritten for audit purposes (see storage.py) -- only
     the FORWARD-looking transition decision on the next scan changes.

  5. KNOWN, DELIBERATELY NOT FIXED THIS PASS (documented per Chad's
     "narrower fix preferred over bigger redesign" instruction): CONFIRMED
     -> STABLE still uses `qualifying_days_count` (distinct calendar dates
     with any gate passing, see storage.py's `COUNT(DISTINCT scan_date)`).
     Because STABLE now ALSO requires `multi_signal_confirmation` to still
     be passing on the scan that makes the transition (not just historical
     qualifying days), a ticker cannot drift into STABLE on stale/unchanged
     single-gate evidence -- but it COULD still reach STABLE after 3
     calendar days of the SAME genuinely-real, unchanged 2-category
     evidence repeating verbatim (e.g. an outage or a quiet market
     returning an identical flow-alerts window on two different dates).
     Closing that fully would require hashing/fingerprinting the underlying
     evidence per qualifying day and is new infrastructure beyond this
     pass's scope -- flagged for a future decision, not built here.

  6. L1 LIFECYCLE FIX (2026-10-16, Chad-approved): `determine_transition`'s
     `current_state == "NEW"` branch used to return ("BUILDING",
     "single_signal_reevaluated") UNCONDITIONALLY whenever `confirmed` was
     False -- so a second scan of an already-NEW ticker with ZERO
     qualifying gates on the CURRENT evaluation still promoted NEW ->
     BUILDING. Fixed: that branch now requires `any_gate_passed` (at least
     one real gate passing on THIS scan) before promoting, mirroring the
     guard every other branch in this function already uses (BUILDING
     keeps/demotes on `any_gate_passed`; CONFIRMED/STABLE demote on it
     too). A ticker with no qualifying evidence on the current scan now
     correctly STAYS "NEW" (`no_gate_passed_remains_new`) instead of
     silently advancing.

     INVESTIGATED AND REJECTED THIS ROUND: a stronger "genuinely NEW
     evidence vs the SAME observations re-evaluated identically" check
     (distinguishing a second real dark-pool/options-flow hit from the
     exact same prints being re-fetched and re-scored). Checked against
     the actual persistence layer and rejected as NOT safely
     implementable this round:
       * `storage.py`'s `ticker_scan_log` stores only (ticker, scan_date,
         any_gate_passed INTEGER 0/1, state, created_at) per scan -- a
         single boolean per day, with no gate-level metrics, no
         observation row ids, and no evidence hash/fingerprint. Nothing
         persisted identifies WHICH prints/flow-alerts produced a given
         day's `any_gate_passed=1`, so there is no reliable way today to
         tell "a different print than yesterday's" from "the same print
         re-read" using data this app actually stores.
       * `dark_pool_baseline.py` observations do carry `event_time` and a
         premium value per row, and `discovery.py` candidates carry a
         source_date/rank/net_premium snapshot -- but neither is passed
         into or stored by this state machine today. Fingerprinting them
         (e.g. hashing event_time+premium per qualifying day into
         `ticker_scan_log`) is real, buildable, but genuinely NEW
         schema/plumbing across storage.py, scan_service.py, and the
         ingestion catalog reads -- not a same-file lifecycle-condition
         fix, and exactly the kind of invented heuristic Chad asked NOT
         to ship in its place (guessing from wall-clock-since-last-scan
         was considered and rejected too, since elapsed time says nothing
         about whether the evidence itself changed).
       * Per Chad's explicit instruction, the smallest RELIABLE fix ships
         instead: `any_gate_passed` on the current scan is a true
         structural guarantee (computed fresh from this scan's own gate
         inputs, never inferred/guessed), even though it is coarser than
         "genuinely new evidence." It correctly blocks the confirmed
         defect (zero-evidence promotion) without introducing an
         unreliable signal. The stronger per-evidence-identity check is
         flagged here as a real follow-up requiring new storage schema,
         not attempted as a patch in this pass.
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

# Gates counted toward the 2-independent-category CONFIRMED/HIGH bar.
# `historical_percentile` (synthetic), `persistence` (calendar-date-only,
# not an independent data source), and `price_confirmation` (its direction
# input is sourced from the SAME options-flow feed as options_flow_anomaly)
# are deliberately excluded -- see the "SIGNAL-INTEGRITY RULE CHANGE" note
# in this module's docstring for the full rationale before changing this
# tuple.
INDEPENDENT_SIGNAL_GATES = (
    "dark_pool_anomaly", "options_flow_anomaly", "catalyst_identified",
)


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

# Gate status vocabulary (signal-integrity remediation). `passed` stays a
# bool for backward compatibility; `status` says what the gate actually
# observed so a missing input is never read as a genuine negative:
#   pass / fail      -- evaluated on real (or, in Demo, fixture) inputs
#   insufficient     -- inputs collected, but the method lacks the history /
#                       coverage it needs to decide (e.g. no baseline yet)
#   unavailable      -- the provider could not be reached for this input
#   unsupported      -- not available in this mode/build (e.g. no manual
#                       catalyst workflow in Live; synthetic estimator in Live)
#   synthetic        -- Demo-only illustrative value, zero decision weight
#   not_evaluated    -- preconditions absent (e.g. no qualifying flow to confirm)
#   context          -- descriptive bookkeeping, zero decision weight
EVALUABLE_STATUSES = ("pass", "fail")
DECISION_WEIGHT = {
    "dark_pool_anomaly": "independent",
    "options_flow_anomaly": "independent",
    "catalyst_identified": "independent",
    "price_confirmation": "corroborating",
    "historical_percentile": "none",
    "persistence": "none",
    "multi_signal_confirmation": "derived",
    "data_quality": "none",
}


def _gate(passed, status, source, metrics, name):
    return {"passed": bool(passed), "status": status, "source": source,
            "decision_weight": DECISION_WEIGHT[name], "metrics": metrics}


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
                    percentile_gate_fn: Optional[PercentileGateFn] = None,
                    options_flow_available: bool = True,
                    darkpool_available: bool = True,
                    collection_status: Optional[dict] = None,
                    direction_detail: Optional[dict] = None,
                    mode: str = "demo",
                    dark_pool_source: Optional[str] = None,
                    dark_pool_metrics: Optional[dict] = None,
                    options_flow_status: Optional[str] = None,
                    options_flow_source: Optional[str] = None,
                    persistence_sessions: Optional[int] = None) -> dict:
    """Evaluates every named gate for one ticker/scan. Pure function -- no
    DB or network. `mode` ("live" | "demo") selects which inputs are
    legitimate: in Live, the synthetic percentile estimator and the
    (fixture-only) catalyst flag are reported as `unsupported` and can never
    pass; in Demo they are labeled synthetic/fixture-sourced. See the
    status vocabulary above; only INDEPENDENT_SIGNAL_GATES ever count toward
    confidence or lifecycle decisions."""
    live = mode == "live"
    collection_status = dict(collection_status or {})
    gates = {}

    # dark_pool_anomaly ------------------------------------------------------
    dp_metrics = {"ratio": tier1_ratio, "z_score": tier1_z, "bootstrap": tier1_bootstrap}
    dp_metrics.update(dark_pool_metrics or {})
    if not darkpool_available:
        gates["dark_pool_anomaly"] = _gate(False, "unavailable", dark_pool_source or "uw_darkpool", {
            "unavailable": True, "reason": "dark-pool data could not be collected this scan",
            "collection_status": collection_status.get("darkpool")}, "dark_pool_anomaly")
    elif tier1_bootstrap:
        gates["dark_pool_anomaly"] = _gate(False, "insufficient", dark_pool_source or "uw_darkpool",
                                           dp_metrics, "dark_pool_anomaly")
    else:
        hit = bool(tier1_hit)
        gates["dark_pool_anomaly"] = _gate(hit, "pass" if hit else "fail", dark_pool_source or "uw_darkpool",
                                           dp_metrics, "dark_pool_anomaly")

    # options_flow_anomaly ---------------------------------------------------
    of_metrics = dict(options_flow_metrics or {})
    if not options_flow_available:
        gates["options_flow_anomaly"] = _gate(False, "unavailable", options_flow_source or "uw_flow_alerts", {
            "unavailable": True, "reason": "options flow could not be collected this scan",
            "collection_status": collection_status.get("options_flow")}, "options_flow_anomaly")
    else:
        status = options_flow_status or ("pass" if options_flow_hit else "fail")
        gates["options_flow_anomaly"] = _gate(status == "pass", status, options_flow_source or "uw_flow_alerts",
                                              of_metrics, "options_flow_anomaly")
    of_qualified = gates["options_flow_anomaly"]["passed"]

    # historical_percentile (synthetic) ---------------------------------------
    if live:
        gates["historical_percentile"] = _gate(False, "unsupported", "none", {
            "reason": "the static premium-bucket estimator is synthetic and disabled in Live Mode; "
                      "no real trailing-percentile history exists in this build"}, "historical_percentile")
    elif today_premium is None:
        gates["historical_percentile"] = _gate(False, "not_evaluated", "synthetic_estimator",
                                               {"reason": "no premium supplied"}, "historical_percentile")
    else:
        pct = (percentile_gate_fn or synthetic_percentile_gate)(ticker, today_premium)
        metrics = dict(pct)
        metrics["illustrative_percentile"] = metrics.pop("percentile", None)
        metrics["bucket_passed"] = bool(metrics.pop("passed", False))
        if metrics.get("synthetic"):
            metrics.pop("window_days", None)  # the static buckets have no trailing window
        metrics["note"] = ("illustrative only: a fixed premium-size bucket, not a historical percentile; "
                           "zero weight in every decision")
        gates["historical_percentile"] = _gate(False, "synthetic", "synthetic_estimator", metrics,
                                               "historical_percentile")

    # persistence (context only) ----------------------------------------------
    if persistence_sessions is not None:
        gates["persistence"] = _gate(persistence_sessions >= 2, "context", "lifecycle_episode", {
            "qualifying_sessions_in_episode": persistence_sessions,
            "note": "distinct trading sessions with qualifying evidence in the current episode; "
                    "not independent evidence"}, "persistence")
    else:
        gates["persistence"] = _gate(bool(tier2_hit or is_new_qualifying_day), "context", "none", {
            "tier2_qualifying_prints": tier2_qualifying_prints, "tier2_max_pct": tier2_max_pct,
            "is_new_qualifying_day": is_new_qualifying_day}, "persistence")

    # price_confirmation (corroborating) --------------------------------------
    pc_metrics = {"price_move_pct": price_move_pct, "direction_heuristic": direction_heuristic,
                  "confirmed": None,
                  "direction_basis": (direction_detail or {}).get("basis"),
                  "direction_ambiguity": (direction_detail or {}).get("ambiguity"),
                  "call_buy_share": (direction_detail or {}).get("call_buy_share"),
                  "note": "a same-direction price move is coincident, not proof the lean was right"}
    if collection_status.get("stock_state", "ok") != "ok":
        pc_status, pc_passed = "unavailable", False
    elif not (of_qualified and options_flow_available):
        pc_status, pc_passed = "not_evaluated", False
    elif direction_heuristic not in ("bullish-lean", "bearish-lean"):
        pc_status, pc_passed = "not_evaluated", False
    elif price_move_pct is None:
        pc_status, pc_passed = "unavailable", False
    else:
        expected_up = direction_heuristic == "bullish-lean"
        confirmed = (expected_up == (price_move_pct > 0))
        pc_metrics["confirmed"] = confirmed
        pc_status, pc_passed = ("pass" if confirmed else "fail"), confirmed
    gates["price_confirmation"] = _gate(pc_passed, pc_status, "uw_stock_state", pc_metrics, "price_confirmation")

    # catalyst_identified ------------------------------------------------------
    if live:
        gates["catalyst_identified"] = _gate(False, "unsupported", "none", {
            "note": "manual-only gate: this build has no catalyst-verification workflow, so it can "
                    "never pass in Live Mode"}, "catalyst_identified")
    else:
        gates["catalyst_identified"] = _gate(bool(catalyst_identified), "pass" if catalyst_identified else "fail",
                                             "fixture_flag", {"note": "Demo fixture flag, not auto-detected"},
                                             "catalyst_identified")

    # multi_signal_confirmation -----------------------------------------------
    contributing = [g for g in INDEPENDENT_SIGNAL_GATES if gates[g]["passed"]]
    evaluable = [g for g in INDEPENDENT_SIGNAL_GATES if gates[g]["status"] in EVALUABLE_STATUSES]
    multi = len(contributing) >= 2
    gates["multi_signal_confirmation"] = _gate(multi, "pass" if multi else "fail", "derived", {
        "contributing_gates": contributing,
        "evaluable_independent_gates": evaluable,
        "excluded_from_tally": ["historical_percentile", "persistence", "price_confirmation"],
    }, "multi_signal_confirmation")

    # data_quality: did every required collection succeed? -------------------
    failed = sorted(k for k, v in collection_status.items() if v != "ok")
    dq_ok = api_errors == 0 and not failed
    gates["data_quality"] = _gate(dq_ok, "pass" if dq_ok else "fail", "collection", {
        "api_errors": max(api_errors, len(failed)), "failed_sources": failed,
        "collection_status": collection_status}, "data_quality")

    return gates


def independent_gates_passed(gates: dict) -> list:
    return [g for g in INDEPENDENT_SIGNAL_GATES if gates.get(g, {}).get("passed")]


def independent_gates_evaluable(gates: dict) -> list:
    out = []
    for g in INDEPENDENT_SIGNAL_GATES:
        gate = gates.get(g, {})
        status = gate.get("status", "pass" if gate.get("passed") else "fail")
        if status in EVALUABLE_STATUSES:
            out.append(g)
    return out


# --- Confidence --------------------------------------------------------------

def classify_confidence(gates: dict, is_watchlist: bool = False, collection_complete: bool = True) -> tuple:
    """Returns (confidence, signal_type, reason, qualifies).

    Confidence is an ordinal EVIDENCE-COVERAGE label, not a probability and
    not a forecast:
      high          -- >= 2 INDEPENDENT_SIGNAL_GATES passed on this scan
      medium        -- exactly one independent gate passed
      low           -- independent gates were evaluated and none passed
      insufficient  -- required data could not be collected, or no
                       independent gate could be evaluated (missing history /
                       unsupported) -- no confidence is assigned
    Synthetic, context and corroborating gates (historical_percentile,
    persistence, price_confirmation) never raise confidence.
    """
    if is_watchlist:
        return ("manual", "watchlist-manual",
                "Manually marked for tracking (named watchlist ticker)", True)

    if not collection_complete:
        return ("insufficient", "collection_incomplete",
                "Required market data could not be collected this scan -- no confidence is assigned "
                "(a provider failure is not evidence either way)", False)

    multi = gates["multi_signal_confirmation"]
    dp = gates["dark_pool_anomaly"]
    of = gates["options_flow_anomaly"]
    ci = gates["catalyst_identified"]

    if multi["passed"]:
        contributing = multi["metrics"]["contributing_gates"]
        reason = f"Two independent evidence categories passed on this scan: {', '.join(contributing)}"
        return ("high", "multi-signal", reason, True)

    if dp["passed"]:
        ratio = dp["metrics"].get("ratio")
        z = dp["metrics"].get("z_score")
        if ratio:
            z_clause = f", z={z:.1f}" if z is not None else ""
            reason = (f"Single-category dark-pool anomaly only ({ratio:.1f}x avg{z_clause}) -- "
                       "real, but not independently corroborated by a 2nd category")
        else:
            reason = "Single-category dark-pool anomaly only -- not independently corroborated"
        return ("medium", "single-signal-dark-pool", reason, True)

    if of["passed"]:
        return ("medium", "single-signal-options-flow",
                "Single-category options-flow anomaly only -- not independently corroborated "
                "by a 2nd category", True)

    if ci["passed"]:
        return ("medium", "single-signal-catalyst",
                "A catalyst was identified, but with no other independent category also passing", True)

    if not independent_gates_evaluable(gates):
        return ("insufficient", "insufficient_evidence",
                "No independent evidence category could be evaluated on this scan (missing baseline "
                "history, unsupported, or unavailable) -- no confidence is assigned", False)

    if dp.get("status") == "insufficient" or dp["metrics"].get("bootstrap"):
        return ("low", "t1-bootstrap",
                "Dark-pool baseline has insufficient history; the independent categories that could be "
                "evaluated did not pass -- not qualified", False)

    return ("low", "none", "Independent evidence categories were evaluated; none passed", False)


# --- State machine ------------------------------------------------------------

def determine_transition(current_state: Optional[str], gates: dict,
                          qualifying_days_count: int, price_move_pct: Optional[float],
                          direction_heuristic: Optional[str], collection_complete: bool = True) -> tuple:
    """Pure, deterministic. Returns (new_state, rule_id). current_state is
    None for a brand-new ticker (no open qualification record yet).

    SIGNAL-INTEGRITY RULE CHANGE (see this module's docstring, rules 2 and
    4): CONFIRMED is reached/held ONLY via `gates["multi_signal_confirmation"]`
    (>= 2 of INDEPENDENT_SIGNAL_GATES on THIS scan) -- the old
    `or qualifying_days_count >= 2` bypass is removed, since that let a
    single real gate seen on two different calendar dates masquerade as
    independent corroboration. Every call RE-EVALUATES the current scan's
    gates against `current_state`: a ticker already CONFIRMED or STABLE
    from a prior scan (whether reached honestly or, before this rule
    change, via the synthetic gate/old persistence bypass) is demoted the
    moment multi-signal confirmation no longer holds -- to BUILDING if at
    least one real gate still passed this scan, to FADING if none did.
    STABLE additionally still requires multi-signal confirmation to hold on
    the transitioning scan itself (not just historical qualifying days) --
    see docstring rule 5 for the one residual limitation this does NOT
    close (repeated identical real evidence across qualifying days).
    """
    # Incomplete collection (a provider 429/5xx/timeout/malformed body)
    # is not evidence: never promote, demote or invalidate on it.
    if not collection_complete:
        return (current_state, "held_incomplete_collection")

    # Invalidation check first -- overrides everything else.
    if price_move_pct is not None and direction_heuristic in ("bullish-lean", "bearish-lean"):
        expected_up = direction_heuristic == "bullish-lean"
        actual_pct = price_move_pct if expected_up else -price_move_pct
        if actual_pct <= INVALIDATION_ADVERSE_PCT:
            return ("INVALIDATED", f"adverse_price_move<= {INVALIDATION_ADVERSE_PCT}%")

    # Only INDEPENDENT evidence keeps or advances a qualification: the
    # synthetic percentile, the calendar-only persistence gate and the
    # corroborating price_confirmation never do (audit B2/E6).
    any_gate_passed = bool(independent_gates_passed(gates))
    confirmed = gates["multi_signal_confirmation"]["passed"]

    if current_state is None:
        # ZERO-GATE LABEL FIX (2026-10-16, Chad-approved, narrowly scoped):
        # this branch used to return reason "first_qualifying_gate"
        # unconditionally on a ticker's first evaluation, even when
        # `any_gate_passed` is False -- i.e. zero gates passed this scan.
        # The lifecycle STATE "NEW" was always correct here; only the
        # REASON STRING falsely implied a qualifying gate had actually
        # fired (confirmed real-world case: ARTNA/MSEX/CZWI on Windows QA
        # showing zero qualifying gates yet labeled as if qualification
        # had occurred). Reuses the already-computed `any_gate_passed`
        # from above -- no recomputation, no new state, no threshold or
        # confidence changes.
        if any_gate_passed:
            return ("NEW", "first_qualifying_gate")
        return ("NEW", "no_qualifying_evidence_yet")

    if current_state == "NEW":
        if confirmed:
            return ("CONFIRMED", "multi_signal_confirmation_two_independent_categories")
        # L1 FIX (2026-10-16, Chad-approved, narrowly scoped -- see this
        # module's docstring rule 6 below): this branch used to return
        # BUILDING unconditionally, so a second scan with ZERO qualifying
        # gates on the CURRENT evaluation still promoted NEW -> BUILDING.
        # `any_gate_passed` is a real structural guard against that --
        # promotion now requires at least one genuine gate to have passed
        # on THIS scan, matching the guard the BUILDING/CONFIRMED/STABLE
        # branches below already use for their own demotions.
        if any_gate_passed:
            return ("BUILDING", "single_signal_reevaluated")
        return ("NEW", "no_gate_passed_remains_new")

    if current_state in ("BUILDING",):
        if confirmed:
            return ("CONFIRMED", "multi_signal_confirmation_two_independent_categories")
        if not any_gate_passed:
            return ("FADING", "no_gate_passed_while_building")
        return ("BUILDING", "still_single_signal")

    if current_state == "CONFIRMED":
        if confirmed and qualifying_days_count >= STABLE_MIN_QUALIFYING_DAYS and gates["price_confirmation"]["passed"]:
            return ("STABLE", f">= {STABLE_MIN_QUALIFYING_DAYS}_qualifying_days_and_price_confirmed_and_still_multi_signal")
        if not confirmed:
            if any_gate_passed:
                return ("BUILDING", "demoted_lost_multi_signal_confirmation")
            return ("FADING", "no_gate_passed_while_confirmed")
        return ("CONFIRMED", "still_confirmed")

    if current_state == "STABLE":
        if not confirmed:
            if any_gate_passed:
                return ("BUILDING", "demoted_lost_multi_signal_confirmation")
            return ("FADING", "no_gate_passed_while_stable")
        return ("STABLE", "still_stable")

    if current_state == "FADING":
        if confirmed:
            return ("CONFIRMED", "reheated_multi_signal")
        if any_gate_passed:
            return ("BUILDING", "reheated_single_signal")
        return ("FADING", "still_fading")

    if current_state == "INVALIDATED":
        return ("INVALIDATED", "terminal_state")

    return (current_state, "no_rule_matched_state_unchanged")
