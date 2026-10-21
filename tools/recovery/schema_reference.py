#!/usr/bin/env python3
"""Generate the Release 2 (147e315) SQLite application-schema reference used by r3recover.py.

    python tools/recovery/schema_reference.py <backend_dir>  > tools/recovery/schema9_reference.json

For every schema state k = 1..9 it builds a fresh database exactly the way the application does
(app.storage.SCHEMA, the schema_version table, then migrations 1..k from app.migrations) and records
every schema object (type, name, table) and every table's column names, with the same queries as
r3probe.py. The checked-in JSON was generated inside the 147e315 image; backend/tests/
test_recovery_tool.py regenerates it from the repository's migrations 1-9 and requires an exact match.
"""
import json
import os
import sqlite3
import sys
import tempfile


def describe(conn):
    objects = sorted([r[0], r[1], r[2]] for r in conn.execute(
        "select type, name, tbl_name from sqlite_master where name not like 'sqlite_%'"))
    columns = {o[1]: [r[1] for r in conn.execute(f"pragma table_info([{o[1]}])")] for o in objects if o[0] == "table"}
    return {"objects": objects, "columns": columns}


def build(backend_dir, max_version=9):
    sys.path.insert(0, backend_dir)
    from app import migrations, storage  # noqa: E402 -- imported from the given backend

    out = {"migrations": [[m.version, m.name] for m in sorted(migrations.MIGRATIONS, key=lambda m: m.version)
                          if m.version <= max_version], "states": {}}
    for k in range(1, max_version + 1):
        d = tempfile.mkdtemp(prefix="r3ref-")
        conn = sqlite3.connect(os.path.join(d, "ref.db"))
        conn.executescript(storage.SCHEMA)
        migrations._ensure_schema_version_table(conn)
        for m in sorted(migrations.MIGRATIONS, key=lambda m: m.version):
            if m.version <= k:
                conn.executescript(m.sql)
        out["states"][str(k)] = describe(conn)
        conn.close()
    return out


if __name__ == "__main__":
    print(json.dumps(build(sys.argv[1]), indent=1, sort_keys=True))
