# Signal-integrity rules (2026-10-15)

Short reference for the confirmation/confidence semantics implemented in
`backend/app/core/signal_engine.py`. Read that file's module docstring for
the full rationale -- this is the summary.

## 1. The synthetic `historical_percentile` gate has zero weight

`historical_percentile` (`synthetic_percentile_gate`) is a static
premium-size-bucket estimator, not a real trailing-history calculation.
It is still computed and still shown in the UI (clearly labeled "Synthetic
baseline estimate ... not real historical data"), but it:

- never counts toward `multi_signal_confirmation`'s 2-category tally,
- never produces "high" or "medium" confidence on its own,
- never counts toward `tip_evidence.categories()`'s "Highest Attention"
  (>= 2 categories) tally either.

A ticker whose only passing gate is this synthetic one stays `low`
confidence / non-qualifying, exactly like a ticker with no gates passing.

## 2. CONFIRMED requires 2 genuinely independent real categories

`INDEPENDENT_SIGNAL_GATES = (dark_pool_anomaly, options_flow_anomaly,
catalyst_identified)`. These are the only three gates in this build whose
"passed" boolean is derived from a genuinely separate real-world input:

| gate | real data source |
|---|---|
| `dark_pool_anomaly` | UW dark-pool trade records |
| `options_flow_anomaly` | UW option-trades / flow-alerts |
| `catalyst_identified` | a manually-verified real catalyst (never auto-detected) |

`price_confirmation`, `persistence`, and `historical_percentile` are
excluded from this list:

- `price_confirmation`'s direction input is computed from the SAME
  options-flow ask/bid split that `options_flow_anomaly` reads
  (`scan_service._direction_heuristic`) -- counting it as a 2nd
  independent category would let a single options-flow anomaly reach
  CONFIRMED by itself, which is exactly the failure mode this rule change
  closes. It still counts as real evidence for `any_gate_passed`
  bookkeeping and the STABLE transition's price check.
- `persistence` is satisfied by `is_new_qualifying_day` -- a fact about
  *when* a scan happened, not an independently-sourced observation.
- `historical_percentile` per rule 1.

CONFIRMED (and "high" confidence) require `multi_signal_confirmation`,
i.e. >= 2 of `INDEPENDENT_SIGNAL_GATES` passing on the SAME scan. A single
real gate -- even a strong dark-pool ratio, even a confirmed price
reaction -- caps at "medium" confidence and a non-CONFIRMED lifecycle
state.

## 3. No manufacturing confirmation via repetition

`determine_transition`'s old `qualifying_days_count >= 2` bypass to
CONFIRMED is removed. Repeated same-day scans were already prevented from
inflating `qualifying_days_count` (`COUNT(DISTINCT scan_date)` in
`storage.py`); this closes the remaining path where the SAME single real
gate, seen on two different calendar dates, used to be treated as "2
independent categories."

Every call to `determine_transition` re-evaluates the CURRENT scan's gates
against whatever state was persisted. A ticker sitting in CONFIRMED or
STABLE from a prior scan (reached honestly, or under the old rules) is
demoted to BUILDING the moment `multi_signal_confirmation` no longer
passes (to FADING if no gate passes at all). `ticker_scan_log` /
`ticker_current_state` rows are never deleted or rewritten -- only the
forward-looking transition decision changes.

**Known, deliberately-not-fixed residual case**: CONFIRMED -> STABLE still
also requires `qualifying_days_count >= 3`, so a ticker could reach STABLE
after 3 calendar days of the exact same unchanged 2-category evidence
repeating verbatim (e.g. a stale/duplicate data window). Closing that
would require fingerprinting the evidence per qualifying day -- new
infrastructure, out of scope for this pass.

## 4. Dark-pool anomaly: no synthetic baseline substitute

`dark_pool_anomaly` only passes when a real trailing-average baseline is
present (`avg_premium_baseline` on at least one trade). Real UW dark-pool
trade records (as returned by `LiveUWAdapter.get_darkpool_trades`) never
carry that field, so in Live Mode this gate always reports
`bootstrap: True` / `passed: False` -- "insufficient history", never a
fabricated PASS -- regardless of how large the real premium is. Only
demo/fixture data (explicitly, visibly carrying `avg_premium_baseline`)
can make this gate genuinely pass; this is a fixture-authoring convenience
for demos, not a Live Mode capability. See
`backend/tests/test_dark_pool_no_synthetic_baseline.py`.

Real dark-pool counts/premiums/timestamps are still shown (that data is
real and observed) -- only "above normal"/"anomaly" language requires the
gate to have actually passed.

### Future real baseline (feasibility note, not implemented)

The local SQLite `ticker_scan_log` table already records one row per scan
per ticker with a date, but does NOT currently store the raw premium
figure observed that scan -- only `any_gate_passed` and `state`. A real
multi-day baseline would need (a) a new column/table to persist the raw
daily dark-pool premium per ticker, (b) enough real trading days
accumulated per ticker to make a trailing average statistically
meaningful (this demo-scale SQLite store, in a fresh install, starts with
zero history), and (c) a decision on window length/weighting. This is
plausible as a future addition but was NOT built this pass per Decision 2
(no full historical-baseline engine).
