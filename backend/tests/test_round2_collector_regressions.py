"""Round-2 collector review (C2-1..C2-8), converted from the reviewer's reproductions into regression
tests: each asserts the corrected behaviour. Kept verbatim where possible; adaptations are marked.
AUDIT2 (independent adversarial review of commit 49e241c, collector + historical integrity).

Every test here asserts the CORRECT / documented behaviour; a FAILING test reproduces a finding
(IDs C2-n in the audit report). Tests prefixed `test_sound_` are probes that PASS on sound code.
All market rows are CONSTRUCTED; no network (tests/uw_paginated fake only)."""
import random
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import session_collection as sc
from app.baseline_jobs import BaselineJobRunner
from app.core import coverage_policy as policy
from app.core import session_baseline, trading_calendar as tc
from app.ingestion import IngestionCatalog, darkpool_print_key
from tests.synthetic_ledger import LedgerUW, stored_keys
from tests.uw_fakes import FakeResponse
from tests.uw_paginated import NY, _ts, iso, prior_sessions, session_alerts, session_prints

T = "ABCD"
EVIDENCE = date(2026, 10, 16)
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
SETTLED = CLOSE + policy.SETTLE_LAG + timedelta(minutes=30)
NEXT_DAY = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "audit2.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def walk(uw, cat, now, d=DAY, ds="darkpool", pages=200, **kw):
    uw.now = now
    return sc.update_session_coverage(uw.adapter().client, cat, ds, T, d, now_utc=now, max_pages=pages, **kw)


def bounds(d):
    info = tc.session_info(d)
    return info.session_open_et.astimezone(timezone.utc), info.session_close_et.astimezone(timezone.utc)


def exact(uw, cat, as_of, d=DAY, ds="darkpool"):
    o, c = bounds(d)
    return stored_keys(cat, ds, T, o, c) == uw.truth(ds, T, o, c, as_of)


def with_hole(rows, lo, hi, visible_from=None, hidden=False):
    out = []
    for r in rows:
        ts = _ts(r["executed_at"])
        if lo <= ts < hi:
            r = {**r, "_hidden": True} if hidden else {**r, "_visible_from": visible_from}
        out.append(r)
    return out


# ------------------------------------------------------------------------------------------------
# C2-1  gap check cannot see any outage shorter than GAP_MIN (20 min), however liquid the ticker
# ------------------------------------------------------------------------------------------------
def test_c2_1_short_outage_on_a_liquid_ticker_is_not_certified(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=0.05)         # one print every 3 s (~7.8k prints)
    hole_lo = OPEN + timedelta(hours=2)
    rows = with_hole(rows, hole_lo, hole_lo + timedelta(minutes=19), visible_from=NEXT_DAY)  # backfilled later
    uw = LedgerUW(darkpool=rows)
    u = walk(uw, cat, SETTLED, pages=200)
    # ~380 prints (expected count in the hole) are missing; exp(-380) is not a plausible quiet spell
    assert not policy.is_certified(u.coverage), (
        f"certified with a 19-minute provider hole; max_gap_s={u.coverage.get('max_gap_s')}")


def test_c2_1b_one_hour_outage_on_a_moderate_ticker_is_not_certified(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=1.5)          # ~261 prints/day
    hole_lo = OPEN + timedelta(hours=2)
    rows = with_hole(rows, hole_lo, hole_lo + timedelta(hours=1), visible_from=NEXT_DAY)
    uw = LedgerUW(darkpool=rows)
    u = walk(uw, cat, SETTLED, pages=200)
    assert not policy.is_certified(u.coverage), (
        f"certified with a 1-hour hole (40 of 261 prints missing); max_gap_s={u.coverage.get('max_gap_s')}")


# ------------------------------------------------------------------------------------------------
# C2-2  a 'suspected' gap is "confirmed" by ANY second walk -- one minute later is enough
# ------------------------------------------------------------------------------------------------
def test_c2_2_suspected_gap_is_not_confirmed_by_an_immediate_second_walk(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=1)
    lo = OPEN + timedelta(hours=2)
    rows = with_hole(rows, lo, lo + timedelta(hours=1), visible_from=NEXT_DAY - timedelta(hours=2))
    uw = LedgerUW(darkpool=rows)
    assert walk(uw, cat, SETTLED).coverage["last_error"] == "suspected_provider_gap"
    u = walk(uw, cat, SETTLED + timedelta(minutes=1))                  # e.g. a second backfill click / reconcile
    assert not policy.is_certified(u.coverage), "transient outage confirmed as genuine 1 minute later"


def test_c2_2b_a_suspected_gap_is_confirmed_after_the_waiting_period(cat):
    """Liveness: a genuine (never backfilled) gap is certified by a walk GAP_CONFIRM_AFTER later."""
    rows = session_prints(T, DAY, 1000.0, every_minutes=1)
    lo = OPEN + timedelta(hours=2)
    rows = [r for r in rows if not (lo <= _ts(r["executed_at"]) < lo + timedelta(hours=1))]
    uw = LedgerUW(darkpool=rows)
    assert walk(uw, cat, SETTLED).coverage["last_error"] == "suspected_provider_gap"
    later = SETTLED + policy.GAP_CONFIRM_AFTER + timedelta(minutes=1)
    u = walk(uw, cat, later)
    assert policy.is_certified(u.coverage) and u.coverage["gap_flag"] == "confirmed"


# ------------------------------------------------------------------------------------------------
# C2-3  a cancellation is irreversible: a print later reported valid again stays excluded forever
# ------------------------------------------------------------------------------------------------
def test_c2_3_reversed_cancellation_is_honoured_by_reconciliation(cat):
    rows = session_prints(T, DAY, 1000.0)
    rows[10] = {**rows[10], "_canceled_from": OPEN}                    # served canceled at the settled walk
    uw = LedgerUW(darkpool=rows)
    assert policy.is_certified(walk(uw, cat, SETTLED).coverage)
    uw.darkpool[T][10].pop("_canceled_from")                           # provider reverses the cancellation
    u = walk(uw, cat, NEXT_DAY, reverify="operator", preserve_on_incomplete=True)
    assert policy.is_certified(u.coverage)
    assert exact(uw, cat, NEXT_DAY), "certified, but a print the provider reports as valid is excluded"


# ------------------------------------------------------------------------------------------------
# C2-4  after settlement an ordinary scan of the evidence session drops "verified from the open"
# ------------------------------------------------------------------------------------------------
def test_c2_4_settlement_rewalk_does_not_unverify_the_evidence_session(cat):
    ev_open, ev_close = bounds(EVIDENCE)
    uw = LedgerUW(darkpool=session_prints(T, EVIDENCE, 100.0, every_minutes=0.05))   # ~7.8k prints: 16 pages
    for h in range(1, 7):
        walk(uw, cat, ev_open + timedelta(hours=h), EVIDENCE, pages=60)
    u = walk(uw, cat, ev_close + timedelta(minutes=10), EVIDENCE, pages=60)
    assert u.coverage["covered_from_utc"] == ev_open.isoformat()
    as_of = ev_close + policy.SETTLE_LAG + timedelta(minutes=5)
    before = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=as_of)
    assert "not verified from the open" not in before.reason
    walk(uw, cat, as_of, EVIDENCE, pages=sc.DEFAULT_SCAN_PAGES, settle=False)   # an ordinary scan in the evening
    # (the app's scan path passes settle=False: main._collect_session_dataset)
    after = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=as_of)
    assert "not verified from the open" not in after.reason, after.reason


# ------------------------------------------------------------------------------------------------
# C2-5  job/overview target counts certified rows, baseline then excludes implausible-empty days
# ------------------------------------------------------------------------------------------------
def test_c2_5_job_target_agrees_with_the_baseline_it_is_building(cat):
    days = prior_sessions(EVIDENCE, 21)
    empty = {days[2], days[5]}
    rows = [r for d in days if d not in empty for r in session_prints(T, d, 1000.0, every_minutes=5)]
    uw = LedgerUW(darkpool=rows)
    uw.now = NEXT_DAY
    res = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=400,
                               lookback_sessions=21, now_utc=NEXT_DAY)
    entry = {"datasets": {"darkpool": {"complete_prior_sessions": res["complete_prior_sessions"]}}}
    done = BaselineJobRunner._ticker_done(entry, 20)
    base = session_baseline.compute(cat, "darkpool", T, EVIDENCE, as_of_utc=NEXT_DAY)
    assert not (done == "complete" and base.eligible_sessions < 20), (
        f"job reports complete ({res['complete_prior_sessions']} certified) but the baseline has "
        f"{base.eligible_sessions} usable sessions (excluded empty: {base.excluded_implausible_empty})")


# ------------------------------------------------------------------------------------------------
# C2-6  a legacy 'complete' row is modified in place by a pre-settlement walk (no re-walk, label lost)
# ------------------------------------------------------------------------------------------------
def test_c2_6_legacy_complete_row_is_not_rewritten_before_its_rewalk(cat):
    rows = session_prints(T, DAY, 1000.0)
    uw = LedgerUW(darkpool=rows)
    cat.catalog_darkpool_observations(T, rows[:-5])
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "complete", OPEN.isoformat(), CLOSE.isoformat(), 1, 1)
    calls = len(uw.calls)
    walk(uw, cat, CLOSE + timedelta(minutes=30))                       # same evening, before settlement
    assert len(uw.calls) == calls                                      # (no request was made)
    cov = cat.get_session_coverage("darkpool", T, DAY.isoformat())
    assert policy.is_legacy_complete(cov), f"legacy row rewritten without any walk: {cov['status']}:{cov['last_error']}"


def test_c2_6b_the_legacy_rewalk_is_logged_as_legacy(cat):
    rows = session_prints(T, DAY, 1000.0)
    uw = LedgerUW(darkpool=rows)
    cat.catalog_darkpool_observations(T, rows[:-5])
    cat.upsert_session_coverage("darkpool", T, DAY.isoformat(), "complete", OPEN.isoformat(), CLOSE.isoformat(), 1, 1)
    walk(uw, cat, CLOSE + timedelta(minutes=30))
    walk(uw, cat, SETTLED)
    recs = [r for r in cat.list_coverage_reconciliations(ticker=T) if r["session_date"] == DAY.isoformat()]
    assert recs and recs[0]["trigger"] == "legacy", [r["trigger"] for r in recs]


# ------------------------------------------------------------------------------------------------
# C2-7  >page_limit dark-pool prints in one second: session permanently unknown and re-walked by
#       every backfill (no 24 h back-off as for rejected content)
# ------------------------------------------------------------------------------------------------
def test_c2_7_cursor_stuck_session_is_not_rewalked_by_every_backfill(cat):
    burst = [{"ticker": T, "executed_at": iso(CLOSE), "price": "100.00", "size": 100 + i, "premium": "1.00",
              "tracking_id": f"MOC-{i}", "canceled": False} for i in range(600)]
    uw = LedgerUW(darkpool=session_prints(T, DAY, 1000.0) + burst)
    u = walk(uw, cat, SETTLED)
    assert u.coverage["status"] == "unknown" and u.coverage["last_error"] == "cursor_stuck"
    uw.now = NEXT_DAY
    calls = len(uw.calls)
    sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=50, lookback_sessions=1,
                         now_utc=NEXT_DAY)
    sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=50, lookback_sessions=1,
                         now_utc=NEXT_DAY)
    # evidence session (EVIDENCE) has no rows: 1 page each run; DAY should not be re-requested within 24 h
    day_calls = [p for u_, p in uw.calls[calls:] if p.get("date") == DAY.isoformat()]
    assert len(day_calls) <= 2, f"stuck session re-walked {len(day_calls)} pages in two back-to-back backfills"


# ------------------------------------------------------------------------------------------------
# C2-8  a failed (429/5xx) gap-fill request does not stop the walk: more requests follow the 429
# ------------------------------------------------------------------------------------------------
class SeqFailUW(LedgerUW):
    """Fails exactly the request numbers in `fail_at` (1-based, per instance) with `resp`."""

    def __init__(self, *a, fail_at=(), resp=None, **k):
        super().__init__(*a, **k)
        self.fail_at, self.resp, self.n = set(fail_at), resp, 0

    def get(self, url, headers=None, params=None, timeout=None):
        self.n += 1
        if self.n in self.fail_at:
            self.calls.append((url, dict(params or {})))
            return self.resp
        return super().get(url, headers, params, timeout)


def test_c2_8_no_provider_request_after_a_rate_limit_in_the_same_walk(cat):
    ev_open, _ = bounds(EVIDENCE)
    rows = session_prints(T, EVIDENCE, 100.0, every_minutes=0.01, end=time(11, 30))
    uw = SeqFailUW(darkpool=rows)
    walk(uw, cat, ev_open + timedelta(hours=1), EVIDENCE, pages=2)     # interval [R, E1] (backward gap below)
    walk(uw, cat, ev_open + timedelta(hours=1, minutes=30), EVIDENCE, pages=2)   # island above it
    cov = cat.get_session_coverage("darkpool", T, EVIDENCE.isoformat())
    assert cov["island_from_utc"] and cov["covered_from_utc"] > ev_open.isoformat()
    uw.fail_at, uw.resp, uw.n = {2}, FakeResponse(429, {"message": "Too Many Requests"}), 0
    start = len(uw.calls)
    u = walk(uw, cat, ev_open + timedelta(hours=1, minutes=31), EVIDENCE, pages=10)
    statuses = u.collection_status
    after_429 = len(uw.calls) - start - 2
    assert after_429 == 0, f"{after_429} further provider request(s) after a 429 in one walk (status {statuses})"


# ------------------------------------------------------------------------------------------------
# Property / fuzz: faults, rejected pages, same-timestamp clusters, budgets, operator reconciles
# ------------------------------------------------------------------------------------------------
class FaultyLedger(LedgerUW):
    def __init__(self, *a, rnd=None, p_fail=0.0, p_rej=0.0, **k):
        super().__init__(*a, **k)
        self.rnd, self.p_fail, self.p_rej = rnd, p_fail, p_rej

    def get(self, url, headers=None, params=None, timeout=None):
        x = self.rnd.random()
        if x < self.p_fail:
            self.calls.append((url, dict(params or {})))
            return FakeResponse(self.rnd.choice([429, 500, 503]), {"message": "injected"})
        resp = super().get(url, headers, params, timeout)
        if x < self.p_fail + self.p_rej and resp.status_code == 200 and resp._body["data"] and params.get("older_than"):
            data = list(resp._body["data"])
            bad = dict(data[0])
            field = "executed_at" if "executed_at" in bad else "created_at"
            bad[field] = iso(_ts(params["older_than"]) + timedelta(seconds=self.rnd.choice([0, 1, 3600])))
            bad["tracking_id"] = bad["id"] = f"INJ-{self.rnd.random()}"
            data.insert(0, bad)
            return FakeResponse(200, {"data": data})
        return resp


def _rows(rnd, ds):
    every = rnd.choice([0.05, 0.2, 0.5, 1, 3])
    rows = (session_prints(T, DAY, 1000.0, every_minutes=every) if ds == "darkpool"
            else session_alerts(T, DAY, 5000.0, every_minutes=every))
    field = "executed_at" if ds == "darkpool" else "created_at"
    limit = 500 if ds == "darkpool" else 200
    for c in range(rnd.randint(0, 6)):                                  # same-timestamp clusters
        base = rnd.choice(rows)
        for i in range(rnd.randint(2, limit - 50)):
            r = {**base, "tracking_id": f"CL{c}-{i}", "id": f"CL{c}-{i}", "size": 7 + i}
            if ds == "darkpool":
                r.pop("id")
            rows.append(r)
    for i in range(rnd.randint(0, 4)):                                  # prints at the exact open / close
        t = rnd.choice([OPEN, CLOSE])
        r = {**rows[0], field: iso(t), "tracking_id": f"EDGE{i}", "id": f"EDGE{i}", "size": 3 + i}
        if ds == "darkpool":
            r.pop("id")
        rows.append(r)
    for r in rows:
        u = rnd.random()
        if u < 0.04:
            r["_visible_from"] = OPEN + timedelta(minutes=rnd.randint(0, 60 * 30))
        elif u < 0.05 and ds == "darkpool":
            r["_canceled_from"] = OPEN + timedelta(minutes=rnd.randint(0, 60 * 30))
    if rnd.random() < 0.3:                                              # an outage, maybe backfilled later
        lo = OPEN + timedelta(minutes=rnd.randint(0, 300))
        hi = lo + timedelta(minutes=rnd.randint(5, 90))
        vf = None if rnd.random() < 0.5 else OPEN + timedelta(hours=rnd.randint(4, 40))
        for r in rows:
            if lo <= _ts(r[field]) < hi:
                if vf is None:
                    r["_hidden"] = True
                else:
                    r["_visible_from"] = max(vf, r.get("_visible_from") or vf)
    return rows


def _key(ds, r):
    pub = {k: v for k, v in r.items() if not k.startswith("_")}
    return darkpool_print_key(T, pub) if ds == "darkpool" else ("i", pub["id"])


# 24 seeds in the regular suite; TIP_FUZZ_SEEDS=120 reproduces the round-2 reviewer's full run
@pytest.mark.parametrize("seed", range(int(__import__("os").environ.get("TIP_FUZZ_SEEDS", "24"))))
def test_fuzz_certification_and_reconcile_invariants(cat, seed):
    rnd = random.Random(1000 + seed)
    ds = "darkpool" if seed % 2 == 0 else "options_flow"
    rows = _rows(rnd, ds)
    uw = FaultyLedger(darkpool=rows if ds == "darkpool" else [], flow=rows if ds != "darkpool" else [],
                      rnd=rnd, p_fail=rnd.choice([0, 0.05, 0.15]), p_rej=rnd.choice([0, 0.05, 0.15]))
    ever_canceled = {_key(ds, r) for r in rows if r.get("_canceled_from")}
    times = sorted(OPEN + timedelta(minutes=rnd.randint(10, 60 * 40)) for _ in range(rnd.randint(3, 12)))
    times += [NEXT_DAY + timedelta(days=1, minutes=i) for i in range(40)]
    for now in times:
        pages = rnd.choice([1, 2, 3, 4, 10, 60])
        operator = now >= CLOSE + policy.SETTLE_LAG and rnd.random() < 0.25
        before = cat.get_session_coverage(ds, T, DAY.isoformat())
        n0 = len(uw.calls)
        kw = {"reverify": "operator", "preserve_on_incomplete": True} if operator else {}
        u = walk(uw, cat, now, ds=ds, pages=pages, **kw)
        assert len(uw.calls) - n0 <= pages, ("request budget exceeded", seed)
        after = cat.get_session_coverage(ds, T, DAY.isoformat())
        if operator and after != before:
            assert policy.is_certified(after) and after["certified_at_utc"] == now.isoformat(), (
                "reconcile changed the stored row without certifying", seed, before, after)
        if after and after.get("certified_by"):
            assert after["status"] == "complete"
        if policy.is_certified(after):
            proof = datetime.fromisoformat(after["proof_started_utc"])
            assert proof >= CLOSE + policy.SETTLE_LAG, (seed, proof)
            stored = stored_keys(cat, ds, T, OPEN, CLOSE)
            missing = uw.truth(ds, T, OPEN, CLOSE, proof) - stored - ever_canceled
            assert not missing, ("certified while missing rows visible at proof start", seed, len(missing))
