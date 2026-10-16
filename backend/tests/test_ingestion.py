"""Phase B: shared ingestion-layer tests. Every test uses an isolated
tmp_path-backed IngestionCatalog (or ':memory:') -- never the real
deployed DB (see app/storage.py's assert_safe_test_db_path pattern this
mirrors)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.ingestion import IngestionCatalog, StorageGuardPausedError, fingerprint, should_refetch
from app.storage_guard import BACKGROUND, ONDEMAND


def _catalog(tmp_path):
    return IngestionCatalog(str(tmp_path / "catalog.db"))


def test_catalog_options_flow_dedupes_by_explicit_id(tmp_path):
    cat = _catalog(tmp_path)
    alert = {"id": "abc123", "created_at": "2026-10-15T14:00:00Z", "total_premium": 1_000_000}
    first = cat.catalog_options_flow_observations("NVDA", [alert])
    second = cat.catalog_options_flow_observations("NVDA", [alert])  # exact re-ingest
    assert first == 1
    assert second == 0  # suppressed by UNIQUE(source_id), not double-inserted


def test_catalog_darkpool_missing_identifier_uses_fingerprint_not_crash(tmp_path):
    """Real UW dark-pool rows (confirmed during this phase's live
    validation) do not reliably carry a usable unique id field -- must
    never crash, and the SAME logical trade re-ingested twice must still
    dedupe via the fingerprint."""
    cat = _catalog(tmp_path)
    trade = {"executed_at": "2026-10-15T14:05:00Z", "price": 450.1, "size": 1000, "premium": 450100.0}
    first = cat.catalog_darkpool_observations("SPY", [trade])
    second = cat.catalog_darkpool_observations("SPY", [trade])
    assert first == 1
    assert second == 0


def test_fingerprint_is_order_and_value_sensitive():
    a = fingerprint("SPY", "2026-10-15", 1.0)
    b = fingerprint("SPY", "2026-10-15", 2.0)
    c = fingerprint("2026-10-15", "SPY", 1.0)
    assert a != b
    assert a != c


def test_catalog_price_bars_dedupe_on_repeated_fetch(tmp_path):
    cat = _catalog(tmp_path)
    candles = [{"t": "2026-10-14", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}]
    first = cat.catalog_price_bars("NVDA", "1d", candles)
    second = cat.catalog_price_bars("NVDA", "1d", candles)
    assert first == 1
    assert second == 0


def test_catalog_signal_evaluation_links_evidence_rows(tmp_path):
    cat = _catalog(tmp_path)
    scan = {"state": "CONFIRMED", "previous_state": "BUILDING", "confidence": "HIGH",
            "transition_rule": "r1"}
    evaluation_id = cat.catalog_signal_evaluation("NVDA", scan, evidence_refs=[
        ("options_flow_observations", 1), ("darkpool_observations", 1),
    ])
    assert evaluation_id is not None
    conn = sqlite3.connect(cat.db_path)
    links = conn.execute(
        "SELECT observation_table, observation_id FROM signal_evaluation_evidence WHERE evaluation_id = ?",
        (evaluation_id,),
    ).fetchall()
    assert set(links) == {("options_flow_observations", 1), ("darkpool_observations", 1)}
    transitions = conn.execute(
        "SELECT from_state, to_state FROM signal_transitions WHERE ticker = 'NVDA'"
    ).fetchall()
    assert transitions == [("BUILDING", "CONFIRMED")]
    conn.close()


def test_catalog_signal_evaluation_no_transition_row_when_state_unchanged(tmp_path):
    cat = _catalog(tmp_path)
    scan = {"state": "STABLE", "previous_state": "STABLE", "confidence": "HIGH"}
    cat.catalog_signal_evaluation("NVDA", scan)
    conn = sqlite3.connect(cat.db_path)
    transitions = conn.execute("SELECT * FROM signal_transitions").fetchall()
    assert transitions == []
    conn.close()


def test_one_evaluation_can_cite_multiple_observations_and_vice_versa(tmp_path):
    """Chad's explicit requirement: the join table must support a
    many-to-many relationship, not just one evidence row per evaluation."""
    cat = _catalog(tmp_path)
    scan1 = {"state": "NEW", "previous_state": None, "confidence": "LOW"}
    scan2 = {"state": "BUILDING", "previous_state": "NEW", "confidence": "MEDIUM"}
    eval1 = cat.catalog_signal_evaluation("NVDA", scan1, evidence_refs=[("darkpool_observations", 5)])
    eval2 = cat.catalog_signal_evaluation("NVDA", scan2, evidence_refs=[("darkpool_observations", 5)])
    assert eval1 != eval2
    conn = sqlite3.connect(cat.db_path)
    rows = conn.execute(
        "SELECT evaluation_id FROM signal_evaluation_evidence WHERE observation_table = 'darkpool_observations' "
        "AND observation_id = 5 ORDER BY evaluation_id"
    ).fetchall()
    assert {r[0] for r in rows} == {eval1, eval2}
    conn.close()


def test_reconcile_correction_never_updates_original_row_appends_new(tmp_path):
    """A revised source record must create a NEW row, never an UPDATE
    that destroys the original."""
    cat = _catalog(tmp_path)
    original = {"id": "trade-1", "executed_at": "2026-10-15T14:00:00Z", "price": 100.0, "size": 10}
    cat.catalog_darkpool_observations("SPY", [original])
    conn = sqlite3.connect(cat.db_path)
    old_id = conn.execute(
        "SELECT id FROM darkpool_observations WHERE source_id = 'trade-1'"
    ).fetchone()[0]
    conn.close()

    corrected = {"id": "trade-1-corrected", "executed_at": "2026-10-15T14:00:00Z", "price": 101.5, "size": 10}
    cat.catalog_darkpool_observations("SPY", [corrected])
    conn = sqlite3.connect(cat.db_path)
    new_id = conn.execute(
        "SELECT id FROM darkpool_observations WHERE source_id = 'trade-1-corrected'"
    ).fetchone()[0]
    cat.reconcile_correction("darkpool_observations", new_id, old_id, reason="UW republished a corrected price")

    original_row = conn.execute("SELECT raw_json FROM darkpool_observations WHERE id = ?", (old_id,)).fetchone()
    assert json.loads(original_row[0])["price"] == 100.0  # untouched -- the correction never overwrote it

    correction = conn.execute(
        "SELECT observation_id, supersedes_observation_id FROM source_corrections WHERE observation_table = ?",
        ("darkpool_observations",),
    ).fetchone()
    assert correction == (new_id, old_id)
    conn.close()


def test_storage_guard_rejects_ondemand_write_when_critical(tmp_path, monkeypatch):
    from collections import namedtuple
    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    cat = _catalog(tmp_path)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: DiskUsage(total=10_000 * 1024 * 1024, used=9_999 * 1024 * 1024, free=1 * 1024 * 1024),
    )
    with pytest.raises(StorageGuardPausedError):
        cat.catalog_darkpool_observations("SPY", [{"id": "t1", "executed_at": "x"}], kind=ONDEMAND)


def test_storage_guard_background_pauses_earlier_than_ondemand(tmp_path, monkeypatch):
    from collections import namedtuple
    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    cat = _catalog(tmp_path)
    monkeypatch.setattr(
        "app.storage_guard.shutil.disk_usage",
        lambda path: DiskUsage(total=10_000 * 1024 * 1024, used=9_000 * 1024 * 1024, free=1_000 * 1024 * 1024),
    )
    # 1000MB free: allowed on-demand, paused in background -- same
    # contract as app/storage_guard.py's own tests, exercised here
    # through the NEW ingestion write path.
    inserted = cat.catalog_darkpool_observations("SPY", [{"id": "t1", "executed_at": "x"}], kind=ONDEMAND)
    assert inserted == 1
    with pytest.raises(StorageGuardPausedError):
        cat.catalog_darkpool_observations("SPY", [{"id": "t2", "executed_at": "y"}], kind=BACKGROUND)


def test_ingestion_job_checkpoint_resume_avoids_double_insert(tmp_path):
    """Simulates an interrupted job: start a job, checkpoint progress,
    then look it up again (as if the process restarted) and confirm the
    checkpoint cursor is recoverable and resuming does not need to
    re-fetch/double-insert anything already cataloged."""
    cat = _catalog(tmp_path)
    job_id = cat.start_ingestion_job("darkpool_day_pagination",
                                      {"ticker": "SPY", "trading_date": "2026-10-14", "older_than": None, "pages": 0})
    trade = {"id": "t1", "executed_at": "2026-10-14T15:00:00Z"}
    cat.catalog_darkpool_observations("SPY", [trade])
    cat.update_ingestion_job(job_id, "running",
                              {"ticker": "SPY", "trading_date": "2026-10-14",
                               "older_than": "2026-10-14T15:00:00Z", "pages": 1})

    # "process restart" -- look the job up fresh by (ticker, date).
    resumed = cat.find_resumable_job("darkpool_day_pagination", {"ticker": "SPY", "trading_date": "2026-10-14"})
    assert resumed is not None
    assert resumed["detail"]["older_than"] == "2026-10-14T15:00:00Z"
    assert resumed["detail"]["pages"] == 1

    # Re-ingesting the same already-collected trade (as a naive resume
    # might do) is still safely deduped, never double-inserted.
    again = cat.catalog_darkpool_observations("SPY", [trade])
    assert again == 0


def test_should_refetch_true_when_nothing_cataloged_yet():
    assert should_refetch(None, freshness_window_s=300) is True


def test_should_refetch_false_within_freshness_window():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    assert should_refetch(now, freshness_window_s=3600) is False


def test_should_refetch_true_outside_freshness_window():
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert should_refetch(old, freshness_window_s=300) is True


def test_discovery_snapshot_and_candidates_cataloged_together(tmp_path):
    cat = _catalog(tmp_path)
    filters = {"min_premium": 1_000_000}
    candidates = [{"ticker": "NVDA", "net_premium": 5_000_000}, {"ticker": "SPY", "net_premium": 2_000_000}]
    snap_id = cat.catalog_discovery_snapshot(filters, candidates, snapshot_at="2026-10-15T14:00:00Z")
    assert snap_id is not None
    conn = sqlite3.connect(cat.db_path)
    rows = conn.execute("SELECT ticker FROM discovery_candidates WHERE snapshot_id = ?", (snap_id,)).fetchall()
    assert {r[0] for r in rows} == {"NVDA", "SPY"}
    conn.close()
