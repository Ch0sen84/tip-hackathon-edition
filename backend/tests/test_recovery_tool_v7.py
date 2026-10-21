"""Release 3 recovery tool: v7 regression tests for the ten defects found in the v6 review.

Every test drives the tool's CLI (`main([...])`) against r3_fake_docker.FakeDocker, which replaces the
tool's subprocess calls. Because nothing tool-internal is patched, the same tests run against ANY version
of the tool: set R3_TOOL_DIR to an extracted tools/recovery directory (e.g. from 80a9305) to reproduce the
v6 defects -- those runs are expected to FAIL; against the current tool every test must pass."""
import copy
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r3_fake_docker as fd  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TOOL_DIR = Path(os.environ.get("R3_TOOL_DIR") or REPO / "tools" / "recovery")
REFERENCE = json.loads((REPO / "tools" / "recovery" / "schema9_reference.json").read_text(encoding="utf-8"))


def load_tool():
    spec = importlib.util.spec_from_file_location(f"r3recover_{abs(hash(str(TOOL_DIR)))}", TOOL_DIR / "r3recover.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    tool = load_tool()
    fake = fd.FakeDocker(REFERENCE)
    monkeypatch.setattr(tool, "subprocess", types.SimpleNamespace(run=fake, CompletedProcess=subprocess.CompletedProcess))
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "compose.yml").write_text("services: {}\n")

    class E:
        pass
    e = E()
    e.tool, e.fake, e.bk, e.tmp, e.capsys = tool, fake, bk, tmp_path, capsys
    dep = ["--project", "p", "--service", "s"]

    def cli(*args):
        capsys.readouterr()
        rc = tool.main([str(x) for x in args])
        out = capsys.readouterr()
        e.last_out, e.last_err = out.out, out.err
        return rc

    def snapshot(root=None):
        return cli("snapshot", *dep, "--backup-root", root if root is not None else bk, "--helper-image", fd.HELPER, "--stop")

    def u4():
        dirs = sorted(p for p in bk.iterdir() if p.name.startswith("U4-") and p.is_dir())
        return dirs[-1]

    def prepared():
        """U4 taken, verified and test-restored; the candidate then ran and wrote to the volume."""
        assert snapshot() == 0, e.last_err
        assert cli("verify", "--u4", u4()) == 0, e.last_err
        assert cli("test-restore", "--u4", u4(), "--helper-image", fd.HELPER) == 0, e.last_err
        c = fake.service_container()
        c["Image"] = fd.CANDIDATE
        c["State"] = {"Status": "running", "Running": True, "Restarting": False}
        fake.mutate()
        fake.volumes[fake.volume]["content"]["sqlite"]["tip_hackathon.db"]["schema_versions"].append([10, "collection_diagnostics"])
        return u4()

    def dry_run():
        rc = cli("rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE, "--dry-run")
        assert rc == 0, e.last_err
        return json.loads(e.last_out)["approval_token"]

    def approve(token):
        return cli("rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE,
                   "--approve", token, "--stop")

    def start():
        return cli("start-rollback", *dep, "--u4", u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE,
                   "--compose-file", bk / "compose.yml", "--quiet-seconds", "0")

    def restored():
        prepared()
        assert approve(dry_run()) == 0, e.last_err

    e.cli, e.snapshot, e.u4, e.prepared, e.dry_run, e.approve, e.start, e.restored, e.dep = \
        cli, snapshot, u4, prepared, dry_run, approve, start, restored, dep
    return e


def running(fake):
    c = fake.service_container()
    return bool(c and c["State"]["Running"])


# --- positive path (the corrected tool must still work end to end) -----------------------------
def test_full_positive_path(env):
    env.restored()
    assert env.start() == 0, env.last_err
    assert running(env.fake) and env.fake.service_container()["Image"] == fd.OLD_IMAGE
    assert env.fake.volumes[env.fake.volume]["content"] == fd.good_volume_manifest(REFERENCE)


# --- finding 2: probe-to-start race, lock, other volume users ------------------------------------
def test_volume_change_after_compose_create_blocks_start(env):
    env.restored()
    env.fake.hooks["after_compose_create"] = lambda f: f.mutate()
    assert env.start() == 2
    assert env.fake.started() == [] and not running(env.fake)


def test_stopped_container_referencing_the_volume_blocks_start(env):
    env.restored()
    env.fake.add_container("intruder", running=False)
    assert env.start() == 2
    assert env.fake.started() == []


def test_operation_lock_excludes_a_concurrent_operation(env):
    (env.bk / ".r3recover-p.lock").write_text("{}")
    assert env.snapshot() == 2
    assert not [c for c in env.fake.calls if c[:2] == ["docker", "stop"]]
    assert "another recovery operation" in env.last_err


# --- finding 3: a failed acceptance never leaves the rollback application running ----------------
@pytest.mark.parametrize("change", [{"scheduler": "enabled"}, {"version": "0" * 40},
                                    {"schema_versions": list(range(1, 11))}, {"uw_attempts": [0, 3]},
                                    {"job_active": True}, {"integrity": "bad"}])
def test_failed_acceptance_stops_the_rollback_container(env, change):
    env.restored()
    env.fake.accept_output = {**env.fake.accept_output, **change}
    assert env.start() == 2
    assert not running(env.fake), "the rollback container must be stopped after a failed acceptance"
    assert env.fake.calls[-1][:2] != ["docker", "start"]


@pytest.mark.parametrize("failure", ["garbage", "exception"])
def test_acceptance_errors_stop_the_rollback_container(env, failure):
    env.restored()
    if failure == "garbage":
        env.fake.accept_output = lambda f: f._ok(b"not json", rc=1, err=b"Traceback")
    else:
        def boom(f):
            raise RuntimeError("exec transport failure")
        env.fake.accept_output = boom
    assert env.start() in (2, 3)
    assert not running(env.fake)


def test_unconfirmed_stop_after_failed_acceptance_is_critical(env):
    env.restored()
    env.fake.accept_output = {**env.fake.accept_output, "scheduler": "enabled"}
    env.fake.stop_fails = True
    assert env.start() == 4
    assert "CRITICAL" in env.last_err


def test_standalone_accept_failure_stops_the_container(env):
    env.restored()
    assert env.start() == 0
    env.fake.accept_output = {**env.fake.accept_output, "scheduler": "enabled"}
    assert env.cli("accept", *env.dep, "--expect-volume", env.fake.volume, "--expect-image", fd.OLD_IMAGE,
                   "--quiet-seconds", "0") == 2
    assert not running(env.fake)


# --- finding 4: complete runtime fingerprint ------------------------------------------------------
def _set(path, value):
    def f(c):
        o = c
        for k in path[:-1]:
            o = o[k]
        o[path[-1]] = value
    return f


@pytest.mark.parametrize("mutation", [
    _set(["Config", "Cmd"], ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]),
    _set(["Config", "Entrypoint"], ["/bin/sh", "-c"]),
    _set(["Config", "WorkingDir"], "/tmp"),
    _set(["Config", "Healthcheck"], {"Test": ["CMD", "true"]}),
    _set(["Config", "User"], "0:0"),
    _set(["Config", "Env"], ["BIND_HOST=0.0.0.0", "BIND_PORT=8000", "TIP_MODE=live"]),
    _set(["HostConfig", "PortBindings"], {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}]}),
    _set(["HostConfig", "SecurityOpt"], ["seccomp=unconfined"]),
    _set(["HostConfig", "Privileged"], True),
    _set(["HostConfig", "CapAdd"], ["SYS_ADMIN"]),
    _set(["HostConfig", "PidMode"], "host"),
    _set(["HostConfig", "NetworkMode"], "host"),
    _set(["NetworkSettings", "Networks", "p_default", "Aliases"], ["p-s-1", "s", "api.example"]),
    _set(["NetworkSettings", "Networks", "p_default", "IPAMConfig"], {"IPv4Address": "172.30.0.9"}),
    _set(["Mounts", 0, "RW"], False),
], ids=["cmd", "entrypoint", "workdir", "healthcheck", "user", "env", "ports", "security_opt", "privileged",
        "cap_add", "pid_mode", "network_mode", "aliases", "ipam", "mount_rw"])
def test_runtime_difference_blocks_start(env, mutation):
    env.restored()
    env.fake.hooks["mutate_created_container"] = mutation
    assert env.start() == 2
    assert env.fake.started() == []


# --- finding 5: SQLite semantic contract for every database ------------------------------------
def _demo(env):
    return env.fake.volumes[env.fake.volume]["content"]["sqlite"]["tip_hackathon.demo.db"]


def _primary(env):
    return env.fake.volumes[env.fake.volume]["content"]["sqlite"]["tip_hackathon.db"]


@pytest.mark.parametrize("break_it", [
    lambda e: _demo(e).update(objects=[["table", "notes", "notes"]], columns={"notes": ["k"]}),
    lambda e: _demo(e).update(schema_versions=[[1, "something_else"]]),
    lambda e: _demo(e).pop("schema_versions"),
    lambda e: _demo(e).update(migration_10_objects_present=["collection_diagnostics"]),
    lambda e: _primary(e).update(objects=[o for o in _primary(e)["objects"] if o[1] != "session_coverage"]),
    lambda e: _primary(e)["columns"].update(session_coverage=["dataset"]),
    lambda e: e.fake.volumes[e.fake.volume]["content"]["sqlite"].update(
        {"extra.sqlite": {"integrity_check": ["ok"], "foreign_key_violations": 0}}),
], ids=["demo_not_a_tip_db", "demo_wrong_migration_names", "demo_no_schema_version", "demo_migration10",
        "primary_missing_table", "primary_missing_columns", "unexpected_database"])
def test_application_invalid_database_blocks_snapshot(env, break_it):
    break_it(env)
    assert env.snapshot() == 2
    assert not any(p.name.startswith("U4-") and (p / "manifest.json").exists() for p in env.bk.iterdir())


def test_demo_database_at_an_earlier_schema_state_is_accepted(env):
    ref8 = REFERENCE["states"]["8"]
    _demo(env).update(schema_versions=REFERENCE["migrations"][:8], objects=copy.deepcopy(ref8["objects"]),
                      columns=copy.deepcopy(ref8["columns"]))
    assert env.snapshot() == 0, env.last_err


# --- finding 6: snapshot source re-probe and ownership ----------------------------------------------
def test_source_change_after_archive_blocks_the_snapshot(env):
    env.fake.hooks["after_archive_check"] = lambda f: f.mutate()
    assert env.snapshot() == 2
    u4 = env.u4()
    assert not (u4 / "manifest.json").exists()
    assert env.cli("verify", "--u4", u4) == 2


def test_stopped_container_referencing_the_volume_blocks_the_snapshot(env):
    env.fake.add_container("old-helper", running=False)
    assert env.snapshot() == 2
    assert not [c for c in env.fake.calls if c[:2] == ["docker", "stop"]]


# --- finding 7: one-time, state-bound approval ------------------------------------------------------
def test_approval_token_cannot_be_replayed(env):
    env.prepared()
    token = env.dry_run()
    assert env.approve(token) == 0, env.last_err
    assert env.approve(token) == 2


def test_approval_is_void_after_the_deployment_changes(env):
    env.prepared()
    token = env.dry_run()
    c = env.fake.service_container()                       # the service container was recreated after the dry run
    del env.fake.containers[c["Id"]]
    c = copy.deepcopy(c); c["Id"] = "e" * 64
    env.fake.containers[c["Id"]] = c
    vol_before = copy.deepcopy(env.fake.volumes[env.fake.volume]["content"])
    assert env.approve(token) == 2
    assert env.fake.volumes[env.fake.volume]["content"] == vol_before


def test_approval_is_void_after_the_test_restore_is_redone(env):
    env.prepared()
    token = env.dry_run()
    assert env.cli("test-restore", "--u4", env.u4(), "--helper-image", fd.HELPER) == 0
    assert env.approve(token) == 2


def test_dry_run_does_not_consume_and_a_fabricated_token_is_refused(env):
    env.prepared()
    first = env.dry_run()
    env.dry_run()
    fake_token = first[:-32] + "0" * 32
    assert env.approve(fake_token) == 2
    assert env.approve(first) == 0, env.last_err


# --- finding 8: backup root validation ---------------------------------------------------------------
def test_relative_backup_root_is_refused(env, monkeypatch):
    monkeypatch.chdir(env.tmp)
    assert env.snapshot(root="bk") == 2
    assert not [c for c in env.fake.calls if c[:2] == ["docker", "stop"]]


def test_backup_root_inside_the_volume_mountpoint_is_refused(env):
    inside = env.tmp / "voldata" / "nested"
    inside.mkdir(parents=True)
    env.fake.volumes[env.fake.volume]["Mountpoint"] = str(env.tmp / "voldata")
    assert env.snapshot(root=inside) == 2


@pytest.mark.parametrize("kind", ["missing", "file", "unc", "docker_storage"])
def test_bad_backup_roots_are_refused(env, kind):
    root = {"missing": env.tmp / "nope", "file": env.bk / "compose.yml", "unc": "\\\\server\\share\\bk",
            "docker_storage": env.tmp / "docker-desktop" / "x"}[kind]
    if kind == "docker_storage":
        Path(root).mkdir(parents=True)
    assert env.snapshot(root=root) == 2


def test_backup_root_alias_is_refused(env):
    link = env.tmp / "alias"
    try:
        os.symlink(env.bk, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(env.bk)], check=True, capture_output=True)
    assert env.snapshot(root=link) == 2
    assert "alias" in env.last_err


# --- restore failure paths --------------------------------------------------------------------------
def test_failed_forensic_copy_blocks_before_the_volume_is_touched(env):
    env.prepared()
    token = env.dry_run()
    before = copy.deepcopy(env.fake.volumes[env.fake.volume]["content"])
    env.fake.hooks["fail_forensic"] = True
    assert env.approve(token) == 2
    assert env.fake.volumes[env.fake.volume]["content"] == before
    assert not (env.u4() / "restore_state.json").exists()


def test_interrupted_restore_is_partial_and_never_started(env, monkeypatch):
    env.fake.volumes[env.fake.volume]["Labels"]["r3.drill"] = "1"
    env.prepared()
    monkeypatch.setenv("R3_DRILL_FAULT", "interrupt_extract")
    assert env.approve(env.dry_run()) == 2
    assert json.loads((env.u4() / "restore_state.json").read_text())["state"] == "BLOCKED_PARTIAL"
    monkeypatch.delenv("R3_DRILL_FAULT")
    assert env.start() == 2 and env.fake.started() == []
    assert env.approve(env.dry_run()) == 0, env.last_err          # a full re-restore with a NEW token
    assert env.start() == 0, env.last_err
