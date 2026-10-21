"""Unit tests for the Release 3 recovery tool (tools/recovery). No Docker: the Docker-facing
calls are faked so the fail-closed paths that a live drill cannot trigger deterministically
are still EXECUTED (identity change after preflight, existing U4, low disk space, dry-run never
reaching a destructive step, partial-restore start refusal, exit codes). The live end-to-end
behaviour is covered by tools/recovery/drill/drill.py."""
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "recovery"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


r3 = _load("r3recover")
probe = _load("r3probe")
GOOD_DB = {"integrity_check": ["ok"], "foreign_key_violations": 0, "schema_versions": r3.SCHEMA9_MIGRATIONS,
           "migration_10_objects_present": [], "scheduler_mode": "disabled", "active_baseline_jobs": 0, "sidecars": [],
           "objects": r3.REFERENCE["states"]["9"]["objects"], "columns": r3.REFERENCE["states"]["9"]["columns"]}


def good_manifest():
    return {"entries": {".": {"type": "dir", "mode": "0o755", "uid": 0, "gid": 0},
                        "settings.json": {"type": "file", "mode": "0o644", "uid": 0, "gid": 0, "size": 2, "sha256": "a" * 64}},
            "unsupported": [], "sqlite": {"tip_hackathon.db": dict(GOOD_DB)}}


# --- comparison and semantic checks -------------------------------------------------------
@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["entries"].pop("settings.json"), "missing: settings.json"),
    (lambda m: m["entries"].__setitem__("x", {"type": "dir"}), "unexpected: x"),
    (lambda m: m["entries"]["settings.json"].__setitem__("mode", "0o600"), "settings.json: mode"),
    (lambda m: m["entries"]["settings.json"].__setitem__("uid", 1000), "settings.json: uid"),
    (lambda m: m["entries"]["settings.json"].__setitem__("sha256", "b" * 64), "settings.json: sha256"),
    (lambda m: m["sqlite"]["tip_hackathon.db"].__setitem__("integrity_check", ["bad"]), "sqlite facts differ"),
])
def test_compare_detects_every_difference(mutate, needle):
    exp, obs = good_manifest(), good_manifest()
    mutate(obs)
    assert any(needle in d for d in r3.compare(exp, obs))
    assert r3.compare(good_manifest(), good_manifest()) == []


@pytest.mark.parametrize("field,value,needle", [
    ("scheduler_mode", "enabled", "scheduler"),
    ("active_baseline_jobs", 1, "active baseline job"),
    ("schema_versions", r3.SCHEMA9_MIGRATIONS + [[10, "collection_diagnostics"]], "schema versions"),
    ("schema_versions", [[v, n + "_x"] for v, n in r3.SCHEMA9_MIGRATIONS], "migration set"),
    ("migration_10_objects_present", ["collection_diagnostics"], "migration-10"),
    ("integrity_check", ["row 3 missing"], "integrity_check"),
    ("foreign_key_violations", 2, "foreign key"),
])
def test_semantic_check_blocks(field, value, needle):
    m = good_manifest()
    m["sqlite"]["tip_hackathon.db"][field] = value
    with pytest.raises(r3.Blocked, match=needle):
        r3.semantic_check(m, expect_schema_versions=r3.SCHEMA9_VERSIONS)
    r3.semantic_check(good_manifest(), expect_schema_versions=r3.SCHEMA9_VERSIONS)


def test_semantic_check_blocks_missing_primary_and_unsupported_entries():
    m = good_manifest(); m["sqlite"] = {}
    with pytest.raises(r3.Blocked, match="primary database"):
        r3.semantic_check(m, expect_schema_versions=r3.SCHEMA9_VERSIONS)
    m = good_manifest(); m["unsupported"] = [{"path": "x.fifo", "reason": "fifo"}]
    with pytest.raises(r3.Blocked, match="unsupported"):
        r3.semantic_check(m, expect_schema_versions=r3.SCHEMA9_VERSIONS)


# --- probe on a real local filesystem -------------------------------------------------------
def test_probe_records_full_metadata_and_never_touches_the_source(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    (root / ".hidden").write_text("h")
    (root / "empty.d").mkdir()
    (root / "settings.json").write_text("{}")
    db = root / "tip_hackathon.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE schema_version (version INTEGER, name TEXT)")
    c.executemany("INSERT INTO schema_version VALUES (?, ?)", r3.SCHEMA9_MIGRATIONS)
    c.commit()                                        # connection left open: -wal/-shm exist
    try:
        os.symlink("settings.json", root / "settings.link")
        has_link = True
    except (OSError, NotImplementedError):
        has_link = False
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and not p.is_symlink()}
    m = probe.build_manifest(str(root))
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and not p.is_symlink()}
    c.close()
    assert before == after                            # the volume is never modified by the probe
    e = m["entries"]
    assert {".hidden", "empty.d", "settings.json", "tip_hackathon.db"} <= set(e)
    assert e["empty.d"]["type"] == "dir" and e["settings.json"]["sha256"] and "mode" in e[".hidden"]
    if has_link:
        assert e["settings.link"] == {**e["settings.link"], "type": "symlink", "target": "settings.json"}
    facts = m["sqlite"]["tip_hackathon.db"]
    assert facts["integrity_check"] == ["ok"] and facts["schema_versions"] == r3.SCHEMA9_MIGRATIONS
    assert "tip_hackathon.db-wal" in [Path(s).name for s in facts["sidecars"]]


def test_probe_reports_hard_links_as_unsupported(tmp_path):
    root = tmp_path / "vol"; root.mkdir()
    (root / "a").write_text("x")
    os.link(root / "a", root / "b")
    assert {u["path"] for u in probe.build_manifest(str(root))["unsupported"]} == {"a", "b"}


# --- U4 directory verification ------------------------------------------------------------------
def make_u4(tmp_path, name="U4-p-20261021T000000Z-abcd1234"):
    u4 = tmp_path / name
    u4.mkdir()
    (u4 / "volume.tar").write_bytes(b"archive")
    m = {"format": r3.FORMAT, "u4_id": name, "archive": {"sha256": r3.sha256_file(u4 / "volume.tar")}}
    (u4 / "manifest.json").write_text(json.dumps(m))
    r3.write_sums(u4)
    return u4


def test_verify_u4_accepts_a_consistent_snapshot(tmp_path):
    assert r3.verify_u4(make_u4(tmp_path))["format"] == r3.FORMAT


@pytest.mark.parametrize("tamper,needle", [
    (lambda u: (u / "volume.tar").unlink(), "incomplete"),
    (lambda u: (u / "volume.tar").write_bytes(b"other"), "checksum mismatch"),
    (lambda u: (u / "SHA256SUMS").write_text("x  volume.tar\n"), "exactly volume.tar and manifest.json"),
])
def test_verify_u4_blocks_tampering(tmp_path, tamper, needle):
    u4 = make_u4(tmp_path)
    tamper(u4)
    with pytest.raises(r3.Blocked, match=needle):
        r3.verify_u4(u4)


def test_verify_u4_blocks_renamed_dir_and_wrong_archive_hash(tmp_path):
    u4 = make_u4(tmp_path)
    moved = u4.rename(tmp_path / "U4-other")
    with pytest.raises(r3.Blocked, match="does not match directory"):
        r3.verify_u4(moved)
    u4 = make_u4(tmp_path, "U4-q")
    m = json.loads((u4 / "manifest.json").read_text()); m["archive"]["sha256"] = "0" * 64
    (u4 / "manifest.json").write_text(json.dumps(m)); r3.write_sums(u4)
    with pytest.raises(r3.Blocked, match="recorded archive SHA-256"):
        r3.verify_u4(u4)
    with pytest.raises(r3.Blocked, match="does not exist"):
        r3.verify_u4(tmp_path / "nope")


# --- images, approval, exit codes -------------------------------------------------------------------
def test_tags_and_names_are_refused_without_calling_docker(monkeypatch):
    monkeypatch.setattr(r3, "run", lambda *a, **k: pytest.fail("docker must not be called for a tag"))
    for ref in ("tip:latest", "python:3.11-slim", "sha256:abc", ""):
        with pytest.raises(r3.Blocked, match="immutable image ID"):
            r3.require_local_image_id(ref, "image")


def _plan_fixture(tmp_path):
    u4 = tmp_path / "U4-x"; u4.mkdir()
    for n in ("manifest.json", "volume.tar", "test_restore.json"):
        (u4 / n).write_text(n)
    m = {"u4_id": "U4-x"}
    return u4, m, r3._binding(u4, m, ident())


def test_approval_token_is_one_time_and_bound_to_the_state(tmp_path):
    u4, m, b = _plan_fixture(tmp_path)
    plan = r3._new_plan(u4, b)
    assert plan["token"].startswith("RESTORE:U4-x:") and plan["token"].endswith(plan["nonce"])
    assert isinstance(r3._consume_token(u4, plan["token"], b), r3.Authorization)
    with pytest.raises(r3.Blocked, match="already used"):
        r3._consume_token(u4, plan["token"], b)                     # replay
    plan2 = r3._new_plan(u4, b)
    with pytest.raises(r3.Blocked, match="changed since the approval"):
        r3._consume_token(u4, plan2["token"], {**b, "container_id": "other"})
    (u4 / "volume.tar").write_text("different archive")              # archive digest changes the binding
    with pytest.raises(r3.Blocked, match="changed since the approval"):
        r3._consume_token(u4, r3._new_plan(u4, b)["token"], r3._binding(u4, m, ident()))
    with pytest.raises(r3.Blocked, match="malformed"):
        r3._consume_token(u4, "RESTORE:U4-x:p_tip-data", b)          # the v6 token format is refused


def test_expired_token_is_refused(tmp_path, monkeypatch):
    u4, m, b = _plan_fixture(tmp_path)
    plan = r3._new_plan(u4, b)
    monkeypatch.setattr(r3.time, "time", lambda: plan["expires_epoch"] + 1)
    with pytest.raises(r3.Blocked, match="expired"):
        r3._consume_token(u4, plan["token"], b)


def test_exit_codes_never_report_success_after_a_failure(monkeypatch):
    assert r3.main(["rollback", "--project", "p", "--service", "s", "--u4", "u", "--helper-image", "h"]) == r3.EXIT_USAGE
    assert r3.main(["rollback", "--project", "p", "--service", "s", "--u4", "u", "--helper-image", "h",
                    "--dry-run", "--approve", "t"]) == r3.EXIT_USAGE
    monkeypatch.setitem(r3.COMMANDS, "verify", lambda a: (_ for _ in ()).throw(r3.Blocked("x")))
    assert r3.main(["verify", "--u4", "u"]) == r3.EXIT_BLOCKED
    monkeypatch.setitem(r3.COMMANDS, "verify", lambda a: 1 / 0)
    assert r3.main(["verify", "--u4", "u"]) == r3.EXIT_INTERNAL
    monkeypatch.setitem(r3.COMMANDS, "verify", lambda a: 7)            # any non-zero handler result is not success
    assert r3.main(["verify", "--u4", "u"]) == r3.EXIT_INTERNAL


# --- orchestration paths with a faked Docker layer ------------------------------------------------------
def ident(created="2026-10-21T00:00:00Z", running=False, cid="c1", labels=None):
    return {"project": "p", "service": "s", "image_id": "sha256:" + "1" * 64,
            "container": {"id": cid, "name": "p-s-1", "status": "exited" if not running else "running",
                          "running": running, "restarting": False},
            "volume": {"name": "p_tip-data", "driver": "local", "created_at": created, "mountpoint": "/m",
                       "labels": {"com.docker.compose.project": "p", **(labels or {})}, "destination": "/srv/data"},
            "runtime": {"ports": {}}, "compose_labels": {}, "other_volume_users": []}


class SnapArgs:
    project, service, expect_volume, helper_image = "p", "s", None, "sha256:" + "2" * 64
    expect_commit, stop = r3.EXPECTED_COMMIT_DEFAULT, True


def _fake_images(monkeypatch):
    monkeypatch.setattr(r3, "require_local_image_id", lambda i, w: i)
    monkeypatch.setattr(r3, "image_commit", lambda i: r3.EXPECTED_COMMIT_DEFAULT)


def test_snapshot_aborts_when_identity_changes_after_preflight(monkeypatch, tmp_path):
    _fake_images(monkeypatch)
    seq = iter([ident(running=True), ident(created="2099-01-01T00:00:00Z")])
    monkeypatch.setattr(r3, "discover", lambda *a, **k: next(seq))
    monkeypatch.setattr(r3, "run", lambda *a, **k: None)                  # 'docker stop'
    monkeypatch.setattr(r3, "probe_volume", lambda *a: pytest.fail("must abort before probing"))
    a = SnapArgs(); a.backup_root = str(tmp_path)
    with pytest.raises(r3.Blocked, match="identity changed"):
        r3.cmd_snapshot(a)
    assert list(tmp_path.iterdir()) == []                                 # no U4 created


def test_snapshot_requires_stop_flag_for_a_running_app(monkeypatch, tmp_path):
    _fake_images(monkeypatch)
    monkeypatch.setattr(r3, "discover", lambda *a, **k: ident(running=True))
    a = SnapArgs(); a.backup_root = str(tmp_path); a.stop = False
    with pytest.raises(r3.Blocked, match="pass --stop"):
        r3.cmd_snapshot(a)


def test_snapshot_blocks_on_low_disk_and_never_overwrites_a_u4(monkeypatch, tmp_path):
    _fake_images(monkeypatch)
    monkeypatch.setattr(r3, "discover", lambda *a, **k: ident())
    monkeypatch.setattr(r3, "probe_volume", lambda *a: good_manifest())
    a = SnapArgs(); a.backup_root = str(tmp_path)

    class DU: free = 10
    monkeypatch.setattr(r3.shutil, "disk_usage", lambda p: DU)
    with pytest.raises(r3.Blocked, match="insufficient free space"):
        r3.cmd_snapshot(a)
    DU.free = 10 ** 15
    monkeypatch.setattr(r3.secrets, "token_hex", lambda n: "deadbeef")

    class FixedDT(r3._dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return r3._dt.datetime(2026, 10, 21, tzinfo=r3._dt.timezone.utc)
    monkeypatch.setattr(r3._dt, "datetime", FixedDT)
    (tmp_path / "U4-p-20261021T000000Z-deadbeef").mkdir()
    with pytest.raises(r3.Blocked, match="already exists; refusing to overwrite"):
        r3.cmd_snapshot(a)


def test_rollback_dry_run_never_reaches_a_destructive_step(monkeypatch, tmp_path):
    u4, m, _ = _plan_fixture(tmp_path)
    monkeypatch.setattr(r3, "_rollback_checks", lambda a: (m | {"archive": {"sha256": "a" * 64}}, "sha256:" + "2" * 64,
                                                           ident(running=True)))
    for fn in ("_extract", "helper_run", "_write_state", "_consume_token"):
        monkeypatch.setattr(r3, fn, lambda *x, **k: pytest.fail(f"dry-run reached {fn}"))
    monkeypatch.setattr(r3, "run", lambda *x, **k: pytest.fail("dry-run must not stop anything"))

    u4_dir = str(u4)

    class A: dry_run, approve, stop, u4, project = True, None, False, u4_dir, "p"
    assert r3.cmd_rollback(A) == r3.EXIT_OK
    assert len(list(u4.glob("rollback-plan-*.json"))) == 1 and not list(u4.glob("*.consumed"))
    assert not (u4 / "restore_state.json").exists() and not list(tmp_path.glob(".r3recover-*.lock"))


def test_start_rollback_refuses_partial_or_unrestored_volumes(monkeypatch, tmp_path):
    u4 = tmp_path / "U4-x"; u4.mkdir()
    (u4 / "manifest.json").write_text("{}")
    monkeypatch.setattr(r3, "verify_u4", lambda p: {"identity": ident(), "helper_image": "h"})

    class A: u4, project = str(tmp_path / "U4-x"), "p"
    with pytest.raises(r3.Blocked, match="only RESTORED_VERIFIED"):
        r3.cmd_start_rollback(A)
    (u4 / "restore_state.json").write_text(json.dumps({"state": "BLOCKED_PARTIAL"}))
    with pytest.raises(r3.Blocked, match="BLOCKED_PARTIAL"):
        r3.cmd_start_rollback(A)
    (u4 / "restore_state.json").write_text(json.dumps({"state": "RESTORED_VERIFIED", "manifest_sha256": "0" * 64}))
    with pytest.raises(r3.Blocked, match="does not belong to this exact U4"):
        r3.cmd_start_rollback(A)


# --- documentation lint: active docs never offer an unauthorized recovery path ------------------------
doclint = _load("doclint")


def test_active_documentation_is_clean():
    assert doclint.lint_repo(TOOLS.parents[1]) == []


@pytest.mark.parametrize("line", [
    "Rollback: code-only rollback to the old image is fine.",
    "Restore the pre-migration .bak over the DB.",
    "Run the SQL zero-count proof first.",
    "Keep the candidate writes after rollback.",
    "A full restore is optional.",
    "No recovery needed if nothing was collected.",
    "Delete the Demo database before starting.",
    "docker tag x tip:rollback-147e315",
])
def test_doclint_flags_unauthorized_recovery_guidance(line):
    assert doclint.lint_text("x.md", f"See {doclint.RUNBOOK}.\n{line}\n")


def test_doclint_requires_runbook_link_history_banner_and_no_active_patches(tmp_path):
    assert doclint.lint_text("x.md", "How to roll back: ...\n")
    (tmp_path / "docs" / "history").mkdir(parents=True)
    (tmp_path / "docs" / "history" / "old.md").write_text("# Old notes\ncode-only rollback works\n")
    assert any("HISTORICAL RECORD" in f for f in doclint.lint_repo(tmp_path))
    h = tmp_path / "handoff"; (h / "historical").mkdir(parents=True)
    (h / "0007-old.patch").write_text("x"); (h / "historical" / "0001.patch").write_text("x")
    found = doclint.lint_handoff(h)
    assert any("0007-old.patch" in f for f in found) and any("WARNING.md" in f for f in found)
    assert not any("historical/0001.patch" in f for f in found)


def test_drill_fault_hook_is_ignored_outside_labelled_drill_volumes(monkeypatch):
    monkeypatch.setenv(r3.DRILL_FAULT_ENV, "interrupt_extract")
    assert r3._drill_fault(ident()) is None
    assert r3._drill_fault(ident(labels={"r3.drill": "1"})) == "interrupt_extract"
    assert r3._drill_fault(ident(labels={"r3.drill": "yes"})) is None


def test_schema_reference_matches_the_application_migrations_1_to_9():
    """The checked-in reference (generated inside the 147e315 image) must equal what the repository's
    storage schema + migrations 1-9 create, i.e. migrations 1-9 are unchanged."""
    ref = _load("schema_reference").build(str(Path(__file__).resolve().parents[1]))
    assert ref == r3.REFERENCE


def test_runtime_fingerprint_treats_unset_values_alike_but_not_real_settings():
    base = {"Id": "a" * 64, "Name": "/p-s-1", "Config": {"Cmd": ["x"]}, "HostConfig": {"OomKillDisable": None, "Memory": 0},
            "Mounts": [], "NetworkSettings": {"Networks": {}}}
    same = {**base, "HostConfig": {"OomKillDisable": False, "Memory": None}}
    assert r3.fingerprint_diff(r3.runtime_fingerprint(base), r3.runtime_fingerprint(same)) == []
    real = {**base, "HostConfig": {"OomKillDisable": True, "Memory": 1 << 30}}
    d = r3.fingerprint_diff(r3.runtime_fingerprint(base), r3.runtime_fingerprint(real))
    assert any("OomKillDisable" in x for x in d) and any("Memory" in x for x in d)
