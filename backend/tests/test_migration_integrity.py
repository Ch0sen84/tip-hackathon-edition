"""Migration runner integrity (persistence audit PE-P1, PE-P2, PE-P6, PE-P7).

* each migration and its schema_version row are applied in ONE transaction: a failure part-way leaves
  the schema exactly as before and the next start applies it cleanly (migration 11's ALTERs would
  otherwise leave duplicate columns that block every later start);
* concurrent first runs on separate connections apply each migration once;
* pre-migration backups: no duplicate backup of an unchanged database (restart loop), refusal on low
  disk, and no partial backup file left behind on failure;
* shipped migrations are FROZEN: editing one (instead of adding a new one) fails this test."""
import hashlib
import sqlite3
import threading

import pytest

from app import migrations
from app.migrations import MIGRATIONS, run_migrations



def digest(m):
    return hashlib.sha256(f"{m.version}|{m.name}|{m.sql}".encode()).hexdigest()


def db_at(tmp_path, upto, name="m.db"):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE user_rows (x)")
    conn.execute("INSERT INTO user_rows VALUES (1)")              # a real DB: backups are taken
    conn.commit()
    saved = list(MIGRATIONS)
    MIGRATIONS[:] = [m for m in saved if m.version <= upto]
    try:
        run_migrations(conn, str(path))
    finally:
        MIGRATIONS[:] = saved
    return path, conn


def cols(conn, table="session_coverage"):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def versions(conn):
    return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]


def test_a_failure_part_way_through_a_migration_changes_nothing_and_a_retry_succeeds(tmp_path, monkeypatch):
    path, conn = db_at(tmp_path, 10)
    before = cols(conn)
    m11 = next(m for m in MIGRATIONS if m.version == 11)
    lines = m11.sql.split("\n")
    third_alter = [i for i, ln in enumerate(lines) if ln.startswith("ALTER TABLE")][2]
    broken = m11._replace(sql="\n".join(lines[:third_alter + 1] + ["INSERT INTO no_such_table VALUES (1);"]
                                        + lines[third_alter + 1:]))
    monkeypatch.setattr(migrations, "MIGRATIONS", [m if m.version != 11 else broken for m in MIGRATIONS])
    with pytest.raises(sqlite3.OperationalError):
        run_migrations(conn, str(path))
    assert cols(conn) == before and versions(conn) == list(range(1, 11))
    monkeypatch.undo()
    run_migrations(conn, str(path))
    assert versions(conn) == list(range(1, 12)) and "certified_by" in cols(conn)


def test_concurrent_first_runs_apply_each_migration_once(tmp_path):
    path = tmp_path / "c.db"
    errors, conns = [], [sqlite3.connect(path, check_same_thread=False, timeout=30) for _ in range(4)]
    barrier = threading.Barrier(4)

    def go(c):
        try:
            barrier.wait()
            run_migrations(c, str(path))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(c,)) for c in conns]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == [] and versions(conns[0]) == [m.version for m in MIGRATIONS]


def test_a_failed_upgrade_retried_takes_no_further_backup(tmp_path, monkeypatch):
    path, conn = db_at(tmp_path, 10)
    for b in tmp_path.glob("*.bak"):
        b.unlink()
    m11 = next(m for m in MIGRATIONS if m.version == 11)
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        [m if m.version != 11 else m11._replace(sql="INSERT INTO no_such_table VALUES (1);")
                         for m in MIGRATIONS])
    for _ in range(3):
        with pytest.raises(sqlite3.OperationalError):
            run_migrations(conn, str(path))
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_low_disk_refuses_to_migrate_and_leaves_no_partial_backup(tmp_path, monkeypatch):
    path, conn = db_at(tmp_path, 10)
    for b in tmp_path.glob("*.bak"):
        b.unlink()

    class DU:
        free = 1
    monkeypatch.setattr(migrations.shutil, "disk_usage", lambda p: DU)
    with pytest.raises(RuntimeError, match="insufficient free space"):
        run_migrations(conn, str(path))
    assert versions(conn) == list(range(1, 11))
    assert not list(tmp_path.glob("*.bak*"))


def test_a_backup_that_fails_part_way_leaves_no_file(tmp_path, monkeypatch):
    path, conn = db_at(tmp_path, 10)
    for b in tmp_path.glob("*.bak"):
        b.unlink()
    real_connect = sqlite3.connect

    class Boom:
        def __init__(self, inner):
            self.inner = inner

        def backup(self, dst):
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            self.inner.close()

    calls = {"n": 0}

    def fake_connect(p, *a, **k):
        calls["n"] += 1
        c = real_connect(p, *a, **k)
        return Boom(c) if calls["n"] == 1 else c
    monkeypatch.setattr(migrations.sqlite3, "connect", fake_connect)
    with pytest.raises(sqlite3.OperationalError):
        migrations.backup_db_file(str(path))
    assert not list(tmp_path.glob("*.bak*"))


def test_shipped_migrations_are_frozen():
    current = {m.version: digest(m) for m in MIGRATIONS}
    pinned = {v: h for v, h in FROZEN_DIGESTS.items()}
    assert sorted(current) == sorted(pinned), "a migration was added or removed: pin its digest here"
    changed = [v for v in pinned if current[v] != pinned[v]]
    assert not changed, f"shipped migration(s) {changed} were edited -- add a NEW migration instead"


# sha256 over version|name|sql of every shipped migration (1-9 = 147e315, 10 = d520e83, verified
# identical). Never edit a shipped migration: add a new one.
FROZEN_DIGESTS = {
    1: "26d18497bc9b0a98df88c453451312340a38b782a31824d0cfa275c1229c7939",
    2: "14df27dea2150cec8b54f22a2a4469fb24c5d290676c36970edfc1338af5a8ae",
    3: "1306a2d65fa8d5532eb287e9265e21e30f712598cd813732c915f8641bae7c9a",
    4: "7ece3891aefa34d1716a5b5b2548a7273e5bfc151927959576495295c22ac013",
    5: "b99114148c9bc2d2486830f3db0f6906e33235d0f0478dc369eafcabc21ec768",
    6: "6935b6978dd570f15f5dfb072d164312bc3f8c314dd8a73016c737a9d5252299",
    7: "8c31ae235b2b6c975998beeaebdb99f08b179ccfbb2760073caf19a825a25b48",
    8: "c019ff7f36cc495838a6957c538a302c8c891bf24d321eccbf62ca3d056127c8",
    9: "bc544fc9ea9ea841a15bc6d845a23a9878933d3f794fb24b177faa99e5955823",
    10: "07a1b657d61023fbae962c1ea935b5bfe2c042e05a19a525a27185ff75c4b9e6",
    11: "25f803fc900db67cab7c94a752b81a7972771886fdc587bafa9bf08070d5fe5c",
}
