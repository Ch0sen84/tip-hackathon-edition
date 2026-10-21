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
| 4 | **CRITICAL**: a container that failed acceptance could not be confirmed stopped | Stop the named container by hand **now**, confirm it is stopped, escalate. The application is not in a safe state. |
| 64 | Usage error | Fix the command line. Nothing was changed. |

No tool step returns 0 after a failure. The tool never restarts the application after a failed check. If
acceptance fails after a start, the tool itself stops that exact container and confirms it is stopped before
exiting 2.

**Concurrency.** `snapshot`, `test-restore`, `rollback` (including `--dry-run`) and `start-rollback` hold an
exclusive lock file `<BK>/.r3recover-<project>.lock` for their whole run, and refuse to run while **any** other
container, running *or stopped*, references the volume. A lock left behind by a crashed run is never removed
automatically: confirm that no recovery operation is running, then delete it by hand. These guards exclude
concurrent use of this tool and of Compose/Docker through ordinary paths. They cannot stop a Docker
administrator who deliberately changes the volume through the daemon in the seconds between the final
re-probe and `docker start`; the deployment window must have no other Docker operator.

## 3. Prerequisites (verify at deployment time)

| # | Prerequisite | Label |
|---|---|---|
| E1 | Docker Desktop with Compose v2 supporting `!reset` (rehearsed with Compose v5.6.0) | VERIFIED in rehearsal; recheck on the deployment host |
| E2 | Python 3.11+ on the host to run the tool | VERIFIED in rehearsal |
| E3 | Helper image present locally **by ID**: `python:3.11-slim` = `sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce` (GNU tar 1.35, Python 3.11) | INFERRED for production (present on the host; recheck the ID) |
| E4 | Production container runs the 147e315 image `sha256:89f9bf26bf4f34b68e0e7decf97998dec34f9b2cfc29846fba244bcac3f57851`, whose `/srv/VERSION` has `BUILD_COMMIT=147e3154b001cd6e73dde16e612978864ff6f500` | INFERRED from read-only inspection; `preflight` rechecks it |
| E5 | Exactly one mount on the service container: named local volume `tip-hackathon-edition_tip-data` at `/srv/data` | INFERRED from read-only inspection; `preflight` rechecks it |
| E6 | `<BK>`: an existing, absolute, writable, access-restricted local directory. The tool refuses relative paths, symlinks and junctions (any path whose real path differs), UNC/network paths, Docker storage paths (`docker-desktop`, `/var/lib/docker`, `wsl.localhost`, `.../volumes/<volume>`), and anything resolving inside the volume mountpoint. It never creates the directory. Free space must be ≥ 2.2 × the volume size + 200 MB. | UNKNOWN until deployment; `snapshot`, `rollback` and `start-rollback` check it |
| E7 | The persisted scheduler mode is `disabled` and no baseline job is active | INFERRED; `snapshot` checks it |
| E8 | **Image retention hold.** Building the candidate moves the `latest` tag; the 147e315 image then has no tag and an image prune would delete it. Run **no** `docker image prune` or `docker system prune` until Release 3 is accepted or rolled back. Optionally add a hold tag (`docker tag sha256:89f9…f57851 tip-r3-hold:147e315`); the tool never uses it. | Operator duty |
| E9 | Git Bash: `export MSYS_NO_PATHCONV=1` before running Docker commands | VERIFIED in rehearsal |
| E10 | No container other than the service container references `tip-hackathon-edition_tip-data`, running or stopped. Leftovers are removed only after they are identified and the release owner approves. | UNKNOWN until deployment; every step checks it |
| E11 | No other Docker operator acts on this host during the deployment window | Operator duty |

`U4` directories, manifests, forensic copies and rollback-run directories are **sensitive**: they contain the
deployment's settings, token and pairing files. Keep them access-restricted; never attach them to tickets,
bundles or chat.

## 4. Approval gates

| Gate | Who | Before |
|---|---|---|
| A1 Deploy approval | Release owner | Stopping production for U4 |
| A2 Go/no-go after U4 and test restore | Release owner | Starting the candidate |
| A3 Rollback approval | Release owner, passing the exact one-time token printed by `rollback --dry-run` | Any change to the volume |
| A4 Post-rollback sign-off | Release owner | Re-enabling any collection |

Common arguments: `D="--project tip-hackathon-edition --service tip-hackathon"`,
`H=sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`, and
`OLD=sha256:89f9bf26bf4f34b68e0e7decf97998dec34f9b2cfc29846fba244bcac3f57851`.

## 5. Upgrade (U steps)

| Step | Command | Stop gate (the tool exits 2 if any fails) |
|---|---|---|
| U1 Preflight (read-only) | `python tools/recovery/r3recover.py preflight $D --expect-volume tip-hackathon-edition_tip-data` | Exactly one service container. Exactly one named local volume at `/srv/data`, existing, labelled for the project. No other mount. No other container, running or stopped, references the volume. Running image is an ID whose `/srv/VERSION` is 147e315. |
| U2 Snapshot (A1) | `python tools/recovery/r3recover.py snapshot $D --expect-volume tip-hackathon-edition_tip-data --backup-root <BK> --helper-image $H --stop` | See "Snapshot gates" below. |
| U3 Verify | `python tools/recovery/r3recover.py verify --u4 <BK>/<U4_ID>` | `SHA256SUMS` lists exactly `volume.tar` and `manifest.json`; both match; format `r3-u4/2` (U4s from earlier tool versions are refused); `u4_id` equals the directory name; the manifest's archive SHA-256 equals the file. |
| U4 Test restore | `python tools/recovery/r3recover.py test-restore --u4 <BK>/<U4_ID> --helper-image $H` | Restored into a **new** disposable volume (labelled `r3.disposable=1`, always removed). Every entry and every SQLite fact must equal U4. Writes `test_restore.json` bound to the manifest's SHA-256. |
| U5 Record | Write `<U4_ID>`, the U4 absolute path and `$OLD` into the deployment record. | **No verified, test-restored U4 → no deployment.** |
| U6 Candidate (A2) | From the candidate checkout in the Compose directory: `tools/build_with_version.sh`, then `docker compose up -d tip-hackathon`. Record the candidate image ID. | Migration 10 runs at startup. |
| U7 Acceptance (read-only) | `python tools/recovery/r3recover.py accept $D --expect-image <candidate id> --expect-commit <candidate commit> --expect-schema 10` | Health ok, version = candidate, scheduler `disabled`, no active job, provider request counter `[0, 0]` across the quiet interval, schema 1–10, `quick_check` ok. |

**Snapshot gates (U2):**
- **Backup root and lock.** `<BK>` is validated as in E6, and the lock is taken.
- **Commit.** The deployed image commit must be 147e315.
- **Stop.** A running app requires `--stop`. After the stop, the tool rediscovers and requires:
  - the container is `exited` or `created`;
  - the same container ID, image ID, runtime fingerprint and volume identity as before.
- **Volume probe.** The volume is probed read-only. SQLite files are examined on a private copy with their sidecars, so the source is never modified. The probe requires:
  - no unsupported entries (hard links, FIFOs, sockets, devices);
  - every SQLite file is one of: the primary DB `tip_hackathon.db` (required), the Demo DB `tip_hackathon.demo.db` (optional), or a `*.pre-migration-*.bak` of either. Any other SQLite database blocks.
  - every DB passes `integrity_check` = ok with 0 foreign-key violations, and contains no migration-10 objects;
  - the primary DB has exactly the 147e315 schema-9 migration set (versions **and** names);
  - the Demo DB's migration set is an exact prefix 1..k of the 147e315 set;
  - both contain every application table, index and view, and every column, that 147e315 creates for their schema state (`tools/recovery/schema9_reference.json`, generated inside the 147e315 image). A structurally valid SQLite file that is not a TIP database is refused;
  - pre-migration backups carry a prefix migration set (or none, for a first-run backup);
  - scheduler `disabled` and 0 active baseline jobs in the primary, and 0 active jobs in the Demo DB.
- **Free space.** The tool checks free space in `<BK>`.
- **The U4 directory.** `U4-<project>-<UTC>-<random>` is created exclusively and is never overwritten. The archive is a POSIX tar with numeric owners and modes. It is then extracted inside the helper and must reproduce the probe manifest exactly.
- **Source re-probe.** After the archive check the source volume is probed again and must still equal what was archived.
- **Final check.** The deployment is rediscovered: unchanged and still stopped, with no other container referencing the volume.
- **On any failure after the U4 directory exists**, no manifest is written and a `SNAPSHOT_FAILED` marker records why, so that U4 can never verify.
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
| R1 Plan (dry run, changes and authorizes nothing) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --dry-run` | U4 verifies. A passing test restore exists for this exact manifest. The helper ID equals U4's. The manifest passes the semantic checks. The current project/service/volume identity equals U4's (a U4 from another deployment is refused). Prints the plan, the consequence and a **one-time** approval token `RESTORE:<U4_ID>:<manifest digest>:<volume>:<nonce>`, recorded in `rollback-plan-<nonce>.json`. Each dry run proposes a new token and consumes none. |
| R2 Restore (A3) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --approve '<token>' --stop` | Same checks as R1. The token must exist, be unused and unexpired (60 minutes), and its binding must equal the current state: U4 id, manifest, archive and test-restore digests, volume (name and creation time), service container ID and image ID. It is then consumed atomically; a replay, or any change to the U4 or the deployment after the dry run, is refused. `--stop` stops a running candidate. A forensic copy of the failed volume goes to `forensic-<U4_ID>-<UTC>-<rand>/` with `SHA256SUMS`; it is **never** a restore source. **Immediately before the destructive step** the U4 is re-verified and the deployment rediscovered, and the binding must still hold. State `IN_PROGRESS` → the volume is emptied completely and the whole archive extracted → every entry and SQLite fact compared with U4 → `RESTORED_VERIFIED`. **Nothing is started.** |
| R3 Start pinned image | `python tools/recovery/r3recover.py start-rollback $D --u4 <BK>/<U4_ID> --helper-image $H --rollback-image $OLD --compose-file <COMPOSE_DIR>/docker-compose.yml --env-file <COMPOSE_DIR>/.env` | See "Start gates" below. |
| R4 Sign-off (A4) | Record the acceptance JSON in the deployment record. | Collection stays disabled until separately approved. |

**Start gates (R3).** All of these must hold before anything starts:
- **Lock and backup root.** The lock is taken; `<BK>` is validated as in E6.
- **Restore state.** It is `RESTORED_VERIFIED` for this exact manifest.
- **Images.** The helper ID equals U4's. `--rollback-image` is an ID (tags refused), present locally (never pulled or built), equal to U4's recorded image, and its `/srv/VERSION` is 147e315.
- **Deployment.** Its identity equals U4's, no service container is running, and no other container (running or stopped) references the volume.
- **Volume.** It is re-probed and must still equal U4 exactly, and pass the semantic checks.
- **Compose override.** The tool writes `rollback-run-<U4_ID>-<UTC>/rollback.override.yml` with `image: <ID>`, `pull_policy: never` and `build: !reset null`. The rendered `docker compose config` must show exactly that.
- **Create without starting.** `docker compose create --no-build --pull never --force-recreate`. The topology is then rediscovered (exactly one service container, no other volume user) and the created container inspected **before starting**. Its image, volume mapping and complete runtime fingerprint must equal the recorded source container; every difference is listed and blocks:
  - `Cmd`, `Entrypoint`, `WorkingDir`, `User`, `Healthcheck`, exposed ports, stop signal and timeout, TTY/stdin flags, non-Compose labels;
  - environment keys plus a hash of all values;
  - published ports, restart policy, privileged/read-only flags, capabilities, security options, network/PID/IPC/UTS/user/cgroup namespace modes, devices, sysctls, tmpfs, extra hosts, DNS, groups, init, runtime, resource limits, ulimits, log configuration, binds/mounts, masked/read-only paths;
  - network attachments: names, aliases, IPAM configuration, links, driver options;
  - mounts: type, name, source, destination, driver, read-write, mode, propagation.
- **Immediately before start.** The U4 is re-verified, the restore state re-checked, the volume **re-probed and required to equal U4 exactly**, and the deployment rediscovered (same created container, still not started, no other volume user). Any change blocks the start.
- **Start and accept.** Only then is it started, and acceptance runs: health ok, version 147e315, scheduler `disabled`, no active job, provider request counter `[0, 0]` across the quiet interval, schema 1–9, `quick_check` ok.
- **Failed acceptance.** On any failure (wrong version, scheduler not `disabled`, schema mismatch, requests observed, a probe error or exception) the tool stops that exact container, confirms it is stopped and exits 2. If it cannot confirm the stop it exits 4 (CRITICAL). `accept` (U7) behaves the same way for the candidate.

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
| Volume or topology changed after the rollback container was created | BLOCKED; not started | Re-run R1 and R2 in full (new token, new A3 approval), then R3. |
| Acceptance after start fails | BLOCKED; the tool stopped the container | Do not restart it. Escalate. |
| Exit 4 (CRITICAL) | The container may still be running | Stop it by hand immediately, confirm it is stopped, escalate. |
| Approval token refused (used, expired, or the state changed) | BLOCKED; nothing changed | Run R1 again and obtain a new A3 approval for the new token. |
| Lock held | BLOCKED; nothing changed | Confirm no other recovery operation is running; only then remove the lock file by hand. |
| Another container references the volume | BLOCKED; nothing changed | Identify it; remove it only with release-owner approval; re-run the step. |
| Backup root refused | BLOCKED; nothing changed | Use an existing, absolute, real local directory outside Docker storage (E6). |

## 8. What was rehearsed (VERIFIED) and what was not

**VERIFIED in disposable Compose environments (synthetic data, internal-only networks, fake provider).**
`tools/recovery/drill/drill.py` runs the final scripts end to end:
- **Positive path:** preflight, then snapshot with `--stop`, verify and test-restore. The candidate then starts and migrates to schema 10, writes jobs and observations and makes failed provider requests. Then come the rollback dry run, the approved restore and the forensic copy. Every post-U4 write was gone, the file set and counts equal U4, and the pinned 147e315 image started via the rollback Compose override. Acceptance passed.
- **Negative matrix:** every case exits nonzero and leaves the application stopped. The evidence records the exact command, exit code and expected and observed outcome for each. Since v7 it also covers:
  - a stopped container referencing the volume (at snapshot and at start);
  - the volume changed after `compose create` (`docker start` provably never ran);
  - runtime differences in command, healthcheck and working directory;
  - acceptance failing after start (the container is stopped again), and a standalone acceptance failure;
  - approval replay and an approval issued before the service container was recreated;
  - a Demo DB that is not a TIP database, and an unexpected SQLite database;
  - the source changed after the archive check;
  - relative, missing, junction and inside-the-volume (Docker Desktop UNC) backup roots;
  - a held lock.

Unit tests execute the paths a live drill cannot trigger deterministically:
- `backend/tests/test_recovery_tool.py`: internal checks, the exact schema reference, token rules and exit codes.
- `backend/tests/test_recovery_tool_v7.py`: the CLI against a fake Docker CLI (`r3_fake_docker.py`), with mutation injection at exact points. Examples are a volume change after `compose create` (asserting `docker start` is never called), a stop failure after a failed acceptance (exit 4), and token replay and state drift. The same file runs against an older tool through `R3_TOOL_DIR`; run against 80a9305 it fails on every v6 defect.

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

**In-app guidance.** Settings → Application Updates → "Copy upgrade guidance" copies text that only points to
this runbook. It states that it is information, not authorization; it requires a verified U4 and approvals A1–A4;
and it contains no git, Docker or Compose command. Copying it authorizes nothing. Builds older than this release
still carry the earlier generic prompt with direct rebuild steps: never follow it.
