# HISTORICAL RECORD: Releases 1 and 2 (NOT operational guidance)

> **WARNING: superseded, do not follow.** This file records how the Release 1 (migrations 7-8) and Release 2
> (migration 9) upgrades were verified in October 2026. The rollback methods it mentions (code-only rollback,
> restoring a `*.pre-migration-*.bak`, an operator volume tar without a verified manifest) are **NOT authorized
> for Release 3 or any later release.** The only authoritative upgrade and rollback procedure is
> [../RELEASE3_RECOVERY_RUNBOOK.md](../RELEASE3_RECOVERY_RUNBOOK.md).

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
