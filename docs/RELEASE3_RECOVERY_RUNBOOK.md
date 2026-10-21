# Release 3 upgrade and recovery runbook (AUTHORITATIVE)

> **This is the only authoritative Release 3 (migration 10) upgrade and rollback procedure.**
> Every other document refers here. If anything elsewhere disagrees with this file, this file wins and the
> other text is a defect.
>
> **Reading this document changes nothing.** Every step below is an operator action that needs the approvals
> named in it. None of it has been executed against the production deployment; it was rehearsed only in
> disposable Docker Compose environments with synthetic data (see "What was rehearsed").

Tool: [`tools/recovery/r3recover.py`](../tools/recovery/r3recover.py) (stdlib Python 3.11, run on the Docker host).
Tool reference: [`tools/recovery/README.md`](../tools/recovery/README.md).

## 1. Recovery policy

Every Release 3 rollback restores the **entire, exact, deployment-specific U4 snapshot** of the `/srv/data`
named volume, then starts the **pinned 147e315 image by immutable ID**. Nothing else is a Release 3 rollback.

These are **not authorized**, and the tool provides no way to do them:

| Not authorized | Why |
|---|---|
| Code-only rollback (old image on the Release 3 volume) | Old code keeps Release 3's writes and can re-certify sessions Release 3 rejected. No database query can prove that no provider request happened. |
| Database-only restore from an automatic `*.pre-migration-*.bak` | It covers one file, not settings, token, pairing, Demo DB or sidecars, and is not the verified U4. |
| Choosing a backup by name, age or pattern | Only the U4 recorded for this deployment, verified by checksum and test restore, may be used. |
| Starting an image by tag (`latest`, any name) | Tags are mutable. Only `sha256:<64 hex>` IDs recorded in U4 are accepted; the tool never pulls or builds. |
| Restoring part of the volume | Restore always empties the volume and extracts the whole archive, then verifies every entry. |
| Automatic rollback | Rollback runs only when the operator passes the U4-specific approval token. |

**If a verified U4 is unavailable, or any mandatory check fails: STOP. The result is BLOCKED.** Keep the
application stopped and escalate to the release owner. Never improvise another restoration path.

### What a rollback costs
**A rollback discards every write made after the U4 snapshot**, including:
- observations, coverage and diagnostics;
- baseline jobs and evaluations;
- settings changes, token changes and pairing changes;
- Demo database activity.

Nothing written after U4 survives a rollback; the forensic copy keeps it only for investigation. Therefore,
**until deployment acceptance is complete, no collection may run**:
- no baseline job, explicit backfill, ticker refresh or scan, chart refresh or `run-once`;
- the scheduler stays `disabled`.

## 2. Exit codes and what to do

| Exit | Meaning | Operator action |
|---|---|---|
| 0 | Step passed | Continue to the next step |
| 2 | **BLOCKED**: a mandatory check failed (the message names it) | **STOP.** Do not retry with different inputs, do not start any container, escalate. |
| 3 | Internal error | Treat as BLOCKED. |
| 64 | Usage error | Fix the command line. Nothing was changed. |

No tool step returns 0 after a failure. The tool never restarts the application after a failed check.

## 3. Prerequisites (verify at deployment time)

| # | Prerequisite | Label |
|---|---|---|
| E1 | Docker Desktop with Compose v2 supporting `!reset` (rehearsed with Compose v5.6.0) | VERIFIED in rehearsal; recheck on the deployment host |
| E2 | Python 3.11+ on the host to run the tool | VERIFIED in rehearsal |
| E3 | Helper image present locally **by ID**: `python:3.11-slim` = `sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce` (GNU tar 1.35, Python 3.11) | INFERRED for production (present on the host; recheck the ID) |
| E4 | Production container runs the 147e315 image `sha256:89f9bf26bf4f34b68e0e7decf97998dec34f9b2cfc29846fba244bcac3f57851`, whose `/srv/VERSION` has `BUILD_COMMIT=147e3154b001cd6e73dde16e612978864ff6f500` | INFERRED from read-only inspection; `preflight` rechecks it |
| E5 | Exactly one mount on the service container: named local volume `tip-hackathon-edition_tip-data` at `/srv/data` | INFERRED from read-only inspection; `preflight` rechecks it |
| E6 | `<BK>`: a new, absolute, access-restricted backup root **outside** the volume, with free space ≥ 2.2 × the volume size + 200 MB | UNKNOWN until deployment; `snapshot` checks it |
| E7 | The persisted scheduler mode is `disabled` and no baseline job is active | INFERRED; `snapshot` checks it |
| E8 | **Image retention hold.** Building the candidate moves the `latest` tag; the 147e315 image then has no tag and an image prune would delete it. Run **no** `docker image prune` or `docker system prune` until Release 3 is accepted or rolled back. Optionally add a hold tag (`docker tag sha256:89f9…f57851 tip-r3-hold:147e315`); the tool never uses it. | Operator duty |
| E9 | Git Bash: `export MSYS_NO_PATHCONV=1` before running Docker commands | VERIFIED in rehearsal |

`U4` directories, manifests, forensic copies and rollback-run directories are **sensitive**: they contain the
deployment's settings, token and pairing files. Keep them access-restricted; never attach them to tickets,
bundles or chat.

## 4. Approval gates

| Gate | Who | Before |
|---|---|---|
| A1 Deploy approval | Release owner | Stopping production for U4 |
| A2 Go/no-go after U4 and test restore | Release owner | Starting the candidate |
| A3 Rollback approval | Release owner, passing the exact token printed by `rollback --dry-run` | Any change to the volume |
| A4 Post-rollback sign-off | Release owner | Re-enabling any collection |

Common arguments: `D="--project tip-hackathon-edition --service tip-hackathon"`,
`H=sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`, and
`OLD=sha256:89f9bf26bf4f34b68e0e7decf97998dec34f9b2cfc29846fba244bcac3f57851`.

## 5. Upgrade (U steps)

| Step | Command | Stop gate (the tool exits 2 if any fails) |
|---|---|---|
| U1 Preflight (read-only) | `python tools/recovery/r3recover.py preflight $D --expect-volume tip-hackathon-edition_tip-data` | Exactly one service container. Exactly one named local volume at `/srv/data`, existing, labelled for the project. No other mount. No other running user of the volume. Running image is an ID whose `/srv/VERSION` is 147e315. |
| U2 Snapshot (A1) | `python tools/recovery/r3recover.py snapshot $D --expect-volume tip-hackathon-edition_tip-data --backup-root <BK> --helper-image $H --stop` | See "Snapshot gates" below. |
| U3 Verify | `python tools/recovery/r3recover.py verify --u4 <BK>/<U4_ID>` | `SHA256SUMS` lists exactly `volume.tar` and `manifest.json`; both match; format `r3-u4/1`; `u4_id` equals the directory name; the manifest's archive SHA-256 equals the file. |
| U4 Test restore | `python tools/recovery/r3recover.py test-restore --u4 <BK>/<U4_ID> --helper-image $H` | Restored into a **new** disposable volume (labelled `r3.disposable=1`, always removed). Every entry and every SQLite fact must equal U4. Writes `test_restore.json` bound to the manifest's SHA-256. |
| U5 Record | Write `<U4_ID>`, the U4 absolute path and `$OLD` into the deployment record. | **No verified, test-restored U4 → no deployment.** |
| U6 Candidate (A2) | From the candidate checkout in the Compose directory: `tools/build_with_version.sh`, then `docker compose up -d tip-hackathon`. Record the candidate image ID. | Migration 10 runs at startup. |
| U7 Acceptance (read-only) | `python tools/recovery/r3recover.py accept $D --expect-image <candidate id> --expect-commit <candidate commit> --expect-schema 10` | Health ok, version = candidate, scheduler `disabled`, no active job, provider request counter `[0, 0]` across the quiet interval, schema 1–10, `quick_check` ok. |

**Snapshot gates (U2):**
- **Commit.** The deployed image commit must be 147e315.
- **Stop.** A running app requires `--stop`. After the stop, the tool rediscovers and requires:
  - the container is `exited` or `created`;
  - the same container ID, image ID, runtime fingerprint and volume identity as before.
- **Volume probe.** The volume is probed read-only. SQLite files are examined on a private copy with their sidecars, so the source is never modified. The probe requires:
  - no unsupported entries (hard links, FIFOs, sockets, devices);
  - the primary DB is present;
  - every DB passes `integrity_check` = ok with 0 foreign-key violations;
  - the primary DB has exactly the 147e315 schema-9 migration set (versions **and** names) and no migration-10 objects;
  - scheduler `disabled` and 0 active baseline jobs.
- **Free space.** The tool checks free space in `<BK>`.
- **The U4 directory.** `U4-<project>-<UTC>-<random>` is created exclusively and is never overwritten. The archive is a POSIX tar with numeric owners and modes. It is then extracted inside the helper and must reproduce the probe manifest exactly.
- **Final check.** The deployment is rediscovered: unchanged and still stopped.
- **Manifest.** `manifest.json` records:
  - host and Compose identity (project, service, config labels including the config hash);
  - container ID, volume (name, driver, creation time, labels, mount point), image ID and commit;
  - helper image ID, UTC time and stopped state;
  - archive absolute path, SHA-256 and size;
  - the full volume manifest: path, type, size, mode, UID/GID, symlink target and SHA-256 of **every** entry (hidden files, Demo DB, settings, token, pairing, `.bak`, sidecars, empty directories, symlinks), plus SQLite facts.
- **Read-only files.** Files are made read-only (0400).

**The application stays stopped after U2–U5.** If any U step is BLOCKED before U6, production has not been
changed: the old container can be started again on the unchanged volume with
`docker start tip-hackathon-edition-tip-hackathon-1`, with release-owner approval.

## 6. Rollback (R steps)

A rollback is needed if U6/U7 fail, or if the release owner decides to back out Release 3.

| Step | Command | Stop gate |
|---|---|---|
| R1 Plan (dry run, changes nothing) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --dry-run` | U4 verifies. A passing test restore exists for this exact manifest. The helper ID equals U4's. The manifest passes the semantic checks. The current project/service/volume identity equals U4's (a U4 from another deployment is refused). Prints the plan, the consequence and the approval token `RESTORE:<U4_ID>:<volume>`. |
| R2 Restore (A3) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --approve 'RESTORE:<U4_ID>:<volume>' --stop` | Same checks as R1, then the token must match exactly. `--stop` stops a running candidate. A forensic copy of the failed volume goes to `forensic-<U4_ID>-<UTC>/` with `SHA256SUMS`; it is **never** a restore source. State `IN_PROGRESS` → the volume is emptied completely and the whole archive extracted → every entry and SQLite fact compared with U4 → `RESTORED_VERIFIED`. **Nothing is started.** |
| R3 Start pinned image | `python tools/recovery/r3recover.py start-rollback $D --u4 <BK>/<U4_ID> --helper-image $H --rollback-image $OLD --compose-file <COMPOSE_DIR>/docker-compose.yml --env-file <COMPOSE_DIR>/.env` | See "Start gates" below. |
| R4 Sign-off (A4) | Record the acceptance JSON in the deployment record. | Collection stays disabled until separately approved. |

**Start gates (R3).** All of these must hold before anything starts:
- **Restore state.** It is `RESTORED_VERIFIED`.
- **Images.** The helper ID equals U4's. `--rollback-image` is an ID (tags refused), present locally (never pulled or built), equal to U4's recorded image, and its `/srv/VERSION` is 147e315.
- **Deployment.** Its identity equals U4's, and no service container is running.
- **Volume.** It is re-probed and must still equal U4 exactly, and pass the semantic checks.
- **Compose override.** The tool writes `rollback-run-<U4_ID>-<UTC>/rollback.override.yml` with `image: <ID>`, `pull_policy: never` and `build: !reset null`. The rendered `docker compose config` must show exactly that.
- **Create without starting.** `docker compose create --no-build --pull never --force-recreate`. The created container is then inspected **before starting**. Its image, volume mapping and runtime fingerprint must equal U4's:
  - ports and restart policy;
  - capabilities, security options, privileged and read-only flags, user;
  - networks;
  - environment keys plus a hash of the values.
- **Start and accept.** Only then is it started, and acceptance runs: health ok, version 147e315, scheduler `disabled`, no active job, provider request counter `[0, 0]` across the quiet interval, schema 1–9, `quick_check` ok.

The Compose file at `<COMPOSE_DIR>` is identical in 147e315 and the candidate (no diff); the runtime-fingerprint
comparison blocks if `.env` or the Compose file changed since U4.

## 7. When something fails

| Situation | Result | Do |
|---|---|---|
| U4 missing, unreadable, checksum failure, wrong deployment, no passing test restore | BLOCKED | STOP. Keep the candidate stopped. Escalate. No other backup may be used. |
| Snapshot or test restore fails before U6 | BLOCKED; production unchanged | Do not deploy. The old container may be started again with approval. |
| Restore fails or is interrupted (`BLOCKED_PARTIAL`) | The volume is partial | Keep **everything stopped**; `start-rollback` refuses. Re-run R2 in full (it empties the volume and extracts the whole archive again); a partial result is never patched. If it keeps failing: STOP, escalate. |
| Volume changed between R2 and R3 | BLOCKED | Re-run R2 in full. |
| Rollback image absent, a tag, a different ID, or wrong commit | BLOCKED | STOP. Do not build or pull a replacement; an image that is not the recorded ID is not the rollback image. Escalate. |
| Helper image absent or a different ID | BLOCKED | STOP. A different helper changes tar/probe behaviour; escalate. |
| Created rollback container differs from U4 | BLOCKED; not started | Remove nothing; investigate the Compose file and `.env`. |
| Acceptance after start fails | BLOCKED | `docker stop` the container, escalate. |

## 8. What was rehearsed (VERIFIED) and what was not

**VERIFIED in disposable Compose environments (synthetic data, internal-only networks, fake provider).**
`tools/recovery/drill/drill.py` runs the final scripts end to end:
- **Positive path:** preflight, then snapshot with `--stop`, verify and test-restore. The candidate then starts and migrates to schema 10, writes jobs and observations and makes failed provider requests. Then come the rollback dry run, the approved restore and the forensic copy. Every post-U4 write was gone, the file set and counts equal U4, and the pinned 147e315 image started via the rollback Compose override. Acceptance passed.
- **Negative matrix:** every case exits nonzero and leaves the application stopped. The evidence records the exact command, exit code and expected and observed outcome for each.

Unit tests (`backend/tests/test_recovery_tool.py`) execute the paths a live drill cannot trigger safely:
- identity change after stop;
- existing U4 directory;
- low disk space;
- dry run reaching no destructive step;
- every exit-code mapping.

**NOT done in production:** no production U4, restore, rollback, upgrade or restart has been performed.

**NOT rehearsed:**
- `tools/build_with_version.sh` (the rehearsal candidate was built from `git archive` with the same version stamp);
- the production Compose directory and `.env` (the drill uses its own Compose file and a synthetic environment);
- volumes of production size;
- the Windows host's own file-permission behaviour for `<BK>` access restriction.

## 9. Not changed by Release 3

Release 3 does **not** change evaluations, confidence or threshold rules, lifecycle state or scheduler
behaviour. It does **not** re-verify legacy sessions certified by earlier collectors (SPY: 26 dark-pool and
20 options-flow `complete` sessions, plus 46 derived `session_value_curves`). The **evaluation hold** in
[RELEASE_MIGRATION_ROLLBACK.md](RELEASE_MIGRATION_ROLLBACK.md) stays in force, and retention pruning stays
prohibited.

**Do not use the in-app "Copy AI-agent update prompt"** (Settings → Application Updates) for Release 3. Its
generic rebuild steps skip every gate in this runbook.
