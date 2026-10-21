#!/usr/bin/env python3
"""Release 3 full-volume backup and recovery tool (operator-invoked, fail-closed).

The ONLY supported Release 3 rollback is a full restore of the deployment-specific
U4 snapshot of the /srv/data named volume. This tool never performs a code-only
rollback, never restores from a database-only *.pre-migration-*.bak, never picks a
backup by name or age, never pulls or builds an image, and never starts anything
automatically after a failed check. See docs/RELEASE3_RECOVERY_RUNBOOK.md.

Subcommands
  preflight      discover the Compose deployment and report (no changes)
  snapshot       stop the app (--stop), verify quiescence, create U4 (archive + manifest)
  verify         verify a U4 directory (checksums, manifest, archive hash)
  test-restore   restore U4 into a NEW disposable volume and require an exact match
  rollback       --dry-run (plan + one-time approval token, no change) | --approve TOKEN
                 (forensic copy, full restore into the existing volume, exact verification)
  start-rollback create the pinned 147e315 container via Compose (no build, no pull),
                 check image/config/mounts/volume BEFORE starting, start, then accept;
                 a failed acceptance stops the container again
  accept         post-start acceptance (version, schema, scheduler, zero provider requests);
                 a failed acceptance stops the container

Exit codes: 0 success; 2 BLOCKED (a mandatory check failed: stop, do not improvise);
3 internal error (treat as BLOCKED); 4 CRITICAL (a container that failed acceptance could
not be confirmed stopped: stop it by hand and escalate); 64 usage error. Nothing returns 0
after a failure. Stdlib only. U4 directories, manifests, plans and forensic copies are
SENSITIVE (they contain the settings/token files of the deployment).

Concurrency: snapshot, test-restore, rollback and start-rollback hold an exclusive lock file
in the backup root, and refuse to run while ANY other container (running or stopped)
references the volume. This excludes concurrent use of this tool and of Compose/Docker
through ordinary paths; it cannot stop a Docker administrator who deliberately changes the
volume through the daemon between the final probe and `docker start` (seconds).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FORMAT = "r3-u4/2"
EXPECTED_COMMIT_DEFAULT = "147e3154b001cd6e73dde16e612978864ff6f500"
DATA_MOUNT = "/srv/data"
PRIMARY_DB = "tip_hackathon.db"
DEMO_DB = "tip_hackathon.demo.db"
BAK_RE = re.compile(r"^(tip_hackathon\.db|tip_hackathon\.demo\.db)\.pre-migration-\d{8}T\d{6}\d*Z(-\d+)?\.bak$")
SCHEMA9_VERSIONS = list(range(1, 10))
# the exact Release 2 (147e315) migration set a rollback target must carry -- names included
SCHEMA9_MIGRATIONS = [[1, "catalog_foundation_tables"], [2, "signal_evidence_and_source_corrections"],
                      [3, "darkpool_coverage_status"], [4, "scheduler_lease_and_state"],
                      [5, "price_bar_freshness_tracking"], [6, "price_bar_current_backfill_and_view"],
                      [7, "lifecycle_episodes"], [8, "session_coverage"], [9, "session_value_curves_and_baseline_jobs"]]
TOOL_DIR = Path(__file__).resolve().parent
# application schema objects/columns per schema state 1..9, generated from 147e315 (schema_reference.py)
REFERENCE = json.loads((TOOL_DIR / "schema9_reference.json").read_text(encoding="utf-8"))
DRILL_FAULT_ENV = "R3_DRILL_FAULT"          # honoured ONLY for volumes labelled r3.drill=1 (rehearsal)
PLAN_TTL_SECONDS = 3600
LOCK_PREFIX = ".r3recover-"
EXIT_OK, EXIT_BLOCKED, EXIT_INTERNAL, EXIT_CRITICAL, EXIT_USAGE = 0, 2, 3, 4, 64


class Blocked(Exception):
    """A mandatory safety check failed. Stop; do not improvise another path."""


class Critical(Exception):
    """A container that failed acceptance could not be confirmed stopped."""


def utcnow():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return f"{_dt.datetime.now(_dt.timezone.utc):%Y%m%dT%H%M%SZ}"


def log(msg):
    print(f"[r3recover] {msg}", file=sys.stderr, flush=True)


def run(cmd, *, check=True, capture=True, input_bytes=None, timeout=3600):
    res = subprocess.run(cmd, capture_output=capture, input=input_bytes, timeout=timeout)
    if check and res.returncode != 0:
        err = (res.stderr or b"").decode(errors="replace").strip()[-800:]
        raise Blocked(f"command failed ({res.returncode}): {' '.join(map(str, cmd[:6]))} ... :: {err}")
    return res


def docker_json(*args):
    out = run(["docker", *args]).stdout.decode()
    return json.loads(out)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


# --- images -------------------------------------------------------------------------------
def require_local_image_id(image_id, what):
    if not (isinstance(image_id, str) and image_id.startswith("sha256:") and len(image_id) == 71):
        raise Blocked(f"{what} must be an immutable image ID 'sha256:<64 hex>', got {image_id!r} "
                      "(tags and names are refused: no pull, no build, no mutable reference)")
    res = run(["docker", "image", "inspect", image_id, "--format", "{{.Id}}"], check=False)
    if res.returncode != 0 or res.stdout.decode().strip() != image_id:
        raise Blocked(f"{what} {image_id} is not present locally; refusing to pull or build it")
    return image_id


def image_commit(image_id):
    res = run(["docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "cat",
               image_id, "/srv/VERSION"], check=False)
    if res.returncode != 0:
        raise Blocked(f"cannot read /srv/VERSION from image {image_id}")
    for line in res.stdout.decode().splitlines():
        if line.startswith("BUILD_COMMIT="):
            return line.split("=", 1)[1].strip()
    raise Blocked(f"/srv/VERSION in {image_id} has no BUILD_COMMIT line")


# --- Compose / volume discovery -----------------------------------------------------------
CONFIG_KEYS = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "ExposedPorts", "StopSignal",
               "StopTimeout", "Tty", "OpenStdin", "StdinOnce", "Domainname", "Volumes", "Shell")
HOST_KEYS = ("PortBindings", "PublishAllPorts", "RestartPolicy", "Privileged", "ReadonlyRootfs", "CapAdd",
             "CapDrop", "SecurityOpt", "NetworkMode", "PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode",
             "Devices", "DeviceCgroupRules", "DeviceRequests", "Sysctls", "Tmpfs", "ExtraHosts", "Dns", "DnsOptions",
             "DnsSearch", "GroupAdd", "Init", "Runtime", "ShmSize", "Memory", "MemorySwap", "NanoCpus", "CpuShares",
             "PidsLimit", "Ulimits", "OomKillDisable", "LogConfig", "Binds", "Mounts", "VolumesFrom", "Links",
             "AutoRemove", "Isolation", "MaskedPaths", "ReadonlyPaths")
UNORDERED = {"CapAdd", "CapDrop", "SecurityOpt", "GroupAdd", "Dns", "DnsOptions", "DnsSearch", "ExtraHosts",
             "DeviceCgroupRules", "Binds", "VolumesFrom", "Links"}


def _norm(v, unordered=False):
    # Docker reports an unset setting as null, false, 0, "" or an empty collection depending on how the
    # container was created; all of these mean "default". A real value (true, a non-zero limit) is kept.
    if v is None or v is False or v == 0 or v in ([], {}, ""):
        return None
    if unordered and isinstance(v, list):
        return sorted(v, key=lambda x: json.dumps(x, sort_keys=True))
    return v


def runtime_fingerprint(c):
    """Everything about how the container runs, except values that legitimately differ between
    two containers of the same service (ID, name, hostname, creation time, Compose bookkeeping
    labels, the image reference string -- the image ID is compared separately)."""
    hc, cfg = c.get("HostConfig") or {}, c.get("Config") or {}
    env = sorted(cfg.get("Env") or [])
    own = {c.get("Id", ""), c.get("Id", "")[:12], (c.get("Name") or "").lstrip("/")}
    nets = {}
    for name, n in ((c.get("NetworkSettings") or {}).get("Networks") or {}).items():
        n = n or {}
        nets[name] = {"aliases": sorted(a for a in (n.get("Aliases") or []) if a not in own) or None,
                      "ipam": _norm(n.get("IPAMConfig")), "links": _norm(n.get("Links"), True),
                      "driver_opts": _norm(n.get("DriverOpts"))}
    mounts = sorted(({k: m.get(k) for k in ("Type", "Name", "Source", "Destination", "Driver", "RW", "Mode",
                                             "Propagation")} for m in (c.get("Mounts") or [])),
                    key=lambda m: json.dumps(m, sort_keys=True))
    return {
        "config": {k: _norm(cfg.get(k)) for k in CONFIG_KEYS},
        "labels": {k: v for k, v in sorted((cfg.get("Labels") or {}).items()) if not k.startswith("com.docker.compose.")},
        "env_keys": sorted(e.split("=", 1)[0] for e in env),
        "env_sha256": hashlib.sha256("\n".join(env).encode()).hexdigest(),   # values hashed, never stored
        "host": {k: _norm(hc.get(k), k in UNORDERED) for k in HOST_KEYS},
        "networks": nets,
        "mounts": mounts,
    }


def fingerprint_diff(a, b, prefix=""):
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if isinstance(x, dict) and isinstance(y, dict):
            out += fingerprint_diff(x, y, f"{prefix}{k}.")
        elif x != y:
            out.append(f"{prefix}{k}: {json.dumps(x)[:120]} != {json.dumps(y)[:120]}")
    return out


def discover(project, service, expect_volume=None):
    """Exactly one Compose service container with exactly one named local volume at /srv/data,
    no other persistent mounts, and NO other container (running, restarting or stopped) that
    references the volume. Never creates anything."""
    ids = run(["docker", "ps", "-a", "-q", "--no-trunc",
               "--filter", f"label=com.docker.compose.project={project}",
               "--filter", f"label=com.docker.compose.service={service}"]).stdout.decode().split()
    if len(ids) != 1:
        raise Blocked(f"expected exactly one container for compose project={project!r} service={service!r}, "
                      f"found {len(ids)}")
    c = docker_json("inspect", ids[0])[0]
    labels = c["Config"].get("Labels") or {}
    if labels.get("com.docker.compose.project") != project or labels.get("com.docker.compose.service") != service:
        raise Blocked("container labels do not match the requested compose project/service")
    mounts = c.get("Mounts") or []
    data = [m for m in mounts if m.get("Destination") == DATA_MOUNT]
    if len(data) != 1:
        raise Blocked(f"expected exactly one mount at {DATA_MOUNT}, found {len(data)}")
    m = data[0]
    if m.get("Type") != "volume" or not m.get("Name"):
        raise Blocked(f"{DATA_MOUNT} is a {m.get('Type')} mount; a named volume is required (bind mounts refused)")
    if m.get("Driver") != "local":
        raise Blocked(f"{DATA_MOUNT} volume driver is {m.get('Driver')!r}; only 'local' is supported")
    extra = [x.get("Destination") for x in mounts if x is not m]
    if extra:
        raise Blocked(f"unexpected additional mounts on the service container: {extra}")
    vname = m["Name"]
    if expect_volume is not None and vname != expect_volume:
        raise Blocked(f"service mounts volume {vname!r}, not the expected {expect_volume!r}")
    res = run(["docker", "volume", "inspect", vname], check=False)       # never 'create'
    if res.returncode != 0:
        raise Blocked(f"volume {vname!r} does not exist (refusing to create one)")
    v = json.loads(res.stdout.decode())[0]
    if v.get("Driver") != "local":
        raise Blocked(f"volume {vname} driver {v.get('Driver')!r} is not 'local'")
    vlabels = v.get("Labels") or {}
    if vlabels.get("com.docker.compose.project") != project:
        raise Blocked(f"volume {vname} is not labelled as belonging to compose project {project!r}")
    users = run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"volume={vname}"]).stdout.decode().split()
    others = []
    for uid in users:
        if uid == c["Id"]:
            continue
        o = docker_json("inspect", uid)[0]
        others.append(f"{o['Name'].lstrip('/')} ({o['State']['Status']})")
    if others:
        raise Blocked(f"other containers reference volume {vname} (running or stopped): {others}; "
                      "only the service container may reference it during recovery")
    return {
        "project": project, "service": service,
        "container": {"id": c["Id"], "name": c["Name"].lstrip("/"), "status": c["State"]["Status"],
                      "running": bool(c["State"].get("Running")), "restarting": bool(c["State"].get("Restarting"))},
        "image_id": c["Image"],
        "compose_labels": {k: v for k, v in labels.items() if k.startswith("com.docker.compose.")},
        "volume": {"name": vname, "driver": v["Driver"], "created_at": v.get("CreatedAt"),
                   "mountpoint": v.get("Mountpoint"), "labels": vlabels, "destination": DATA_MOUNT},
        "other_volume_users": [],
        "runtime": runtime_fingerprint(c),
    }


def stable_identity(ident):
    """Fields that must not change between preflight, stop, snapshot and rollback."""
    return {"project": ident["project"], "service": ident["service"], "volume": ident["volume"]}


def same_deployment(a, b):
    return (stable_identity(a) == stable_identity(b) and a["container"]["id"] == b["container"]["id"]
            and a["image_id"] == b["image_id"] and a["runtime"] == b["runtime"])


def _drill_fault(ident):
    """Rehearsal fault injection: honoured only for volumes labelled r3.drill=1."""
    return os.environ.get(DRILL_FAULT_ENV) if (ident["volume"]["labels"] or {}).get("r3.drill") == "1" else None


def _drill_mutate(helper, volume):
    helper_run(helper, [(volume, "/v", "rw")], ["sh", "-c", "echo injected > /v/.r3-drill-mutation"])


# --- backup root and lock ------------------------------------------------------------------
def validate_backup_root(path, ident=None):
    """Absolute, existing, writable directory that is not an alias (symlink, junction, UNC, Docker
    storage path) and does not resolve inside the source volume. Never creates anything."""
    raw = str(path)
    p = Path(raw)
    if not p.is_absolute():
        raise Blocked(f"backup root {raw!r} is not an absolute path")
    if raw.startswith(("\\\\", "//")):
        raise Blocked(f"backup root {raw!r} is a UNC/network/device path; a local directory is required")
    low = raw.replace("\\", "/").lower()
    vol = ((ident or {}).get("volume") or {}).get("name", "")
    for marker in ("docker-desktop", "/var/lib/docker", "wsl.localhost", "wsl$", "/docker/volumes/"):
        if marker in low:
            raise Blocked(f"backup root {raw!r} points into Docker storage ({marker!r})")
    if vol and f"/volumes/{vol.lower()}/" in low + "/":
        raise Blocked(f"backup root {raw!r} points into the source volume {vol}")
    if not p.exists():
        raise Blocked(f"backup root {raw!r} does not exist (it is never created)")
    if p.is_symlink() or not p.is_dir():
        raise Blocked(f"backup root {raw!r} is not a plain directory")
    real, absp = os.path.realpath(raw), os.path.abspath(raw)
    if os.path.normcase(real) != os.path.normcase(absp):
        raise Blocked(f"backup root {raw!r} is an alias resolving to {real!r}; give the real path")
    mp = ((ident or {}).get("volume") or {}).get("mountpoint")
    if mp:
        mpn, rn = os.path.normcase(os.path.normpath(mp)), os.path.normcase(real)
        if rn == mpn or rn.startswith(mpn.rstrip("\\/") + os.sep) or rn.replace("\\", "/").startswith(mp.rstrip("/") + "/"):
            raise Blocked(f"backup root {raw!r} resolves inside the source volume mountpoint {mp}")
    try:
        fd, probe = tempfile.mkstemp(prefix=".r3-write-test-", dir=real)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise Blocked(f"backup root {raw!r} is not writable: {exc}")
    return Path(real)


def validate_u4_path(u4, ident=None):
    p = Path(str(u4))
    if not p.is_absolute():
        raise Blocked(f"U4 path {str(u4)!r} is not absolute")
    if p.is_symlink():
        raise Blocked(f"U4 path {str(u4)!r} is a symlink")
    validate_backup_root(str(p.parent), ident)
    return p


class OperationLock:
    """Exclusive per-deployment lock file in the backup root (created with O_EXCL, never stolen)."""

    def __init__(self, root, project, command):
        self.path = Path(root) / f"{LOCK_PREFIX}{project}.lock"
        self.token = secrets.token_hex(16)
        self.command = command

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Blocked(f"another recovery operation holds {self.path}. If you are certain none is running, "
                          "inspect and remove the lock file by hand.")
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "host": platform.node(), "utc": utcnow(), "command": self.command,
                       "token": self.token}, f)
        return self

    def __exit__(self, *exc):
        try:
            if json.loads(self.path.read_text()).get("token") == self.token:
                self.path.unlink()
        except (OSError, ValueError):
            pass
        return False


# --- helper-container operations --------------------------------------------------------
def helper_run(helper, mounts, cmd, *, check=True, input_bytes=None):
    args = ["docker", "run", "--rm", "--pull", "never", "--network", "none"]
    for src, dst, mode in mounts:
        args += ["-v", f"{src}:{dst}:{mode}"]
    args += ["--entrypoint", cmd[0], helper, *cmd[1:]]
    return run(args, check=check, input_bytes=input_bytes)


def probe_volume(helper, volume):
    res = helper_run(helper, [(volume, "/target", "ro"), (str(TOOL_DIR), "/tool", "ro")],
                     ["python", "/tool/r3probe.py", "manifest", "/target"], check=False)
    try:
        out = json.loads(res.stdout.decode())
    except ValueError:
        raise Blocked(f"probe produced no manifest (exit {res.returncode}): {res.stderr.decode()[-400:]}")
    if res.returncode != 0 or "error" in out:
        raise Blocked(f"probe failed: {out.get('error')}")
    return out


def _schema_state(f, rel, errs):
    """Returns k if the migration set is exactly the first k 147e315 migrations (names included)."""
    sv = f.get("schema_versions")
    if sv is None:
        return None
    if not sv or sv != SCHEMA9_MIGRATIONS[:len(sv)]:
        errs.append(f"{rel}: migration set {sv} is not a prefix of the exact 147e315 schema-9 set")
        return None
    return len(sv)


def _app_contract(f, rel, k, errs):
    ref = REFERENCE["states"][str(k)]
    have = {tuple(o) for o in f.get("objects") or []}
    missing = sorted(tuple(o) for o in ref["objects"] if tuple(o) not in have)
    if missing:
        errs.append(f"{rel}: missing application schema objects for schema state {k}: {missing[:8]}")
    cols = f.get("columns") or {}
    for table, want in ref["columns"].items():
        if table in cols and not set(want) <= set(cols[table]):
            errs.append(f"{rel}: table {table} lacks columns {sorted(set(want) - set(cols[table]))}")


def semantic_check(manifest, *, expect_schema_versions=SCHEMA9_VERSIONS):
    """Mandatory SQLite/state checks for a Release 2 (schema-9) rollback target. Every SQLite file
    must be one of: the primary DB (required, exactly schema 9), the Demo DB (optional, an exact
    prefix 1..k of the 147e315 migrations), or a pre-migration backup of either (integrity and a
    prefix migration set). Raises Blocked listing every failure."""
    if expect_schema_versions != SCHEMA9_VERSIONS:
        raise Blocked("only the schema-9 (147e315) rollback target is supported")
    errs = []
    if manifest.get("unsupported"):
        errs.append(f"unsupported filesystem entries: {manifest['unsupported']}")
    dbs = manifest.get("sqlite", {})
    if PRIMARY_DB not in dbs:
        errs.append(f"primary database {PRIMARY_DB} not found")
    for rel, f in dbs.items():
        kind = "primary" if rel == PRIMARY_DB else "demo" if rel == DEMO_DB else "backup" if BAK_RE.match(rel) else None
        if kind is None:
            errs.append(f"{rel}: unexpected SQLite database (not the primary, Demo or a pre-migration backup)")
            continue
        if "error" in f:
            errs.append(f"{rel}: {f['error']}")
            continue
        if f.get("integrity_check") != ["ok"]:
            errs.append(f"{rel}: integrity_check {(f.get('integrity_check') or [])[:3]}")
        if f.get("foreign_key_violations"):
            errs.append(f"{rel}: {f['foreign_key_violations']} foreign key violations")
        if f.get("migration_10_objects_present"):
            errs.append(f"{rel}: migration-10 objects present {f['migration_10_objects_present']}")
        k = _schema_state(f, rel, errs)
        if kind == "primary":
            if f.get("schema_versions") != SCHEMA9_MIGRATIONS:
                errs.append(f"{rel}: schema versions {[v for v, _ in f.get('schema_versions') or []]} != required "
                            f"{SCHEMA9_VERSIONS} (exact 147e315 migration set)")
            else:
                _app_contract(f, rel, 9, errs)
            if f.get("scheduler_mode") != "disabled":
                errs.append(f"{rel}: persisted scheduler mode is {f.get('scheduler_mode')!r}, must be 'disabled'")
            if f.get("active_baseline_jobs"):
                errs.append(f"{rel}: {f['active_baseline_jobs']} active baseline job(s)")
        elif kind == "demo":
            if k is None:
                errs.append(f"{rel}: the Demo database has no valid 147e315 migration set")
            else:
                _app_contract(f, rel, k, errs)
            if f.get("active_baseline_jobs"):
                errs.append(f"{rel}: {f['active_baseline_jobs']} active baseline job(s)")
    if errs:
        raise Blocked("semantic checks failed: " + " | ".join(errs))


def compare(expected, observed):
    """Exact comparison of two probe manifests (filesystem entries + SQLite facts)."""
    diffs = []
    e, o = expected.get("entries", {}), observed.get("entries", {})
    for p in sorted(set(e) - set(o)):
        diffs.append(f"missing: {p}")
    for p in sorted(set(o) - set(e)):
        diffs.append(f"unexpected: {p}")
    for p in sorted(set(e) & set(o)):
        for k in sorted(set(e[p]) | set(o[p])):
            if e[p].get(k) != o[p].get(k):
                diffs.append(f"{p}: {k} {e[p].get(k)!r} != {o[p].get(k)!r}")
    if expected.get("sqlite") != observed.get("sqlite"):
        diffs.append("sqlite facts differ")
    if observed.get("unsupported"):
        diffs.append(f"unsupported entries: {observed['unsupported']}")
    return diffs


# --- U4 directory ---------------------------------------------------------------------------
def write_sums(u4):
    lines = [f"{sha256_file(u4 / n)}  {n}" for n in ("volume.tar", "manifest.json")]
    (u4 / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def verify_u4(path):
    u4 = Path(path)
    if not u4.is_dir():
        raise Blocked(f"U4 directory {u4} does not exist")
    for n in ("volume.tar", "manifest.json", "SHA256SUMS"):
        if not (u4 / n).is_file():
            raise Blocked(f"U4 is incomplete: {n} missing")
    sums = {}
    for line in (u4 / "SHA256SUMS").read_text().splitlines():
        h, n = line.split(None, 1)
        sums[n.strip().lstrip("*")] = h
    if set(sums) != {"volume.tar", "manifest.json"}:
        raise Blocked("SHA256SUMS must list exactly volume.tar and manifest.json")
    for n, h in sums.items():
        if sha256_file(u4 / n) != h:
            raise Blocked(f"checksum mismatch: {n}")
    m = json.loads((u4 / "manifest.json").read_text())
    if m.get("format") != FORMAT:
        raise Blocked(f"unknown manifest format {m.get('format')!r} (this tool requires {FORMAT})")
    if m.get("u4_id") != u4.name:
        raise Blocked(f"manifest u4_id {m.get('u4_id')!r} does not match directory {u4.name!r}")
    if m.get("archive", {}).get("sha256") != sums["volume.tar"]:
        raise Blocked("manifest's recorded archive SHA-256 does not match volume.tar")
    return m


def manifest_sha(u4):
    return sha256_file(Path(u4) / "manifest.json")


# --- subcommands -----------------------------------------------------------------------------
def cmd_preflight(a):
    ident = discover(a.project, a.service, a.expect_volume)
    require_local_image_id(ident["image_id"], "running image")
    ident["image_commit"] = image_commit(ident["image_id"])
    print(json.dumps(ident, indent=1, sort_keys=True))
    if a.expect_commit and ident["image_commit"] != a.expect_commit:
        raise Blocked(f"running image commit {ident['image_commit']} != expected {a.expect_commit}")
    return EXIT_OK


def cmd_snapshot(a):
    helper = require_local_image_id(a.helper_image, "helper image")
    pre = discover(a.project, a.service, a.expect_volume)
    root = validate_backup_root(a.backup_root, pre)
    with OperationLock(root, a.project, "snapshot"):
        return _snapshot(a, helper, root, pre)


def _snapshot(a, helper, root, pre):
    require_local_image_id(pre["image_id"], "running image")
    commit = image_commit(pre["image_id"])
    if commit != a.expect_commit:
        raise Blocked(f"deployed image commit {commit} != expected {a.expect_commit}")
    if pre["container"]["running"] or pre["container"]["restarting"]:
        if not a.stop:
            raise Blocked("application container is running; pass --stop to stop it before the snapshot")
        log(f"stopping {pre['container']['name']}")
        run(["docker", "stop", "--time", "60", pre["container"]["id"]])
    post = discover(a.project, a.service, a.expect_volume)
    if post["container"]["running"] or post["container"]["restarting"] or post["container"]["status"] not in ("exited", "created"):
        raise Blocked(f"container is not fully stopped: {post['container']['status']}")
    if not same_deployment(pre, post):
        raise Blocked("deployment identity changed between preflight and stop")
    vm = probe_volume(helper, post["volume"]["name"])
    semantic_check(vm, expect_schema_versions=SCHEMA9_VERSIONS)
    need = int(sum(e.get("size", 0) for e in vm["entries"].values()) * 2.2) + (200 << 20)
    free = shutil.disk_usage(root).free
    if free < need:
        raise Blocked(f"insufficient free space in {root}: {free} < {need}")
    u4_id = f"U4-{a.project}-{stamp()}-{secrets.token_hex(4)}"
    u4 = root / u4_id
    try:
        u4.mkdir(mode=0o700)                   # exclusive: never overwrite an existing U4
    except FileExistsError:
        raise Blocked(f"U4 directory {u4} already exists; refusing to overwrite it")
    try:
        helper_run(helper, [(post["volume"]["name"], "/src", "ro"), (str(u4), "/u4", "rw")],
                   ["tar", "--numeric-owner", "--format=posix", "-cpf", "/u4/volume.tar", "-C", "/src", "."])
        # archive fidelity: extract inside the helper and require the same manifest
        chk = helper_run(helper, [(str(u4), "/u4", "ro"), (str(TOOL_DIR), "/tool", "ro")],
                         ["sh", "-c", "mkdir /tmp/x && tar --numeric-owner -xpf /u4/volume.tar -C /tmp/x "
                                      "&& python /tool/r3probe.py manifest /tmp/x"])
        diffs = compare(vm, json.loads(chk.stdout.decode()))
        if diffs:
            raise Blocked(f"archive does not reproduce the volume exactly: {diffs[:10]}")
        if _drill_fault(post) == "mutate_after_archive":
            _drill_mutate(helper, post["volume"]["name"])
        # the source must still be exactly what was archived, and the deployment unchanged and stopped
        diffs = compare(vm, probe_volume(helper, post["volume"]["name"]))
        if diffs:
            raise Blocked(f"source volume changed after it was archived: {diffs[:10]}")
        final = discover(a.project, a.service, a.expect_volume)
        if not same_deployment(post, final) or final["container"]["running"] or final["container"]["restarting"]:
            raise Blocked("deployment changed or restarted during the snapshot")
    except BaseException as exc:
        (u4 / "SNAPSHOT_FAILED").write_text(f"{utcnow()} {type(exc).__name__}: {exc}\n")
        raise
    manifest = {
        "format": FORMAT, "u4_id": u4_id, "created_utc": utcnow(), "deployment_host": platform.node(),
        "identity": post, "expected_commit": a.expect_commit, "image_commit": commit,
        "helper_image": helper, "stopped_state": post["container"]["status"], "backup_root": str(root),
        "archive": {"file": "volume.tar", "abs_path": str((u4 / "volume.tar").resolve()),
                    "sha256": sha256_file(u4 / "volume.tar"), "bytes": (u4 / "volume.tar").stat().st_size},
        "volume_manifest": vm,
    }
    (u4 / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    write_sums(u4)
    for n in ("volume.tar", "manifest.json", "SHA256SUMS"):
        os.chmod(u4 / n, 0o400)
    print(json.dumps({"u4_id": u4_id, "u4_dir": str(u4.resolve()), "archive_sha256": manifest["archive"]["sha256"],
                      "entries": len(vm["entries"]), "databases": sorted(vm["sqlite"])}, indent=1))
    log("U4 created. Next: verify, then test-restore. The application stays stopped.")
    return EXIT_OK


def cmd_verify(a):
    m = verify_u4(a.u4)
    print(json.dumps({"u4_id": m["u4_id"], "archive_sha256": m["archive"]["sha256"],
                      "volume": m["identity"]["volume"]["name"], "test_restore": _test_restore_state(a.u4)}, indent=1))
    return EXIT_OK


def _test_restore_state(u4):
    p = Path(u4) / "test_restore.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def _extract(helper, volume, u4, fault=None):
    cmd = "find /dst -mindepth 1 -delete && tar --numeric-owner --same-owner -xpf /u4/volume.tar -C /dst"
    if fault == "interrupt_extract":           # rehearsal only: feed tar half of the archive
        cmd = ("find /dst -mindepth 1 -delete && head -c $(( $(stat -c %s /u4/volume.tar) / 2 )) /u4/volume.tar "
               "| tar --numeric-owner --same-owner -xpf - -C /dst")
    return helper_run(helper, [(volume, "/dst", "rw"), (str(u4), "/u4", "ro")], ["sh", "-c", cmd], check=False)


def cmd_test_restore(a):
    u4p = validate_u4_path(a.u4)
    m = verify_u4(u4p)
    helper = require_local_image_id(a.helper_image, "helper image")
    if helper != m["helper_image"]:
        raise Blocked(f"helper image {helper} != U4 helper image {m['helper_image']}")
    semantic_check(m["volume_manifest"], expect_schema_versions=SCHEMA9_VERSIONS)
    with OperationLock(u4p.parent, m["identity"]["project"], "test-restore"):
        vol = f"r3-testrestore-{m['u4_id'][-8:]}-{secrets.token_hex(3)}"
        if run(["docker", "volume", "inspect", vol], check=False).returncode == 0:
            raise Blocked(f"disposable volume {vol} already exists")
        run(["docker", "volume", "create", "--label", "r3.disposable=1", vol])
        result = {"u4_id": m["u4_id"], "manifest_sha256": manifest_sha(u4p), "volume": vol, "utc": utcnow()}
        try:
            res = _extract(helper, vol, u4p)
            if res.returncode != 0:
                raise Blocked(f"test restore extraction failed: {res.stderr.decode()[-300:]}")
            diffs = compare(m["volume_manifest"], probe_volume(helper, vol))
            if diffs:
                raise Blocked(f"test restore differs from U4: {diffs[:10]}")
            result["result"] = "PASS"
        except Blocked as exc:
            result.update(result="FAIL", reason=str(exc)[:500])
            raise
        finally:
            run(["docker", "volume", "rm", vol], check=False)
            (u4p / "test_restore.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    return EXIT_OK


# --- one-time, state-bound approval ---------------------------------------------------------
class Authorization:
    """Created ONLY by _consume_token(); destructive steps require it."""
    def __init__(self, token, binding):
        self.token = token
        self.binding = binding


def _binding(u4, m, cur):
    """Everything an approval is bound to; recomputed before every destructive step."""
    trp = Path(u4) / "test_restore.json"
    return {"u4_id": m["u4_id"], "manifest_sha256": manifest_sha(u4), "archive_sha256": sha256_file(Path(u4) / "volume.tar"),
            "test_restore_sha256": sha256_file(trp) if trp.is_file() else None,
            "project": cur["project"], "service": cur["service"], "volume": cur["volume"]["name"],
            "volume_created_at": cur["volume"]["created_at"], "container_id": cur["container"]["id"],
            "image_id": cur["image_id"]}


def _token(b, nonce):
    return f"RESTORE:{b['u4_id']}:{b['manifest_sha256'][:16]}:{b['volume']}:{nonce}"


def _plan_path(u4, nonce):
    return Path(u4) / f"rollback-plan-{nonce}.json"


def _new_plan(u4, b):
    nonce = secrets.token_hex(16)
    now = int(time.time())
    plan = {"nonce": nonce, "binding": b, "created_utc": utcnow(), "created_epoch": now,
            "expires_epoch": now + PLAN_TTL_SECONDS, "token": _token(b, nonce)}
    fd = os.open(_plan_path(u4, nonce), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(plan, f, indent=1, sort_keys=True)
    return plan


def _load_plan(u4, token):
    m = re.fullmatch(r"RESTORE:[^:]+:[0-9a-f]{16}:[^:]+:([0-9a-f]{32})", token or "")
    if not m:
        raise Blocked("approval token missing or malformed (run rollback --dry-run for a fresh one)")
    p = _plan_path(u4, m.group(1))
    if not p.is_file():
        raise Blocked("no rollback plan exists for this approval token")
    plan = json.loads(p.read_text())
    if plan.get("token") != token:
        raise Blocked("approval token does not match its rollback plan")
    if Path(str(p) + ".consumed").exists():
        raise Blocked("approval token was already used (one-time); run rollback --dry-run for a new one")
    if int(time.time()) > plan.get("expires_epoch", 0):
        raise Blocked("approval token expired; run rollback --dry-run for a new one")
    return plan, p


def _check_binding(plan, current):
    if plan["binding"] != current:
        changed = sorted(k for k in set(plan["binding"]) | set(current) if plan["binding"].get(k) != current.get(k))
        raise Blocked(f"the U4 snapshot or deployment changed since the approval was issued ({changed}); "
                      "the token is not valid for the current state")


def _consume_token(u4, token, current):
    plan, p = _load_plan(u4, token)
    _check_binding(plan, current)
    try:
        fd = os.open(str(p) + ".consumed", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Blocked("approval token was already used (one-time)")
    with os.fdopen(fd, "w") as f:
        f.write(utcnow() + "\n")
    return Authorization(token, plan["binding"])


def _write_state(u4, state, **extra):
    (Path(u4) / "restore_state.json").write_text(json.dumps({"state": state, "utc": utcnow(), **extra}, indent=1))


def _rollback_checks(a):
    u4p = validate_u4_path(a.u4)
    m = verify_u4(u4p)
    tr = _test_restore_state(u4p)
    if not tr or tr.get("result") != "PASS" or tr.get("manifest_sha256") != manifest_sha(u4p):
        raise Blocked("U4 has no passing test-restore for this exact manifest; run test-restore first")
    helper = require_local_image_id(a.helper_image, "helper image")
    if helper != m["helper_image"]:
        raise Blocked(f"helper image {helper} != U4 helper image {m['helper_image']}")
    semantic_check(m["volume_manifest"], expect_schema_versions=SCHEMA9_VERSIONS)
    cur = discover(a.project, a.service)
    if stable_identity(cur) != stable_identity(m["identity"]):
        raise Blocked("this U4 belongs to a different deployment (project/service/volume identity mismatch)")
    validate_u4_path(u4p, cur)
    return m, helper, cur


def cmd_rollback(a):
    u4p = validate_u4_path(a.u4)
    with OperationLock(u4p.parent, a.project, "rollback"):
        return _rollback(a, u4p)


def _rollback(a, u4p):
    m, helper, cur = _rollback_checks(a)
    vol = cur["volume"]["name"]
    running = cur["container"]["running"] or cur["container"]["restarting"]
    if a.dry_run:                                # never consumes or grants: only proposes a one-time token
        plan = _new_plan(u4p, _binding(u4p, m, cur))
        print(json.dumps({"dry_run": True, "u4_id": m["u4_id"], "volume": vol,
                          "would_stop_running_container": cur["container"]["name"] if running else None,
                          "forensic_copy_to": str(u4p.parent), "archive_sha256": m["archive"]["sha256"],
                          "approval_token": plan["token"], "token_expires_utc":
                              _dt.datetime.fromtimestamp(plan["expires_epoch"], _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "token_rules": "one-time; void if the U4, its test restore, the volume or the "
                                         "service container change; void after expiry",
                          "consequence": "EVERY write made after the U4 snapshot will be discarded from this volume."},
                         indent=1))
        log("dry run: nothing was changed and nothing was authorized")
        return EXIT_OK
    if running and not a.stop:
        raise Blocked("the candidate container is running; pass --stop with --approve to stop it")
    auth = _consume_token(u4p, a.approve, _binding(u4p, m, cur))
    if running:
        run(["docker", "stop", "--time", "60", cur["container"]["id"]])
    # forensic copy of the failed volume (separate directory, never used for restore)
    fx = u4p.parent / f"forensic-{m['u4_id']}-{stamp()}-{secrets.token_hex(2)}"
    fx.mkdir(mode=0o700)
    helper_run(helper, [(vol, "/src", "ro"), (str(fx), "/fx", "rw")],
               ["tar", "--numeric-owner", "--format=posix", "-cpf", "/fx/volume-post-upgrade.tar", "-C", "/src", "."])
    (fx / "SHA256SUMS").write_text(f"{sha256_file(fx / 'volume-post-upgrade.tar')}  volume-post-upgrade.tar\n")
    # revalidate immediately before the destructive step
    m2 = verify_u4(u4p)
    now = discover(a.project, a.service)
    if now["container"]["running"] or now["container"]["restarting"]:
        raise Blocked("the service container is running again; refusing to restore")
    _check_binding({"binding": auth.binding}, _binding(u4p, m2, now))
    fault = _drill_fault(now)
    _write_state(u4p, "IN_PROGRESS", volume=vol, token=auth.token, forensic=str(fx))
    res = _extract(helper, vol, u4p, fault)
    if res.returncode != 0:
        _write_state(u4p, "BLOCKED_PARTIAL", volume=vol, reason="extraction failed")
        raise Blocked("restore extraction failed: the volume is PARTIAL. Keep the application stopped and "
                      "re-run the full restore. " + res.stderr.decode()[-300:])
    diffs = compare(m2["volume_manifest"], probe_volume(helper, vol))
    if diffs:
        _write_state(u4p, "BLOCKED_PARTIAL", volume=vol, reason="verification failed", diffs=diffs[:20])
        raise Blocked(f"restored volume differs from U4 (PARTIAL; keep stopped, re-restore): {diffs[:10]}")
    _write_state(u4p, "RESTORED_VERIFIED", volume=vol, forensic=str(fx), manifest_sha256=manifest_sha(u4p),
                 container_id=now["container"]["id"])
    print(json.dumps({"restored": True, "u4_id": m["u4_id"], "volume": vol, "forensic": str(fx)}, indent=1))
    log("restored and verified. Nothing was started. Next: start-rollback.")
    return EXIT_OK


def cmd_start_rollback(a):
    u4p = validate_u4_path(a.u4)
    with OperationLock(u4p.parent, a.project, "start-rollback"):
        return _start_rollback(a, u4p)


def _restored_state(u4p):
    st = u4p / "restore_state.json"
    state = json.loads(st.read_text()) if st.is_file() else {}
    if state.get("state") != "RESTORED_VERIFIED":
        raise Blocked(f"restore state is {state.get('state')!r}; only RESTORED_VERIFIED may be started")
    if state.get("manifest_sha256") != manifest_sha(u4p):
        raise Blocked("restore state does not belong to this exact U4 manifest")
    return state


def _start_rollback(a, u4p):
    m = verify_u4(u4p)
    _restored_state(u4p)
    helper = require_local_image_id(a.helper_image, "helper image")
    if helper != m["helper_image"]:
        raise Blocked(f"helper image {helper} != U4 helper image {m['helper_image']}")
    expected_image = m["identity"]["image_id"]
    require_local_image_id(a.rollback_image, "rollback image")       # refuses tags/names and absent images first
    if a.rollback_image != expected_image:
        raise Blocked(f"--rollback-image {a.rollback_image} != the image recorded in U4 {expected_image}")
    if image_commit(expected_image) != m["expected_commit"]:
        raise Blocked("rollback image /srv/VERSION does not carry the expected 147e315 commit")
    cur = discover(a.project, a.service)
    if stable_identity(cur) != stable_identity(m["identity"]):
        raise Blocked("deployment identity does not match the U4")
    if cur["container"]["running"] or cur["container"]["restarting"]:
        raise Blocked("a service container is running; it must be stopped")
    validate_u4_path(u4p, cur)
    diffs = compare(m["volume_manifest"], probe_volume(helper, cur["volume"]["name"]))
    if diffs:
        raise Blocked(f"volume no longer matches U4 since the restore: {diffs[:10]}")
    semantic_check(m["volume_manifest"], expect_schema_versions=SCHEMA9_VERSIONS)
    rdir = u4p.parent / f"rollback-run-{m['u4_id']}-{stamp()}-{secrets.token_hex(2)}"
    rdir.mkdir(mode=0o700)
    override = rdir / "rollback.override.yml"
    override.write_text(f"services:\n  {a.service}:\n    image: {expected_image}\n    pull_policy: never\n"
                        f"    build: !reset null\n")
    base = ["docker", "compose", "-p", a.project]
    for f in a.compose_file:
        base += ["-f", f]
    base += ["-f", str(override)]
    if a.env_file:
        base += ["--env-file", a.env_file]
    cfg = json.loads(run(base + ["config", "--format", "json"]).stdout.decode())
    svc = cfg["services"][a.service]
    if svc.get("image") != expected_image or "build" in svc or svc.get("pull_policy") != "never":
        raise Blocked(f"rendered rollback config is not pinned (image={svc.get('image')}, build={'build' in svc}, "
                      f"pull_policy={svc.get('pull_policy')})")
    run(base + ["create", "--no-build", "--pull", "never", "--force-recreate", a.service])
    new = discover(a.project, a.service)              # topology again: one container, no other volume users
    problems = []
    if new["image_id"] != expected_image:
        problems.append(f"image {new['image_id']} != {expected_image}")
    if stable_identity(new) != stable_identity(m["identity"]):
        problems.append("volume mapping differs from U4")
    rdiff = fingerprint_diff(m["identity"]["runtime"], new["runtime"])
    if rdiff:
        problems.append({"runtime differs from the recorded deployment": rdiff[:20]})
    if new["container"]["running"] or new["container"]["restarting"]:
        problems.append("container started before inspection")
    if problems:
        raise Blocked(f"created rollback container does not match the recorded deployment; NOT starting: {problems}")
    if _drill_fault(new) == "mutate_after_create":
        _drill_mutate(helper, new["volume"]["name"])
    # last checks immediately before start: U4 unchanged, volume exactly U4, same created container
    m2 = verify_u4(u4p)
    if manifest_sha(u4p) != _restored_state(u4p)["manifest_sha256"]:
        raise Blocked("U4 manifest changed after the restore")
    diffs = compare(m2["volume_manifest"], probe_volume(helper, new["volume"]["name"]))
    if diffs:
        raise Blocked(f"volume changed after the rollback container was created; NOT starting: {diffs[:10]}")
    final = discover(a.project, a.service)
    if not same_deployment(new, final) or final["container"]["running"] or final["container"]["restarting"]:
        raise Blocked("rollback container or its topology changed before start; NOT starting")
    run(["docker", "start", final["container"]["id"]])
    log("pinned rollback container started; running acceptance")
    return _accept_or_stop(a.project, a.service, final["container"]["id"], expected_image, m["expected_commit"],
                           SCHEMA9_VERSIONS, a.quiet_seconds, fault=_drill_fault(final))


ACCEPT_SNIPPET = r"""
import json, sqlite3, sys, time, urllib.request
import os
B = "http://127.0.0.1:8000"
O = "http://%s:%s" % (os.environ.get("BIND_HOST", "127.0.0.1"), os.environ.get("BIND_PORT", "8000"))
H = {"Origin": O, "Sec-Fetch-Site": "same-origin"}   # the app only trusts its own published origin
def g(p, h):
    with urllib.request.urlopen(urllib.request.Request(B + p, headers=h), timeout=20) as r:
        return json.loads(r.read())
for _ in range(90):
    try:
        g("/api/health", H); break
    except Exception:
        time.sleep(1)
tok = g("/api/auth/bootstrap", H).get("token"); A = {**H, "X-TIP-Auth": tok}
out = {"health": g("/api/health", H).get("ok"), "version": g("/api/settings/version", A).get("commit"),
       "scheduler": g("/api/scheduler/status", A).get("mode")}
j = g("/api/baseline/jobs/current", A)
out["job_active"] = bool(j.get("job_thread_alive")) or ((j.get("job") or {}).get("status") in ("running", "pause_requested"))
t1 = g("/api/diagnostics/uw-requests", A).get("totals", {})
time.sleep(float(sys.argv[1]))
t2 = g("/api/diagnostics/uw-requests", A).get("totals", {})
out["uw_attempts"] = [t1.get("http_attempts", 0), t2.get("http_attempts", 0)]
c = sqlite3.connect("file:/srv/data/tip_hackathon.db?mode=ro", uri=True)
out["schema_versions"] = [r[0] for r in c.execute("select version from schema_version order by version")]
out["integrity"] = c.execute("pragma quick_check").fetchone()[0]
print(json.dumps(out))
"""


def acceptance_errors(out, expected_commit, expect_schema):
    errs = []
    if not out.get("health"):
        errs.append("health not ok")
    if out.get("version") != expected_commit:
        errs.append(f"version {out.get('version')} != {expected_commit}")
    if out.get("scheduler") != "disabled":
        errs.append(f"scheduler {out.get('scheduler')}")
    if out.get("job_active"):
        errs.append("a baseline job is active")
    if out.get("uw_attempts") != [0, 0]:
        errs.append(f"provider requests observed {out.get('uw_attempts')}")
    if out.get("schema_versions") != expect_schema:
        errs.append(f"schema {out.get('schema_versions')} != {expect_schema}")
    if out.get("integrity") != "ok":
        errs.append(f"integrity {out.get('integrity')}")
    return errs


def _accept(cid, expected_image, expected_commit, expect_schema, quiet_seconds):
    c = docker_json("inspect", cid)[0]
    if not c["State"].get("Running"):
        raise Blocked("service container is not running")
    if c["Image"] != expected_image:
        raise Blocked(f"running image {c['Image']} != {expected_image}")
    res = run(["docker", "exec", cid, "python", "-c", ACCEPT_SNIPPET, str(quiet_seconds)], check=False, timeout=600)
    try:
        out = json.loads(res.stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Blocked(f"acceptance probe failed: {res.stderr.decode()[-400:]}")
    print(json.dumps(out, indent=1))
    errs = acceptance_errors(out, expected_commit, expect_schema)
    if errs:
        raise Blocked("acceptance failed: " + "; ".join(errs))
    return out


def _stop_and_confirm(cid):
    try:
        run(["docker", "stop", "--time", "30", cid], timeout=180)
    except BaseException as exc:  # noqa: BLE001
        log(f"docker stop failed: {exc}")
    try:
        st = docker_json("inspect", cid)[0]["State"]
    except BaseException as exc:  # noqa: BLE001
        raise Critical(f"could not confirm container {cid[:12]} is stopped ({exc}). STOP IT BY HAND and escalate.")
    if st.get("Running") or st.get("Restarting"):
        raise Critical(f"container {cid[:12]} is still {st.get('Status')} after a failed acceptance. "
                       "STOP IT BY HAND and escalate; the application is NOT in a safe state.")


def _accept_or_stop(project, service, cid, expected_image, expected_commit, expect_schema, quiet_seconds, fault=None):
    """Acceptance; on ANY failure the exact container is stopped and confirmed stopped."""
    try:
        if fault == "fail_acceptance":
            raise Blocked("drill fault: acceptance forced to fail")
        _accept(cid, expected_image, expected_commit, expect_schema, quiet_seconds)
    except BaseException as exc:
        _stop_and_confirm(cid)                      # raises Critical if it cannot be confirmed stopped
        raise Blocked(f"{exc}. The container {cid[:12]} was stopped and confirmed stopped; do not restart it, "
                      "escalate.") from exc
    log("acceptance PASSED")
    return EXIT_OK


def cmd_accept(a):
    require_local_image_id(a.expect_image, "expected image")
    cur = discover(a.project, a.service)
    schema = list(range(1, a.expect_schema + 1))
    return _accept_or_stop(a.project, a.service, cur["container"]["id"], a.expect_image, a.expect_commit, schema,
                           a.quiet_seconds, fault=_drill_fault(cur))


def build_parser():
    p = argparse.ArgumentParser(prog="r3recover", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def dep(sp, vol=True):
        sp.add_argument("--project", required=True)
        sp.add_argument("--service", required=True)
        if vol:
            sp.add_argument("--expect-volume")

    sp = sub.add_parser("preflight"); dep(sp)
    sp.add_argument("--expect-commit", default=EXPECTED_COMMIT_DEFAULT)
    sp = sub.add_parser("snapshot"); dep(sp)
    sp.add_argument("--backup-root", required=True)
    sp.add_argument("--helper-image", required=True)
    sp.add_argument("--expect-commit", default=EXPECTED_COMMIT_DEFAULT)
    sp.add_argument("--stop", action="store_true", help="stop the application container first")
    sp = sub.add_parser("verify"); sp.add_argument("--u4", required=True)
    sp = sub.add_parser("test-restore"); sp.add_argument("--u4", required=True); sp.add_argument("--helper-image", required=True)
    sp = sub.add_parser("rollback"); dep(sp, vol=False)
    sp.add_argument("--u4", required=True); sp.add_argument("--helper-image", required=True)
    sp.add_argument("--stop", action="store_true", help="with --approve: stop a running candidate first")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--approve", metavar="TOKEN")
    sp = sub.add_parser("start-rollback"); dep(sp, vol=False)
    sp.add_argument("--u4", required=True); sp.add_argument("--helper-image", required=True)
    sp.add_argument("--rollback-image", required=True)
    sp.add_argument("--compose-file", required=True, action="append")
    sp.add_argument("--env-file")
    sp.add_argument("--quiet-seconds", type=float, default=10.0)
    sp = sub.add_parser("accept"); dep(sp, vol=False)
    sp.add_argument("--expect-image", required=True)
    sp.add_argument("--expect-commit", default=EXPECTED_COMMIT_DEFAULT)
    sp.add_argument("--expect-schema", type=int, default=9)
    sp.add_argument("--quiet-seconds", type=float, default=10.0)
    return p


COMMANDS = {"preflight": cmd_preflight, "snapshot": cmd_snapshot, "verify": cmd_verify,
            "test-restore": cmd_test_restore, "rollback": cmd_rollback, "start-rollback": cmd_start_rollback,
            "accept": cmd_accept}


def main(argv=None):
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        rc = COMMANDS[a.cmd](a)
        return rc if rc == EXIT_OK else EXIT_INTERNAL
    except Critical as exc:
        print(f"CRITICAL: {exc}", file=sys.stderr)
        return EXIT_CRITICAL
    except Blocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return EXIT_BLOCKED
    except Exception as exc:  # noqa: BLE001 -- never succeed after an unexpected failure
        print(f"BLOCKED (internal error): {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
