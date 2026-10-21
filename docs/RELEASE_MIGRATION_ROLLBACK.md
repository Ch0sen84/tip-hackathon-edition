# Release, migration and rollback plan: signal-integrity remediation

**Status: not released.** This document is a plan only. Nothing here has been pushed, deployed or run against
a real installation. Each step needs explicit owner approval.

## What changes for an existing install
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

## Pre-release checklist (needs approval)
- [x] Real UW data excluded from the release branch's entire history (`release/signal-integrity-rc`);
      the private capture is git-ignored; public tests use the synthetic fixture.
- [x] Bounded live verification of UW pagination (10 requests, 2026-10-20; see
      [UW_PAGINATION_VERIFICATION.md](UW_PAGINATION_VERIFICATION.md)).
- [ ] Back up the deployed volume (`tip_hackathon.db`, `settings.json`, `.auth_token`).
- [ ] Review and merge the branch via PR (not a direct push to main).

## Deployment steps (after approval)
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

## Verified on disposable copies (2026-10-20)
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

## Verified in Docker on a copied volume (2026-10-20)
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

## Release 2: baseline cache, baseline builder, retention tools (migration 9)
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

## Release 3: collector reliability (migration 10)

**Migration 10** is additive: it creates `collection_diagnostics` plus an index. Migrations ≥ 7 take an automatic `*.pre-migration-*.bak` first; on the current ~555 MB DB that is about 0.6 GB.

The **[R]** marks are explained in the legend at the end of this section.

### Rollback limits (mandatory)
- **Code-only rollback** to the 147e315 image is allowed **only when it is proven that the candidate made no collector request and no collection write.** The proof is the database check below.
  - It returns all zeros only if no observation, coverage, diagnostic or baseline-job write happened after migration 10 was applied.
  - App telemetry is *not* proof: it resets on every restart.
- **After any candidate collection activity, rollback must restore the exact verified pre-upgrade snapshot** (step U4). Do not use a code-only rollback.
  - Reason (rehearsed): 147e315 re-walks sessions Release 3 left `unknown: invalid_rows` / `rows_out_of_order` and certifies them `complete` under its lenient filter. It also keeps every row and coverage change Release 3 wrote.

Code-only proof. Run it with the app stopped, in a throwaway container mounting the volume read-only. All four counts must be 0:
```sql
-- A = (SELECT applied_at FROM schema_version WHERE version = 10)
SELECT (SELECT COUNT(*) FROM darkpool_observations     WHERE retrieved_at > A)
     + (SELECT COUNT(*) FROM options_flow_observations WHERE retrieved_at > A) AS observations_after,
       (SELECT COUNT(*) FROM session_coverage          WHERE retrieved_at > A) AS coverage_after,
       (SELECT COUNT(*) FROM collection_diagnostics)                          AS diagnostics,
       (SELECT COUNT(*) FROM baseline_jobs             WHERE created_at   > A) AS jobs_after;
```

### Scheduler startup safety (why the check is required)
- Every app startup starts the scheduler thread (`main._on_startup` → `SchedulerRunner.start`). Its behaviour depends only on the persisted `scheduler_state.mode`:

  | Mode | Behaviour |
  |---|---|
  | `disabled` | Does nothing |
  | `dry_run` | One discovery (screener) request per cycle during market hours |
  | `enabled` | Discovery **plus per-ticker evaluations** and the watchlist baseline-backfill slice |

- A baseline job paused by a restart does **not** stop the scheduler. An `enabled` persisted mode would therefore collect and evaluate after an upgrade or rollback even though every job is paused.
- So the persisted mode must be checked **before** starting the candidate **and before** starting the rollback image, and again **after** startup.

### Operator runbook

Names:
- container `tip-hackathon-edition-tip-hackathon-1`;
- volume `tip-hackathon-edition_tip-data`;
- `<BK>` is a new backup directory on the host.

Every command runs on the host with `MSYS_NO_PATHCONV=1` under Git Bash.

#### Upgrade
| Step | Action |
|---|---|
| U1 | Record the running build (`GET /api/settings/version` → `147e315`) and image ID. Tag it for a pinned rollback: `docker tag <image id> tip-hackathon-edition-tip-hackathon:rollback-147e315`. |
| U2 [R] | Stop the app: `docker stop tip-hackathon-edition-tip-hackathon-1`. |
| U3 [R] | List sidecars while stopped: `tip_hackathon.db-wal`, `-shm`, `-journal`. **Never delete them.** A stopped database's `-wal` can hold committed data; the full-volume snapshot below captures them with the DB. |
| U4 [R] | Full-volume snapshot, checksum and manifest. This is **the only restore point**. Record its path and SHA-256. `docker run --rm --network none -v tip-hackathon-edition_tip-data:/src:ro -v "<BK>:/dst" python:3.11-slim sh -c 'tar -C /src -cpf /dst/tip-data-pre-upgrade.tar . && cd /dst && sha256sum tip-data-pre-upgrade.tar > tip-data-pre-upgrade.tar.sha256'`. Also record a manifest (schema version, `integrity_check`, row counts of `darkpool_observations`, `options_flow_observations`, `session_coverage`, `signal_evaluations`, `baseline_jobs`) from a read-only container. |
| U5 [R] | Persisted scheduler check (read-only, app stopped): `SELECT mode FROM scheduler_state` must be **`disabled`**. If it is not, stop and resolve it **before** any startup. |
| U6 | Build the candidate with `tools/build_with_version.sh` (stamps the commit) and start it. Migration 10 runs at startup. |
| U7 [R] | After startup, with **no** refresh, backfill, job or scan:<br>• `GET /api/scheduler/status` = `disabled`;<br>• `GET /api/diagnostics/uw-requests` totals = 0 (re-read after a few seconds);<br>• no baseline job running;<br>• schema 10;<br>• a new `*.pre-migration-*.bak` exists. |

#### Rollback
| Step | Action |
|---|---|
| R0 | Decide the type: run the code-only proof (app stopped). Any non-zero count means **full restore**. |
| R1 [R] | Stop the app. |
| R2 [R] | Preserve the failed post-upgrade volume for forensics: `tar -C /src -cpf /dst/tip-data-post-upgrade-forensic.tar .` plus its `sha256sum`, into a separate directory. |
| R3 [R] | Identify the restore point **only** by the U4 path and checksum: `cd <BK> && sha256sum -c tip-data-pre-upgrade.tar.sha256`. **Never choose among `*.pre-migration-*.bak` files by name or time.** The `.bak` is a DB-only fallback and is used only if the U4 snapshot is unavailable *and* its own recorded checksum is verified. |
| R4 [R] | Restore **into the existing volume** (app stopped). Empty it completely, including `-wal` / `-shm` sidecars, settings and tokens, then extract the snapshot: `docker run --rm --network none -v tip-hackathon-edition_tip-data:/d -v "<BK>:/b:ro" python:3.11-slim sh -c 'find /d -mindepth 1 -delete && tar -C /d -xpf /b/tip-data-pre-upgrade.tar'` |
| R5 [R] | Verify, read-only: `integrity_check` = ok; schema max = **9**; `settings.json` and `.auth_token` present; no sidecars that were absent from the snapshot; row counts **equal the U4 manifest**; `scheduler_state.mode` = **`disabled`** (the persisted check before the rollback startup). |
| R6 [R] | Start the **pinned** 147e315 image (`…:rollback-147e315`, or by image ID). Never start `latest`. |
| R7 [R] | Validate with no provider-triggering action (no refresh, backfill, job, scan or chart refresh): version `147e315`, schema 9, scheduler `disabled`, UW totals 0 (re-read), no job running. |

**Legend.** **[R]** = rehearsed on 2026-10-20 in a disposable Docker environment:
- a copy of the deployed schema-9 DB plus synthetic settings and token files;
- the candidate run against a local fake provider on an internal network, so it really collected;
- the code-only proof correctly refused (1,603 observations, 4 coverage rows and 1 job after migration 10), and in a separate run correctly permitted with zero writes;
- R4 removed simulated stray `-wal` / `-shm` files;
- R5 matched the manifest exactly;
- R6 started by pinned image ID.

**Not rehearsed:** U1's tag, U6's `build_with_version.sh` (the rehearsal built with `git archive`), and the production Compose file.

### Evaluation hold (release-level prohibition)
Deploying Release 3 does **not** re-verify sessions certified by earlier releases. `session_coverage` has no collector-version column, so the engine accepts every legacy `complete` session.

Until legacy sessions are revalidated by corrected collection, or quarantined and excluded under an approved policy, **no evaluation may consume them**. In practice:
- no `POST /api/ticker/{t}/refresh` / scan for an affected ticker;
- no `run-once`;
- the scheduler stays `disabled`.

Affected records today (deployed DB, read-only inventory):
- SPY: 26 `darkpool` `complete` sessions (D1 and D5) and 20 `options_flow` `complete` sessions (certified without whole-page validation);
- the session collected while in progress (late-report limitation);
- the 46 derived `session_value_curves` built from them.

Nothing in this release changes evaluations, confidence rules or lifecycle state.

## Rollback
- **Code.** Redeploy the 05c47ca image. The extra tables are ignored by the old code, and the migrations
  never modify legacy tables. Rows the new version wrote while running (new evaluations, observations) stay,
  and the old code reads them without error; it shows the newest evaluation, not the pre-upgrade verdict.
- **Data.** For the exact pre-upgrade state, stop the container and either:
  - replace `tip_hackathon.db` with the automatic `*.pre-migration-*.bak` (DB only; the upgrade never
    touches `settings.json` or `.auth_token`), or
  - restore the whole volume from the step-1 tar:
    ```bash
    docker run --rm --network none -v <volume>:/dst -v "<backup-dir>:/bk:ro" python:3.11-slim sh -c "rm -rf /dst/* /dst/.[!.]* ; tar -C /dst -xpf /bk/tip-data.tar"
    ```
  The Demo DB file can simply be deleted.
- **Nothing is deleted by this change**, so no data recovery is needed for a code-only rollback.

## Explicitly not done
- No push, PR, release, deployment, live-DB migration, or live UW / paid-AI request.
