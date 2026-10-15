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
    paths = []
    env_path = os.environ.get(_VERSION_FILE_ENV)
    if env_path:
        paths.append(env_path)
    paths.extend(_DEFAULT_VERSION_FILE_CANDIDATES)
    return paths


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
