"""F-1 (contest-readiness finding): during market hours a baseline job spent its pages re-polling the
in-progress evidence session, so a job whose history could not progress (every prior session awaiting gap
confirmation) burned its whole confirmed budget, and a 1-page step did not even report the waiting sessions.

These tests drive the REAL routes, job runner and session collector against the verified-contract fake provider
(tests/uw_paginated.py), with a pinned clock. They assert bounded progress, never a looser certification rule.
Constructed data only."""
from datetime import datetime, time, timedelta, timezone

from fastapi.testclient import TestClient

from app import session_collection as sc
from app.core import trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, live, run_job  # noqa: F401 (fixture)
from tests.uw_paginated import NY, PaginatedUW, prior_sessions, session_alerts, session_prints

AFTER_CLOSE = datetime.combine(EVIDENCE, time(21, 0), tzinfo=NY).astimezone(timezone.utc)
assert AS_OF.astimezone(NY).time() == time(12, 0)          # the shared fixture clock is during market hours


def gapped_history_uw(history=22):
    """Every prior session has a genuine provider-side hole (activity 09:30-11:30, then one print at the
    close): the gap check rightly withholds each one for confirmation. The evidence session is normal."""
    dp, fl = [], []
    for d in prior_sessions(EVIDENCE, history):
        dp += session_prints("ABCD", d, 10_000.0, every_minutes=1, end=time(11, 30), tag="a")
        dp += session_prints("ABCD", d, 10.0, every_minutes=60, start=time(16, 0), end=time(16, 0), tag="z")
        fl += session_alerts("ABCD", d, 1_000.0, every_minutes=1, end=time(11, 30), tag="a")
        fl += session_alerts("ABCD", d, 10.0, every_minutes=60, start=time(15, 55), end=time(15, 55), tag="z")
    dp += session_prints("ABCD", EVIDENCE, 10_000.0)
    fl += session_alerts("ABCD", EVIDENCE, 1_000.0)
    return PaginatedUW(darkpool=dp, flow=fl, static={"stock-state": STATE})


def normal_uw(history=26):
    dp, fl = [], []
    for d in prior_sessions(EVIDENCE, history):
        dp += session_prints("ABCD", d, 10_000.0)
        fl += session_alerts("ABCD", d, 50_000.0)
    dp += session_prints("ABCD", EVIDENCE, 10_000.0)
    fl += session_alerts("ABCD", EVIDENCE, 50_000.0)
    return PaginatedUW(darkpool=dp, flow=fl, static={"stock-state": STATE})


def advancing_clock(live, monkeypatch, start=AS_OF, step=timedelta(minutes=1)):
    """The market keeps moving between job steps: every read of the app clock is one minute later, so the
    in-progress evidence session always has new prints to fetch (the condition behind F-1)."""
    state = {"now": start}

    def now():
        state["now"] = state["now"] + step
        return state["now"]

    monkeypatch.setattr(live, "_utcnow", now)
    return state


def evidence_calls(uw, start=0):
    return [p for u, p in uw.calls[start:] if ("/api/darkpool/" in u and p.get("date") == EVIDENCE.isoformat())
            or ("flow-alerts" in u and str(p.get("newer_than", "")).startswith(EVIDENCE.isoformat()))]


def provider_pages(uw, start=0):
    return [u for u, _ in uw.calls[start:] if "/api/darkpool/" in u or "flow-alerts" in u]


# --- the reported failure ---------------------------------------------------------------------------------
def test_market_hours_job_with_only_waiting_history_pauses_instead_of_burning_its_budget(live, monkeypatch):
    advancing_clock(live, monkeypatch)
    uw = live.use_uw(gapped_history_uw())
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "paused" and entry["state"] == "awaiting_gap_confirmation", (job["status"], entry)
    for ds in ("darkpool", "options_flow"):
        assert len(entry["datasets"][ds]["awaiting_gap_confirmation"]) == 22, ds
        assert entry["datasets"][ds]["complete_prior_sessions"] == 0        # nothing certified by force
    # every session of the lookback walked once (each fits in one page here; the 8 oldest are genuinely empty)
    # + the evidence catch-up + at most one evidence extension per step: far below the confirmed 400 pages
    lookback = sc.BACKFILL_LOOKBACK_SESSIONS
    assert job["pages_used"] <= 2 * lookback + 2 * 2 + 2 * entry["steps"], (job["pages_used"], entry["steps"])
    assert job["pages_used"] < 100
    assert len(provider_pages(uw)) == job["pages_used"]                     # accounting stays exact
    assert "can be confirmed from" in (job["last_error"] or "")


def test_market_hours_job_reaches_its_target_and_leaves_the_evidence_session_covered_from_the_open(live, monkeypatch):
    advancing_clock(live, monkeypatch)
    uw = live.use_uw(normal_uw())
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    entry = job["progress"]["ABCD"]
    assert job["status"] == "completed" and entry["state"] == "complete"
    open_utc = tc.session_info(EVIDENCE).session_open_et.astimezone(timezone.utc)
    for ds in ("darkpool", "options_flow"):
        assert entry["datasets"][ds]["complete_prior_sessions"] >= 20
        ev = live.get_catalog().get_session_coverage(ds, "ABCD", EVIDENCE.isoformat())
        assert ev and datetime.fromisoformat(ev["covered_from_utc"]) <= open_utc, ds
    # the evidence session is walked to the open once, then extended at most once per step and dataset
    assert len(evidence_calls(uw)) <= 2 * 2 + 2 * entry["steps"]
    assert len(provider_pages(uw)) == job["pages_used"] <= 400
    b = client.get("/api/ticker/ABCD/baseline").json()["datasets"]
    assert all(b[ds]["baseline"]["eligible_sessions"] >= 20 for ds in ("darkpool", "options_flow"))


def test_after_the_close_a_job_with_only_waiting_history_makes_no_evidence_polling(live, monkeypatch):
    monkeypatch.setattr(live, "_utcnow", lambda: AFTER_CLOSE)
    uw = live.use_uw(gapped_history_uw())
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    assert job["status"] == "paused"
    assert len(evidence_calls(uw)) <= 2 * 2                  # the one-time walk of the closed session only
    assert job["pages_used"] <= 2 * sc.BACKFILL_LOOKBACK_SESSIONS + 2 * 2


def test_resuming_a_waiting_job_early_costs_at_most_one_evidence_page_per_dataset(live, monkeypatch):
    advancing_clock(live, monkeypatch)
    uw = live.use_uw(gapped_history_uw())
    client = TestClient(live.app)
    job = run_job(live, client, max_pages_per_ticker=400)
    assert job["status"] == "paused"
    before, used_before = len(uw.calls), job["pages_used"]
    r = client.post(f"/api/baseline/jobs/{job['id']}/resume", json={"confirm": True})
    assert r.status_code == 200, r.text
    live.get_baseline_job_runner().join(60)
    job = live.get_baseline_job_store().get(job["id"])
    assert job["status"] == "paused" and job["progress"]["ABCD"]["state"] == "awaiting_gap_confirmation"
    spent = len(provider_pages(uw, before))
    assert spent == job["pages_used"] - used_before <= 2, spent


# --- the collector contract the job relies on -------------------------------------------------------------
def _cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "f1.db"))


def test_waiting_sessions_are_reported_even_when_the_budget_is_one_page(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)
    cat, uw = _cat(tmp_path), gapped_history_uw()
    client = uw.adapter(["ABCD"]).client
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=400, lookback_sessions=22,
                         now_utc=AS_OF)                         # first walk: every prior session suspected
    res = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=1, lookback_sessions=22,
                               now_utc=AS_OF + timedelta(minutes=5))
    assert len(res["awaiting_gap_confirmation"]) == 22
    assert res["prior_budget"] == 1 and res["prior_pages_used"] == 0      # history had pages and needed none
    assert res["pages_used"] <= 1                                          # the page left went to the evidence


def test_prior_sessions_get_the_budget_before_an_evidence_session_already_covered(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)
    cat, uw = _cat(tmp_path), normal_uw()
    client = uw.adapter(["ABCD"]).client
    first = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=3, lookback_sessions=26,
                                 now_utc=AS_OF)
    assert first["sessions_touched"][0]["session_date"] == EVIDENCE.isoformat()   # one-time catch-up first
    later = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=3, lookback_sessions=26,
                                 now_utc=AS_OF + timedelta(minutes=5))
    dates = [t["session_date"] for t in later["sessions_touched"]]
    assert dates[0] != EVIDENCE.isoformat() and later["prior_pages_used"] >= 1
    assert dates.count(EVIDENCE.isoformat()) <= 1                       # extended at most once, last


def test_a_provider_failure_stops_all_fetching_including_the_evidence_session(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)
    cat = _cat(tmp_path)
    uw = normal_uw()
    client = uw.adapter(["ABCD"]).client
    sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=3, lookback_sessions=26,
                         now_utc=AS_OF)                         # evidence covered from the open
    real_get = uw.get

    def failing(url, headers=None, params=None, timeout=None):
        if "/api/darkpool/" in url and (params or {}).get("date") != EVIDENCE.isoformat():
            from tests.uw_fakes import FakeResponse
            return FakeResponse(500, {"message": "provider error"})
        return real_get(url, headers, params, timeout)

    uw.get = failing
    before = len(uw.calls)
    res = sc.backfill_baseline(client, cat, "darkpool", "ABCD", EVIDENCE, max_pages=20, lookback_sessions=26,
                               now_utc=AS_OF + timedelta(minutes=5))
    assert res["sessions_touched"] and res["sessions_touched"][-1]["session_date"] != EVIDENCE.isoformat()
    assert not [p for _, p in uw.calls[before:] if p.get("date") == EVIDENCE.isoformat()]
