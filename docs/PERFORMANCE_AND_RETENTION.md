# Performance, storage and retention (Phase 4, isolated optimization branch)

All measurements below are VERIFIED unless marked INFERRED. They were taken in the release Docker image's Python
3.11 environment (`--cpus 2 --memory 2g`, no network) on a synthetic SPY-scale database:
- 31 sessions;
- 50,000 dark-pool prints and 15,000 flow alerts per session;
- row sizes matching the real catalog.

## 1. Where the time went (before)

`session_baseline.compute()`, dark pool, after the close, 30 eligible prior sessions:

| Metric | Value |
|---|---|
| Wall time | **16.5 s** (8.8 s mid-session); flow 4.9 s |
| Peak RSS | ~465 MB |
| SQL rows fetched | 2.7 M per call. Each session's window query reads its date **±1 day**, so it fetches ~1.74× the rows it uses. |

cProfile shares (dark pool, after close):

| Component | Share of time |
|---|---|
| `darkpool_print_key` (SHA-256 composite identity, `_num` / `round` / `join`) | ~39% |
| JSON decode of `raw_json` | ~26% |
| `datetime.isoformat` per row | ~5% |
| SQL fetch | ~6% |
| Timestamp parse | ~4% |

The same immutable completed sessions were re-parsed on **every** refresh.

**Storage** (one SPY-scale ticker, 31 sessions, 1.67 GB):

| Object | Size |
|---|---|
| Dark-pool table | 0.91 GB (avg `raw_json` 426 B) |
| Flow table | 0.48 GB (avg `raw_json` 906 B) |
| Indexes | 0.28 GB, of which 111 MB are `source_id` unique indexes |

`raw_json` compresses to 10% (dark pool) and 16% (flow) with zlib. Each SPY-scale evaluation row carries ~668 KB of
capped raw evidence (`RAW_ROW_CAP = 500` rows per source).

## 2. Completed-session value curves (implemented)

**Why a curve, not a statistic.** The comparison is evidence-session value **up to its covered point** versus the
**same elapsed slice** of each prior complete session, so a full-session total is useless.
`session_value_curves` (migration 9, additive) stores, per completed session and dataset, the ordered
`(event time µs, value)` sequence of exactly the observations the raw path counts:
- deduplicated (dark-pool composite key, flow `id`);
- canceled prints excluded;
- in catalog order.

**Exactness.** The slice value is `sum()` over the same elements in the same order as the raw path, so it is
bit-identical by construction. This holds in any Python version, including 3.12+ compensated `sum`. Mixed timestamp
formats (whose string order is not chronological) use an order-preserving filter instead of bisect.

**Invalidation.** Validated on every use against the raw window's `(COUNT(*), MAX(id))`, taken **before** any
build. Observation tables are append-only, so late, corrected and canceled prints all force a rebuild. A session
where one key appears at two different times (possible only for legacy-scheme rows) is not cacheable and always uses
the raw path. `TIP_SESSION_CURVE_CACHE=0` disables the cache.

**What is unchanged.** Raw rows stay the source of truth. Coverage, eligibility, look-ahead protection, thresholds
and lifecycle policy are untouched; eligibility is still computed from `session_coverage` on every call.

**Results (SPY scale):**

| Case | Before | After (warm) |
|---|---|---|
| Dark pool, after close | 16.5 s | **0.80 s** |
| Dark pool, mid-session | 8.8 s | **0.53 s** |
| Flow, after close | 4.9 s | **0.23 s** |
| Flow, mid-session | 2.6 s | **0.16 s** |
| SQL rows fetched (dark pool) | 2.7 M | 50 k |
| Statements | 32 | 62 (signature checks) |
| First build per ticker | n/a | ~17 s (one-time) |
| Extra storage | n/a | +31 MB per ticker |

Equivalence: all 8 benchmark cases are bit-identical to the unmodified engine (status, value, mean, standard
deviation, ratio, percentile, per-session history). Equivalence, invalidation and fallback are tested in
`tests/test_session_curve_cache.py`.

**Cold cache, honestly.** The first baseline computation after a session becomes complete (or after its raw
rows change) builds that session's curve from raw rows. A full first build for an SPY-scale ticker with 30 prior
sessions took ~17 s, about the old per-refresh cost, once. Upgrading therefore makes the first refresh of each
ticker no faster than before; later refreshes are fast.

**Curve storage policy.** Curves are derived data, about 2% of the raw rows they summarize (~1 MB per
SPY-scale session per dataset; 31 MB for 31 sessions). They are kept alongside the raw rows, with no separate
growth policy, and rebuilt automatically whenever invalid. Dropping the table (with the app stopped) is always
safe: curves rebuild on demand. The only exception is curves *sealed* by retention, which must be kept while their
raw rows exist only in archives.

**Remaining bottleneck.** The evidence session itself is still read from raw rows (~0.4–0.6 s for 50 k prints),
dominated again by composite-key hashing. Possible next step: reuse the stored `source_id` (which *is* the composite
key for rows written since the identity fix) and hash only legacy rows. Deferred, because it touches deduplication
semantics.

## 2b. Baseline builds: how Pause and Cancel work

- **Cooperative.** Pause and Cancel are recorded at once (`pause_requested` / `cancel_requested`, shown as
  "requested") but take effect only when the step in progress finishes. The job then becomes `paused` /
  `cancelled`.
- **Step size.** A step is limited to 10 **logical pages** by default (5 per dataset).
- **No wall-clock or attempt bound.**
  - Each page can use up to 3 HTTP attempts (the adapter's bounded retries).
  - Each attempt can wait for the request timeout (20 s by default).
  - A provider `Retry-After` is honoured as given, without a cap.
  - So a step, and therefore a stop, has no fixed duration. In the synthetic rehearsal it took ≤ 6 more requests,
    but that is not a guarantee.
- **Why not per request.** Cancelling inside a collection window could leave a session's coverage mislabelled, so
  cancellation happens only between steps. Coverage is checkpointed per window, so stopping and resuming loses no
  verified work.

## 3. Retention policy (recommended; prototype implemented, OFF by default)

**Data classes and how long to keep them:**

| Data | Needed for | Policy |
|---|---|---|
| Raw observations of recent sessions | current evaluation, evidence display | **keep** at least the newest 90 complete sessions per ticker (CLI minimum 30) |
| Raw observations of the evidence session of any recorded evaluation | reproducing that decision | **never pruned**. For legacy (pre-v2) evaluations, which do not record an evidence session, the New York retrieval date and the preceding trading session are both protected. |
| Completed-session curves | anomaly baselines | keep indefinitely (~1 MB per session per SPY-scale ticker); **sealed** when raw rows are pruned |
| Older raw observations | audit, research, re-derivation | **archive before delete**: per-session gzip JSONL + sha256 manifest, re-verified, restorable with original ids |
| Evaluations and lifecycle history | audit trail | keep. Recommended follow-up: store references instead of the 500-row raw copy (~668 KB per SPY-scale refresh). |
| Research / predictive validation | future outcome studies | the archives are the research corpus; keep them off the live volume (cold storage) |

**Prototype `tools/retention.py`** (operator-run, never by the app; not in the Docker image; no startup,
migration, scheduler or background path calls it):
- **`plan`**: read-only.
- **`archive`**: non-destructive.
- **`prune`**: requires `TIP_RETENTION_ENABLED=1` **and** `--i-understand-this-deletes-raw-rows`. It only prunes
  sessions that are complete, older than `--keep-sessions`, archived and re-verified, have an exact curve, are not an
  evaluation's evidence session, and have no cited rows. It also re-checks that every already-sealed session of that
  ticker is unchanged.
- **`restore`**: re-inserts the archived rows.

The engine does **not** populate `signal_evaluation_evidence` (per-observation citations), so that table alone
protects nothing in practice; the evidence-session rule above is what keeps decisions reproducible.

If new raw rows ever arrive for a sealed session, the baseline **excludes** it (`unknown:sealed_session_changed`)
rather than mixing partial data, until it is restored. Tested in `tests/test_retention_tool.py`.

**Measured at SPY scale** (keep = 10, a measurement setting only):

| Step | Result |
|---|---|
| Archive | 42 session-datasets → 127 MB gzip (raw ≈ 9× larger), 125 s |
| Prune | 1.37 M rows, 68 s |
| `VACUUM` | **1,619 MB → 504 MB**, 6 s |
| Baselines afterwards | bit-identical in all 8 cases |

**Growth projections** (INFERRED from ~51 MB per SPY-scale session including indexes):

| Scenario | 30 sessions | 1 year (252 sessions) |
|---|---|---|
| 1 SPY-scale ticker, no retention | 1.6 GB | ~13 GB |
| 5 liquid tickers, no retention (0.5–1× SPY each) | 4–8 GB | **32–64 GB** |
| 5 liquid tickers, retention keep = 90 | 4–8 GB | live DB plateaus at **12–23 GB**; archives grow ~1.1–2.2 GB/year |
| Evaluations at 26 refreshes/day × 5 SPY-scale tickers (scheduler enabled) | | +~22 GB/year until evaluation payloads become references |

**Upgrade and rollback.** The authoritative Release 3 procedure is
[RELEASE3_RECOVERY_RUNBOOK.md](RELEASE3_RECOVERY_RUNBOOK.md): a full restore of the deployment's verified U4 volume
snapshot is the only rollback. The Release 2 (migration 9) upgrade record is kept for audit only in
[history/RELEASES_1_2_UPGRADE_RECORD.md](history/RELEASES_1_2_UPGRADE_RECORD.md).
- **Retention pruning is prohibited on deployed data** without a separate owner decision. A U4 snapshot of a
  pruned volume would still restore exactly, but pruned rows live only in the retention archives
  (`retention restore`).

**Status and recommendation.** Ship `plan`, `archive` and `restore` as operator tools. `prune` is a
**prototype**: it is off by default, double opt-in, and has been verified only on synthetic data. Do not use it on
the deployed data without a separate owner decision.

Known limitations of pruning:
- chart markers for pruned days disappear;
- raw-row audit lives in the archives, not the DB;
- predictive research must read the archives;
- a sealed session that later receives late rows is excluded from baselines until restored.

**Operating defaults:**
- Retention **disabled**. Run `plan` monthly.
- Consider `archive` + `prune --keep-sessions 90` only after an owner decision, with the app stopped and a volume
  backup taken.
- Keep the archives outside the live volume.
- `tools/` is not in the Docker image; run it from a checkout against a stopped container's volume (or a copy).
