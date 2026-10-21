# Release 3 recovery tooling

The operator procedure lives in [docs/RELEASE3_RECOVERY_RUNBOOK.md](../../docs/RELEASE3_RECOVERY_RUNBOOK.md)
(authoritative). This file describes the tools. Running any of them against a deployment is an operator action
that needs the approvals named in the runbook.

| File | Runs where | Purpose |
|---|---|---|
| `r3recover.py` | Docker host (Python 3.11+, stdlib only) | Fail-closed orchestration: `preflight`, `snapshot`, `verify`, `test-restore`, `rollback`, `start-rollback`, `accept` |
| `r3probe.py` | Inside the pinned helper image, volume mounted read-only | Writes the volume manifest: every entry's path, type, size, mode, UID/GID, symlink target and SHA-256. Also reports unsupported entries and SQLite facts (`integrity_check`, foreign keys, migration set, migration-10 objects, scheduler mode, active jobs). |
| `doclint.py` | Anywhere | Fails if an active document offers an unauthorized recovery path, or a hand-off contains an active patch |
| `drill/` | Docker host, disposable only | The rehearsal harness and its synthetic fixtures (see below) |

## Exit codes
`0` success; `2` BLOCKED (a mandatory check failed, the message names it); `3` internal error (treat as
BLOCKED); `64` usage error. `main()` maps any unexpected exception to 3, and any non-zero handler result to 3.

## Design rules (enforced in code)
- **Discovery** uses Compose labels only:
  - exactly one service container;
  - exactly one mount, a named volume with the `local` driver at `/srv/data`;
  - the volume exists (never created) and carries the project label;
  - no other running or restarting container uses it.
- **Images** are accepted only as local `sha256:<64 hex>` IDs.
  - Every `docker run` uses `--pull never --network none`.
  - The rollback Compose override sets `image: <ID>`, `pull_policy: never` and `build: !reset null`. Creation uses `--no-build --pull never`.
  - The rendered config and the created container are inspected **before** start.
- **Snapshot** requires the app to be stopped and the deployment unchanged across the stop.
  - The U4 directory is created exclusively and never overwritten.
  - The archive is a POSIX tar with numeric owners and modes. It is extracted again in the helper and must reproduce the probe manifest exactly.
  - Files are made read-only after writing.
- **Restore** never runs without a passing `test_restore.json` bound to the manifest SHA-256 and the U4-specific approval token. `--dry-run` reaches no destructive step.
  - A forensic copy of the volume is written first.
  - The volume is emptied completely, the whole archive extracted, and every entry compared.
  - A failure records `BLOCKED_PARTIAL`, and `start-rollback` then refuses.
  - Nothing is ever started by `rollback`.
- **SQLite** is examined only on a private copy of each database with its sidecars; the volume is never opened by SQLite.
- **`R3_DRILL_FAULT`** (rehearsal fault injection) is honoured only for volumes labelled `r3.drill=1`. Production volumes do not carry it.

## Tests
- `backend/tests/test_recovery_tool.py` covers the deterministic paths with a faked Docker layer:
  - identity change after the stop, an existing U4 and low disk space;
  - dry run, approval token, partial-state refusal, fault-hook gating and exit codes;
  - manifest comparison, semantic checks including the exact migration names, and U4 tampering;
  - the doc lint.
- `drill/drill.py` runs the real tools end to end in disposable Compose projects:

  ```
  python tools/recovery/drill/drill.py --old-image <147e315 image ID> --candidate-image <candidate image ID> \
      --helper-image <python:3.11-slim image ID> --candidate-commit <candidate commit> --workdir <new empty dir>
  ```

  It covers the positive path and every negative case, and writes `evidence.json` (command, exit code,
  expected and observed outcome, whether the app was running). It exits 0 only if every step passes.

  Every drill resource is labelled `r3.drill=1` or `r3.disposable=1` and uses project names starting with
  `r3drill`. The drill refuses to run against a project or volume without that prefix. Its network is
  `internal: true`; the provider is `drill/fake_uw_server.py` (synthetic data only); all settings, token and
  pairing values are placeholders.
