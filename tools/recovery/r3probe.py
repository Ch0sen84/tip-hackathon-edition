"""Release 3 recovery probe -- runs INSIDE the pinned helper container.

Usage (always mount the target read-only at /target):
    python r3probe.py manifest /target          -> JSON on stdout, exit 0
    python r3probe.py manifest /target --strict  -> exit 2 on unsupported entries or SQLite failures

What it records (never file contents -- only metadata and SHA-256):
  * every filesystem entry under the root, including the root itself, hidden
    files, empty directories, symlinks (target, not followed), regular files
    (size, SHA-256), with mode / uid / gid for every entry;
  * unsupported entries (hard links, sockets, FIFOs, devices) are listed under
    "unsupported" -- the orchestrator treats any as a hard failure, never drops them;
  * every SQLite database found by its header, examined on a PRIVATE COPY of the
    database plus its -wal/-shm/-journal sidecars (the volume itself is never
    opened by SQLite: with sidecars present SQLite would need to write the -shm
    index, and a read-only mount forbids it): integrity_check, foreign_key_check,
    schema_version rows, migration-10 objects, persisted scheduler mode and
    active baseline jobs.

Exit codes: 0 ok; 2 strict check failed; 3 internal error. Stdlib only.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile

SQLITE_MAGIC = b"SQLite format 3\x00"
SIDECARS = ("-wal", "-shm", "-journal")
ACTIVE_JOB_STATES = ("running", "pause_requested", "cancel_requested")
MIGRATION_10_OBJECTS = ("collection_diagnostics", "idx_collection_diagnostics_session")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk(root):
    entries, unsupported = {}, []

    def record(rel, full):
        st = os.lstat(full)
        e = {"mode": oct(stat.S_IMODE(st.st_mode)), "uid": st.st_uid, "gid": st.st_gid}
        if stat.S_ISLNK(st.st_mode):
            e.update(type="symlink", target=os.readlink(full))
        elif stat.S_ISDIR(st.st_mode):
            e.update(type="dir")
        elif stat.S_ISREG(st.st_mode):
            if st.st_nlink > 1:
                unsupported.append({"path": rel, "reason": f"hard link (nlink={st.st_nlink})"})
            e.update(type="file", size=st.st_size, sha256=sha256_file(full))
        else:
            kind = ("socket" if stat.S_ISSOCK(st.st_mode) else "fifo" if stat.S_ISFIFO(st.st_mode)
                    else "device" if (stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode)) else "unknown")
            unsupported.append({"path": rel, "reason": kind})
            e.update(type=kind)
        entries[rel] = e

    record(".", root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            record(rel, full)
    return dict(sorted(entries.items())), unsupported


def is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def examine_sqlite(root, rel):
    """Opens a private copy of the DB and its sidecars; returns semantic facts."""
    tmp = tempfile.mkdtemp(prefix="r3probe-")
    try:
        base = os.path.join(root, rel)
        name = os.path.basename(rel)
        shutil.copy2(base, os.path.join(tmp, name))
        sidecars = []
        for suf in SIDECARS:
            if os.path.lexists(base + suf):
                shutil.copy2(base + suf, os.path.join(tmp, name + suf))
                sidecars.append(rel + suf)
        c = sqlite3.connect(os.path.join(tmp, name))
        facts = {"sidecars": sidecars,
                 "integrity_check": [r[0] for r in c.execute("pragma integrity_check")],
                 "foreign_key_violations": len(c.execute("pragma foreign_key_check").fetchall())}
        tables = {r[0] for r in c.execute("select name from sqlite_master where type in ('table','index')")}
        if "schema_version" in tables:
            facts["schema_versions"] = [[r[0], r[1]] for r in
                                        c.execute("select version, name from schema_version order by version")]
        facts["migration_10_objects_present"] = sorted(o for o in MIGRATION_10_OBJECTS if o in tables)
        if "scheduler_state" in tables:
            row = c.execute("select mode from scheduler_state where id = 1").fetchone()
            facts["scheduler_mode"] = row[0] if row else None
        if "baseline_jobs" in tables:
            facts["active_baseline_jobs"] = c.execute(
                f"select count(*) from baseline_jobs where status in ({','.join('?' * len(ACTIVE_JOB_STATES))})",
                ACTIVE_JOB_STATES).fetchone()[0]
        c.close()
        return facts
    except sqlite3.DatabaseError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_manifest(root):
    entries, unsupported = walk(root)
    dbs = {}
    for rel, e in entries.items():
        if e["type"] == "file" and not rel.endswith(SIDECARS) and is_sqlite(os.path.join(root, rel)):
            dbs[rel] = examine_sqlite(root, rel)
    return {"entries": entries, "unsupported": unsupported, "sqlite": dict(sorted(dbs.items()))}


def main(argv):
    if len(argv) < 3 or argv[1] != "manifest":
        print(__doc__, file=sys.stderr)
        return 64
    root, strict = argv[2], "--strict" in argv[3:]
    if not os.path.isdir(root):
        print(json.dumps({"error": f"not a directory: {root}"}))
        return 2
    m = build_manifest(root)
    print(json.dumps(m, sort_keys=True))
    if strict and (m["unsupported"] or any("error" in v or v.get("integrity_check") != ["ok"]
                                           for v in m["sqlite"].values())):
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # noqa: BLE001 -- never exit 0 on an unexpected failure
        print(json.dumps({"error": f"internal: {type(exc).__name__}: {exc}"}))
        sys.exit(3)
