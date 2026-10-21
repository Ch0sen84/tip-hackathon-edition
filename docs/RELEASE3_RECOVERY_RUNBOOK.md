# Release 3 upgrade and recovery runbook (AUTHORITATIVE)

> **This is the only authoritative Release 3 (migrations 10 and 11) upgrade and rollback procedure.**
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
| E6 | `<BK>`: an existing, absolute, writable, access-restricted local directory. The tool refuses relative paths, symlinks and junctions (any path whose real path differs), UNC/network paths, Docker storage paths (`docker-desktop`, `/var/lib/docker`, `wsl.localhost`, `.../volumes/<volume>`), and anything resolving inside the volume mountpoint. It never creates the directory. Free space must be ≥ 2.2 × the volume size + 200 MB at `snapshot`, and ≥ 1.1 × the current (candidate) volume size + 200 MB for the forensic copy at `rollback --approve` (checked after the stop and **before** the token is consumed). | UNKNOWN until deployment; `snapshot`, `rollback` and `start-rollback` check it |
| E7 | The persisted scheduler mode is `disabled` and no baseline job is active | INFERRED; `snapshot` checks it |
| E8 | **Image retention hold (MANDATORY, before U2).** Building the candidate moves the `tip-hackathon-edition-tip-hackathon:latest` tag; the 147e315 image would then have no tag and an image prune would delete the only rollback image. Before U2 run `docker tag $OLD tip-r3-hold:147e315` and check `docker image inspect tip-r3-hold:147e315 --format '{{.Id}}'` prints `$OLD`. `snapshot` refuses (before stopping anything) while the running image has no tag other than `<project>-<service>:latest`. The tool still starts the image only by ID; the tag only keeps it. Run **no** `docker image prune` or `docker system prune` until Release 3 is accepted or rolled back, and keep the hold tag until then. | Operator duty; `snapshot` enforces the tag |
| E9 | Git Bash: `export MSYS_NO_PATHCONV=1` before running Docker commands | VERIFIED in rehearsal |
| E10 | No container other than the service container references `tip-hackathon-edition_tip-data`, running or stopped. Leftovers are removed only after they are identified and the release owner approves. | UNKNOWN until deployment; every step checks it |
| E11 | No other Docker operator acts on this host during the deployment window | Operator duty |
| E12 | **Never `docker compose down`, `docker rm`, `docker compose up` or a rebuild on the deployment during the window** except the exact U6 command. A failed candidate is stopped by the tool, never removed. (If the service container was removed anyway, R1–R3 still work: see §6.) Never `down -v`: it deletes the volume, and with it any possible rollback. | Operator duty |
| E13 | **Compose inputs unchanged.** The Compose directory, its `docker-compose.yml`, any `docker-compose.override.yml` and `.env` stay exactly as they were at U2 (the release does not change `docker-compose.yml`). U2 records the Compose files, project directory and env file the container was created with (from its Compose labels) and their SHA-256; R3 recreates the rollback container from exactly those, and U7 (with `--u4`) refuses a candidate created from different inputs or with a different runtime. | Operator duty; U2, U7 and R3 check it |
| E14 | **Clean, pinned build.** U6 builds from a clean checkout of the candidate commit: `tools/build_with_version.sh` refuses a working tree with modified or untracked files (its `TIP_ALLOW_DIRTY_BUILD=1` override is for development only and stamps `<commit>-dirty`, which U7 rejects). The base image is pinned in the Dockerfile as `python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce` (the same image as E3), and `.dockerignore` keeps local state, secrets, private captures, host venvs and tests out of the image. Python dependencies other than the 8 direct pins in `backend/requirements.txt` still resolve at build time (not hash-locked in this release). | VERIFIED that the pinned base resolves locally and the context excludes those files; dirty-tree refusal is unit-tested; the U6 build itself is NOT rehearsed (§8) |

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

**The approval token is a procedural gate, not a secret.** It binds A3 to one exact plan (U4, test restore,
volume, service container, images) and makes every approval one-time; it does not authenticate anyone. Anyone
who can run the tool and write to `<BK>` can mint one with `--dry-run`. A3 is therefore enforced by the release
owner relaying the exact token, by E11 and by access restriction of `<BK>`, not by the token itself.

Common arguments: `D="--project tip-hackathon-edition --service tip-hackathon"`,
`V=tip-hackathon-edition_tip-data`,
`H=sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce`, and
`OLD=sha256:89f9bf26bf4f34b68e0e7decf97998dec34f9b2cfc29846fba244bcac3f57851`.
`<COMPOSE_DIR>` is the deployment's Compose directory (the checkout whose directory name gives the project
name `tip-hackathon-edition`).

## 5. Upgrade (U steps)

| Step | Command | Stop gate (the tool exits 2 if any fails) |
|---|---|---|
| U0 Hold tag (E8) | `docker tag $OLD tip-r3-hold:147e315`, then `docker image inspect tip-r3-hold:147e315 --format '{{.Id}}'` | Prints exactly `$OLD`. |
| U1 Preflight (read-only) | `python tools/recovery/r3recover.py preflight $D --expect-volume tip-hackathon-edition_tip-data` | Exactly one service container. Exactly one named local volume at `/srv/data`, existing, labelled for the project. No other mount. No other container, running or stopped, references the volume. Running image is an ID whose `/srv/VERSION` is 147e315. |
| U2 Snapshot (A1) | `python tools/recovery/r3recover.py snapshot $D --expect-volume tip-hackathon-edition_tip-data --backup-root <BK> --helper-image $H --stop` | See "Snapshot gates" below. |
| U3 Verify | `python tools/recovery/r3recover.py verify --u4 <BK>/<U4_ID>` | `SHA256SUMS` lists exactly `volume.tar` and `manifest.json`; both match; format `r3-u4/2` (U4s from earlier tool versions are refused); `u4_id` equals the directory name; the manifest's archive SHA-256 equals the file. |
| U4 Test restore | `python tools/recovery/r3recover.py test-restore --u4 <BK>/<U4_ID> --helper-image $H` | Restored into a **new** disposable volume (labelled `r3.disposable=1`, always removed). Every entry and every SQLite fact must equal U4. Writes `test_restore.json` bound to the manifest's SHA-256. |
| U5 Record | Write `<U4_ID>`, the U4 absolute path, `$OLD`, the hold tag and the U4 manifest's `compose_inputs` (Compose files, project directory, env file) into the deployment record. | **No verified, test-restored U4 → no deployment.** |
| U6 Candidate (A2) | In `<COMPOSE_DIR>`, checked out cleanly at the candidate commit (E14), with the **project pinned**: `tools/build_with_version.sh -p tip-hackathon-edition tip-hackathon`, then `docker compose -p tip-hackathon-edition up -d --no-build tip-hackathon`. Record the image ID the build script prints. Never run U6 from another checkout or worktree, without `-p`, or with extra `-f`/`--env-file` arguments: the container must be created from the same Compose inputs as the U4 container (E13). | Migrations 10 and 11 run at startup. |
| U7 Acceptance (read-only) | `python tools/recovery/r3recover.py accept $D --expect-volume $V --u4 <BK>/<U4_ID> --expect-image <candidate id> --expect-commit <candidate commit> --expect-schema 11` | The service container mounts `$V`. **No other running container** uses the candidate image (a candidate started under another project is reported, by name and ID, and not stopped by the tool). With `--u4`: the volume/project/service identity, the complete runtime fingerprint and the Compose inputs equal the U4 container's (only the image differs), and the U4 image is still present. Health ok, version = candidate, scheduler `disabled`, no active job, provider request counter `[0, 0]` across the quiet interval, schema 1–11, `quick_check` ok. Any failure stops the candidate. |

**Snapshot gates (U2):**
- **Backup root and lock.** `<BK>` is validated as in E6, and the lock is taken.
- **Commit.** The deployed image commit must be 147e315.
- **Hold tag (E8).** Before anything is stopped, the deployed image must carry a tag other than `<project>-<service>:latest`.
- **Compose inputs (E13).** Before anything is stopped, every Compose file and env file named in the container's Compose labels must be readable on this host (they are recorded with their SHA-256 in the manifest as `compose_inputs`).
- **Stop.** A running app requires `--stop`. After the stop, the tool rediscovers and requires:
  - the container is `exited` or `created`;
  - the same container ID, image ID, runtime fingerprint and volume identity as before.
- **Volume probe.** The volume is probed read-only. SQLite files are examined on a private copy with their sidecars, so the source is never modified. The probe requires:
  - no unsupported entries (hard links, FIFOs, sockets, devices);
  - every SQLite file is one of: the primary DB `tip_hackathon.db` (required), the Demo DB `tip_hackathon.demo.db` (optional), or a `*.pre-migration-*.bak` of either. Any other SQLite database blocks.
  - every DB passes `integrity_check` = ok with 0 foreign-key violations, and contains no migration-10 or migration-11 objects;
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
  - the Compose inputs (`compose_inputs`: config files in order, project directory, env file(s) or the implicit `<project dir>/.env`, each with its SHA-256) and the hold tag(s);
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

**If the service container no longer exists** (for example someone ran `docker compose down` after a failed
candidate, against E12), R1–R3 still apply unchanged: the tool then requires the U4 volume to exist with exactly
the U4 identity (name, creation time, labels, mount point) and **no** container of any kind to reference it, binds
the approval to "no service container" (a container created after the dry run voids the token), restores the
volume, and R3 creates the container from the U4-recorded Compose inputs. Do not recreate the container by hand
first. If the volume itself is gone (`down -v`), the result is BLOCKED: escalate.

| Step | Command | Stop gate |
|---|---|---|
| R1 Plan (dry run, changes and authorizes nothing) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --rollback-image $OLD --dry-run` | U4 verifies. A passing test restore exists for this exact manifest. The helper ID equals U4's. The manifest passes the semantic checks. **The rollback image is checked before anything else can happen:** an ID (tags refused), present locally, equal to U4's image, `/srv/VERSION` = 147e315 (a missing hold tag is reported as a warning). The current project/service/volume identity equals U4's (a U4 from another deployment is refused); the service container may be absent (see above). Prints the plan, the consequence and a **one-time** approval token `RESTORE:<U4_ID>:<manifest digest>:<volume>:<nonce>`, recorded in `rollback-plan-<nonce>.json`. Each dry run proposes a new token and consumes none. |
| R2 Restore (A3) | `python tools/recovery/r3recover.py rollback $D --u4 <BK>/<U4_ID> --helper-image $H --rollback-image $OLD --approve '<token>' --stop` | Same checks as R1, including the rollback image, so a restore never happens when the image that must start afterwards is missing. The token must exist, be unused and unexpired (60 minutes), and its binding must equal the current state: U4 id, manifest, archive and test-restore digests, volume (name and creation time), service container ID (or its absence), its image ID and the rollback image. The binding is validated first; `--stop` then stops a running candidate; **free space for the forensic copy** (E6) is checked; only then is the token consumed atomically. A replay, or any change to the U4 or the deployment after the dry run, is refused. A forensic copy of the failed volume goes to `forensic-<U4_ID>-<UTC>-<rand>/` with `SHA256SUMS`; it is **never** a restore source. **Immediately before the destructive step** the U4 is re-verified and the deployment rediscovered, and the binding must still hold. State `IN_PROGRESS` → the volume is emptied completely and the whole archive extracted → every entry and SQLite fact compared with U4 → `RESTORED_VERIFIED`. **Nothing is started.** |
| R3 Start pinned image | `python tools/recovery/r3recover.py start-rollback $D --u4 <BK>/<U4_ID> --helper-image $H --rollback-image $OLD` | See "Start gates" below. The Compose files, project directory and env file are taken from U4 (`compose_inputs`); `--compose-file`/`--env-file` may be given only to cross-check them and must be identical. Only a U4 that recorded no Compose inputs needs `--compose-file <COMPOSE_DIR>/docker-compose.yml` (plus `--env-file` if the container was created with one). |
| R4 Sign-off (A4) | Record the acceptance JSON in the deployment record. | Collection stays disabled until separately approved. |

**Start gates (R3).** All of these must hold before anything starts:
- **Lock and backup root.** The lock is taken; `<BK>` is validated as in E6.
- **Restore state.** It is `RESTORED_VERIFIED` for this exact manifest.
- **Images.** The helper ID equals U4's. `--rollback-image` is an ID (tags refused), present locally (never pulled or built), equal to U4's recorded image, and its `/srv/VERSION` is 147e315.
- **Compose inputs.** The U4-recorded Compose files and env file must all exist (an implicit `.env` that existed at U4 must still exist, and one that did not must not have appeared); any operator-given `--compose-file`/`--env-file` must equal them. A content change since U4 is reported; whether it matters is decided by the runtime-fingerprint comparison below.
- **Deployment.** Its identity equals U4's, no service container is running (it may be absent), and no other container (running or stopped) references the volume.
- **Volume.** It is re-probed and must still equal U4 exactly, and pass the semantic checks.
- **Compose override.** The tool writes `rollback-run-<U4_ID>-<UTC>/rollback.override.yml` with `image: <ID>`, `pull_policy: never` and `build: !reset null`, and renders `docker compose -p <project> --project-directory <recorded dir> -f <recorded files…> -f <override> [--env-file <recorded env file>] config`. The rendered config must show exactly that image, no build and `pull_policy: never`.
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

**Deliberately unchanged in this release** (changing any of them alters the runtime fingerprint or the volume's
file ownership, which would block R3 against the U4 container): `docker-compose.yml` (so the variables listed in
INSTALL_AGENT.md as "no effect inside Docker" stay unforwarded), the container running as root (image `User` empty),
no `HEALTHCHECK` and no restart policy. They are hardening items for a later release that takes a new U4.

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
| U2 refused: no hold tag | BLOCKED; nothing stopped | Do U0 (E8), then re-run U2. |
| U2 refused: Compose inputs not readable | BLOCKED; nothing stopped | Run the tool on the Docker host where the Compose directory is reachable; never edit the paths. |
| U6 build refused: dirty working tree | Nothing built | Get a clean checkout of the candidate commit (E14); never use `TIP_ALLOW_DIRTY_BUILD` for a release. |
| U7: the candidate image also runs outside the project | BLOCKED; the in-project container is stopped, the stray one is not | U6 ran without `-p`/from another directory. Stop the named stray container by its ID with release-owner approval (it uses another volume; production data is not in it). Then decide: roll back (R1–R3), or escalate. Never `down` anything. |
| U7: runtime or Compose inputs differ from U4 | BLOCKED; the candidate is stopped | U6 used another Compose file, override or `.env`. Do not retry with other inputs: roll back (R1–R3) or escalate. |
| Service container absent (removed with `down`/`rm`) | R1–R3 still apply | Run R1–R3 as written; do not recreate the container by hand. If the volume is gone too: STOP, escalate. |
| Insufficient free space for the forensic copy | BLOCKED; the token was not used | Free space in `<BK>` (never delete U4 or forensic directories), then re-run R2 with the same token if it has not expired, otherwise R1 again. |
| R3: recorded Compose inputs missing or `--compose-file` differs | BLOCKED; nothing created | Restore the exact recorded files (U5 record); never substitute another file. Escalate. |

## 8. What was rehearsed (VERIFIED) and what was not

**VERIFIED in disposable Compose environments (synthetic data, internal-only networks, fake provider).**
`tools/recovery/drill/drill.py` runs the final scripts end to end:
- **Positive path:** preflight, then snapshot with `--stop`, verify and test-restore. The candidate then starts and migrates to its schema (10 at the last drill run; the drill now takes `--candidate-schema`, default 11), writes jobs and observations and makes failed provider requests. Then come the rollback dry run, the approved restore and the forensic copy. Every post-U4 write was gone, the file set and counts equal U4, and the pinned 147e315 image started via the rollback Compose override. Acceptance passed.
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

- `backend/tests/test_deploy_audit_regressions.py`: the deployment-audit fixes (a removed service container,
  U7 against a stray candidate and against a changed runtime, the rollback image checked before the restore,
  R3 using the U4-recorded Compose inputs, forensic free space before the token is consumed, the hold-tag gate,
  the binding check in Local Mode, the build script's dirty-tree refusal, the doc lint's rebuild rule, and real
  `docker compose config` rendering when Docker is available).

**Drill status.** The drill was updated for the tool changes of the deployment audit (`--rollback-image` on
every rollback, `--expect-volume` on acceptance, `--u4` on the candidate acceptance, the runtime cases editing a
recorded Compose file, `--candidate-schema`) but has **not been re-run** since; re-run it before U1.

**NOT done in production:** no production U4, restore, rollback, upgrade or restart has been performed.

**NOT rehearsed:**
- `tools/build_with_version.sh` and the pinned-project U6 command (the rehearsal candidate was built from `git archive` with the same version stamp);
- the absent-service-container rollback path against real Docker (unit-tested against the fake Docker CLI only);
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
