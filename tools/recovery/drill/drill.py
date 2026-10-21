#!/usr/bin/env python3
"""DISPOSABLE Release 3 recovery drill: positive path + negative matrix.

Every step invokes the real tools/recovery/r3recover.py subcommands. Every Compose project is
prefixed 'r3drill' and every network is internal; no production container, volume or
credential is used. Synthetic data only (seed_volume.py, fake_uw_server.py).

    python drill.py --old-image sha256:... --candidate-image sha256:... --helper-image sha256:...
                    --candidate-commit <sha> --workdir <dir for U4/evidence>

Writes <workdir>/evidence.json and prints a summary. Exit 0 only if EVERY positive step
succeeded and EVERY negative case exited non-zero with the application not started.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DRILL = Path(__file__).resolve().parent
REPO = DRILL.parent.parent.parent
TOOL = DRILL.parent / "r3recover.py"
BASE_COMPOSE = REPO / "docker-compose.yml"
OVERRIDE = DRILL / "compose.drill.yml"
ENVF = DRILL / "drill.env"
SERVICE = "tip-hackathon"
EVIDENCE = []


def sh(cmd, env=None, check=False, timeout=900):
    e = dict(os.environ, **(env or {}))
    r = subprocess.run([str(c) for c in cmd], capture_output=True, env=e, timeout=timeout)
    if check and r.returncode != 0:
        raise SystemExit(f"setup command failed: {cmd}\n{r.stderr.decode()[-1500:]}")
    return r


class Drill:
    def __init__(self, a):
        self.a = a
        self.work = Path(a.workdir).resolve()
        self.work.mkdir(parents=True, exist_ok=True)

    # --- compose plumbing ------------------------------------------------------------------
    def cenv(self, image):
        return {"R3_APP_IMAGE": image, "R3_FAKE_IMAGE": self.a.old_image, "R3_DRILL_DIR": str(DRILL)}

    def compose(self, project, image, *args, extra=(), check=True):
        if not project.startswith("r3drill"):          # never operate on a non-disposable project
            raise SystemExit(f"refusing to run Compose for non-drill project {project!r}")
        cmd = ["docker", "compose", "-p", project, "-f", BASE_COMPOSE, "-f", OVERRIDE]
        for x in extra:
            cmd += ["-f", x]
        cmd += ["--env-file", ENVF, *args]
        return sh(cmd, env=self.cenv(image), check=check)

    def volume(self, project):
        return f"{project}_tip-data"

    def deploy(self, project, *, seed=(), start=True, extra=(), image=None):
        """Create a disposable deployment, seed its volume, optionally start the app."""
        image = image or self.a.old_image
        self.compose(project, image, "create", "--no-build", "--pull", "never", extra=extra, check=True)
        vol = self.volume(project)
        sh(["docker", "run", "--rm", "--pull", "never", "--network", "none", "-v", f"{vol}:/srv/data",
            "-v", f"{DRILL}:/r:ro", "--entrypoint", "python", self.a.old_image, "/r/seed_volume.py", "/srv/data", *seed],
           check=True)
        if start:
            self.compose(project, image, "start", extra=extra, check=True)
            self.wait_health(project)

    def wait_health(self, project):
        cid = self.container(project)
        for _ in range(90):
            r = sh(["docker", "exec", cid, "python", "-c",
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)"])
            if r.returncode == 0:
                return
            time.sleep(1)
        raise SystemExit(f"{project}: app did not become healthy")

    def container(self, project):
        ids = sh(["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}",
                  "--filter", f"label=com.docker.compose.service={SERVICE}"], check=True).stdout.decode().split()
        return ids[0] if ids else None

    def app_running(self, project):
        cids = sh(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                   "--filter", f"label=com.docker.compose.service={SERVICE}"]).stdout.decode().split()
        return bool(cids)

    def down(self, project, image=None, extra=()):
        self.compose(project, image or self.a.old_image, "down", "-v", "--remove-orphans", extra=extra)

    # --- tool invocation + evidence -------------------------------------------------------------
    def r3(self, *args, env=None):
        r = sh([sys.executable, TOOL, *args], env=env)
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def step(self, kind, name, args, expect_rc, project, env=None, note=""):
        rc, out, err = self.r3(*args, env=env)
        running = self.app_running(project) if project else None
        ok = (rc == expect_rc) if kind == "positive" else (rc != 0 and rc == expect_rc and not running)
        rec = {"kind": kind, "name": name, "command": "python tools/recovery/r3recover.py " + " ".join(map(str, args)),
               "env": {k: v for k, v in (env or {}).items() if k.startswith("R3_")},
               "expected_exit": expect_rc, "observed_exit": rc,
               "expected_outcome": note, "observed_outcome": (err.strip().splitlines() or [""])[-1][:400],
               "app_running_after": running, "result": "PASS" if ok else "FAIL"}
        EVIDENCE.append(rec)
        print(f"[{rec['result']}] {kind:8} {name}: exit {rc} (expected {expect_rc}) app_running={running}", flush=True)
        return rc, out, err

    def fact(self, name, ok, detail):
        EVIDENCE.append({"kind": "check", "name": name, "result": "PASS" if ok else "FAIL", "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] check    {name}: {detail}", flush=True)

    # --- helpers on volumes ---------------------------------------------------------------------
    def counts(self, vol):
        code = ("import sqlite3,shutil,os,json,tempfile;t=tempfile.mkdtemp()\n"
                "for f in os.listdir('/v'):\n"
                "    if f.startswith('tip_hackathon.db') and not f.endswith('.bak'): shutil.copy2('/v/'+f,t+'/'+f)\n"
                "c=sqlite3.connect(t+'/tip_hackathon.db')\n"
                "q=lambda s:c.execute(s).fetchone()[0]\n"
                "tabs={r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")}\n"
                "print(json.dumps({'schema':q('select max(version) from schema_version'),"
                "'darkpool':q('select count(*) from darkpool_observations'),'flow':q('select count(*) from options_flow_observations'),"
                "'coverage':q('select count(*) from session_coverage'),'jobs':q('select count(*) from baseline_jobs'),"
                "'diagnostics':q('select count(*) from collection_diagnostics') if 'collection_diagnostics' in tabs else None,"
                "'files':sorted(os.listdir('/v'))}))")
        r = sh(["docker", "run", "--rm", "--pull", "never", "--network", "none", "-v", f"{vol}:/v:ro",
                "--entrypoint", "python", self.a.helper_image, "-c", code], check=True)
        return json.loads(r.stdout.decode())

    def mutate(self, vol, shell):
        sh(["docker", "run", "--rm", "--pull", "never", "--network", "none", "-v", f"{vol}:/v",
            "--entrypoint", "sh", self.a.helper_image, "-c", shell], check=True)

    def copy_u4(self, u4, name, edit):
        dst = self.work / name
        if dst.exists():
            shutil.rmtree(dst, onerror=lambda f, p, e: (os.chmod(p, 0o600), f(p)))
        shutil.copytree(u4, dst)
        for f in dst.iterdir():
            os.chmod(f, 0o600)
        edit(dst)
        return dst

    @staticmethod
    def resum(u4):
        import hashlib
        h = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        (u4 / "SHA256SUMS").write_text(f"{h(u4 / 'volume.tar')}  volume.tar\n{h(u4 / 'manifest.json')}  manifest.json\n")

    # --- the drill --------------------------------------------------------------------------------
    def run(self):
        a = self.a
        H, OLD, CAND = a.helper_image, a.old_image, a.candidate_image
        P = "r3drill"
        bk = self.work / "u4"
        bk.mkdir(exist_ok=True)
        vol = self.volume(P)
        self.down(P)
        self.deploy(P)
        dep = ["--project", P, "--service", SERVICE]

        # ---------------- POSITIVE PATH ----------------
        self.step("positive", "P1 preflight discovers the running drill deployment", ["preflight", *dep, "--expect-volume", vol], 0, None)
        rc, out, _ = self.step("positive", "P2 snapshot (--stop): quiescence + U4 + manifest",
                               ["snapshot", *dep, "--expect-volume", vol, "--backup-root", bk, "--helper-image", H, "--stop"], 0, None)
        u4 = Path(json.loads(out)["u4_dir"])
        self.fact("P2 app stopped by snapshot and not restarted", not self.app_running(P), "service container stopped")
        self.step("positive", "P3 verify U4", ["verify", "--u4", u4], 0, None)
        self.step("positive", "P4 test-restore into a disposable volume (exact match)", ["test-restore", "--u4", u4, "--helper-image", H], 0, None)
        u4_counts = self.counts(vol)

        # candidate upgrade + candidate writes (observations, job, diagnostics, failed provider requests)
        self.compose(P, CAND, "up", "-d", "--no-build", "--pull", "never", "--force-recreate", SERVICE, check=True)
        self.wait_health(P)
        self.step("positive", "P5 candidate acceptance (schema 10, scheduler disabled, 0 provider requests)",
                  ["accept", *dep, "--expect-image", CAND, "--expect-commit", a.candidate_commit, "--expect-schema", "10",
                   "--quiet-seconds", "3"], 0, P)
        act = sh(["docker", "exec", self.container(P), "python", "-c", CANDIDATE_ACTIVITY], timeout=600)
        activity = json.loads(act.stdout.decode().strip().splitlines()[-1]) if act.returncode == 0 else {"error": act.stderr.decode()[-300:]}
        cand_counts = self.counts(vol)
        self.fact("P6 candidate wrote observations, coverage, jobs and diagnostics and made failed provider requests",
                  cand_counts["schema"] == 10 and cand_counts["darkpool"] > u4_counts["darkpool"] and cand_counts["jobs"] > u4_counts["jobs"]
                  and (cand_counts["diagnostics"] or 0) > 0 and activity.get("failed_provider_requests", 0) > 0,
                  {"u4": u4_counts, "candidate": cand_counts, "activity": activity})

        # dry-run must not change anything, even with the candidate running
        before = self.counts(vol)
        rc, out, _ = self.step("positive", "P7 rollback --dry-run (plan + token, no change)", ["rollback", *dep, "--u4", u4, "--helper-image", H, "--dry-run"], 0, None)
        token = json.loads(out)["approval_token"]
        self.fact("P7 dry-run changed nothing and did not stop the candidate", self.counts(vol) == before and self.app_running(P), "volume and container unchanged")
        self.step("negative", "N-token wrong approval token", ["rollback", *dep, "--u4", u4, "--helper-image", H, "--approve", "RESTORE:wrong", "--stop"], 2, None,
                  note="BLOCKED, nothing stopped or restored")
        self.fact("N-token left the volume untouched", self.counts(vol) == before, "counts unchanged")
        self.compose(P, CAND, "stop", SERVICE, check=True)

        # ---------------- NEGATIVE MATRIX (part 1: before restore) ----------------
        self.step("negative", "N1 missing snapshot", ["rollback", *dep, "--u4", self.work / "no-such-u4", "--helper-image", H, "--dry-run"], 2, P,
                  note="BLOCKED: U4 directory does not exist")
        dmg = self.copy_u4(u4, "damaged-u4", lambda d: _flip_byte(d / "volume.tar"))
        self.step("negative", "N2 damaged archive (bad checksum)", ["verify", "--u4", _as_named(dmg, u4.name, self.work)], 2, P,
                  note="BLOCKED: checksum mismatch volume.tar")
        def edit_identity(d):
            import hashlib
            m = json.loads((d / "manifest.json").read_text()); m["identity"]["volume"]["created_at"] = "2000-01-01T00:00:00Z"
            (d / "manifest.json").write_text(json.dumps(m)); self.resum(d)
            tr = json.loads((d / "test_restore.json").read_text())     # attacker-consistent test-restore record
            tr["manifest_sha256"] = hashlib.sha256((d / "manifest.json").read_bytes()).hexdigest()
            (d / "test_restore.json").write_text(json.dumps(tr))
        self.step("negative", "N4a manifest identity mismatch (consistent checksums)",
                  ["rollback", *dep, "--u4", _as_named(self.copy_u4(u4, "ident-u4", edit_identity), u4.name, self.work),
                   "--helper-image", H, "--dry-run"], 2, P, note="BLOCKED: different deployment identity / no matching test-restore")
        def edit_archive_hash(d):
            m = json.loads((d / "manifest.json").read_text()); m["archive"]["sha256"] = "0" * 64
            (d / "manifest.json").write_text(json.dumps(m)); self.resum(d)
        self.step("negative", "N4b manifest records a different archive hash",
                  ["verify", "--u4", _as_named(self.copy_u4(u4, "ahash-u4", edit_archive_hash), u4.name, self.work)], 2, P,
                  note="BLOCKED: manifest archive SHA-256 != volume.tar")
        self.step("negative", "N9 wrong helper image", ["rollback", *dep, "--u4", u4, "--helper-image", OLD, "--dry-run"], 2, P,
                  note="BLOCKED: helper image != U4 helper image")
        sleeper = sh(["docker", "run", "-d", "--pull", "never", "--network", "none", "-v", f"{vol}:/x:ro",
                      "--entrypoint", "sleep", H, "600"], check=True).stdout.decode().strip()
        self.step("negative", "N13 another running container uses the volume",
                  ["rollback", *dep, "--u4", u4, "--helper-image", H, "--dry-run"], 2, P, note="BLOCKED: another container uses the volume")
        sh(["docker", "rm", "-f", sleeper], check=True)

        # wrong-deployment archive: a second, fully verified drill deployment
        PB = "r3drillb"
        self.down(PB)
        self.deploy(PB)
        bkb = self.work / "u4b"; bkb.mkdir(exist_ok=True)
        rcb, outb, _ = self.r3("snapshot", "--project", PB, "--service", SERVICE, "--backup-root", bkb, "--helper-image", H, "--stop")
        u4b = Path(json.loads(outb)["u4_dir"])
        self.r3("test-restore", "--u4", u4b, "--helper-image", H)
        self.step("negative", "N3 correctly checksummed, test-restored U4 from the WRONG deployment",
                  ["rollback", *dep, "--u4", u4b, "--helper-image", H, "--approve", f"RESTORE:{u4b.name}:{vol}"], 2, P,
                  note="BLOCKED: U4 belongs to a different deployment")
        self.fact("N3 left the candidate volume untouched", self.counts(vol)["schema"] == 10, "schema still 10 (no restore)")
        self.down(PB)

        # ---------------- PARTIAL / INTERRUPTED RESTORE ----------------
        self.step("negative", "N14 interrupted restore (archive cut mid-stream)",
                  ["rollback", *dep, "--u4", u4, "--helper-image", H, "--approve", token], 2, P,
                  env={"R3_DRILL_FAULT": "interrupt_extract"}, note="BLOCKED: restore extraction failed; volume PARTIAL")
        state = json.loads((u4 / "restore_state.json").read_text())["state"]
        self.fact("N14 restore state recorded as BLOCKED_PARTIAL", state == "BLOCKED_PARTIAL", state)
        self.step("negative", "N14b start-rollback refused while PARTIAL",
                  ["start-rollback", *dep, "--u4", u4, "--helper-image", H, "--rollback-image", OLD, "--compose-file", BASE_COMPOSE,
                   "--compose-file", OVERRIDE, "--env-file", ENVF], 2, P, env=self.cenv(OLD), note="BLOCKED: state BLOCKED_PARTIAL")

        # ---------------- FULL RESTORE (positive) ----------------
        self.step("positive", "P8 full restore (--approve) after the partial attempt", ["rollback", *dep, "--u4", u4, "--helper-image", H, "--approve", token], 0, P)
        restored = self.counts(vol)
        self.fact("P8 every post-U4 write is gone (counts and file set equal U4)", restored == u4_counts,
                  {"u4": u4_counts, "candidate": cand_counts, "restored": restored})

        # ---------------- NEGATIVE MATRIX (part 2: tampering after a verified restore) ----------------
        tamper = [
            ("N15a hidden file missing", "rm /v/.r3-hidden-marker"),
            ("N15b hidden file altered", "printf X >> /v/.r3-hidden-marker"),
            ("N16a wrong file mode", "chmod 0644 /v/.auth_token"),
            ("N16b wrong symlink target", "ln -sfn tip_hackathon.db /v/settings.link"),
            ("N17 missing empty directory", "rmdir /v/empty.d"),
            ("N18 Demo DB corrupted", "printf 'XXXXXXXX' | dd of=/v/tip_hackathon.demo.db bs=1 seek=100 conv=notrunc 2>/dev/null"),
            ("N19a settings.json hash differs", "printf ' ' >> /v/settings.json"),
            ("N19b .auth_token hash differs", "printf Z >> /v/.auth_token"),
            ("N19c .pairing_code hash differs", "printf 1 >> /v/.pairing_code"),
            ("N19d wrong owner", "chown 0:0 /v/keep/sub/owned.txt"),
        ]
        start_args = ["start-rollback", *dep, "--u4", u4, "--helper-image", H, "--rollback-image", OLD,
                      "--compose-file", BASE_COMPOSE, "--compose-file", OVERRIDE, "--env-file", ENVF]
        for name, cmd in tamper:
            self.mutate(vol, cmd)
            self.step("negative", name, start_args, 2, P, env=self.cenv(OLD), note="BLOCKED: volume no longer matches U4; not started")
            self.r3("rollback", *dep, "--u4", u4, "--helper-image", H, "--approve", token)   # clean re-restore
        self.step("negative", "N20a rollback image given as a mutable tag",
                  [*start_args[:start_args.index("--rollback-image") + 1], "tip-hackathon-edition-tip-hackathon:latest",
                   *start_args[start_args.index("--rollback-image") + 2:]], 2, P, env=self.cenv(OLD),
                  note="BLOCKED: tags refused (immutable sha256 ID required)")
        self.step("negative", "N8 wrong rollback image (the candidate)",
                  [*start_args[:start_args.index("--rollback-image") + 1], CAND, *start_args[start_args.index("--rollback-image") + 2:]],
                  2, P, env=self.cenv(OLD), note="BLOCKED: --rollback-image != U4 image")

        # ---------------- START PINNED ROLLBACK (positive) ----------------
        self.step("positive", "P9 start-rollback: pinned 147e315 via Compose (no build/pull), config checked before start, acceptance",
                  [*start_args, "--quiet-seconds", "5"], 0, None, env=self.cenv(OLD))
        self.step("positive", "P10 independent acceptance (version 147e315, schema 1-9, scheduler disabled, 0 requests)",
                  ["accept", *dep, "--expect-image", OLD, "--quiet-seconds", "5"], 0, None)
        self.fact("P10 rollback container runs the pinned image", sh(["docker", "inspect", self.container(P), "--format", "{{.Image}}"]).stdout.decode().strip() == OLD, OLD)

        # ---------------- DISCOVERY / QUIESCENCE NEGATIVES ----------------
        self.step("negative", "N-vol misspelled --expect-volume (nothing created)", ["preflight", *dep, "--expect-volume", "r3drill_tip-dta"], 2, None,
                  note="BLOCKED: wrong volume; no volume created")
        self.fact("N-vol no volume was created", sh(["docker", "volume", "inspect", "r3drill_tip-dta"]).returncode != 0, "volume absent")
        self.step("negative", "N-proj misspelled project (nothing created)", ["preflight", "--project", "r3drlll", "--service", SERVICE], 2, None,
                  note="BLOCKED: no container")
        variants = self.variants()
        for name, proj, files, kw in variants:
            self.down(proj, extra=files)
            self.deploy(proj, extra=files, start=kw.get("start", False), seed=kw.get("seed", ()), image=kw.get("image"))
            if kw.get("image"):                    # N5: the candidate migrated the volume to schema 10; put the
                self.compose(proj, kw["image"], "stop", SERVICE, extra=files, check=True)   # 147e315 image back
                self.compose(proj, self.a.old_image, "create", "--no-build", "--pull", "never", "--force-recreate",
                             SERVICE, extra=files, check=True)                             # (never started)
            if kw.get("scale"):
                self.compose(proj, OLD, "up", "--no-start", "--no-build", "--pull", "never", "--scale", f"{SERVICE}=2", extra=files, check=True)
            args = ["snapshot", "--project", proj, "--service", SERVICE, "--backup-root", self.work / "neg", "--helper-image", H, "--stop"] \
                if kw.get("snapshot") else ["preflight", "--project", proj, "--service", SERVICE]
            (self.work / "neg").mkdir(exist_ok=True)
            self.step("negative", name, args, 2, proj, note=kw["note"])
            self.down(proj, extra=files)

        # unexpected pull: U4 records an image that is no longer present locally
        self.pull_negative()
        self.down(P)
        return EVIDENCE

    def variants(self):
        d = self.work / "variants"
        d.mkdir(exist_ok=True)
        def f(name, text):
            p = d / name; p.write_text(text); return p
        nomount = f("nomount.yml", "services:\n  tip-hackathon:\n    volumes: !override\n      - tip-data:/data\n")
        bind = f("bind.yml", "services:\n  tip-hackathon:\n    volumes: !override\n      - type: bind\n"
                 f"        source: {(d / 'bindsrc').as_posix()}\n        target: /srv/data\n")
        (d / "bindsrc").mkdir(exist_ok=True)
        extra = f("extra.yml", "services:\n  tip-hackathon:\n    volumes:\n      - extra-data:/srv/extra\nvolumes:\n  extra-data: {}\n")
        return [
            ("N10a no /srv/data mount (volume mounted elsewhere)", "r3drill-nomount", [nomount], {"note": "BLOCKED: no mount at /srv/data"}),
            ("N10b ambiguous: two service containers", "r3drill-dup", [], {"scale": True, "note": "BLOCKED: expected exactly one container"}),
            ("N11 bind mount instead of a named volume", "r3drill-bind", [bind], {"note": "BLOCKED: bind mounts refused"}),
            ("N12 unexpected additional persistent mount", "r3drill-extra", [extra], {"note": "BLOCKED: additional mounts"}),
            ("N5 wrong schema (volume already at schema 10)", "r3drill-schema10", [],
             {"start": True, "snapshot": True, "image": self.a.candidate_image, "note": "BLOCKED: schema 1-10 != 1-9"}),
            ("N6 persisted scheduler enabled", "r3drill-sched", [],
             {"seed": ("--scheduler", "enabled"), "snapshot": True, "note": "BLOCKED: scheduler 'enabled'"}),
            ("N7 unexpected active baseline job", "r3drill-job", [],
             {"seed": ("--active-job",), "snapshot": True, "note": "BLOCKED: active baseline job"}),
            ("N18b Demo DB corrupted at snapshot time", "r3drill-demo", [],
             {"seed": ("--corrupt-demo",), "snapshot": True, "note": "BLOCKED: integrity_check"}),
            ("N21 unsupported filesystem entry (FIFO)", "r3drill-fifo", [],
             {"seed": ("--fifo",), "snapshot": True, "note": "BLOCKED: unsupported entry (never silently dropped)"}),
            ("N22 unsupported filesystem entry (hard link)", "r3drill-hlink", [],
             {"seed": ("--hardlink",), "snapshot": True, "note": "BLOCKED: hard link refused"}),
        ]

    def pull_negative(self):
        a, H = self.a, self.a.helper_image
        P = "r3drill-pull"
        tag = "r3drill-throwaway:once"
        ctx = self.work / "throwaway-ctx"
        ctx.mkdir(exist_ok=True)
        base_tag = "tip-hackathon-edition-tip-hackathon:latest"
        if sh(["docker", "image", "inspect", base_tag, "--format", "{{.Id}}"], check=True).stdout.decode().strip() != a.old_image:
            raise SystemExit("base tag no longer points at the 147e315 image")
        (ctx / "Dockerfile").write_text(f"FROM {base_tag}\nLABEL r3.drill.throwaway=1\n")
        sh(["docker", "build", "-q", "--pull=false", "-t", tag, ctx], check=True)
        tid = sh(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], check=True).stdout.decode().strip()
        self.down(P)
        self.deploy(P, image=tid)
        bk = self.work / "u4pull"; bk.mkdir(exist_ok=True)
        rc, out, err = self.r3("snapshot", "--project", P, "--service", SERVICE, "--backup-root", bk, "--helper-image", H, "--stop")
        if rc != 0:
            self.fact("N20b setup snapshot", False, err[-300:]); return
        u4 = Path(json.loads(out)["u4_dir"])
        self.r3("test-restore", "--u4", u4, "--helper-image", H)
        token = f"RESTORE:{u4.name}:{self.volume(P)}"
        self.r3("rollback", "--project", P, "--service", SERVICE, "--u4", u4, "--helper-image", H, "--approve", token)
        self.compose(P, a.candidate_image, "create", "--no-build", "--pull", "never", "--force-recreate", SERVICE, check=True)
        sh(["docker", "rmi", tag], check=True)
        gone = sh(["docker", "image", "inspect", tid]).returncode != 0
        t0 = str(int(time.time()))
        self.step("negative", "N20b U4 image no longer local: no pull, no build, no start",
                  ["start-rollback", "--project", P, "--service", SERVICE, "--u4", u4, "--helper-image", H, "--rollback-image", tid,
                   "--compose-file", BASE_COMPOSE, "--compose-file", OVERRIDE, "--env-file", ENVF], 2, P, env=self.cenv(tid),
                  note="BLOCKED: image not present locally; refusing to pull or build")
        ev = sh(["docker", "events", "--since", t0, "--until", str(int(time.time()) + 1), "--filter", "type=image",
                 "--format", "{{.Action}} {{.Actor.ID}}"]).stdout.decode()
        self.fact("N20b no image pull/build event occurred", gone and "pull" not in ev and "build" not in ev, ev.strip() or "no image events")
        self.down(P, image=a.candidate_image)


CANDIDATE_ACTIVITY = r"""
import json, time, urllib.request, urllib.error
import os
B="http://127.0.0.1:8000"; O="http://%s:%s"%(os.environ.get("BIND_HOST","127.0.0.1"),os.environ.get("BIND_PORT","8000"))
H={"Origin":O,"Sec-Fetch-Site":"same-origin","Content-Type":"application/json"}
def call(m,p,body=None,h=None):
    r=urllib.request.Request(B+p,method=m,headers=h or H,data=json.dumps(body).encode() if body is not None else (b"{}" if m=="POST" else None))
    try:
        with urllib.request.urlopen(r,timeout=120) as x: return x.status,json.loads(x.read() or b"null")
    except urllib.error.HTTPError as e: return e.code,None
tok=call("GET","/api/auth/bootstrap")[1]["token"]; A={**H,"X-TIP-Auth":tok}
s,b=call("POST","/api/baseline/jobs",{"tickers":["SYNT"],"max_pages_per_ticker":12,"step_pages":10},A)
for _ in range(240):
    j=call("GET","/api/baseline/jobs/current",h=A)[1]["job"]
    if j and j["status"] in ("completed","paused","cancelled"): break
    time.sleep(0.5)
call("POST","/api/ticker/FAILX/baseline/backfill?max_pages=2",h=A)
t=call("GET","/api/diagnostics/uw-requests",h=A)[1]
failed=sum(v.get("failed",0) for v in t.get("by_path",{}).values())
print(json.dumps({"job_status":j["status"],"job_pages":j["pages_used"],"provider_totals":t.get("totals"),"failed_provider_requests":failed}))
"""


def _flip_byte(p):
    b = bytearray(p.read_bytes()); b[len(b) // 2] ^= 0xFF; p.write_bytes(bytes(b))


def _as_named(d, name, work):
    """A tampered copy must carry the original U4 directory name (manifest u4_id check)."""
    tgt = work / "named" / d.name / name
    if tgt.exists():
        shutil.rmtree(tgt)
    tgt.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(d, tgt)
    return tgt


def main():
    p = argparse.ArgumentParser()
    for k in ("--old-image", "--candidate-image", "--helper-image", "--candidate-commit", "--workdir"):
        p.add_argument(k, required=True)
    a = p.parse_args()
    ev = Drill(a).run()
    out = Path(a.workdir) / "evidence.json"
    out.write_text(json.dumps(ev, indent=1))
    fails = [e for e in ev if e["result"] != "PASS"]
    tot = {k: sum(1 for e in ev if e["kind"] == k) for k in ("positive", "negative", "check")}
    print(f"\nSUMMARY: {len(ev) - len(fails)}/{len(ev)} PASS ({tot}); evidence: {out}")
    for f in fails:
        print("  FAIL:", f["name"], f.get("observed_exit"), f.get("observed_outcome") or f.get("detail"))
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    main()
