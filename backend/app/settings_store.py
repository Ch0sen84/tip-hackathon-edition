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
import logging
import os
import time
from datetime import datetime, timezone
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
    # Market Radar (2026-10-14, Phase A): user-editable watchlist +
    # last-scan telemetry, persisted the same way everything else here
    # is -- survives container recreation, same file. `watchlist` is
    # None until the user ever saves one (falls back to the env-seeded
    # default, see get_watchlist() below).
    #
    # UX remediation item 2 (2026-10-16): last-scan telemetry is now
    # scoped PER MODE (last_scan_at_demo/last_scan_count_demo vs.
    # last_scan_at_live/last_scan_count_live) rather than one shared
    # pair. REAL BUG this fixes: the old single `last_scan_at`/
    # `last_scan_count` pair was written on EVERY real live_fetch in
    # _radar_impl, including every ordinary Demo Mode page load (Demo
    # Mode's adapter is never a LiveUWAdapter, so live_fetch is always
    # True there) -- a Demo-mode scan timestamp/count then leaked
    # straight into Live Mode's displayed freshness the moment the user
    # switched modes, because Live Mode's cache-only GET /api/radar read
    # that SAME shared field with zero awareness it was ever written by
    # a different mode. Two independent pairs, written and read by the
    # mode the route is actually scoped to (see _radar_impl's
    # `record_scan(..., mode=...)` / `get_last_scan(mode)` calls), make
    # this leak structurally impossible rather than papering over one
    # reproduction of it. This is a new pair of plain JSON keys in this
    # flat settings file (no SQL table, no migrations.py version bump,
    # no schema in the migration-framework sense) -- an old file simply
    # lacks these keys and _read() fills in None defaults, exactly like
    # every other field addition this store has ever had.
    "watchlist",
    "last_scan_at_demo",
    "last_scan_count_demo",
    "last_scan_at_live",
    "last_scan_count_live",
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
        "watchlist": None,
        "last_scan_at_demo": None,
        "last_scan_count_demo": None,
        "last_scan_at_live": None,
        "last_scan_count_live": None,
    }


MAX_WATCHLIST_SIZE = int(os.environ.get("TIP_MAX_WATCHLIST_SIZE", "20"))


def hash_key(api_key: Optional[str]) -> Optional[str]:
    """sha256 of a UW key for the verification record -- never store or
    return the raw key itself. Returns None for a falsy key so "no key"
    and "a hash of an empty string" are never confused."""
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


_log = logging.getLogger(__name__)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return                                      # not supported (e.g. Windows): best effort
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class SettingsUnavailableError(RuntimeError):
    """The settings file exists but could not be read (I/O error). Nothing is moved or overwritten."""


class SettingsStore:
    """Thread-safe (within one process) JSON file store. One instance is
    created in main.py; tests may point a fresh instance at a tmp_path file
    for isolation."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = Lock()
        self.recovery_notice: Optional[str] = None
        # P2-1: a quarantine notice outlives a restart until the operator removes the marker / the copies
        marker = self._notice_marker()
        if marker.exists() and self.preserved_copies():
            try:
                self.recovery_notice = marker.read_text(encoding="utf-8")[:500]
            except OSError:
                pass

    def _notice_marker(self) -> Path:
        return self.path.with_name(self.path.name + ".recovery-notice")

    def _quarantine(self, why: str) -> None:
        """A settings file that cannot be read is MOVED aside, never overwritten: the saved keys stay
        recoverable from the copy and the problem is surfaced (GET /api/settings -> settings_warning)."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{os.getpid()}")
        n = 1
        while aside.exists():                     # P2-1: never overwrite an earlier preserved copy
            aside = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{os.getpid()}-{n}")
            n += 1
        try:
            os.replace(self.path, aside)
            try:
                os.chmod(aside, 0o600)            # the copy can hold credentials
            except OSError:
                pass
            self.recovery_notice = (f"settings file was unreadable ({why}); it was preserved as {aside.name} "
                                    "and defaults are in use -- re-enter credentials or restore that file. "
                                    "The preserved copy may still contain API keys; delete it once recovered")
        except OSError as exc:
            self.recovery_notice = f"settings file is unreadable ({why}) and could not be moved aside: {exc}"
        try:
            self._notice_marker().write_text(self.recovery_notice, encoding="utf-8")
        except OSError:
            pass
        _log.error("settings: %s", self.recovery_notice)

    def _read(self) -> dict:
        if not self.path.exists():
            return _defaults()
        raw = None
        for attempt in range(3):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                break
            except FileNotFoundError:
                return _defaults()
            except (ValueError, UnicodeDecodeError) as exc:      # content is corrupt (JSONDecodeError too)
                self._quarantine(f"{type(exc).__name__}: {str(exc)[:120]}")
                return _defaults()
            except OSError as exc:
                # S2-6: an I/O error (antivirus or backup lock, EMFILE) says nothing about the content. Never
                # move a possibly valid file aside or fall back to defaults that a later write would persist.
                if attempt == 2:
                    raise SettingsUnavailableError(f"settings file temporarily unreadable: {type(exc).__name__}")
                time.sleep(0.1 * (attempt + 1))
        if not isinstance(raw, dict):
            self._quarantine(f"ValueError: top-level JSON is {type(raw).__name__}, not an object")
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
        # 0600 from creation (no window in which the keys are world-readable), fsync before the rename;
        # S2-7: a stale tmp is removed and the new one created exclusively, never followed if a symlink
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({k: data.get(k) for k in _FIELDS}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # best-effort on platforms/filesystems that don't support chmod
        _fsync_dir(self.path.parent)

    def preserved_copies(self) -> list:
        """Names of quarantined settings copies (they may still hold API keys -- S2-6)."""
        return sorted(p.name for p in self.path.parent.glob(f"{self.path.name}.corrupt-*"))

    def purge_preserved_copies(self) -> list:
        """S2-6: removing a credential also removes the app's own unreadable copies of the settings file,
        which may still contain it (they cannot be parsed, so the key cannot be cut out of them). Returns
        the names removed; the recovery notice ends with them."""
        removed = []
        for name in self.preserved_copies():
            try:
                (self.path.parent / name).unlink()
                removed.append(name)
            except OSError:
                pass
        if removed and not self.preserved_copies():
            self.recovery_notice = None
            try:
                self._notice_marker().unlink()
            except OSError:
                pass
        return removed

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

    def get_watchlist(self, default: list[str]) -> list[str]:
        """Returns the persisted watchlist, or `default` (the env-seeded
        TIP_LIVE_WATCHLIST) if the user has never saved one. `default` is
        passed in rather than read from os.environ here so this module
        stays free of any dependency on config.py (matches the rest of
        this file's existing style -- config/env reading lives in
        config.py, this file only persists what it's given)."""
        data = self.get_all()
        wl = data.get("watchlist")
        if wl is None:
            return list(default)
        return list(wl)

    def set_watchlist(self, tickers: list[str]) -> dict:
        """Validates (format + size cap), normalizes (upper, deduped,
        order-preserving), and persists a new watchlist. Raises
        ValueError on any violation -- the caller (main.py's route) is
        responsible for turning that into an HTTP 400, never silently
        truncating or dropping an invalid entry."""
        from .ai.validation import TICKER_RE  # local import avoids a circular import at module load

        if not isinstance(tickers, list):
            raise ValueError("watchlist must be a list of ticker strings")
        normalized: list[str] = []
        seen = set()
        for t in tickers:
            if not isinstance(t, str):
                raise ValueError(f"watchlist entries must be strings, got {type(t).__name__}")
            up = t.strip().upper()
            if not up:
                continue
            if not TICKER_RE.match(up):
                raise ValueError(f"'{up}' is not a valid ticker symbol")
            if up not in seen:
                normalized.append(up)
                seen.add(up)
        if len(normalized) > MAX_WATCHLIST_SIZE:
            raise ValueError(
                f"watchlist has {len(normalized)} symbols, which is over the {MAX_WATCHLIST_SIZE}-symbol "
                "limit -- each watchlist ticker costs a real UW API call per scan, so this cap exists to "
                "keep Scan Now's quota usage predictable, not arbitrarily"
            )
        with self._lock:
            data = self._read()
            data["watchlist"] = normalized
            self._write(data)
            return data

    def record_scan(self, ticker_count: int, mode: str = "live") -> dict:
        """Called once per real live_fetch in _radar_impl -- persists
        when it ran and how many tickers it covered, so a container
        restart doesn't lose "last scanned" context (the honest
        alternative to either showing nothing or silently claiming the
        data is fresher than it is).

        UX remediation item 2 (2026-10-16): `mode` ("demo" or "live")
        selects WHICH pair of fields gets written -- see this module's
        _FIELDS comment for the real Demo-mode-leaking-into-Live-mode
        bug this closes. The caller always passes the mode the scan
        that just ran was actually performed under (never guessed here).
        Returns the mode-scoped {"last_scan_at", "last_scan_count"} pair
        just written (same shape as get_last_scan below), not the whole
        settings dict, so callers can't accidentally read the other
        mode's fields by habit."""
        if mode not in ("demo", "live"):
            raise ValueError("mode must be 'demo' or 'live'")
        with self._lock:
            data = self._read()
            data[f"last_scan_at_{mode}"] = time.time()
            data[f"last_scan_count_{mode}"] = ticker_count
            self._write(data)
            return {"last_scan_at": data[f"last_scan_at_{mode}"], "last_scan_count": data[f"last_scan_count_{mode}"]}

    def get_last_scan(self, mode: str) -> dict:
        """Read-only counterpart to record_scan -- the mode-scoped
        {"last_scan_at", "last_scan_count"} pair for `mode`, never the
        other mode's values. Used by _radar_impl's cache-only read path
        (which is always Live Mode, by construction) so a Live Mode
        display can never show a timestamp/count that was actually
        recorded by a Demo Mode scan, or vice versa."""
        if mode not in ("demo", "live"):
            raise ValueError("mode must be 'demo' or 'live'")
        data = self.get_all()
        return {"last_scan_at": data.get(f"last_scan_at_{mode}"), "last_scan_count": data.get(f"last_scan_count_{mode}")}

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
