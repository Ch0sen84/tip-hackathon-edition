"""Completed-session value curves (Phase 4 performance) are an exact, self-
invalidating cache of what the baseline reads from raw observations.

The baseline compares the evidence session's value up to its covered point
with the SAME elapsed slice of each prior complete session, so a full-session
total cannot be cached. The cache stores each completed session's ordered
(event time, value) sequence and is validated against the raw rows' signature
on every use. These tests prove: bit-identical results to the raw path at many
cutoffs (including timestamp formats whose string order is not chronological),
rebuilds on late / canceled prints, raw fallback when deduplication is not
prefix-stable, and preserved dark-pool composite identity.
"""
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import certified_provenance

from app.core import session_baseline, trading_calendar as tc
from app.ingestion import IngestionCatalog

T = "CURV"
EVIDENCE = date(2026, 10, 19)


def sessions_before(d, n):
    out = []
    while len(out) < n:
        d = tc.previous_trading_day(d)
        out.append(d)
    return sorted(out)


def utc_open_close(d):
    si = tc.session_info(d)
    return si.session_open_et.astimezone(timezone.utc), si.session_close_et.astimezone(timezone.utc)


def stamp(dt, style):
    if style == "z":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if style == "frac":   # sorts BEFORE the whole-second "Z" form of the same second
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.isoformat()   # "+00:00"


def dp_rows(d, n, rnd, styles=("z",)):
    o, c = utc_open_close(d)
    span = int((c - o).total_seconds())
    rows = []
    for i in range(n):
        ts = o + timedelta(seconds=rnd.randrange(span), microseconds=rnd.randrange(1_000_000))
        p, s = round(100 + rnd.random() * 5, 2), rnd.randint(100, 9000)
        rows.append({"ticker": T, "tracking_id": rnd.randint(1, 40), "executed_at": stamp(ts, rnd.choice(styles)),
                     "price": str(p), "size": s, "volume": rnd.randint(1000, 90000), "premium": str(round(p * s, 2)),
                     "canceled": False})
    return rows


def flow_rows(d, n, rnd):
    o, c = utc_open_close(d)
    span = int((c - o).total_seconds())
    return [{"id": f"{d.isoformat()}-{i}", "ticker": T, "type": rnd.choice(["call", "put"]),
             "created_at": stamp(o + timedelta(seconds=rnd.randrange(span), microseconds=rnd.randrange(10**6)), "frac"),
             "total_premium": str(rnd.randint(10_000, 900_000))} for i in range(n)]


def seed(tmp_path, styles=("z",), n_sessions=22, seed_value=11):
    rnd = random.Random(seed_value)
    cat = IngestionCatalog(str(tmp_path / "curves.db"))
    for d in sessions_before(EVIDENCE, n_sessions) + [EVIDENCE]:
        o, c = utc_open_close(d)
        cat.catalog_darkpool_observations(T, dp_rows(d, 400, rnd, styles))
        cat.catalog_options_flow_observations(T, flow_rows(d, 150, rnd))
        for ds in ("darkpool", "options_flow"):
            cat.upsert_session_coverage(ds, T, d.isoformat(), "complete", o.isoformat(), c.isoformat(), 1, 1,
                                        provenance=certified_provenance())
    return cat


def compute_both(cat, ds, as_of, monkeypatch):
    monkeypatch.setenv("TIP_SESSION_CURVE_CACHE", "0")
    raw = session_baseline.compute(cat, ds, T, EVIDENCE, as_of_utc=as_of).as_metrics()
    monkeypatch.setenv("TIP_SESSION_CURVE_CACHE", "1")
    cached = session_baseline.compute(cat, ds, T, EVIDENCE, as_of_utc=as_of).as_metrics()
    return raw, cached


def cutoffs():
    o, c = utc_open_close(EVIDENCE)
    t = o + timedelta(minutes=1, seconds=7, microseconds=123_457)
    while t < c + timedelta(hours=1):
        yield t
        t += timedelta(minutes=23, seconds=11, microseconds=777)


@pytest.mark.parametrize("styles", [("z",), ("z", "frac", "iso")], ids=["uniform", "mixed-formats"])
@pytest.mark.parametrize("ds", ["darkpool", "options_flow"])
def test_cached_baseline_is_bit_identical_to_raw_at_every_cutoff(tmp_path, monkeypatch, ds, styles):
    cat = seed(tmp_path, styles=styles)
    session_baseline.CURVE_STATS.update(hits=0, misses=0, uncacheable=0, write_errors=0)
    n = 0
    for as_of in cutoffs():
        raw, cached = compute_both(cat, ds, as_of, monkeypatch)
        assert cached == raw, as_of
        n += 1
    assert n >= 15
    assert session_baseline.CURVE_STATS["hits"] > 0 and session_baseline.CURVE_STATS["write_errors"] == 0


def test_mixed_formats_produce_non_monotonic_curves_and_still_match(tmp_path, monkeypatch):
    cat = seed(tmp_path, styles=("z", "frac"))
    d = sessions_before(EVIDENCE, 1)[0]
    o, c = utc_open_close(d)
    curve = session_baseline._build_curve(cat, "darkpool", T, "premium", o, c)
    assert curve is not None and curve.monotonic is False
    raw, cached = compute_both(cat, "darkpool", c + timedelta(hours=1), monkeypatch)
    assert cached == raw


def test_late_print_in_a_completed_session_rebuilds_the_curve(tmp_path, monkeypatch):
    cat = seed(tmp_path)
    as_of = utc_open_close(EVIDENCE)[0] + timedelta(hours=2)
    before_raw, before_cached = compute_both(cat, "darkpool", as_of, monkeypatch)
    assert before_raw == before_cached
    old = sessions_before(EVIDENCE, 5)[0]
    o, _ = utc_open_close(old)
    cat.catalog_darkpool_observations(T, [{"ticker": T, "tracking_id": 999, "executed_at": stamp(o + timedelta(minutes=5), "z"),
                                           "price": "101.00", "size": 50_000, "volume": 1, "premium": "5050000.00",
                                           "canceled": False}])
    session_baseline.CURVE_STATS.update(hits=0, misses=0)
    after_raw, after_cached = compute_both(cat, "darkpool", as_of, monkeypatch)
    assert after_cached == after_raw
    assert after_raw["historical_mean"] != before_raw["historical_mean"]       # the late print matters
    assert session_baseline.CURVE_STATS["misses"] >= 1                          # and the stale curve was rebuilt


def test_cancellation_of_a_cached_print_is_honoured(tmp_path, monkeypatch):
    cat = seed(tmp_path)
    as_of = utc_open_close(EVIDENCE)[1] + timedelta(hours=1)
    compute_both(cat, "darkpool", as_of, monkeypatch)                           # warm the cache
    old = sessions_before(EVIDENCE, 3)[0]
    o, c = utc_open_close(old)
    victim = cat.observations_between("darkpool", T, o, c)[0]
    canceled = {k: v for k, v in victim.items() if not k.startswith("_")}
    canceled["canceled"] = True
    canceled["sale_cond_codes"] = "cancel"                                      # distinct raw row, same print
    cat.catalog_darkpool_observations(T, [canceled])
    raw, cached = compute_both(cat, "darkpool", as_of, monkeypatch)
    assert cached == raw


def test_flow_id_seen_at_two_times_falls_back_to_raw(tmp_path, monkeypatch):
    cat = seed(tmp_path)
    old = sessions_before(EVIDENCE, 2)[0]
    o, c = utc_open_close(old)
    first = cat.observations_between("options_flow", T, o, c)[0]
    moved = {k: v for k, v in first.items() if not k.startswith("_")}
    moved["created_at"] = stamp(c - timedelta(minutes=1), "frac")               # same alert id, corrected time
    moved["total_premium"] = "1"
    assert cat.catalog_options_flow_observations(T, [moved]) == 0                # id-keyed: ignored at write
    # Only a legacy row stored under an older source_id scheme can put the same
    # alert id at a second time into the window; such a session is not
    # prefix-stable, so the curve declines and the raw path is used.
    import json
    with cat._conn() as conn:
        conn.execute("INSERT INTO options_flow_observations (source_id, ticker, event_time, retrieved_at, raw_json) "
                     "VALUES (?, ?, ?, ?, ?)", ("legacy-scheme-1", T, moved["created_at"], moved["created_at"],
                                                json.dumps(moved)))
        conn.commit()
    assert session_baseline._build_curve(cat, "options_flow", T, "total_premium", o, c) is None
    for as_of in list(cutoffs())[::3]:
        raw, cached = compute_both(cat, "options_flow", as_of, monkeypatch)
        assert cached == raw


def test_shared_tracking_id_prints_stay_distinct_in_the_curve(tmp_path):
    cat = IngestionCatalog(str(tmp_path / "ids.db"))
    d = sessions_before(EVIDENCE, 1)[0]
    o, c = utc_open_close(d)
    base = {"ticker": T, "tracking_id": 7, "price": "100.00", "size": 10, "volume": 5, "premium": "1000.00", "canceled": False}
    rows = [dict(base, executed_at=stamp(o + timedelta(minutes=1), "z")),
            dict(base, executed_at=stamp(o + timedelta(minutes=2), "z")),           # same tracking_id, distinct print
            dict(base, executed_at=stamp(o + timedelta(minutes=1), "z"), price="100.0", premium="1000")]  # re-format dup
    cat.catalog_darkpool_observations(T, rows)
    curve = session_baseline._build_curve(cat, "darkpool", T, "premium", o, c)
    assert curve.n == 2 and curve.upto(c) == (2000.0, 2)


def test_cache_can_be_disabled_and_then_writes_nothing(tmp_path, monkeypatch):
    cat = seed(tmp_path, n_sessions=3)
    monkeypatch.setenv("TIP_SESSION_CURVE_CACHE", "0")
    session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=utc_open_close(EVIDENCE)[1])
    with cat._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session_value_curves").fetchone()[0] == 0
