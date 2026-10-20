# Synthetic UW replay fixture (public)

`synthetic_capture.json.gz` is **fully synthetic**: built deterministically
(fixed seed, fixed gzip header) by `tools/build_synthetic_uw_fixture.py`, with
fictional tickers and constructed values. It contains **no real market data**
(`provenance.contains_real_market_data: false`). It mirrors the private recorded
capture's format so `tools/uw_replay_server.py` can drive the real Live-mode
pipeline in the public test suite:

- `SYNP` -- put-buying dominated flow while the stock is down ~3.46% (the pattern
  05c47ca mislabeled bullish and falsely invalidated)
- `SYNC` -- call-buying dominated flow, daily price bars for the chart path
- `SYNQ` -- quiet: a few dark-pool prints, no flow alerts

Dark-pool rows deliberately include prints that share a `tracking_id` and a few
canceled prints, matching behaviour verified against the real API.
