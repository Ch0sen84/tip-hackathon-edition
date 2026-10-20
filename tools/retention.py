#!/usr/bin/env python3
"""Raw-observation retention: PROTOTYPE, operator-run, OFF by default.

Never run by the app. Nothing is deleted unless ALL of these hold:
  * TIP_RETENTION_ENABLED=1 in the environment, AND the --i-understand-this-deletes-raw-rows flag;
  * the session's rows were archived by `archive` and the archive re-verifies (sha256, ids);
  * the session's coverage is complete and older than --keep-sessions complete sessions;
  * an exact value curve (session_value_curves) exists for it and is valid against its raw rows,
    so the baseline keeps bit-identical results from the curve ("sealed" after pruning);
  * the session was not the evidence session of any recorded evaluation, and no row of it is
    cited by one (signal_evaluation_evidence), so every recorded decision stays reproducible from
    the database alone (baseline-history values come from the sealed exact curves);
  * every already-sealed curve of that ticker/dataset still matches its signature (no late rows).

Subcommands (stop the app first for archive/prune/restore; plan is read-only):
  plan     what WOULD be archived/pruned, with sizes (opens the DB read-only)
  archive  write <archive-dir>/<dataset>/<ticker>/<session>.jsonl.gz + manifest.jsonl (non-destructive)
  prune    delete archived raw rows and seal their curves (guarded as above)
  restore  re-insert archived rows with their original ids and unseal the curves

Usage: python tools/retention.py --db <tip_hackathon.db> plan [--keep-sessions 90]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core import session_baseline, trading_calendar as tc  # noqa: E402

TABLES = {"darkpool": "darkpool_observations", "options_flow": "options_flow_observations"}
DEFAULT_KEEP = 90


def _day_bounds(session_date: str):
    d = datetime.fromisoformat(session_date).date()
    start = datetime.combine(d, datetime.min.time(), tzinfo=tc.NY_TZ)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _regular_window(session_date: str):
    si = tc.session_info(datetime.fromisoformat(session_date).date())
    return si.session_open_et.astimezone(timezone.utc), si.session_close_et.astimezone(timezone.utc)


def _day_rows(conn, dataset, ticker, session_date, cols="id, source_id, ticker, event_time, retrieved_at, raw_json"):
    lo, hi = _day_bounds(session_date)
    rows = conn.execute(f"SELECT {cols} FROM {TABLES[dataset]} WHERE ticker = ? AND event_time >= ? AND event_time < ? "
                        f"ORDER BY id", (ticker, (lo - timedelta(days=1)).date().isoformat(),
                                         (hi + timedelta(days=1)).date().isoformat())).fetchall()
    out = []
    for r in rows:
        ts = datetime.fromisoformat(str(r[3]).replace("Z", "+00:00"))
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if lo <= ts < hi:
            out.append(r)
    return out


def _candidates(conn, keep: int):
    """(dataset, ticker, session_date) complete sessions older than the newest `keep` complete ones."""
    out = []
    for dataset, ticker in conn.execute("SELECT DISTINCT dataset, ticker FROM session_coverage").fetchall():
        done = [r[0] for r in conn.execute(
            "SELECT session_date FROM session_coverage WHERE dataset = ? AND ticker = ? AND status = 'complete' "
            "ORDER BY session_date DESC", (dataset, ticker))]
        out += [(dataset, ticker, d) for d in sorted(done[keep:])]
    return out


def _cited_ids(conn, table):
    try:
        return {r[0] for r in conn.execute("SELECT observation_id FROM signal_evaluation_evidence "
                                           "WHERE observation_table = ?", (table,))}
    except sqlite3.OperationalError:
        return set()


def _evidence_sessions(conn, ticker) -> set:
    """Sessions that were the EVIDENCE session of any recorded evaluation of this
    ticker. Their raw rows are what that decision read directly, so they are never
    pruned (the engine does not record per-observation citations; see
    signal_evaluation_evidence, which stays empty in practice)."""
    out = set()
    try:
        rows = conn.execute(
            "SELECT json_extract(raw_json, '$.evidence_session'), retrieved_at FROM signal_evaluations "
            "WHERE ticker = ?", (ticker,)).fetchall()
    except sqlite3.OperationalError:
        return out
    for ev, retrieved_at in rows:
        if ev:
            out.add(ev)
            continue
        # Legacy (pre-v2) evaluations carry no evidence_session: protect, conservatively,
        # the New York date they were retrieved on and the trading session before it.
        try:
            ts = datetime.fromisoformat(str(retrieved_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        ny = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(tc.NY_TZ).date()
        out.add(ny.isoformat())
        out.add(tc.previous_trading_day(ny).isoformat())
    return out


def cmd_plan(db, keep):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cited = {ds: _cited_ids(conn, t) for ds, t in TABLES.items()}
    report = {"keep_sessions": keep, "sessions": [], "rows": 0, "raw_bytes": 0, "protected_sessions": 0}
    for dataset, ticker, sd in _candidates(conn, keep):
        rows = _day_rows(conn, dataset, ticker, sd, cols="id, length(raw_json), 0, event_time")
        protected = any(r[0] in cited[dataset] for r in rows) or sd in _evidence_sessions(conn, ticker)
        report["sessions"].append({"dataset": dataset, "ticker": ticker, "session_date": sd, "rows": len(rows),
                                   "raw_bytes": sum(r[1] or 0 for r in rows), "protected_by_evaluations": protected})
        if protected:
            report["protected_sessions"] += 1
        else:
            report["rows"] += len(rows)
            report["raw_bytes"] += sum(r[1] or 0 for r in rows)
    conn.close()
    return report


def _archive_path(archive_dir: Path, dataset, ticker, sd) -> Path:
    return archive_dir / dataset / ticker / f"{sd}.jsonl.gz"


def _write_archive(path: Path, rows) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(dict(zip(("id", "source_id", "ticker", "event_time", "retrieved_at", "raw_json"), r)),
                                 sort_keys=True) + "\n" for r in rows).encode()
    with open(path, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as gz:
            gz.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _read_archive(path: Path):
    payload = gzip.decompress(path.read_bytes())
    return [json.loads(l) for l in payload.decode().splitlines() if l], hashlib.sha256(payload).hexdigest()


def _manifest(archive_dir: Path) -> dict:
    m = {}
    f = archive_dir / "manifest.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            e = json.loads(line)
            m[(e["dataset"], e["ticker"], e["session_date"])] = e
    return m


def cmd_archive(db, keep, archive_dir: Path):
    from app.ingestion import IngestionCatalog
    cat = IngestionCatalog(db)
    conn = sqlite3.connect(db)
    done, skipped = 0, []
    existing = _manifest(archive_dir)
    for dataset, ticker, sd in _candidates(conn, keep):
        if (dataset, ticker, sd) in existing:
            continue
        o, c = _regular_window(sd)
        curve = session_baseline._session_curve(cat, dataset, ticker, session_baseline.VALUE_KEYS[dataset], sd, o, c,
                                                force=True)
        if curve is None or curve is session_baseline.SEALED_CHANGED:
            skipped.append({"dataset": dataset, "ticker": ticker, "session_date": sd, "why": "no exact curve"})
            continue
        rows = _day_rows(conn, dataset, ticker, sd)
        path = _archive_path(archive_dir, dataset, ticker, sd)
        sha = _write_archive(path, rows)
        back, sha2 = _read_archive(path)
        if sha2 != sha or [r["id"] for r in back] != [r[0] for r in rows]:
            raise SystemExit(f"archive verification failed for {path}")
        entry = {"dataset": dataset, "ticker": ticker, "session_date": sd, "rows": len(rows),
                 "ids_sha256": hashlib.sha256(",".join(str(r[0]) for r in rows).encode()).hexdigest(),
                 "sha256": sha, "file": str(path.relative_to(archive_dir)), "created_at": datetime.now(timezone.utc).isoformat()}
        with open(archive_dir / "manifest.jsonl", "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        done += 1
    conn.close()
    return {"archived_sessions": done, "skipped": skipped}


def _refresh_sealed_signatures(conn, cat, dataset, ticker):
    for sd, w0, w1 in conn.execute("SELECT session_date, window_start_utc, window_end_utc FROM session_value_curves "
                                   "WHERE dataset = ? AND ticker = ? AND sealed = 1", (dataset, ticker)).fetchall():
        sig = _signature(conn, dataset, ticker, datetime.fromisoformat(w0), datetime.fromisoformat(w1))
        conn.execute("UPDATE session_value_curves SET src_count = ?, src_max_id = ? WHERE dataset = ? AND ticker = ? "
                     "AND session_date = ?", (*sig, dataset, ticker, sd))


def _signature(conn, dataset, ticker, start, end):
    lo, hi = (start - timedelta(days=1)).date().isoformat(), (end + timedelta(days=1)).date().isoformat()
    r = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {TABLES[dataset]} WHERE ticker = ? "
                     f"AND event_time >= ? AND event_time < ?", (ticker, lo, hi)).fetchone()
    return int(r[0]), int(r[1])


def _sealed_all_valid(conn, dataset, ticker) -> bool:
    for w0, w1, c, m in conn.execute("SELECT window_start_utc, window_end_utc, src_count, src_max_id FROM "
                                     "session_value_curves WHERE dataset = ? AND ticker = ? AND sealed = 1",
                                     (dataset, ticker)).fetchall():
        if _signature(conn, dataset, ticker, datetime.fromisoformat(w0), datetime.fromisoformat(w1)) != (c, m):
            return False
    return True


def cmd_prune(db, keep, archive_dir: Path, confirmed: bool):
    if os.environ.get("TIP_RETENTION_ENABLED") != "1" or not confirmed:
        raise SystemExit("refused: pruning needs TIP_RETENTION_ENABLED=1 and --i-understand-this-deletes-raw-rows")
    from app.ingestion import IngestionCatalog
    cat = IngestionCatalog(db)
    manifest = _manifest(archive_dir)
    conn = sqlite3.connect(db, isolation_level=None)
    cited = {ds: _cited_ids(conn, t) for ds, t in TABLES.items()}
    pruned, skipped = [], []
    for dataset, ticker, sd in _candidates(conn, keep):
        e = manifest.get((dataset, ticker, sd))
        why = None
        if e is None:
            why = "not archived"
        else:
            back, sha = _read_archive(archive_dir / e["file"])
            rows = _day_rows(conn, dataset, ticker, sd)
            ids = [r[0] for r in rows]
            if sha != e["sha256"] or [r["id"] for r in back] != ids:
                why = "archive does not match current rows (re-archive first)"
            elif any(i in cited[dataset] for i in ids):
                why = "cited by a recorded evaluation"
            elif sd in _evidence_sessions(conn, ticker):
                why = "evidence session of a recorded evaluation"
            else:
                o, c = _regular_window(sd)
                curve = session_baseline._session_curve(cat, dataset, ticker, session_baseline.VALUE_KEYS[dataset], sd, o, c,
                                                force=True)
                if curve is None or curve is session_baseline.SEALED_CHANGED:
                    why = "no exact curve"
                elif not _sealed_all_valid(conn, dataset, ticker):
                    why = "a sealed session of this ticker changed since pruning (restore first)"
        if why:
            skipped.append({"dataset": dataset, "ticker": ticker, "session_date": sd, "why": why})
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(f"DELETE FROM {TABLES[dataset]} WHERE id = ?", [(i,) for i in ids])
            conn.execute("UPDATE session_value_curves SET sealed = 1, archive_ref = ? WHERE dataset = ? AND ticker = ? "
                         "AND session_date = ?", (e["file"], dataset, ticker, sd))
            _refresh_sealed_signatures(conn, cat, dataset, ticker)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        pruned.append({"dataset": dataset, "ticker": ticker, "session_date": sd, "rows": len(ids)})
    conn.close()
    return {"pruned_sessions": pruned, "skipped": skipped,
            "note": "space is reused by SQLite; run VACUUM (with the app stopped) to shrink the file"}


def cmd_restore(db, archive_dir: Path, only=None):
    from app.ingestion import IngestionCatalog
    cat = IngestionCatalog(db)
    conn = sqlite3.connect(db, isolation_level=None)
    restored = []
    for (dataset, ticker, sd), e in sorted(_manifest(archive_dir).items()):
        if only and sd != only:
            continue
        back, sha = _read_archive(archive_dir / e["file"])
        if sha != e["sha256"]:
            raise SystemExit(f"archive checksum mismatch: {e['file']}")
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(f"INSERT OR IGNORE INTO {TABLES[dataset]} (id, source_id, ticker, event_time, retrieved_at, raw_json) "
                         f"VALUES (?, ?, ?, ?, ?, ?)", [(r["id"], r["source_id"], r["ticker"], r["event_time"],
                                                         r["retrieved_at"], r["raw_json"]) for r in back])
        conn.execute("UPDATE session_value_curves SET sealed = 0, archive_ref = NULL WHERE dataset = ? AND ticker = ? "
                     "AND session_date = ?", (dataset, ticker, sd))
        _refresh_sealed_signatures(conn, cat, dataset, ticker)
        conn.execute("COMMIT")
        restored.append({"dataset": dataset, "ticker": ticker, "session_date": sd, "rows": len(back)})
    conn.close()
    return {"restored_sessions": restored}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("command", choices=["plan", "archive", "prune", "restore"])
    ap.add_argument("--keep-sessions", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--archive-dir")
    ap.add_argument("--session")
    ap.add_argument("--i-understand-this-deletes-raw-rows", dest="confirmed", action="store_true")
    a = ap.parse_args(argv)
    if a.keep_sessions < 30:
        raise SystemExit("refused: --keep-sessions below 30 would not leave a full lookback of raw history")
    if a.command == "plan":
        out = cmd_plan(a.db, a.keep_sessions)
    else:
        if not a.archive_dir:
            raise SystemExit("--archive-dir is required")
        d = Path(a.archive_dir)
        out = {"archive": lambda: cmd_archive(a.db, a.keep_sessions, d),
               "prune": lambda: cmd_prune(a.db, a.keep_sessions, d, a.confirmed),
               "restore": lambda: cmd_restore(a.db, d, a.session)}[a.command]()
    print(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    main()
