# TIP Hackathon Edition

Standalone hackathon build of TIP (Trading Intelligence Platform) for the
Unusual Whales Hackathon. A FastAPI backend ports the production app's
**pure decision logic** (named gate evaluation, confidence scoring, and the
6-state signal lifecycle) unmodified, and runs it against either synthetic
fixtures or the real Unusual Whales API through two interchangeable
adapters. A small plain HTML/CSS/JS frontend (no build step) renders Market
Radar and Ticker Analysis against this app's own API.

This is a clean-room extraction/port for the hackathon -- it does not import
or depend on the production TIP app, has no Postgres/TimescaleDB/Databento
dependency, and makes no calls to the production TIP service.

## Why this exists / what's ported vs. rebuilt

| Component | Source | What changed |
|---|---|---|
| `backend/app/core/signal_engine.py` | production `signal_engine.py` | Verbatim gate/confidence/state-transition logic & thresholds. The one functional change: `evaluate_gates` takes an injectable `percentile_gate_fn` instead of hard-importing the Postgres-backed `baselines.py`. Persistence helpers (`log_gate_results`, `log_transition`, `qualifying_days_count`) were intentionally **not** ported -- see Design Decisions below. |
| `backend/app/core/tip_evidence.py` | production `tip_evidence.py` | Ported verbatim; the source file has zero imports/DB/network access, so no adaptation was needed. |
| `backend/app/adapters/live_uw_adapter.py` | production `uw_client.py` | Same ~15 endpoint wrappers. Reads `UW_API_KEY` from env instead of a fixed secrets file path; added explicit, crash-proof handling for HTTP 401/403 (bad key / tier not entitled). |
| `backend/app/adapters/synthetic_adapter.py` | new | Loads `fixtures/scenario_*.json` and serves them through the same adapter interface as live data. |
| `backend/app/core/scan_service.py` | new | Glue: turns raw UW-shaped data (from either adapter) into the keyword arguments `signal_engine.evaluate_gates` expects. This heuristic layer did not exist standalone in production (it was inline in `flow_tracker.py`/`midday_scan.py` against weeks of Postgres history) -- see Design Decisions. |
| `backend/app/storage.py` | new | Local SQLite replacement for the Postgres-backed `qualifying_days_count()`. |
| `frontend/` | production `webapp/frontend/index.html` | Rebuilt small and standalone against this app's own API. Only CSS variables, the card/list layout idea, and mobile breakpoints were reused -- not the file wholesale. Market Radar + Ticker Analysis only (Hypothesis Lab and Market Statistics are out of scope for this hackathon build). |

## Design decisions / tradeoffs (read this before reviewing)

1. **`percentile_gate_fn` injection.** Production's historical-percentile
   gate needs weeks of real trailing-window data from Postgres. This repo
   has none, so `evaluate_gates()` takes an optional `percentile_gate_fn`
   callable (default: `signal_engine.synthetic_percentile_gate`, a static
   premium-size-bucket estimator, clearly documented in its docstring as
   NOT the production model). A real implementation backed by
   `LiveUWAdapter` historical calls could be injected later without
   touching `evaluate_gates` itself.

2. **`qualifying_days_count` without Postgres.** Production counts distinct
   calendar days (ET) with a logged gate pass, from a table fed by cron jobs
   running for weeks. Here, `backend/app/storage.QualifyingDaysStore` keeps
   an equivalent SQLite log (`ticker_scan_log`), one row per `/api/radar` or
   `/api/ticker/{t}` scan, and computes the same `COUNT(DISTINCT scan_date)`
   query. The semantics are identical; the available history is just
   however many times you've actually run this app, not weeks of
   production cron. Re-running scans (or feeding multi-day fixture sets) is
   what drives NEW -> BUILDING -> CONFIRMED -> STABLE in this build.

3. **Deriving gate inputs from raw UW data (`scan_service.py`).** Production
   computes `tier1_hit`/`tier1_ratio`/`options_flow_hit`/etc. deep inside
   `flow_tracker.py` and `midday_scan.py` by comparing live data against
   Postgres-stored historical baselines. This app derives the same *shape*
   of inputs directly and transparently from one scan's worth of
   flow-alert/dark-pool data using simple, documented rules in
   `scan_service.py` (dark-pool ratio vs. a baseline field on the trade,
   one-sided ask/bid premium share for direction, sweep flag for options
   anomaly). This is a reasonable stand-in for a demo, not a claim of
   parity with production's real statistical baselines -- swap this module
   first if adapting the app for anything beyond a demo.

4. **`UW_API_KEY` via environment, not a secrets file.** Production reads
   a fixed path on one specific machine. This repo is meant to run anywhere
   (Docker, CI, a laptop), so it reads `UW_API_KEY` from the environment
   (see `.env.example`). Never commit a real key.

5. **No MCP / LLM code anywhere in this repo**, per the hackathon task scope.
   The Settings page's "AI / MCP (optional)" section only saves/clears a
   provider API key for now -- no AI/MCP calls happen yet.

6. **Settings infrastructure is .env-primary, UI-layer-on-top (Phase 2.5
   judgment call).** `backend/app/settings_store.py` is a small local JSON
   file (`data/settings.json`, gitignored) holding mode + both keys. It is
   genuinely safe UI-based key *saving* (server-side only, chmod 600,
   never echoed back), not a cosmetic stub -- see "Credential precedence"
   below for why `.env` is still called primary. A full secrets-manager /
   OS-keychain integration (e.g. `keyring`) was considered and rejected as
   over-engineering for a single-user local/Docker hackathon app; a
   plain-JSON file under a gitignored, chmod-600 directory is a pragmatic
   match for the actual threat model here (protecting against an
   accidental git commit or UI leak, not a local filesystem attacker).

## Credential precedence (read this before testing Live Mode)

Two ways to configure a UW API key, in this precedence order:

1. **UI-saved key** (Settings page -> Save, or `POST /api/settings/uw-key`)
   -- written to `data/settings.json`. **Always wins if present.**
2. **`.env` / environment `UW_API_KEY`** -- read once at process startup
   (via `python-dotenv`, see `backend/app/config.py`). Used only as a
   fallback when no UI-saved key exists.

`GET /api/settings` reports which source is active (`"source": "ui"` or
`"env"`) plus a `configured` boolean and the key's last 4 characters --
**never the real key value**, in either case. Switching `TIP_MODE`/Live
via the UI is independent of which source supplied the key; `POST
/api/settings/mode {"mode":"live"}` just requires *some* effective key to
exist (fails with 400 otherwise), and `DELETE /api/settings/uw-key`
clears the UI-saved key and force-reverts mode to `demo`.

`PUBLIC_DEMO=1` (see `.env.example`) renders Settings in explanatory-only
mode (Live Mode and key inputs disabled/hidden client-side) **and** blocks
`POST /api/settings/mode {"mode":"live"}`, `POST /api/settings/uw-key`,
and `POST /api/settings/ai-key` server-side with 403 -- defense in depth
for the later public static-demo build, implemented now so that code path
never needs revisiting.

## Running locally (no Docker)

```bash
cd backend
python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r requirements.txt
TIP_MODE=demo uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/` for the frontend, or hit the API directly:

- `GET /api/health`
- `GET /api/mode`
- `GET /api/radar` -- Market Radar: scans every ticker in the adapter's
  universe, returns highest-attention / other, computed live from
  `signal_engine` + `tip_evidence`.
- `GET /api/ticker/{ticker}` -- Ticker Analysis: full gate/confidence/state
  drill-down plus raw adapter data for one ticker.
- `GET /api/scenarios` -- (demo mode only) lists what each synthetic
  fixture is meant to demonstrate.
- `GET /api/settings` -- current mode, `public_demo` flag, and whether the
  UW/AI keys are configured (boolean + last-4-characters only, never the
  real value).
- `POST /api/settings/mode {"mode": "demo"|"live"}` -- switch mode.
- `POST /api/settings/uw-key {"api_key": "..."}` / `DELETE /api/settings/uw-key`
  -- save/clear the UW key (saving requires a non-empty key; clearing
  force-reverts mode to `demo` if it was `live`).
- `POST /api/settings/uw-key/test` -- a **real** live call to the UW API
  (stock-info for SPY) using the saved key; returns `success`,
  `unauthorized`, `forbidden_tier_limited`, `network_error`, or
  `not_configured`.
- `POST /api/settings/ai-key {"provider": "anthropic"|"openai", "api_key": "..."}`
  / `DELETE /api/settings/ai-key` -- save/clear the optional AI provider
  key for the upcoming (not-yet-implemented) MCP/AI module.

Switch to Live Mode either through the Settings page (Save a UW key, then
toggle to Live) or via env: `TIP_MODE=live UW_API_KEY=... uvicorn ...`. See
"Credential precedence" above for how a UI-saved key and `.env` interact.

The SQLite file (`TIP_DB_PATH`, default `./data/tip_hackathon.db`) is
created automatically on first run -- delete it to reset all lifecycle
state and start every ticker at NEW again.

## Running with Docker

```bash
cp .env.example .env   # edit if using TIP_MODE=live
docker compose up --build
```

Serves on `http://localhost:8000/`.

## Tests

```bash
cd backend
source ../.venv/bin/activate
python -m pytest -v
```

Covers: every branch of `signal_engine.evaluate_gates` /
`classify_confidence` / `determine_transition` with synthetic gate inputs;
`SyntheticDataAdapter` loading all fixtures and feeding them through the
real engine (proving different fixtures produce different, non-canned
NEW/BUILDING/CONFIRMED results); `LiveUWAdapter` against a mocked HTTP
session covering success, 401, 403, and network-exception paths without
ever making a real network call.

## Fixtures

See `fixtures/README_SYNTHETIC.md`. Every `scenario_*.json` file there is
fabricated demo data (`"synthetic": true`), shaped like real UW API field
names (`total_premium`, `has_sweep`, dark-pool `premium`/`size`, etc.) so
the engine that runs on them is the exact same code path that would run on
live data.

## Repo layout

```
backend/app/core/            pure, DB-free logic (signal_engine, tip_evidence, scan_service)
backend/app/adapters/        SyntheticDataAdapter, LiveUWAdapter (same interface)
backend/app/main.py          FastAPI routes, incl. /api/settings*
backend/app/settings_store.py  local JSON settings/credential store (data/settings.json)
backend/app/storage.py       local SQLite qualifying-days store
backend/tests/                pytest suite (incl. test_settings.py)
fixtures/                    synthetic scenario_*.json + README_SYNTHETIC.md
frontend/                    plain HTML/CSS/JS, no build step (incl. Settings view)
docs/                        design notes
data/                        gitignored: settings.json + tip_hackathon.db, created at runtime
```

## Status

Phase 1 + Phase 2 scope (backend port + synthetic/live adapters + minimal
frontend + tests) is implemented and passing locally. Phase 2.5 (Settings
UI + local credential store, Demo/Live mode switching, PUBLIC_DEMO
explanatory mode, no MCP/AI calls yet) is also implemented and passing
locally. This README is a skeleton; polish (screenshots, architecture
diagram, deployment notes) is planned for a later pass before submission.
