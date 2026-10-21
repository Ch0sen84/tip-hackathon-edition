# Release, migration and rollback

**The authoritative Release 3 upgrade and rollback procedure is
[RELEASE3_RECOVERY_RUNBOOK.md](RELEASE3_RECOVERY_RUNBOOK.md).** This file only summarises the migration and holds
the release-level evaluation hold; it contains no procedure of its own.

## Release 3: collector reliability (migrations 10 and 11)

- **Migration 10** is additive: it creates `collection_diagnostics` and `idx_collection_diagnostics_session`.
- **Migration 11** (`coverage_settlement_and_provenance`) is additive: it adds ten nullable columns to
  `session_coverage` (NULL on every existing row) and creates `coverage_reconciliations` and
  `idx_coverage_reconciliations_session`. A deployed candidate therefore reports schema 1–11 (runbook U7).
- The upgrade is built and started only as the runbook's U6 step (clean checkout, pinned Compose project, the
  base image pinned by digest); a plain rebuild of an existing deployment is not an upgrade path.
- At startup the migration code also writes an automatic, database-only `*.pre-migration-*.bak`. That file is
  **not** a Release 3 recovery source. It is never used by the runbook and must not be restored.
- **Rollback is a full restore of the deployment's own verified U4 snapshot of the whole volume, followed by the
  pinned 147e315 image started by immutable ID.** This is the only path, and it runs only through
  `tools/recovery/r3recover.py` as described in the runbook.
- **No verified U4 → no deployment. U4 unavailable or any check failing at rollback time → STOP (BLOCKED).**
- A rollback **discards every write made after U4**, so no collection may run before acceptance is complete,
  and the scheduler stays `disabled`.
- Legacy sessions certified by earlier collectors are **not** re-verified by deploying Release 3 (see the
  runbook, section 9). They are not modified either.

### Evaluation hold (release-level prohibition)
Migration 11 records which collector certified each session (`certified_by`). Release 3 baselines count only
sessions certified by the settled collector (`settled-v1`), so legacy `complete` sessions are **excluded
automatically**; with none certified yet, Live baselines report `insufficient` and no gate can pass on
baseline evidence. The engine no longer consumes legacy sessions.

The operational hold still applies **until the post-deployment acceptance is complete and re-verification is
separately approved**, because a rollback discards every write made after U4:
- no `POST /api/ticker/{t}/refresh` / scan;
- no `run-once` (the API refuses it without an explicit confirmation);
- the scheduler stays `disabled` (arming it also requires an explicit confirmation), and its historical backfill
  stays off (`TIP_SCHEDULER_BASELINE_BACKFILL` unset);
- no baseline job, backfill or reconciliation without separate approval; each states its page budget and
  requires an explicit confirmation.

Release 3 changes what counts as baseline evidence (certified sessions only) and adds the confirmations above.
It does not change gate thresholds, confidence rules or lifecycle rules.

## Earlier releases

The Release 1 and Release 2 upgrade records are kept for audit only, in
[history/RELEASES_1_2_UPGRADE_RECORD.md](history/RELEASES_1_2_UPGRADE_RECORD.md). They are **not** operational
guidance, and none of their rollback methods apply to Release 3.
