---
name: tip-hackathon
description: Use when installing, configuring, troubleshooting, or operating TIP Hackathon Edition (a FastAPI + plain HTML/CSS/JS trading-signal demo app run via Docker Compose).
---

# TIP Hackathon Edition

TIP Hackathon Edition is a standalone FastAPI backend + plain HTML/CSS/JS
frontend that evaluates options-flow/dark-pool "signal" logic against
either synthetic fixtures (demo mode) or the real Unusual Whales API (live
mode). It runs via Docker Compose and serves everything on one port
(`8000`).

This skill works with any agent or environment that can run shell commands
and read markdown — it does not assume or require any particular coding
agent, IDE, or orchestration tool.

## Installation

Full agent-executable install steps (host checks, clone, env setup, build,
health verification, troubleshooting, uninstall) live in
`INSTALL_AGENT.md` at the repo root — read and follow that file for a
fresh install. In short, once in the repo root:

```bash
cp .env.example .env     # demo mode needs no edits
docker compose up --build
curl -s http://localhost:8000/api/health   # expect scenarios_loaded: 11
```

Serves at `http://localhost:8000/`.

## Day-2 operation

### Check health / mode

```bash
curl -s http://localhost:8000/api/health
curl -s http://localhost:8000/api/mode
curl -s http://localhost:8000/api/scenarios   # demo mode only
```

### View logs

```bash
docker compose logs          # one-shot
docker compose logs -f       # follow/tail live
```

### Restart

```bash
docker compose restart
# or, for a full rebuild after dependency/code changes:
docker compose down
docker compose up --build
```

### Switching Demo <-> Live mode

Demo mode (`TIP_MODE=demo`, the default) needs no API key and serves the
synthetic fixtures described below. Live mode needs a real Unusual
Whales API key, entered by the user — never by this skill on the user's
behalf — either through the app's own Settings UI at
`http://localhost:8000/` or via the Settings API:

```bash
# 1. Save the key (user supplies the value themselves)
curl -s -X POST http://localhost:8000/api/settings/uw-key \
  -H 'Content-Type: application/json' \
  -d '{"api_key": "<user-supplied>"}'

# 2. Test it with one real live call before switching
curl -s -X POST http://localhost:8000/api/settings/uw-key/test

# 3. Switch mode
curl -s -X POST http://localhost:8000/api/settings/mode \
  -H 'Content-Type: application/json' \
  -d '{"mode": "live"}'

# Revert to demo / clear the saved key:
curl -s -X POST http://localhost:8000/api/settings/mode \
  -H 'Content-Type: application/json' -d '{"mode": "demo"}'
curl -s -X DELETE http://localhost:8000/api/settings/uw-key
```

**Never** type, log, or persist a real API key value as part of running
this skill. Only the user enters their own key, directly into the UI or
the `curl` command they run themselves.

### Settings API reference

| Endpoint | Purpose |
|---|---|
| `GET /api/settings` | Current mode, `public_demo` flag, whether UW/AI keys are configured (boolean + last 4 chars only, never the real value) |
| `POST /api/settings/mode {"mode":"demo"\|"live"}` | Switch mode (requires an effective UW key to switch to live) |
| `POST /api/settings/uw-key {"api_key":"..."}` | Save a UW API key (server-side only) |
| `DELETE /api/settings/uw-key` | Clear the saved UW key; force-reverts mode to `demo` |
| `POST /api/settings/uw-key/test` | Real live call (SPY stock-info) to validate the saved key; returns `success`/`unauthorized`/`forbidden_tier_limited`/`network_error`/`not_configured` |
| `POST /api/settings/ai-key {"provider":"anthropic"\|"openai","api_key":"..."}` | Save the optional AI provider key for "Investigate with AI" (no env-var equivalent) |
| `DELETE /api/settings/ai-key` | Clear the AI provider key |
| `GET /api/investigate/availability` | Whether the Investigate-with-AI button should be enabled, and why not if disabled |
| `POST /api/investigate/{ticker}` | Bounded AI investigation; demo mode always returns a static, clearly-labeled simulated transcript |

### The synthetic scenarios (demo mode)

Source: `fixtures/README_SYNTHETIC.md`. All fabricated demo data
(`"synthetic": true`), shaped like real UW API fields, fed through the
same engine code path real data would use.

| file | ticker | what it demonstrates |
|---|---|---|
| `scenario_call_sweep.json` | ZYNC | Large one-sided call sweep → `options_flow_anomaly` gate passes, bullish-lean direction |
| `scenario_darkpool_anomaly.json` | QRVX | Dark-pool print far above its baseline → `dark_pool_anomaly` gate passes (strong ratio) |
| `scenario_multi_signal.json` | FLUX | Dark pool AND options flow both anomalous in the same session → `multi_signal_confirmation`, high confidence, CONFIRMED-eligible |
| `scenario_quiet_control.json` | NADA | Ordinary/quiet activity → no gates pass (negative control, proves the engine doesn't always say yes) |
| `scenario_fading_signal.json` | DRFT | Was previously CONFIRMED, this scan shows nothing qualifying → demonstrates the FADING transition |

## Guardrails

- Never ask the user to paste a raw API key into chat; keys go only into
  the app's own Settings UI or a `curl` command the user runs themselves.
- Never hardcode a key/secret into any file, including `.env`.
- Never run unreviewed remote shell scripts.
- Never touch Docker containers/images/volumes unrelated to this project.
- Never send any credential anywhere other than the user's own
  locally-running instance of this app.

## Verification status

This skill documents the same Docker Compose workflow and API surface
already described and previously verified in the repo's own `README.md`
and `INSTALL_AGENT.md`. It has not itself been independently executed
end-to-end by a live coding agent in this pass. This is a
**DOCUMENTATION-ONLY** deliverable, not an independently agent-tested one.
