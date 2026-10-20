# Recorded Unusual Whales data (test fixture)

`uw_capture_2026-10-15_17.json.gz` holds **real Unusual Whales API responses**
captured by a TIP Hackathon Edition Live-mode install between 2026-10-15 and
2026-10-17. It is used by the backend tests to run the real Live-mode pipeline
(`LiveUWAdapter` -> normalization -> gates -> lifecycle -> API) offline.

- Built by `tools/build_recorded_uw_fixture.py` from a captured `tip_hackathon.db`
  opened read-only.
- Rows are in UW **wire format** (`total_ask_side_prem`, `type`, string-typed
  stock-state numerics), so replay exercises the adapter's real normalization.
- Sanitized: only market-data fields the app reads are kept. No credentials,
  settings, or account data exist in the source tables or in this file.
- Known coverage: no trading session in this capture is fully paginated. Every
  captured scan saw at most the newest 100 flow alerts and 500 dark-pool prints.
  So the session-baseline gates honestly evaluate to **insufficient history** on
  this data. The tests assert that outcome rather than manufacturing a pass.

**Licensing:** this is third-party market data. Confirm the Unusual Whales terms
permit redistribution before publishing it in a public repository. Every test
that needs it skips cleanly when the file is absent, so it can be dropped
without breaking the suite.
