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

import hashlib
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Optional

_FIELDS = (
    "mode",
    "uw_api_key",
    "ai_provider",
    "ai_api_key",
    # Live-mode activation gate (see main.py /api/settings/mode): a hash
    # (never the raw key) of the LAST key that actually passed
    # /api/settings/uw-key/test, plus when. Only a key whose hash matches
    # this is allowed to be activated into "live" mode. Persisted (not the
    # in-memory-only _last_uw_test_result main.py already had) so the gate
    # survives process restarts, and keyed by hash so it is tied to the
    # exact key tested, not just "a key exists".
    "verified_uw_key_hash",
    "verified_uw_key_at",
)


def _defaults() -> dict:
    env_mode = os.environ.get("TIP_MODE", "demo").strip().lower()
    return {
        "mode": env_mode if env_mode in ("demo", "live") else "demo",
        "uw_api_key": None,
        "ai_provider": None,
        "ai_api_key": None,
        "verified_uw_key_hash": None,
        "verified_uw_key_at": None,
    }


def hash_key(api_key: Optional[str]) -> Optional[str]:
    """sha256 of a UW key for the verification record -- never store or
    return the raw key itself. Returns None for a falsy key so "no key"
    and "a hash of an empty string" are never confused."""
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


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
            # A newly-saved/replaced key has NOT itself passed Test
            # Connection yet -- invalidate any prior verification record so
            # live-mode activation requires a fresh test of THIS key. If
            # the app was already in "live" mode on the old (verified) key,
            # force it back to demo rather than silently keep running
            # "live" against an unverified replacement credential.
            data["verified_uw_key_hash"] = None
            data["verified_uw_key_at"] = None
            if data["mode"] == "live":
                data["mode"] = "demo"
            self._write(data)
            return data

    def clear_uw_key(self) -> dict:
        with self._lock:
            data = self._read()
            data["uw_api_key"] = None
            data["verified_uw_key_hash"] = None
            data["verified_uw_key_at"] = None
            if data["mode"] == "live":
                data["mode"] = "demo"  # never leave the app pointed at "live" with no key
            self._write(data)
            return data

    def record_verified_uw_key(self, api_key: str) -> dict:
        """Call ONLY after a real Test Connection call succeeds (result ==
        "success") for this exact key. Persists its hash + a timestamp so
        /api/settings/mode can later confirm live-mode is being activated
        for a credential that has actually passed Test Connection, not just
        any key that happens to be present (and not just an in-memory flag
        that forgets which key it was for)."""
        with self._lock:
            data = self._read()
            data["verified_uw_key_hash"] = hash_key(api_key)
            data["verified_uw_key_at"] = time.time()
            self._write(data)
            return data

    def is_key_verified(self, api_key: Optional[str]) -> bool:
        """True only if `api_key` is non-empty and its hash matches the
        hash of the last key that passed Test Connection. Applies equally
        to UI-saved and .env-sourced keys -- there is no "trusted by
        default" source, every key must pass its own Test Connection."""
        if not api_key:
            return False
        data = self.get_all()
        stored = data.get("verified_uw_key_hash")
        return stored is not None and stored == hash_key(api_key)

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
