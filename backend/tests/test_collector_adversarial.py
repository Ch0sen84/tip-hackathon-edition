"""Deterministic generative (seeded) adversarial tests for historical collection.

A seeded provider model serves a session's dark-pool or flow history with the
verified pagination contract, then corrupts chosen responses: out-of-bounds
rows, rows at the exclusive cursor, malformed rows (non-object, foreign ticker,
missing / unparseable timestamp), out-of-order pages, transient HTTP 503,
429 + Retry-After, persistent 500. Walks run with random page budgets 0..25,
are interrupted (storage failures, a "crash" before the checkpoint write) and
resumed. After EVERY call the hard invariants are checked against an oracle:

  A  a session marked complete holds every provider row in [open, close]
  B  no corrupted / injected row is ever stored; a partial checkpoint's
     interval [covered_from, covered_through] is fully stored
  C  a call never uses more logical pages than its budget
  D  HTTP attempts are measured apart from logical pages
  E  (via A/B) no boundary row is skipped
  F  no duplicate observations
  G  (via A) unknown/partial never becomes complete without full evidence

A failing seed is reported in the assertion message; keep it in
REGRESSION_SEEDS so it is replayed forever. Constructed data only."""
import random
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.adapters.live_uw_adapter import LiveUWAdapter, UWRestClient
from app.ingestion import IngestionCatalog, darkpool_print_key
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, PaginatedUW, _ts, iso

DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
MARK = "INJECTED-ADVERSARIAL"
SEEDS = range(0, 60)
REGRESSION_SEEDS = []          # seeds that once found a defect are replayed here forever

CONTENT_FAULTS = ("above_cursor", "below_lower", "at_cursor", "non_object", "foreign_ticker", "bad_ts",
                  "missing_ts", "out_of_order", "ignore_upper")
HTTP_FAULTS = ("http_503_once", "http_429_retry_after", "http_500_persistent")


def gen_rows(rng, dataset, ticker="ABCD"):
    """Session history: varied same-timestamp multiplicity (dense boundaries,
    occasionally more than a whole page in one second), plus pre-market and
    after-hours rows that must never count, and a print exactly AT the close."""
    out, t, i = [], OPEN, 0
    limit = sc.DATASETS[dataset].page_limit
    target = rng.choice([0, 3, 120, 499, 500, 501, 1500, 2600])
    burst_at = rng.random() < 0.08
    while len(out) < target and t < CLOSE:
        k = rng.choice([1, 1, 2, 3, 7, 31]) if not (burst_at and len(out) > target // 2 and i % 97 == 0) else limit + 5
        for _ in range(k):
            out.append(make_row(dataset, ticker, t, i))
            i += 1
        t += timedelta(seconds=rng.choice([1, 2, 5, 40]), microseconds=0 if dataset == "darkpool" else rng.randrange(1, 999999))
    out.append(make_row(dataset, ticker, CLOSE, i)); i += 1                       # exactly at the close
    for extra in (OPEN - timedelta(minutes=30), CLOSE + timedelta(minutes=5)):     # outside the session
        out.append(make_row(dataset, ticker, extra, i)); i += 1
    return out


def make_row(dataset, ticker, t, i):
    if dataset == "darkpool":
        return {"ticker": ticker, "executed_at": iso(t), "price": "100.00", "size": 100 + i % 7, "premium": "1.00",
                "tracking_id": f"t{i}", "volume": i, "canceled": False}
    return {"id": f"a{i}", "ticker": ticker, "created_at": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": "call", "total_premium": 1, "total_ask_side_prem": 1, "total_bid_side_prem": 0}


def oracle(rows, dataset):
    f = sc.DATASETS[dataset].time_field
    return {key(dataset, r) for r in rows if OPEN <= _ts(r[f]) <= CLOSE}


def key(dataset, r):
    return darkpool_print_key("ABCD", r) if dataset == "darkpool" else r["id"]


class AdversarialUW(PaginatedUW):
    def __init__(self, rows, dataset, rng, p_fault):
        super().__init__(darkpool=rows if dataset == "darkpool" else None, flow=rows if dataset == "options_flow" else None)
        self.dataset, self.rng, self.p_fault = dataset, rng, p_fault
        self.attempts = 0
        self._pending_http = None

    def get(self, url, headers=None, params=None, timeout=None):
        if "/api/darkpool/" not in url and "flow-alerts" not in url:
            return super().get(url, headers, params, timeout)
        self.attempts += 1
        req = (url, tuple(sorted((params or {}).items())))
        if self._pending_http and self._pending_http[0] == req:      # the client's transport retry of the same request
            fault, self._pending_http = self._pending_http[1], None
            if fault == "http_500_persistent":
                return FakeResponse(500, {"error": "down"})              # the retry fails too: logical failure
            return super().get(url, headers, params, timeout)          # transient: the retry succeeds
        self._pending_http = None
        if self.rng.random() < self.p_fault:
            fault = self.rng.choice(CONTENT_FAULTS + HTTP_FAULTS)
            if fault in HTTP_FAULTS:
                self._pending_http = (req, fault)
                if fault == "http_429_retry_after":
                    return FakeResponse(429, {"error": "slow down"}, headers={"Retry-After": "2"})
                return FakeResponse(500 if fault == "http_500_persistent" else 503, {"error": "upstream"})
            return self._corrupt(fault, super().get(url, headers, params, timeout), params)
        return super().get(url, headers, params, timeout)

    def _corrupt(self, fault, resp, params):
        data = list(resp.json()["data"])
        f = sc.DATASETS[self.dataset].time_field
        hi, lo = _ts(params["older_than"]), _ts(params["newer_than"])
        limit = int(params["limit"])

        def bad(ts=None, **over):
            r = make_row(self.dataset, "ABCD", ts or (hi - timedelta(seconds=1)), 10**9)
            r.update({"tracking_id": MARK} if self.dataset == "darkpool" else {"id": MARK})
            r.update(over)
            return r
        put = (lambda r: [r] + data[:-1]) if len(data) >= limit else (lambda r: data + [r])
        if fault == "above_cursor":
            data = put(bad(hi + timedelta(seconds=30)))
        elif fault == "below_lower":
            data = put(bad(lo - timedelta(minutes=10)))
        elif fault == "at_cursor":
            # a row exactly at an EXCLUSIVE cursor is out of bounds; at an inclusive one it is legitimate
            data = put(bad(hi if not sc.DATASETS[self.dataset].upper_inclusive else hi + timedelta(microseconds=1)))
        elif fault == "non_object":
            data = put(["ABCD", iso(hi), 1])
        elif fault == "foreign_ticker":
            data = put(bad(ticker="WXYZ"))
        elif fault == "bad_ts":
            data = put(bad(**{f: "not-a-time"}))
        elif fault == "missing_ts":
            r = bad(); r.pop(f); data = put(r)
        elif fault == "out_of_order":
            if len(data) < 2 or _ts(data[0][f]) == _ts(data[-1][f]):
                data = put(bad(hi + timedelta(seconds=30)))          # cannot reorder a single timestamp: inject instead
            else:
                data = list(reversed(data))
        elif fault == "ignore_upper":
            rows = self.darkpool.get("ABCD", []) if self.dataset == "darkpool" else self.flow.get("ABCD", [])
            if self.dataset == "darkpool":
                rows = [r for r in rows if _ts(r[f]).astimezone(NY).date().isoformat() == params["date"]]
            fresh = self._page(rows, f, {"newer_than": params["newer_than"], "limit": limit})
            data = fresh if fresh and fresh != data else put(bad(hi + timedelta(seconds=30)))
        return FakeResponse(200, {"data": data})

    def adapter(self, watchlist=None):
        self.slept = []
        client = UWRestClient("fake-test-key", base_url="http://127.0.0.1:9", session=self, max_retries=1,
                              sleep_fn=self.slept.append, circuit_breaker_threshold=10**6)
        return LiveUWAdapter("fake-test-key", client=client, watchlist=watchlist or [])


def stored(cat, dataset):
    lo, hi = OPEN - timedelta(days=1), CLOSE + timedelta(days=1)
    tb = "darkpool_observations" if dataset == "darkpool" else "options_flow_observations"
    with cat._conn() as c:
        raws = [r[0] for r in c.execute(f"SELECT raw_json FROM {tb}")]
        dup = c.execute(f"SELECT COUNT(*) - COUNT(DISTINCT source_id) FROM {tb}").fetchone()[0]
    keys = {key(dataset, r) for r in cat.observations_between(dataset, "ABCD", lo, hi)}
    return keys, raws, dup


def check(seed, step, dataset, cat, rows, u, budget, logical_before, logical_after):
    tag = f"seed={seed} step={step} dataset={dataset} budget={budget}"
    keys, raws, dup = stored(cat, dataset)
    cov = cat.get_session_coverage(dataset, "ABCD", DAY.isoformat()) or {}
    assert dup == 0, f"F duplicate observations [{tag}]"
    assert not any(MARK in r or "WXYZ" in r or "not-a-time" in r for r in raws), f"B corrupted row stored [{tag}]"
    assert u.pages <= max(budget, 0), f"C pages {u.pages} > budget [{tag}]"
    assert logical_after - logical_before == u.pages, f"C/D logical requests != pages [{tag}]"
    f = sc.DATASETS[dataset].time_field
    if cov.get("status") == "complete":
        missing = oracle(rows, dataset) - keys
        assert not missing, f"A complete session missing {len(missing)} rows [{tag}]"
    if cov.get("status") in ("complete", "partial") and cov.get("covered_from_utc"):
        frm, thr = _ts(cov["covered_from_utc"]), _ts(cov["covered_through_utc"])
        # dark pool (exclusive older_than, 1 s resolution): the island starts one second above the last
        # page's oldest second, so it is proven INCLUSIVE of covered_from. Flow (inclusive older_than):
        # rows exactly AT covered_from may be cut by the page limit; the resumed window re-reads that
        # instant (inclusive), so only (covered_from, covered_through] is proven.
        exclusive_lo = sc.DATASETS[dataset].upper_inclusive and frm > OPEN
        need = {key(dataset, r) for r in rows if (frm < _ts(r[f]) if exclusive_lo else frm <= _ts(r[f]))
                and _ts(r[f]) <= thr and OPEN <= _ts(r[f]) <= CLOSE}
        assert not (need - keys), f"B checkpoint [{frm}, {thr}] not fully stored ({len(need - keys)} missing) [{tag}]"


@pytest.mark.parametrize("dataset", ["darkpool", "options_flow"])
@pytest.mark.parametrize("seed", list(SEEDS) + REGRESSION_SEEDS)
def test_generative_walks_hold_the_coverage_invariants(tmp_path, monkeypatch, dataset, seed):
    monkeypatch.setattr(sc, "_sleep", lambda _s: None)
    rng = random.Random(seed * 7919 + (0 if dataset == "darkpool" else 1))
    monkeypatch.setattr(sc, "BOUNDS_RETRIES_PER_WINDOW", rng.choice([0, 1, 1, 2]))
    rows = gen_rows(rng, dataset)
    cat = IngestionCatalog(str(tmp_path / "adv.db"))
    uw = AdversarialUW(rows, dataset, rng, p_fault=rng.choice([0.0, 0.05, 0.2, 0.5]))
    # storage failures: the catalog write or the checkpoint write raises now and then
    real_cat = cat.catalog_darkpool_observations if dataset == "darkpool" else cat.catalog_options_flow_observations
    real_up = cat.upsert_session_coverage
    real_diag = cat.record_collection_diagnostic

    def flaky_catalog(*a, **k):
        if rng.random() < 0.03:
            raise RuntimeError("storage guard")
        return real_cat(*a, **k)

    def flaky_upsert(*a, **k):
        if rng.random() < 0.03:
            raise RuntimeError("crash before checkpoint write")
        return real_up(*a, **k)

    def flaky_diag(*a, **k):
        if rng.random() < 0.3:
            raise RuntimeError("diagnostic store unavailable")
        return real_diag(*a, **k)

    monkeypatch.setattr(cat, "catalog_darkpool_observations" if dataset == "darkpool" else
                        "catalog_options_flow_observations", flaky_catalog)
    monkeypatch.setattr(cat, "upsert_session_coverage", flaky_upsert)
    monkeypatch.setattr(cat, "record_collection_diagnostic", flaky_diag)
    for step in range(rng.randint(1, 14)):
        budget = rng.randint(0, 25)
        client = uw.adapter().client                       # a fresh client per call ~ a restart
        before = sum(v["logical_requests"] for v in client.telemetry.summary().values())
        try:
            u = sc.update_session_coverage(client, cat, dataset, "ABCD", DAY, now_utc=NOW, max_pages=budget)
        except RuntimeError as exc:                         # "crash" between page writes and checkpoint
            assert "checkpoint" in str(exc)
            continue
        after = sum(v["logical_requests"] for v in client.telemetry.summary().values())
        check(seed, step, dataset, cat, rows, u, budget, before, after)
        attempts = sum(v["http_attempts"] for v in client.telemetry.summary().values())
        assert attempts >= after - before, f"D attempts < logical [seed={seed}]"
        if u.coverage and u.coverage.get("status") == "unknown" and u.coverage.get("last_error") in sc.REJECTED_CONTENT_REASONS:
            break                                           # backfills never re-walk such a session


@pytest.mark.parametrize("dataset", ["darkpool", "options_flow"])
@pytest.mark.parametrize("seed", range(0, 40))
def test_generative_clean_provider_always_completes_exactly(tmp_path, monkeypatch, dataset, seed):
    """No faults: any sequence of budgets 1..25 eventually completes the session
    with exactly the provider's rows (no skip at any interruption point), unless
    one timestamp holds more than a page (then never complete)."""
    rng = random.Random(10_000 + seed * 31 + (0 if dataset == "darkpool" else 1))
    rows = gen_rows(rng, dataset)
    cat = IngestionCatalog(str(tmp_path / "clean.db"))
    uw = AdversarialUW(rows, dataset, rng, p_fault=0.0)
    f = sc.DATASETS[dataset].time_field
    counts = {}
    for r in rows:
        counts[_ts(r[f])] = counts.get(_ts(r[f]), 0) + 1
    over_page = max(counts.values()) >= sc.DATASETS[dataset].page_limit
    for step in range(200):
        budget = rng.randint(1, 25)
        u = sc.update_session_coverage(uw.adapter().client, cat, dataset, "ABCD", DAY, now_utc=NOW, max_pages=budget)
        assert u.pages <= budget
        st = (u.coverage or {}).get("status")
        if st in ("complete", "unknown"):
            break
    keys, _, dup = stored(cat, dataset)
    assert dup == 0
    if over_page:
        assert st != "complete", f"seed={seed}: a >page same-timestamp burst must never certify complete"
    else:
        assert st == "complete", f"seed={seed}: clean provider did not complete ({u.coverage})"
        assert keys == oracle(rows, dataset), f"seed={seed}: stored != provider rows in [open, close]"
