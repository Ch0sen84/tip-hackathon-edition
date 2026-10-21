# Release 3 recovery tooling

The operator procedure lives in [docs/RELEASE3_RECOVERY_RUNBOOK.md](../../docs/RELEASE3_RECOVERY_RUNBOOK.md)
(authoritative). This file describes the tools. Running any of them against a deployment is an operator action
that needs the approvals named in the runbook.

| File | Runs where | Purpose |
|---|---|---|
| `r3recover.py` | Docker host (Python 3.11+, stdlib only) | Fail-closed orchestration: `preflight`, `snapshot`, `verify`, `test-restore`, `rollback`, `start-rollback`, `accept` |
| `r3probe.py` | Inside the pinned helper image, volume mounted read-only | Writes the volume manifest: every entry's path, type, size, mode, UID/GID, symlink target and SHA-256. Also reports unsupported entries and SQLite facts (`integrity_check`, foreign keys, migration set, migration-10 and migration-11 objects, scheduler mode, active jobs). |
| `doclint.py` | Anywhere | Fails if an active document offers an unauthorized recovery path, or a hand-off contains an active patch |
| `schema9_reference.json` | Read by `r3recover.py` | Every application table/index/view and column that 147e315 creates for schema states 1..9 |
| `schema_reference.py` | Inside the 147e315 image (generation); in tests (re-derivation) | Generates `schema9_reference.json` from `app.storage.SCHEMA` + migrations 1..k |
| `drill/` | Docker host, disposable only | The rehearsal harness and its synthetic fixtures (see below) |

## Exit codes
`0` success; `2` BLOCKED (a mandatory check failed, the message names it); `3` internal error (treat as
BLOCKED); `4` CRITICAL (a container that failed acceptance could not be confirmed stopped); `64` usage error.
`main()` maps any unexpected exception to 3, and any non-zero handler result to 3.

## Design rules (enforced in code)
- **Discovery** uses Compose labels only:
  - exactly one service container;
  - exactly one mount, a named volume with the `local` driver at `/srv/data`;
  - the volume exists (never created) and carries the project label;
  - no other container references it, **running or stopped**.
  - `rollback` and `start-rollback` (only) also accept an ABSENT service container: the U4 volume must then exist with the U4 identity and no container at all may reference it; the approval binds to "no container", and `start-rollback` creates it.
- **Compose inputs.** `snapshot` records the service container's Compose files, project directory and env file(s) from its `com.docker.compose.project.*` labels (with SHA-256s) as `compose_inputs`, and refuses if they are unreadable. `start-rollback` renders with exactly those (`--project-directory`, `-f …`, `--env-file`); `--compose-file`/`--env-file` must equal them, and are required only for a U4 without recorded inputs.
- **Rollback image** is checked by `rollback` (dry run and approve) as well as `start-rollback`, before anything destructive: an ID, present, equal to U4's, `/srv/VERSION` = 147e315. `snapshot` refuses, before stopping anything, unless the deployed image carries a hold tag other than `<project>-<service>:latest` (runbook E8).
- **Acceptance** (`accept`) requires `--expect-volume`, refuses while the expected image also runs in a container outside the project (reported, not stopped), and with `--u4` requires the U4 identity, runtime fingerprint and Compose inputs (U7). The probe presents the app's own Host/Origin (`ALLOWED_HOST`/`BIND_HOST`, `BIND_PORT`).
- **Forensic free space** is checked by `rollback --approve` after the stop and before the token is consumed.
- **Lock.** `snapshot`, `test-restore`, `rollback` and `start-rollback` create `<BK>/.r3recover-<project>.lock` with `O_EXCL` and never remove a lock they did not create.
- **Backup root** (`<BK>`, and the parent of every U4 path): absolute, existing, writable, not a symlink or junction (its real path must equal the given path), not UNC/network, not a Docker storage path, and not inside the volume mountpoint. It is never created.
- **Images** are accepted only as local `sha256:<64 hex>` IDs.
  - Every `docker run` uses `--pull never --network none`.
  - The rollback Compose override sets `image: <ID>`, `pull_policy: never` and `build: !reset null`. Creation uses `--no-build --pull never`.
  - The rendered config and the created container are inspected **before** start, against the complete runtime fingerprint of the recorded source container: Cmd, Entrypoint, WorkingDir, User, Healthcheck, ports, environment (keys + value hash), non-Compose labels, HostConfig security/namespace/device/resource/log/mount settings, network attachments (aliases, IPAM, links) and mounts.
  - Immediately before `docker start`, the U4 is re-verified, the volume re-probed (it must equal U4 exactly) and the topology rediscovered.
  - If acceptance fails for any reason, the exact container is stopped and confirmed stopped (exit 2), or the tool exits 4 (CRITICAL).
- **Snapshot** requires the app to be stopped and the deployment unchanged across the stop.
  - The U4 directory is created exclusively and never overwritten.
  - The archive is a POSIX tar with numeric owners and modes. It is extracted again in the helper and must reproduce the probe manifest exactly.
  - The source is re-probed after the archive check and must be unchanged. On any failure after the U4 directory exists, no manifest is written and `SNAPSHOT_FAILED` records why.
  - Files are made read-only after writing.
- **Restore** never runs without a passing `test_restore.json` bound to the manifest SHA-256 and a **one-time** approval token.
  - The token `RESTORE:<U4_ID>:<manifest digest>:<volume>:<nonce>` is proposed by `--dry-run`, which consumes and authorizes nothing, and is recorded in `rollback-plan-<nonce>.json` (60-minute expiry).
  - It is bound to the U4 id, the manifest, archive and test-restore digests, the volume name and creation time, and the service container and image IDs.
  - It is consumed atomically (`<plan>.consumed`); the binding is re-checked immediately before extraction.
  - A forensic copy of the volume is written first.
  - The volume is emptied completely, the whole archive extracted, and every entry compared.
  - A failure records `BLOCKED_PARTIAL`, and `start-rollback` then refuses.
  - Nothing is ever started by `rollback`.
- **SQLite** is examined only on a private copy of each database with its sidecars; the volume is never opened by SQLite.
  - Every SQLite file must be the primary DB (exact 147e315 schema 9), the Demo DB (an exact prefix 1..k of the 147e315 migrations) or a `*.pre-migration-*.bak` of either.
  - The primary and Demo DBs must contain every application object and column of their schema state.
- **`R3_DRILL_FAULT`** (rehearsal fault injection: `interrupt_extract`, `mutate_after_archive`, `mutate_after_create`, `fail_acceptance`) is honoured only for volumes labelled `r3.drill=1`. Production volumes do not carry it.

## Tests
- `backend/tests/test_recovery_tool_v7.py` drives the CLI against `backend/tests/r3_fake_docker.py`, a fake Docker/Compose CLI with mutation hooks. It covers the race after `compose create`, other volume users, the lock, acceptance failures (including exit 4), every runtime-fingerprint field class, the SQLite contract, source change after archive, token replay and state drift, backup roots, forensic failure and partial restore. `R3_TOOL_DIR=<extracted tools/recovery>` runs the same tests against another version of the tool, for reproduction.
- `backend/tests/test_deploy_audit_regressions.py` covers the deployment-audit fixes (absent service container, stray candidate, U7 runtime comparison, rollback image before restore, recorded Compose inputs, forensic space, hold tag, `check_deployment_binding.sh` in Local Mode, the version-stamped build script's dirty-tree refusal, the doc lint's rebuild rule, real `docker compose config` rendering).
- `backend/tests/test_recovery_tool.py` covers the internals:
  - identity change after the stop, an existing U4 and low disk space;
  - dry run, approval token, partial-state refusal, fault-hook gating and exit codes;
  - manifest comparison, semantic checks including the exact migration names, and U4 tampering;
  - the doc lint.
- `drill/drill.py` runs the real tools end to end in disposable Compose projects:

  ```
  python tools/recovery/drill/drill.py --old-image <147e315 image ID> --candidate-image <candidate image ID> \
      --helper-image <python:3.11-slim image ID> --candidate-commit <candidate commit> --workdir <new empty dir> \
      [--candidate-schema 11]
  ```

  The 147e315 image must carry a tag other than `r3drill-tip-hackathon:latest` (runbook E8; its usual
  `tip-hackathon-edition-tip-hackathon:latest` tag qualifies for the drill projects).

  It covers the positive path and every negative case, and writes `evidence.json` (command, exit code,
  expected and observed outcome, whether the app was running). It exits 0 only if every step passes.
  `--tool-dir <dir>` runs the same drill against another version of the tool (used to reproduce the v6 defects).

  Every drill resource is labelled `r3.drill=1` or `r3.disposable=1` and uses project names starting with
  `r3drill`. The drill refuses to run against a project or volume without that prefix. Its network is
  `internal: true`; the provider is `drill/fake_uw_server.py` (synthetic data only); all settings, token and
  pairing values are placeholders.
