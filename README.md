# TIP Hackathon Edition

> ⚠️ **EARLY DEVELOPMENT — NOT PRODUCTION READY**
>
> TIP Hackathon Edition is an experimental, actively developed trading
> intelligence application built for the Unusual Whales hackathon.
>
> This software is being released for evaluation, demonstration, feedback,
> and development testing.
>
> Features, interfaces, configuration, and installation procedures may
> change. Bugs, incomplete functionality, inaccurate analysis, and
> installation problems may occur.
>
> **Not recommended for production use or making live trading decisions.**
> Financial analysis is informational and should be independently
> verified.
>
> Users should run the application locally, protect their API
> credentials, and avoid exposing the application or its Settings API to
> untrusted networks.

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

5. **"Investigate with AI" -> MCP transport: real MCP protocol now works,
   via an isolated venv -- read this for the full transport-decision
   history.** The original plan was to speak real MCP (the official `mcp`
   Python SDK's streamable-HTTP client) to UW's hosted server at
   `https://api.unusualwhales.com/api/mcp`, *in the same venv/process as
   the main FastAPI app*. That specific approach genuinely doesn't work:
   - The current PyPI `mcp` release pulls in a `starlette` floor that's
     incompatible with this repo's pinned `fastapi==0.115.6`/`starlette`
     combination when installed into the *same* venv -- installing it
     broke part of the existing test suite by upgrading `starlette` out
     from under FastAPI.
   - Downgrading to an `mcp` release old enough to coexist in that same
     venv (`1.9.4`) comes with three published HIGH-severity OSV advisories
     (GHSA-9h52-p55h-vw2f, GHSA-j975-95f5-7wqh, GHSA-jpw9-pfvf-9f58).

   Both of those are **same-venv** problems, though, not inherent problems
   with real MCP. Re-checking PyPI/OSV (2026-10-12): `mcp==2.3.0` (current
   latest) now returns **zero** advisories from the OSV API, and all of
   1.9.4's three HIGH advisories above are fixed by then anyway (they were
   server-transport issues -- SSE/StreamableHTTP session auth, DNS
   rebinding -- that never applied to this app's pure client usage in the
   first place). So the fix isn't a new dependency, it's **isolation**:
   `mcp_isolated/` at the repo root is a *separate* Python venv
   (`mcp_isolated/venv/`, built from `mcp_isolated/requirements.txt` --
   pins `mcp==2.3.0`, nothing else) that is never on the main app's
   `PYTHONPATH` and is never merged into `backend/requirements.txt`. The
   main app talks to it only by shelling out to
   `mcp_isolated/venv/bin/python mcp_isolated/mcp_investigate.py`, passing
   JSON on stdin/stdout and the UW API key via the subprocess's `env=`
   mapping only (never a CLI arg, never a file) -- see
   `backend/app/ai/mcp_tool_client.py`. No new port, no new long-running
   process; the main app's own dependencies are completely untouched.

   This transport is real, live, and selectable via
   `INVESTIGATE_TRANSPORT=mcp` (default remains `rest`; see `.env.example`
   and `backend/app/config.py`) -- a drop-in alternate backend behind the
   same `POST /api/investigate/{ticker}` contract, not a parallel feature
   (see `orchestrator.py`'s `transport` parameter). We DID connect to the
   live hosted MCP server directly (not guessed) to confirm the real tool
   names and schemas via its actual `tools/list` capability: **all 10
   allowlisted tool names match the live server exactly, no renaming
   needed**, but 3 of them (`get_flow_alerts`, `get_dark_pool_trades`,
   `get_option_trades`) take `ticker_symbol` as the argument name, not
   `ticker`, and 5 others (`get_insider_activity_by_ticker`,
   `get_short_data_by_ticker`, `get_company_info`, `get_max_pain`,
   `get_greek_exposure_by_ticker`) don't accept a `limit` argument at all
   (the live schemas are `additionalProperties: false`, so sending one
   would fail the call outright). `get_institutional_ownership_by_ticker`
   caps `limit` at 50 and `get_analyst_ratings` at 500, tighter than the
   REST port's generic 1-100 clamp. All of this is reconciled in
   `MCP_ARG_BUILDERS` in `mcp_tool_client.py` -- the REST transport's
   assumed argument shapes were never wrong on tool *names*, only on a few
   argument *names*/limits. The REST transport
   (`backend/app/ai/rest_tool_client.py`) remains in place and is still the
   default, clearly labeled as the fallback, not removed.

   The bounded design itself (round 1: LLM selects <=4 read-only tools from
   a fixed allowlist with every argument validated/sanitized server-side;
   execution: <=4 real calls; round 2: ONE synthesis call producing
   `observed_facts` tagged by source tool, `supporting_evidence`,
   `conflicting_observations`, `limitations`, and a separately-rendered
   `ai_interpretation` -- no trade recommendation or price prediction
   allowed in either round's output, enforced by both prompt and a
   post-hoc schema/heuristic check) is unaffected by the transport choice
   and is implemented in full under `backend/app/ai/`. See
   `POST /api/investigate/{ticker}` below. Demo mode and `PUBLIC_DEMO=1`
   never call either a real UW endpoint or a real LLM for this feature --
   they always serve one of the static, clearly-labeled transcripts in
   `fixtures/investigate_simulated/`.

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

To use the real MCP transport for Investigate with AI
(`INVESTIGATE_TRANSPORT=mcp`), also set up the isolated venv once (separate
from the one above, never merged into `backend/requirements.txt`):

```bash
cd mcp_isolated
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Then run the main app with `INVESTIGATE_TRANSPORT=mcp` in its environment
(or `.env`). If `mcp_isolated/venv/` doesn't exist, the MCP transport fails
fast with a clear 502 (`MCP transport unavailable: mcp_isolated venv not
found at ...`) instead of a confusing stack trace -- it never silently
falls back to REST.

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
  key used by Investigate with AI.
- `GET /api/investigate/availability` -- whether the Investigate-with-AI
  button should be enabled, and why not if disabled (no network call).
- `POST /api/investigate/{ticker}` -- bounded, optional AI investigation
  for one ticker. Demo mode / `PUBLIC_DEMO=1` always returns a static,
  clearly-labeled simulated transcript (no network call of any kind).
  Live mode requires both a UW key and an AI provider key (else 400) and
  makes <=4 real read-only UW REST calls (see Design Decision #5 above for
  why REST, not MCP) chosen by one LLM tool-selection call, followed by one
  LLM synthesis call -- never more than those two LLM calls, never a tool
  outside `backend/app/ai/allowlist.py`. Returns
  `{ticker, tools_used, observed_facts: [{fact, source_tool}],
  supporting_evidence, conflicting_observations, limitations,
  ai_interpretation, simulated, simulated_label}` or a 4xx with a clear
  reason (invalid ticker, no AI key, no UW key) / 502 if the AI response
  was malformed and got rejected by validation.

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

Serves on `http://127.0.0.1:8000/` by default -- loopback-only, not
reachable from your LAN or the internet. `BIND_HOST`/`BIND_PORT` in
`.env.example` control this; see that file for how to open it to a
specific LAN interface if you want to reach it from another device
(**never** set `BIND_HOST=0.0.0.0`, which exposes it on every interface,
including any public one). The Settings UI's credential-management
endpoints have no authentication of their own -- they rely entirely on
this network-level binding, so after deploying, verify the actual
published binding matches what you intended:

```bash
tools/check_deployment_binding.sh <your-compose-project-name>
```

This fails loudly if the container is published on `0.0.0.0`/`::` (every
interface) unless you explicitly set `ALLOW_PUBLIC_BIND=1` first.

## Checking for updates

Settings → **Application Updates** shows the installed build and can
check the public GitHub repo for newer commits. This is a **check-and-
inform feature only** -- it never runs `git`, Docker, or anything
destructive by itself; updating is always a manual or AI-agent-assisted
step you run yourself (see the "Copy AI-agent update prompt" button,
which gives you a ready-to-paste prompt that explicitly preserves
`.env`, the `tip-data` volume, and your saved credentials).

**How the installed version is known.** There is no `.git` directory
inside the running container (the Dockerfile only `COPY`s source), so
the commit can't be discovered at runtime. It is baked in at **image
build time** instead, via Docker build ARGs written to a plain
`/srv/VERSION` file (see `backend/app/version.py`):

```bash
# Build with real version info instead of plain `docker compose build`:
tools/build_with_version.sh
# ...or equivalently:
BUILD_COMMIT=$(git rev-parse HEAD) BUILD_VERSION=$(git describe --tags --always) \
  docker compose build
```

If you build with a plain `docker compose build` (no version args), the
app honestly shows "unknown installed version" rather than guessing.

**The four states** `GET /api/settings/update-check` (and the Settings
UI) can report:

| State | Meaning |
|---|---|
| **Up to date** | The installed commit is identical to (or ahead of) the latest commit on `main`. |
| **Update available** | GitHub confirms `main` has commits the installed commit doesn't (via GitHub's compare API, not just "the hashes differ"). |
| **Unable to check** | A network failure, GitHub rate limit, a 404 for an unrecognized commit, or a diverged history that can't be cleanly compared. Never silently retried. |
| **Unknown installed version** | No resolvable commit was baked in at build time (see above) -- nothing to compare. |

The check is **one unauthenticated HTTPS call** to GitHub's public REST
API per click (never automatic, never polled) -- no GitHub token and no
app credential of any kind is ever sent.

## Install with an AI Coding Agent

This repo includes `INSTALL_AGENT.md`, a standalone install walkthrough
written for coding agents (Claude Code and compatible tools), and
`skills/tip-hackathon/SKILL.md`, an Agent Skills-compatible reference for
day-2 operation (mode switching, health checks, logs, Settings API). Both
reuse the exact Docker Compose workflow described above rather than a
different one — there is no hidden alternate install path.

To use it, copy the prompt below into your coding agent (Claude Code,
or any compatible agent with shell + file access):

```
Install and verify "TIP Hackathon Edition" on this machine.

1. Inspect this machine's OS and check for prerequisites (Docker,
   Docker Compose, git). If anything required is missing, propose how
   to install it and ask for my approval before installing anything.
2. Clone https://github.com/Ch0sen84/tip-hackathon-edition into a
   directory of your choosing on this machine.
3. Follow README.md and INSTALL_AGENT.md in the cloned repo as the
   authoritative installation steps — don't invent a different
   install path.
4. Install using the documented Docker Compose workflow and initialize
   persistent storage (the SQLite volume) as described.
5. Start the app in credential-free Demo Mode (the default — no API
   key required) and confirm /api/health reports success with a
   nonzero scenarios_loaded count.
6. Open the app in a browser and verify Market Radar, Ticker Analysis
   (including the price chart), and the Settings page all load and
   work using the synthetic demo data.
7. Recreate the container (docker compose down && docker compose up)
   and confirm Settings/state persisted across the restart.
8. Keep the app bound to localhost only — do not change the binding
   to listen on all interfaces or expose it to the network.
9. After Demo Mode is confirmed working, OPTIONALLY offer to help me
   configure a real Unusual Whales API key (Live Mode) and/or an AI
   provider key for the Investigate feature — only if I explicitly
   want to. Never ask me for these secrets in chat; have me enter
   them through the app's own Settings UI, and never make a paid AI
   request without my explicit approval first.
10. Report a clear PASS / PARTIAL / FAIL verdict for each step above,
    and document any installation blockers you hit and how you
    resolved (or couldn't resolve) them.
```

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
ever making a real network call; Settings/credential endpoints including a
key-leak check; and Investigate with AI (`test_investigate.py`) --
argument validation/sanitization (bad tickers, out-of-range limits,
non-allowlisted tools all rejected), the tool-selection and synthesis
rounds each firing exactly once and real tool execution never exceeding 4
calls (all via `monkeypatch`, no real network), demo/`PUBLIC_DEMO` always
returning the static transcript and asserting the mocked real-call
functions are never invoked, and schema enforcement rejecting a
synthesis response missing `ai_interpretation` or smuggling a prediction
into `observed_facts`. **95 passed, 0 failed** (73 prior + 22 new) as of
this feature's addition; **108 passed, 0 failed** (95 prior + 13 new,
`test_mcp_tool_client.py`) after adding the real MCP transport -- argument
reconciliation per tool against the live schema, credential-in-env-not-argv,
one-bad-call/connection-failure/timeout/unparseable-output/missing-venv all
degrading or raising the documented way, all via `monkeypatch` on
`subprocess.run`, no network or real `mcp` SDK/venv required for the suite
to pass.

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
backend/app/ai/              Investigate with AI: allowlist, validation, REST + real-MCP tool
                              clients (transport-selectable), provider-agnostic LLM wrapper,
                              orchestrator, schemas
backend/app/main.py          FastAPI routes, incl. /api/settings*, /api/investigate/*
backend/app/settings_store.py  local JSON settings/credential store (data/settings.json)
backend/app/storage.py       local SQLite qualifying-days store
backend/tests/                pytest suite (incl. test_settings.py, test_investigate.py,
                               test_mcp_tool_client.py)
fixtures/                    synthetic scenario_*.json + README_SYNTHETIC.md
fixtures/investigate_simulated/  static, labeled simulated AI-investigation transcripts
frontend/                    plain HTML/CSS/JS, no build step (incl. Settings, Investigate views)
docs/                        design notes
data/                        gitignored: settings.json + tip_hackathon.db, created at runtime
mcp_isolated/                 isolated venv + subprocess script for the real MCP transport --
                              own requirements.txt (mcp==2.3.0 only), never merged into
                              backend/requirements.txt; venv/ itself is gitignored
```

## Status

Phase 1 + Phase 2 scope (backend port + synthetic/live adapters + minimal
frontend + tests) is implemented and passing locally. Phase 2.5 (Settings
UI + local credential store, Demo/Live mode switching, PUBLIC_DEMO
explanatory mode) is also implemented and passing locally. The optional
"Investigate with AI" module (bounded tool-selection + synthesis rounds,
REST-fallback transport -- see Design Decision #5 above for why not real
MCP, demo-mode static simulated transcripts, Settings-gated graceful
disable with zero AI key) is implemented and passing locally: **95 tests
passing, 0 failing** (73 prior + 22 new). This README is a skeleton;
polish (screenshots, architecture diagram, deployment notes) is planned
for a later pass before submission.
