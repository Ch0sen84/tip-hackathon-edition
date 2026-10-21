# Release, migration and rollback

**Active procedure: Release 3 (migration 10), below. It is the only operational rollback guidance in this file.**
Release 3 supports exactly one rollback: a full-volume restore of the deployment's own verified U4 snapshot.
Code-only rollback and the automatic DB-only `.bak` are **not** supported Release 3 recovery paths.

## Release 3: collector reliability (migration 10): the ONLY active procedure

**Migration 10** is additive: it creates `collection_diagnostics` plus an index. At startup it also writes an automatic, DB-only `*.pre-migration-*.bak`. That file is **not** a rollback source for Release 3 (see below).

### Recovery policy
1. **Full-volume restore is the only supported Release 3 rollback.**
   - Rollback restores the **deployment-specific U4 snapshot**: the exact, checksum-verified, test-restored archive of the whole persistent volume, created **at deployment time** with the application stopped.
2. **Code-only rollback is NOT supported for Release 3.**
   - Never start the 147e315 image on a volume Release 3 has opened.
   - A database check cannot establish that no provider request happened. A failed request writes nothing, and the app's request counters reset on every restart.
   - Old code on a Release 3 volume keeps whatever Release 3 wrote. It would also re-walk sessions Release 3 rejected (`unknown: invalid_rows` / `rows_out_of_order`) and could certify them `complete` (observed in rehearsal).
   - The SQL "zero-count proof" from earlier drafts is **retired** and must not be used.
3. **The automatic DB-only `.bak` is NOT a supported rollback source.**
   - It covers only the database file, not `settings.json`, `.auth_token`, `.pairing_code` or sidecars.
   - It is not the deployment's verified U4 snapshot.
   - Never select a backup by filename pattern or modification time.
4. **No verified U4 snapshot → no deployment.** If the snapshot cannot be created, checksummed **and** test-restored with a matching manifest, **STOP before deploying**.
5. **Missing or damaged U4 snapshot at rollback time → STOP and report BLOCKED.** No fallback.

### Operational implications (read before deploying)
- **A rollback discards every database write made after the U4 snapshot:** observations, coverage, diagnostics, baseline jobs, evaluations, any settings change, and any token or pairing change. Nothing written after the snapshot survives a rollback.
- **Therefore candidate collection must stay disabled until deployment acceptance is complete.** That means no baseline job, no explicit backfill, no ticker refresh or scan, and no chart refresh.
  - Acceptance uses read-only checks only (U7).
  - Any collection after acceptance is a separately approved action.
- **The scheduler must be `disabled`** in the persisted state **before** the candidate starts (U3), **after** it starts (U7), **before** a rollback image starts (R6) and after it starts (R8).
  - Every startup starts the scheduler thread (`main._on_startup` → `SchedulerRunner.start`). Its behaviour depends only on the persisted `scheduler_state.mode`:

    | Mode | Behaviour |
    |---|---|
    | `disabled` | Does nothing |
    | `dry_run` | Discovery requests |
    | `enabled` | Discovery **plus per-ticker evaluations** and a baseline-backfill slice, even while every baseline job is paused |

- **Legacy SPY sessions remain uncertified.**
  - Deploying Release 3 does **not** re-verify the sessions certified by earlier collectors: SPY's 26 dark-pool `complete` sessions (resume-boundary and close-second gaps) and 20 options-flow `complete` sessions (certified without whole-page validation), the session built while in progress, and the 46 derived `session_value_curves`.
  - Release 3 deployment alone repairs no legacy data.
- **SPY evaluation and all historical collection remain separately gated** (evaluation hold, below).

### Pre-deployment and upgrade

Names:
- container `tip-hackathon-edition-tip-hackathon-1`;
- Compose volume `tip-hackathon-edition_tip-data`;
- `<BK>` = an **absolute** host path to a new, deployment-specific backup directory.

Every command runs on the host (Git Bash: `export MSYS_NO_PATHCONV=1`). `[R]` marks steps executed in the disposable rehearsal.

| Step | Action |
|---|---|
| U1 | **Pin the rollback image.** Record `GET /api/settings/version` (must be `147e315`) and the running image ID. `docker tag <image id> tip-hackathon-edition-tip-hackathon:rollback-147e315`. Record the image ID in the deployment record. |
| U2 [R] | **Stop the application:** `docker stop tip-hackathon-edition-tip-hackathon-1`. All later U steps run with it stopped. |
| U3 [R] | **Persisted scheduler check** (read-only): `SELECT mode FROM scheduler_state` must be **`disabled`**. Otherwise STOP. |
| U4 [R] | **Create the exact full-volume snapshot** (this is *the* U4 snapshot):<br>(a) list sidecars (`*-wal`, `*-shm`, `*-journal`) and **keep** them: the archive must contain the volume exactly as it is while stopped;<br>(b) `docker run --rm --network none -v tip-hackathon-edition_tip-data:/src:ro -v "<BK>:/dst" python:3.11-slim sh -c 'tar -C /src -cpf /dst/tip-data-pre-upgrade.tar . && cd /dst && sha256sum tip-data-pre-upgrade.tar > tip-data-pre-upgrade.tar.sha256'`;<br>(c) write `<BK>/u4-manifest.json`, then `sha256sum u4-manifest.json > u4-manifest.json.sha256`. |
| U5 [R] | **Verify the archive and test-restore it.**<br>• `sha256sum -c tip-data-pre-upgrade.tar.sha256` and `sha256sum -c u4-manifest.json.sha256`;<br>• extract into a **new disposable volume**, recompute the manifest, and require it to be **identical** to U4 (every file's SHA-256, schema 9, integrity, counts, fingerprints);<br>• then remove the disposable volume.<br>**Any mismatch or failure: STOP, do not deploy.** |
| U6 | Build the candidate with `tools/build_with_version.sh` (stamps the commit) and start it. Migration 10 runs at startup. |
| U7 [R] | **Acceptance, read-only only** (no job, backfill, refresh, scan or chart refresh):<br>• `GET /api/settings/version` = candidate;<br>• `GET /api/scheduler/status` = `disabled`;<br>• `GET /api/diagnostics/uw-requests` totals = 0, re-read after a few seconds;<br>• no baseline job running;<br>• schema 10. |

**`u4-manifest.json` contents:**
- the archive's absolute path and SHA-256;
- Compose volume identity (`docker volume inspect`: name, `CreatedAt`, labels `com.docker.compose.project` / `com.docker.compose.volume`);
- container name and app image ID (147e315);
- UTC time;
- the SHA-256 of **every file** in the volume, including sidecars, `settings.json`, `.auth_token` and `.pairing_code` (fingerprints only, never contents);
- DB `integrity_check`, schema version, persisted scheduler mode;
- row counts of `darkpool_observations`, `options_flow_observations`, `session_coverage`, `signal_evaluations`, `baseline_jobs`, `session_value_curves`;
- a fingerprint of `session_coverage`.

### Rollback (full-volume restore)
| Step | Action |
|---|---|
| R1 [R] | **Stop the candidate container.** |
| R2 [R] | **Preserve a forensic copy** of the failed upgraded volume when it is safely possible (enough disk, the container stopped): `tar` plus `sha256sum` into a **separate** directory. It is never used for restore. |
| R3 [R] | **Identify the snapshot** from the deployment record: the U4 **absolute path** plus checksum files. `cd <BK> && sha256sum -c tip-data-pre-upgrade.tar.sha256 && sha256sum -c u4-manifest.json.sha256`.<br>**If the archive or manifest is missing, unreadable or fails its checksum: STOP and report BLOCKED.** Do not use a `*.pre-migration-*.bak` or any other file. |
| R4 [R] | **Restore with the application stopped**, into the existing volume. Empty it completely (this removes any post-snapshot `-wal` / `-shm` sidecars and files), then extract: `docker run --rm --network none -v tip-hackathon-edition_tip-data:/d -v "<BK>:/b:ro" python:3.11-slim sh -c 'find /d -mindepth 1 -delete && tar -C /d -xpf /b/tip-data-pre-upgrade.tar'`. The volume then holds exactly the snapshot's files, sidecars included. |
| R5 [R] | **Verify against the U4 manifest** (read-only): every file's SHA-256 equal (database, sidecars, settings and token fingerprints), the same file set, `integrity_check` = ok, schema **9**, row counts and the coverage fingerprint equal. **Any difference: STOP (BLOCKED).** |
| R6 [R] | **Persisted scheduler check before starting:** `scheduler_state.mode` = **`disabled`**. Otherwise STOP. |
| R7 [R] | **Start only the pinned rollback image** (`tip-hackathon-edition-tip-hackathon:rollback-147e315`, whose ID must equal the U1 record). Never start `latest` or the candidate. |
| R8 [R] | **Validate with no provider-triggering action:**<br>• `GET /api/health` ok;<br>• version `147e315`;<br>• scheduler `disabled`;<br>• `GET /api/diagnostics/uw-requests` totals 0, re-read after an interval;<br>• no baseline job running;<br>• schema 9. |

**[R] rehearsal (2026-10-21).** Run in a disposable Docker environment:
- a copy of the deployed schema-9 DB plus **synthetic** settings, token and pairing files, and a synthetic `-wal` sidecar;
- the candidate run against a local fake provider on an internal network, so it really collected;
- the rollback image pinned by ID.

Results:
- U5's test-restore manifest was identical to U4.
- R5 matched U4 exactly, and every post-snapshot write was gone.
- R3 **stopped with BLOCKED** for a deliberately damaged archive and for a missing archive.

**Not rehearsed:**
- U1's tag (the rehearsal pinned the existing image by ID);
- U6's `build_with_version.sh` (the rehearsal built the candidate with `git archive`);
- the production Compose file.

### Evaluation hold (release-level prohibition)
`session_coverage` has no collector-version column, so the engine accepts every legacy `complete` session.

Until legacy sessions are revalidated by corrected collection, or quarantined and excluded under an approved policy, **no evaluation may consume them**:
- no `POST /api/ticker/{t}/refresh` / scan for an affected ticker;
- no `run-once`;
- the scheduler stays `disabled`;
- no historical collection without separate approval.

Nothing in Release 3 changes evaluations, confidence rules, lifecycle state or scheduler behaviour.

## History: Releases 1 and 2 (already deployed; NOT operational guidance)

> These are the records of the release-1 (migrations 7–8) and release-2 (migration 9) upgrades, kept for audit.
> Their rollback notes (code-only rollback, restoring a `*.pre-migration-*.bak`) described those releases only.
> **They do not apply to Release 3, and must not be used for it.** For Release 3, use only the procedure above.

### What changes for an existing install
1. **Schema (additive only).**
   - Migration 7: `lifecycle_episodes`, `lifecycle_session_evidence`, `lifecycle_scans`.
   - Migration 8: `session_coverage`.
   - No existing table is altered or dropped.
   - Before migration 7 runs on an existing DB, `migrations.backup_db_file` writes
     `<db>.pre-migration-<timestamp>.bak` using SQLite's online backup API. Names are unique and never
     overwrite each other.
2. **Lifecycle restart (intentional).**
   - Live lifecycle state starts empty in the new episode tables.
   - Legacy `ticker_scan_log` / `ticker_current_state` rows stay in place as audit history but are no longer
     read: they were unscoped, counted server-calendar dates, and contained Demo rows. The captured
     pre-05c47ca DB holds 92 Demo-attributed scan rows and 11 Demo state rows, FLUX/CATX CONFIRMED among them.
   - Run `tools/audit_demo_live_contamination.py --db <live.db>` (read-only) to see what is there.
   - Legacy `signal_evaluations` rows (`engine_rule_version = 'v1'`) are kept as history but are never
     served as a ticker's current read. After upgrading, every watchlist ticker reads "not yet evaluated"
     (with a note that earlier evaluations came from a previous engine version) until its first real
     evaluation. Found by the Docker upgrade rehearsal, which showed 05c47ca's BUILDING/INVALIDATED verdicts
     in Live before this fix.
3. **Demo database.**
   - Demo now writes to `TIP_DEMO_DB_PATH`, which defaults to `<TIP_DB_PATH>.demo.db` in the same volume.
   - The Docker volume layout is unchanged.
4. **Live behaviour change users will see.**
   - Until ≥ 20 complete prior sessions are collected for a ticker, both real gates report
     `insufficient` and nothing qualifies.
   - This replaces the previous always-passing options gate. Expect a quiet radar after upgrade.
5. **New UW request volume in Live.**
   - Each refresh uses up to 4 pages per dataset for the evidence session.
   - Each evaluating scheduler cycle backfills up to 60 pages for each of 2 rotating watchlist tickers.
   - The explicit backfill route is clamped to 200 pages.
   - All of it goes through the existing 2-concurrent-request gate.
   - Tunable: `TIP_SCAN_COVERAGE_PAGES`, `TIP_BASELINE_BACKFILL_PAGES`,
     `TIP_BASELINE_BACKFILL_TICKERS_PER_CYCLE`, `TIP_BASELINE_LOOKBACK_SESSIONS`.

### Pre-release checklist (needs approval)
- [x] Real UW data excluded from the release branch's entire history (`release/signal-integrity-rc`);
      the private capture is git-ignored; public tests use the synthetic fixture.
- [x] Bounded live verification of UW pagination (10 requests, 2026-10-20; see
      [UW_PAGINATION_VERIFICATION.md](UW_PAGINATION_VERIFICATION.md)).
- [ ] Back up the deployed volume (`tip_hackathon.db`, `settings.json`, `.auth_token`).
- [ ] Review and merge the branch via PR (not a direct push to main).

### Deployment steps (after approval)
1. Stop the container (`docker compose stop`), then back up the **whole volume** read-only. This is the
   procedure verified in the Docker rehearsal:
   ```bash
   docker run --rm --network none -v tip-hackathon-edition_tip-data:/src:ro -v "<backup-dir>:/dst" python:3.11-slim tar -C /src -cpf /dst/tip-data.tar .
   ```
   Record `sha256sum tip-data.tar`.
2. Build with `tools/build_with_version.sh` and start the new image. The migrations run automatically, and
   migration 7 writes its own `*.pre-migration-*.bak` first.
3. Verify:
   - `GET /api/health` returns `replay_clock: null`, and `GET /api/settings/version` returns the new commit.
   - Watchlist tickers read "not yet evaluated", not a legacy BUILDING/INVALIDATED verdict.
   - `GET /api/ticker/SPY/baseline` returns coverage (0 sessions at first).
   - A refresh shows `insufficient`, not `medium`.
4. Optionally run `tools/audit_demo_live_contamination.py` against the migrated DB (read-only).
5. Build baselines deliberately (README, "Building Live baselines"). A disabled scheduler never backfills.

### Verified on disposable copies (2026-10-20)
A copy of a real pre-05c47ca production DB, plus copies of `settings.json` and `.auth_token`, was taken
through this full cycle, with UW pointed at a dead port. **29/29 checks passed:**
1. 05c47ca code migrates it to schema 1–6. Legacy Demo contamination is visible (FLUX = CONFIRMED).
2. Release-candidate startup applies migrations 7–8:
   - One automatic `*.pre-migration-*.bak` is created. It passes `integrity_check` and is content-identical
     to the pre-upgrade DB.
   - All legacy and catalog tables are content-identical before and after.
   - The new lifecycle tables start empty, and legacy contamination does not reach Live (FLUX / SPY have no
     Live state).
   - Config and token files are unchanged.
3. A second startup is a no-op: no migration, no backup, no table change. Two backups in the same second get
   distinct names.
4. Rollbacks:
   - **Code-only** (05c47ca on the migrated DB): starts and reads legacy state exactly as before.
   - **Full DB restore from the automatic `.bak`:** content-identical to pre-upgrade; 05c47ca starts.
   - **Restore from an operator volume copy:** identical; starts.
   - **Re-upgrade after restore:** succeeds.
5. Fresh install: separate Live and Demo DB files, schema 1–8, no pointless backups, Demo activity only in
   the Demo DB.

### Verified in Docker on a copied volume (2026-10-20)
Copy of the deployed `tip-data` volume, isolated Compose project, internal-only network, UW pointed at a dead
port or a local replay:
- **Upgrade:** migrations 7–8 applied, with a valid automatic `.bak` identical to the pre-upgrade DB. The
  only pre-existing table that changed is `schema_version`.
- **Restart:** container recreation is idempotent.
- **Demo:** activity wrote only to `<db>.demo.db`.
- **Failures:** 429, 500, 503, timeout and malformed responses all held state and reported
  `collection_failed`.
- **Code-only rollback** (05c47ca image on the upgraded volume): starts, serves every view, and changes no
  table.
- **Full restore:** from the `.bak` and from the volume tar, the DB is content-identical to the original.
  After a full-volume restore and an 05c47ca start, `settings.json`, `.auth_token` and the DB are
  byte-identical to the original.
- **Re-upgrade after a restore:** succeeds.

### Release 2: baseline cache, baseline builder, retention tools (migration 9)
- **Migration 9** (additive): `session_value_curves` (derived cache, plus `sealed` / `archive_ref` used only by
  `tools/retention.py`) and `baseline_jobs`. The automatic pre-migration `.bak` is taken before it runs, as for
  7–8.
- **Before upgrading:** stop the container and take the same read-only volume tar as for release 1.
- **After upgrading:**
  - nothing collects by itself;
  - the scheduler mode is untouched;
  - baselines are built only from Settings → Historical baselines (Live), by an explicit click, in bounded steps.
- **Code-only rollback** to the release-1 image (`c1570dd`) on a schema-9 volume: starts and changes no table
  (verified in Docker); the old code ignores the new tables. Valid **only if `tools/retention.py prune` was never
  run.**
- **Full rollback:** stop, then either copy the migration-9 `.bak` over the DB or restore the volume tar.
- **If pruning was ever run:** `python tools/retention.py --db <db> restore --archive-dir <dir>` before running
  any older code.
- **Retention is never run by the app.** `prune` is a double-opt-in prototype; do not run it on deployed data
  without a separate decision.

### Explicitly not done
- No push, PR, release, deployment, live-DB migration, or live UW / paid-AI request.
