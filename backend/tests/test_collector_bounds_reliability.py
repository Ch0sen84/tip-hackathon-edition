"""Phase 9 collector reliability: a page with rows outside the requested
window is rejected whole (never stored), recorded as a SANITIZED diagnostic,
and the same window is re-requested at most BOUNDS_RETRIES_PER_WINDOW times.
Coverage is proven only by pages that pass every check, so a recovered walk
is as trustworthy as a clean one and a persistent violation stays UNKNOWN.

FaultyUW wraps the verified-contract fake (tests/uw_paginated.py) and corrupts
chosen dark-pool responses. All market rows here are CONSTRUCTED."""
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.core import session_baseline
from app.ingestion import IngestionCatalog, darkpool_print_key
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, PaginatedUW, _ts, iso, prior_sessions, session_prints

EVIDENCE = date(2026, 10, 16)
AS_OF = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
DAY = prior_sessions(EVIDENCE, 1)[0]                       # a completed prior session
OPEN_UTC = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE_UTC = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
INJECTED = "INJECTED-OUT-OF-WINDOW"


class FaultyUW(PaginatedUW):
    """`faults` maps the n-th dark-pool request (1-based) to a corruption;
    `persistent=(n, mode)` corrupts every dark-pool request from the n-th on."""

    def __init__(self, *a, faults=None, persistent=None, **k):
        super().__init__(*a, **k)
        self.faults, self.persistent, self.dp_calls = dict(faults or {}), persistent, 0

    def get(self, url, headers=None, params=None, timeout=None):
        resp = super().get(url, headers, params, timeout)
        if "/api/darkpool/" not in url or resp.status_code != 200:
            return resp
        self.dp_calls += 1
        mode = self.faults.get(self.dp_calls)
        if mode is None and self.persistent and self.dp_calls >= self.persistent[0]:
            mode = self.persistent[1]
        if mode is None:
            return resp
        p, data = dict(params), list(resp.json()["data"])
        hi, lo = _ts(p["older_than"]), _ts(p["newer_than"])

        def bad(ts):
            return {"ticker": "ABCD", "tracking_id": INJECTED, "executed_at": iso(ts), "price": "999.99",
                    "size": 7, "premium": "6999.93", "canceled": False}

        if mode == "above_cursor":
            data = [bad(hi + timedelta(seconds=30))] + data[:-1]
        elif mode == "at_cursor":
            data = [bad(hi)] + data[:-1]
        elif mode == "below_lower":
            data = data[:-1] + [bad(lo - timedelta(minutes=10))]
        elif mode == "hour_offset":
            data = [bad(hi + timedelta(hours=4))] + data[:-1]
        elif mode == "ignore_upper":            # provider drops older_than: the window's newest page again
            rows = [r for r in self.darkpool["ABCD"] if _ts(r["executed_at"]).astimezone(NY).date().isoformat() == p["date"]]
            data = self._page(rows, "executed_at", {"newer_than": p["newer_than"], "limit": p["limit"]})
        return FakeResponse(200, {"data": data})


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "bounds.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(sc, "_sleep", slept.append)
    return slept


def dense_day(per_second=1, every_minutes=0.05):
    """~7.8k prints over the session (16 pages of 500); `per_second` prints
    share each timestamp so page breaks split seconds."""
    rows, t, i = [], datetime.combine(DAY, time(9, 30), tzinfo=NY), 0
    stop = datetime.combine(DAY, time(16, 0), tzinfo=NY)
    while t <= stop and len(rows) < 7800:
        for k in range(per_second):
            rows.append({"ticker": "ABCD", "executed_at": iso(t), "price": "100.00", "size": 100 + k,
                         "premium": "10000.00", "tracking_id": f"p{i}", "volume": i, "canceled": False})
            i += 1
        t += timedelta(minutes=every_minutes)
    return rows


def cover(uw, cat, d=DAY, pages=60, now=AS_OF):
    return sc.update_session_coverage(uw.adapter().client, cat, "darkpool", "ABCD", d, now_utc=now, max_pages=pages)


def stored_keys(cat, d=DAY):
    o = datetime.combine(d, time(0, 0), tzinfo=NY).astimezone(timezone.utc)
    return {darkpool_print_key("ABCD", r) for r in cat.observations_between("darkpool", "ABCD", o, o + timedelta(days=1))}


def expected_keys(rows):
    return {darkpool_print_key("ABCD", r) for r in rows}


def injected_stored(cat):
    with cat._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM darkpool_observations WHERE raw_json LIKE ?",
                            (f"%{INJECTED}%",)).fetchone()[0]


def diags(cat, d=DAY):
    return list(reversed(cat.list_collection_diagnostics("darkpool", "ABCD", d.isoformat())))


# --- failure location ---------------------------------------------------------

def test_bounds_failure_on_page_1_without_retry_stores_nothing_and_is_unknown(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={1: "above_cursor"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "bounds_not_honored"
    assert u.coverage["covered_from_utc"] is None and u.coverage["covered_through_utc"] is None
    assert stored_keys(cat) == set() and injected_stored(cat) == 0
    assert u.pages == 1 and u.bounds_retries == 0
    [d] = diags(cat)
    assert d["page_in_window"] == 1 and d["attempt"] == 1 and d["action"] == "session_left_unknown"
    assert d["violations"]["above_upper"] == 1 and d["classification"] == "upper_bound_violated"
    assert d["request"] == {"date": DAY.isoformat(), "limit": 500, "newer_than": iso(OPEN_UTC),
                            "older_than": iso(CLOSE_UTC + timedelta(seconds=1))}   # [open, close] inclusive


def test_bounds_failure_after_several_good_pages_keeps_them_but_claims_no_coverage(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    rows = dense_day()
    clean = PaginatedUW(darkpool=rows)
    cover(clean, cat.__class__(str(cat.db_path) + ".ref"))            # reference walk: the first 4 pages
    ref_pages = [p for _, p in clean.calls][:5]
    uw = FaultyUW(darkpool=rows, faults={5: "above_cursor"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "bounds_not_honored"
    assert u.coverage["covered_from_utc"] is None
    assert [p for _, p in uw.calls] == ref_pages                       # identical requests up to the rejection
    page4_oldest = _ts(ref_pages[4]["older_than"]) - timedelta(seconds=1)   # pages 1-4 reach back to here
    assert stored_keys(cat) == expected_keys([r for r in rows if _ts(r["executed_at"]) >= page4_oldest])
    assert injected_stored(cat) == 0
    [d] = diags(cat)
    assert d["page_in_window"] == 5 and d["previous_page"]["oldest"] is not None


# --- bounded retry ------------------------------------------------------------

def test_one_transient_bad_page_is_retried_and_the_session_completes_exactly(cat, no_sleep):
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={3: "ignore_upper"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "complete" and u.bounds_retries == 1 and u.bounds_recovered == 1
    assert stored_keys(cat) == expected_keys(rows)                    # nothing skipped, nothing extra
    assert injected_stored(cat) == 0
    clean = PaginatedUW(darkpool=rows)
    cover(clean, IngestionCatalog(str(cat.db_path) + ".ref"))
    assert len(uw.calls) == len(clean.calls) + 1                       # exactly one extra request
    assert uw.calls[2][1] == uw.calls[3][1]                            # the SAME window re-requested
    [d] = diags(cat)
    assert d["action"] == "retry_same_window" and d["classification"] == "upper_bound_ignored"
    assert no_sleep == [sc.BOUNDS_RETRY_DELAY_S]


def test_repeated_bounds_failure_is_bounded_and_stays_unknown(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 2)
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, persistent=(4, "above_cursor"))
    u = cover(uw, cat, pages=60)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "bounds_not_honored"
    assert uw.dp_calls == 3 + 3                                        # 3 good pages, then 1 + 2 retries, no loop
    assert u.bounds_retries == 2 and u.bounds_recovered == 0
    actions = [d["action"] for d in diags(cat)]
    assert actions == ["retry_same_window", "retry_same_window", "session_left_unknown"]
    assert [d["attempt"] for d in diags(cat)] == [1, 2, 3]
    assert injected_stored(cat) == 0


def test_a_retry_never_exceeds_the_page_budget_and_never_promotes(cat):
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={3: "above_cursor"})
    u = cover(uw, cat, pages=3)                                         # the bad page is the last one allowed
    assert uw.dp_calls == 3 and u.pages == 3
    assert u.coverage["status"] == "unknown" and u.bounds_retries == 0
    assert diags(cat)[-1]["action"] == "session_left_unknown"


def test_retries_zero_restores_the_previous_behaviour(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    uw = FaultyUW(darkpool=dense_day(), faults={3: "ignore_upper"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "unknown" and uw.dp_calls == 3


def test_dense_same_second_boundaries_with_a_recovered_retry_skip_nothing(cat):
    rows = dense_day(per_second=31, every_minutes=1 / 60)               # 31 prints in every second
    uw = FaultyUW(darkpool=rows, faults={2: "at_cursor"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "complete" and u.bounds_recovered == 1
    assert stored_keys(cat) == expected_keys(rows)
    with cat._conn() as conn:
        dup = conn.execute("SELECT COUNT(*) - COUNT(DISTINCT source_id) FROM darkpool_observations").fetchone()[0]
    assert dup == 0
    assert diags(cat)[0]["classification"] == "cursor_boundary_inclusive"


def test_failure_then_restart_resumes_from_the_checkpoint_and_dedups(cat):
    rows = dense_day()
    first = cover(PaginatedUW(darkpool=rows), cat, pages=4)            # interrupted: partial checkpoint
    assert first.coverage["status"] == "partial"
    c_from = first.coverage["covered_from_utc"]
    with cat._conn() as conn:
        before = conn.execute("SELECT COUNT(*) FROM darkpool_observations").fetchone()[0]
    uw = FaultyUW(darkpool=rows, faults={2: "above_cursor"})            # resumed walk hits one bad page
    u = cover(uw, cat)
    assert u.coverage["status"] == "complete" and u.bounds_recovered == 1
    assert all(_ts(p["older_than"]) <= _ts(c_from) + timedelta(seconds=1) for _, p in uw.calls)   # no re-walk
    assert stored_keys(cat) == expected_keys(rows) and injected_stored(cat) == 0
    with cat._conn() as conn:
        after = conn.execute("SELECT COUNT(*) FROM darkpool_observations").fetchone()[0]
        dup = conn.execute("SELECT COUNT(*) - COUNT(DISTINCT source_id) FROM darkpool_observations").fetchone()[0]
    assert dup == 0 and after - before == len(expected_keys(rows)) - before


def test_persistent_failure_in_a_resumed_window_drops_to_unknown_never_complete(cat):
    rows = dense_day()
    cover(PaginatedUW(darkpool=rows), cat, pages=4)
    u = cover(FaultyUW(darkpool=rows, persistent=(1, "below_lower")), cat)
    assert u.coverage["status"] == "unknown" and u.coverage["covered_from_utc"] is None
    assert diags(cat)[0]["classification"] == "lower_bound_ignored"
    # the unknown session is excluded from the baseline, and the backfill does not re-walk it
    b = session_baseline.compute(cat, "darkpool", "ABCD", EVIDENCE, as_of_utc=AS_OF, min_sessions=0)
    assert b.coverage_summary.get("unknown:bounds_not_honored") == 1 and b.eligible_sessions == 0
    probe = FaultyUW(darkpool=rows)
    sc.backfill_baseline(probe.adapter().client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20,
                         lookback_sessions=1, now_utc=AS_OF)
    assert not [p for _, p in probe.calls if p.get("date") == DAY.isoformat()]


# --- diagnostics: content, classification, sanitization -------------------------

@pytest.mark.parametrize("mode,expected", [
    ("at_cursor", "cursor_boundary_inclusive"),
    ("hour_offset", "timezone_offset_suspected"),
    ("below_lower", "lower_bound_ignored"),
    ("above_cursor", "upper_bound_violated"),
])
def test_diagnostic_classification_hypotheses(cat, monkeypatch, mode, expected):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    cover(FaultyUW(darkpool=dense_day(), faults={2: mode}), cat)
    [d] = diags(cat)
    assert d["classification"] == expected
    if mode == "hour_offset":
        assert "whole_hour_offsets" in d["flags"]


def test_diagnostics_are_sanitized_and_bounded(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={2: "ignore_upper"})            # 500 offending rows on one page
    cover(uw, cat)
    with cat._conn() as conn:
        [(raw,)] = conn.execute("SELECT detail_json FROM collection_diagnostics").fetchall()
    d = json.loads(raw)
    assert d["response_rows"] == 500 and d["violations"]["above_upper"] + d["violations"]["at_upper"] >= 1
    assert len(d["samples"]) <= sc.DIAGNOSTIC_SAMPLE_ROWS
    assert set(d["samples"][0]) == {"index", "raw", "utc", "ny_date", "kind", "delta_s", "fractional_seconds"}
    for forbidden in ("fake-test-key", "Authorization", "Bearer", "tracking_id", "premium", "price", "size", "p1"):
        assert forbidden not in raw
    assert d["observed"]["min"] and d["observed"]["max"] and d["expected"]["upper_inclusive"] is False
    assert len(raw) < 4000


def test_diagnostic_table_cap_and_a_diagnostic_failure_never_changes_the_outcome(cat, monkeypatch):
    monkeypatch.setattr(cat, "DIAGNOSTICS_MAX_ROWS", 1)
    cover(FaultyUW(darkpool=dense_day(), faults={2: "at_cursor"}), cat)         # 1 stored
    u = cover(FaultyUW(darkpool=dense_day(), faults={1: "at_cursor"}), cat, d=prior_sessions(EVIDENCE, 2)[1])
    with cat._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_diagnostics").fetchone()[0] == 1
    assert u.coverage["status"] == "complete"

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(cat, "record_collection_diagnostic", boom)
    u2 = cover(FaultyUW(darkpool=dense_day(), faults={1: "at_cursor"}), cat, d=prior_sessions(EVIDENCE, 3)[2])
    assert u2.coverage["status"] == "complete"


def test_flow_alert_windows_get_the_same_protection(cat, monkeypatch):
    """Flow alerts: both bounds inclusive; an alert outside the window is
    rejected and retried the same way."""
    from tests.uw_paginated import session_alerts
    alerts = session_alerts("ABCD", DAY, 1000.0, every_minutes=0.5)
    uw = PaginatedUW(flow=alerts)
    real_get, n = uw.get, {"i": 0}

    def get(url, headers=None, params=None, timeout=None):
        resp = real_get(url, headers, params, timeout)
        if "flow-alerts" in url:
            n["i"] += 1
            if n["i"] == 2:
                data = resp.json()["data"]
                late = {**data[0], "id": INJECTED, "created_at": iso(_ts(params["older_than"]) + timedelta(seconds=5))}
                return FakeResponse(200, {"data": [late] + data[:-1]})
        return resp
    uw.get = get
    u = sc.update_session_coverage(uw.adapter().client, cat, "options_flow", "ABCD", DAY, now_utc=AS_OF, max_pages=30)
    assert u.coverage["status"] == "complete" and u.bounds_recovered == 1
    ids = {r["id"] for r in cat.observations_between("options_flow", "ABCD", OPEN_UTC, CLOSE_UTC)}
    assert ids == {a["id"] for a in alerts}
    [d] = cat.list_collection_diagnostics("options_flow", "ABCD")
    assert d["expected"]["upper_inclusive"] is True and d["request"]["ticker_symbol"] == "ABCD"


def test_migration_10_is_additive(cat):
    with cat._conn() as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(collection_diagnostics)")]
        cov_cols = [r[1] for r in conn.execute("PRAGMA table_info(session_coverage)")]
    assert versions == list(range(1, 11))
    assert "detail_json" in cols
    assert cov_cols == ["id", "dataset", "ticker", "session_date", "status", "covered_from_utc",
                        "covered_through_utc", "pages_walked", "rows_seen", "last_error", "retrieved_at"]
