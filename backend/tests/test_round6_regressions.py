"""Round 6 -- adversarial verification of a30485a against its parent df7a686 (reviewer reproductions).

Every test asserts the CORRECT behaviour: a FAILING test reproduces a finding (R6-n, see audit6-report.md).
Tests named `test_sound_*` document a claim that was attacked and held. Constructed data only: no network, no
Docker, no production data.
"""
from __future__ import annotations

import importlib.util
import random
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app import session_collection as sc
from app.baseline_jobs import BaselineJobRunner, BaselineJobStore
from app.core import coverage_policy as policy
from app.core import session_baseline
from app.core import trading_calendar as tc
from app.ingestion import IngestionCatalog
from tests.synthetic_ledger import LedgerUW
from tests.uw_paginated import NY, _ts, prior_sessions, session_alerts, session_prints

REPO = Path(__file__).resolve().parents[2]
T = "ABCD"
EVIDENCE = date(2026, 10, 16)
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
NOON = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "audit6.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def _load_source(name, source):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(source, name, "exec"), mod.__dict__)
    return mod


def _parent(path):
    """The df7a686 version of a repo file (read from this clone's object store)."""
    try:
        return subprocess.run(["git", "-C", str(REPO), "show", f"df7a686:{path}"], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    except Exception:  # noqa: BLE001
        pytest.skip("df7a686 not available in this clone")


# ======================================================================================================
# 1. gap_assessment per-side judgement (R5-1)
# ======================================================================================================
def _gen(rng, n, span=390, cluster=1.0):
    """U-shaped genuine session (seconds from the open); clusters of mean size `cluster`."""
    w = [0.45 + 2.2 * (1 - (m + .5) / span) ** 6 + 1.6 * ((m + .5) / span) ** 6 for m in range(span)]
    s, out = sum(w), []
    for m in range(span):
        lam = n / cluster * w[m] / s
        k, p, L = 0, rng.random(), pow(2.718281828, -lam)
        while p > L:
            k += 1
            p *= rng.random()
        for _ in range(k):
            t0 = m * 60 + rng.random() * 60
            size = 1
            while cluster > 1 and rng.random() > 1 / cluster:
                size += 1
            out += [min(span * 60 - 1, t0 + (rng.random() * 120 if cluster > 1 else 0)) for _ in range(size)]
    return sorted(out)


def _flag(mod, secs, span=390):
    close = OPEN + timedelta(minutes=span)
    return mod.gap_assessment([OPEN + timedelta(seconds=x) for x in secs], OPEN, close)[1]


def test_sound_r5_1_new_rule_flags_everything_the_parent_flagged():
    """No new false negative: on genuine sessions and on outage shapes (mid, edge, contiguous-island, near-total)
    the new per-side rule flags every session df7a686 flagged (a provable superset; measured here)."""
    old = _load_source("cp_df7a686", _parent("backend/app/core/coverage_policy.py"))
    rng = random.Random(606)
    regressions, caught_old, caught_new, n_cases = [], 0, 0, 0
    for n in (60, 150, 400, 1500):
        for cluster in (1.0, 3.0):
            for (a, b) in ((None, None), (10, 190), (5, 150), (100, 130), (200, 380), (300, 385), (0, 60), (330, 390),
                           (1, 389)):
                for _ in range(12):
                    t = _gen(rng, n, cluster=cluster)
                    if a is not None:
                        t = [x for x in t if not (a * 60 <= x < b * 60)]
                    o, w = _flag(old, t), _flag(policy, t)
                    n_cases += 1
                    caught_old += o
                    caught_new += w
                    if o and not w:
                        regressions.append((n, cluster, a, b))
    assert not regressions, f"{len(regressions)} sessions flagged by df7a686 are certified by a30485a: {regressions[:5]}"
    assert caught_new >= caught_old


def test_sound_r5_1_genuine_false_positive_rate_unchanged():
    old = _load_source("cp_df7a686b", _parent("backend/app/core/coverage_policy.py"))
    rng = random.Random(77)
    for n in (150, 400, 1500, 8000):
        sessions = [_gen(rng, n) for _ in range(150)]
        fo = sum(_flag(old, t) for t in sessions)
        fn = sum(_flag(policy, t) for t in sessions)
        assert fn - fo <= 3, f"{n} prints/day: FP {fo}/150 -> {fn}/150"


@pytest.mark.parametrize("secs, span", [
    ([], 390),                                               # no events at all
    ([30.0], 390),                                           # a single event
    ([5.0, 10.0, 15.0], 3),                                  # session shorter than one side
    ([s for s in range(0, 240, 2)], 390),                    # session ends with a 386-minute silence
    ([390 * 60 - 1 - s for s in range(0, 240, 2)], 390),     # session starts with the silence
    ([m * 60 + 1 for m in range(0, 4)] + [m * 60 + 1 for m in range(30, 390)], 390),  # left side < 5 min
])
def test_sound_gap_assessment_edge_cases_do_not_raise(secs, span):
    close = OPEN + timedelta(minutes=span)
    g, flag = policy.gap_assessment([OPEN + timedelta(seconds=s) for s in secs], OPEN, close)
    assert g >= 0 and flag in (True, False)


def test_sound_gap_assessment_zero_span():
    assert policy.gap_assessment([OPEN], OPEN, OPEN) == (0.0, False)


def test_sound_one_sided_gaps_still_judged_by_the_remaining_side():
    # busy steady session that stops 120 minutes early (provider outage to the close): one side only
    secs = [m * 60 + s for m in range(0, 270) for s in (5, 25, 45)]
    assert _flag(policy, secs) is True
    # ... and one that starts 120 minutes late
    secs = [m * 60 + s for m in range(120, 390) for s in (5, 25, 45)]
    assert _flag(policy, secs) is True


def test_sound_thin_side_still_vetoes_like_the_parent():
    """A gap next to a side with < 3 events and < 70% active minutes is not judged (same as df7a686): a
    thin context cannot tell an outage from a quiet stretch."""
    old = _load_source("cp_df7a686c", _parent("backend/app/core/coverage_policy.py"))
    busy = [m * 60 + 5 for m in range(0, 120)]              # saturated 2 hours
    thin = [(200 + 7 * k) * 60 for k in range(2)]            # two events after a 79-minute gap
    tail = [m * 60 + 5 for m in range(260, 390, 9)]
    secs = busy + thin + tail
    assert _flag(policy, secs) == _flag(old, secs)


def test_sound_half_day_session():
    half_open = datetime(2025, 11, 28, 14, 30, tzinfo=timezone.utc)
    half_close = half_open + timedelta(minutes=210)
    secs = [m * 60 + s for m in range(0, 210) for s in (5, 35)]
    ts = [half_open + timedelta(seconds=x) for x in secs]
    assert policy.gap_assessment(ts, half_open, half_close)[1] is False
    cut = [t for t in ts if not (half_open + timedelta(minutes=60) <= t < half_open + timedelta(minutes=100))]
    assert policy.gap_assessment(cut, half_open, half_close)[1] is True


# ======================================================================================================
# 2. small-session rule (R5-7): usable_prior_count and compute agree
# ======================================================================================================
def _history(evidence, cadences, ds="darkpool"):
    """cadences: per prior session (oldest first) the print interval in minutes, or None for an empty day."""
    days = prior_sessions(evidence, len(cadences))           # newest first
    days = list(reversed(days))
    rows = []
    for d, every in zip(days, cadences):
        if every is None:
            continue
        rows += (session_prints(T, d, 1000.0, every_minutes=every) if ds == "darkpool"
                 else session_alerts(T, d, 50000.0, every_minutes=every))
    return rows, days


def _count_and_baseline(cat, evidence, cadences, now, ds="darkpool"):
    rows, days = _history(evidence, cadences, ds)
    uw = LedgerUW(darkpool=rows) if ds == "darkpool" else LedgerUW(flow=rows)
    uw.now = now
    res = sc.backfill_baseline(uw.adapter().client, cat, ds, T, evidence, max_pages=2000,
                               lookback_sessions=len(cadences), now_utc=now)
    base = session_baseline.compute(cat, ds, T, evidence, as_of_utc=now, min_sessions=1)
    certified = sum(1 for c in cat.list_session_coverage(ds, T)
                    if c["session_date"] < evidence.isoformat() and policy.is_certified(c))
    return res["complete_prior_sessions"], base, certified


@pytest.mark.parametrize("label, cadences, expect_excluded", [
    ("active, one near-total loss", [5] * 6 + [60] + [5] * 5, 1),            # ~7 rows vs median 79 (9%)
    ("active, one quiet but plausible day", [5] * 6 + [15] + [5] * 5, 0),     # 27 rows (34%)
    ("active, an empty day", [5] * 6 + [None] + [5] * 5, 1),
    ("thin ticker keeps small days", [30] * 6 + [195] + [30] * 5, 0),         # median 14 < 20
    ("4 active sessions: rule not armed", [5, 5, 120, 5], 0),
    ("5 active sessions: rule armed", [5, 5, 120, 5, 5], 1),
    ("pre-listing empties, then one small first day", [None, None, 120, 5, 5, 5, 5, 5], 1),
])
def test_sound_r5_7_job_count_equals_baseline_sessions(cat, label, cadences, expect_excluded):
    count, base, certified = _count_and_baseline(cat, EVIDENCE, cadences, NOON)
    assert certified == len(cadences), (label, certified)
    assert count == base.eligible_sessions, (label, count, base.eligible_sessions, base.excluded_implausible_empty)
    assert base.excluded_implausible_empty == expect_excluded, (label, base.excluded_implausible_empty)


def test_sound_r5_7_history_growing_across_the_threshold(cat):
    """Each prefix of a growing history: the job count always equals what the baseline uses."""
    cadences = [5, 5, 90, 5, 5, 5, 5]
    rows, days = _history(EVIDENCE, cadences)
    uw = LedgerUW(darkpool=rows)
    uw.now = NOON
    sc.backfill_baseline(uw.adapter().client, cat, "darkpool", T, EVIDENCE, max_pages=2000,
                         lookback_sessions=len(cadences), now_utc=NOON)
    covs = cat.list_session_coverage("darkpool", T)
    seen = []
    for k in range(1, len(days)):
        ev = days[k]                              # a certified session: its own coverage is verified from the open
        n = policy.usable_prior_count(covs, ev.isoformat())
        base = session_baseline.compute(cat, "darkpool", T, ev, as_of_utc=NOON, min_sessions=1)
        assert "not verified" not in base.reason, base.reason
        assert n == base.eligible_sessions, (ev, n, base.eligible_sessions)
        seen.append((k, n, base.excluded_implausible_empty))
    # the 90-minute-cadence day (5 rows) counts while < 5 active sessions exist, is excluded from then on
    assert [x[2] for x in seen] == [0, 0, 0, 0, 1, 1], seen


def test_sound_r5_7_half_day_is_not_an_implausibly_small_session(cat):
    evidence = date(2025, 12, 2)
    days = list(reversed(prior_sessions(evidence, 8)))
    assert date(2025, 11, 28) in days
    now = datetime(2025, 12, 2, 17, 0, tzinfo=timezone.utc)
    count, base, certified = _count_and_baseline(cat, evidence, [5] * 8, now)
    assert base.excluded_implausible_empty == 0 and count == base.eligible_sessions == 8, (
        count, base.eligible_sessions, base.excluded_implausible_empty)


def test_sound_r5_7_options_flow_uses_the_same_rule(cat):
    count, base, _ = _count_and_baseline(cat, EVIDENCE, [10] * 6 + [None] + [10] * 5, NOON, ds="options_flow")
    assert count == base.eligible_sessions


# ======================================================================================================
# 3. baseline jobs: waiting datasets, awaiting tickers, paused jobs, resume (R5-2/R5-3/R5-9)
# ======================================================================================================
def _gappy_darkpool(days):
    rows = []
    for d in days:
        o = datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
        lo, hi = o + timedelta(hours=2), o + timedelta(hours=3)
        rows += [r for r in session_prints(T, d, 1000.0, every_minutes=0.05, tag=d.isoformat())
                 if not (lo <= _ts(r["executed_at"]) < hi)]
    return rows


class _RealSteps:
    """run_step over the REAL collector with the production page split; counts steps."""

    def __init__(self, uw, cat, now, lookback=2, limit=200):
        self.uw, self.cat, self.now, self.lookback, self.limit, self.calls = uw, cat, now, lookback, limit, 0

    def __call__(self, ticker, pages, datasets=None):
        self.calls += 1
        if self.calls > self.limit:
            raise RuntimeError("job is spinning")
        chosen = [d for d in ("darkpool", "options_flow") if datasets is None or d in datasets]
        q, r = divmod(pages, len(chosen))
        out = {}
        for i, d in enumerate(chosen):
            a = q + (1 if i < r else 0)
            res = sc.backfill_baseline(self.uw.adapter().client, self.cat, d, ticker, EVIDENCE, max_pages=a,
                                       lookback_sessions=self.lookback, now_utc=self.now)
            res["pages_allocated"] = a
            out[d] = res
        return out


def _rearm(monkeypatch, store, job_id):
    from app import main as m
    monkeypatch.setattr(m, "get_baseline_job_store", lambda: store)
    m._rearm_awaiting_tickers(job_id)


def test_sound_r5_3_end_to_end_pause_early_resume_and_late_resume(cat, monkeypatch):
    days = prior_sessions(EVIDENCE, 2)
    uw = LedgerUW(darkpool=_gappy_darkpool(days), flow=[a for d in days for a in session_alerts(T, d, 5e4)])
    uw.now = NOON
    store = BaselineJobStore(cat.db_path, cat._conn)
    job = store.create([T], max_pages_per_ticker=1000, step_pages=10, requested_by="audit6", target_sessions=20)
    steps = _RealSteps(uw, cat, NOON)
    BaselineJobRunner(store, steps)._loop(job["id"])
    j = store.get(job["id"])
    assert j["status"] == "paused" and "confirmed from" in (j["last_error"] or ""), j
    assert j["progress"][T]["state"] == "awaiting_gap_confirmation"
    spent = j["pages_used"]
    for _ in range(3):                               # resume too early, three times: one cheap step each
        _rearm(monkeypatch, store, job["id"])
        store.transition(job["id"], ("paused",), "running")
        before_calls, before_steps = len(uw.calls), steps.calls
        BaselineJobRunner(store, steps)._loop(job["id"])
        j = store.get(job["id"])
        assert j["status"] == "paused" and j["progress"][T]["state"] == "awaiting_gap_confirmation"
        assert len(uw.calls) == before_calls and steps.calls - before_steps == 1 and j["pages_used"] == spent
    later = NOON + policy.GAP_CONFIRM_AFTER + timedelta(minutes=5)
    uw.now = steps.now = later
    _rearm(monkeypatch, store, job["id"])
    store.transition(job["id"], ("paused",), "running")
    BaselineJobRunner(store, steps)._loop(job["id"])
    j = store.get(job["id"])
    covs = {c["session_date"]: (c["status"], c.get("gap_flag")) for c in cat.list_session_coverage("darkpool", T)}
    assert j["status"] == "completed", j
    assert all(covs[d.isoformat()] == ("complete", "confirmed") for d in days), covs


class _FakeSteps:
    """Ticker WAIT: darkpool awaits confirmation, options_flow has `flow_pages` pages of work.
    Ticker WORK: both datasets have work and reach the target."""

    def __init__(self, flow_pages=3, limit=60):
        self.calls, self.limit, self.flow_left, self.log = 0, limit, flow_pages, []

    def __call__(self, ticker, pages, datasets=None):
        self.calls += 1
        if self.calls > self.limit:
            raise RuntimeError("spinning")
        chosen = [d for d in ("darkpool", "options_flow") if datasets is None or d in datasets]
        q, r = divmod(pages, len(chosen))
        alloc = {d: q + (1 if i < r else 0) for i, d in enumerate(chosen)}
        self.log.append((ticker, dict(alloc)))
        out = {}
        for d in chosen:
            if ticker == "WAIT" and d == "darkpool":
                out[d] = {"pages_used": 0, "complete_prior_sessions": 3, "sessions_touched": [],
                          "awaiting_gap_confirmation": [{"session_date": "2026-10-14",
                                                         "confirmable_at": "2026-10-17T04:00:00+00:00"}]}
            elif ticker == "WAIT":
                used = min(alloc[d], self.flow_left)
                self.flow_left -= used
                out[d] = {"pages_used": used, "complete_prior_sessions": 3 + (0 if self.flow_left else 0),
                          "sessions_touched": [{"status": "ok"}] if used else [], "awaiting_gap_confirmation": []}
            else:
                out[d] = {"pages_used": min(alloc[d], 1), "complete_prior_sessions": 20,
                          "sessions_touched": [{"status": "ok"}], "awaiting_gap_confirmation": []}
            out[d]["pages_allocated"] = alloc[d]
        return out


@pytest.fixture
def store(tmp_path):
    c = IngestionCatalog(str(tmp_path / "audit6_jobs.db"))
    return BaselineJobStore(c.db_path, c._conn)


def test_sound_multi_ticker_waiting_dataset_gets_no_pages_and_job_pauses(store):
    job = store.create(["WAIT", "WORK"], max_pages_per_ticker=100, step_pages=10, requested_by="audit6",
                       target_sessions=20)
    steps = _FakeSteps(flow_pages=12)
    BaselineJobRunner(store, steps)._loop(job["id"])
    j = store.get(job["id"])
    assert j["status"] == "paused", j
    assert j["progress"]["WAIT"]["state"] == "awaiting_gap_confirmation"
    assert j["progress"]["WAIT"]["confirmable_at"] == "2026-10-17T04:00:00+00:00"
    assert j["progress"]["WORK"]["state"] == "complete"
    wait_steps = [a for t, a in steps.log if t == "WAIT"]
    # after the first step darkpool is waiting_now: every later WAIT step gives all pages to options_flow
    assert all("darkpool" not in a for a in wait_steps[1:]), wait_steps
    assert steps.calls <= 6, steps.log


def test_sound_resume_with_a_paused_job_and_cancel(store, monkeypatch):
    job = store.create(["WAIT"], max_pages_per_ticker=100, step_pages=10, requested_by="audit6", target_sessions=20)
    BaselineJobRunner(store, _FakeSteps(flow_pages=0))._loop(job["id"])
    assert store.get(job["id"])["status"] == "paused"
    _rearm(monkeypatch, store, job["id"])
    p = store.get(job["id"])["progress"]["WAIT"]
    assert p["state"] == "pending" and not any(d.get("waiting_now") for d in p["datasets"].values())
    assert store.transition(job["id"], ("paused",), "cancelled") is not None


class _RacingStore(BaselineJobStore):
    """A user's Pause (or Cancel) lands between the runner's last read and its final status write: injected
    right before the runner's first end-of-job transition (whichever state it tries first)."""

    def __init__(self, *a, to="pause_requested", **k):
        super().__init__(*a, **k)
        self.to, self.fired = to, False

    def transition(self, job_id, from_states, to_state, **kw):
        if not self.fired and to_state in ("paused", "completed", "cancelled") and self.get(job_id)["status"] == "running":
            job = self.get(job_id)
            if all(p["state"] not in ("pending", "running") for p in job["progress"].values()):
                self.fired = True
                super().transition(job_id, ("running",), self.to)
        return super().transition(job_id, from_states, to_state, **kw)


@pytest.mark.parametrize("to, final", [("pause_requested", "paused"), ("cancel_requested", "cancelled")])
def test_r6_4_final_pause_honours_a_concurrent_pause_or_cancel(tmp_path, to, final):
    c = IngestionCatalog(str(tmp_path / "race.db"))
    store = _RacingStore(c.db_path, c._conn, to=to)
    job = store.create(["WAIT"], max_pages_per_ticker=100, step_pages=10, requested_by="audit6", target_sessions=20)
    BaselineJobRunner(store, _FakeSteps(flow_pages=0))._loop(job["id"])
    j = store.get(job["id"])
    assert j["status"] == final, f"job left '{j['status']}' with no worker thread (last_error={j['last_error']!r})"
    if final == "cancelled":
        assert not str(j["last_error"] or "").startswith("paused:"), j["last_error"]


# ======================================================================================================
# 4. doclint check 6 (R5-8)
# ======================================================================================================
H = "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\n"


def _doclint(src=None):
    if src is None:
        spec = importlib.util.spec_from_file_location("doclint_audit6", REPO / "tools" / "recovery" / "doclint.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return _load_source("doclint_df7a686", src)


def _flags(mod, text):
    return [f for f in mod.lint_text("x.md", H + text) if "rebuild command" in f]


# R6-1 regression: UPDATE_STATUS exempts the upgrade word even on the command's own line
@pytest.mark.parametrize("text", [
    "When an update is available, run `docker compose up -d --build`.\n",
    "If an upgrade is available, run `docker compose up -d --build`.\n",
    "Updates are available on GitHub; pull them and run `docker compose up -d --build`.\n",
    "To upgrade check out the new tag and run `docker compose up -d --build`.\n",
    "After the update-check reports a release, run `docker compose up -d --build`.\n",
])
def test_r6_1_update_status_wording_does_not_excuse_upgrade_advice(text):
    old = _doclint(_parent("tools/recovery/doclint.py"))
    assert _flags(old, text), "control: df7a686 flagged this"
    assert _flags(_doclint(), text), text


# R6-2 new false positives: clause splitting drops a negation that governs the command
@pytest.mark.parametrize("text", [
    "Never, under any circumstances, run `docker compose up -d --build` to upgrade.\n",
    "Do not use this to upgrade: `docker compose up -d --build` is for a first install.\n",
])
def test_r6_2_negated_command_with_punctuation_is_not_flagged(text):
    assert not _flags(_doclint(), text), text


# R6-3 pre-existing, incomplete R5-8 fix: other non-negating "don't X" / "do NOT forget" forms
@pytest.mark.parametrize("text", [
    "Do NOT forget to upgrade: run `docker compose up -d --build`.\n",
    "Don't wait to update: run `docker compose up -d --build`.\n",
    "Don't ever forget: upgrade with `docker compose up -d --build`.\n",
])
def test_r6_3_non_negating_dont_forms_are_still_advice(text):
    assert _flags(_doclint(), text), text


@pytest.mark.parametrize("text", [
    "Don't forget to update: run `docker compose up -d --build`.\n",
    "Never upgrade by hand; to update, run `docker compose up -d --build`.\n",
    "To update the app:\n\n```\ndocker compose \\\n  up \\\n  -d --build\n```\n",
    "Do not update the .env file. To upgrade to a new release, git pull and run:\n\n```\n"
    "docker compose up -d --build\n```\n",
])
def test_sound_r5_8_bypasses_closed(text):
    assert _flags(_doclint(), text), text


@pytest.mark.parametrize("text", [
    "Never upgrade with `docker compose up -d --build`.\n",
    "Do not run `docker compose up -d --build` to upgrade.\n",
    "First install only:\n\n```\ndocker compose up -d --build\n```\n",
    "```\ndocker compose up -d --build\n```\n\nThe Settings page then shows whether an update is available.\n",
    "```\ndocker compose up -d --build\n```\n\nThe update check runs daily and never changes the app.\n",
])
def test_sound_r5_8_sound_text_accepted(text):
    assert not _flags(_doclint(), text), text


def test_sound_doclint_repo_has_no_findings():
    r = subprocess.run([sys.executable, str(REPO / "tools" / "recovery" / "doclint.py")], capture_output=True,
                       text=True, cwd=str(REPO))
    assert r.returncode == 0 and "0 finding" in (r.stdout + r.stderr), r.stdout + r.stderr
