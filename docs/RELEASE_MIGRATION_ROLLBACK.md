# Release, migration and rollback

**The authoritative Release 3 upgrade and rollback procedure is
[RELEASE3_RECOVERY_RUNBOOK.md](RELEASE3_RECOVERY_RUNBOOK.md).** This file only summarises the migration and holds
the release-level evaluation hold; it contains no procedure of its own.

## Release 3: collector reliability (migration 10)

- **Migration 10** is additive: it creates `collection_diagnostics` and `idx_collection_diagnostics_session`.
- At startup the migration code also writes an automatic, database-only `*.pre-migration-*.bak`. That file is
  **not** a Release 3 recovery source. It is never used by the runbook and must not be restored.
- **Rollback is a full restore of the deployment's own verified U4 snapshot of the whole volume, followed by the
  pinned 147e315 image started by immutable ID.** This is the only path, and it runs only through
  `tools/recovery/r3recover.py` as described in the runbook.
- **No verified U4 → no deployment. U4 unavailable or any check failing at rollback time → STOP (BLOCKED).**
- A rollback **discards every write made after U4**, so no collection may run before acceptance is complete,
  and the scheduler stays `disabled`.
- Legacy sessions certified by earlier collectors are **not** re-verified by deploying Release 3 (see the
  runbook, section 9).

### Evaluation hold (release-level prohibition)
`session_coverage` has no collector-version column, so the engine accepts every legacy `complete` session.

Until legacy sessions are revalidated by corrected collection, or quarantined and excluded under an approved policy, **no evaluation may consume them**:
- no `POST /api/ticker/{t}/refresh` / scan for an affected ticker;
- no `run-once`;
- the scheduler stays `disabled`;
- no historical collection without separate approval.

Nothing in Release 3 changes evaluations, confidence rules, lifecycle state or scheduler behaviour.

## Earlier releases

The Release 1 and Release 2 upgrade records are kept for audit only, in
[history/RELEASES_1_2_UPGRADE_RECORD.md](history/RELEASES_1_2_UPGRADE_RECORD.md). They are **not** operational
guidance, and none of their rollback methods apply to Release 3.
