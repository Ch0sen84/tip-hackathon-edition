"""Build-time version/commit info for the running instance.

There is no `.git` directory inside the running Docker container (the
Dockerfile only COPYs source, never the repo's git history), so the
installed commit/version cannot be discovered at runtime by running `git`
commands. Instead it is baked in at IMAGE BUILD TIME via Docker build ARGs
(see Dockerfile: BUILD_COMMIT / BUILD_VERSION / BUILD_DATE) and written to
a plain `/srv/VERSION` file during the build. This module only ever READS
that file (or, for a non-Docker local run, falls back to the equivalent
environment variables) -- it never shells out to git and never guesses.

If neither the file nor the env vars are present (e.g. someone built the
image without passing the build args), every field is None. The frontend
and update_check.py both treat that honestly as "unknown installed
version" -- never fabricated, never assumed to be "latest".
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_VERSION_FILE_ENV = "TIP_VERSION_FILE"
_DEFAULT_VERSION_FILE_CANDIDATES = (
    "/srv/VERSION",
    str(Path(__file__).resolve().parents[2] / "VERSION"),
)


def _candidate_paths() -> list[str]:
    """REAL BUG FIX (2026-10-15, Priority 5 -- test-isolation around
    /srv/VERSION): TIP_VERSION_FILE is meant as an explicit OVERRIDE (used
    by every test in test_update_check.py via monkeypatch.setenv), but this
    used to always ALSO append the hardcoded /srv/VERSION and repo-root
    VERSION candidates after it. When a test deliberately points
    TIP_VERSION_FILE at a path that does NOT exist (e.g.
    test_get_build_info_missing_file_returns_all_none /
    test_update_check_route_unknown_version_when_nothing_baked_in), the
    lookup fell through to the real ambient /srv/VERSION on whatever
    machine the suite happens to run on -- a file this app's own Dockerfile
    creates at image-build time and that can genuinely exist on a dev/CI
    box from an unrelated local build, leaking that machine's real state
    into a test that explicitly asked for "no version file present". An
    explicit override must be authoritative: when TIP_VERSION_FILE is set,
    ONLY that exact path is ever considered (missing means missing, never
    a silent fallback to ambient filesystem state). The hardcoded defaults
    are only consulted when the env var is unset entirely -- the real
    production/Docker case this was designed for.
    """
    env_path = os.environ.get(_VERSION_FILE_ENV)
    if env_path:
        return [env_path]
    return list(_DEFAULT_VERSION_FILE_CANDIDATES)


def _read_version_file() -> dict:
    for path in _candidate_paths():
        p = Path(path)
        if not p.exists():
            continue
        try:
            data = {}
            for line in p.read_text().splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
            return data
        except OSError:
            continue
    return {}


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def get_build_info() -> dict:
    """Returns {"commit": str|None, "version": str|None, "built_at": str|None}.
    `commit` is expected to be a short or full git SHA when present -- see
    update_check.py for how that is validated before being compared
    against GitHub."""
    file_data = _read_version_file()
    commit = _clean(file_data.get("BUILD_COMMIT")) or _clean(os.environ.get("TIP_BUILD_COMMIT"))
    version = _clean(file_data.get("BUILD_VERSION")) or _clean(os.environ.get("TIP_BUILD_VERSION"))
    built_at = _clean(file_data.get("BUILD_DATE")) or _clean(os.environ.get("TIP_BUILD_DATE"))
    return {"commit": commit, "version": version, "built_at": built_at}
