"""Regression tests for the Release 3 deployment / recovery-tooling audit (findings F1-F16 at 345decb).

The auditor's reproductions asserted that each defect EXISTS; these assert the corrected behaviour.
Tool tests drive tools/recovery/r3recover.py's CLI against the fake Docker CLI (r3_fake_docker.py, via
test_recovery_tool_v7's `env` fixture). Shell-script tests run the real scripts under bash with a fake
`docker` on PATH (skipped without a usable bash). The real-Compose rendering test runs only
`docker compose -p <disposable> config` (client-side rendering; nothing is created) and is skipped when
Docker Compose is not installed. Nothing here touches a real container, volume or image."""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import r3_fake_docker as fd  # noqa: E402
from test_recovery_tool_v7 import REFERENCE, env, load_tool  # noqa: E402,F401  (fixture)

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
RUNBOOK = REPO / "docs" / "RELEASE3_RECOVERY_RUNBOOK.md"
INSTALL = REPO / "INSTALL_AGENT.md"
SKILL = REPO / "skills" / "tip-hackathon" / "SKILL.md"
COMPOSE = REPO / "docker-compose.yml"


def _text(p):
    return p.read_text(encoding="utf-8")


def _service(fake):
    return fake.service_container()


def _remove_service_container(fake):
    for cid in [i for i, c in fake.containers.items()
                if c["Config"]["Labels"].get("com.docker.compose.service") == fake.service]:
        del fake.containers[cid]


def _rollback(e, *extra):
    return e.cli("rollback", *e.dep, "--u4", e.u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE, *extra)


def _start_recorded(e, *extra):
    """start-rollback WITHOUT --compose-file: the U4-recorded Compose inputs are used."""
    return e.cli("start-rollback", *e.dep, "--u4", e.u4(), "--helper-image", fd.HELPER, "--rollback-image", fd.OLD_IMAGE,
                 "--quiet-seconds", "0", *extra)


def _accept(e, *extra, volume=None):
    return e.cli("accept", *e.dep, "--expect-volume", volume or e.fake.volume, "--quiet-seconds", "0", *extra)


def _candidate_accept_output(e):
    e.fake.accept_output = {**e.fake.accept_output, "version": "c" * 40, "schema_versions": list(range(1, 12))}


# =================================================================================================
# F3: a rollback path exists when the service container was removed (`docker compose down`)
# =================================================================================================
def test_f3_rollback_works_when_the_service_container_was_removed(env):
    env.prepared()                                       # U4 ok; the candidate ran and wrote (schemas 10/11)
    _remove_service_container(env.fake)                  # e.g. `docker compose down` after a failed U7
    assert _rollback(env, "--dry-run") == 0, env.last_err
    plan = json.loads(env.last_out)
    assert plan["service_container"].startswith("ABSENT") and plan["would_stop_running_container"] is None
    assert _rollback(env, "--approve", plan["approval_token"]) == 0, env.last_err
    assert env.fake.volumes[env.fake.volume]["content"] == fd.good_volume_manifest(REFERENCE)
    assert _service(env.fake) is None                    # rollback itself never creates or starts anything
    assert env.start() == 0, env.last_err                # R3 creates the container, checks it, starts it
    c = _service(env.fake)
    assert c["Image"] == fd.OLD_IMAGE and c["State"]["Running"]


def test_f3_a_container_created_after_the_dry_run_voids_the_absent_container_token(env):
    env.prepared()
    _remove_service_container(env.fake)
    assert _rollback(env, "--dry-run") == 0, env.last_err
    token = json.loads(env.last_out)["approval_token"]
    before = copy.deepcopy(env.fake.volumes[env.fake.volume]["content"])
    c = copy.deepcopy(env.fake.template)
    c.update(Id="f" * 64, State={"Status": "created", "Running": False, "Restarting": False})
    env.fake.containers[c["Id"]] = c
    assert _rollback(env, "--approve", token, "--stop") == 2
    assert "changed since the approval" in env.last_err
    assert env.fake.volumes[env.fake.volume]["content"] == before


@pytest.mark.parametrize("change", ["other_user", "recreated_volume"])
def test_f3_absent_container_path_still_requires_the_exact_u4_volume_and_no_users(env, change):
    env.prepared()
    _remove_service_container(env.fake)
    if change == "other_user":
        env.fake.add_container("leftover", running=False)
    else:
        env.fake.volumes[env.fake.volume]["CreatedAt"] = "2099-01-01T00:00:00Z"
    assert _rollback(env, "--dry-run") == 2
    assert not list(env.u4().glob("rollback-plan-*.json"))


def test_f3_preflight_and_accept_still_require_the_service_container(env):
    _remove_service_container(env.fake)
    assert env.cli("preflight", *env.dep) == 2
    assert _accept(env, "--expect-image", fd.OLD_IMAGE) == 2


# =================================================================================================
# F4: acceptance is pinned to the deployment's volume and sees a candidate running elsewhere
# =================================================================================================
def test_f4_accept_requires_expect_volume(env):
    assert env.cli("accept", *env.dep, "--expect-image", fd.OLD_IMAGE, "--quiet-seconds", "0") == 64


def test_f4_accept_refuses_a_wrong_volume(env):
    assert _accept(env, "--expect-image", fd.OLD_IMAGE, volume="p_tip-dta") == 2
    assert "not the expected" in env.last_err


def test_f4_accept_reports_a_candidate_running_under_another_project(env):
    """U6 run from a checkout whose directory name != the production project name."""
    assert env.snapshot() == 0, env.last_err             # production container now stopped
    stray = fd.container_template("tip-hackathon-edition-collector", "s", "tip-hackathon-edition-collector_tip-data",
                                  fd.CANDIDATE, "/var/lib/docker/volumes/x/_data")
    env.fake.containers[stray["Id"]] = stray            # running candidate on a NEW, empty volume
    _candidate_accept_output(env)
    rc = _accept(env, "--expect-image", fd.CANDIDATE, "--expect-commit", "c" * 40, "--expect-schema", "11")
    assert rc == 2
    assert "OUTSIDE" in env.last_err and "tip-hackathon-edition-collector-s-1" in env.last_err
    # reported, not silently ignored; the tool stops only the in-project container, never a foreign one
    assert env.fake.containers[stray["Id"]]["State"]["Running"] is True


def test_f4_rollback_acceptance_is_not_failed_by_another_deployment_of_the_rollback_release(env):
    """Drill finding (P10/N28): another container of the pinned 147e315 image elsewhere on the host (a second
    deployment, a test stack) must not fail -- and so stop -- the service a rollback has just restored. The
    stray check guards CANDIDATE acceptance only (start-rollback itself created the rollback container)."""
    other = fd.container_template("another-stack", "s", "another-stack_tip-data", fd.OLD_IMAGE,
                                  "/var/lib/docker/volumes/y/_data")
    env.fake.containers[other["Id"]] = other
    _accept(env, "--expect-image", fd.OLD_IMAGE)
    assert "OUTSIDE" not in (env.last_err or "")
    assert env.fake.containers[other["Id"]]["State"]["Running"] is True


def test_build_script_pins_the_compose_project():
    s = _text(TOOLS / "build_with_version.sh")
    assert '--project-name' in s and 'COMPOSE+=(-p "$PROJECT_NAME")' in s
    assert '"${COMPOSE[@]}" build "$@"' in s


# =================================================================================================
# F5: U4 records the Compose inputs; R3 reuses exactly them; U7 compares the runtime with U4
# =================================================================================================
@pytest.fixture
def compose_dir(env):
    d = env.tmp / "composedir"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}\n")
    (d / "docker-compose.override.yml").write_text("services: {}\n")
    (d / ".env").write_text("TIP_MODE=demo\n")
    t = env.tool
    labels = {t.LABEL_CONFIG_FILES: f"{d / 'docker-compose.yml'},{d / 'docker-compose.override.yml'}",
              t.LABEL_WORKING_DIR: str(d)}
    for c in (env.fake.template, _service(env.fake)):
        c["Config"]["Labels"].update(labels)
    return d


def _compose_calls(fake, verb):
    return [c for c in fake.calls if c[:2] == ["docker", "compose"] and verb in c]


def test_f5_u4_records_the_compose_inputs_and_r3_reuses_them(env, compose_dir):
    env.restored()
    m = json.loads((env.u4() / "manifest.json").read_text())
    ci = m["compose_inputs"]
    assert ci["config_files"] == [str(compose_dir / "docker-compose.yml"), str(compose_dir / "docker-compose.override.yml")]
    assert ci["env_files"] == [] and ci["default_env_file"] == str(compose_dir / ".env")
    assert all(ci["sha256"][p] for p in ci["sha256"])            # every input hashed (values never stored)
    assert _start_recorded(env) == 0, env.last_err
    cfg = _compose_calls(env.fake, "config")[-1]
    assert cfg[cfg.index("--project-directory") + 1] == str(compose_dir)
    files = [cfg[i + 1] for i, x in enumerate(cfg) if x == "-f"]
    assert files[:2] == ci["config_files"] and files[2].endswith("rollback.override.yml")   # the override is NOT dropped
    assert "--env-file" not in cfg                     # the implicit <project dir>/.env is loaded exactly as at U4


@pytest.mark.parametrize("break_it,needle", [
    (lambda d: (d / "docker-compose.override.yml").unlink(), "cannot be reproduced"),
    (lambda d: (d / ".env").unlink(), "implicit env file"),
], ids=["override_missing", "env_missing"])
def test_f5_r3_blocks_when_a_recorded_input_is_gone(env, compose_dir, break_it, needle):
    env.restored()
    break_it(compose_dir)
    assert _start_recorded(env) == 2
    assert needle in env.last_err and env.fake.started() == [] and not _compose_calls(env.fake, "create")


def test_f5_r3_refuses_operator_inputs_that_differ_from_u4(env, compose_dir):
    env.restored()
    assert _start_recorded(env, "--compose-file", compose_dir / "docker-compose.yml") == 2       # override omitted
    assert "differs from the Compose files" in env.last_err
    assert _start_recorded(env, "--env-file", env.tmp / "other.env") == 2
    assert env.fake.started() == []
    assert _start_recorded(env, "--compose-file", compose_dir / "docker-compose.yml",
                           "--compose-file", compose_dir / "docker-compose.override.yml",
                           "--env-file", compose_dir / ".env") == 0, env.last_err


def test_f5_snapshot_refuses_unreadable_compose_inputs_before_stopping(env):
    t = env.tool
    for c in (env.fake.template, _service(env.fake)):
        c["Config"]["Labels"][t.LABEL_CONFIG_FILES] = str(env.tmp / "gone" / "docker-compose.yml")
    assert env.snapshot() == 2
    assert "not readable" in env.last_err
    assert not [c for c in env.fake.calls if c[:2] == ["docker", "stop"]] and _service(env.fake)["State"]["Running"]


def test_f5_u7_with_u4_accepts_an_unchanged_runtime(env):
    env.prepared()                                       # candidate image on the same Compose inputs
    _candidate_accept_output(env)
    assert _accept(env, "--u4", env.u4(), "--expect-image", fd.CANDIDATE, "--expect-commit", "c" * 40,
                   "--expect-schema", "11") == 0, env.last_err


@pytest.mark.parametrize("change,needle", [
    (lambda e: _service(e.fake)["Config"].__setitem__("Env", ["BIND_HOST=127.0.0.1", "BIND_PORT=8000", "TIP_MODE=demo"]),
     "runtime differs"),
    (lambda e: _service(e.fake)["HostConfig"].__setitem__(
        "PortBindings", {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}, {"HostIp": "192.168.1.50", "HostPort": "8000"}]}),
     "runtime differs"),
    (lambda e: _service(e.fake)["Config"]["Labels"].__setitem__(e.tool.LABEL_CONFIG_FILES, "/x/docker-compose.yml"),
     "different Compose inputs"),
    (lambda e: e.fake.images.pop(fd.OLD_IMAGE), "rollback image"),
], ids=["env_changed", "override_port_added", "compose_files_changed", "rollback_image_gone"])
def test_f5_u7_with_u4_blocks_and_stops_a_candidate_that_would_make_r3_impossible(env, change, needle):
    env.prepared()
    _candidate_accept_output(env)
    change(env)
    assert _accept(env, "--u4", env.u4(), "--expect-image", fd.CANDIDATE, "--expect-commit", "c" * 40,
                   "--expect-schema", "11") == 2
    assert needle in env.last_err
    assert not _service(env.fake)["State"]["Running"]            # a failed acceptance stops the candidate


# =================================================================================================
# F6: the rollback image is verified BEFORE the destructive restore; the hold tag is mandatory
# =================================================================================================
def test_f6_rollback_requires_the_rollback_image_argument(env):
    env.prepared()
    assert env.cli("rollback", *env.dep, "--u4", env.u4(), "--helper-image", fd.HELPER, "--dry-run") == 64


@pytest.mark.parametrize("problem", ["pruned", "wrong_commit", "tag_given", "other_id"])
def test_f6_restore_never_runs_without_a_startable_rollback_image(env, problem):
    env.prepared()
    token = env.dry_run()
    before = copy.deepcopy(env.fake.volumes[env.fake.volume]["content"])
    image = fd.OLD_IMAGE
    if problem == "pruned":
        env.fake.images.pop(fd.OLD_IMAGE)
    elif problem == "wrong_commit":
        env.fake.images[fd.OLD_IMAGE] = "0" * 40
    elif problem == "tag_given":
        image = "tip-hackathon-edition-tip-hackathon:latest"
    else:
        image = fd.CANDIDATE
    n = len(env.fake.calls)
    rc = env.cli("rollback", *env.dep, "--u4", env.u4(), "--helper-image", fd.HELPER, "--rollback-image", image,
                 "--approve", token, "--stop")
    assert rc == 2
    assert env.fake.volumes[env.fake.volume]["content"] == before          # the volume was not touched
    assert not (env.u4() / "restore_state.json").exists()
    assert not list(env.u4().glob("*.consumed"))                           # the approval was not used up
    assert not [c for c in env.fake.calls[n:] if c[:2] == ["docker", "stop"]]   # the candidate was not even stopped
    assert _service(env.fake)["State"]["Running"]


def test_f6_snapshot_requires_a_hold_tag_before_stopping_anything(env):
    env.fake.image_tags[fd.OLD_IMAGE] = ["p-s:latest"]                     # only the tag the U6 build moves
    assert env.snapshot() == 2
    assert "hold tag" in env.last_err and "docker tag" in env.last_err
    assert not [c for c in env.fake.calls if c[:2] == ["docker", "stop"]]
    assert _service(env.fake)["State"]["Running"]
    env.fake.image_tags[fd.OLD_IMAGE] = ["docker.io/library/p-s:latest", "tip-r3-hold:147e315"]
    assert env.snapshot() == 0, env.last_err
    assert json.loads((env.u4() / "manifest.json").read_text())["rollback_image_hold_tags"] == ["tip-r3-hold:147e315"]


def test_f6_dry_run_reports_the_checked_rollback_image(env):
    env.prepared()
    assert _rollback(env, "--dry-run") == 0, env.last_err
    ri = json.loads(env.last_out)["rollback_image"]
    assert ri["id"] == fd.OLD_IMAGE and ri["commit"] == fd.COMMIT and "tip-r3-hold:147e315" in ri["tags"]


# =================================================================================================
# F14: free space for the forensic copy is checked before the approval token is consumed
# =================================================================================================
def test_f14_forensic_space_is_checked_before_the_token_is_consumed(env, monkeypatch):
    env.prepared()
    token = env.dry_run()
    before = copy.deepcopy(env.fake.volumes[env.fake.volume]["content"])
    real = shutil.disk_usage
    monkeypatch.setattr(env.tool.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10))
    assert env.approve(token) == 2
    assert "insufficient free space for the forensic copy" in env.last_err
    assert not list(env.u4().glob("*.consumed")) and not list(env.bk.glob("forensic-*"))
    assert env.fake.volumes[env.fake.volume]["content"] == before
    monkeypatch.setattr(env.tool.shutil, "disk_usage", real)
    assert env.approve(token) == 0, env.last_err                           # the same token still works


# =================================================================================================
# F13: the acceptance probe presents the deployment's own Host/Origin
# =================================================================================================
class _Stop(BaseException):
    pass


@pytest.mark.parametrize("envvars,host,origin", [
    ({}, "127.0.0.1:8000", "http://127.0.0.1:8000"),
    ({"BIND_HOST": "192.168.1.50", "BIND_PORT": "8080"}, "192.168.1.50:8080", "http://192.168.1.50:8080"),
    ({"BIND_HOST": "127.0.0.1", "ALLOWED_HOST": "tip.lan", "ALLOWED_ORIGIN": "https://tip.lan"}, "tip.lan:8000",
     "https://tip.lan"),
])
def test_f13_acceptance_probe_uses_the_published_host(monkeypatch, envvars, host, origin):
    import urllib.request
    tool = load_tool()
    for k in ("BIND_HOST", "BIND_PORT", "ALLOWED_HOST", "ALLOWED_ORIGIN"):
        monkeypatch.delenv(k, raising=False)
    for k, v in envvars.items():
        monkeypatch.setenv(k, v)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update(url=req.full_url, headers={k.lower(): v for k, v in req.header_items()})
        raise _Stop()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "argv", ["probe", "0"])
    with pytest.raises(_Stop):
        exec(compile(tool.ACCEPT_SNIPPET, "accept_snippet", "exec"), {"__name__": "probe"})
    assert seen["url"] == "http://127.0.0.1:8000/api/health"          # still the in-container listener
    assert seen["headers"]["host"] == host and seen["headers"]["origin"] == origin


# =================================================================================================
# F10: check_deployment_binding.sh fails a non-loopback publish while Local Mode is in effect
# =================================================================================================
def _bash():
    for c in [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]:
        if c and os.path.isfile(c) and "system32" not in c.lower() and "windowsapps" not in c.lower():
            return c
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no POSIX bash available")


def _lf_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))
    return dst


DOCKER_SHIM_BINDING = """#!/usr/bin/env bash
case "$1" in
  inspect) if [ "${2:-}" = "--format" ]; then printf '%s' "$FAKE_ENV" | tr '|' '\\n'; echo; fi; exit 0 ;;
  port) printf '%s' "$FAKE_PORTS" | tr '|' '\\n'; echo; exit 0 ;;
esac
exit 1
"""


def _run_binding(tmp_path, ports, envlines, allow_public=False):
    script = _lf_copy(TOOLS / "check_deployment_binding.sh", tmp_path / "t" / "check_deployment_binding.sh")
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    (shim / "docker").write_bytes(DOCKER_SHIM_BINDING.encode())
    os.chmod(shim / "docker", 0o755)
    e = dict(os.environ, FAKE_PORTS="|".join(f"8000/tcp -> {p}" for p in ports), FAKE_ENV="|".join(envlines),
             PATH=str(shim) + os.pathsep + os.environ.get("PATH", ""))
    e.pop("ALLOW_PUBLIC_BIND", None)
    if allow_public:
        e["ALLOW_PUBLIC_BIND"] = "1"
    r = subprocess.run([BASH, str(script), "p"], capture_output=True, text=True, env=e, timeout=60)
    return r.returncode, r.stdout + r.stderr


SECRET = "UW_API_KEY=sk-test-should-never-print"


@needs_bash
@pytest.mark.parametrize("ports,envlines,allow,rc", [
    (["127.0.0.1:8000"], ["BIND_HOST=127.0.0.1", "TIP_LOCAL_MODE=auto"], False, 0),
    # the auditor's case: an override adds a LAN address while the container stays loopback/Local Mode
    (["127.0.0.1:8000", "192.168.1.50:8000"], ["BIND_HOST=127.0.0.1", "TIP_LOCAL_MODE=auto"], False, 1),
    # INSTALL_AGENT's old advice (bare 8080:8000): every interface with Local Mode on; no opt-out
    (["0.0.0.0:8080", "[::]:8080"], ["BIND_HOST=127.0.0.1"], True, 1),
    (["192.168.1.50:8000"], ["BIND_HOST=127.0.0.1", "TIP_LOCAL_MODE=1"], False, 1),
    # Network Mode (token required): a deliberate LAN bind passes; every-interface needs the opt-in
    (["192.168.1.50:8000"], ["BIND_HOST=192.168.1.50", "TIP_LOCAL_MODE=auto"], False, 0),
    (["127.0.0.1:8000", "192.168.1.50:8000"], ["BIND_HOST=127.0.0.1", "TIP_LOCAL_MODE=0"], False, 0),
    (["0.0.0.0:8000"], ["BIND_HOST=0.0.0.0"], False, 1),
    (["0.0.0.0:8000"], ["BIND_HOST=0.0.0.0"], True, 0),
    (["[::1]:8000"], ["BIND_HOST=::1"], False, 0),
], ids=["loopback_ok", "lan_override_local_mode", "bare_port_local_mode_optin_ignored", "forced_local_mode_lan",
        "network_mode_lan", "forced_network_mode", "public_network_mode", "public_network_mode_optin", "ipv6_loopback"])
def test_f10_binding_check_against_local_mode(tmp_path, ports, envlines, allow, rc):
    got, out = _run_binding(tmp_path, ports, envlines + [SECRET], allow_public=allow)
    assert got == rc, out
    assert "sk-test-should-never-print" not in out and "UW_API_KEY" not in out
    if rc and "BIND_HOST=127.0.0.1" in envlines and "TIP_LOCAL_MODE=0" not in envlines:
        assert "Local Mode" in out
    assert "have no auth of their own" not in out                 # stale D11 wording is gone


# =================================================================================================
# F8: build_with_version.sh refuses a dirty tree (dev override stamps -dirty) and pins the project
# =================================================================================================
DOCKER_SHIM_BUILD = """#!/usr/bin/env bash
echo "ARGS:$* BUILD_COMMIT=${BUILD_COMMIT:-}" >> "$FAKE_LOG"
case " $* " in
  *" config --services "*) echo tip-hackathon ;;
  *" image inspect "*) echo "sha256:$(printf '0%.0s' $(seq 1 64))" ;;
esac
exit 0
"""


@pytest.fixture
def build_repo(tmp_path):
    if BASH is None or shutil.which("git") is None:
        pytest.skip("needs bash and git")
    repo = tmp_path / "repo"
    _lf_copy(TOOLS / "build_with_version.sh", repo / "tools" / "build_with_version.sh")
    (repo / "README").write_text("x\n")
    g = ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "init"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "docker").write_bytes(DOCKER_SHIM_BUILD.encode())
    os.chmod(shim / "docker", 0o755)
    log = tmp_path / "docker.log"

    def run(*args, **envx):
        e = dict(os.environ, FAKE_LOG=str(log), PATH=str(shim) + os.pathsep + os.environ.get("PATH", ""))
        for k in ("TIP_ALLOW_DIRTY_BUILD", "COMPOSE_PROJECT_NAME", "BUILD_COMMIT"):
            e.pop(k, None)
        e.update(envx)
        r = subprocess.run([BASH, str(repo / "tools" / "build_with_version.sh"), *args], capture_output=True, text=True,
                           env=e, timeout=120)
        calls = log.read_text().splitlines() if log.exists() else []
        if log.exists():
            log.unlink()
        return r.returncode, r.stdout + r.stderr, calls
    return types.SimpleNamespace(repo=repo, head=head, run=run)


def test_f8_clean_tree_builds_with_the_commit_and_the_pinned_project(build_repo):
    rc, out, calls = build_repo.run("-p", "tip-hackathon-edition", "tip-hackathon")
    assert rc == 0, out
    build = [c for c in calls if " build " in f" {c} "][0]
    assert build.startswith("ARGS:compose -p tip-hackathon-edition build tip-hackathon")
    assert build.endswith(f"BUILD_COMMIT={build_repo.head}")
    assert "Built image tip-hackathon-edition-tip-hackathon:latest = sha256:" in out


@pytest.mark.parametrize("dirt", ["untracked", "modified"])
def test_f8_dirty_tree_is_refused_and_nothing_is_built(build_repo, dirt):
    if dirt == "untracked":
        (build_repo.repo / "new.py").write_text("print(1)\n")
    else:
        (build_repo.repo / "README").write_text("changed\n")
    rc, out, calls = build_repo.run("-p", "tip-hackathon-edition")
    assert rc == 1 and "REFUSED" in out and calls == []
    rc, out, calls = build_repo.run("-p", "tip-hackathon-edition", TIP_ALLOW_DIRTY_BUILD="1")
    assert rc == 0, out
    assert [c for c in calls if " build" in c][0].endswith(f"BUILD_COMMIT={build_repo.head}-dirty")


def test_f8_base_image_is_pinned_by_digest():
    first = [ln for ln in _text(REPO / "Dockerfile").splitlines() if ln.startswith("FROM ")]
    assert first == ["FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"]
    assert "e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce" in _text(RUNBOOK)


# =================================================================================================
# F7: .dockerignore keeps local state, secrets, private captures and host venvs out of the image
# =================================================================================================
def test_f7_dockerignore_excludes_local_state_and_keeps_the_copied_sources():
    lines = [ln.strip() for ln in _text(REPO / ".dockerignore").splitlines() if ln.strip() and not ln.startswith("#")]
    for must in (".git", ".env", "backend/data", "data", "**/*.db", "**/*.bak", "fixtures/recorded_uw/*.json.gz",
                 "**/venv", "**/.venv", "mcp_isolated/venv", "**/__pycache__", "**/.pytest_cache", "**/node_modules",
                 "backend/tests"):
        assert must in lines, must
    copied = re.findall(r"^COPY (\S+) ", _text(REPO / "Dockerfile"), re.M)
    assert copied == ["backend/requirements.txt", "backend", "frontend", "fixtures", "mcp_isolated"]
    for src in copied:                                  # no pattern drops a COPY source or the demo fixtures
        assert src not in lines and f"{src}/" not in lines and f"{src}/*" not in lines
    assert not [ln for ln in lines if "scenario" in ln or ln in ("*.json", "**/*.json", "frontend/*.js")]


# =================================================================================================
# F1, F2, F9, F11: agent-facing documentation
# =================================================================================================
def test_f1_install_agent_port_advice_keeps_loopback():
    t = _text(INSTALL)
    port_item = t[t.index("**Port 8000 already in use**"):t.index("**Which `BIND_HOST` values work**")]
    assert "BIND_PORT" in port_item and "127.0.0.1:8080:8000" in port_item
    assert "Never edit the `ports:` line" in port_item
    assert "change the host side" not in t                         # the old advice is gone
    # the advised configuration renders loopback-only and the app's origin follows the port
    from tests.test_compose_config_rendering import _VAR_PATTERN

    def sub(m):
        env = {"BIND_PORT": "8080"}
        return env.get(m.group(1), m.group("default") if m.group("default") is not None else "")
    svc = yaml.safe_load(_VAR_PATTERN.sub(sub, _text(COMPOSE)))["services"]["tip-hackathon"]
    assert svc["ports"] == ["127.0.0.1:8080:8000"]
    from app.core.local_mode import compute_allowed_origins
    assert "http://127.0.0.1:8080" in compute_allowed_origins(str(svc["environment"]["BIND_HOST"]),
                                                               str(svc["environment"]["BIND_PORT"]))


def test_f2_agent_docs_point_upgrades_to_the_runbook_and_doclint_enforces_it():
    for p in (INSTALL, SKILL):
        t = _text(p)
        assert "RELEASE3_RECOVERY_RUNBOOK.md" in t
        assert "for a full rebuild after dependency/code changes" not in t
        assert "docker compose build --no-cache" not in t
    doclint = _load_doclint()
    assert doclint.lint_repo(REPO) == []


def _load_doclint():
    import importlib.util
    spec = importlib.util.spec_from_file_location("doclint_audit", TOOLS / "recovery" / "doclint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("text", [
    # the old SKILL.md "Restart" advice
    "## Restart\n```bash\ndocker compose restart\n# or, for a full rebuild after dependency/code changes:\n"
    "docker compose down\ndocker compose up --build\n```\n",
    # the old INSTALL_AGENT.md troubleshooting item
    "### j. Troubleshooting\n- **Stale build / dependency changes not picked up**: force a clean rebuild:\n"
    "  ```bash\n  docker compose build --no-cache\n  docker compose up\n  ```\n",
    "## Updating\nRun `tools/build_with_version.sh` then `docker compose up -d`.\n",
    # a runbook link elsewhere in the file does not excuse a section without one
    "## Install\nSee RELEASE3_RECOVERY_RUNBOOK.md for upgrades.\n## Upgrade quickly\ndocker compose up -d --build\n",
    # a section that links the runbook still may not offer the rebuild as upgrade advice
    "## Install\nUpgrades: RELEASE3_RECOVERY_RUNBOOK.md\n\nTo update to a new version:\ndocker compose up --build\n",
])
def test_f2_doclint_flags_rebuild_upgrade_advice(text):
    found = _load_doclint().lint_text("x.md", text)
    assert any("rebuild command" in f for f in found), found


@pytest.mark.parametrize("text", [
    "## Running with Docker\n```bash\ncp .env.example .env\ndocker compose up --build\n```\n"
    "> Upgrading an existing deployment? Follow only docs/RELEASE3_RECOVERY_RUNBOOK.md.\n",
    # a '#' comment inside a code fence is not a heading (README's build_with_version block)
    "## Checking for updates\nUpgrades follow docs/RELEASE3_RECOVERY_RUNBOOK.md.\n\n```bash\n"
    "# Build with real version info instead of plain `docker compose build`:\ntools/build_with_version.sh\n```\n",
    "## Logs\ndocker compose logs -f\n",
])
def test_f2_doclint_accepts_fresh_install_sections(text):
    assert [f for f in _load_doclint().lint_text("x.md", text) if "rebuild command" in f] == []


def test_f2_doclint_exempts_the_runbook_itself():
    assert _load_doclint().lint_rebuild("docs/RELEASE3_RECOVERY_RUNBOOK.md", "## U6\ndocker compose up --build\n") == []


def _env_example_names():
    return set(re.findall(r"^#?\s?([A-Z][A-Z0-9_]+)=", _text(REPO / ".env.example"), re.M))


def test_f9_every_documented_but_unforwarded_variable_is_named_in_install_agent():
    compose_env = set(yaml.safe_load(_text(COMPOSE))["services"]["tip-hackathon"]["environment"])
    image_env = set(re.findall(r"\b([A-Z][A-Z0-9_]+)=", "\n".join(
        ln for ln in _text(REPO / "Dockerfile").splitlines() if ln.startswith(("ENV", "    "))) ))
    unforwarded = _env_example_names() - compose_env - {"BUILD_COMMIT", "BUILD_VERSION", "BUILD_DATE"}
    t = _text(INSTALL)
    section = t[t.index("**A `.env` setting has no effect inside Docker**"):]
    missing = sorted(v for v in unforwarded if f"`{v}`" not in section)
    assert missing == [], f"documented in .env.example but neither forwarded by Compose nor listed: {missing}"
    for v in ("TIP_BOUNDS_RETRIES_PER_WINDOW", "TIP_BOUNDS_RETRY_DELAY_S"):     # read by the app, not forwarded
        assert v not in compose_env and f"`{v}`" in section
    assert "TIP_DB_PATH" in image_env


def test_f11_install_agent_documents_the_working_bind_host_values():
    t = _text(INSTALL)
    item = t[t.index("**Which `BIND_HOST` values work**"):]
    assert "`localhost` is rejected by Compose" in item and "`::1`" in item and "127.0.0.1" in item


# =================================================================================================
# F12: real `docker compose config` rendering (the PyYAML renderer in test_compose_config_rendering
# differs from Compose for an empty value); skipped without Docker Compose
# =================================================================================================
def _compose_config(tmp_path, envtext):
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not installed")
    envf = tmp_path / "render.env"
    envf.write_text(envtext)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("BIND_", "COMPOSE_", "TIP_", "UW_"))}
    r = subprocess.run(["docker", "compose", "-p", "r3audit-render-test", "-f", str(COMPOSE), "--env-file", str(envf),
                        "config", "--format", "json"], capture_output=True, text=True, env=e, timeout=120)
    if r.returncode != 0 and "is not a docker command" in (r.stderr or ""):
        pytest.skip("docker compose plugin not installed")
    return r


@pytest.mark.parametrize("envtext,host_ip,published,bind_port", [
    ("", "127.0.0.1", "8000", "8000"),
    ("BIND_PORT=8080\n", "127.0.0.1", "8080", "8080"),
    ("BIND_HOST=\n", "127.0.0.1", "8000", "8000"),          # Compose: empty -> default (the PyYAML renderer differs)
])
def test_f12_real_compose_rendering_keeps_loopback(tmp_path, envtext, host_ip, published, bind_port):
    r = _compose_config(tmp_path, envtext)
    assert r.returncode == 0, r.stderr[-500:]
    svc = json.loads(r.stdout)["services"]["tip-hackathon"]
    (port,) = svc["ports"]
    assert (port["host_ip"], str(port["published"]), port["target"]) == (host_ip, published, 8000)
    assert svc["environment"]["BIND_HOST"] == host_ip and str(svc["environment"]["BIND_PORT"]) == bind_port


def test_f12_real_compose_rejects_bind_host_localhost(tmp_path):
    r = _compose_config(tmp_path, "BIND_HOST=localhost\n")
    assert r.returncode != 0 and "invalid IP address" in r.stderr


# =================================================================================================
# Candidate schema 11; the runbook's commands parse with the tool
# =================================================================================================
def test_runbook_u7_expects_the_candidate_schema():
    from app.migrations import MIGRATIONS
    top = max(m.version for m in MIGRATIONS)
    assert top == 11
    u7 = [ln for ln in _text(RUNBOOK).splitlines() if ln.startswith("| U7 ")][0]
    assert f"--expect-schema {top}`" in u7 and f"schema 1–{top}" in u7
    drill = _text(TOOLS / "recovery" / "drill" / "drill.py")
    assert '"--candidate-schema", type=int, default=11' in drill
    assert '"--expect-schema", "10"' not in drill and '["schema"] == 10' not in drill


def test_runbook_tool_commands_parse():
    tool = load_tool()
    subs = {"$D": "--project tip-hackathon-edition --service tip-hackathon", "$V": "tip-hackathon-edition_tip-data",
            "$H": fd.HELPER, "$OLD": fd.OLD_IMAGE, "<BK>/<U4_ID>": "/bk/U4-x", "'<token>'": "RESTORE:x",
            "<candidate id>": fd.CANDIDATE, "<candidate commit>": "c" * 40}
    cmds = re.findall(r"`(python tools/recovery/r3recover\.py [^`]+)`", _text(RUNBOOK))
    assert {c.split()[2] for c in cmds} >= {"preflight", "snapshot", "verify", "test-restore", "rollback",
                                            "start-rollback", "accept"}
    for c in cmds:
        for k, v in subs.items():
            c = c.replace(k, v)
        c = re.sub(r"<[^>]+>", "X", c)
        try:
            tool.build_parser().parse_args(shlex.split(c)[2:])
        except SystemExit as exc:
            pytest.fail(f"runbook command does not parse ({exc.code}): {c}")
