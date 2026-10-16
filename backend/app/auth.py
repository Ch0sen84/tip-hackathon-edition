"""Phase A: real browser-usable authentication for every state-changing /
paid route.

Design (see README.md "Security / Authentication" section for the full
user-facing writeup):

  * A single random token (secrets.token_hex(32)) is generated on first
    run and persisted to a file inside the SAME persistent volume the app
    already uses for settings/db (TIP_SETTINGS_PATH's directory --
    /srv/data in the real Docker deployment, via docker-compose's existing
    `tip-data` named volume). File mode 0600.
  * GET /api/auth/bootstrap serves that token ONLY to a request whose
    peer IP is loopback (127.0.0.1 / ::1 / localhost) -- see is_loopback()
    below. This is what lets a genuine browser on the SAME machine as the
    Docker host self-serve the token with no shell access at all. Anyone
    else (LAN/remote) is told, in the 403 body AND in README/
    INSTALL_AGENT.md, to retrieve it via
    `docker exec <container> cat /srv/data/.auth_token` instead.
  * Every mutating route (POST/PUT/DELETE under /api/, except the
    bootstrap route itself) requires a matching `X-TIP-Auth` header,
    compared with hmac.compare_digest (constant-time) -- see
    require_auth() in main.py, which wires this up as a FastAPI
    dependency.

This module holds ONLY the token storage + comparison primitives, kept
deliberately free of FastAPI/Request details (other than the one small,
directly-unit-testable is_loopback() helper) so it is easy to test in
isolation and easy to re-point at an isolated tmp_path during tests --
never the real deployed token file.
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Optional

TOKEN_FILENAME = ".auth_token"
TOKEN_HEADER = "X-TIP-Auth"

# Real request.client.host values for traffic that genuinely originated on
# this machine. IPv4-mapped-IPv6 loopback ("::ffff:127.0.0.1") is included
# defensively; uvicorn's ASGI scope normally reports a plain form, but this
# never widens the check beyond "this same host".
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def is_loopback(host: Optional[str]) -> bool:
    """Pure, directly-unit-testable check -- never touches a request or
    the network. Deliberately a plain string-membership check (not a
    socket/DNS lookup) so it can never block or behave differently under
    test vs. production."""
    return bool(host) and host in LOOPBACK_HOSTS


class AuthTokenStore:
    """Generates/persists/compares the single shared auth token.

    One instance lives on `app.main` for the life of the process (see
    main.py's `_auth_store`); tests construct their own instance pointed
    at an isolated tmp_path file, exactly like SettingsStore/
    QualifyingDaysStore already do elsewhere in this codebase -- never the
    real default path during a test run.
    """

    def __init__(self, token_path: "str | os.PathLike"):
        self.token_path = Path(token_path)
        self._cached: Optional[str] = None

    def get_or_create(self) -> str:
        if self._cached:
            return self._cached
        if self.token_path.exists():
            try:
                existing = self.token_path.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
            if existing:
                self._cached = existing
                return existing
        token = secrets.token_hex(32)
        self._write(token)
        self._cached = token
        return token

    def _write(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.token_path.with_suffix(self.token_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(token)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass  # best-effort on platforms/filesystems that don't support chmod
        os.replace(tmp_path, self.token_path)
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def reset_cache(self) -> None:
        """Forces the next get_or_create() to re-read (or regenerate) from
        disk -- used by tests that point this store at a fresh tmp_path
        per test."""
        self._cached = None

    def matches(self, candidate: Optional[str]) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(self.get_or_create(), candidate)


def default_token_path(settings_path: "str | os.PathLike") -> str:
    """The token lives as a sibling of settings.json -- same directory,
    hence the SAME persistent volume (TIP_SETTINGS_PATH is explicitly
    pointed at /srv/data/settings.json in docker-compose.yml, inside the
    existing `tip-data` named volume). Overridable via TIP_AUTH_TOKEN_PATH
    for anyone who wants it elsewhere."""
    return str(Path(settings_path).resolve().parent / TOKEN_FILENAME)
