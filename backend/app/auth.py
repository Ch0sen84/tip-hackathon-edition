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


# --- QA remediation D1: secure pairing/provisioning fallback -----------
#
# ROOT CAUSE (real Windows Docker Desktop test, D1 in the QA report): on
# Docker Desktop, a browser request made FROM the Windows host arrives at
# this container through Docker Desktop's internal network bridge/VM, so
# `request.client.host` is the bridge gateway address (something like
# 172.x.x.1), never a real 127.0.0.1/::1 peer. is_loopback() correctly
# rejects that (it is NOT loopback, and -- this is the whole point --
# cannot be told apart from some OTHER device on the same Docker/LAN
# network just by looking at the IP). The bootstrap route above therefore
# 403s for every genuine Docker Desktop browser, forcing a manual
# `docker exec ... cat .auth_token`.
#
# REJECTED FIXES (all explicitly forbidden by Chad's brief, and why):
#   * "Trust the Docker bridge gateway IP" -- the gateway address is a
#     fixed, well-known, GUESSABLE value (typically x.x.x.1) on every
#     Docker Desktop install; any other container, or any device that can
#     reach this one over the LAN via port-forwarding, can spoof traffic
#     that LOOKS like it came from the gateway. It proves nothing about
#     who is actually driving a browser on the real host.
#   * "Trust any RFC1918 private range" -- this is just a wider version of
#     the same mistake: it would also trust a phone on the same office
#     WiFi, a different VM, or a compromised machine elsewhere on the LAN.
#   * "Trust X-Forwarded-For / X-Real-IP / Host: localhost" -- every one
#     of these is a plain HTTP header the CLIENT sends. A remote attacker
#     controls every header in their own request; "trusting" a header as
#     proof of origin is trusting the attacker's own unverified claim.
#
# THE ACTUAL FIX: a short-lived, single-use PAIRING CODE, generated at
# process startup and surfaced ONLY through a channel an attacker on the
# LAN/Docker network cannot read: `docker logs <container>` (this
# process's own stdout, loudly, at startup -- see main.py's startup
# handler) and the persisted sibling file inside the SAME host-mounted
# volume the auth token itself already lives in (readable via
# `docker exec`, same as the token was before this fix, but no longer
# the ONLY way to get a working session). Reading either of those
# requires Docker CLI/daemon access on the real host -- not "being on
# the same network", not "guessing a gateway IP", not "sending a header".
# This is exactly option (a) from Chad's brief: a one-time code written
# host-side that a legitimate user copy/pastes into the browser once, to
# mint a token with NO network-origin trust decision involved at all.
#
# The existing loopback bootstrap route above is UNCHANGED and remains
# the fully-automatic path for every topology where it genuinely works
# (plain `uvicorn` locally, Docker on Linux without Desktop's extra
# bridging, etc.) -- this pairing mechanism is purely the fallback for
# when it doesn't, never a replacement that weakens the loopback case.
PAIRING_CODE_FILENAME = ".pairing_code"
PAIRING_CODE_TTL_S = 15 * 60  # rotates automatically after 15 minutes
PAIRING_CODE_MAX_FAILED_ATTEMPTS = 20  # defense-in-depth on top of 64 bits of entropy


class PairingCodeStore:
    """Generates/persists/rotates the one-time Docker-host pairing code.

    Deliberately modeled on AuthTokenStore above (same tmp_path-isolated
    construction for tests, same 0600 file permissions) but is its own
    separate secret/file -- consuming a pairing code never reveals or
    rotates the real long-lived auth token's file, and vice versa.
    """

    def __init__(self, code_path: "str | os.PathLike", ttl_s: float = PAIRING_CODE_TTL_S):
        import threading

        self.code_path = Path(code_path)
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._code: Optional[str] = None
        self._expires_at: float = 0.0
        self._failed_attempts = 0

    def _write(self, code: str) -> None:
        self.code_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.code_path.with_suffix(self.code_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, self.code_path)
        try:
            os.chmod(self.code_path, 0o600)
        except OSError:
            pass

    def _generate_locked(self) -> str:
        import time

        # 16 hex chars = 64 bits of entropy -- short enough to type/paste
        # from a terminal, far too large to brute-force online even
        # without the attempt cap below.
        code = secrets.token_hex(8)
        self._code = code
        self._expires_at = time.monotonic() + self.ttl_s
        self._failed_attempts = 0
        self._write(code)
        return code

    def current_code(self, force_new: bool = False) -> str:
        """Returns the live pairing code, generating/rotating it first if
        none exists yet or the previous one has expired. `force_new=True`
        is used at process startup so every restart gets a fresh code
        (never reuses a code that may have already been shown/used in a
        previous process lifetime)."""
        import time

        with self._lock:
            if force_new or self._code is None or time.monotonic() > self._expires_at:
                return self._generate_locked()
            return self._code

    def consume(self, candidate: Optional[str]) -> bool:
        """One-time-use check: on a correct, still-valid code, rotates to
        a brand-new code (so the SAME code can never be replayed/reused)
        and returns True. Every other case (wrong code, expired, too
        many recent failed attempts -- a brute-force defense independent
        of the already-large keyspace) returns False and never mutates
        the live code, so a wrong guess doesn't invalidate a code the
        real user hasn't used yet."""
        import time

        with self._lock:
            if self._failed_attempts >= PAIRING_CODE_MAX_FAILED_ATTEMPTS:
                return False
            if (
                not candidate
                or self._code is None
                or time.monotonic() > self._expires_at
            ):
                self._failed_attempts += 1
                return False
            if hmac.compare_digest(self._code, candidate):
                self._generate_locked()  # one-time use: rotate immediately on success
                return True
            self._failed_attempts += 1
            return False

    def reset_cache(self) -> None:
        """Test-only: forces the next current_code() call to generate a
        brand-new code rather than reuse one left from a previous test."""
        self._code = None
        self._failed_attempts = 0


def default_pairing_code_path(settings_path: "str | os.PathLike") -> str:
    """Same sibling-of-settings.json placement as default_token_path --
    SAME persistent volume, different filename, overridable via
    TIP_PAIRING_CODE_PATH."""
    return str(Path(settings_path).resolve().parent / PAIRING_CODE_FILENAME)
