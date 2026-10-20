# Gate-by-gate proof (Live and Demo)

For each gate, this lists the real data source, the calculation, the runtime wiring, and the tests that prove
its pass, fail, missing-data and stale-data behaviour. File references are relative to `backend/`.

**Test environment.** Every test runs offline:
- a fake UW session (`tests/uw_fakes.py`), or
- a cursor-honouring paginated fake (`tests/uw_paginated.py`), or
- the recorded-data HTTP replay (`tools/uw_replay_server.py`).

The pytest suite refuses any non-loopback connection (`tests/conftest.py`).

---

## dark_pool_anomaly (independent)

| | |
|---|---|
| **Source** | UW `/api/darkpool/{T}?date=&older_than=&limit=500`, paged by `app/session_collection.py` and cataloged (`ingestion.darkpool_observations`, deduplicated by `tracking_id`, canceled prints excluded on read) |
| **Calculation** | `app/core/session_baseline.compute(..., "darkpool")`: session-to-covered-point premium vs the same elapsed slice of ≥ 20 prior complete sessions; ratio ≥ 3 and percentile ≥ 0.95 |
| **Runtime wiring** | `main._run_scan` → `_collect_session_dataset("darkpool")` → `_session_baseline_gate_inputs` → `scan_service.build_gate_inputs(mode="live")` → `signal_engine.evaluate_gates` |
| **Live** | supported, once ≥ 20 complete prior sessions are collected (backfill: scheduler / route) |
| **Demo** | fixture `avg_premium_baseline`, source `demo_fixture_baseline` |

Proof:
- **pass:** `test_darkpool_session_baseline::test_refresh_after_backfill_evaluates_a_real_pass`
- **fail:** `::test_normal_activity_fails_rather_than_passing`
- **insufficient:** `::test_too_few_prior_sessions_is_insufficient_not_fail`,
  `::test_without_any_backfill_a_single_refresh_is_insufficient`,
  `::test_partial_evidence_session_coverage_cannot_qualify`,
  `::test_recorded_real_capture_cannot_establish_a_baseline`
- **unavailable:** `::test_provider_failure_during_collection_is_unavailable_and_keeps_coverage`,
  `test_collection_status`
- **stale / duplicate:** `::test_coverage_already_current_makes_no_request`,
  `::test_canceled_prints_are_excluded_and_tracking_id_dedupes`,
  `test_lifecycle_episodes::test_identical_evidence_on_a_later_session_is_a_stale_reread_and_is_held`
- **no look-ahead:** `::test_future_sessions_are_never_used_as_history`
- **unverifiable provider behaviour:** `::test_pagination_cursor_not_honored_is_unknown_not_complete`,
  `::test_running_out_of_data_is_partial_but_a_stuck_full_page_is_unknown`

---

## options_flow_anomaly (independent)

| | |
|---|---|
| **Source** | UW `/api/option-trades/flow-alerts?ticker_symbol=&older_than=&limit=200`, paged and cataloged the same way; `older_than` is UW-documented and every page is checked to verify it is honoured |
| **Calculation** | `session_baseline.compute(..., "options_flow")`: the same method as dark pool, on alert `total_premium` |
| **Runtime wiring** | `_collect_session_dataset("options_flow")` → `_options_baseline` → `build_gate_inputs(options_flow_baseline=...)` |
| **Live** | supported (same history requirement). The old snapshot rule is never used in Live |
| **Demo** | snapshot threshold over fixture alerts, source `demo_fixture_snapshot_threshold` |

Proof (`test_options_session_baseline`):
- **pass:** `::test_unusually_heavy_options_activity_passes`
- **fail:** `::test_normal_options_activity_fails`
- **insufficient:** `::test_short_history_is_insufficient`,
  `::test_recorded_real_scans_no_longer_pass_by_default` (41/41 recorded scans: 05c47ca passed every one; now
  insufficient)
- **unavailable:** `test_collection_status::test_outage_holds_state_and_writes_no_lifecycle_history`
- **coverage proof:** `::test_options_coverage_reaches_the_open_by_paging_into_the_prior_session`

---

## catalyst_identified (independent, manual only)

| | |
|---|---|
| **Source** | Demo fixture flag only; there is no catalyst workflow in this build |
| **Live** | **unsupported**: status `unsupported`, can never pass |
| **Demo** | `pass`/`fail` from fixture, source `fixture_flag` |

Proof: `test_synthetic_zero_weight::test_catalyst_flag_cannot_pass_in_live`.

---

## price_confirmation (corroborating, zero confidence weight)

| | |
|---|---|
| **Source** | UW `/api/stock/{T}/stock-state`: close vs prev_close (`_normalize_stock_state`), plus the price move since the episode reference price for invalidation |
| **Calculation** | Does the price move's sign match the inferred lean (`core/flow_direction.py`)? Evaluated only when options flow qualified |
| **Statuses** | `pass` / `fail`; `not_evaluated` (no qualifying flow, or ambiguous lean); `unavailable` (stock-state failed) |

Proof:
- `test_flow_direction` (PLTR regression on real data),
- `test_signal_engine::TestEvaluateGates::test_price_confirmation_*`,
- `test_synthetic_zero_weight::test_price_confirmation_alone_cannot_hold_a_qualification`,
- `test_e2e_live_replay_cdp.py` (rendered "Price data could not be collected").

---

## historical_percentile (synthetic, zero weight)

| | |
|---|---|
| **Live** | `unsupported`, not computed |
| **Demo** | `synthetic`: illustrative premium bucket, `passed` always False, no trailing-window claim |

Proof: `test_synthetic_zero_weight::*`, `test_signal_engine::test_historical_percentile_*`,
`test_signal_integrity_ui.mjs`.

---

## persistence (context, zero weight)

| | |
|---|---|
| **Source** | Lifecycle episode session evidence (`lifecycle_session_evidence`) |
| **Calculation** | Number of distinct NYSE sessions with qualifying evidence in the current episode; `context` status |

Proof: `test_lifecycle_episodes`.

---

## multi_signal_confirmation (derived)
At least 2 independent gates must pass on the same scan.

Proof:
- `test_options_session_baseline::test_full_pipeline_can_earn_high_confirmed_and_stable_on_genuine_multi_session_evidence`
  (constructed multi-week histories through the real pipeline: HIGH → CONFIRMED → STABLE over 3 sessions),
- `::test_one_anomalous_category_alone_stays_medium_and_never_confirms`.

---

## data_quality (meta, zero weight)
Passes when every required collection succeeded; lists the failed sources otherwise.

Proof: `test_signal_engine::test_data_quality_fails_when_a_collection_failed`, `test_collection_status`.

---

## End to end on recorded real data
- `tests/test_e2e_live_replay.py` runs over a real HTTP socket against the recorded capture, through the real
  Settings flow.
- `frontend/test_e2e_live_replay_cdp.py` checks the rendered UI.
- On that data every independent gate is honestly `insufficient`, because no session in the capture has
  verified coverage. This is asserted, not worked around.
