"""Audit 3 (deployment safety) reproductions at 7206583. Each test asserts the CORRECT behaviour and
FAILS while the defect exists, except the tests marked "sound check", which document behaviour that was
verified correct. No Docker, no network: real-Docker evidence is described in the audit report."""
from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_deploy_audit_regressions import BASH, build_repo  # noqa: E402,F401  (fixture)

REPO = Path(__file__).resolve().parents[2]
RUNBOOK = (REPO / "docs" / "RELEASE3_RECOVERY_RUNBOOK.md").read_text(encoding="utf-8")
RECOVERY = REPO / "tools" / "recovery"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _section(text, start, end):
    a = text.index(start)
    return text[a:text.index(end, a)]


# ---------------------------------------------------------------------------------------------------
# Docker's .dockerignore semantics (moby/patternmatcher): patterns are relative to the context root,
# '*' never crosses '/', '**' crosses any number of directories, a match on a parent directory excludes
# everything below it, and later '!' lines re-include.
# ---------------------------------------------------------------------------------------------------
def _pattern_regex(p):
    p = p.strip().lstrip("/")
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif p.startswith("**", i):
            out += ".*"
            i += 2
        elif p[i] == "*":
            out += "[^/]*"
            i += 1
        elif p[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(p[i])
            i += 1
    return re.compile(f"^{out}$")


def _dockerignored(path, lines):
    parts = path.split("/")
    candidates = ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
    excluded = False
    for raw in lines:
        neg = raw.startswith("!")
        rx = _pattern_regex(raw[1:] if neg else raw)
        if any(rx.match(c) for c in candidates):
            excluded = not neg
    return excluded


def _dockerignore_lines():
    return [ln.strip() for ln in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_matcher_agrees_with_the_patterns_the_repo_relies_on():
    lines = _dockerignore_lines()
    assert _dockerignored(".env", lines)
    assert not _dockerignored(".env.example", lines)
    assert _dockerignored("backend/data/settings.json", lines)
    assert _dockerignored("backend/app/__pycache__/x.pyc", lines)
    assert not _dockerignored("backend/app/main.py", lines)
    assert not _dockerignored("fixtures/scenario_quiet_control.json", lines)


# R3D-1: nested .env files are git-ignored (so build_with_version.sh's clean-tree check passes), are NOT
# excluded by .dockerignore ('.env' only matches the context root), are COPYed into the image, and
# app/config.py's load_dotenv() finds /srv/backend/app/.env or /srv/backend/.env at runtime.
@pytest.mark.parametrize("path", ["backend/.env", "backend/app/.env", "mcp_isolated/.env", "fixtures/.env",
                                  "frontend/.env"])
def test_r3d1_nested_env_files_never_enter_the_image(path):
    assert _dockerignored(path, _dockerignore_lines()), f"{path} would be copied into the image"


# R3D-2: after a successful R3 the deployment's Compose directory is still checked out at the candidate and
# <project>-<service>:latest is the candidate image, so any later plain `docker compose up -d` (the natural
# restart: the service has no restart policy) silently recreates the container from the CANDIDATE image on
# the restored schema-9 volume (Compose's config hash differs from the rollback override's). The runbook
# must close that window after a rollback.
def test_r3d2_runbook_returns_compose_dir_and_latest_tag_to_147e315_after_rollback():
    sec = _section(RUNBOOK, "## 6. Rollback", "## 8.")
    restores_checkout = re.search(r"(git checkout|check(ed)? out)[^\n]{0,160}147e315", sec, re.I)
    restores_tag = re.search(r"docker tag \$OLD [^\s`]*tip-hackathon:latest", sec)
    forbids_up = re.search(r"(after (the|a) rollback|R4)[^\n]{0,200}docker compose up", sec, re.I)
    assert (restores_checkout or restores_tag) and forbids_up, \
        "no post-rollback step pins the Compose directory / :latest tag back to 147e315 or forbids `compose up`"


# R3D-3: an untracked file in <COMPOSE_DIR> (e.g. a docker-compose.override.yml, which .gitignore does not
# cover and which E13 says must stay exactly as it was) makes U6's build refuse. The runbook's remedy ("get a
# clean checkout") contradicts U6/E13 ("never run U6 from another checkout"; inputs unchanged), so the
# operator has no safe way forward. The script behaviour is CONFIRMED here; the runbook must give a remedy
# that keeps the recorded Compose inputs in place.
def test_r3d3_untracked_compose_override_has_a_safe_u6_path(build_repo):
    (build_repo.repo / "docker-compose.override.yml").write_text("services: {}\n")
    rc, out, calls = build_repo.run("-p", "tip-hackathon-edition", "tip-hackathon")
    assert rc == 1 and "docker-compose.override.yml" in out and not [c for c in calls if " build" in c]
    row = [ln for ln in RUNBOOK.splitlines() if ln.startswith("| U6 build refused")][0]
    assert re.search(r"override|info/exclude|\.env", row, re.I), row


# R3D-4: the runbook (E14, section 7) says a TIP_ALLOW_DIRTY_BUILD image "stamps <commit>-dirty, which U7
# rejects", but accept only compares the stamp with --expect-commit: an operator who copies the stamp the
# app reports passes U7 with a dirty build.
def test_r3d4_acceptance_refuses_a_dirty_build_stamp():
    tool = _load("r3recover_audit3", RECOVERY / "r3recover.py")
    out = {"health": True, "version": "a" * 40 + "-dirty", "scheduler": "disabled", "job_active": False,
           "uw_attempts": [0, 0], "schema_versions": list(range(1, 12)), "integrity": "ok"}
    assert tool.acceptance_errors(out, "a" * 40 + "-dirty", list(range(1, 12))), "a -dirty stamp was accepted"


# R3D-5: doclint check 6 is bypassed by any negation word on the upgrade line, by a plain `docker build`,
# and by upgrade wording more than two lines above the command.
@pytest.mark.parametrize("text", [
    "## Install\nUpgrades: RELEASE3_RECOVERY_RUNBOOK.md\n\nTo update to a new version (no downtime):\n"
    "docker compose up -d --build\n",
    "## Install\nUpgrades: RELEASE3_RECOVERY_RUNBOOK.md\n\nTo update:\n"
    "docker build -t tip-hackathon-edition-tip-hackathon . && docker compose up -d\n",
    "## Install\nUpgrades: RELEASE3_RECOVERY_RUNBOOK.md\n\nTo update an installed app:\n\n1. git fetch\n"
    "2. then run\n\ndocker compose up -d --build\n",
])
def test_r3d5_doclint_flags_disguised_rebuild_upgrade_advice(text):
    doclint = _load("doclint_audit3", RECOVERY / "doclint.py")
    assert [f for f in doclint.lint_text("x.md", text) if "rebuild command" in f], text


# R3D-6: on this host's Docker 29 containerd image store a Compose/BuildKit build that moves
# <project>-<service>:latest makes the previous image unresolvable by ID IMMEDIATELY (no prune needed; seen
# in a disposable r3audit3 project, even while a running container used it). The runbook asks for the drill
# to be re-run before U1 with a candidate image but places nothing before the hold tag (U0); building the
# candidate in <COMPOSE_DIR> (the image U6 will deploy) before U0 loses the only 147e315 image.
def test_r3d6_hold_tag_is_required_before_any_candidate_or_drill_build():
    drill = _section(RUNBOOK, "**Drill status.**", "**NOT done in production:**")
    e8 = [ln for ln in RUNBOOK.splitlines() if ln.startswith("| E8 ")][0]
    assert "U0" in drill or re.search(r"before (any|the first|every)[^|]{0,40}build", e8, re.I), \
        "nothing requires the hold tag before the drill/candidate build"
    assert "prune" not in e8 or re.search(r"(immediately|without (a|any) prune|as soon as)", e8, re.I), \
        "E8 says only a prune deletes the untagged image; a Compose build already does"


# Sound check (passes): migration-10/11 objects, including the ten session_coverage columns alone, are
# detected in an otherwise schema-9 database by r3probe and refused by semantic_check.
@pytest.mark.parametrize("drop_table", [False, True])
def test_sound_migration_11_objects_are_detected_in_a_schema9_volume(tmp_path, drop_table):
    sys.path.insert(0, str(REPO / "backend"))
    from app import migrations, storage
    probe = _load("r3probe_audit3", RECOVERY / "r3probe.py")
    tool = _load("r3recover_audit3b", RECOVERY / "r3recover.py")
    root = tmp_path / "vol"
    root.mkdir()
    conn = sqlite3.connect(root / "tip_hackathon.db")
    conn.executescript(storage.SCHEMA)
    migrations._ensure_schema_version_table(conn)
    for m in sorted(migrations.MIGRATIONS, key=lambda m: m.version):
        if m.version <= 9 or m.version == 11:
            conn.executescript(m.sql)
        if m.version <= 9:
            conn.execute("insert into schema_version(version, name, applied_at) values (?, ?, 'x')", (m.version, m.name))
    if drop_table:
        conn.executescript("drop index idx_coverage_reconciliations_session; drop table coverage_reconciliations;")
    conn.execute("insert or replace into scheduler_state(id, mode, updated_at) values (1, 'disabled', 'x')")
    conn.commit()
    conn.close()
    facts = probe.build_manifest(str(root))["sqlite"]["tip_hackathon.db"]
    assert any(o.startswith("session_coverage.") for o in facts["migration_11_objects_present"])
    assert ("coverage_reconciliations" in facts["migration_11_objects_present"]) is (not drop_table)
    with pytest.raises(tool.Blocked, match="migration-11 objects present"):
        tool.semantic_check(probe.build_manifest(str(root)))
