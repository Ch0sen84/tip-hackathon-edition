# Signal-integrity rules

This document covers the confirmation, confidence and lifecycle semantics as implemented after the
signal-integrity remediation (branch `fix/signal-integrity-remediation`, based on 05c47ca). Code is the
source of truth:
- `backend/app/core/signal_engine.py` (gates, confidence)
- `backend/app/core/lifecycle.py` (episodes)
- `backend/app/core/session_baseline.py` and `backend/app/session_collection.py` (real baselines)
- `backend/app/core/flow_direction.py` (direction)

Companion documents: [GATE_PROOF.md](GATE_PROOF.md) (gate by gate), [TRADING_VALIDITY.md](TRADING_VALIDITY.md)
(what outputs mean), [RELEASE_MIGRATION_ROLLBACK.md](RELEASE_MIGRATION_ROLLBACK.md). Upgrade and rollback:
[RELEASE3_RECOVERY_RUNBOOK.md](RELEASE3_RECOVERY_RUNBOOK.md) (authoritative).

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
    - `complete`: `[open, close]` proven **and certified** (see *Settlement certification* below)
    - `partial`: `session_in_progress`, `page_budget_exhausted`, `awaiting_settlement`,
      `suspected_provider_gap`
    - `unknown`: `invalid_rows`, `bounds_not_honored`, `wrong_session_date`, `rows_out_of_order`,
      `cursor_stuck`
    - `unavailable`: `provider_history_cutoff` (UW's 403 `historic_data_access_missing` only).
      `not_entitled` appears only on rows written by earlier releases; such a session is probed again.
  - A transient provider failure leaves coverage unchanged. **401 and any other 403 are transient
    failures** (a revoked or mistyped key must not mark history unavailable).
  - A **local** catalog write failure (disk full, locked database, storage guard) is reported as a failed
    collection (`catalog_write_failed`) and the stored coverage is kept unchanged.
  - **Rejected pages.** Every row of a page is validated before any row is stored. One invalid row rejects the
    whole page: nothing from it is stored, and an invalid short page never proves a window exhausted.
    - `invalid_rows`: a row that is not an object, carries another ticker, or has a missing or unparseable
      timestamp.
    - `bounds_not_honored`: a row outside the requested bounds. `wrong_session_date`: a dark-pool row off the
      session date.
    - `rows_out_of_order`: rows that are not newest-first. Equal timestamps in any order are fine. The cursor
      relies on a full page being the *newest* rows of its window.
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
  - **Settlement certification (migration 11).** UW can report prints late. A session therefore becomes
    `complete` only through a full `[open, close]` walk that **starts at or after close + settlement lag**
    (`TIP_COVERAGE_SETTLE_MINUTES`, default 180, clamped 30..1440).
    - Before that, a finished walk is `partial / awaiting_settlement`. After it, a session that is unsettled,
      legacy (see below) or suspected of a gap is walked again **from scratch**, not extended.
    - **Gap check.** Every run of at least `TIP_COVERAGE_GAP_MIN_MINUTES` (default 5) empty minutes is judged
      against the activity in the 30 minutes on each side of it (the quieter side where both exist, so a busy
      opening followed by a quiet day is not an outage). It is suspicious when (a) the local rate of *events*
      (a run of consecutive active minutes counts once, so a burst of prints is one event) or (b), for a ticker
      active in ≥ 70% of those minutes, the local row rate (capped at 10 rows per minute) predicts more than
      `ln(events or rows in the session) + 5.5` inside the gap -- under a Poisson model the chance that any
      natural gap of a session reaches that is about 0.4% -- or (c) the gap spans more than half of the session
      and the session has ≥ 50 rows. A suspicious session is withheld (`suspected_provider_gap`) and skipped by
      backfills and jobs until `TIP_COVERAGE_GAP_CONFIRM_HOURS` (default 12) after the suspicion; a later
      settled walk certifies it (`gap_flag = confirmed`) if it finds the same data, so nothing is stuck.
    - **Measured operating characteristics** (synthetic sessions: Poisson prints with a U-shaped intraday
      profile, optionally clustered, with a closing cross or a 4× lunch lull; 100–150 trials per cell):
      - outages missed: liquid tickers (≥ 1,500 prints/day) ~0% for outages of ≥ 10 minutes anywhere in the
        session (a 5-minute midday hole, ~0.7% of the rows, is not caught); 400 prints/day: hour-long outages
        and outages near the open or close that lose ≥ 6–9% of the rows are caught, 10–30-minute midday
        outages (1–4% of the rows) are not; ≤ 150 prints/day: only large losses are caught (an outage cannot
        be told from a quiet day).
      - genuine sessions withheld once: ~0–6% for ordinary profiles; up to ~15–22% for the harshest
        synthetic profiles (strong clustering with a closing cross, or a 4× lunch lull). Each costs 12 hours
        and one more settled walk, never a wrong certification.
    - **Provenance.** Each certified row records `certified_by` (`settled-v1`), `certified_at_utc`,
      `proof_started_utc`, `session_rows`, `max_gap_s` and, for a re-walk, what triggered it and how many
      rows were stored before. Every post-settlement re-walk is logged in `coverage_reconciliations` with the
      number of rows it newly found (`GET /api/diagnostics/coverage-reconciliations`, local, zero UW calls).
    - **Legacy rows.** A `complete` row written by an earlier release has no certifier. It is shown as
      `complete:legacy_uncertified`, is **not** counted toward any baseline and is **never modified** until
      an approved baseline job or reconciliation re-walks it.
    - **Reconciliation.** `POST /api/ticker/{t}/baseline/reconcile` (authenticated, Live only, one prior
      settled session, at most 200 pages, confirmation required) re-walks a session even if it is
      certified. It replaces the stored row only if the re-walk certifies; otherwise nothing changes.
    - **Guarantee.** After certification, the catalog holds every row the provider reported for the session
      by `proof_started_utc` and nothing that was not reported by `certified_at_utc` (verified against an
      authoritative synthetic ledger, including a 24-seed property test). A row first reported *during* a
      multi-step settled walk, or after it, is found by a later reconciliation.
  - **Cancellations.** A print later re-reported as canceled is stored as an additional canceled version;
    every version of a canceled print is excluded from sessions and baselines. Corrections (a changed price
    or size under the same identity) are not detected: UW's behaviour is unverified and `tracking_id` is not
    unique.
  - **Rejected sessions** (every retry rejected) are skipped by backfills for 24 hours, then walked again. A
    rejection on the last page of a budget is treated as an exhausted budget, not as a rejected session.
  - **Session window.** `[open, close]` is inclusive, as the baseline reads it. A dark-pool walk that reaches
    the close requests `older_than = close + 1 s` (exclusive, second resolution), so prints stamped exactly
    at the close are collected. While a session is in progress, the live edge stays `now − 60 s` (exclusive),
    floored to a whole second for dark pool.
  - **Resuming an interrupted walk.** When a walk stops on its page budget, the checkpoint's `covered_from`
    is the bound the next request would have used, not the oldest row seen.
    - Dark pool: the oldest second + 1 s. Prints sharing that second may not all have fit on the last page,
      and the resumed window `[open, covered_from)` re-reads them.
    - Flow: the oldest instant, which the resumed inclusive window re-reads.
    - A zero page budget makes no request and writes no coverage.
  - **Page budgets are exact.** A step's logical pages are split between datasets by quotient and remainder,
    so allocations never exceed the remaining budget. With 1 page left and 2 datasets, one gets 1 and the
    other 0. A dataset allocated 0 pages makes no request but still reports its status. This holds for jobs,
    resumed jobs and `POST /api/ticker/{t}/baseline/backfill`.
    - Job progress reports logical pages (`pages_used`) and HTTP attempts (`http_attempts`, which include
      transport-level retries) separately.
    - A job charges a step's pages before the step's first request and settles them to the pages actually
      used afterwards. An error or restart mid-step can therefore only under-spend: the reserved pages stay
      charged, and a resume never exceeds `max_pages_per_ticker`.
    - Page budgets below 1 are refused (HTTP 400) with no request.
  - **Baseline jobs** stop requesting a dataset once it has the job's target of complete prior sessions,
    and spend later steps on the other dataset. Eligibility, the 30-session lookback and the 20-session
    requirement are unchanged. Current-session evidence is still collected by every Live refresh.
  - Sessions before a ticker's first observed activity (pre-listing) are excluded from the baseline.
  - **Only certified sessions count.** Legacy, partial and awaiting-settlement sessions are excluded, and the
    report says how many legacy sessions are waiting for re-verification.
  - **Implausibly empty sessions.** A certified session with zero activity *after* the ticker's first activity
    is excluded (`excluded_implausible_empty`) when the median active session has ≥ 20 rows; for thin tickers
    a quiet day stays a genuine zero.
  - **Market calendar.** Unscheduled closures (e.g. national days of mourning, 2012 storm days, 2025-01-09)
    are not trading sessions; more can be added with `TIP_EXTRA_MARKET_CLOSURES` (comma-separated dates).
- **No look-ahead.** Only sessions strictly before the evidence session are eligible.
- **Data hygiene.** Dark-pool prints are identified by `(tracking_id, executed_at, price, size, volume)`.
  UW `tracking_id` alone is **not** unique (verified live). Canceled prints are excluded.
- **How coverage grows:**
  - every Live refresh (small page budget for the evidence session),
  - approved baseline jobs (`POST /api/baseline/jobs`; the server answers 428 with the total page budget until
    the request is confirmed),
  - `POST /api/ticker/{t}/baseline/backfill` (authenticated, budget-clamped),
  - reconciliations (above),
  - the scheduler's rotating watchlist backfill **only if** `TIP_SCHEDULER_BASELINE_BACKFILL=1`, and only on
    recurring evaluating cycles; never from a one-time run. It is off by default.
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
- **After upgrading to schema 11, existing sessions are legacy.** Baselines report `insufficient` until
  approved baseline jobs re-verify ≥ 20 sessions per ticker and dataset. Nothing is re-walked automatically.
- **Settlement lag and gap thresholds are heuristics.** UW's late-report window is not documented; 180 minutes
  covers what was observed. Reconciliation is the remedy for anything later.
- **Corrections** of already-stored prints are not detected (see *Cancellations*).
