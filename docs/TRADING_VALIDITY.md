# Trading validity: what TIP outputs are, and are not

**Technical correctness is not predictive usefulness.** The test suite proves the pipeline computes what it
says it computes on the data it actually has. Nothing in this repository shows that any TIP output predicts
returns.

## Factual observations (as reported by Unusual Whales)
- Flow-alert rows: time, option type, premium, ask-side / bid-side premium, sweep and multi-leg flags.
- Dark-pool prints: time, price, size, premium, `tracking_id`, canceled flag.
- Stock-state close / prev close and the daily change derived from them.
- OHLC bars.
- Per-source collection status and timestamps: what was fetched, when, and whether it succeeded.
- Verified session coverage: which part of a session has actually been collected.

## Descriptive statistics (computed facts, not predictions)
- **Session-baseline comparisons:** this session's cumulative premium vs the same time of day in earlier
  fully collected sessions (ratio, percentile, z-score, sample size).
- **Observed premium splits** (call/put × ask/bid).

## Heuristic classifications (rules, not validated models)
- **Gate pass/fail** at fixed thresholds (ratio 3×, percentile 0.95, ≥ 20 sessions). These were carried over
  from earlier phases and **not tuned** on outcomes.
- **The inferred directional "lean"** from call-vs-put aggressor buying. Flow data cannot tell opening trades
  from closing ones, or hedges from speculation.
- **Confidence (`high` / `medium` / `low` / `insufficient`).** An ordinal label for how many independent
  evidence categories passed. **It is not a probability**, and the UI says so.
- **Lifecycle states (NEW / BUILDING / CONFIRMED / STABLE / FADING / INVALIDATED).** These describe how
  evidence accumulated across trading sessions, not what price will do. "INVALIDATED" means the price moved
  ≥ 3% against the inferred lean; it says nothing about whether the flow was "wrong".

## No demonstrated predictive validity
- There is no backtest, out-of-sample evaluation, hit-rate or calibration study in this repository.
- A price move that agrees with the inferred lean (price_confirmation) is **coincident**, not evidence that
  the inference was right.
- The demo scenarios and all constructed test histories are synthetic and say nothing about markets.

## What it would take to make predictive claims
1. Collect verified session coverage over many months and tickers (the backfill machinery exists).
2. Log forward returns per state and confidence at fixed horizons, with sample sizes.
3. Evaluate out of sample against simple baselines (e.g. unconditional drift, volume-only rules), with
   multiple-testing control.
4. Only then attach calibrated language. Until then, keep all labels descriptive.

## Using it for trading research today
TIP is a real-data **evidence collector and viewer** with transparent, rule-based classifications:
- It reliably tells you what UW reported, what was and wasn't collected, and how a session compares with its
  own collected history.
- Treat every label as a pointer for further research, never a signal to trade on by itself.
