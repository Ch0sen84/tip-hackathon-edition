# TIP Hackathon Edition

> **WORKING HACKATHON / RESEARCH APPLICATION — NOT A TRADING OR FINANCIAL EXECUTION SYSTEM**
>
> Public release v10 (`70c5d90790b300cc1bc7ecee6aba24c3d038692a`) is a working local application built for the
> Unusual Whales hackathon. A fresh public-GitHub installation was verified with real Unusual Whales data, real
> MCP tool calls, and Anthropic investigations. Demo Mode runs without credentials.
>
> TIP provides research-oriented, informational analysis. It does not place trades or provide financial advice;
> independently verify its output before making decisions. Run it locally, protect your API credentials, and do
> not expose the application or its Settings API to untrusted networks.

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

## For judges: start here

**Demo video:** https://youtu.be/I-8EcvTfrSk · **Source:** https://github.com/Ch0sen84/tip-hackathon-edition

TIP finds unusual options-flow and dark-pool activity with your own Unusual Whales key and explains, gate by gate,
what the evidence does and does not support. A signal is reported only against at least 20 **certified** prior
sessions of real data; until then a gate says "insufficient", never a guess. **Demo Mode** (synthetic data, no key)
shows the interface without an account.

**Requirements:** Docker Desktop with Compose v2 (or Python 3.11, see "Running locally"); for Live Mode an Unusual
Whales API key (verified with a UW Basic API key); optionally an Anthropic or OpenAI key for "Investigate with AI".
This section is a first install; an existing deployment follows only
[docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md).

```bash
git clone https://github.com/Ch0sen84/tip-hackathon-edition
cd tip-hackathon-edition
cp .env.example .env
docker compose up --build
```

Open http://127.0.0.1:8000/ (if port 8000 is taken, set `BIND_PORT` in `.env`). It starts in Demo Mode. Settings
shows the installed version as "unknown" unless the image is built with `tools/build_with_version.sh`; that is
expected.

**Live Mode in five steps** (measured on 2026-10-22 with a real key):
1. **Settings → Unusual Whales API Key:** paste your key → **Save** → **Test Connection** → **Switch to Live Mode**
   (the switch stays disabled until the connection test passes).
2. **Settings → Historical baselines (Live) → Build baseline** for a ticker and confirm the page budget shown. Each
   build spends at most 400 pages; click **Build** again until both columns show 20/20. SPY, QQQ and NVDA each
   needed about 1,200–1,700 pages (three to five builds, a few minutes). A session counts only once it is
   *certified*: walked in full at least 3 hours after its close with no implausible gap.
3. **Ticker Analysis → the ticker → Refresh this ticker** (confirm). The dark-pool and options-flow gates now compare
   this session with the same time of day in the certified sessions and show the ratio and percentile. "No"
   means normal activity; a gate passes only at 3× the average and the 95th percentile, so most days show none.
4. **Investigate with AI** (optional, after saving an AI key under Settings → AI / MCP): two billed AI calls plus
   at most four read-only Unusual Whales tool calls; every observed fact names the tool it came from, and the
   interpretation is labelled as the model's reasoning.
5. **Market Discovery** (tab on My Watchlist): **Scan** runs the real UW screener; **Evaluate in TIP** analyzes a
   candidate.

Every action that can spend many requests (refreshes, baseline builds and resumes, reconciliation, Investigate
with AI) asks for confirmation first and states its budget. Scan, Load Chart Data and the Settings connection
tests make a few bounded requests when clicked. Opening pages makes no provider request.

**Demo Mode notes:** every figure is synthetic (the sidebar badge says so); some chart captions say "real" because
they describe Live Mode; Market Intelligence stays empty in Demo Mode by design (synthetic candidates are never
written to Live history).

## Signal integrity (read before interpreting any label)

Confidence and lifecycle labels are rule-based evidence-coverage descriptions, **not probabilities and not predictions**. In Live Mode both real gates (dark pool, options flow) need at least 20 fully collected prior trading sessions before they can pass or fail; until then they honestly report "insufficient". A provider failure is reported as unavailable data, never as a quiet market. See [docs/SIGNAL_INTEGRITY_RULES.md](docs/SIGNAL_INTEGRITY_RULES.md), [docs/GATE_PROOF.md](docs/GATE_PROOF.md) and [docs/TRADING_VALIDITY.md](docs/TRADING_VALIDITY.md).

### Building Live baselines

A Live **Refresh** collects only the current evidence session. Prior sessions are collected by one of the
paths below; with none of them, the gates stay "insufficient" indefinitely.

A prior session counts only once it is **certified**: walked in full at least `TIP_COVERAGE_SETTLE_MINUTES`
(default 180) after its close, so prints the provider reports late are included. Sessions collected by an
earlier release are shown as **legacy, needs re-verification** and are not counted until a build re-walks
them; after upgrading, an existing install therefore reports "insufficient" until you build baselines again.
Nothing is re-walked automatically. Details: [docs/SIGNAL_INTEGRITY_RULES.md](docs/SIGNAL_INTEGRITY_RULES.md)
section 4.

0. **Settings → Historical baselines (Live)** (recommended). Shows each watchlist ticker's collected dark-pool
   and options-flow sessions against the 20-session requirement, and why it is still insufficient (including a
   provider history cutoff). **Build baseline** (one ticker) or **Build baselines for the whole watchlist** starts
   a bounded job:
   - before anything is sent, the app shows the total page budget the server computed and asks you to
     confirm (the API refuses an unconfirmed job with HTTP 428);
   - 10 UW pages per step, at most 400 per ticker per build (click **Build** again to continue a busy
     ticker), stopping at 20 complete sessions;
   - **Pause** and **Cancel** are cooperative: they take effect when the step in progress finishes, not
     instantly. A step is limited to 10 logical pages, but HTTP retries add attempts, and provider timeouts or a
     provider `Retry-After` can delay the stop. The panel shows "requested" until the job has actually stopped;
   - progress and **Resume**;
   - a provider error pauses the job and shows the error;
   - an application restart leaves the job paused; it never auto-resumes;
   - already-verified sessions are never fetched again.
   - history comes first: a job walks the current session back to the open once (the comparison needs it),
     then spends each step on prior sessions and extends the current session only with the pages left in the
     step; it stops when no prior session needs work. A job whose remaining sessions all await gap confirmation
     pauses and says from when they can be confirmed.

   Opening the page makes no UW requests; only the button does.

1. **Explicit backfill (operator-run, no scheduler needed).** Run on the machine hosting TIP. Each call walks
   back through prior sessions, newest first, using at most `max_pages` (capped at 200) UW requests through
   the normal 2-concurrent-request gate. Repeat per ticker until the response shows
   `eligible_sessions` ≥ 20 for both datasets, or a provider history cutoff:

   ```bash
   curl -X POST "http://127.0.0.1:8000/api/ticker/SPY/baseline/backfill?max_pages=200&confirm=true" -H "Origin: http://127.0.0.1:8000" -H "Sec-Fetch-Site: same-origin"
   ```

   (Local Mode accepts these headers only on a loopback-bound install; adjust the port to your `BIND_PORT`.)
2. **Scheduler `enabled`, opt-in only.** With `TIP_SCHEDULER_BASELINE_BACKFILL=1` (default off), each
   recurring evaluating cycle during market hours also backfills `TIP_BASELINE_BACKFILL_PAGES` pages for
   `TIP_BASELINE_BACKFILL_TICKERS_PER_CYCLE` rotating watchlist tickers. `disabled`, `dry_run` and every
   one-time run never backfill.
3. **Reconciliation of one settled session.** `POST /api/ticker/{t}/baseline/reconcile` with
   `{"dataset": "darkpool", "session_date": "YYYY-MM-DD", "max_pages": 100, "confirm": true}` re-walks one
   prior session (at most 200 pages) to pick up prints reported after it was certified. Without
   `"confirm": true` it answers 428 with the budget and does nothing.

Measured cost for one SPY-scale ticker:
- on 2026-10-22 (about 15,000 dark-pool prints and 15,000 flow alerts per session), 1,700 UW pages and about
  3.5 minutes certified 21 sessions per dataset for SPY (QQQ about 1,600 pages, NVDA about 1,200 plus
  re-walks); busier periods (earlier measurements: about 50,000 prints per session) cost about 2,800–4,200
  pages for 20 sessions and 4,200–6,300 for the full 30-session lookback;
- a busy ticker prints roughly 3–5 dark-pool pages per 15 minutes, so the default 4-page budget per
  Refresh (`TIP_SCAN_COVERAGE_PAGES`) cannot walk the current session back to the open on its own; raise
  it (e.g. 25) or rely on backfill, otherwise the evidence session stays "not verified from the open";
- about 1.6 GB of SQLite storage at 30 sessions;
- about 12 s (mid-session) to 22 s (after the close) of baseline computation per refresh, with ~0.5 GB peak
  memory.

Observations are not pruned automatically; monitor the size of the data volume. Thinner tickers cost a small
fraction of this.

With the completed-session curve cache (migration 9), the per-refresh baseline computation drops to about 0.2–0.8 s
once each completed session has been read once. See [docs/PERFORMANCE_AND_RETENTION.md](docs/PERFORMANCE_AND_RETENTION.md)
for measurements, the recommended retention policy, and the operator-run, off-by-default `tools/retention.py`
(archive before delete, restore).

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

## Local Mode / Network Mode (authentication redesign)

As of this round, this app runs in exactly one of two authentication
modes, decided automatically by the backend from `BIND_HOST` (see
`.env.example`) -- there is no setup step for the common case:

* **Local Mode (the default for the supported loopback-only install):**
  `BIND_HOST=127.0.0.1` (or unset, or `::1`/`localhost`) -- opening TIP
  in a normal browser on this same machine works **immediately, with
  zero authentication prompts of any kind**: no banner, no pairing
  code, no token field. State-changing requests (Settings, the
  watchlist editor, Market Discovery, scheduler controls, Investigate
  with AI) are allowed when, and only when, the browser's own
  `Sec-Fetch-Site: same-origin` header is present (the browser computes
  this itself from the actual request; it cannot be forged by a remote
  page) AND, whenever an `Origin` header is present, it exactly matches
  this deployment's configured origin. An unrelated website opened in
  the same browser **cannot** perform any protected action against your
  TIP instance -- its requests arrive with `Sec-Fetch-Site: cross-site`
  (or, from a same-site-but-different-origin page, `same-site`), both of
  which are explicitly rejected, with no exception for direct
  navigation/typed-URL requests (`Sec-Fetch-Site: none`) either. A
  browser old enough to never send `Sec-Fetch-Site` at all falls back to
  requiring Network Mode's token (fail-**closed**, never fail-open).
* **Network Mode (any other `BIND_HOST`, or `TIP_LOCAL_MODE=0`):** the
  exact, unmodified `X-TIP-Auth` bootstrap/pairing-code token model
  described below -- nothing about it changed in this round.

**Custom ports and both loopback spellings are genuinely supported**
(Windows Docker Desktop QA round 2, findings B1/B3): `docker-compose.yml`
forwards your real `BIND_HOST`/`BIND_PORT` into the container's own
environment (not just into the `ports:` publish mapping), so a custom
`BIND_PORT` (e.g. `18000`) is reflected correctly in both the Host and
Origin checks below -- and on a loopback install, opening the app via
`http://127.0.0.1:<port>` or `http://localhost:<port>` are equally
supported, interchangeably, on the same port. Neither one is "the"
canonical address.

**What Local Mode's `Sec-Fetch-Site`/`Origin` check actually protects
against -- and what it does NOT (read this before assuming more than it
promises):** this mechanism defends against a **malicious or unrelated
WEBSITE**, opened in the same browser, trying to issue a cross-origin
request against your TIP instance -- the real CSRF/cross-origin threat
`Sec-Fetch-Site` exists to signal, and which only a real browser can
compute (a remote page cannot forge it). It does **not**, and
fundamentally **cannot**, authenticate an arbitrary **local process** on
the same machine (a curl command, a Python script, any other HTTP
client) -- `Sec-Fetch-Site` and `Origin` are browser-computed signals
with zero enforcement for a non-browser caller, which can set either
header to literally anything it wants. A local script that deliberately
sends `Sec-Fetch-Site: same-origin` gets the exact same zero-token
bypass a real browser tab does. This is **not a bug to fix** -- a
process already running arbitrary code on this machine is already
inside the trust boundary every localhost-bound dev tool on earth
implicitly relies on (the same boundary SSH-agent, most local dev
servers, and Docker's own default socket permissions already assume).
Local Mode's value is specifically stopping a **remote attacker's
website** from reaching your local instance through your browser; it
was never a defense against **your own machine being compromised**,
and no amount of header-checking from inside this one process could
honestly provide that.

**`BIND_HOST` is read as your STATED INTENT, never independently
verified.** This process cannot confirm from inside a container that a
loopback-looking `BIND_HOST` genuinely means nothing outside this
machine can reach the port. `docker-compose.yml` forwards the SAME
`BIND_HOST`/`BIND_PORT` values used to publish the container into the
container's own environment, so the Compose-level publish and this
process's own Local-Mode decision cannot structurally diverge (Windows
Docker Desktop QA round 2, finding B2) -- but this process still cannot
independently verify that your Compose/host networking setup, overall,
genuinely isolates this port to loopback. Do **not** combine Local Mode
with any of the following -- none of them are supported, and doing so
defeats Local Mode's trust model entirely:

* a custom `docker-compose.override.yml` (or any other Compose
  override) that republishes or changes the port mapping;
* a reverse proxy (nginx/Caddy/Traefik/etc.) in front of this app;
* a tunnel (ngrok, cloudflared, Tailscale Funnel, or similar);
* any other mechanism that makes this port reachable from anywhere but
  the literal machine Docker is running on.

If you need any of the above, either set `TIP_LOCAL_MODE=0` to force
Network Mode and rely on its token model, or independently enforce
authenticated access at that outer layer yourself. Setting
`TIP_LOCAL_MODE=1` explicitly against a non-loopback `BIND_HOST` makes
this app **refuse to start** (a loud startup failure, matching the
existing single-process guard's fail-closed pattern) rather than
silently run an unsafe configuration -- see
`backend/app/core/local_mode.py`.

The frontend never guesses which mode is active: it calls the
unauthenticated `GET /api/auth/mode` before deciding whether to run any
auth-setup UI at all.

## Authentication (Phase A) -- Network Mode only

Everything in this section describes **Network Mode's** behavior, which
is completely unchanged from the prior QA-remediation round. In Local
Mode (the default loopback-only install, see above), none of this UI or
flow is ever shown -- state-changing requests from a genuine same-origin
browser tab just work.

Every state-changing or paid API call (settings/credential writes, the
watchlist editor, `POST /api/radar/discover`, and `POST
/api/investigate/{ticker}`) requires a matching `X-TIP-Auth` header. A
single random token (`secrets.token_hex(32)`) is generated on first run
and persisted to `.auth_token` inside the SAME persistent volume
`TIP_SETTINGS_PATH`/`TIP_DB_PATH` already use (`/srv/data` in the real
Docker deployment -- the existing `tip-data` named volume, never a new
one), file mode `0600`.

**Plain localhost (no Docker Desktop) / Docker on Linux:** nothing to do.
The frontend automatically calls `GET /api/auth/bootstrap` on first load,
which hands out the token to a browser running ON the same machine as the
backend (checked server-side via the request's real peer IP, not anything
the browser claims) and caches it in `localStorage`.

**Docker Desktop (Windows/Mac) -- QA remediation D1:** `GET
/api/auth/bootstrap` reliably 403s here even for a genuine same-machine
browser. This is a real, confirmed Docker Desktop networking quirk, not a
bug introduced by this app: a browser request made from the Windows/Mac
host arrives at the container through Docker Desktop's internal network
bridge, so the backend sees the bridge gateway address as the peer, never
a real loopback IP -- and the backend correctly refuses to treat that as
proof the request came from the real host (a gateway/bridge IP is not
meaningfully different from "some other device on the same Docker/LAN
network" from the server's point of view; trusting it would be exactly
the kind of spoofable-signal shortcut this app deliberately avoids).

The fix is a one-time **pairing code**, which this is genuinely simpler
than the old fallback: open the UI, and if auto-bootstrap failed you'll
see a "Pair this browser" banner with a code-entry box. Get the current
code by running:

```bash
docker logs <container>   # or: docker compose logs
```

and looking for the most recent line like:

```
TIP Hackathon Edition -- Docker Desktop / browser pairing code: 3f9a1c2b7e8d4a01
```

A fresh code is printed every time the container starts; it also rotates
automatically every 15 minutes and is single-use (entering it once mints
a real session and immediately invalidates that code). Paste it into the
banner -- no `docker exec`, no finding/mounting a file by hand. This is a
secure *provisioning* step, not a network-trust shortcut: the code is
never derivable from the request itself (not the IP, not a header), only
from a channel that requires real Docker CLI/daemon access on the host
machine -- something nobody merely sharing the same LAN/Docker bridge
network can read.

**LAN or remote deployment (`BIND_HOST` set to something other than
`127.0.0.1`/`localhost`, or any reverse proxy):** neither `GET
/api/auth/bootstrap` nor the pairing-code banner is reachable to you with
any advantage over anyone else on that network (the loopback bootstrap
still 403s for a non-loopback peer, same as before; the pairing code
still requires reading `docker logs`/the host-mounted file). Retrieve the
token the same way as before:

```bash
docker exec <container> cat /srv/data/.auth_token
```

**This app provides no transport encryption of its own.** Opening it
beyond localhost (`BIND_HOST=0.0.0.0`/a LAN IP, or any reverse proxy) means
the access token and every request travel in plaintext unless YOU put a
real TLS-terminating reverse proxy in front of it (e.g. Caddy/nginx/
Cloudflare Tunnel) -- do this before exposing the app beyond your own
machine. See `tools/check_deployment_binding.sh` for a sanity check on
the actual published binding.

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
reachable from your LAN or the internet. **QA remediation D2:** both
`http://127.0.0.1:8000/` and `http://localhost:8000/` work out of the
box -- `TrustedHostMiddleware`'s allowed-host list now accepts both
loopback spellings by default (it previously only accepted whichever
single literal value `BIND_HOST` resolved to, so `localhost:8000`
returned 400 even though `127.0.0.1:8000` worked). If you've explicitly
set `ALLOWED_HOST`/`BIND_HOST` to something else (a reverse-proxy
hostname, a specific LAN IP), only that exact value is accepted, same as
before -- this widening only applies to the default loopback case.
`BIND_HOST`/`BIND_PORT` in `.env.example` control this; see that file for
how to open it to a specific LAN interface if you want to reach it from
another device (**never** set `BIND_HOST=0.0.0.0`, which exposes it on
every interface, including any public one). The Settings UI's credential-management
endpoints DO require authentication (the `X-TIP-Auth` token -- see
"Authentication" above); the network-level binding below is your first
line of defense, not your only one. After deploying, verify the actual
published binding matches what you intended:

```bash
tools/check_deployment_binding.sh <your-compose-project-name>
```

This fails loudly if the container is published on `0.0.0.0`/`::` (every
interface) unless you explicitly set `ALLOW_PUBLIC_BIND=1` first.

> **Upgrading an existing deployment to Release 3 (migrations 10 and 11)?** Follow only
> [docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md): it requires a verified full-volume
> U4 snapshot and release-owner approvals A1-A4 before the upgrade, and is the only supported rollback.
> Builds older than Release 3 still offer a generic "AI-agent update prompt" with direct rebuild steps: never
> follow it for an existing deployment.

## Checking for updates

Settings → **Application Updates** shows the installed build and can
check the public GitHub repo for newer commits. This is a **check-and-
inform feature only** -- it never runs `git`, Docker, or anything
destructive by itself. Upgrading an existing installation is a manual,
operator-run procedure that follows only
[docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md). The
"Copy upgrade guidance" button copies text that points to that runbook,
requires a verified U4 snapshot and approvals A1-A4, contains no git or
Docker command, and states that copying it authorizes nothing.

### How the installed version is known

This section is about version stamping on a **fresh install** only. Upgrading an existing deployment follows
only [docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md).

There is no `.git` directory
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

### What the update check reports

**The four states** `POST /api/settings/update-check` (authenticated; and the Settings
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

## Market-wide Radar / background scheduler (Phase D)

A background scheduler can run market-wide discovery (and, bounded,
per-ticker evaluation) on an interval, reusing the SAME screener and
evaluation pipeline Market Discovery and Ticker Analysis already use --
it is not a second data pipeline. It ships **disabled by default** on
every fresh install and upgrade (an upgrade that already has an
explicit setting from a prior version keeps it, never silently resets
or silently turns it on).

Three modes, settable via `POST /api/scheduler/mode` (requires
`X-TIP-Auth` like every other mutating route). `dry_run` and `enabled` arm
recurring provider requests, so the request must also carry `"confirm"`
equal to the mode (e.g. `{"mode": "enabled", "confirm": "enabled"}`);
otherwise the server answers 428 with the per-cycle request budget and
changes nothing. `disabled` needs no confirmation:

* `disabled` -- zero background UW calls, ever.
* `dry_run` -- one bounded screener call per cycle, writes a discovery
  snapshot + ranked candidates. No per-ticker evaluation, no UW calls
  beyond that one screener call.
* `enabled` -- `dry_run`'s discovery step, plus a bounded top-N
  (`TIP_SCHEDULER_TOP_N`, default 5) subset evaluated through the
  existing per-ticker pipeline.

`GET /api/scheduler/status` (also auth-required) reports the current
mode, last/next run, how many candidates the last cycle evaluated, any
pause reason (e.g. the storage guard pausing background writes), and
(QA remediation D9) the **effective running configuration** actually in
effect right now: `top_n`, `scan_interval_s`, `lease_ttl_s`,
`poll_interval_s`, `uw_max_concurrent_requests`, and whether a cycle is
currently in progress (`cycle_in_progress`) -- not just the documented
defaults, the real values this process picked up from its environment.
`GET /api/radar/market-wide` (auth-required) returns the most recent
discovery snapshot's candidates, each tagged `current` / `stale` /
`unevaluated` based on how recently it has a cataloged evaluation
(`TIP_EVAL_FRESHNESS_WINDOW_S`, default 1200s/20min) -- a ticker
evaluated via both a watchlist scan and a discovery/scheduler
evaluation the same day shows exactly one entry (whichever is most
recent), never two conflicting rows.

Market-hours-aware (reuses the real NYSE calendar, `TIP_SCHEDULER_INTERVAL_S`
default 900s/15min) and durably cross-restart-safe: a SQLite-backed
lease (`scheduler_lease` table) ensures a container restart, a crashed-
and-restarted process, or more than one worker process cannot ever run
overlapping collection cycles. Graceful shutdown releases the lease
immediately.

**QA remediation D9: configuration is genuinely per-deployment now.**
`TIP_SCHEDULER_TOP_N` / `TIP_SCHEDULER_INTERVAL_S` / `TIP_SCHEDULER_LEASE_TTL_S`
/ `TIP_SCHEDULER_POLL_S` / `TIP_UW_MAX_CONCURRENT_REQUESTS` were already
read from the environment by the app code, but `docker-compose.yml` never
forwarded them into the container -- setting them in `.env` previously had
no real effect on a Docker deployment. Fixed: the Compose file now passes
all five through (see `.env.example`). `TIP_SCHEDULER_TOP_N=1` is a
supported, safe minimum (bounds exactly one ticker per cycle in `enabled`
mode -- no off-by-one/division issue). Do not raise
`TIP_UW_MAX_CONCURRENT_REQUESTS` above its default of `2` without
independently re-verifying UW's real concurrency ceiling.

**QA remediation D9: bounded "Run once" validation.** `POST
/api/scheduler/run-once` (auth-required) runs **exactly one** discovery
(+bounded top-N evaluation, if the scheduler's current mode is `enabled`;
discovery-only if `dry_run`) cycle on demand and returns to idle --
no new recurring timer is started by calling it, and it never makes an
Anthropic call under any circumstance (same zero-AI-import guarantee as
the automatic scheduler). It reuses the exact same SQLite
`scheduler_lease` the automatic background loop uses, plus an in-process
lock, so a second `run-once` call while one is already executing (or
while the automatic loop happens to be mid-cycle) gets a `409` --
rejected outright, never queued for later. Calling `run-once` while the
scheduler's mode is `disabled` returns `{"ran": false, "reason":
"disabled"}` -- set `dry_run` or `enabled` first. Any `run-once` that will
make provider requests (every call with a `depth`, and the no-depth call
while the mode is not `disabled`) must carry `"confirm": true`; otherwise
it answers 428 with the request budget and runs nothing. A one-time run
never backfills history.

### Single-process by design

This app's UW concurrency guard (`TIP_UW_MAX_CONCURRENT_REQUESTS`,
default **2** -- intentionally one below UW's confirmed hard ceiling of
3 concurrent requests, since the background scheduler now shares this
same budget with interactive requests) is a `threading`-based gate,
confirmed **process-local**: it has no visibility into any other
process. The real Dockerfile's `CMD` runs plain `uvicorn` with no
`--workers` flag -- exactly one worker process. The app refuses to
start (see `app/core/single_process_guard.py`) if it detects
`WEB_CONCURRENCY` / `UVICORN_WORKERS` / `GUNICORN_WORKERS` / a Gunicorn
`--workers` invocation configured above 1, rather than silently running
with an unenforced concurrency ceiling across multiple processes. If
you genuinely need multiple worker processes, the concurrency gate
needs real cross-process coordination (e.g. via the same SQLite lease
pattern the scheduler uses) before raising this -- don't just remove
the guard.

Interactive (user-initiated) UW requests are never starved by
background scheduler traffic competing for the same concurrency
budget: a background call backs off whenever an interactive caller is
waiting for a slot (`app/core/uw_concurrency.py`), rather than racing
it evenly.

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
into `observed_facts`. At public release v10, independent Linux review ran the full backend suite with **1,714
passed, 0 failed, and 16 environment-dependent skips**. Development qualification also passed **41/41 frontend
scripts**. The real MCP transport and Anthropic investigation path were verified end to end during the fresh-install
Live qualification; ordinary automated tests remain network-isolated and use mocks or synthetic fixtures.

### Signal-integrity, replay and network safety

- The pytest suite refuses any non-loopback network connection (`backend/tests/conftest.py`);
  set `TIP_ALLOW_LIVE_NETWORK_TESTS=1` only for a deliberate, bounded live check.
- End-to-end Live-mode tests replay data over a real local socket with
  `tools/uw_replay_server.py`, which implements the live-verified UW pagination
  semantics (`docs/UW_PAGINATION_VERIFICATION.md`):
  - **public:** `fixtures/synthetic_uw/` (fully synthetic) --
    `backend/tests/test_e2e_live_replay_synthetic.py`, `frontend/test_e2e_live_replay_cdp.py`;
  - **private, optional:** a locally built real capture in `fixtures/recorded_uw/`
    (git-ignored, never distributed). Tests that need it skip when it is absent.
- Browser end-to-end: start `tools/uw_replay_server.py --as-of 2026-10-16T15:02:30Z`, a
  backend with `UW_BASE_URL=http://127.0.0.1:<replay port>`, `TIP_REPLAY_CLOCK=2026-10-16T15:02:30Z`
  and matching `BIND_PORT`, then run `frontend/test_e2e_live_replay_cdp.py`.

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

Public hackathon release v10 (`70c5d90790b300cc1bc7ecee6aba24c3d038692a`). A fresh installation from
public GitHub was verified end to end with a user-supplied Unusual Whales key: real dark-pool, options-flow,
screener and price data; 20+ certified sessions per dataset across SPY, QQQ, NVDA, AMD and TSLA; gates evaluated
against that history; real MCP tool calls and Anthropic investigations; and crash/restart recovery. Demo Mode
covers the interface without credentials. Independent Linux review recorded 1,714 backend tests passed with 16
environment-dependent skips; development qualification passed all 41 frontend scripts. Run them with `cd backend
&& TIP_MODE=demo python -m pytest -q` and `frontend/test_*.mjs` (frontend, against a running demo backend).

## License

TIP Hackathon Edition (this repository's public source code) is licensed
under the [MIT License](LICENSE).

A few things the license does **not** cover:

- **Private production TIP is separate.** The production Trading
  Intelligence Platform this project was ported from is a private,
  unpublished codebase with its own Postgres/TimescaleDB/Databento
  infrastructure, baselines, and operational history. It is not included
  in this repository and this license does not apply to it.
- **You need your own Unusual Whales API credentials for Live Mode.**
  This project does not include, proxy, or grant any license to
  Unusual Whales market data. Live Mode simply calls the Unusual Whales
  API using a key you supply yourself, under your own UW account and
  subject to Unusual Whales' own terms.
- **No redistribution rights over third-party market data.** The MIT
  license covers this repository's source code only. It does not grant
  any right to redistribute, resell, or republish Unusual Whales (or any
  other third-party) market data obtained through Live Mode; that
  remains governed entirely by the applicable data provider's own terms.
- **Third-party components retain their own licenses.** Notably,
  `frontend/lightweight-charts.js` (and its `static-demo/` copy) is
  [TradingView Lightweight Charts™](https://github.com/tradingview/lightweight-charts)
  v4.2.0, Copyright (c) 2024 TradingView, Inc., licensed under the
  [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) — its
  original license header is kept intact in the vendored file. Ordinary
  pip dependencies (FastAPI, Uvicorn, Requests, httpx, python-dotenv,
  pytest, the isolated `mcp` client, etc.) are not vendored into this
  repo and remain under their own respective licenses as installed from
  PyPI.
