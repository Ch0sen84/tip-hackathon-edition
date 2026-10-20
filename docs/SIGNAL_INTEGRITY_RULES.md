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
- **Coverage must be proven by pagination, never assumed** (contract verified live:
  [UW_PAGINATION_VERIFICATION.md](UW_PAGINATION_VERIFICATION.md)).
  - Every request is a **bounded window** (`newer_than` / `older_than`). A page shorter than the limit,
    including an empty one, proves the window exhausted. So a quiet or thin session can be complete without
    a print at the open.
  - Each page's bounds, session date and cursor progress are checked.
  - Statuses (with reason codes):
    - `complete`
    - `partial`: `session_in_progress`, `page_budget_exhausted`
    - `unknown`: `invalid_rows`, `bounds_not_honored`, `wrong_session_date`, `cursor_stuck`,
      `catalog_write_failed`
    - `unavailable`: `provider_history_cutoff`, `not_entitled`
  - A transient provider failure leaves coverage unchanged.
  - **Rejected pages.** Every row of a page is validated before any row is stored. One invalid row rejects the
    whole page: nothing from it is stored, and an invalid short page never proves a window exhausted.
    - `invalid_rows`: a row that is not an object, carries another ticker, or has a missing or unparseable
      timestamp.
    - `bounds_not_honored`: a row outside the requested bounds. `wrong_session_date`: a dark-pool row off the
      session date.
    - The same window is re-requested at most `TIP_BOUNDS_RETRIES_PER_WINDOW` times **per page** (default 1,
      max 3, 0 = off). The allowance resets only when an accepted page advances the cursor. Each retry
      counts against the page budget.
    - Only a page that passes every check advances the cursor. If every retry for a page is rejected, the
      session stays `unknown` with that reason, and backfills skip it.
    - Each rejection is recorded as a sanitized diagnostic: request bounds, observed min/max timestamps,
      violation counts, at most 5 samples, and a classification *hypothesis*. Samples of out-of-window rows
      carry the timestamp only; samples of invalid rows carry their position and value type only. It never
      includes prices, sizes, ids, raw rows or pages, or credentials. Read it with
      `GET /api/diagnostics/collection-bounds` (local, zero UW calls). Nothing reads diagnostics to decide
      coverage, eligibility or signals.
  - **Page budgets are exact.** A step's logical pages are split between datasets by quotient and remainder,
    so allocations never exceed the remaining budget. With 1 page left and 2 datasets, one gets 1 and the
    other 0. A dataset allocated 0 pages makes no request but still reports its status. This holds for jobs,
    resumed jobs and `POST /api/ticker/{t}/baseline/backfill`.
    - Job progress reports logical pages (`pages_used`) and HTTP attempts (`http_attempts`, which include
      transport-level retries) separately.
  - **Baseline jobs** stop requesting a dataset once it has the job's target of complete prior sessions,
    and spend later steps on the other dataset. Eligibility, the 30-session lookback and the 20-session
    requirement are unchanged. Current-session evidence is still collected by every Live refresh.
  - Sessions before a ticker's first observed activity (pre-listing) are excluded from the baseline.
- **No look-ahead.** Only sessions strictly before the evidence session are eligible.
- **Data hygiene.** Dark-pool prints are identified by `(tracking_id, executed_at, price, size, volume)`.
  UW `tracking_id` alone is **not** unique (verified live). Canceled prints are excluded.
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
- **Pagination semantics** for both endpoints were verified with 10 bounded live requests (2026-10-20). If
  UW ever changes them, the per-page checks mark coverage `unknown` rather than guessing.
- **Basic plan rate limits and the exact history cutoff** were not probed (≥ ~30 sessions verified
  accessible).
- **`catalyst_identified` is unsupported in Live** (no verification workflow).
