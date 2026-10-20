"""Independent-review blockers on the collector reliability candidate (d520e83).
Each test first reproduces the reviewer's exact scenario, then pins the
corrected behaviour. Constructed data and disposable databases only.

B1  whole-page validation: a page with ANY invalid row (non-object,
    foreign ticker, missing/unparseable timestamp, out of bounds) is rejected
    whole -- nothing stored, never "exhausted" -- and goes through the same
    diagnostic + bounded-retry path; persistent rejection is unknown.
B2  the bounds retry allowance is per logical page: it resets only when an
    accepted page advances the cursor, so two separate transient failures in
    one walk are both recoverable.
B3  page budget: the per-dataset split of a step never exceeds the remaining
    logical budget (quotient/remainder), for jobs, resumed jobs and the
    explicit backfill route; HTTP attempts are reported apart from pages."""
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import session_collection as sc
from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient
from app.ingestion import IngestionCatalog
from tests.test_baseline_jobs import AS_OF, EVIDENCE, STATE, build_uw, live  # noqa: F401 (fixture)
from tests.test_collector_bounds_reliability import (DAY, INJECTED, FaultyUW, cover, dense_day, diags,
                                                     expected_keys, injected_stored, stored_keys)
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, PaginatedUW, _ts, iso, prior_sessions, session_alerts, session_prints


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "blockers.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda _s: None)


def valid_print(t, i):
    return {"ticker": "ABCD", "executed_at": iso(t), "price": "100.00", "size": 100, "premium": "10000.00",
            "tracking_id": f"v{i}", "volume": i, "canceled": False}


MALFORMED = {
    "unparseable_timestamp": lambda t: {"ticker": "ABCD", "executed_at": "not-a-time", "price": "1", "size": 1,
                                        "premium": "1", "tracking_id": INJECTED},
    "missing_timestamp": lambda t: {"ticker": "ABCD", "price": "1", "size": 1, "premium": "1",
                                    "tracking_id": INJECTED},
    "foreign_ticker": lambda t: {**valid_print(t, 0), "ticker": "WXYZ", "tracking_id": INJECTED},
    "non_object": lambda t: ["ABCD", iso(t), "1"],
}


class MalformedUW(PaginatedUW):
    """Appends one malformed row to the n-th dark-pool response (or every one
    from `persistent_from`)."""

    def __init__(self, *a, kind, at=(), persistent_from=None, position="end", **k):
        super().__init__(*a, **k)
        self.kind, self.at, self.persistent_from, self.position, self.dp_calls = (
            kind, set(at), persistent_from, position, 0)

    def get(self, url, headers=None, params=None, timeout=None):
        resp = super().get(url, headers, params, timeout)
        if "/api/darkpool/" not in url or resp.status_code != 200:
            return resp
        self.dp_calls += 1
        if self.dp_calls not in self.at and not (self.persistent_from and self.dp_calls >= self.persistent_from):
            return resp
        data = list(resp.json()["data"])
        t = _ts(params["older_than"]) - timedelta(seconds=2)
        bad = MALFORMED[self.kind](t)
        if self.position == "end":
            data = (data[:-1] if len(data) >= int(params["limit"]) else data) + [bad]
        else:
            data = [bad] + data[:-1]
        return FakeResponse(200, {"data": data})


# --- B1: whole-page validation ---------------------------------------------------

def test_b1_reviewer_repro_short_page_with_an_unparseable_row_never_exhausts(cat, monkeypatch):
    """Reviewer: valid + unparseable row on a SHORT page gave outcome=exhausted,
    requests=1, stored=1. Corrected: rejected whole, nothing stored, unknown."""
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    t = datetime.combine(DAY, time(12, 0), tzinfo=NY).astimezone(timezone.utc)
    uw = PaginatedUW(static={})
    page = [valid_print(t, 1), MALFORMED["unparseable_timestamp"](t)]
    uw.get = lambda url, headers=None, params=None, timeout=None: (
        uw.calls.append((url, params)) or FakeResponse(200, {"data": page}))
    spec = sc.DATASETS["darkpool"]
    lo = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
    hi = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
    w = sc._fetch_window(spec, uw.adapter().client, cat, "ABCD", DAY, lo, hi, 10, "on_demand")
    assert w["outcome"] == "unverifiable" and w["reason"] == "invalid_rows"
    assert w["pages"] == 1 and w["rows"] == 0 and stored_keys(cat) == set()
    u = cover(uw, cat)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "invalid_rows"
    assert u.coverage["covered_from_utc"] is None and stored_keys(cat) == set()


@pytest.mark.parametrize("kind", sorted(MALFORMED))
@pytest.mark.parametrize("where", ["short_page", "full_page_after_valid_pages"])
def test_b1_any_invalid_row_rejects_the_whole_page(cat, monkeypatch, kind, where):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    if where == "short_page":           # one short page holding many valid prints + one bad row
        rows = session_prints("ABCD", DAY, 1.0, every_minutes=5)
        uw = MalformedUW(darkpool=rows, kind=kind, at={1})
    else:                               # pages 1-3 fine, page 4 (full) has a bad row after 499 valid ones
        rows = dense_day()
        uw = MalformedUW(darkpool=rows, kind=kind, at={4})
    u = cover(uw, cat)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "invalid_rows"
    assert u.coverage["covered_from_utc"] is None
    assert injected_stored(cat) == 0
    if where == "short_page":
        assert stored_keys(cat) == set()                          # no valid subset written either
    [d] = diags(cat)
    assert d["reason"] == "invalid_rows" and d["action"] == "session_left_unknown"
    key = {"unparseable_timestamp": "unparseable", "missing_timestamp": "unparseable",
           "foreign_ticker": "other_ticker", "non_object": "non_object"}[kind]
    assert d["violations"][key] == 1 and d["classification"] == "invalid_rows"
    raw = str(d)
    assert "WXYZ" not in raw and INJECTED not in raw                # no raw row content in diagnostics


@pytest.mark.parametrize("kind", ["unparseable_timestamp", "non_object"])
def test_b1_invalid_page_retry_recovers_and_persistent_failure_is_unknown(cat, kind):
    rows = dense_day()
    ok = cover(MalformedUW(darkpool=rows, kind=kind, at={3}), cat)
    assert ok.coverage["status"] == "complete" and ok.bounds_recovered == 1
    assert stored_keys(cat) == expected_keys(rows) and injected_stored(cat) == 0
    cat2 = IngestionCatalog(str(cat.db_path) + ".2")
    uw = MalformedUW(darkpool=rows, kind=kind, persistent_from=3)
    bad = cover(uw, cat2)
    assert bad.coverage["status"] == "unknown" and bad.coverage["last_error"] == "invalid_rows"
    assert uw.dp_calls == 2 + 1 + sc.BOUNDS_RETRIES_PER_WINDOW


def test_b1_invalid_short_page_after_valid_pages_never_establishes_completeness(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 0)
    rows = dense_day()
    clean = PaginatedUW(darkpool=rows)
    cover(clean, IngestionCatalog(str(cat.db_path) + ".ref"))
    last = len(clean.calls)                                        # the final (short) page of the walk
    assert last > 3
    u = cover(MalformedUW(darkpool=rows, kind="unparseable_timestamp", at={last}), cat)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "invalid_rows"
    assert u.coverage["covered_from_utc"] is None and injected_stored(cat) == 0


# --- B2: per-page retry allowance ------------------------------------------------------

def test_b2_reviewer_repro_two_separate_transient_failures_both_recover(cat):
    """Reviewer: bad A -> good retry A -> bad B: stopped at B because the
    allowance was walk-global. Corrected: each page gets its own allowance."""
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={3: "above_cursor", 6: "above_cursor"})
    u = cover(uw, cat)
    assert u.coverage["status"] == "complete"
    assert u.bounds_retries == 2 and u.bounds_recovered == 2
    assert stored_keys(cat) == expected_keys(rows) and injected_stored(cat) == 0
    assert uw.calls[2][1] == uw.calls[3][1] and uw.calls[5][1] == uw.calls[6][1]   # same windows re-requested
    assert [d["action"] for d in diags(cat)] == ["retry_same_window", "retry_same_window"]


def test_b2_persistent_failure_on_the_second_bad_page_is_bounded_and_unknown(cat, monkeypatch):
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", 2)
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={2: "above_cursor"}, persistent=(5, "above_cursor"))
    u = cover(uw, cat)
    # page1, bad2, ok2(retry), page3, bad4 x3 (1 + 2 retries) -> stop
    assert uw.dp_calls == 4 + 3
    assert u.coverage["status"] == "unknown" and u.bounds_retries == 3 and u.bounds_recovered == 1
    assert [d["attempt"] for d in diags(cat)] == [1, 1, 2, 3]


def test_b2_budget_exhaustion_during_recovery_is_never_complete(cat):
    rows = dense_day()
    uw = FaultyUW(darkpool=rows, faults={2: "above_cursor", 4: "above_cursor"})
    u = cover(uw, cat, pages=4)                                     # 4th request is the 2nd bad page; no budget left
    assert uw.dp_calls == 4 and u.pages == 4
    assert u.coverage["status"] == "unknown" and u.bounds_retries == 1 and u.bounds_recovered == 1
    assert diags(cat)[-1]["action"] == "session_left_unknown"


# --- B3: exact page budget --------------------------------------------------------------

def provider_calls(uw):
    return [u for u, _ in uw.calls if "/api/darkpool/" in u or "flow-alerts" in u]


def both_datasets_uw(history=26):
    dp, fl = [], []
    for d in prior_sessions(EVIDENCE, history):
        dp += session_prints("ABCD", d, 10_000.0)
        fl += session_alerts("ABCD", d, 1_000.0)
    return PaginatedUW(darkpool=dp, flow=fl, static={"stock-state": STATE})


def test_b3_allocation_is_exact_quotient_remainder():
    from app.main import _allocate_pages
    assert _allocate_pages(1, ("darkpool", "options_flow")) == {"darkpool": 1, "options_flow": 0}
    assert _allocate_pages(11, ("darkpool", "options_flow")) == {"darkpool": 6, "options_flow": 5}
    assert _allocate_pages(10, ("darkpool", "options_flow")) == {"darkpool": 5, "options_flow": 5}
    assert _allocate_pages(0, ("darkpool", "options_flow")) == {"darkpool": 0, "options_flow": 0}
    assert _allocate_pages(7, ("options_flow",)) == {"options_flow": 7}
    for n in range(0, 60):
        assert sum(_allocate_pages(n, ("darkpool", "options_flow")).values()) == n


@pytest.mark.parametrize("cap", [1, 2, 3, 11, 13, 21, 25])
def test_b3_reviewer_repro_a_job_never_exceeds_its_page_cap(live, cap):
    """Reviewer: cap 11 -> step 1 = 10, step 2 gave BOTH datasets 1 page -> 12."""
    uw = live.use_uw(both_datasets_uw())
    client = TestClient(live.app)
    job = client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": cap}).json()["job"]
    live.get_baseline_job_runner().join(60)
    j = live.get_baseline_job_store().get(job["id"])
    entry = j["progress"]["ABCD"]
    assert j["pages_used"] <= cap and entry["pages_used"] == j["pages_used"]
    assert len(provider_calls(uw)) == j["pages_used"]                # cumulative accounting exact
    assert entry["state"] == "budget_exhausted"


def test_b3_zero_budget_dataset_makes_no_request_but_keeps_its_status(live):
    uw = live.use_uw(both_datasets_uw())
    out = live._run_baseline_backfill(live.get_adapter(), "ABCD", 1, "on_demand")
    assert set(out) == {"darkpool", "options_flow"}
    assert out["darkpool"]["pages_allocated"] == 1 and out["options_flow"]["pages_allocated"] == 0
    assert out["options_flow"]["pages_used"] == 0 and out["options_flow"]["sessions_touched"] == []
    assert "complete_prior_sessions" in out["options_flow"]
    assert not [u for u, _ in uw.calls if "flow-alerts" in u] and len(provider_calls(uw)) == 1


def _resume_with(live, uw, datasets_state, remaining):
    store = live.get_baseline_job_store()
    job = store.create(["ABCD"], 100, 10, requested_by="user")
    progress = job["progress"]
    progress["ABCD"].update({"pages_used": 100 - remaining, "steps": 9, "datasets": datasets_state})
    store.update(job["id"], progress=progress, pages_used=100 - remaining)
    store.recover_after_restart()
    client = TestClient(live.app)
    assert client.post(f"/api/baseline/jobs/{job['id']}/resume").status_code == 200
    live.get_baseline_job_runner().join(60)
    return store.get(job["id"])


def test_b3_resume_with_one_page_left_and_both_datasets_unfinished(live):
    uw = live.use_uw(both_datasets_uw())
    st = {"darkpool": {"complete_prior_sessions": 3, "history_cutoff": False},
          "options_flow": {"complete_prior_sessions": 2, "history_cutoff": False}}
    j = _resume_with(live, uw, st, remaining=1)
    assert j["pages_used"] == 100 and len(provider_calls(uw)) == 1
    assert j["progress"]["ABCD"]["state"] == "budget_exhausted"


def test_b3_resume_with_one_page_left_and_only_one_dataset_unfinished(live):
    uw = live.use_uw(both_datasets_uw())
    st = {"darkpool": {"complete_prior_sessions": 20, "history_cutoff": False},
          "options_flow": {"complete_prior_sessions": 2, "history_cutoff": False}}
    j = _resume_with(live, uw, st, remaining=1)
    assert j["pages_used"] == 100 and len(provider_calls(uw)) == 1
    assert [u for u, _ in uw.calls if "flow-alerts" in u] and not [u for u, _ in uw.calls if "/api/darkpool/" in u]


def test_b3_explicit_backfill_route_with_max_pages_1_makes_one_request(live):
    uw = live.use_uw(both_datasets_uw())
    client = TestClient(live.app)
    r = client.post("/api/ticker/ABCD/baseline/backfill?max_pages=1")
    assert r.status_code == 200 and r.json()["max_pages"] == 1
    assert len(provider_calls(uw)) == 1
    bf = r.json()["backfill"]
    assert sum(d["pages_used"] for d in bf.values()) == 1
    assert sum(d["pages_allocated"] for d in bf.values()) == 1


class FlakyTransportUW(PaginatedUW):
    """The first dark-pool HTTP attempt answers 503; the client retries it at
    the transport layer (one logical page, two HTTP attempts)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.failed_once = False

    def get(self, url, headers=None, params=None, timeout=None):
        if "/api/darkpool/" in url and not self.failed_once:
            self.failed_once = True
            self.calls.append((url, dict(params or {})))
            return FakeResponse(503, {"error": "upstream"})
        return super().get(url, headers, params, timeout)

    def adapter(self, watchlist=None):
        client = UWRestClient("fake-test-key", base_url="http://127.0.0.1:9", session=self, max_retries=2,
                              sleep_fn=lambda _s: None, circuit_breaker_threshold=1000)
        return LiveUWAdapter("fake-test-key", client=client, watchlist=watchlist or [])


def test_b3_logical_pages_and_http_attempts_are_reported_separately(live):
    dp = []
    for d in prior_sessions(EVIDENCE, 26):
        dp += session_prints("ABCD", d, 10_000.0)
    uw = live.use_uw(FlakyTransportUW(darkpool=dp, static={"stock-state": STATE, "flow-alerts": FakeResponse(200, {"data": []})}))
    client = TestClient(live.app)
    job = client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 15}).json()["job"]
    live.get_baseline_job_runner().join(60)
    j = live.get_baseline_job_store().get(job["id"])
    entry = j["progress"]["ABCD"]
    assert j["pages_used"] <= 15
    assert entry["http_attempts"] == j["pages_used"] + 1            # one transport retry, not a logical page
    assert sum(d["http_attempts"] for d in entry["datasets"].values() if "http_attempts" in d) >= 1


def test_b3_cumulative_accounting_per_dataset_and_http_attempts(live):
    uw = live.use_uw(both_datasets_uw())
    client = TestClient(live.app)
    job = client.post("/api/baseline/jobs", json={"tickers": ["ABCD"], "step_pages": 10,
                                                  "max_pages_per_ticker": 37}).json()["job"]
    live.get_baseline_job_runner().join(60)
    j = live.get_baseline_job_store().get(job["id"])
    entry = j["progress"]["ABCD"]
    assert j["pages_used"] == 37 and entry["steps"] == 4
    assert sum(d["pages_used"] for d in entry["datasets"].values()) == 37
    assert entry["http_attempts"] == len(provider_calls(uw)) == 37      # no transport retries here


def test_b3_last_page_is_not_wasted_on_a_dataset_with_nothing_to_do(live, monkeypatch):
    """1 page left: the remainder goes to dark pool, which has no work (0 used).
    The job must not stop as 'no_more_work' -- the next step gives the page to
    options flow -- and the cap still holds."""
    live.use_uw(both_datasets_uw())
    calls = []

    def fake_step(ticker, pages, datasets=None):
        calls.append((pages, datasets))
        alloc = live._allocate_pages(pages, tuple(datasets or ("darkpool", "options_flow")))
        out = {}
        for ds, n in alloc.items():
            used = min(n, 1) if ds == "options_flow" else 0
            out[ds] = {"pages_used": used, "pages_allocated": n, "sessions_touched": [],
                       "complete_prior_sessions": 5, "http_attempts": used, "provider_requests": used}
        return out

    monkeypatch.setattr(live, "_baseline_job_step", fake_step)
    live._baseline_job_runner = None
    store = live.get_baseline_job_store()
    job = store.create(["ABCD"], 1, 10, requested_by="user")
    live.get_baseline_job_runner().start(job["id"])
    live.get_baseline_job_runner().join(30)
    j = store.get(job["id"])
    assert calls == [(1, None), (1, ["options_flow"])]
    assert j["pages_used"] == 1 and j["progress"]["ABCD"]["state"] == "budget_exhausted"
    assert j["progress"]["ABCD"]["datasets"]["darkpool"]["no_more_work"] is True
