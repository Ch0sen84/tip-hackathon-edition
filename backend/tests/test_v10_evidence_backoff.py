"""v10 release blocker (Layla's review of v9 727ffc2): F-1 / R7-1 residual.

An in-progress evidence session left `unknown` (rejected content, e.g. bounds_not_honored) is backed off for the
catch-up step, but v9 still gave it the LEFTOVER walk of step (3). That walk could not reach a proven edge within
the leftover pages and stored `partial / page_budget_exhausted` over `unknown`, erasing the backoff; the next call
then ran the catch-up again, hit the rejection again (`unknown`), and so on: the session oscillated between
unknown and partial and kept spending pages.

These multi-call tests drive the real collector (and the real job runner) against the verified-contract fake
provider with a deterministic deep rejection. Constructed data only; no network."""
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.ingestion import IngestionCatalog
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, live, run_job  # noqa: F401 (fixture)
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, PaginatedUW, _ts, iso, prior_sessions, session_alerts, session_prints

OPEN_EV = datetime.combine(EVIDENCE, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
DEEP = datetime.combine(EVIDENCE, time(10, 0), tzinfo=NY).astimezone(timezone.utc)
assert AS_OF.astimezone(NY).time() == time(12, 0)            # market hours: the evidence session is in progress


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def dense(d, start=time(9, 30), end=time(12, 0), step_s=3, tag="d"):
    """One dark-pool print every 3 s: 500-row pages = 25 minutes, so ~6 pages from 12:00 to the open."""
    out, t, i = [], datetime.combine(d, start, tzinfo=NY), 0
    stop = datetime.combine(d, end, tzinfo=NY)
    while t <= stop:
        out.append({"ticker": "ABCD", "executed_at": iso(t), "price": "100.00", "size": 100 + (i % 7),
                    "premium": "10000.00", "tracking_id": f"{tag}{d.isoformat()}-{i}", "volume": i,
                    "canceled": False})
        t += timedelta(seconds=step_s)
        i += 1
    return out


class DeepRejectUW(PaginatedUW):
    """The evidence session's dark-pool pages are clean until the cursor reaches 10:00 NY; from there every
    response carries one row above the cursor (a persistent provider fault deep in the session).
    `healed = True` makes the provider clean again (a legitimate recovery)."""

    healed = False

    def get(self, url, headers=None, params=None, timeout=None):
        resp = super().get(url, headers, params, timeout)
        p = dict(params or {})
        if (not self.healed and "/api/darkpool/" in url and p.get("date") == EVIDENCE.isoformat()
                and resp.status_code == 200 and _ts(p["older_than"]) <= DEEP):
            hi = _ts(p["older_than"])
            bad = {"ticker": "ABCD", "tracking_id": "BAD", "executed_at": iso(hi + timedelta(seconds=30)),
                   "price": "1", "size": 1, "premium": "1", "canceled": False}
            return FakeResponse(200, {"data": [bad] + list(resp.json()["data"])[:-1]})
        return resp


def ev_dp(uw, start=0):
    return [p for u, p in uw.calls[start:] if "/api/darkpool/" in u and p.get("date") == EVIDENCE.isoformat()]


def provider_pages(uw, start=0):
    return [u for u, _ in uw.calls[start:] if "/api/darkpool/" in u or "flow-alerts" in u]


def world(n_prior=8):
    rows = dense(EVIDENCE)
    for d in prior_sessions(EVIDENCE, n_prior):
        rows += session_prints("ABCD", d, 10_000.0)                       # 1 page each
    return DeepRejectUW(darkpool=rows)


# --- Layla's sequence, multi-call ----------------------------------------------------------------------------
def test_backed_off_evidence_never_oscillates_and_spends_nothing_until_its_backoff_expires(tmp_path):
    uw = world()
    cat, client = IngestionCatalog(str(tmp_path / "v10.db")), uw.adapter(["ABCD"]).client
    states, rejected_at = [], None
    for k in range(12):                                                    # 12 calls x 4 pages
        before = len(uw.calls)
        r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=4, lookback_sessions=8,
                                 now_utc=AS_OF + timedelta(minutes=k))
        ev = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat()) or {}
        states.append((ev.get("status"), ev.get("last_error"), len(ev_dp(uw, before))))
        assert r["pages_used"] == len(provider_pages(uw, before)) <= 4         # exact accounting, cap intact
        if rejected_at is None and ev.get("status") == "unknown":
            rejected_at = k
    assert rejected_at is not None, states                                  # the walk was rejected once
    after = states[rejected_at + 1:]
    # backed off: stays unknown/bounds_not_honored on every later call, with no provider request for it
    assert all(s[:2] == ("unknown", "bounds_not_honored") for s in after), states
    assert all(s[2] == 0 for s in after), states
    # history progresses meanwhile: all 8 prior sessions certified
    assert r["complete_prior_sessions"] == 8, states


def test_an_incomplete_scan_walk_never_replaces_a_backed_off_unknown_with_partial(tmp_path):
    """Requirement 3 at the collector: a walk of a backed-off session that runs out of budget before reaching
    a proven edge (here: a Refresh-sized 2-page walk) leaves `unknown` and its backoff clock untouched."""
    uw = world(0)
    cat, client = IngestionCatalog(str(tmp_path / "v10b.db")), uw.adapter(["ABCD"]).client
    sc.update_session_coverage(client, cat, "darkpool", "ABCD", EVIDENCE, now_utc=AS_OF, max_pages=20,
                               settle=False)
    first = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert first["status"] == "unknown" and first["last_error"] == "bounds_not_honored"
    uw.healed = True                                     # even a clean provider: 2 pages cannot reach the open
    u = sc.update_session_coverage(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=2, settle=False,
                                   now_utc=AS_OF + timedelta(minutes=5))
    after = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert u.pages <= 2
    assert (after["status"], after["last_error"]) == ("unknown", "bounds_not_honored"), after
    assert after["retrieved_at"] == first["retrieved_at"]                  # the backoff clock is not reset


def test_a_session_that_recovers_legitimately_moves_to_a_verified_state(tmp_path, monkeypatch):
    """Requirement 6: after the backoff expires (shortened here) and the provider serves clean pages again,
    the catch-up walks it to the open: partial / session_in_progress, covered from the open."""
    monkeypatch.setattr(sc, "REJECTED_RETRY_AFTER", timedelta(minutes=30))
    uw = world(2)
    cat, client = IngestionCatalog(str(tmp_path / "v10c.db")), uw.adapter(["ABCD"]).client
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=2,
                         now_utc=AS_OF)
    assert cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())["status"] == "unknown"
    # the catalog stamps retrieved_at with the wall clock; align it with this test's pinned clock (in production
    # both are the same clock), so the backoff expires 30 minutes after the rejection as it would live
    with cat._conn() as conn:
        conn.execute("UPDATE session_coverage SET retrieved_at = ? WHERE ticker = 'ABCD' AND dataset = 'darkpool' "
                     "AND session_date = ?", (AS_OF.isoformat(), EVIDENCE.isoformat()))
        conn.commit()
    assert cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())["retrieved_at"] == AS_OF.isoformat()
    uw.healed = True
    before = len(uw.calls)
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=2,
                         now_utc=AS_OF + timedelta(minutes=10))            # still backed off: no request
    assert ev_dp(uw, before) == []
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=2,
                         now_utc=AS_OF + timedelta(minutes=40))            # backoff expired: catch-up
    ev = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert ev["status"] == "partial" and ev["last_error"] == "session_in_progress", ev
    assert sc._evidence_from_open(ev, EVIDENCE)


def test_a_valid_from_open_evidence_session_still_extends_with_the_leftover(tmp_path):
    rows = dense(EVIDENCE, end=time(15, 0))
    for d in prior_sessions(EVIDENCE, 2):
        rows += session_prints("ABCD", d, 10_000.0)
    uw = PaginatedUW(darkpool=rows)
    cat, client = IngestionCatalog(str(tmp_path / "v10d.db")), uw.adapter(["ABCD"]).client
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=2,
                         now_utc=AS_OF)
    ev1 = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert sc._evidence_from_open(ev1, EVIDENCE)
    before = len(uw.calls)
    r = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=5, lookback_sessions=2,
                             now_utc=AS_OF + timedelta(minutes=30))
    ev2 = cat.get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert ev_dp(uw, before) and ev2["covered_through_utc"] > ev1["covered_through_utc"]
    assert sc._evidence_from_open(ev2, EVIDENCE) and r["pages_used"] <= 5


# --- the real job runner -------------------------------------------------------------------------------------
def test_a_market_hours_job_with_an_unprovable_evidence_session_finishes_history_within_its_cap(live, monkeypatch):
    state = {"now": AS_OF}

    def clock():
        state["now"] += timedelta(minutes=1)
        return state["now"]

    monkeypatch.setattr(live, "_utcnow", clock)
    dp, fl = dense(EVIDENCE), []
    for d in prior_sessions(EVIDENCE, 26):
        dp += session_prints("ABCD", d, 10_000.0)
        fl += session_alerts("ABCD", d, 50_000.0)
    fl += session_alerts("ABCD", EVIDENCE, 50_000.0, end=time(11, 55))
    uw = live.use_uw(DeepRejectUW(darkpool=dp, flow=fl, static={"stock-state": STATE}))
    job = run_job(live, TestClient(live.app), max_pages_per_ticker=400, step_pages=10)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "complete", (job["status"], entry)
    assert all(d["complete_prior_sessions"] >= 20 for d in entry["datasets"].values())
    ev = live.get_catalog().get_session_coverage("darkpool", "ABCD", EVIDENCE.isoformat())
    assert ev["status"] == "unknown"                                          # honestly unprovable
    # the evidence session was walked until its rejection (catch-up + one retry), never again
    assert len(ev_dp(uw)) <= 12, len(ev_dp(uw))
    assert len(provider_pages(uw)) == job["pages_used"] <= 400
