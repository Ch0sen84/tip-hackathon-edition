# Agent Installation Guide — TIP Hackathon Edition

This document is written for a coding agent (Claude Code, Codex, Hermes, or
similar) that has been handed this repository's URL and asked to install
and run it for a user. It is standalone: follow it in order without
needing to read the rest of the repo first. It documents exactly the same
Docker Compose workflow already described in `README.md`'s "Running with
Docker" section — nothing here is a new or alternate install path.

Repository: https://github.com/Ch0sen84/tip-hackathon-edition (public —
this is an early-development, non-production release; see the warning
banner in README.md).

## Ground rules (read before doing anything)

- **Never** ask the user to paste a raw API key (Unusual Whales or AI
  provider) into this chat/session or into any file you write. Keys are
  entered by the user directly into the running app's Settings UI, or via
  the Settings API calls below — which the user runs themselves or
  authorizes you to run against their own local instance only.
- **Never** hardcode a key, token, or secret into any file in this repo
  (including `.env`) on the user's behalf. `.env` ships as a template; the
  only field you fill in automatically is `TIP_MODE=demo`, which needs no
  secret.
- **Never** run unreviewed remote shell scripts (e.g. `curl | bash`). Every
  command in this guide is explicit and inspectable.
- **Never** touch Docker containers, images, volumes, or networks on the
  host that aren't created by this project's own `docker-compose.yml`.
- **Never** send any credential anywhere except to the user's own
  locally-running instance of this app (`http://localhost:8000`). Do not
  relay it to any other host, log, or file.

## Step-by-step installation

### a. Inspect the host

```bash
uname -a                       # OS + kernel
uname -m                       # CPU arch (x86_64 / arm64 / aarch64)
docker --version
docker compose version         # Compose v2 plugin; if missing, try `docker-compose --version`
docker info >/dev/null 2>&1 && echo "Docker daemon is running" || echo "Docker daemon NOT running"
lsof -i :8000 || ss -ltnp | grep :8000 || true   # check if port 8000 is already in use
```

If the Docker daemon isn't running, start it (`sudo systemctl start docker`
on most Linux distros, or launch Docker Desktop on macOS/Windows) before
continuing. If port 8000 is already bound by something else, note it — see
Troubleshooting below for how to remap it.

### b. Verify basic resources

```bash
df -h .                        # confirm a few hundred MB free is enough headroom
free -h 2>/dev/null || vm_stat 2>/dev/null   # RAM; this is a small FastAPI app, 1-2GB free is plenty
```

This app has no heavyweight dependencies (no Postgres/TimescaleDB/GPU); a
normal laptop/VM easily meets requirements.

### c. Clone the repository

```bash
git clone https://github.com/Ch0sen84/tip-hackathon-edition.git
cd tip-hackathon-edition
```

(If the user already has it cloned locally, just `cd` into that existing
directory instead of re-cloning.)

### d. Review and copy the env template

```bash
cat .env.example
cp .env.example .env
```

Read `.env.example` yourself before copying — it documents every variable.
For a first run in demo mode (the default, `TIP_MODE=demo`), **no edits
are required**: demo mode uses the bundled synthetic fixtures and needs no
API key. Leave `UW_API_KEY=` and all other secret-shaped fields blank.

### e. Build and start the app (fresh install only)

**Stop first if TIP is already installed on this host** (a `tip-data`
volume or a `tip-hackathon` container already exists, e.g. `docker volume
ls | grep tip-data`). Upgrading an existing deployment is NOT an install:
never run the command below (or any other rebuild) against it. Building and
starting new code there runs the Release 3 database migrations with no
verified snapshot, no acceptance check and no rollback. The only supported
upgrade and rollback procedure is
[docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md), an
operator procedure with release-owner approvals; an installing agent does
not run it on its own initiative.

For a fresh install (no existing volume):

```bash
docker compose up --build
```

This is the exact command documented in `README.md` under "Running with
Docker" — no custom flags needed. Leave it running in the foreground (or
add `-d` if the user wants it detached), and watch the build/startup logs
for errors.

### f. Verify it's healthy

Once the container reports it's up (or after a few seconds if detached):

```bash
curl -s http://localhost:8000/api/health
```

Confirm the response is successful and that it reports a nonzero
`scenarios_loaded` count matching the number of `scenario_*.json` files
under `fixtures/` (currently 11). If `scenarios_loaded` is `0` or doesn't
match, something is wrong with the fixtures mount/build — see
Troubleshooting.

You can also sanity-check the mode and radar endpoints:

```bash
curl -s http://localhost:8000/api/mode
curl -s http://localhost:8000/api/radar
```

### g. Report the URL

Tell the user the app is available at **http://localhost:8000/** (frontend
and API on the same port). Nothing further is required for demo mode.

Before telling the user it's ready, verify the binding is what they
intended (loopback-only by default) rather than assuming it:

```bash
tools/check_deployment_binding.sh <compose_project_name>
```

This fails loudly if the container ended up published on `0.0.0.0`/`::`
(every interface) instead of the intended `BIND_HOST`. It also fails when
the zero-token **Local Mode** (step h) is in effect inside the container
while the port is published on ANY non-loopback address (a LAN IP, or a
port mapping without a host IP): Local Mode trusts browser-supplied
headers, which a client on the network can forge, so it is only safe when
the port is reachable from this machine alone. Re-run this check after
every change to `.env` or the port mapping.

### h. Authentication: Local Mode vs. Network Mode

For the normal loopback-only install this walkthrough just set up
(`BIND_HOST=127.0.0.1`, the default), the app runs in **Local Mode** --
see README.md "Local Mode / Network Mode" for the full design. A real
browser opening the app at `http://localhost:8000` works immediately,
with **zero authentication prompts**: no token, no banner, no agent
action needed. This is enforced server-side via the browser's own
`Sec-Fetch-Site`/`Origin` headers, not via a token the frontend has to
fetch first -- there is nothing for an installing agent to retrieve for
a normal user-facing verification pass in this mode.

If you (the installing agent) changed `BIND_HOST` to something other
than loopback, or explicitly set `TIP_LOCAL_MODE=0`, the app is in
**Network Mode** instead -- the pre-existing `X-TIP-Auth` bootstrap/
pairing-code token model (unchanged from the prior QA-remediation
round) applies to every settings/credential write, the watchlist
editor, Market Discovery, and Investigate with AI.

**Driving a mutating API call yourself via `curl` for verification**
(e.g. the Live-mode examples in step i below): by default, a plain
`curl` command never sends `Sec-Fetch-Site`/`Origin` headers at all, so
it falls through to needing the Network Mode token regardless of which
mode the app is actually running in. Be precise about WHY, though
(Windows Docker Desktop QA round 2, finding S1): this is **not** because
Local Mode somehow detects and blocks curl specifically, or because
these headers are enforced for non-browser clients -- they are not.
`Sec-Fetch-Site`/`Origin` are headers a real BROWSER computes and
attaches itself, which a remote malicious WEBSITE's page cannot forge;
that is the actual, real security property Local Mode relies on. A
**local** script (curl, a Python `requests` call, anything else running
on this same machine) can simply set `Sec-Fetch-Site: same-origin`
itself and get the exact same zero-token bypass a real browser tab
does -- Local Mode provides **zero protection against an already-
running local process**, by design, because that process already has
arbitrary code execution on this machine and is already inside the
trust boundary the whole scheme (and every other localhost-bound dev
tool) relies on. Don't use `curl -H "Sec-Fetch-Site: same-origin"` as a
shortcut here even though it would work -- always fetch a real token so
your verification pass exercises the SAME code path a real browser (or
a real LAN/remote caller, who cannot forge these headers through an
intervening network) goes through. Fetch the token the same way a
browser's auto-bootstrap would (this call only succeeds because you're
running on the same machine as the container):

```bash
TIP_TOKEN=$(curl -s http://localhost:8000/api/auth/bootstrap | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
```

If that 403s (meaning you are NOT on the same machine as the container --
e.g. driving a remote host over SSH), retrieve it via `docker exec`
instead:

```bash
TIP_TOKEN=$(docker exec <container_name_or_id> cat /srv/data/.auth_token)
```

Then pass it as a header on every mutating call: `-H "X-TIP-Auth: $TIP_TOKEN"`.
Never paste this token into chat or any log the user will see shared
publicly -- treat it like any other local secret.

### i. Enabling Live UW mode (optional, user-driven)

Live mode replaces synthetic fixtures with real Unusual Whales API calls.
This requires a real UW API key, which **only the user** should supply,
and only into their own running instance:

1. Point the user to `http://localhost:8000/` → Settings page, where they
   can paste their key directly into the app's own UI. This is the
   preferred path.
2. If the user instead asks you to drive it via API on their behalf (still
   only against their own local instance, and only because they explicitly
   asked), the calls are:
   ```bash
   # TIP_TOKEN from step h above -- every one of these is now a mutating
   # call and 401s without it.
   curl -s -X POST http://localhost:8000/api/settings/uw-key \
     -H 'Content-Type: application/json' -H "X-TIP-Auth: $TIP_TOKEN" \
     -d '{"api_key": "<the user enters/supplies this themselves>"}'

   curl -s -X POST http://localhost:8000/api/settings/uw-key/test \
     -H "X-TIP-Auth: $TIP_TOKEN"

   curl -s -X POST http://localhost:8000/api/settings/mode \
     -H 'Content-Type: application/json' -H "X-TIP-Auth: $TIP_TOKEN" \
     -d '{"mode": "live"}'
   ```
   `uw-key/test` makes one real live call (stock-info for SPY) and reports
   `success`, `unauthorized`, `forbidden_tier_limited`, `network_error`, or
   `not_configured` — check this before switching mode.

Do not type the key value into these commands yourself from anything you
were told in chat — the user must supply it directly (e.g. by running the
`curl` command themselves, or pasting into the Settings UI).

### i. Optional AI / MCP configuration

- The optional "Investigate with AI" feature's LLM provider key (Anthropic
  or OpenAI) has **no environment variable**. It is configured exclusively
  through the running app's own Settings API:
  ```bash
  curl -s -X POST http://localhost:8000/api/settings/ai-key \
    -H 'Content-Type: application/json' \
    -d '{"provider": "anthropic", "api_key": "<user-supplied>"}'
  ```
  Same rule as above: the user supplies the value, never you.
- `INVESTIGATE_TRANSPORT=mcp` (an alternate transport for that same
  feature, default is `rest`) requires the isolated `mcp_isolated` venv.
  As of this repo's current Dockerfile, that venv is **built automatically
  as part of the image build in step e** — no extra manual step is
  needed inside Docker. (The separate manual venv setup documented in
  README.md under "Running locally (no Docker)" is only relevant if the
  user runs the backend outside Docker.)

### j. Troubleshooting

- **Port 8000 already in use**: either stop whatever else is using it, or
  choose another host port by setting `BIND_PORT` in `.env` (e.g.
  `BIND_PORT=8080`) and use that port instead (`http://localhost:8080/`).
  **Never edit the `ports:` line of `docker-compose.yml`.** `BIND_PORT=8080`
  renders the mapping as `127.0.0.1:8080:8000`: the `127.0.0.1:` host-IP
  prefix keeps the port reachable from this machine only, and the same
  value reaches the app inside the container, so its allowed Origin is
  `http://127.0.0.1:8080` / `http://localhost:8080`. Two edits that look
  equivalent are not:
  - a bare `8080:8000` (no host IP) publishes on EVERY interface while the
    container still believes it is loopback-bound, so the zero-token Local
    Mode stays on and anyone on the network who forges two browser headers
    can change settings without a token;
  - `127.0.0.1:8080:8000` typed into `ports:` with `BIND_PORT` left at
    8000 keeps the app's allowed Origin at port 8000, so every browser
    action then fails with 403.

  If a Compose override really must define the port, write it with the
  host-IP prefix exactly as `127.0.0.1:8080:8000` AND set `BIND_PORT=8080`.
  Then re-run `tools/check_deployment_binding.sh` (step g).
- **Which `BIND_HOST` values work**: use the literal `127.0.0.1` (the
  default) for a loopback-only install. `localhost` is rejected by Compose
  ("invalid IP address"), and IPv6 loopback (`::1`, `[::1]`) is not
  supported: the browser's Host/Origin never match what the app expects.
  A specific LAN address (e.g. `192.168.1.50`) switches the app to Network
  Mode (token required); `0.0.0.0` exposes every interface and needs
  `ALLOW_PUBLIC_BIND=1` for the binding check to pass.
- **A `.env` setting has no effect inside Docker**: `docker-compose.yml`
  forwards only the variables listed in its `environment:` block. These
  are documented in `.env.example` or read by the app but are NOT
  forwarded, so setting them in `.env` changes nothing in the container:
  `PUBLIC_DEMO`, `ALLOWED_HOST`, `ALLOWED_ORIGIN`, `TIP_AUTH_TOKEN_PATH`,
  `TIP_STORAGE_GUARD_BACKGROUND_MIN_MB`, `TIP_STORAGE_GUARD_ONDEMAND_MIN_MB`,
  `TIP_BOUNDS_RETRIES_PER_WINDOW`, `TIP_BOUNDS_RETRY_DELAY_S`, and the Release 3 collector settings
  `TIP_COVERAGE_SETTLE_MINUTES`, `TIP_COVERAGE_GAP_MIN_MINUTES`, `TIP_COVERAGE_GAP_CONFIRM_HOURS`,
  `TIP_EXTRA_MARKET_CLOSURES`, `TIP_SCHEDULER_BASELINE_BACKFILL` (their defaults are the safe settings). In
  particular `PUBLIC_DEMO=1` does NOT turn a Docker deployment into a
  public demo, and a reverse proxy cannot set `ALLOWED_HOST`/
  `ALLOWED_ORIGIN` this way. `TIP_DB_PATH`, `TIP_DEMO_DB_PATH` and
  `TIP_FIXTURES_DIR` are fixed by the image and volume layout. Do not add
  them to the Compose file on an existing deployment: the Compose file and
  the container's environment are part of the Release 3 rollback
  fingerprint ([docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md)).
- **App refuses to start / logs a `MultiWorkerConfigurationError`
  (Phase D)**: this app is single-process by design -- its UW
  concurrency guard is not safe across multiple worker processes (see
  README.md "Single-process by design"). Do not add `--workers` to the
  Dockerfile `CMD`, and do not set `WEB_CONCURRENCY` /
  `UVICORN_WORKERS` / `GUNICORN_WORKERS` above 1 for this deployment.
- **Market-wide Radar / background scheduler is off and I expected it
  on**: that's the correct default -- it ships `disabled` on every
  fresh install, and an upgrade never silently turns it on. Enable it
  explicitly via `POST /api/scheduler/mode` with `{"mode": "dry_run"}`
  (discovery-only) or `{"mode": "enabled"}` (bounded per-ticker
  evaluation too), with a valid `X-TIP-Auth` header. Never enable this
  on the user's behalf without them explicitly asking for it.
- **Docker daemon not running**: start Docker first (see step a); `docker
  compose up` will fail immediately with a connection error otherwise.
- **New code or dependencies not picked up on an installed app**: do NOT
  rebuild it yourself. Moving an existing deployment to new code is an
  upgrade, and the only supported upgrade (with its full-volume snapshot,
  acceptance and rollback) is
  [docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md),
  run by the operator with release-owner approval. Report the situation to
  the user instead.
- **Anything else unclear from the running container**: check logs:
  ```bash
  docker compose logs
  docker compose logs -f   # follow
  ```
- **`scenarios_loaded` is 0 or doesn't match the fixture count**: confirm
  the `fixtures/` directory exists in the checkout and wasn't excluded by
  a `.dockerignore`/build-context issue (the repository's `.dockerignore`
  keeps every `scenario_*.json`). On a fresh install with no data yet, re-run
  step e after confirming; never on an existing deployment (see the runbook
  above).

### k. Shutdown and uninstall

Clean stop (keeps the image and any named volume):

```bash
docker compose down
```

**Not during an upgrade window.** While a Release 3 upgrade or rollback is
in progress ([docs/RELEASE3_RECOVERY_RUNBOOK.md](docs/RELEASE3_RECOVERY_RUNBOOK.md)),
never run `docker compose down`, `rm` or `up` on the deployment: the
operator follows only the runbook.

Full uninstall (also removes the named volume and the built image; this
permanently deletes all of the app's data, including settings and the
database):

```bash
docker compose down -v
docker image rm $(docker compose images -q) 2>/dev/null || true
```

Then, if desired, delete the cloned directory itself.

## Verification status

These steps were authored to match this repository's own documented and
previously-tested Docker Compose workflow (the same commands as README.md's
"Running with Docker" / "Tests" sections) — they are not a new, invented
install path. However, **this specific document itself has not been
executed end-to-end by a live coding agent in this pass.** This is a
**DOCUMENTATION-ONLY** deliverable: content was derived from reading the
existing, previously-verified README, not independently re-verified by
running these exact steps now.
