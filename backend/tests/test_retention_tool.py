"""Retention prototype (tools/retention.py): off by default, archive-before-delete,
never removes evidence a recorded decision cites, and keeps baselines bit-identical
by sealing the exact completed-session curves. Synthetic, disposable data only."""
import hashlib
import importlib.util
import json
import random
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.core import session_baseline
from app.ingestion import IngestionCatalog
from tests.test_session_curve_cache import EVIDENCE, T, dp_rows, flow_rows, sessions_before, utc_open_close

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("retention", ROOT / "tools" / "retention.py")
retention = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retention)

KEEP = 5          # tests only; the CLI refuses anything below 30 and defaults to 90


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "ret.db")
    rnd = random.Random(5)
    cat = IngestionCatalog(path)
    for d in sessions_before(EVIDENCE, 24) + [EVIDENCE]:
        o, c = utc_open_close(d)
        cat.catalog_darkpool_observations(T, dp_rows(d, 120, rnd, ("z", "frac")))
        cat.catalog_options_flow_observations(T, flow_rows(d, 60, rnd))
        for ds in ("darkpool", "options_flow"):
            cat.upsert_session_coverage(ds, T, d.isoformat(), "complete", o.isoformat(), c.isoformat(), 1, 1)
    return path


def baselines(path, monkeypatch, cache="1"):
    monkeypatch.setenv("TIP_SESSION_CURVE_CACHE", cache)
    cat = IngestionCatalog(path)
    o, c = utc_open_close(EVIDENCE)
    return [session_baseline.compute(cat, ds, T, EVIDENCE, as_of_utc=t).as_metrics()
            for ds in ("darkpool", "options_flow") for t in (o + timedelta(minutes=47), o + timedelta(hours=4), c)]


def raw_count(path, table="darkpool_observations"):
    return sqlite3.connect(path).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_plan_is_read_only_and_reports_what_would_go(db):
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    plan = retention.cmd_plan(db, KEEP)
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before
    assert plan["rows"] > 0 and len(plan["sessions"]) == 2 * (25 - KEEP)


def test_prune_refuses_without_opt_in_or_archive(db, tmp_path, monkeypatch):
    monkeypatch.delenv("TIP_RETENTION_ENABLED", raising=False)
    with pytest.raises(SystemExit, match="refused"):
        retention.cmd_prune(db, KEEP, tmp_path / "arch", confirmed=True)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    with pytest.raises(SystemExit, match="refused"):
        retention.cmd_prune(db, KEEP, tmp_path / "arch", confirmed=False)
    out = retention.cmd_prune(db, KEEP, tmp_path / "arch", confirmed=True)          # nothing archived yet
    assert out["pruned_sessions"] == [] and all(s["why"] == "not archived" for s in out["skipped"])
    with pytest.raises(SystemExit, match="below 30"):
        retention.main(["--db", db, "plan", "--keep-sessions", "20"])


def test_archive_then_prune_keeps_every_baseline_bit_identical(db, tmp_path, monkeypatch):
    before = baselines(db, monkeypatch, "0")                                       # raw path, pre-retention
    arch = tmp_path / "arch"
    assert retention.cmd_archive(db, KEEP, arch)["archived_sessions"] == 2 * (25 - KEEP)
    rows_before = raw_count(db)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    out = retention.cmd_prune(db, KEEP, arch, confirmed=True)
    assert len(out["pruned_sessions"]) == 2 * (25 - KEEP) and raw_count(db) < rows_before
    assert baselines(db, monkeypatch, "1") == before
    assert baselines(db, monkeypatch, "0") == before          # sealed sessions are honoured even with the cache off


def test_sessions_cited_by_a_recorded_evaluation_are_never_pruned(db, tmp_path, monkeypatch):
    old = sessions_before(EVIDENCE, 24)[0].isoformat()
    conn = sqlite3.connect(db)
    obs_id = retention._day_rows(conn, "darkpool", T, old)[0][0]
    conn.execute("INSERT INTO signal_evaluations (source_id, ticker, event_time, retrieved_at, state, confidence, "
                 "engine_rule_version, raw_json) VALUES ('e1', ?, 'x', 'x', 'NEW', 'low', 'v2-signal-integrity', '{}')", (T,))
    conn.execute("INSERT INTO signal_evaluation_evidence (evaluation_id, observation_table, observation_id, created_at) "
                 "VALUES (1, 'darkpool_observations', ?, 'x')", (obs_id,))
    conn.commit()
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    out = retention.cmd_prune(db, KEEP, arch, confirmed=True)
    assert {"dataset": "darkpool", "ticker": T, "session_date": old, "why": "cited by a recorded evaluation"} in out["skipped"]
    assert conn.execute("SELECT COUNT(*) FROM darkpool_observations WHERE id = ?", (obs_id,)).fetchone()[0] == 1


def test_evidence_sessions_of_recorded_evaluations_are_never_pruned(db, tmp_path, monkeypatch):
    old = sessions_before(EVIDENCE, 24)[2].isoformat()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO signal_evaluations (source_id, ticker, event_time, retrieved_at, state, confidence, "
                 "engine_rule_version, raw_json) VALUES ('e2', ?, 'x', 'x', 'NO_STATE', 'insufficient', "
                 "'v2-signal-integrity', ?)", (T, json.dumps({"evidence_session": old})))
    conn.commit()
    assert any(s["session_date"] == old and s["protected_by_evaluations"] for s in retention.cmd_plan(db, KEEP)["sessions"])
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    out = retention.cmd_prune(db, KEEP, arch, confirmed=True)
    assert sum(1 for s in out["skipped"] if s["session_date"] == old
               and s["why"] == "evidence session of a recorded evaluation") == 2      # both datasets


def test_legacy_evaluations_without_evidence_session_still_protect_their_days(db, tmp_path, monkeypatch):
    days = sessions_before(EVIDENCE, 24)
    target = days[5]                                         # a legacy v1 scan retrieved during this session
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO signal_evaluations (source_id, ticker, event_time, retrieved_at, state, confidence, "
                 "engine_rule_version, raw_json) VALUES ('legacy', ?, 'x', ?, 'BUILDING', 'medium', 'v1', '{}')",
                 (T, f"{target.isoformat()}T18:00:00+00:00"))
    conn.commit()
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    out = retention.cmd_prune(db, KEEP, arch, confirmed=True)
    protected = {s["session_date"] for s in out["skipped"] if s["why"] == "evidence session of a recorded evaluation"}
    assert {target.isoformat(), days[4].isoformat()} <= protected


def test_legacy_v1_evaluation_without_any_evidence_linkage_protects_retrieval_day_and_previous_session(
        db, tmp_path, monkeypatch):
    """Reviewer-requested regression: a real-shaped pre-v2 evaluation that records NO evidence
    session and has NO signal_evaluation_evidence rows must still keep its raw data unprunable."""
    days = sessions_before(EVIDENCE, 24)
    monday = next(d for d in days[:-KEEP] if d.weekday() == 0 and days.index(d) > 0)
    friday = days[days.index(monday) - 1]
    assert friday.weekday() == 4                                   # previous session crosses the weekend
    legacy_scan = {"ticker": T, "state": "BUILDING", "previous_state": "NEW", "confidence": "medium",
                   "gates": {"dark_pool_anomaly": {"passed": True, "metrics": {}}}, "raw": {}}
    assert "evidence_session" not in legacy_scan
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO signal_evaluations (source_id, ticker, event_time, retrieved_at, state, confidence, "
                 "engine_rule_version, raw_json) VALUES ('legacy-v1', ?, ?, ?, 'BUILDING', 'medium', 'v1', ?)",
                 (T, f"{monday.isoformat()}T12:00:00Z", f"{monday.isoformat()}T12:00:00Z", json.dumps(legacy_scan)))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM signal_evaluation_evidence").fetchone()[0] == 0
    plan = {(s["dataset"], s["session_date"]): s["protected_by_evaluations"] for s in retention.cmd_plan(db, KEEP)["sessions"]}
    for d in (monday, friday):
        assert plan[("darkpool", d.isoformat())] and plan[("options_flow", d.isoformat())]
    rows_before = {d: len(retention._day_rows(conn, "darkpool", T, d.isoformat())) for d in (monday, friday)}
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    out = retention.cmd_prune(db, KEEP, arch, confirmed=True)
    skipped = {(s["dataset"], s["session_date"]) for s in out["skipped"] if s["why"] == "evidence session of a recorded evaluation"}
    for d in (monday, friday):
        assert {("darkpool", d.isoformat()), ("options_flow", d.isoformat())} <= skipped
        assert len(retention._day_rows(conn, "darkpool", T, d.isoformat())) == rows_before[d] > 0
    pruned = {(p["dataset"], p["session_date"]) for p in out["pruned_sessions"]}
    assert pruned and not any(d in (monday.isoformat(), friday.isoformat()) for _, d in pruned)   # targeted, not blanket


def test_late_row_in_a_pruned_session_is_excluded_never_miscounted_and_restore_recovers(db, tmp_path, monkeypatch):
    before = baselines(db, monkeypatch, "0")
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    retention.cmd_prune(db, KEEP, arch, confirmed=True)
    old = sessions_before(EVIDENCE, 24)[3]
    o, _ = utc_open_close(old)
    IngestionCatalog(db).catalog_darkpool_observations(T, [{"ticker": T, "tracking_id": 4242,
        "executed_at": (o + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "price": "101", "size": 7,
        "volume": 1, "premium": "707", "canceled": False}])
    monkeypatch.setenv("TIP_SESSION_CURVE_CACHE", "1")
    b = session_baseline.compute(IngestionCatalog(db), "darkpool", T, EVIDENCE, as_of_utc=utc_open_close(EVIDENCE)[1])
    assert b.coverage_summary.get("unknown:sealed_session_changed") == 1
    assert old.isoformat() not in [h["session_date"] for h in b.history_sessions]
    restored = retention.cmd_restore(db, arch)
    assert len(restored["restored_sessions"]) == 2 * (25 - KEEP)
    after = baselines(db, monkeypatch, "0")
    assert after[3:] == before[3:]                                             # flow untouched by the late print
    assert after[:3] != before[:3]                                             # dark pool now includes it, from raw


def test_restore_puts_back_the_exact_rows(db, tmp_path, monkeypatch):
    conn = sqlite3.connect(db)
    snap = conn.execute("SELECT id, source_id, ticker, event_time, retrieved_at, raw_json FROM darkpool_observations "
                        "ORDER BY id").fetchall()
    arch = tmp_path / "arch"
    retention.cmd_archive(db, KEEP, arch)
    monkeypatch.setenv("TIP_RETENTION_ENABLED", "1")
    retention.cmd_prune(db, KEEP, arch, confirmed=True)
    retention.cmd_restore(db, arch)
    assert conn.execute("SELECT id, source_id, ticker, event_time, retrieved_at, raw_json FROM darkpool_observations "
                        "ORDER BY id").fetchall() == snap
    assert conn.execute("SELECT COUNT(*) FROM session_value_curves WHERE sealed = 1").fetchone()[0] == 0
    manifest = [json.loads(l) for l in (arch / "manifest.jsonl").read_text().splitlines()]
    assert all(len(m["sha256"]) == 64 for m in manifest)
