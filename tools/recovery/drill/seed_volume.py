"""Seed a DISPOSABLE drill volume (mounted at /srv/data) with a representative,
SYNTHETIC-ONLY Release 2 (schema 9) deployment state. Runs inside the 147e315 image:
    python /r/seed_volume.py /srv/data [--scheduler enabled] [--active-job] [--corrupt-demo]
        [--fifo] [--hardlink] [--bad-demo] [--unexpected-db]
Creates: primary DB (schema 1-9, synthetic SPY-free rows, scheduler row), Demo DB with a
live -wal/-shm pair, a zero-length primary -journal, settings.json / .auth_token /
.pairing_code (placeholders, never real credentials), a migration .bak, a hidden file, an
empty directory, a nested 0700 directory, a symlink, and files owned by uid/gid 1000."""
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, "/srv/backend")
from app.ingestion import IngestionCatalog  # noqa: E402
from app.storage import QualifyingDaysStore  # noqa: E402

root = sys.argv[1]
os.makedirs(root, exist_ok=True)
db = os.path.join(root, "tip_hackathon.db")
QualifyingDaysStore(db)                                      # as the app does: storage schema + migrations 1-9
cat = IngestionCatalog(db)
cat.catalog_darkpool_observations("SYNT", [{"ticker": "SYNT", "executed_at": "2026-10-15T14:00:00Z", "price": "1",
                                            "size": 1, "premium": "100", "tracking_id": "seed-1", "volume": 1}])
cat.catalog_options_flow_observations("SYNT", [{"id": "seed-a1", "ticker": "SYNT", "created_at": "2026-10-15T14:00:00Z",
                                                "type": "call", "total_premium": 1}])
cat.upsert_session_coverage("darkpool", "SYNT", "2026-10-15", "complete", "2026-10-15T13:30:00+00:00",
                            "2026-10-15T20:00:00+00:00", 1, 1, None)
c = sqlite3.connect(db)
mode = "enabled" if "--scheduler" in sys.argv and sys.argv[sys.argv.index("--scheduler") + 1] == "enabled" else "disabled"
c.execute("INSERT OR REPLACE INTO scheduler_state (id, mode, updated_at, updated_by) VALUES (1, ?, '2026-10-16T00:00:00Z', 'seed')",
          (mode,))
if "--active-job" in sys.argv:
    c.execute("INSERT INTO baseline_jobs (created_at, updated_at, status, tickers_json, max_pages_per_ticker, step_pages, "
              "target_sessions, pages_used, progress_json, requested_by) VALUES ('2026-10-16T00:00:00Z', "
              "'2026-10-16T00:00:00Z', 'running', '[\"SYNT\"]', 10, 10, 20, 0, '{}', 'seed')")
c.commit()
c.close()
shutil.copy2(db, os.path.join(root, "tip_hackathon.db.pre-migration-20261016T000000Z.bak"))
open(db + "-journal", "wb").close()                          # zero-length (not hot) journal sidecar

demo = os.path.join(root, "tip_hackathon.demo.db")
if "--bad-demo" in sys.argv:                                 # negative fixture: a valid SQLite file that is not
    sqlite3.connect(demo).executescript("CREATE TABLE notes (k TEXT);")      # a TIP Demo database
else:
    QualifyingDaysStore(demo)
d = sqlite3.connect(demo)
d.execute("PRAGMA journal_mode=WAL")
d.execute("CREATE TABLE IF NOT EXISTS drill_demo (k TEXT)")
d.execute("INSERT INTO drill_demo VALUES ('demo-row')")
d.commit()
if "--corrupt-demo" in sys.argv:                             # negative fixture: checkpoint, then damage page 2
    d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    d.close()
    with open(demo, "r+b") as f:
        f.seek(4096)
        f.write(bytes([0xFF]) * 64)

json.dump({"mode": "live", "uw_api_key": "drill-placeholder-not-a-key", "watchlist": ["SYNT"]},
          open(os.path.join(root, "settings.json"), "w"))
for name, val in ((".auth_token", "drill-token-placeholder"), (".pairing_code", "000000")):
    with open(os.path.join(root, name), "w") as f:
        f.write(val)
    os.chmod(os.path.join(root, name), 0o600)
with open(os.path.join(root, ".r3-hidden-marker"), "w") as f:
    f.write("hidden")
os.makedirs(os.path.join(root, "empty.d"), exist_ok=True)
os.makedirs(os.path.join(root, "keep", "sub"), exist_ok=True)
os.chmod(os.path.join(root, "keep"), 0o700)
with open(os.path.join(root, "keep", "sub", "owned.txt"), "w") as f:
    f.write("owned by 1000")
os.chmod(os.path.join(root, "keep", "sub", "owned.txt"), 0o640)
os.chown(os.path.join(root, "keep", "sub", "owned.txt"), 1000, 1000)
os.chown(os.path.join(root, "keep", "sub"), 1000, 1000)
if not os.path.lexists(os.path.join(root, "settings.link")):
    os.symlink("settings.json", os.path.join(root, "settings.link"))
if "--unexpected-db" in sys.argv:                            # negative fixture: an unknown SQLite database
    sqlite3.connect(os.path.join(root, "extra.sqlite")).executescript("CREATE TABLE t (x);")
if "--fifo" in sys.argv:                                     # negative fixtures: unsupported entries
    os.mkfifo(os.path.join(root, "drill.fifo"))
if "--hardlink" in sys.argv:
    os.link(os.path.join(root, "settings.json"), os.path.join(root, "settings.hardlink"))
os._exit(0)                                                  # leave the Demo DB's -wal/-shm in place
