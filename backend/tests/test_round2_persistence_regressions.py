"""Round-2 persistence review (P2-1..P2-11), converted from the reviewer's reproductions into regression
tests asserting the corrected behaviour.

Independent adversarial persistence audit (audit2-persist) of 339c84f.

Every test asserts the CORRECT behaviour: a failing test is a confirmed defect.
Old-code trees (147e315 = production schema 9, 345decb = previous RC schema 10) are
extracted with `git archive` into <repo>/.oldcode/<commit>/backend (untracked).
No network, no Docker, temp directories only.
"""
from __future__ import annotations

import errno
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
# earlier releases' app code is extracted OUTSIDE the repository (never shipped, never in the tree)
OLD = Path(os.environ.get("TIP_OLDCODE_DIR") or (Path(__import__("tempfile").gettempdir()) / "tip-oldcode"))


def old_backend(commit: str) -> Path:
    d = OLD / commit / "backend"
    if not (d / "app").exists():
        (OLD / commit).mkdir(parents=True, exist_ok=True)
        arch = subprocess.run(["git", "-C", str(REPO), "archive", commit, "backend/app"], capture_output=True)
        if arch.returncode != 0:
            pytest.skip(f"git history for {commit} not available in this checkout")
        subprocess.run(["tar", "-x", "-C", str(OLD / commit)], input=arch.stdout, check=True)
    return d


def run_py(backend_dir: Path, code: str, timeout=120):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST") and k != "UW_API_KEY"}
    env["PYTHONPATH"] = str(backend_dir)
    env["TIP_MODE"] = "demo"
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(backend_dir), env=env,
                          capture_output=True, text=True, timeout=timeout)


def make_db(commit: str, path: Path, extra: str = ""):
    """A real DB created by the given release's own stores (plus optional seeding code)."""
    r = run_py(old_backend(commit), f"""
        from app.storage import QualifyingDaysStore
        from app.ingestion import IngestionCatalog
        from app.core.scheduler_store import SchedulerStore
        db = {str(path)!r}
        s = QualifyingDaysStore(db); c = IngestionCatalog(db); SchedulerStore(db)
        s.record_scan("AAA", True, "NEW", scan_date="2026-10-14")
        c.upsert_session_coverage("darkpool", "AAA", "2026-10-13", "complete",
                                  "2026-10-13T13:30:00+00:00", "2026-10-13T20:00:00+00:00", 3, 10)
        {extra}
    """)
    assert r.returncode == 0, r.stderr[-3000:]


def versions(path):
    c = sqlite3.connect(path)
    try:
        return [v for (v,) in c.execute("SELECT version FROM schema_version ORDER BY version")]
    finally:
        c.close()


def columns(path, table="session_coverage"):
    c = sqlite3.connect(path)
    try:
        return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    finally:
        c.close()


# ---------------------------------------------------------------- 1. crash / kill during migration 11

def test_kill9_inside_migration11_leaves_db_at_schema10_and_restart_completes(tmp_path):
    db = tmp_path / "tip_hackathon.db"
    make_db("345decb", db)
    assert versions(db) == list(range(1, 11))
    r = run_py(BACKEND, f"""
        import os
        from app import migrations
        orig = migrations._statements
        def killer(sql):
            for i, s in enumerate(orig(sql)):
                if "certified_at_utc" in s:      # third ALTER of migration 11 executes, then the process dies
                    yield s
                    os._exit(9)
                yield s
        migrations._statements = killer
        from app.storage import QualifyingDaysStore
        QualifyingDaysStore({str(db)!r})
    """)
    assert r.returncode == 9, (r.returncode, r.stderr[-2000:])
    assert versions(db) == list(range(1, 11))
    assert "proof_started_utc" not in columns(db)               # hot journal rolled the ALTERs back
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    r = run_py(BACKEND, f"""
        from app.storage import QualifyingDaysStore
        QualifyingDaysStore({str(db)!r})
    """)
    assert r.returncode == 0, r.stderr[-2000:]
    assert versions(db) == list(range(1, 12))
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM session_coverage").fetchone()[0] == 1


def test_concurrent_processes_apply_migrations_exactly_once(tmp_path):
    db = tmp_path / "tip_hackathon.db"
    make_db("147e315", db)
    code = f"""
        from app.storage import QualifyingDaysStore
        from app.ingestion import IngestionCatalog
        QualifyingDaysStore({str(db)!r}); IngestionCatalog({str(db)!r})
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env["PYTHONPATH"] = str(BACKEND)
    procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], cwd=str(BACKEND), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-800:] for o in outs]
    c = sqlite3.connect(db)
    assert [r for r in c.execute("SELECT version, COUNT(*) FROM schema_version GROUP BY version HAVING COUNT(*) > 1")] == []
    assert versions(db) == list(range(1, 12))
    assert columns(db).count("certified_by") == 1


def test_kill_between_backup_and_rename_leaves_no_orphan_after_next_successful_start(tmp_path):
    """A kill after the backup copy and before its rename leaves <db>.pre-migration-*.bak.partial. The next
    start migrates fine, but the orphan (a complete SQLite file) is never cleaned up -- and any SQLite file in
    the volume that is not the primary/Demo/.bak makes r3recover's semantic check BLOCK."""
    db = tmp_path / "tip_hackathon.db"
    make_db("345decb", db)
    r = run_py(BACKEND, f"""
        import os
        from app import migrations
        migrations.os.replace = lambda a, b: os._exit(9)
        from app.storage import QualifyingDaysStore
        QualifyingDaysStore({str(db)!r})
    """)
    assert r.returncode == 9
    assert list(tmp_path.glob("*.partial")), "precondition: the kill left a partial backup"
    assert versions(db) == list(range(1, 11))
    r = run_py(BACKEND, f"from app.storage import QualifyingDaysStore; QualifyingDaysStore({str(db)!r})")
    assert r.returncode == 0, r.stderr[-1500:]
    assert versions(db) == list(range(1, 12))
    sys.path.insert(0, str(REPO / "tools" / "recovery"))
    import r3probe
    import r3recover
    m = r3probe.build_manifest(str(tmp_path))
    stray = [k for k in m["sqlite"] if k != r3recover.PRIMARY_DB and not r3recover.BAK_RE.match(k)]
    assert stray == [], f"orphan SQLite files left in the data directory: {stray}"


def test_low_disk_refuses_backup_and_leaves_db_untouched(tmp_path, monkeypatch):
    from app import migrations
    db = tmp_path / "tip_hackathon.db"
    make_db("345decb", db)
    before = db.read_bytes()

    class DU:
        free = 1024
    monkeypatch.setattr(migrations.shutil, "disk_usage", lambda p: DU)
    from app.storage import QualifyingDaysStore
    with pytest.raises(RuntimeError, match="insufficient free space"):
        QualifyingDaysStore(str(db))
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("*.bak*"))


# ---------------------------------------------------------------- 2. busy timeout (locked DB)

def _hold_write_lock(db, seconds, started):
    c = sqlite3.connect(db, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")
    started.set()
    time.sleep(seconds)
    c.execute("COMMIT")
    c.close()


def test_lifecycle_write_waits_out_a_7s_write_lock_like_the_catalog_does(tmp_path):
    """The commit claims a 30 s busy timeout, but only IngestionCatalog got it: QualifyingDaysStore (lifecycle
    apply, BEGIN IMMEDIATE) and SchedulerStore still connect with sqlite3's 5 s default."""
    from app.storage import QualifyingDaysStore
    db = str(tmp_path / "tip_hackathon.db")
    store = QualifyingDaysStore(db)
    started = threading.Event()
    t = threading.Thread(target=_hold_write_lock, args=(db, 7, started))
    t.start()
    started.wait(5)
    try:
        store.record_scan("AAA", True, "NEW", scan_date="2026-10-14")
    finally:
        t.join()
    assert store.current_state("AAA") == "NEW"


def test_scheduler_store_uses_the_same_busy_timeout_as_the_catalog(tmp_path):
    from app.core.scheduler_store import SchedulerStore
    db = str(tmp_path / "tip_hackathon.db")
    s = SchedulerStore(db)
    with s._conn() as conn:
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert bt >= 30000, f"scheduler store busy timeout {bt} ms"


# ---------------------------------------------------------------- 3. settings quarantine

def _seed_settings(tmp_path):
    from app.settings_store import SettingsStore
    p = tmp_path / "settings.json"
    s = SettingsStore(p)
    s.set_uw_key("test-key-0000-AAAA")
    s.record_verified_uw_key("test-key-0000-AAAA")
    s.set_mode("live")
    return s, p


def _transient_open(monkeypatch, n_failures, err=errno.EMFILE):
    from app import settings_store
    real_open = open
    state = {"left": n_failures}

    def flaky(path, *a, **k):
        if state["left"] > 0 and str(path).endswith("settings.json"):
            state["left"] -= 1
            raise OSError(err, os.strerror(err))
        return real_open(path, *a, **k)
    monkeypatch.setattr(settings_store, "open", flaky, raising=False)


def test_transient_read_error_does_not_move_credentials_aside(tmp_path, monkeypatch):
    s, p = _seed_settings(tmp_path)
    _transient_open(monkeypatch, 1)                    # e.g. EMFILE under fd pressure, EACCES, EIO
    s.get_all()                                        # one failed read
    s.record_scan(3, mode="live")                      # the very next scan persists its timestamp
    monkeypatch.undo()
    data = s.get_all()
    assert data["uw_api_key"] == "test-key-0000-AAAA", (
        f"credentials gone from the active settings file; aside copies: {[q.name for q in tmp_path.iterdir()]}")
    assert data["mode"] == "live"


def test_two_quarantines_in_one_second_never_overwrite_the_first_copy(tmp_path, monkeypatch):
    from app import settings_store
    s, p = _seed_settings(tmp_path)

    class FrozenDT(settings_store.datetime):
        @classmethod
        def now(cls, tz=None):
            return settings_store.datetime(2026, 10, 21, 12, 0, 0, tzinfo=tz)
    monkeypatch.setattr(settings_store, "datetime", FrozenDT)
    _transient_open(monkeypatch, 1)
    s.get_all()                                       # quarantine #1: the file holding the key
    s.record_scan(1, mode="live")                     # a fresh defaults file is written
    _transient_open(monkeypatch, 1)
    s.get_all()                                       # quarantine #2 in the same second
    found = any("test-key-0000-AAAA" in q.read_text(errors="ignore") for q in tmp_path.iterdir() if q.is_file())
    assert found, f"the only copy of the saved credentials was overwritten: {[q.name for q in tmp_path.iterdir()]}"


def test_recovery_notice_survives_a_restart(tmp_path):
    from app.settings_store import SettingsStore
    p = tmp_path / "settings.json"
    p.write_text("{not json")
    s = SettingsStore(p)
    s.get_all()
    assert s.recovery_notice
    s.record_scan(1, mode="demo")
    s2 = SettingsStore(p)                             # container restart
    s2.get_all()
    assert s2.recovery_notice, "after a restart the UI no longer says the credentials were moved aside"


# ---------------------------------------------------------------- 4. rollback: old code on a schema-11 DB

SEED_R3 = """
    from app.ingestion import IngestionCatalog
    c = IngestionCatalog(db)
    # certified, awaiting-settlement and suspected-gap rows written by the candidate
    c.upsert_session_coverage("darkpool", "AAA", "2026-10-15", "complete", "2026-10-15T13:30:00+00:00",
        "2026-10-15T20:00:00+00:00", 4, 50, provenance={"certified_by": "settled-v1",
        "certified_at_utc": "2026-10-16T01:00:00+00:00", "proof_started_utc": "2026-10-15T23:30:00+00:00"})
    c.upsert_session_coverage("darkpool", "AAA", "2026-10-16", "partial", "2026-10-16T13:30:00+00:00",
        "2026-10-16T20:00:00+00:00", 4, 50, "awaiting_settlement",
        provenance={"proof_started_utc": "2026-10-16T19:00:00+00:00"})
    c.upsert_session_coverage("darkpool", "AAA", "2026-10-17", "partial", "2026-10-17T13:30:00+00:00",
        "2026-10-17T20:00:00+00:00", 4, 50, "suspected_provider_gap",
        provenance={"proof_started_utc": "2026-10-17T23:30:00+00:00", "gap_flag": "suspected"})
    t = {"executed_at": "2026-10-15T15:00:00Z", "price": "10", "size": 100, "premium": "1000",
         "market_center": "L", "tracking_id": 1, "canceled": False}
    c.catalog_darkpool_observations("AAA", [t])
    c.catalog_darkpool_observations("AAA", [dict(t, canceled=True)])
"""


@pytest.mark.parametrize("commit", ["345decb", "147e315"])
def test_previous_code_starts_and_reads_a_schema11_db(tmp_path, commit):
    db = tmp_path / "tip_hackathon.db"
    make_db("147e315", db)
    r = run_py(BACKEND, f"db = {str(db)!r}\n" + textwrap.dedent(SEED_R3))
    assert r.returncode == 0, r.stderr[-2000:]
    assert versions(db) == list(range(1, 12))
    r = run_py(old_backend(commit), f"""
        import json
        from app.storage import QualifyingDaysStore
        from app.ingestion import IngestionCatalog
        from app.core.scheduler_store import SchedulerStore
        db = {str(db)!r}
        s = QualifyingDaysStore(db); c = IngestionCatalog(db); SchedulerStore(db)
        s.record_scan("AAA", True, "BUILDING", scan_date="2026-10-15")
        print(json.dumps([x["status"] for x in c.list_session_coverage("darkpool", "AAA")]))
    """)
    assert r.returncode == 0, r.stderr[-2000:]
    assert versions(db) == list(range(1, 12))          # old runner neither re-applies nor drops anything
    assert sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_old_code_writes_never_become_certified_after_roll_forward(tmp_path):
    """345decb run on the schema-11 DB promotes the candidate's withheld rows (awaiting_settlement,
    suspected_provider_gap) to 'complete' without a request (documented reason code-only rollback is
    forbidden). Rolling forward again must treat every such row as legacy/uncertified."""
    db = tmp_path / "tip_hackathon.db"
    make_db("147e315", db)
    assert run_py(BACKEND, f"db = {str(db)!r}\n" + textwrap.dedent(SEED_R3)).returncode == 0
    r = run_py(old_backend("345decb"), f"""
        import json
        from datetime import date, datetime, timezone
        from app.ingestion import IngestionCatalog
        from app import session_collection as sc
        c = IngestionCatalog({str(db)!r})
        out = []
        for d in (date(2026, 10, 16), date(2026, 10, 17)):
            u = sc.update_session_coverage(None, c, "darkpool", "AAA", d,
                                           now_utc=datetime(2026, 10, 20, 12, tzinfo=timezone.utc), max_pages=3)
            out.append((u.coverage or {{}}).get("status"))
        print(json.dumps(out))
    """)
    assert r.returncode == 0, r.stderr[-2000:]
    promoted = json.loads(r.stdout.strip().splitlines()[-1])
    from app.ingestion import IngestionCatalog
    from app.core import coverage_policy as policy
    c = IngestionCatalog(str(db))
    rows = {x["session_date"]: x for x in c.list_session_coverage("darkpool", "AAA")}
    assert policy.is_certified(rows["2026-10-15"])
    for d in ("2026-10-16", "2026-10-17"):
        assert not policy.is_certified(rows[d]), (d, promoted, rows[d])


def test_pre_migration_backup_of_a_schema9_db_is_restorable_by_147e315(tmp_path):
    db = tmp_path / "tip_hackathon.db"
    make_db("147e315", db)
    pre = sqlite3.connect(db)
    counts = {t: pre.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("session_coverage", "lifecycle_episodes", "lifecycle_scans", "scheduler_state")}
    pre.close()
    r = run_py(BACKEND, f"""
        from app.storage import QualifyingDaysStore
        from app.ingestion import IngestionCatalog
        QualifyingDaysStore({str(db)!r}); IngestionCatalog({str(db)!r}); QualifyingDaysStore({str(db)!r})
    """)
    assert r.returncode == 0, r.stderr[-2000:]
    baks = sorted(tmp_path.glob("tip_hackathon.db.pre-migration-*.bak"))
    assert len(baks) == 1, [b.name for b in baks]           # one backup per upgrade, none on restart
    assert versions(baks[0]) == list(range(1, 10))
    restored = tmp_path / "restored" / "tip_hackathon.db"
    restored.parent.mkdir()
    restored.write_bytes(baks[0].read_bytes())
    r = run_py(old_backend("147e315"), f"""
        from app.storage import QualifyingDaysStore
        from app.ingestion import IngestionCatalog
        QualifyingDaysStore({str(restored)!r}); IngestionCatalog({str(restored)!r})
    """)
    assert r.returncode == 0, r.stderr[-2000:]
    c = sqlite3.connect(restored)
    assert {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts} == counts
    assert versions(restored) == list(range(1, 10))


def test_canceled_marker_row_is_not_served_as_an_extra_print(tmp_path):
    """C-F3 stores '<source_id>#canceled' as a second darkpool_observations row. _iter_window handles it,
    but get_cached_darkpool_observations (cache-only ticker view / chart markers) returns it as one more
    print next to the original -- and every pre-R3 reader (old code after rollback) does the same."""
    from app.ingestion import IngestionCatalog
    c = IngestionCatalog(str(tmp_path / "tip_hackathon.db"))
    t = {"executed_at": "2026-10-15T15:00:00Z", "price": "10", "size": 100, "premium": "1000",
         "market_center": "L", "tracking_id": 1, "canceled": False}
    c.catalog_darkpool_observations("AAA", [t])
    c.catalog_darkpool_observations("AAA", [dict(t, canceled=True)])
    served = c.get_cached_darkpool_observations("AAA")
    live = [x for x in served if not x.get("canceled")]
    assert len(served) <= 1 or not live, f"a canceled print is served {len(served)}x ({len(live)} as live)"


# ---------------------------------------------------------------- 5. diagnostics ring buffer

def test_diagnostics_ring_buffer_keeps_exactly_the_newest_rows(tmp_path, monkeypatch):
    from app.ingestion import IngestionCatalog
    c = IngestionCatalog(str(tmp_path / "tip_hackathon.db"))
    monkeypatch.setattr(IngestionCatalog, "DIAGNOSTICS_MAX_ROWS", 5)
    for i in range(12):
        assert c.record_collection_diagnostic({"dataset": "darkpool", "ticker": "AAA", "session_date": "2026-10-15",
                                               "reason": f"r{i}", "detail": {}})
    with c._conn() as conn:
        rows = [r[0] for r in conn.execute("SELECT reason FROM collection_diagnostics ORDER BY id")]
        other = conn.execute("SELECT COUNT(*) FROM session_coverage").fetchone()[0]
    assert rows == [f"r{i}" for i in range(7, 12)]
    assert other == 0


# ---------------------------------------------------------------- 6. retention tool on schema 11

def _retention():
    sys.path.insert(0, str(REPO / "tools"))
    import retention
    return retention


def test_retention_never_prunes_uncertified_legacy_sessions(tmp_path):
    """Since migration 11 a legacy 'complete' row is uncertified and will be RE-WALKED by the collector; the
    retention tool (unchanged since f34da74) still selects every status='complete' row as a prune candidate."""
    from app.ingestion import IngestionCatalog
    db = str(tmp_path / "tip_hackathon.db")
    c = IngestionCatalog(db)
    import datetime as dt
    d = dt.date(2026, 1, 2)
    n = 0
    while n < 35:
        if d.weekday() < 5:
            c.upsert_session_coverage("darkpool", "AAA", d.isoformat(), "complete", f"{d}T14:30:00+00:00",
                                      f"{d}T21:00:00+00:00", 1, 1)          # legacy: certified_by NULL
            n += 1
        d += dt.timedelta(days=1)
    rt = _retention()
    with sqlite3.connect(db) as conn:
        cands = rt._candidates(conn, 30)
    assert cands == [], f"{len(cands)} uncertified legacy sessions are prune candidates"


def test_retention_restore_reports_rows_actually_restored(tmp_path):
    """restore re-inserts with INSERT OR IGNORE and reports len(archive) as restored -- even when nothing
    was inserted (e.g. the session was re-walked after pruning, so the source_ids already exist under new ids)."""
    from app.ingestion import IngestionCatalog
    db = str(tmp_path / "tip_hackathon.db")
    IngestionCatalog(db)
    rt = _retention()
    arch = tmp_path / "arch"
    rows = [(101, "dp:x1", "AAA", "2026-03-02T15:00:00+00:00", "2026-03-02T23:00:00+00:00", "{}"),
            (102, "dp:x2", "AAA", "2026-03-02T15:01:00+00:00", "2026-03-02T23:00:00+00:00", "{}")]
    f = arch / "darkpool" / "AAA" / "2026-03-02.jsonl.gz"
    sha = rt._write_archive(f, rows)
    (arch / "manifest.jsonl").write_text(json.dumps({"dataset": "darkpool", "ticker": "AAA",
                                                     "session_date": "2026-03-02", "rows": 2, "sha256": sha,
                                                     "file": "darkpool/AAA/2026-03-02.jsonl.gz"}) + "\n")
    with sqlite3.connect(db) as conn:                        # the re-walk stored the same prints under new ids
        conn.executemany("INSERT INTO darkpool_observations (id, source_id, ticker, event_time, retrieved_at, raw_json)"
                         " VALUES (?, ?, ?, ?, ?, ?)", [(900 + i, r[1], *r[2:]) for i, r in enumerate(rows)])
    out = rt.cmd_restore(db, arch)
    with sqlite3.connect(db) as conn:
        present = {r[0] for r in conn.execute("SELECT id FROM darkpool_observations")}
    restored_ids = present & {101, 102}
    assert out["restored_sessions"][0]["rows"] == len(restored_ids), (out, sorted(present))


# ---------------------------------------------------------------- 7. legacy rows the candidate misreads

class _StubClient:
    def __init__(self):
        self.calls = []

    def darkpool_ticker(self, ticker, date, limit, newer_than, older_than):
        self.calls.append((date, newer_than, older_than))
        return {"data": []}


def test_legacy_not_entitled_rows_do_not_stop_backfill_forever(tmp_path):
    """R3 makes 401/403 transient ('never a property of the session'), but rows 147e315/345decb already wrote
    as unavailable/not_entitled still read as a history cutoff: backfill_baseline breaks at them for ever."""
    from datetime import date, datetime, timezone
    from app import session_collection as sc
    from app.ingestion import IngestionCatalog
    c = IngestionCatalog(str(tmp_path / "tip_hackathon.db"))
    c.upsert_session_coverage("darkpool", "AAA", "2026-10-15", "unavailable", None, None, 1, 0, "not_entitled")
    client = _StubClient()
    sc.backfill_baseline(client, c, "darkpool", "AAA", date(2026, 10, 16), max_pages=20,
                         now_utc=datetime(2026, 10, 16, 23, tzinfo=timezone.utc))
    touched = {d for d, _a, _b in client.calls}
    assert touched - {"2026-10-16"}, f"no session before the legacy not_entitled row was ever requested: {sorted(touched)}"


def test_stale_island_left_by_old_code_never_produces_an_inverted_window(tmp_path):
    """Old code upserts only its own columns, so after a code-only rollback a row can carry island columns
    BELOW its covered_through. The R3 collector then requests (covered_through, island_from) with lo > hi."""
    from datetime import date, datetime, timezone
    from app import session_collection as sc
    from app.ingestion import IngestionCatalog
    c = IngestionCatalog(str(tmp_path / "tip_hackathon.db"))
    c.upsert_session_coverage("darkpool", "AAA", "2026-10-16", "partial", "2026-10-16T13:30:00+00:00",
                              "2026-10-16T19:00:00+00:00", 3, 10, "page_budget_exhausted",
                              provenance={"proof_started_utc": "2026-10-17T01:00:00+00:00",
                                          "island_from_utc": "2026-10-16T17:00:00+00:00",
                                          "island_through_utc": "2026-10-16T18:00:00+00:00"})
    client = _StubClient()
    sc.update_session_coverage(client, c, "darkpool", "AAA", date(2026, 10, 16),
                               now_utc=datetime(2026, 10, 20, 12, tzinfo=timezone.utc), max_pages=5)
    bad = [(a, b) for _d, a, b in client.calls if a and b and a >= b]
    assert not bad, f"inverted request windows sent to the provider: {bad}"
