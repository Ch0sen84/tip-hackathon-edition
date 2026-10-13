# synthetic: true

**Every file in this directory named `scenario_*.json` is FABRICATED DEMO
DATA.** No fixture here was downloaded from, derived from, or represents any
real Unusual Whales API response, any real ticker's actual trading activity,
or any real date. Tickers, premiums, prices, timestamps, and baselines were
all invented for this hackathon demo.

They exist only to prove that `SyntheticDataAdapter` feeds data through the
exact same `backend/app/core/signal_engine.py` + `scan_service.py` logic
`LiveUWAdapter` would use on real UW data, and that the engine produces a
*computed* NEW/BUILDING/CONFIRMED/STABLE/FADING/INVALIDATED result -- not a
hardcoded label baked into the fixture.

## Field shapes

Each fixture matches the real UW API field names so the port is honest:

- `flow_alerts[]` -- shaped like `GET /api/option-trades/flow-alerts`:
  `total_premium`, `total_ask_premium`, `total_bid_premium`, `has_sweep`,
  `has_multileg`, `underlying_price`, `is_call`, `is_put`, `ticker_symbol`.
- `darkpool_trades[]` -- shaped like `GET /api/darkpool/{ticker}`:
  `premium`, `size`, `price`, `executed_at`, plus a synthetic-only
  `avg_premium_baseline` field (production would instead look this average
  up from weeks of Postgres history via `baselines.py`; see
  `backend/app/core/signal_engine.synthetic_percentile_gate` and
  `backend/app/core/scan_service._darkpool_metrics` docstrings for why a
  fixture-supplied baseline stands in for that here).
- `stock_state` -- shaped like `GET /api/stock/{ticker}/stock-state`:
  `last_price`, `change_percent`.

## Scenarios

| file | ticker | what it demonstrates |
|---|---|---|
| `scenario_call_sweep.json` | ZYNC | large one-sided call sweep -> options_flow_anomaly gate passes, bullish-lean direction |
| `scenario_darkpool_anomaly.json` | QRVX | dark-pool print far above its baseline -> dark_pool_anomaly gate passes (strong ratio) |
| `scenario_multi_signal.json` | FLUX | dark pool AND options flow both anomalous same session -> multi_signal_confirmation -> high confidence, CONFIRMED-eligible |
| `scenario_quiet_control.json` | NADA | ordinary/quiet activity -> no gates pass (negative control, proves the engine doesn't always say yes) |
| `scenario_fading_signal.json` | DRFT | was previously CONFIRMED, this scan shows nothing qualifying -> demonstrates the FADING transition |
