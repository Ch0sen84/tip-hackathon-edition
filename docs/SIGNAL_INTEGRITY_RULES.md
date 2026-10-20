# Signal-integrity rules

This document covers the confirmation, confidence and lifecycle semantics as implemented after the
signal-integrity remediation (branch `fix/signal-integrity-remediation`, based on 05c47ca). Code is the
source of truth:
- `backend/app/core/signal_engine.py` (gates, confidence)
- `backend/app/core/lifecycle.py` (episodes)
- `backend/app/core/session_baseline.py` and `backend/app/session_collection.py` (real baselines)
- `backend/app/core/flow_direction.py` (direction)

Companion documents: [GATE_PROOF.md](GATE_PROOF.md) (gate by gate), [TRADING_VALIDITY.md](TRADING_VALIDITY.md)
(what outputs mean), [RELEASE_MIGRATION_ROLLBACK.md](RELEASE_MIGRATION_ROLLBACK.md).

## 1. Missing data is never negative evidence
- **Explicit collection status.** Every UW request reports one of `ok`, `error`, `rate_limited`, `unauthorized`,
  `unavailable_on_tier`, `malformed` or `circuit_open`. Only `ok` rows are a real answer.
- **What a failed source does.** It makes its gate `unavailable`. Confidence becomes `insufficient` and the
  lifecycle is **held**: no promotion, demotion or invalidation, and no lifecycle history row. The attempt is
  still recorded in the catalog with its per-source status.
- **What the API and UI show.** Freshness `collection_failed` ("Data collection failed"), never "Current".
  `last_successful_collection_at` is reported separately from `last_evaluated_at`.
- **Retries** stay bounded (3 attempts) under the shared concurrency gate and per-endpoint circuit breaker.

## 2. Gate status and decision weight
- **Status.** Every gate reports `status`: `pass` / `fail` / `insufficient` / `unavailable` / `unsupported` /
  `synthetic` / `not_evaluated` / `context`.
- **Independent gates.** Only `INDEPENDENT_SIGNAL_GATES` = `dark_pool_anomaly`, `options_flow_anomaly` and
  `catalyst_identified` carry decision weight.
- **Corroborating only.** `price_confirmation`. It never raises confidence and never keeps a qualification
  alive on its own.
- **Zero weight.** `historical_percentile` (synthetic: illustrative in Demo, `unsupported` in Live) and
  `persistence` (context: sessions with qualifying evidence in the current episode).

## 3. Confidence is an evidence-coverage label, not a probability
| confidence | meaning |
|---|---|
| high | two independent categories passed on this scan |
| medium | exactly one independent category passed |
| low | independent categories were evaluated; none passed |
| insufficient | collection failed, or no independent category could be evaluated (e.g. no baseline history) |

No confidence value is calibrated or predictive (see [TRADING_VALIDITY.md](TRADING_VALIDITY.md)).

## 4. Real baselines (Live)
`dark_pool_anomaly` and `options_flow_anomaly` use the same method in Live:
- **The comparison.** This session's cumulative premium (dark-pool prints; UW flow-alert premium) from the
  regular-session **open** up to the **verified** covered point, against the same elapsed slice of **≥ 20**
  earlier **complete** sessions. Each earlier session is cut at its own close on early-close days.
- **Pass condition** (thresholds unchanged from Phase C): ratio ≥ 3× the mean **and** percentile ≥ 0.95.
- **Otherwise** `insufficient` (missing history or unverified coverage) or `fail`.
- **Coverage must be proven by pagination, never assumed.** Each page's cursor and date scoping are checked;
  coverage reaches the open only when a row at or before the open is seen; an empty first page proves
  nothing. A provider failure leaves coverage unchanged.
- **No look-ahead.** Only sessions strictly before the evidence session are eligible.
- **Data hygiene.** Dark-pool prints are deduplicated by UW `tracking_id`; canceled prints are excluded.
- **How coverage grows:**
  - every Live refresh (small page budget for the evidence session),
  - each evaluating scheduler cycle (bounded, rotating watchlist backfill),
  - `POST /api/ticker/{t}/baseline/backfill` (authenticated, budget-clamped).
  `GET /api/ticker/{t}/baseline` reports coverage with zero UW calls.
- **Demo** uses labeled fixture inputs (`demo_fixture_baseline`, `demo_fixture_snapshot_threshold`).

## 5. Direction (inferred, not observed)
- **Observed:** premium at the ask and at the bid, split by calls and puts.
- **Inferred lean:** from aggressor **buying** in single-leg alerts only, call-ask vs put-ask premium, at
  0.65 / 0.35.
- **No lean** when buying is mixed, absent or mostly unsided, or when selling dominates. Selling is reported
  but not used, because covered calls, cash-secured puts and closing trades are indistinguishable from
  directional selling.
- `price_confirmation` and invalidation use the lean only when options flow **qualified** on that scan.

## 6. Lifecycle: qualification episodes, counted in NYSE trading sessions
- **Session attribution.** Evidence belongs to the most recent NYSE session that had opened by scan time.
  Weekend, holiday and pre-open scans belong to the previous session.
- **Episodes.** An episode opens on the first complete scan with qualifying independent evidence. A ticker
  without one has no state (UI: "No active qualification"); it is never shown as NEW.
- **Transitions:**
  - NEW → BUILDING needs qualifying evidence on a **later** session.
  - STABLE needs two-category confirmation on **≥ 3 distinct sessions of the same episode**, plus price
    corroboration.
  - Identical evidence already seen in an earlier session of the episode is held as a stale re-read.
- **Invalidation and expiry:**
  - INVALIDATED (a ≥ 3% move against the lean, measured from the episode's reference price) closes the
    episode. A new episode can open only on a later session (rule `new_episode_after_invalidation`).
  - Episodes expire after 5 trading sessions without qualifying evidence.
- **Storage.** Lifecycle data lives in migration-7 tables, separately in the Live DB (`TIP_DB_PATH`) and the
  Demo DB (`TIP_DEMO_DB_PATH`). The legacy `ticker_scan_log` / `ticker_current_state` tables are untouched
  audit history and are no longer read.

## 7. Demo vs Live
- **Separate SQLite files:** Demo state, history and seeds can never reach the Live database.
- **Read-only contamination check:** `tools/audit_demo_live_contamination.py` reports pre-existing
  Demo-attributed rows in an older Live database.

## Known limitations
- **HIGH/CONFIRMED in Live** requires ≥ 20 complete prior sessions of verified coverage for *both* real
  gates. A fresh install, or the recorded 2026-10-15..17 capture, honestly yields `insufficient`.
- **`older_than` on flow-alerts** is UW-documented but not live-verified by this project. If UW ignores it,
  the collector marks coverage `unknown` and the options gate stays `insufficient`, rather than guessing.
- **Thinly traded tickers:** the oldest available session, or a ticker with no pre-open dark-pool print,
  cannot be proven complete. That is a conservative choice.
- **`catalyst_identified` is unsupported in Live** (no verification workflow).
