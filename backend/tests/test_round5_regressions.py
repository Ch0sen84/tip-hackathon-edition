"""AUDIT5 -- narrow adversarial verification of bacbe60 / e03bc86 / df7a686.

Every test asserts the CORRECT behaviour; a FAILING test reproduces a finding (R5-n). Constructed data only.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app.baseline_jobs import BaselineJobRunner, BaselineJobStore
from app.core import coverage_policy as policy
from app.ingestion import IngestionCatalog
from tests.uw_paginated import NY

T = "ABCD"
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)


def _at(minute, second):
    return OPEN + timedelta(minutes=minute, seconds=second)


def _session(open_minutes, open_prints):
    """open burst on `open_minutes`, then a 180-minute outage (minutes 10..189), then a steady
    two prints every other minute to the close (minutes 190..388)."""
    ts = [_at(m, 5 + 9 * i) for m in open_minutes for i in range(open_prints)]
    ts += [_at(m, s) for m in range(190, 390, 2) for s in (10, 40)]
    return ts


# R5-1 a long outage that leaves a short CONTIGUOUS island at the open (or the close) is never judged: the
# saturated island is one event (< GAP_MIN_SIDE_EVENTS) and vetoes rule (a) through min() over sides, and the
# other side's share (< 0.7) vetoes rule (b). ~44% of the session's rows are lost and the session certifies.
def test_r5_1_long_outage_after_a_contiguous_open_burst_is_suspected():
    contiguous = _session(range(0, 10), 3)          # minutes 0..9 all active: ONE event on the left side
    spaced = _session(range(0, 10, 2), 6)           # same rows at the open, five events
    assert policy.gap_assessment(spaced, OPEN, CLOSE)[1] is True      # control: the same outage IS caught
    g, implausible = policy.gap_assessment(contiguous, OPEN, CLOSE)
    assert implausible, (f"{len(contiguous)} rows, a {g / 60:.0f}-minute silence after a 10-minute opening burst "
                         "(right side alone predicts ~90 events against a threshold of ~10) is certified")


class _Steps:
    """Fake run_step with the production page split (main._allocate_pages): darkpool's only remaining
    sessions await gap confirmation (spends nothing); options_flow still has work."""

    def __init__(self, limit=25, flow_work=True):
        self.calls, self.limit, self.flow_work = 0, limit, flow_work

    def __call__(self, ticker, pages, datasets=None):
        self.calls += 1
        if self.calls > self.limit:
            raise RuntimeError("spinning")
        chosen = [d for d in ("darkpool", "options_flow") if datasets is None or d in datasets]
        q, r = divmod(pages, len(chosen))
        alloc = {d: q + (1 if i < r else 0) for i, d in enumerate(chosen)}
        out = {}
        for d in chosen:
            if d == "darkpool":
                out[d] = {"pages_used": 0, "complete_prior_sessions": 5, "sessions_touched": [],
                          "awaiting_gap_confirmation": [{"session_date": "2026-10-14",
                                                         "confirmable_at": "2026-10-17T04:00:00+00:00"}]}
            else:
                used = min(alloc[d], 1) if self.flow_work else 0
                out[d] = {"pages_used": used, "complete_prior_sessions": 5,
                          "sessions_touched": [{"status": "ok"}] if used else [], "awaiting_gap_confirmation": []}
            out[d]["pages_allocated"] = alloc[d]
        return out


@pytest.fixture
def store(tmp_path):
    cat = IngestionCatalog(str(tmp_path / "audit5.db"))
    return BaselineJobStore(cat.db_path, cat._conn)


# R5-2 one page left (max_pages_per_ticker odd/1, or the last page of a budget): the split gives it to darkpool,
# which awaits confirmation and is no longer marked no_more_work (R4-4), so options_flow is starved forever and
# the step repeats with used == 0 -- a hot loop that never completes and never spends the page.
def test_r5_2_job_does_not_spin_when_one_page_is_left_and_darkpool_awaits(store):
    job = store.create([T], max_pages_per_ticker=1, step_pages=10, requested_by="audit5", target_sessions=20)
    steps = _Steps()
    try:
        BaselineJobRunner(store, steps)._loop(job["id"])
    except RuntimeError:
        pass
    assert steps.calls <= 3, f"{steps.calls} steps and still running: {store.get(job['id'])['progress'][T]}"


# R5-3 a job whose last ticker ends `awaiting_gap_confirmation` transitions to `completed`; Resume (and the
# R4-4 re-arm) accept only `paused`, so the awaiting ticker can never be walked again by this job, contrary to
# the R4-4 comment and the UI label ("resume ... after it").
def test_r5_3_job_with_an_awaiting_ticker_stays_resumable(store):
    job = store.create([T], max_pages_per_ticker=100, step_pages=10, requested_by="audit5", target_sessions=20)
    BaselineJobRunner(store, _Steps(flow_work=False))._loop(job["id"])
    j = store.get(job["id"])
    assert j["progress"][T]["state"] == "awaiting_gap_confirmation", j["progress"][T]
    assert j["status"] != "completed", "job completed (not resumable) while its ticker awaits gap confirmation"


# R5-7 a near-total loss (a session with a tiny fraction of the ticker's usual activity) has no long gap the
# gap check could see; it certifies. The baseline and the job targets exclude it for an active ticker, so it is
# never averaged in as real activity -- but only once there are enough active sessions to know what usual is.
def _covs(rows):
    d, out = date(2026, 6, 1), []
    for n in rows:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({"session_date": d.isoformat(), "status": "complete", "certified_by": policy.CERTIFIER,
                    "session_rows": n})
        d += timedelta(days=1)
    return out


def test_r5_7_near_total_loss_is_not_counted_for_an_active_ticker():
    normal = _covs([200] * 12)
    lossy = _covs([200] * 6 + [8] + [200] * 5)            # 8 rows = 4% of the median
    before = "2026-12-31"
    n_normal = policy.usable_prior_count(normal, before)
    assert policy.usable_prior_count(lossy, before) == n_normal - (1 if _all_trading(lossy) else 0)
    # a genuinely quiet but plausible day (25% of the median) still counts
    assert policy.usable_prior_count(_covs([200] * 6 + [50] + [200] * 5), before) == n_normal


def test_r5_7_small_session_rule_needs_enough_active_sessions():
    # two sessions only: the rule must not fire (the median of two is not a reliable "usual")
    covs = _covs([400, 12])
    assert policy.usable_prior_count(covs, "2026-12-31") == sum(1 for _ in _trading(covs))


def _trading(covs):
    from app.core import trading_calendar as tc
    return [c for c in covs if tc.is_trading_day(date.fromisoformat(c["session_date"]))]


def _all_trading(covs):
    return len(_trading(covs)) == len(covs)


# R5-8 doclint check 6 bypasses: "don't forget to update" read as a negation, one negated upgrade word
# exempting a whole paragraph, and a rebuild command split over shell continuation lines.
REPO = Path(__file__).resolve().parents[2]


def _doclint():
    spec = importlib.util.spec_from_file_location("doclint_audit5", REPO / "tools" / "recovery" / "doclint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("text", [
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nDon't forget to update: run `docker compose up -d --build`.\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nNever upgrade by hand; to update, run "
    "`docker compose up -d --build`.\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nTo update the app:\n\n```\ndocker compose up \\\n"
    "  -d --build\n```\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nDo not update the .env file. To upgrade to a new release, "
    "git pull and run:\n\n```\ndocker compose up -d --build\n```\n",
])
def test_r5_8_doclint_flags_upgrade_advice(text):
    assert [f for f in _doclint().lint_text("x.md", text) if "rebuild command" in f], text


@pytest.mark.parametrize("text", [
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nNever upgrade with `docker compose up -d --build`.\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nFirst install only:\n\n```\ndocker compose up -d --build\n```\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\n```\ndocker compose up -d --build\n```\n\n"
    "The Settings page then shows whether an update is available.\n",
])
def test_r5_8_doclint_still_accepts_sound_text(text):
    assert not [f for f in _doclint().lint_text("x.md", text) if "rebuild command" in f], text

