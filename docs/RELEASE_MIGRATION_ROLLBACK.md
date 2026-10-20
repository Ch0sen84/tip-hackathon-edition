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
1. Stop the container. Copy the DB file aside: `docker cp <container>:/srv/data/tip_hackathon.db ./backup/`.
2. Build and start the new image. The migrations run automatically, and migration 7 writes its own backup
   file first.
3. Verify:
   - `GET /api/health` returns `replay_clock: null`.
   - `GET /api/ticker/SPY/baseline` returns coverage (0 sessions at first).
   - A refresh shows `insufficient`, not `medium`.
4. Optionally run `tools/audit_demo_live_contamination.py` against the migrated DB (read-only).

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

**Not exercised: the Docker container path.** The Docker daemon was not running and was deliberately not
started, because the deployed container could auto-start. Static review: `TIP_DB_PATH=/srv/data/...` keeps
the Demo DB and all backups in the persistent `tip-data` volume. The new budget variables are forwarded by
`docker-compose.yml`.

## Rollback
- **Code.** Redeploy the 05c47ca image. The extra tables are ignored by the old code, and the legacy tables
  it reads were never modified, so the old app resumes exactly where it was.
- **Data.** If a full restore is wanted, stop the container and replace `tip_hackathon.db` with the backup
  copy or the automatic `*.pre-migration-*.bak`. The Demo DB file can simply be deleted.
- **Nothing is deleted by this change**, so no data recovery is needed for a code-only rollback.

## Explicitly not done
- No push, PR, release, deployment, live-DB migration, or live UW / paid-AI request.
