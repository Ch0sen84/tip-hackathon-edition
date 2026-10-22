"""Round-4 backend/deploy review (R4-1..R4-10), converted from the reviewer's reproductions into regression
tests asserting the corrected behaviour.

AUDIT4 -- narrow adversarial verification of the round-3 fixes (8870320 backend, 2c7cc85 deployment).

Every test asserts the CORRECT behaviour; a FAILING test reproduces a finding (R4-n). Tests named
`test_sound_*` document behaviour verified correct and pass. Constructed data only; no network, no Docker
(the recovery tool runs against tests/r3_fake_docker.py)."""
from __future__ import annotations

import importlib.util
import json
import math
import random
import subprocess
import sys
import types
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from app import session_collection as sc
from app.baseline_jobs import BaselineJobRunner, BaselineJobStore
from app.core import coverage_policy as policy
from app.ingestion import IngestionCatalog
from tests.synthetic_ledger import LedgerUW
from tests.uw_paginated import NY, _ts, prior_sessions, session_prints

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r3_fake_docker as fd  # noqa: E402
from test_deploy_audit_regressions import BASH, build_repo  # noqa: E402,F401  (fixture)

REPO = Path(__file__).resolve().parents[2]
RECOVERY = REPO / "tools" / "recovery"
RUNBOOK = (REPO / "docs" / "RELEASE3_RECOVERY_RUNBOOK.md").read_text(encoding="utf-8")
T = "ABCD"
EVIDENCE = date(2026, 10, 16)
DAY = date(2026, 10, 15)
OPEN = datetime.combine(DAY, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
CLOSE = datetime.combine(DAY, time(16, 0), tzinfo=NY).astimezone(timezone.utc)
SETTLED = CLOSE + policy.SETTLE_LAG + timedelta(minutes=30)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cat(tmp_path):
    return IngestionCatalog(str(tmp_path / "audit4.db"))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sc, "_sleep", lambda s: None)


def walk(uw, cat, now, d=DAY, pages=400):
    uw.now = now
    return sc.update_session_coverage(uw.adapter().client, cat, "darkpool", T, d, now_utc=now, max_pages=pages)


def _hide(rows, lo, hi):
    return [{**r, "_hidden": True} if lo <= _ts(r["executed_at"]) < hi else r for r in rows]


# ------------------------------------------------------------------------------------------------------
# R4-1  (regression of C2-1 by the R3B-2 fix) the active-minute model cannot see ANY outage shorter than
#       ~15 minutes, however liquid the ticker: a saturated ticker has every minute active, so the outside
#       share predicts g active minutes in a g-minute gap and the threshold is 15. At 80053c4 (row model) a
#       5-14 minute outage on a liquid ticker was flagged 100% of the time.
# ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("minutes", [5, 10, 14])
def test_r4_1_short_outage_on_a_liquid_ticker_is_not_certified(cat, minutes):
    rows = session_prints(T, DAY, 1000.0, every_minutes=0.05)             # a print every 3 s (~7.8k)
    lo = OPEN + timedelta(hours=2)
    rows = _hide(rows, lo, lo + timedelta(minutes=minutes))                # never backfilled
    missing = sum(1 for r in rows if r.get("_hidden"))
    u = walk(SETTLED_UW(rows), cat, SETTLED)
    assert not policy.is_certified(u.coverage), (
        f"certified complete with a {minutes}-minute hole ({missing} of {len(rows)} prints missing)")


def SETTLED_UW(rows):
    return LedgerUW(darkpool=rows)


# ------------------------------------------------------------------------------------------------------
# R4-2  (R3B-1 still reachable) a most-of-the-session outage is certified whenever the surviving stretch has
#       fewer than GAP_MIN_ACTIVE_MINUTES (20) active minutes -- the "however short that stretch was" rule
#       only applies from 20 active minutes. Liquid ticker, 9:30-15:41 never delivered: 95% of prints lost.
# ------------------------------------------------------------------------------------------------------
def test_r4_2_most_of_session_outage_with_18_surviving_minutes_is_not_certified(cat):
    rows = session_prints(T, DAY, 1000.0, every_minutes=0.05)
    rows = _hide(rows, OPEN, CLOSE - timedelta(minutes=18))   # + the 16:00:00 print = 19 active minutes
    kept = [r for r in rows if not r.get("_hidden")]
    u = walk(SETTLED_UW(rows), cat, SETTLED)
    assert not policy.is_certified(u.coverage), (
        f"certified complete with {len(kept)} of {len(rows)} prints (6h12m never delivered)")


def test_r4_2b_gap_assessment_direct():
    times = [CLOSE - timedelta(minutes=19) + timedelta(seconds=3 * i) for i in range(19 * 20)]
    assert policy.gap_assessment(times, OPEN, CLOSE)[1], "6h11m silent, then 380 prints in 19 minutes"


# ------------------------------------------------------------------------------------------------------
# R4-3  (R3B-2 only partly fixed; model-dependent, INFERRED) genuine Poisson sessions with a realistic
#       U-shaped intraday profile (open ~2.6x, midday ~0.45x the mean rate) and a 30% closing cross are
#       still flagged often at 60-250 prints/day. Seeded; see scratchpad a4/sim.py for the full matrix.
# ------------------------------------------------------------------------------------------------------
def _ushape(x):
    return 0.45 + 2.2 * (1 - x) ** 6 + 1.6 * x ** 6


def _genuine(rng, n_prints, close_frac=0.3, span=390):
    w = [_ushape((m + 0.5) / span) for m in range(span)]
    s = sum(w)
    out = []
    for m in range(span):
        lam, L, k, p = n_prints * (1 - close_frac) * w[m] / s, None, 0, 1.0
        L = math.exp(-lam)
        while True:
            p *= rng.random()
            if p <= L:
                break
            k += 1
        out += [OPEN + timedelta(seconds=m * 60 + rng.random() * 60) for _ in range(k)]
    out += [CLOSE - timedelta(seconds=1 + rng.random() * 59) for _ in range(int(n_prints * close_frac))]
    return sorted(out)


@pytest.mark.parametrize("n_prints", [60, 100, 150])
def test_r4_3_genuine_u_shaped_sessions_are_rarely_flagged(n_prints):
    rng = random.Random(1234 + n_prints)
    trials = 300
    flagged = sum(policy.gap_assessment(_genuine(rng, n_prints), OPEN, CLOSE)[1] for _ in range(trials))
    assert flagged / trials <= 0.10, f"{n_prints} prints/day: {100 * flagged / trials:.1f}% of genuine sessions flagged"


# ------------------------------------------------------------------------------------------------------
# R4-4  (side effect of the R3B-3 fix) a job whose remaining lookback sessions are all awaiting the 12 h
#       gap confirmation spends 0 pages on its next step and ends the ticker as `no_more_work` ("nothing more
#       to collect in the lookback window"), although those sessions still need their confirming walk; nothing
#       comes back to them (Resume excludes no_more_work tickers).
# ------------------------------------------------------------------------------------------------------
def test_r4_4_job_does_not_report_no_more_work_while_sessions_await_confirmation(cat, monkeypatch):
    days = prior_sessions(EVIDENCE, 2)
    rows = []
    for d in days:
        o = datetime.combine(d, time(9, 30), tzinfo=NY).astimezone(timezone.utc)
        lo, hi = o + timedelta(hours=2), o + timedelta(hours=3)
        rows += [r for r in session_prints(T, d, 1000.0, every_minutes=0.05, tag=d.isoformat())
                 if not (lo <= _ts(r["executed_at"]) < hi)]
    uw = LedgerUW(darkpool=rows)
    now = datetime(2026, 10, 16, 16, 0, tzinfo=timezone.utc)
    uw.now = now

    def step(ticker, pages, datasets=None):
        r = sc.backfill_baseline(uw.adapter().client, cat, "darkpool", ticker, EVIDENCE, max_pages=pages,
                                 lookback_sessions=2, now_utc=now)
        r["pages_allocated"] = pages
        return {"darkpool": r}

    store = BaselineJobStore(cat.db_path, cat._conn)
    job = store.create([T], max_pages_per_ticker=1000, step_pages=200, requested_by="audit4", target_sessions=20)
    BaselineJobRunner(store, step)._loop(job["id"])
    j = store.get(job["id"])
    covs = {c["session_date"]: c["last_error"] for c in cat.list_session_coverage("darkpool", T)}
    waiting = [d.isoformat() for d in days if covs.get(d.isoformat()) == "suspected_provider_gap"]
    assert waiting, covs
    assert j["progress"][T]["state"] != "no_more_work", (
        f"job ended {j['status']} / ticker {j['progress'][T]['state']} while {waiting} await their 12 h confirmation")


# ------------------------------------------------------------------------------------------------------
# Sound checks: readers agree (R3B-4) for the tricky identities
# ------------------------------------------------------------------------------------------------------
def _p(t, i, tag="X", **kw):
    return {"ticker": T, "executed_at": t.isoformat().replace("+00:00", "Z"), "price": "100.00", "size": 100 + i,
            "premium": "1000.00", "tracking_id": f"{tag}-{i}", "canceled": False, **kw}


def _counts(cat):
    window = cat.observations_between("darkpool", T, OPEN, CLOSE)
    cached = cat.get_cached_darkpool_observations(T)
    by_date = [x for v in cat.list_darkpool_observations_by_date(T).values() for x in v]
    return window, cached, by_date


def test_sound_readers_agree_on_cancel_reinstate_and_duplicate_ids(cat):
    a = _p(OPEN + timedelta(hours=1), 1, "A")
    b = _p(OPEN + timedelta(hours=2), 2, "B")
    cat.catalog_darkpool_observations(T, [a, b], retrieved_at="2026-10-15T15:00:00+00:00")
    cat.catalog_darkpool_observations(T, [{**a, "canceled": True}], retrieved_at="2026-10-15T16:00:00+00:00")
    cat.catalog_darkpool_observations(T, [a], retrieved_at="2026-10-15T17:00:00+00:00")       # reinstated
    cat.catalog_darkpool_observations(T, [{**b, "id": "uw-b"}], retrieved_at="2026-10-15T18:00:00+00:00")  # 2nd sid
    w, c, d = _counts(cat)
    assert len(w) == len(c) == len(d) == 2, (len(w), len(c), len(d))


# R4-5 (INFO) the readers pick DIFFERENT versions of the same print: _iter_window (baseline, curves, gap check)
# serves the OLDEST non-canceled row, the cache readers the NEWEST version. They differ only when a later
# version carries different non-key fields (premium is not part of the print key), e.g. a reinstatement
# reported with a corrected premium.
def test_r4_5_readers_serve_the_same_version_of_a_print(cat):
    a = _p(OPEN + timedelta(hours=1), 1, "A")
    cat.catalog_darkpool_observations(T, [a], retrieved_at="2026-10-15T15:00:00+00:00")
    cat.catalog_darkpool_observations(T, [{**a, "canceled": True}], retrieved_at="2026-10-15T16:00:00+00:00")
    cat.catalog_darkpool_observations(T, [{**a, "premium": "2500.00"}], retrieved_at="2026-10-15T17:00:00+00:00")
    w, c, d = _counts(cat)
    assert [float(x["premium"]) for x in w] == [float(x["premium"]) for x in c] == [x["premium"] for x in d], (
        [x["premium"] for x in w], [x["premium"] for x in c], [x["premium"] for x in d])


# ------------------------------------------------------------------------------------------------------
# Deployment
# ------------------------------------------------------------------------------------------------------
def _mkenv(tmp_path, monkeypatch, capsys, swallow_tag=False, candidate_tagged=False):
    import test_recovery_tool_v7 as v7
    tool = v7.load_tool()
    fake = fd.FakeDocker(v7.REFERENCE)

    def runner(cmd, *a, **k):
        if swallow_tag and list(cmd[:2]) == ["docker", "tag"]:
            return subprocess.CompletedProcess(cmd, 0, b"", b"")         # R3D-2 path disabled: tag never moves
        return fake(cmd, *a, **k)
    monkeypatch.setattr(tool, "subprocess", types.SimpleNamespace(run=runner, CompletedProcess=subprocess.CompletedProcess))
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "compose.yml").write_text("services: {}\n")
    dep = ["--project", "p", "--service", "s"]
    e = types.SimpleNamespace(tool=tool, fake=fake, bk=bk)

    def cli(*args):
        capsys.readouterr()
        rc = tool.main([str(x) for x in args])
        out = capsys.readouterr()
        e.last_out, e.last_err = out.out, out.err
        return rc

    def u4():
        return sorted(p for p in bk.iterdir() if p.name.startswith("U4-") and p.is_dir())[-1]

    def restored():
        assert cli("snapshot", *dep, "--backup-root", bk, "--helper-image", fd.HELPER, "--stop") == 0, e.last_err
        assert cli("verify", "--u4", u4()) == 0, e.last_err
        assert cli("test-restore", "--u4", u4(), "--helper-image", fd.HELPER) == 0, e.last_err
        c = fake.service_container()
        c["Image"] = fd.CANDIDATE
        c["State"] = {"Status": "running", "Running": True, "Restarting": False}
        if candidate_tagged:                                       # what the U6 build really does to :latest
            fake.image_tags[fd.OLD_IMAGE].remove("p-s:latest")
            fake.image_tags.setdefault(fd.CANDIDATE, []).append("p-s:latest")
        fake.mutate()
        fake.volumes[fake.volume]["content"]["sqlite"]["tip_hackathon.db"]["schema_versions"].append([10, "x"])
        assert cli("rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE,
                   "--dry-run") == 0, e.last_err
        tok = json.loads(e.last_out)["approval_token"]
        assert cli("rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE,
                   "--approve", tok, "--stop") == 0, e.last_err

    def start():
        return cli("start-rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image",
                   fd.OLD_IMAGE, "--compose-file", bk / "compose.yml", "--quiet-seconds", "0")
    e.restored, e.start = restored, start
    return e


def _v7_assertion(fake):
    return ("p-s:latest" in fake.image_tags.get(fd.OLD_IMAGE, [])
            and not any("p-s:latest" in t for i, t in fake.image_tags.items() if i != fd.OLD_IMAGE))


# Sound check: with the realistic pre-state (U6 moved :latest to the candidate) start-rollback moves it back,
# leaves the hold tag in place, and a second run refuses (container running) without touching tags.
def test_sound_start_rollback_moves_latest_back_from_the_candidate(tmp_path, monkeypatch, capsys):
    e = _mkenv(tmp_path, monkeypatch, capsys, candidate_tagged=True)
    e.restored()
    assert "p-s:latest" in e.fake.image_tags[fd.CANDIDATE]
    assert e.start() == 0, e.last_err
    assert _v7_assertion(e.fake) and "tip-r3-hold:147e315" in e.fake.image_tags[fd.OLD_IMAGE]
    assert e.start() != 0 and _v7_assertion(e.fake)


# R4-6 (LOW, test quality) the R3D-2 assertion added to test_recovery_tool_v7::test_full_positive_path is
# vacuous: the fake starts with p-s:latest on OLD_IMAGE and prepared() never moves it to the candidate, so it
# holds even when no `docker tag` ever runs (shown here by swallowing every tag command).
def test_r4_6_v7_positive_path_assertion_detects_a_missing_retag(tmp_path, monkeypatch, capsys):
    e = _mkenv(tmp_path, monkeypatch, capsys, swallow_tag=True, candidate_tagged=True)   # as v7 prepared() now does
    e.restored()
    assert e.start() == 0, e.last_err
    assert not _v7_assertion(e.fake), "v7's R3D-2 assertion passes although no `docker tag` ran"


# R4-7 (LOW/MED) the drill instruction added in 2c7cc85 builds the drill candidate with a plain
# `docker build -t r3drill-candidate:<commit> .`: no --build-arg, so /srv/VERSION says BUILD_COMMIT=unknown and
# the drill's candidate acceptance (accept --expect-commit <sha>, expected exit 0) cannot pass. Since 2c7cc85 a
# short --candidate-commit is refused too (the 7206583 drill image on this host is stamped with the SHORT
# commit "7206583", so a re-run of that drill now fails).
def test_r4_7_runbook_drill_build_stamps_the_full_commit():
    drill = RUNBOOK[RUNBOOK.index("**Drill status.**"):RUNBOOK.index("**NOT done in production:**")]
    cmd = [ln for ln in drill.splitlines() if "docker build" in ln]
    assert cmd and all("BUILD_COMMIT" in ln for ln in cmd), cmd
    assert "40" in drill or "full" in drill.lower(), "drill text never says --candidate-commit must be the full SHA"


# R4-8 (LOW) U6-refused remedy: `.git/info/exclude` only hides UNTRACKED files; a MODIFIED tracked Compose input
# (the row says "untracked or modified") still makes the build refuse, so the remedy fails for that case.
def test_r4_8_info_exclude_remedy_covers_what_the_runbook_says(build_repo):
    r = build_repo.repo
    (r / "docker-compose.override.yml").write_text("services: {}\n")
    (r / ".git" / "info").mkdir(exist_ok=True)
    with open(r / ".git" / "info" / "exclude", "a") as f:
        f.write("docker-compose.override.yml\nREADME\n")
    rc, out, _ = build_repo.run("-p", "tip-hackathon-edition", "tip-hackathon")
    assert rc == 0, "untracked override + exclude should build: " + out          # sound part
    (r / "README").write_text("locally modified tracked input\n")
    rc, out, _ = build_repo.run("-p", "tip-hackathon-edition", "tip-hackathon")
    row = [ln for ln in RUNBOOK.splitlines() if ln.startswith("| U6 build refused")][0]
    claims_modified = "untracked or modified" in row
    assert rc == 0 or not claims_modified, "runbook offers .git/info/exclude for MODIFIED inputs; build still refused"


# R4-9 (LOW) doclint check 6 regression: upgrade wording on the command's OWN line is no longer examined
# (prose is collected from lines a..i-1 only). 80053c4 flagged this text; 2c7cc85 does not.
@pytest.mark.parametrize("text", [
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nTo update the app, run `docker compose up -d --build`.\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\n```\ndocker compose up -d --build\n```\n\n"
    "Re-run the command above whenever you update.\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nTo update to a new version you never need to stop the app, "
    "just run\n\ndocker compose up -d --build\n",
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md.\n\nTo update:\n\n"
    "`docker buildx build -t tip-hackathon-edition-tip-hackathon . && docker compose up -d`\n",
])
def test_r4_9_doclint_flags_upgrade_advice(text):
    doclint = _load("doclint_audit4", RECOVERY / "doclint.py")
    assert [f for f in doclint.lint_text("x.md", text) if "rebuild command" in f], text


def test_sound_doclint_repo_is_clean():
    doclint = _load("doclint_audit4b", RECOVERY / "doclint.py")
    assert doclint.lint_repo(REPO) == []
