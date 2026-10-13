"""Local-only settings/credential store.

Persists four fields to a small JSON file under data/ (gitignored, never
committed): current mode (demo|live), the saved UW API key (or None), the
saved AI provider name (or None), and the saved AI provider API key (or
None). This file is written server-side only -- nothing in this module is
ever imported by the frontend, and no route handler ever returns the raw
values of uw_api_key / ai_api_key (see backend/app/main.py's /api/settings*
routes: they return only booleans + last-4-characters).

On first run (no file yet), mode defaults to "demo" -- the safe default
required by the hackathon task. A fresh file is seeded once from TIP_MODE/
UW_API_KEY env vars if those happen to be set (keeps existing
docker-compose-driven deployments working without surprise), but every run
after that is controlled by this store, not the environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Optional

_FIELDS = ("mode", "uw_api_key", "ai_provider", "ai_api_key")


def _defaults() -> dict:
    env_mode = os.environ.get("TIP_MODE", "demo").strip().lower()
    return {
        "mode": env_mode if env_mode in ("demo", "live") else "demo",
        "uw_api_key": None,
        "ai_provider": None,
        "ai_api_key": None,
    }


class SettingsStore:
    """Thread-safe (within one process) JSON file store. One instance is
    created in main.py; tests may point a fresh instance at a tmp_path file
    for isolation."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = Lock()

    def _read(self) -> dict:
        if not self.path.exists():
            return _defaults()
        try:
            with open(self.path, "r") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return _defaults()
        data = _defaults()
        for key in _FIELDS:
            if key in raw:
                data[key] = raw[key]
        if data["mode"] not in ("demo", "live"):
            data["mode"] = "demo"
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump({k: data.get(k) for k in _FIELDS}, f)
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # best-effort on platforms/filesystems that don't support chmod

    def get_all(self) -> dict:
        with self._lock:
            return self._read()

    def get_mode(self) -> str:
        return self.get_all()["mode"]

    def set_mode(self, mode: str) -> dict:
        if mode not in ("demo", "live"):
            raise ValueError("mode must be 'demo' or 'live'")
        with self._lock:
            data = self._read()
            data["mode"] = mode
            self._write(data)
            return data

    def set_uw_key(self, api_key: str) -> dict:
        with self._lock:
            data = self._read()
            data["uw_api_key"] = api_key
            self._write(data)
            return data

    def clear_uw_key(self) -> dict:
        with self._lock:
            data = self._read()
            data["uw_api_key"] = None
            if data["mode"] == "live":
                data["mode"] = "demo"  # never leave the app pointed at "live" with no key
            self._write(data)
            return data

    def set_ai_key(self, provider: str, api_key: str) -> dict:
        with self._lock:
            data = self._read()
            data["ai_provider"] = provider
            data["ai_api_key"] = api_key
            self._write(data)
            return data

    def clear_ai_key(self) -> dict:
        with self._lock:
            data = self._read()
            data["ai_provider"] = None
            data["ai_api_key"] = None
            self._write(data)
            return data


def last4(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    return key[-4:] if len(key) >= 4 else key
