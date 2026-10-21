"""A small, deterministic fake of the Docker / Compose CLI surface used by tools/recovery/r3recover.py.

It replaces subprocess.run inside the tool module, so the tool runs unmodified -- any version of it --
against an in-memory deployment: containers, volumes (whose "content" is a probe manifest), images and
helper containers. Hooks inject faults at exact points (after `compose create`, after the archive check,
on `docker stop`), and every command is logged so a test can assert, e.g., that `docker start` was never
called. Used by test_recovery_tool_v7.py for regression tests and for reproducing v6 defects."""
import copy
import json
import secrets
import subprocess
from pathlib import Path

OLD_IMAGE = "sha256:" + "1" * 64
HELPER = "sha256:" + "2" * 64
CANDIDATE = "sha256:" + "3" * 64
COMMIT = "147e3154b001cd6e73dde16e612978864ff6f500"


def good_volume_manifest(reference):
    ref9 = reference["states"]["9"]
    db = {"integrity_check": ["ok"], "foreign_key_violations": 0, "schema_versions": copy.deepcopy(reference["migrations"]),
          "migration_10_objects_present": [], "scheduler_mode": "disabled", "active_baseline_jobs": 0, "sidecars": [],
          "objects": copy.deepcopy(ref9["objects"]), "columns": copy.deepcopy(ref9["columns"])}
    demo = copy.deepcopy(db)
    entries = {".": {"type": "dir", "mode": "0o755", "uid": 0, "gid": 0}}
    for n in ("tip_hackathon.db", "tip_hackathon.demo.db", "settings.json", ".auth_token", ".pairing_code"):
        entries[n] = {"type": "file", "mode": "0o600", "uid": 0, "gid": 0, "size": 10, "sha256": n.encode().hex()[:64].ljust(64, "0")}
    return {"entries": entries, "unsupported": [], "sqlite": {"tip_hackathon.db": db, "tip_hackathon.demo.db": demo}}


def container_template(project, service, volume, image, mountpoint):
    return {
        "Id": secrets.token_hex(32), "Name": f"/{project}-{service}-1", "Image": image,
        "State": {"Status": "running", "Running": True, "Restarting": False},
        "Config": {"Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service,
                              "com.docker.compose.config-hash": secrets.token_hex(8)},
                   "Env": ["BIND_HOST=127.0.0.1", "BIND_PORT=8000", "TIP_MODE=live"],
                   "Cmd": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"], "Entrypoint": None,
                   "WorkingDir": "/srv/backend", "User": "", "Healthcheck": None, "ExposedPorts": {"8000/tcp": {}}},
        "HostConfig": {"PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8000"}]},
                       "RestartPolicy": {"Name": "no"}, "NetworkMode": f"{project}_default", "Privileged": False,
                       "CapAdd": None, "CapDrop": None, "SecurityOpt": None,
                       "Mounts": [{"Type": "volume", "Source": volume, "Target": "/srv/data"}]},
        "Mounts": [{"Type": "volume", "Name": volume, "Source": mountpoint, "Destination": "/srv/data",
                    "Driver": "local", "Mode": "z", "RW": True, "Propagation": ""}],
        "NetworkSettings": {"Networks": {f"{project}_default": {"Aliases": [f"{project}-{service}-1", service],
                                                                "IPAMConfig": None, "Links": None, "DriverOpts": None}}},
    }


class FakeDocker:
    def __init__(self, reference, project="p", service="s", mountpoint="/var/lib/docker/volumes/p_tip-data/_data"):
        self.project, self.service = project, service
        self.volume = f"{project}_tip-data"
        self.images = {OLD_IMAGE: COMMIT, HELPER: None, CANDIDATE: "c" * 40}
        # RepoTags per image; the 147e315 image carries the runbook E8 hold tag by default
        self.image_tags = {OLD_IMAGE: [f"{project}-{service}:latest", "tip-r3-hold:147e315"], HELPER: ["python:3.11-slim"],
                           CANDIDATE: []}
        self.volumes = {self.volume: {"Name": self.volume, "Driver": "local", "CreatedAt": "2026-10-21T00:00:00Z",
                                      "Mountpoint": mountpoint, "Labels": {"com.docker.compose.project": project,
                                                                           "com.docker.compose.volume": "tip-data"},
                                      "content": good_volume_manifest(reference)}}
        self.template = container_template(project, service, self.volume, OLD_IMAGE, mountpoint)
        c = copy.deepcopy(self.template)
        self.containers = {c["Id"]: c}
        self.calls = []
        self.hooks = {}
        self.accept_output = {"health": True, "version": COMMIT, "scheduler": "disabled", "job_active": False,
                              "uw_attempts": [0, 0], "schema_versions": list(range(1, 10)), "integrity": "ok"}
        self.stop_fails = False

    # --- helpers ------------------------------------------------------------------------------
    def service_container(self):
        ids = [i for i, c in self.containers.items() if c["Config"]["Labels"].get("com.docker.compose.project") == self.project
               and c["Config"]["Labels"].get("com.docker.compose.service") == self.service]
        return self.containers[ids[0]] if len(ids) == 1 else None

    def add_container(self, name, volume=None, running=False):
        c = {"Id": secrets.token_hex(32), "Name": f"/{name}", "Image": HELPER,
             "State": {"Status": "running" if running else "exited", "Running": running, "Restarting": False},
             "Config": {"Labels": {}, "Env": []}, "HostConfig": {},
             "Mounts": [{"Type": "volume", "Name": volume or self.volume, "Destination": "/x", "Driver": "local"}],
             "NetworkSettings": {"Networks": {}}}
        self.containers[c["Id"]] = c
        return c

    def started(self):
        return [c for c in self.calls if c[:2] == ["docker", "start"]]

    def _ok(self, out=b"", rc=0, err=b""):
        if isinstance(out, (dict, list)):
            out = json.dumps(out).encode()
        elif isinstance(out, str):
            out = out.encode()
        return subprocess.CompletedProcess([], rc, out, err)

    # --- the fake subprocess.run ------------------------------------------------------------------
    def __call__(self, cmd, capture_output=True, input=None, timeout=None, **kw):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[0] != "docker":
            raise AssertionError(f"unexpected command {cmd}")
        a = cmd[1:]
        if a[0] == "ps":
            if any(x.startswith("volume=") for x in a):
                v = [x for x in a if x.startswith("volume=")][0].split("=", 1)[1]
                ids = [i for i, c in self.containers.items() if any(m.get("Name") == v for m in c.get("Mounts") or [])]
            elif any(x.startswith("ancestor=") for x in a):
                img = [x for x in a if x.startswith("ancestor=")][0].split("=", 1)[1]
                ids = [i for i, c in self.containers.items() if c["Image"] == img]
                if "-a" not in a:
                    ids = [i for i in ids if self.containers[i]["State"]["Running"]]
            else:
                lab = dict(x.split("=", 1)[1].split("=", 1) for x in a if x.startswith("label="))
                ids = [i for i, c in self.containers.items()
                       if all(c["Config"]["Labels"].get(k) == v for k, v in lab.items())]
                if "-a" not in a:
                    ids = [i for i in ids if self.containers[i]["State"]["Running"]]
            return self._ok("\n".join(ids) + ("\n" if ids else ""))
        if a[0] == "inspect":
            c = self.containers.get(a[1])
            return self._ok([copy.deepcopy(c)]) if c else self._ok(rc=1, err=b"No such object")
        if a[:2] == ["volume", "inspect"]:
            v = self.volumes.get(a[2])
            return self._ok([{k: x for k, x in v.items() if k != "content"}]) if v else self._ok(rc=1, err=b"no such volume")
        if a[:2] == ["volume", "create"]:
            self.volumes[a[-1]] = {"Name": a[-1], "Driver": "local", "CreatedAt": "now", "Mountpoint": "/x",
                                   "Labels": {"r3.disposable": "1"}, "content": {"entries": {}, "unsupported": [], "sqlite": {}}}
            return self._ok(a[-1])
        if a[:2] == ["volume", "rm"]:
            self.volumes.pop(a[2], None)
            return self._ok()
        if a[:2] == ["image", "inspect"]:
            if a[2] not in self.images:
                return self._ok(rc=1, err=b"No such image")
            fmt = a[a.index("--format") + 1] if "--format" in a else ""
            return self._ok(json.dumps(self.image_tags.get(a[2], [])) if "RepoTags" in fmt else a[2])
        if a[0] == "tag":
            for img, tags in self.image_tags.items():
                if a[2] in tags:
                    tags.remove(a[2])
            self.image_tags.setdefault(a[1], []).append(a[2])
            return self._ok()
        if a[0] == "run":
            return self._run(a)
        if a[0] == "stop":
            c = self.containers[a[-1]]
            if self.stop_fails:
                return self._ok(rc=1, err=b"cannot stop")
            c["State"] = {"Status": "exited", "Running": False, "Restarting": False}
            return self._ok(a[-1])
        if a[0] == "start":
            c = self.containers[a[-1]]
            c["State"] = {"Status": "running", "Running": True, "Restarting": False}
            return self._ok(a[-1])
        if a[0] == "exec":
            out = self.accept_output
            if callable(out):
                return out(self)
            return self._ok(json.dumps(out) + "\n")
        if a[0] == "compose":
            return self._compose(a)
        raise AssertionError(f"unhandled docker command {cmd}")

    def _run(self, a):
        mounts, i = [], 1
        entry = None
        while i < len(a):
            if a[i] in ("--rm",):
                i += 1
            elif a[i] in ("--pull", "--network"):
                i += 2
            elif a[i] == "-v":
                src, dst, mode = a[i + 1].rsplit(":", 2) if a[i + 1].count(":") >= 2 else (*a[i + 1].split(":"), "rw")
                mounts.append((src, dst, mode)); i += 2
            elif a[i] == "--entrypoint":
                entry = a[i + 1]; i += 2
            else:
                break
        image, args = a[i], a[i + 1:]
        if image not in self.images:
            return self._ok(rc=125, err=b"Unable to find image locally (pull refused)")
        by_dst = {d: s for s, d, _ in mounts}
        if entry == "cat" and args == ["/srv/VERSION"]:
            commit = self.images[image]
            return self._ok(f"BUILD_COMMIT={commit}\n" if commit else "", rc=0 if commit else 1)
        if entry == "python" and args[:2] == ["/tool/r3probe.py", "manifest"]:
            return self._ok(self.volumes[by_dst["/target"]]["content"])
        if entry == "tar" and "-cpf" in args:
            out = args[args.index("-cpf") + 1]                      # /u4/volume.tar or /fx/volume-post-upgrade.tar
            d, name = out.rsplit("/", 1)
            if d == "/fx" and self.hooks.get("fail_forensic"):
                return self._ok(rc=2, err=b"tar: write error: No space left on device")
            Path(by_dst[d], name).write_text(json.dumps(self.volumes[by_dst["/src"]]["content"], sort_keys=True))
            return self._ok()
        if entry == "sh":
            script = args[-1]
            if "r3probe.py manifest /tmp/x" in script:            # archive re-extraction check
                out = self._ok(Path(by_dst["/u4"], "volume.tar").read_text())
                self._hook("after_archive_check")
                return out
            if "-xpf" in script:                                  # restore
                vol = self.volumes[by_dst["/dst"]]
                if "head -c" in script:
                    vol["content"] = {"entries": {".": {"type": "dir"}}, "unsupported": [], "sqlite": {}}
                    return self._ok(rc=2, err=b"tar: Unexpected EOF in archive")
                vol["content"] = json.loads(Path(by_dst["/u4"], "volume.tar").read_text())
                return self._ok()
            if "r3-drill-mutation" in script:
                self.mutate(self.volumes[by_dst["/v"]])
                return self._ok()
        raise AssertionError(f"unhandled helper run {a}")

    def mutate(self, vol=None):
        vol = vol or self.volumes[self.volume]
        vol["content"]["entries"][".injected"] = {"type": "file", "mode": "0o644", "uid": 0, "gid": 0, "size": 1,
                                                  "sha256": "f" * 64}

    def _hook(self, name):
        fn = self.hooks.get(name)
        if fn:
            fn(self)

    def _compose(self, a):
        files = [a[i + 1] for i, x in enumerate(a) if x == "-f"]
        override = Path(files[-1]).read_text()
        image = [ln.split("image:", 1)[1].strip() for ln in override.splitlines() if "image:" in ln][0]
        if "config" in a:
            return self._ok({"services": {self.service: {"image": image, "pull_policy": "never"}}})
        if "create" in a:
            for i in [i for i, c in self.containers.items()
                      if c["Config"]["Labels"].get("com.docker.compose.service") == self.service
                      and c["Config"]["Labels"].get("com.docker.compose.project") == self.project]:
                del self.containers[i]
            c = copy.deepcopy(self.template)
            c.update(Id=secrets.token_hex(32), Image=image, State={"Status": "created", "Running": False, "Restarting": False})
            c["Config"]["Labels"]["com.docker.compose.config-hash"] = secrets.token_hex(8)
            if self.hooks.get("mutate_created_container"):
                self.hooks["mutate_created_container"](c)
            self.containers[c["Id"]] = c
            self._hook("after_compose_create")
            return self._ok()
        raise AssertionError(f"unhandled compose command {a}")
