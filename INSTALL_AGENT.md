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

### e. Build and start the app

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
(every interface) instead of the intended `BIND_HOST`. The Settings UI's
credential-management endpoints have no authentication of their own --
they rely entirely on this network-level binding, so this check matters
more than it might look like for a demo app.

### h. Enabling Live UW mode (optional, user-driven)

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
   curl -s -X POST http://localhost:8000/api/settings/uw-key \
     -H 'Content-Type: application/json' \
     -d '{"api_key": "<the user enters/supplies this themselves>"}'

   curl -s -X POST http://localhost:8000/api/settings/uw-key/test

   curl -s -X POST http://localhost:8000/api/settings/mode \
     -H 'Content-Type: application/json' \
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
  as part of `docker compose up --build`** — no extra manual step is
  needed inside Docker. (The separate manual venv setup documented in
  README.md under "Running locally (no Docker)" is only relevant if the
  user runs the backend outside Docker.)

### j. Troubleshooting

- **Port 8000 already in use**: either stop whatever else is using it, or
  edit the port mapping in `docker-compose.yml` (e.g. change the host side
  of the port mapping from `8000:8000` to `8080:8000`) and use that port
  instead. Do not touch any other part of the compose file.
- **Docker daemon not running**: start Docker first (see step a); `docker
  compose up` will fail immediately with a connection error otherwise.
- **Stale build / dependency changes not picked up**: force a clean
  rebuild:
  ```bash
  docker compose build --no-cache
  docker compose up
  ```
- **Anything else unclear from the running container**: check logs:
  ```bash
  docker compose logs
  docker compose logs -f   # follow
  ```
- **`scenarios_loaded` is 0 or doesn't match the fixture count**: confirm
  the `fixtures/` directory exists in the checkout and wasn't excluded by
  a `.dockerignore`/build-context issue; re-run `docker compose up --build`
  after confirming.

### k. Shutdown and uninstall

Clean stop (keeps the image and any named volume):

```bash
docker compose down
```

Full uninstall (also removes the named volume and the built image):

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
