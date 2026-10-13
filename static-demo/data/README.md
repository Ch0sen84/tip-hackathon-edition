# Static demo data — provenance

Every JSON file under this directory is a **verbatim capture of a real response**
from a locally-running instance of this same app, in `TIP_MODE=demo` (the
`SyntheticDataAdapter` feeding the unmodified `backend/app/core/signal_engine.py`
logic over the fabricated fixtures in `fixtures/scenario_*.json`, every one of
which carries its own `"synthetic": true` marker and is documented in
`fixtures/README_SYNTHETIC.md`).

These are **computed engine outputs on fake input**, not hand-written sample
text and not a second, parallel implementation of the detection logic. The
public static demo and the downloadable local edition run the identical
`evaluate_gates` / `classify_confidence` / `determine_transition` code path;
the only difference is that this static build captured the results once and
replays them, instead of running the engine live on each page load.

| File | Captured from |
|---|---|
| `radar.json` | `GET /api/radar` |
| `ticker/<TICKER>.json` | `GET /api/ticker/<TICKER>` for DRFT, FLUX, NADA, QRVX, ZYNC |
| `investigate/<TICKER>.json` | `POST /api/investigate/<TICKER>` (demo mode -- always the simulated transcript; see `fixtures/investigate_simulated/`) |
| `investigate-availability.json` | `GET /api/investigate/availability` |
| `scenarios.json` | `GET /api/scenarios` |

No live Unusual Whales data, no real AI/LLM call, and no real MCP call are
reachable from this static build at all -- there is no backend here to make
them with.
