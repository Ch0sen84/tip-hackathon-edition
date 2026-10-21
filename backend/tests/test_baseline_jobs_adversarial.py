"""Deterministic generative (seeded) tests of baseline JOBS end to end through
the real routes, runner, allocation and collector, against the verified-
contract fake with injected transport failures and step exceptions.

  C  provider logical requests never exceed max_pages_per_ticker, also across
     step exceptions, pauses, restarts and resumes; a cap below 1 is refused
  D  HTTP attempts (incl. transport retries) are reported apart from pages
  G  eligibility == complete prior sessions, each fully stored
  H  after a restart nothing runs until an explicit resume
  I  another ticker's rows and coverage are untouched
  J  no evaluation / transition / lifecycle row, unchanged policy constants
Constructed data only. A failing seed goes to REGRESSION_SEEDS."""
import hashlib
import random

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient
from app.core import dark_pool_baseline, session_baseline
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, live  # noqa: F401 (fixture)
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import PaginatedUW, prior_sessions, session_alerts, session_prints

SEEDS = range(0, 40)
REGRESSION_SEEDS = []


class JobUW(PaginatedUW):
    """Transport-level 503s on some first attempts (the client retries them)."""

    def __init__(self, *a, rng, p503, **k):
        super().__init__(*a, **k)
        self.rng, self.p503, self.attempts, self._retry = rng, p503, 0, None

    def get(self, url, headers=None, params=None, timeout=None):
        if "/api/darkpool/" in url or "flow-alerts" in url:
            self.attempts += 1
            req = (url, tuple(sorted((params or {}).items())))
            if self._retry != req and self.rng.random() < self.p503:
                self._retry = req
                return FakeResponse(503, {"error": "upstream"})
            self._retry = None
        return super().get(url, headers, params, timeout)

    def adapter(self, watchlist=None):
        client = UWRestClient("fake-test-key", base_url="http://127.0.0.1:9", session=self, max_retries=1,
                              sleep_fn=lambda _s: None, circuit_breaker_threshold=10**6)
        return LiveUWAdapter("fake-test-key", client=client, watchlist=watchlist or [])


def build(rng, history, flow_heavy):
    dp, fl = [], []
    for d in prior_sessions(EVIDENCE, history):
        dp += session_prints("ABCD", d, 10_000.0, every_minutes=rng.choice([1, 5]))
        if flow_heavy:
            fl += session_alerts("ABCD", d, 1_000.0, every_minutes=rng.choice([1, 15]))
    dp += session_prints("ABCD", EVIDENCE, 10_000.0)
    static = {"stock-state": STATE}
    if not flow_heavy:
        static["flow-alerts"] = FakeResponse(200, {"data": []})       # one-dataset regime: flow trivially complete
    return JobUW(darkpool=dp, flow=fl, static=static, rng=rng, p503=rng.choice([0.0, 0.0, 0.1]))


def logical(uw):
    return sum(v["logical_requests"] for v in uw.client_ref.telemetry.summary().values())


def fingerprint(cat, ticker):
    h = hashlib.sha256()
    with cat._conn() as c:
        for tb in ("darkpool_observations", "options_flow_observations"):
            for r in c.execute(f"SELECT source_id, event_time, raw_json FROM {tb} WHERE ticker = ? ORDER BY id", (ticker,)):
                h.update(repr(r).encode())
        for r in c.execute("SELECT * FROM session_coverage WHERE ticker = ? ORDER BY id", (ticker,)):
            h.update(repr(r).encode())
    return h.hexdigest()


@pytest.mark.parametrize("seed", list(SEEDS) + REGRESSION_SEEDS)
def test_generative_jobs_respect_budget_isolation_and_policy(live, monkeypatch, seed):
    monkeypatch.setattr(sc, "_sleep", lambda _s: None)
    rng = random.Random(424_242 + seed)
    history = rng.choice([19, 20, 21, 26])
    uw = build(rng, history, flow_heavy=rng.random() < 0.6)
    live.use_uw(uw)
    uw.client_ref = live.get_adapter().client
    cat = live.get_catalog()
    other = session_prints("OTHR", prior_sessions(EVIDENCE, 1)[0], 5.0)          # I: an unrelated ticker
    cat.catalog_darkpool_observations("OTHR", other)
    cat.upsert_session_coverage("darkpool", "OTHR", prior_sessions(EVIDENCE, 1)[0].isoformat(), "partial",
                                None, None, 1, len(other), "page_budget_exhausted")
    fp_other = fingerprint(cat, "OTHR")
    client = TestClient(live.app)
    cap, step = rng.randint(0, 25), rng.choice([10, 11, 20])
    tag = f"seed={seed} cap={cap} step={step} history={history}"

    r = client.post("/api/baseline/jobs", json={"confirm": True, "tickers": ["ABCD"], "max_pages_per_ticker": cap, "step_pages": step})
    if cap < 1:
        assert r.status_code == 400 and logical(uw) == 0, tag
        return
    job_id = r.json()["job"]["id"]

    # step exceptions after provider requests (storage refusing a checkpoint write)
    real_up = cat.upsert_session_coverage

    def flaky(*a, **k):
        if rng.random() < 0.08:
            raise RuntimeError("storage guard: refusing write")
        return real_up(*a, **k)
    monkeypatch.setattr(cat, "upsert_session_coverage", flaky)
    store = live.get_baseline_job_store()
    for attempt in range(12):
        live.get_baseline_job_runner().join(60)
        j = store.get(job_id)
        assert logical(uw) <= cap, f"C provider pages {logical(uw)} > cap [{tag}]"
        assert j["pages_used"] <= cap, f"C persisted pages_used > cap [{tag}]"
        if j["status"] != "paused":
            break
        if rng.random() < 0.5:                                                     # H: a restart while paused
            before = logical(uw)
            store.recover_after_restart()
            assert store.get(job_id)["status"] == "paused" and logical(uw) == before, f"H ran after restart [{tag}]"
        assert client.post(f"/api/baseline/jobs/{job_id}/resume", json={"confirm": True}).status_code == 200, tag
    live.get_baseline_job_runner().join(60)                 # never assert while the job thread still runs
    assert not live.get_baseline_job_runner().is_running(), f"job still running [{tag}]"
    monkeypatch.setattr(cat, "upsert_session_coverage", real_up)
    j = store.get(job_id)
    entry = j["progress"]["ABCD"]
    assert logical(uw) <= cap and j["pages_used"] <= cap, f"C [{tag}]"
    # D: HTTP attempts reported by the job == attempts the provider actually saw (incl. transport
    # retries and steps that raised), and never fewer than logical pages
    assert entry.get("http_attempts", 0) == uw.attempts, f"D attempts {entry.get('http_attempts')} != {uw.attempts} [{tag}]"
    assert uw.attempts >= logical(uw), f"D [{tag}]"

    # G: eligibility counts exactly the complete prior sessions, each fully stored
    for ds in ("darkpool", "options_flow"):
        b = session_baseline.compute(cat, ds, "ABCD", EVIDENCE, as_of_utc=AS_OF)
        complete = [c for c in cat.list_session_coverage(ds, "ABCD")
                    if c["status"] == "complete" and c["session_date"] < EVIDENCE.isoformat()]
        # verified-empty sessions before the first observed activity are excluded by existing policy
        assert b.eligible_sessions + b.excluded_before_first_activity == min(
            len(complete), session_baseline.MAX_HISTORY_SESSIONS), f"G [{tag}]"
        src = uw.darkpool.get("ABCD", []) if ds == "darkpool" else uw.flow.get("ABCD", [])
        f = sc.DATASETS[ds].time_field
        for c in complete:
            from datetime import datetime, time, timezone
            from app.core import trading_calendar as tc
            info = tc.session_info(datetime.fromisoformat(c["session_date"]).date())
            o, cl = info.session_open_et.astimezone(timezone.utc), info.session_close_et.astimezone(timezone.utc)
            want = {r["tracking_id"] if ds == "darkpool" else r["id"] for r in src
                    if o <= datetime.fromisoformat(r[f].replace("Z", "+00:00")) <= cl}
            got = {r.get("tracking_id") if ds == "darkpool" else r.get("id") for r in cat.observations_between(ds, "ABCD", o, cl)}
            assert want <= got, f"G complete session {c['session_date']} missing {len(want - got)} [{tag}]"
    assert fingerprint(cat, "OTHR") == fp_other, f"I unrelated ticker modified [{tag}]"
    with cat._conn() as c:
        for table in ("signal_evaluations", "signal_transitions", "lifecycle_episodes", "lifecycle_scans",
                      "lifecycle_session_evidence"):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, f"J {table} [{tag}]"
    assert dark_pool_baseline.BASELINE_READY_MIN == 20 and sc.BACKFILL_LOOKBACK_SESSIONS == 30


@pytest.mark.parametrize("cap", range(1, 26))
@pytest.mark.parametrize("both_unfinished", [True, False])
def test_resume_with_one_page_left_never_overspends(live, cap, both_unfinished):
    """Resume with exactly one page remaining, one or two datasets unfinished."""
    uw = build(random.Random(cap), 26, flow_heavy=True)
    live.use_uw(uw)
    uw.client_ref = live.get_adapter().client
    store = live.get_baseline_job_store()
    job = store.create(["ABCD"], cap, 10, requested_by="user")
    progress = job["progress"]
    progress["ABCD"].update({"pages_used": cap - 1, "steps": 1, "datasets": {
        "darkpool": {"complete_prior_sessions": 3 if both_unfinished else 20, "history_cutoff": False},
        "options_flow": {"complete_prior_sessions": 2, "history_cutoff": False}}})
    store.update(job["id"], progress=progress, pages_used=cap - 1)
    store.recover_after_restart()
    assert logical(uw) == 0
    client = TestClient(live.app)
    assert client.post(f"/api/baseline/jobs/{job['id']}/resume", json={"confirm": True}).status_code == 200
    live.get_baseline_job_runner().join(60)
    j = store.get(job["id"])
    assert logical(uw) == 1 and j["pages_used"] == cap


def test_inconsistent_persisted_progress_pauses_without_a_request(live):
    uw = build(random.Random(1), 26, flow_heavy=True)
    live.use_uw(uw)
    uw.client_ref = live.get_adapter().client
    store = live.get_baseline_job_store()
    for bad in (-3, "7", None, True):
        job = store.create(["ABCD"], 20, 10, requested_by="user")
        progress = job["progress"]
        progress["ABCD"]["pages_used"] = bad
        store.update(job["id"], progress=progress)
        live.get_baseline_job_runner().start(job["id"])
        live.get_baseline_job_runner().join(30)
        j = store.get(job["id"])
        assert j["status"] == "paused" and "inconsistent" in (j["last_error"] or ""), (bad, j)
        store.transition(job["id"], ("paused",), "cancelled")
    assert logical(uw) == 0
