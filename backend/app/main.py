"""FastAPI app wiring Market Radar / Ticker Analysis routes to the ported
core logic via whichever adapter TIP_MODE selects.

TIP_MODE=demo (default) -> SyntheticDataAdapter over fixtures/*.json
TIP_MODE=live            -> LiveUWAdapter over the real UW API (needs
                             UW_API_KEY; see .env.example)

Routes are intentionally thin: they fetch raw data from the adapter, run it
through backend/app/core/scan_service.py (which is where the real gate /
confidence / state-transition decision happens), persist the scan result to
the local SQLite store for qualifying-days counting, and format it with
backend/app/core/tip_evidence.py for display. No business logic lives here.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import date, timedelta
from typing import Optional

from pathlib import Path

# REAL BUG FIX (2026-10-14): discovered while diagnosing the NVDA/PLTR
# synthesis-truncation report -- app.ai.orchestrator's and
# app.ai.llm_provider's logger.info(...) diagnostic lines (tool-selection
# round completed, tool execution N/M succeeded, etc.) were being
# silently swallowed. Nothing in this app ever called logging.basicConfig,
# so those loggers had no handler and fell back to Python's "last resort"
# handler, which only shows WARNING and above -- the exact INFO-level
# lines needed to diagnose which round/stage failed were invisible in
# `docker logs`. uvicorn's own request-logging keeps working either way;
# this just makes our OWN app loggers actually emit INFO to the same
# stream uvicorn already writes to stdout.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adapters.base import UWDataAdapter
from .adapters.live_uw_adapter import DISCOVERY_LIMIT_CAP, ERROR, UNAUTHORIZED, UNAVAILABLE, LiveUWAdapter, UWRestClient
from .adapters.synthetic_adapter import SyntheticDataAdapter
from .ai import orchestrator as ai_orchestrator
from .ai import llm_provider as ai_llm_provider
from .ai import mcp_tool_client as ai_mcp_tool_client
from .ai.llm_provider import LLMCallError, LLMTruncatedOutputError
from .ai.mcp_tool_client import MCPSubprocessError
from .ai.schemas import MalformedAIResponse
from .ai.simulated import get_simulated_transcript
from .ai.validation import ValidationError as AiValidationError
from .auth import (
    TOKEN_HEADER,
    AuthTokenStore,
    PairingCodeStore,
    default_pairing_code_path,
    default_token_path,
    is_loopback,
)
from .config import settings
from .core import dark_pool_baseline, discovery, scan_service, tip_evidence
from .core import local_mode as local_mode_module
from .core import scheduler_freshness, single_process_guard, uw_concurrency
from .core.scheduler_store import InvalidModeError, MODES as SCHEDULER_MODES, SchedulerStore, new_worker_id
from . import scheduler as scheduler_module
from .core.candles import build_markers
from .ingestion import IngestionCatalog, StorageGuardPausedError as IngestionStorageGuardPausedError
from .settings_store import MAX_WATCHLIST_SIZE, SettingsStore, last4
from .storage import QualifyingDaysStore, StorageGuardPausedError
from . import update_check as _update_check
from . import version as _version

app = FastAPI(title="TIP Hackathon Edition", version="0.1.0")

# --- Phase A: CORS / Host hardening -------------------------------------
#
# This is a same-origin SPA (see the StaticFiles mount at the bottom of
# this file) -- the frontend is served BY this same backend, not from a
# separate frontend origin, so there is no legitimate cross-origin caller
# that ever needs allow_origins=["*"]. The allowed origin/host is derived
# from BIND_HOST/BIND_PORT (the exact env vars docker-compose.yml already
# uses to publish this app -- see .env.example), or an explicit
# ALLOWED_ORIGIN/ALLOWED_HOST override for anyone fronting this with a
# reverse proxy under a different hostname.
_BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
_BIND_PORT = os.environ.get("BIND_PORT", "8000")
_ALLOWED_HOST_ENV_RAW = os.environ.get("ALLOWED_HOST")  # None iff never set explicitly
ALLOWED_HOST = os.environ.get("ALLOWED_HOST", _BIND_HOST)

# QA remediation D2: localhost:8000 returned 400 while 127.0.0.1:8000
# worked, because the default ALLOWED_HOST list only ever contained
# whichever single literal value BIND_HOST/ALLOWED_HOST resolved to
# (127.0.0.1 by default) -- "localhost" was never in it, so
# TrustedHostMiddleware rejected that Host header outright. Since D1's
# new pairing mechanism and the existing loopback bootstrap both treat
# 127.0.0.1/::1/localhost as equally "this machine" already (see
# auth.LOOPBACK_HOSTS), the consistent fix is to accept BOTH spellings
# of loopback by default too -- not to pick one as "the" canonical
# address and document the other as unsupported. An operator who has
# explicitly set ALLOWED_HOST to something else (a reverse-proxy
# hostname, a specific LAN IP) keeps getting EXACTLY that one value and
# nothing else added -- this widening only ever applies to the
# loopback-default case, never silently broadens a custom deployment.
_LOOPBACK_HOST_ALIASES = {"127.0.0.1", "localhost"}
if ALLOWED_HOST in _LOOPBACK_HOST_ALIASES:
    _ALLOWED_HOSTS = sorted(_LOOPBACK_HOST_ALIASES)
else:
    _ALLOWED_HOSTS = [ALLOWED_HOST]
# Optional explicit override/extension for anyone who needs a specific
# extra host accepted (comma-separated) without relying on the
# loopback-alias widening above.
_extra_hosts = os.environ.get("ALLOWED_HOSTS_EXTRA", "").strip()
if _extra_hosts:
    _ALLOWED_HOSTS += [h.strip() for h in _extra_hosts.split(",") if h.strip()]
# Test-only widening: fastapi.testclient.TestClient has no real network
# interface, so its requests' Host header is whatever base_url the test
# gave it. This can ONLY ever add hosts during an actual pytest run (same
# guard pattern as app/storage.py's _running_under_pytest) -- it is a
# no-op for the real deployed app (uvicorn/Docker).
if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
    _ALLOWED_HOSTS += ["testserver", "testclient"]

# QA remediation B1/B3 (Windows Docker Desktop QA round 2): Origin
# validation used to be a single hardcoded ALLOWED_ORIGIN string computed
# once from BIND_HOST/BIND_PORT. Two concrete bugs followed from that:
#   B1 -- BIND_PORT was never actually forwarded into the container's
#         environment by docker-compose.yml (fixed there now), so a
#         custom published port's real browser Origin never matched
#         whatever port this process happened to compute.
#   B3 -- even with the right port, a browser opening the app via
#         `http://localhost:<port>` sent Origin `http://localhost:<port>`,
#         which never matched the single `http://127.0.0.1:<port>` this
#         process computed -- even though TrustedHostMiddleware above
#         already accepts BOTH `localhost` and `127.0.0.1` as the Host
#         header. Origin validation must accept the SAME set of loopback
#         aliases Host validation already does, for the SAME configured
#         port -- never a wildcard, never a weakened Host check.
# ALLOWED_ORIGIN is kept as a single representative value (existing
# tests/docstrings/error messages reference it), but the real membership
# check and the real CORSMiddleware config use the full computed set.
_ALLOWED_ORIGIN_ENV = os.environ.get("ALLOWED_ORIGIN")
_ALLOWED_ORIGINS = local_mode_module.compute_allowed_origins(
    ALLOWED_HOST, _BIND_PORT, allowed_origin_override=_ALLOWED_ORIGIN_ENV
)
ALLOWED_ORIGIN = _ALLOWED_ORIGINS[0]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_ALLOWED_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Local Mode / Network Mode (Fetch Metadata auth redesign) ----------
#
# Local Mode is the DEFAULT loopback-only install's zero-token same-
# origin bypass (Sec-Fetch-Site + exact Origin validation, see
# require_auth() below); Network Mode is the pre-existing, completely
# unmodified X-TIP-Auth bootstrap/pairing-code token model. BIND_HOST is
# read here as the operator's STATED intent only, never independently
# verified -- see core/local_mode.py's module docstring. TIP_LOCAL_MODE
# is an optional explicit override (unset/"auto" is the zero-config
# default: Local Mode is active iff BIND_HOST is loopback, Network Mode
# otherwise -- exactly today's documented safe-by-default behavior).
_TIP_LOCAL_MODE_RAW = os.environ.get("TIP_LOCAL_MODE")
LOCAL_MODE_ACTIVE = local_mode_module.effective_local_mode(_BIND_HOST, _TIP_LOCAL_MODE_RAW)

_adapter: Optional[UWDataAdapter] = None
_adapter_sig: Optional[tuple] = None
# Reliability round (2026-10-16), item 5: survives _reset_adapter_cache()
# calls (unlike _adapter/_adapter_sig above, which every settings save
# unconditionally nulls) so get_adapter() can tell "mode/key genuinely
# changed since the last live client was built" apart from "something
# else (e.g. the watchlist) changed, forcing a new _adapter object but
# not requiring a new underlying UW HTTP client/telemetry instance."
_last_live_client = None
_last_live_client_key_sig: Optional[tuple] = None
_store: Optional[QualifyingDaysStore] = None
_catalog: Optional[IngestionCatalog] = None
_settings_store = SettingsStore(os.environ.get("TIP_SETTINGS_PATH", "./data/settings.json"))
_last_uw_test_result: Optional[str] = None  # in-memory only, informational; never persisted

# --- Phase A: real browser-usable auth for every mutating route --------
#
# See auth.py's module docstring for the full design. One AuthTokenStore
# lives on this module for the process lifetime; tests replace it with
# their own instance pointed at an isolated tmp_path (see conftest.py's
# `app_main` fixture), exactly like _settings_store above.
_auth_store = AuthTokenStore(
    os.environ.get(
        "TIP_AUTH_TOKEN_PATH",
        default_token_path(os.environ.get("TIP_SETTINGS_PATH", "./data/settings.json")),
    )
)

# QA remediation D1: the Docker-Desktop-safe pairing fallback -- see
# auth.PairingCodeStore's docstring for the full security design. One
# instance lives on this module for the process lifetime, exactly like
# _auth_store above; tests replace it with their own tmp_path-isolated
# instance (see conftest.py's `app_main` fixture).
_pairing_store = PairingCodeStore(
    os.environ.get(
        "TIP_PAIRING_CODE_PATH",
        default_pairing_code_path(os.environ.get("TIP_SETTINGS_PATH", "./data/settings.json")),
    )
)


def _origin_header_matches_configured_origin(origin: Optional[str]) -> bool:
    """B3 fix: membership in the full computed _ALLOWED_ORIGINS set (every
    loopback alias on the configured port, or the single explicit
    ALLOWED_ORIGIN override) -- never a single hardcoded string. Still an
    exact match against a known, bounded set; never a wildcard."""
    return origin in _ALLOWED_ORIGINS


def _sec_fetch_site_is_same_origin(request: Request) -> bool:
    """Refinement #1: ONLY the literal value "same-origin" ever counts.
    "cross-site", "same-site", and "none" (direct navigation/typed-URL/
    bookmark/missing header entirely) are all explicitly NOT sufficient
    for the Local Mode bypass -- there is no broadened/partial match
    here. A missing Sec-Fetch-Site header (an unsupported/very old
    browser) falls into this same "not same-origin" bucket by
    construction (.get() returns None, which != "same-origin") -- the
    fail-closed "unsupported browser" case from refinement #7(e)."""
    return request.headers.get("sec-fetch-site") == "same-origin"


def _local_mode_bypass_allowed(request: Request) -> bool:
    """Refinements #1/#2/#3: the Local Mode zero-token bypass requires
    ALL of:
      * LOCAL_MODE_ACTIVE (BIND_HOST says loopback, or an explicit
        TIP_LOCAL_MODE=1 confirms it -- see core/local_mode.py; this is
        read as the operator's stated intent, not re-verified here).
      * a genuine Sec-Fetch-Site: same-origin header -- cross-site,
        same-site, none, and missing-entirely are all refused (see
        _sec_fetch_site_is_same_origin above). No narrow exception is
        implemented for any MUTATING route in this app: every route
        gated by require_auth (this function) is a state-changing or
        paid action, and the one case that genuinely can't avoid a
        "none"/missing Sec-Fetch-Site -- a plain non-mutating page-load
        GET -- was already unauthenticated/ungated before this change
        (e.g. GET /api/radar, GET /api/ticker/{t}, GET /api/auth/mode)
        and stays that way; it never needed, and never gets, a
        broadened Sec-Fetch-Site rule.
      * IF an Origin header is present, it must exactly match
        ALLOWED_ORIGIN -- a present-but-mismatched Origin is already
        rejected outright by require_auth before this function is even
        called (see below), so by the time we get here a present
        Origin is already known to match. A genuinely MISSING Origin
        does not, by itself, grant the bypass -- it is accepted here
        only because a real Sec-Fetch-Site: same-origin signal is
        already independently established above (refinement #2: a
        missing Origin is never treated as evidence of legitimacy on
        its own; it never substitutes for proof, it is simply not
        independently disqualifying once Sec-Fetch-Site already is).
    """
    if not LOCAL_MODE_ACTIVE:
        return False
    return _sec_fetch_site_is_same_origin(request)


def require_auth(request: Request) -> None:
    """FastAPI dependency applied to every mutating (POST/PUT/DELETE) /
    sensitive-read route under /api/ except /api/auth/bootstrap and
    /api/auth/pair themselves (their own, separate gates -- see those
    routes' docstrings).

    Two-stage check, in order:
      1. Origin validation (refinement #2) -- applies identically in
         BOTH Local Mode and Network Mode, unconditionally, BEFORE any
         mode-specific branching: whenever an Origin header is present
         on the request, it must exactly equal this deployment's
         configured ALLOWED_ORIGIN or the request is rejected outright
         (403), regardless of what Sec-Fetch-Site says and regardless
         of whether a valid X-TIP-Auth token is also present. A missing
         Origin header is NOT evidence of anything by itself -- it just
         means this check has nothing to validate, and falls through to
         step 2.
      2. Local Mode bypass (refinements #1/#3) -- see
         _local_mode_bypass_allowed above. When it does not apply
         (Network Mode, or Local Mode without a genuine same-origin
         Sec-Fetch-Site), this is EXACTLY the original, completely
         unmodified X-TIP-Auth token check (refinement #4: Network
         Mode's protections are provably unweakened -- see
         test_network_mode_unweakened.py) -- fail CLOSED to the token
         requirement, never fail-open.
    """
    origin = request.headers.get("origin")
    if origin is not None and not _origin_header_matches_configured_origin(origin):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Origin header {origin!r} does not match this deployment's configured "
                f"origin(s) ({sorted(_ALLOWED_ORIGINS)!r}). A missing Origin header is never "
                "treated as proof of legitimacy on its own, but a PRESENT, mismatched one is "
                "always rejected outright, in both Local Mode and Network Mode."
            ),
        )
    if _local_mode_bypass_allowed(request):
        return
    candidate = request.headers.get(TOKEN_HEADER)
    if not _auth_store.matches(candidate):
        raise HTTPException(
            status_code=401,
            detail=(
                f"Missing or invalid {TOKEN_HEADER} header. Use GET /api/auth/bootstrap "
                "from the same machine (localhost), or see README/INSTALL_AGENT.md for "
                "LAN/remote setup instructions."
            ),
        )


def _effective_uw_key(store_data: dict) -> tuple[Optional[str], str]:
    """A UI-saved key always wins over the .env/environment fallback value
    (see config.py docstring + README). Returns (key_or_None, source)."""
    ui_key = store_data.get("uw_api_key")
    if ui_key:
        return ui_key, "ui"
    if settings.uw_api_key:
        return settings.uw_api_key, "env"
    return None, "none"


def _reset_adapter_cache() -> None:
    global _adapter, _adapter_sig
    _adapter = None
    _adapter_sig = None
    # Reliability round (2026-10-16), item 5: deliberately NOT clearing
    # _last_live_client/_last_live_client_sig here. Every caller of this
    # function (set_watchlist, set_mode, key save, etc.) wants the next
    # get_adapter() to pick up fresh SETTINGS, not necessarily a fresh
    # underlying UW HTTP client -- see get_adapter()'s reuse logic below,
    # which is exactly why this separate pair exists instead of being
    # folded into _adapter/_adapter_sig (those two are unconditionally
    # nulled by this function on every settings save, including a
    # watchlist-only one, so they can never be the thing a later
    # get_adapter() call reads the "was it really mode/key that changed"
    # decision from).


def get_adapter() -> UWDataAdapter:
    global _adapter, _adapter_sig, _last_live_client, _last_live_client_key_sig
    data = _settings_store.get_all()
    mode = data["mode"]
    key, _source = _effective_uw_key(data)
    # Market Radar (2026-10-14, Phase A): the watchlist is now user-
    # editable and persisted (SettingsStore.get_watchlist(), falling back
    # to TIP_LIVE_WATCHLIST only as the seed default) -- included in the
    # adapter cache signature so saving a new watchlist actually takes
    # effect on the NEXT /api/radar call instead of silently continuing
    # to scan the old one until some unrelated cache-busting event.
    watchlist = tuple(_settings_store.get_watchlist(default=settings.live_watchlist))
    sig = (mode, key, watchlist)
    if _adapter is None or _adapter_sig != sig:
        if mode != "live":
            _adapter = SyntheticDataAdapter(settings.fixtures_dir)
        else:
            # Reliability round (2026-10-16), item 5: a watchlist-only
            # settings save (add/remove one ticker) changes `sig` (the
            # watchlist is deliberately part of it, see the Phase A
            # comment above) and therefore always rebuilds `_adapter` --
            # but rebuilding the ADAPTER must not mean rebuilding the
            # underlying UW HTTP CLIENT, because UWRestClient owns the
            # real diagnostics/telemetry counters (/api/diagnostics/
            # uw-requests). Before this fix, every watchlist edit (or
            # any other sig change that doesn't actually touch mode/key)
            # silently handed out a brand-new client with zeroed
            # telemetry, making the counters look like they "reset" for
            # a reason that had nothing to do with real UW request
            # volume. Root cause confirmed by reading LiveUWAdapter.
            # __init__ (adapters/live_uw_adapter.py): it already accepts
            # an optional `client=` override specifically for this kind
            # Only construct a genuinely NEW client when mode or the
            # effective key actually changed since the LAST LIVE CLIENT
            # was built; reuse that client (and its accumulated
            # telemetry) whenever only the watchlist changed in between.
            # Deliberately read from _last_live_client_key_sig, NOT
            # _adapter_sig/_adapter -- _reset_adapter_cache() (called by
            # every settings save, including a watchlist-only one) always
            # nulls those two, so they can never answer "did mode/key
            # really change" across a reset. Telemetry itself only ever
            # observes real HTTP attempts already made through this same
            # client -- reusing the object changes nothing about what
            # requests are made, only whether the counters survive.
            key_sig = (mode, key)
            reuse_client = (
                _last_live_client
                if _last_live_client is not None and _last_live_client_key_sig == key_sig
                else None
            )
            _adapter = LiveUWAdapter(
                api_key=key or "",
                base_url=settings.uw_base_url,
                client_api_id=settings.uw_client_api_id,
                watchlist=list(watchlist),
                client=reuse_client,
            )
            _last_live_client = _adapter.client
            _last_live_client_key_sig = key_sig
        _adapter_sig = sig
    return _adapter


def get_store() -> QualifyingDaysStore:
    global _store
    if _store is None:
        _store = QualifyingDaysStore(settings.db_path)
        # Backfill the one demo scenario (PRSX) that needs genuinely
        # distinct historical scan_date rows to show a real multi-day
        # BUILDING -> CONFIRMED trajectory -- see seed_demo_history.py's
        # module docstring. Idempotent, non-fatal (demo-only convenience;
        # never block app startup on it), and runs on real fixture data
        # through the real scan_service, not a hardcoded state.
        try:
            from .seed_demo_history import seed_persistence_progression
            seed_persistence_progression(_store, settings.fixtures_dir)
        except Exception:
            pass
    return _store


def get_catalog() -> IngestionCatalog:
    """Phase B: the shared historical-catalog writer -- SAME db file as
    get_store()'s QualifyingDaysStore (settings.db_path), so the new
    catalog tables live in the one existing `tip-data` Docker volume
    file, never a second database. Lazily constructed exactly like
    _store/_adapter above; tests reset this via conftest's app_main
    fixture the same way."""
    global _catalog
    if _catalog is None:
        _catalog = IngestionCatalog(settings.db_path)
    return _catalog


def _scheduler_run_ticker_scan(adapter: UWDataAdapter, ticker: str) -> dict:
    """Phase D: the ONE function the background scheduler uses for its
    bounded per-ticker evaluation step (mode="enabled" only) -- a thin
    wrapper around the EXACT SAME `_run_scan` the interactive Market
    Radar route already calls, just with kind="background" so storage
    writes check the earlier/more-conservative background threshold
    (see storage_guard.py) instead of the on-demand one. Intentionally
    NOT a new evaluation pipeline -- see app/scheduler.py's module
    docstring and Chad's item 6."""
    return _run_scan(adapter, get_store(), ticker, kind="background")


_scheduler_runner: Optional[scheduler_module.SchedulerRunner] = None


def get_scheduler_runner() -> scheduler_module.SchedulerRunner:
    """Lazily constructed exactly like _store/_adapter/_catalog above;
    tests reset this via conftest's app_main fixture the same way."""
    global _scheduler_runner
    if _scheduler_runner is None:
        _scheduler_runner = scheduler_module.SchedulerRunner(
            db_path=settings.db_path,
            get_adapter=get_adapter,
            get_catalog=get_catalog,
            run_ticker_scan=_scheduler_run_ticker_scan,
        )
    return _scheduler_runner


def _reset_scheduler_runner() -> None:
    global _scheduler_runner
    _scheduler_runner = None


@app.on_event("startup")
def _on_startup() -> None:
    """Phase D: refuse to start under a multi-worker launcher (see
    core/single_process_guard.py -- the UW concurrency gate is
    process-local and not safe across multiple worker processes), then
    start the background scheduler thread. The scheduler thread itself
    is a no-op loop whenever scheduler_state.mode == 'disabled' (the
    hardcoded fallback for any fresh install/upgrade with no prior
    explicit setting) -- starting the thread is NOT the same as making
    background UW calls."""
    # Local Mode / Network Mode refinement #3: refuse to start (fail
    # loud, never silently downgrade) if TIP_LOCAL_MODE explicitly
    # requests Local Mode against a non-loopback BIND_HOST -- see
    # core/local_mode.py's docstring. Mirrors single_process_guard's
    # own fail-closed startup pattern immediately below.
    local_mode_module.assert_local_mode_configuration(
        _BIND_HOST, _TIP_LOCAL_MODE_RAW, allowed_host_override=_ALLOWED_HOST_ENV_RAW
    )
    single_process_guard.assert_single_process()
    get_scheduler_runner().start()
    # QA remediation D1: loudly surface the pairing code on every real
    # startup (force_new=True -- never reuse a code a previous process
    # lifetime may have already shown/used). This is the ONLY place this
    # code is ever printed -- `docker logs <container>` is the host-only
    # channel a legitimate operator reads it from. See auth.py's
    # PairingCodeStore docstring for why this is safe against a LAN/
    # Docker-network attacker (reading container logs requires Docker
    # CLI/daemon access on the real host, not mere network reachability).
    #
    # Reliability round (2026-10-16), item 5: the pairing code only has a
    # real job to do when Network Mode's token/pairing flow is actually
    # the active auth model -- Local Mode (loopback BIND_HOST, the
    # default/common case) never shows any pairing UI at all (see
    # app.live.js's showAuthSetupBanner "ZERO authentication UI" guard)
    # and never calls /api/auth/pair, so a code generated and warn-
    # logged every single startup is pure log noise there: nothing in
    # Local Mode will ever read or need it. Gate the WARNING-level
    # banner behind Network Mode actually being active; still generate
    # the code either way (force_new=True must still run every startup
    # so a stale code from a prior process lifetime is never silently
    # left live) and still log it, just at DEBUG, for the rare case an
    # operator is debugging Local Mode's own gate.
    code = _pairing_store.current_code(force_new=True)
    network_mode_active = not local_mode_module.effective_local_mode(
        _BIND_HOST, _TIP_LOCAL_MODE_RAW
    )
    log = logging.getLogger(__name__)
    log_fn = log.warning if network_mode_active else log.debug
    log_fn(
        "\n%s\nTIP Hackathon Edition -- Docker Desktop / browser pairing code: %s\n"
        "If the app's auto-bootstrap (GET /api/auth/bootstrap) fails in your browser "
        "(expected on Docker Desktop for Windows/Mac), paste this code into the "
        "'Pair this browser' prompt shown in the UI. One-time use; expires in 15 minutes "
        "-- restart the container (or wait) for a fresh one if it expires.\n%s",
        "=" * 70, code, "=" * 70,
    )


@app.on_event("shutdown")
def _on_shutdown() -> None:
    """Graceful shutdown: releases the scheduler's lease immediately
    (see scheduler_store.SchedulerStore.release_lease) rather than
    leaving the next process to wait out the full lease TTL."""
    if _scheduler_runner is not None:
        _scheduler_runner.stop()


def _price_move_pct(stock_state: Optional[dict]) -> Optional[float]:
    if not stock_state or stock_state.get("unavailable"):
        return None
    for key in ("change_percent", "chg_pct", "percent_change"):
        if stock_state.get(key) is not None:
            return float(stock_state[key])
    return None


def _catalog_scan_best_effort(adapter: UWDataAdapter, ticker: str, flow_alerts: list,
                               darkpool_trades: list, result: dict, kind: str = "on_demand") -> None:
    """Phase B: persists this scan's raw inputs + evaluation into the
    historical catalog tables -- PURELY an added persistence layer
    underneath the existing live-fetch logic above. This function's
    success/failure can NEVER change what _run_scan returns to its
    caller (every exception is swallowed here, not propagated) --
    see app/ingestion.py's module docstring for why that invariant is
    load-bearing for this phase's "observable response contract must
    not change" requirement. Only ever called for a LiveUWAdapter scan;
    the synthetic/demo adapter's fixture data is deliberately never
    cataloged (it isn't real upstream data, and Phase A's seed_demo_
    history already simulates multi-day history for it).

    Phase D: `kind` is threaded through to every catalog_* write so the
    scheduler's calls (kind="background") check storage_guard's earlier,
    more conservative BACKGROUND threshold instead of always being
    treated as an on-demand write regardless of who's actually writing.
    Default stays "on_demand" -- every pre-existing caller is unaffected."""
    if not isinstance(adapter, LiveUWAdapter):
        return
    try:
        catalog = get_catalog()
        n_flow = catalog.catalog_options_flow_observations(ticker, flow_alerts, kind=kind)
        n_dp = catalog.catalog_darkpool_observations(ticker, darkpool_trades, kind=kind)
        catalog.catalog_signal_evaluation(ticker, result, kind=kind)
    except Exception:  # noqa: BLE001 -- see docstring: never propagate (incl. IngestionStorageGuardPausedError)
        logging.getLogger(__name__).info(
            "Phase B ingestion catalog write skipped for %s (non-fatal, response unaffected)", ticker
        )


def _catalog_dark_pool_baseline_best_effort(adapter: UWDataAdapter, ticker: str) -> Optional[dict]:
    """Phase C: computes a real historical dark-pool baseline PURELY from
    whatever Phase B has already cataloged for `ticker` (never a live UW
    call, never a backfill) and shapes it into the exact keyword-argument
    form signal_engine.evaluate_gates already expects for its
    dark_pool_anomaly gate -- see scan_service.build_gate_inputs'
    `catalog_dark_pool_baseline` parameter and
    dark_pool_baseline.BaselineResult.as_gate_inputs().

    Only meaningful for a LiveUWAdapter scan (the synthetic/demo adapter's
    data is never cataloged, same precondition `_catalog_scan_best_effort`
    already uses). Returns None -- never raises -- on ANY failure
    (including an empty/nonexistent catalog for a ticker never scanned
    before): this must never be allowed to block or alter the scan it
    feeds into, exactly like `_catalog_scan_best_effort` below."""
    if not isinstance(adapter, LiveUWAdapter):
        return None
    try:
        catalog = get_catalog()
        result = dark_pool_baseline.compute_baseline_for_ticker(catalog, ticker)
        return result.as_gate_inputs()
    except Exception:  # noqa: BLE001 -- best-effort, same invariant as _catalog_scan_best_effort
        logging.getLogger(__name__).info(
            "Phase C dark-pool baseline computation skipped for %s (non-fatal, falls back to bootstrap)", ticker
        )
        return None


def _catalog_discovery_best_effort(adapter: UWDataAdapter, filters: dict, result: dict, kind: str = "on_demand") -> None:
    """QA remediation D3: the ONE shared persistence call for a Market
    Discovery snapshot, used by BOTH this route and the Phase D
    scheduler's background cycle (see scheduler.py's _run_cycle, updated
    this round to call through here too instead of calling
    catalog_discovery_snapshot directly) -- never a second/duplicate
    discovery-persistence pipeline.

    Same Demo/Live separation mechanism every other catalog_*_best_effort
    helper in this file already uses: ONLY ever persists for a real
    LiveUWAdapter scan. SyntheticDataAdapter's demo-mode candidates are
    deliberately never written to discovery_snapshots/discovery_candidates
    -- they are not real upstream data, and persisting them would let
    synthetic/demo rows contaminate the same historical catalog tables
    Live data relies on for Market-wide Radar/freshness. Same invariant
    as every sibling helper: failures here are swallowed, never allowed
    to change what the route returns to its caller.
    """
    if not isinstance(adapter, LiveUWAdapter):
        return
    try:
        catalog = get_catalog()
        catalog.catalog_discovery_snapshot(
            filters=filters,
            candidates=result.get("candidates") or [],
            snapshot_at=result.get("snapshot_at") or scheduler_module_utcnow(),
            kind=kind,
        )
    except Exception:  # noqa: BLE001 -- see docstring: never propagate (incl. StorageGuardPausedError)
        logging.getLogger(__name__).info(
            "Discovery catalog snapshot write skipped (non-fatal, response unaffected)"
        )


def scheduler_module_utcnow() -> str:
    """Tiny shared helper so _catalog_discovery_best_effort never needs a
    second import of scheduler_store just for a timestamp fallback."""
    from .core.scheduler_store import utcnow_iso

    return utcnow_iso()


def _run_scan(adapter: UWDataAdapter, store: QualifyingDaysStore, ticker: str, kind: str = "on_demand") -> dict:
    """Phase D: `kind` ("on_demand" default, or "background" for the
    scheduler) is threaded through to the catalog AND the qualifying-
    days storage_guard check -- see _catalog_scan_best_effort and
    storage.QualifyingDaysStore.record_scan. A background caller that
    raises StorageGuardPausedError here is expected to catch it itself
    (see app.main._scheduler_run_ticker_scan) rather than get the
    on-demand-route-shaped HTTPException(507) below, which only makes
    sense for an interactive HTTP request."""
    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    stock_state = adapter.get_stock_state(ticker)
    current_state = store.current_state(ticker)
    qualifying_days = store.qualifying_days_count(ticker)
    catalog_dark_pool_baseline = _catalog_dark_pool_baseline_best_effort(adapter, ticker)
    result = scan_service.evaluate_ticker_scan(
        ticker=ticker,
        flow_alerts=flow_alerts,
        darkpool_trades=darkpool_trades,
        current_state=current_state,
        qualifying_days_count=qualifying_days,
        price_move_pct=_price_move_pct(stock_state),
        catalyst_identified=adapter.get_catalyst_identified(ticker),
        catalog_dark_pool_baseline=catalog_dark_pool_baseline,
    )
    any_gate_passed = len(result["gates_passed"]) > 0
    try:
        store.record_scan(ticker, any_gate_passed, result["state"], kind=kind)
    except StorageGuardPausedError as e:
        if kind != "on_demand":
            raise  # background caller: let it propagate for its own handling, never shaped as an HTTP 507
        # Phase A storage guard: never a crash/500 -- a clear, structured
        # 507 ("Insufficient Storage") disclosing exactly why new writes
        # are paused. Nothing is deleted; this scan's evidence is still
        # returned to the caller below, just not persisted to the
        # qualifying-days history until free space recovers.
        raise HTTPException(status_code=507, detail=str(e)) from e
    result["stock_state"] = stock_state
    result["raw"] = {"flow_alerts": flow_alerts, "darkpool_trades": darkpool_trades}
    # Phase B: added persistence layer, never changes `result` above --
    # this call happens AFTER result is fully built and must never be
    # allowed to alter it (see _catalog_scan_best_effort's docstring).
    _catalog_scan_best_effort(adapter, ticker, flow_alerts, darkpool_trades, result, kind=kind)
    return result


def _to_evidence_item(scan: dict) -> dict:
    """Shape one scan result into the dict backend/app/core/tip_evidence.py
    expects (see its module docstring for the schema)."""
    stock_state = scan.get("stock_state") or {}
    chg = _price_move_pct(stock_state)
    dp_metrics = scan["gates"]["dark_pool_anomaly"]["metrics"]
    return {
        "ticker": scan["ticker"],
        "state": scan["state"],
        "gates_passed": scan["gates_passed"],
        # tip_evidence._short()'s "synthetic_baseline" family needs the
        # real historical_percentile gate metrics (percentile value) to
        # disclose an honest number instead of a generic "elevated" --
        # carried through here rather than re-derived in the presentation
        # layer (see families()/evidence()/current_read() fixes, 2026-10-15).
        "gates_metrics": {name: g.get("metrics") for name, g in scan["gates"].items()},
        "dp": {"ratio": dp_metrics.get("ratio"), "hist": None, "prints": None} if dp_metrics.get("ratio") else None,
        "options": None,
        "options_partial": False,
        "price": {"chg_pct": chg, "last": stock_state.get("last_price")} if stock_state else None,
        "alerts": None,
        "context": None,
        "next_earnings_date": None,
        "research": [],
        # BUG FIX (#33, 2026-10-15 round 2): read directly from scan["gates"]
        # rather than scan["gates_passed"] -- gates_passed deliberately
        # excludes catalyst_identified (see scan_service.evaluate_ticker_scan),
        # so tip_evidence.families()/categories() needs its own explicit
        # field to recognize a genuine catalyst_identified pass as its own
        # independent evidence category (matching
        # signal_engine.INDEPENDENT_SIGNAL_GATES, which already counts it).
        "catalyst_identified": bool(scan["gates"].get("catalyst_identified", {}).get("passed")),
    }


@app.get("/api/health")
def health():
    adapter = get_adapter()
    return {"ok": True, "mode": _settings_store.get_mode(), "adapter": adapter.health()}


@app.get("/api/mode")
def mode():
    return {"mode": _settings_store.get_mode()}


@app.get("/api/auth/mode")
def auth_mode_indicator():
    """Refinement #5: unauthenticated, reachable before any auth
    decision -- lets the frontend branch its auth UI on a real backend
    fact (which mode is actually active right now) instead of guessing
    client-side. Local Mode: the frontend shows ZERO auth UI (no
    banner, no bootstrap polling, no token field). Network Mode: the
    frontend's existing pairing/bootstrap/token-entry UI stays fully
    intact and functional, completely unchanged.

    Discloses only which mode is active -- no token, no secret, no
    credential of any kind. This is a plain GET/non-mutating read (like
    /api/health, /api/mode, /api/settings/version), so it is exactly
    the kind of route a "none"/missing Sec-Fetch-Site genuinely can't
    avoid on first page load -- and, consistent with every other
    read-only route in this app, it was never gated on Sec-Fetch-Site/
    Origin/token in the first place (see require_auth's docstring)."""
    return {"mode": "local" if LOCAL_MODE_ACTIVE else "network", "local_mode_active": LOCAL_MODE_ACTIVE}


@app.get("/api/auth/bootstrap")
def auth_bootstrap(request: Request):
    """Self-serves the auth token to a genuine browser running ON the same
    machine as this backend (loopback peer IP) -- no shell/docker exec
    needed. Anyone else (LAN/remote) gets a clear 403 pointing at the
    documented `docker exec <container> cat /srv/data/.auth_token`
    fallback (see README/INSTALL_AGENT.md). This is the ONLY unauthenticated
    route that ever hands out the token, and it is itself exempt from
    require_auth (a brand-new install has no token to send yet)."""
    client_host = request.client.host if request.client else None
    if not is_loopback(client_host):
        raise HTTPException(
            status_code=403,
            detail=(
                "Token bootstrap is only available from the same machine (localhost). "
                "For a LAN or remote deployment, retrieve the token yourself: "
                "`docker exec <container> cat /srv/data/.auth_token` -- see README.md / "
                "INSTALL_AGENT.md 'Authentication' section."
            ),
        )
    return {"token": _auth_store.get_or_create()}


class PairBody(BaseModel):
    pairing_code: str


@app.post("/api/auth/pair")
def auth_pair(body: PairBody):
    """QA remediation D1: the secure fallback for exactly the real-world
    failure mode this item reproduces -- a genuine Docker Desktop browser
    whose request arrives at `is_loopback()` as the bridge-gateway
    address, not a real loopback peer, so GET /api/auth/bootstrap 403s.

    This route is intentionally NOT gated on request.client.host at all
    -- see auth.PairingCodeStore's module comment for why: the security
    boundary here is possession of the one-time code itself (64 bits of
    entropy, single-use, 15-minute TTL, rate-limited), which is only ever
    disclosed via `docker logs`/the host-mounted volume file -- channels
    a LAN/Docker-network-adjacent attacker who is NOT at the real host
    cannot read. Knowing this app is reachable, or even being on the
    exact same Docker bridge network, gives an attacker zero information
    about the current code.
    """
    if not _pairing_store.consume(body.pairing_code.strip()):
        raise HTTPException(
            status_code=403,
            detail=(
                "Invalid, expired, or already-used pairing code. Run `docker logs <container>` "
                "(or `docker compose logs`) on the Docker host and look for the most recent "
                "'Docker Desktop / browser pairing code' line -- codes rotate every 15 minutes "
                "and are single-use. See README.md / INSTALL_AGENT.md 'Authentication' section."
            ),
        )
    return {"token": _auth_store.get_or_create()}


# --- Settings / credentials -------------------------------------------
#
# These routes are the only place UW/AI API keys are read or written.
# Keys are NEVER included in a response body -- only a `configured`
# boolean and the key's last 4 characters (for user recognition) are ever
# returned. See settings_store.py for the on-disk format and
# README "Credential precedence" for the .env-vs-UI-key precedence rule.


class ModeBody(BaseModel):
    mode: str


class UwKeyBody(BaseModel):
    api_key: str


class AiKeyBody(BaseModel):
    provider: str
    api_key: str


class ConfirmBody(BaseModel):
    # Phase 4B: AI provider test is a REAL, individually-billed API call
    # (unlike MCP test, which is UW-quota-only) -- confirm=true is a
    # structural guard against it ever firing from anything but an
    # explicit user click that has already seen the cost disclosure in
    # the UI, not just a disabled-by-default frontend button.
    confirm: bool = False


@app.get("/api/settings")
def get_settings():
    data = _settings_store.get_all()
    uw_key, uw_source = _effective_uw_key(data)
    ai_key = data.get("ai_api_key")
    # Disclosure for item 10: if the UI-saved key was removed but a
    # .env-level key is still configured, the effective key silently
    # becomes that env key -- say so explicitly rather than letting the
    # user believe "Remove Saved Key" cleared all credentials.
    env_key_present = bool(settings.uw_api_key)
    ui_key_present = bool(data.get("uw_api_key"))
    return {
        "mode": data["mode"],
        "public_demo": settings.public_demo,
        "uw": {
            "configured": bool(uw_key),
            "source": uw_source,  # "ui" | "env" | "none"
            "last4": last4(uw_key),
            "last_test_result": _last_uw_test_result,
            "verified": _settings_store.is_key_verified(uw_key),
            "env_fallback_present": env_key_present,
            "env_fallback_disclosure": (
                "UI-saved key removed. A .env-configured key is still present and will be used."
                if env_key_present and not ui_key_present
                else None
            ),
        },
        "ai": {
            "configured": bool(ai_key),
            "provider": data.get("ai_provider"),
            "last4": last4(ai_key),
            # Priority 5 fix (2026-10-15 round 3): "an AI provider key is
            # configured" and "which transport Investigate with AI will
            # actually use" are two separate facts and must not be
            # conflated in the Settings display -- a key being present
            # does not mean MCP transport is active; INVESTIGATE_TRANSPORT
            # (env var, unrelated to any credential) determines that.
            # This exposes the EXISTING config value only -- the same one
            # /api/investigate/availability already discloses once a
            # ticker is loaded -- so it's visible in Settings up front,
            # before the user has navigated anywhere. No new business
            # logic; genuinely safe to disclose (carries no credential).
            "investigate_transport": settings.investigate_transport,
        },
        # Market Radar (2026-10-14, Phase A): included here too (not just
        # the dedicated /api/settings/watchlist GET) so the Settings page
        # can render the watchlist editor from the same /api/settings
        # call it already makes, without a second round-trip.
        "watchlist": {
            "tickers": _settings_store.get_watchlist(default=settings.live_watchlist),
            "max_size": MAX_WATCHLIST_SIZE,
        },
        # Application Updates (2026-10-15): bundled here too so Settings
        # can render the "Installed version" line from the same call it
        # already makes -- no network call involved (see version.py).
        # The actual GitHub check is a separate, explicitly-triggered
        # GET /api/settings/update-check (never auto-fetched).
        "build": {**_version.get_build_info(), "repo_url": f"https://github.com/{_update_check.GITHUB_REPO}"},
    }


class WatchlistBody(BaseModel):
    watchlist: list[str]


@app.get("/api/settings/watchlist")
def get_watchlist():
    """Market Radar (2026-10-14, Phase A): the persisted watchlist (or
    the env-seeded default if never saved), plus the size cap so the UI
    can disclose it rather than discovering it only on a rejected save."""
    wl = _settings_store.get_watchlist(default=settings.live_watchlist)
    return {"watchlist": wl, "max_size": MAX_WATCHLIST_SIZE, "default": settings.live_watchlist}


@app.put("/api/settings/watchlist", dependencies=[Depends(require_auth)])
def set_watchlist(body: WatchlistBody):
    """Whole-list replace (not incremental add/remove) -- simpler and
    atomic; the frontend maintains the current list client-side and PUTs
    the full updated array on every add/remove click. Resets the adapter
    cache immediately so the very next /api/radar scan reflects it."""
    try:
        data = _settings_store.set_watchlist(body.watchlist)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _reset_adapter_cache()
    return {"watchlist": data["watchlist"], "max_size": MAX_WATCHLIST_SIZE}


@app.post("/api/settings/mode", dependencies=[Depends(require_auth)])
def set_mode(body: ModeBody):
    requested = body.mode.strip().lower()
    if requested not in ("demo", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'demo' or 'live'")
    if requested == "live":
        if settings.public_demo:
            raise HTTPException(
                status_code=403,
                detail="Live mode is not available in the public demo build.",
            )
        key, _source = _effective_uw_key(_settings_store.get_all())
        if not key:
            raise HTTPException(
                status_code=400,
                detail="Cannot switch to live mode: no UW API key is configured yet.",
            )
        # Real server-side gate (not just a disabled UI button): live mode
        # may only be activated for a key that has ITSELF passed
        # /api/settings/uw-key/test -- applies identically whether the key
        # came from the UI store or the .env fallback (no source is
        # "trusted by default"). A key merely being present is not enough.
        if not _settings_store.is_key_verified(key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cannot switch to live mode: this UW API key has not passed "
                    "Test Connection yet. Use Test Connection in Settings first."
                ),
            )
    data = _settings_store.set_mode(requested)
    _reset_adapter_cache()
    return {"mode": data["mode"]}


@app.post("/api/settings/uw-key", dependencies=[Depends(require_auth)])
def save_uw_key(body: UwKeyBody):
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential entry is not available in the public demo build.")
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    _settings_store.set_uw_key(key)
    _reset_adapter_cache()
    global _last_uw_test_result
    _last_uw_test_result = None
    return {"configured": True, "last4": last4(key)}


@app.post("/api/settings/uw-key/test", dependencies=[Depends(require_auth)])
def test_uw_key():
    """Real live test against the UW API using whichever key is currently
    effective (UI-saved, else .env fallback) -- not a stub. Never raises on
    a UW-side error; every branch returns a structured, keyless result."""
    global _last_uw_test_result
    data = _settings_store.get_all()
    key, source = _effective_uw_key(data)
    if not key:
        _last_uw_test_result = "not_configured"
        return {"result": "not_configured", "message": "No UW API key is configured.", "source": source}

    client = UWRestClient(
        api_key=key,
        base_url=settings.uw_base_url,
        client_api_id=settings.uw_client_api_id,
    )
    body = client.stock_info("SPY")  # cheap, cache-friendly endpoint used purely as a connectivity probe
    status = body.get("status") if isinstance(body, dict) else None
    if status == UNAUTHORIZED:
        result, message = "unauthorized", "Unusual Whales rejected this API key (401)."
    elif status == UNAVAILABLE:
        result, message = "forbidden_tier_limited", "This endpoint isn't available on the configured UW tier (403)."
    elif status == ERROR:
        result, message = "network_error", "Could not reach the Unusual Whales API."
    else:
        result, message = "success", "Connected to the Unusual Whales API."
    _last_uw_test_result = result
    if result == "success":
        # Persist a hash of THIS exact key as the one verified-live-capable
        # credential. This is the only place that ever sets the record --
        # a mere "key is present" never does. See settings_store.record_
        # verified_uw_key / is_key_verified and the live-mode gate above.
        _settings_store.record_verified_uw_key(key)
    return {"result": result, "message": message, "source": source}


@app.delete("/api/settings/uw-key", dependencies=[Depends(require_auth)])
def delete_uw_key():
    data = _settings_store.clear_uw_key()
    _reset_adapter_cache()
    global _last_uw_test_result
    _last_uw_test_result = None
    return {"configured": False, "mode": data["mode"]}


@app.post("/api/settings/ai-key", dependencies=[Depends(require_auth)])
def save_ai_key(body: AiKeyBody):
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential entry is not available in the public demo build.")
    provider = body.provider.strip().lower()
    if provider not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail="provider must be 'anthropic' or 'openai'")
    key = body.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="api_key must not be empty")
    _settings_store.set_ai_key(provider, key)
    return {"configured": True, "provider": provider, "last4": last4(key)}


@app.delete("/api/settings/ai-key", dependencies=[Depends(require_auth)])
def delete_ai_key():
    _settings_store.clear_ai_key()
    return {"configured": False}


@app.post("/api/settings/ai-key/test", dependencies=[Depends(require_auth)])
def test_ai_key(body: ConfirmBody):
    """Phase 4B: minimal, bounded connectivity test for the SAVED AI
    provider key -- one short request, tiny token cap, no tool-selection
    or synthesis prompt. This is a REAL, individually-billed API call
    (unlike the UW/MCP test below), so it requires confirm=true from an
    explicit user action that has already seen the cost disclosure --
    never fired automatically. A success here proves ONLY that the
    credential+model answer a request; it is NOT evidence that MCP or any
    tool execution works (see /api/settings/mcp/test, which is separate
    and LLM-free) -- the response is intentionally plain so the frontend
    cannot accidentally conflate the two."""
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Credential testing is not available in the public demo build.")
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required -- this makes one real, individually-billed request to the configured AI provider.",
        )
    data = _settings_store.get_all()
    provider = data.get("ai_provider")
    key = data.get("ai_api_key")
    if not provider or not key:
        raise HTTPException(status_code=400, detail="No AI provider key is configured yet. Save one first.")
    result = ai_llm_provider.test_provider_connection(provider, key)
    return {**result, "provider": provider, "proves_mcp_works": False}


@app.post("/api/settings/mcp/test", dependencies=[Depends(require_auth)])
def test_mcp():
    """Phase 4B: standalone genuine MCP-protocol connectivity test
    (connect, authenticate, tool discovery, ONE bounded read-only tool
    call against a fixed test ticker) -- NO LLM call of any kind, so this
    does not touch your AI provider key or incur any AI provider cost; it
    does use one real, bounded UW API call against your existing UW key.
    A success here proves ONLY that MCP transport + UW auth work; it is
    NOT evidence that AI-directed tool selection/synthesis works (see
    /api/settings/ai-key/test, which is separate) -- the response is
    intentionally plain so the frontend cannot accidentally conflate the
    two."""
    if settings.public_demo:
        raise HTTPException(status_code=403, detail="Connectivity testing is not available in the public demo build.")
    data = _settings_store.get_all()
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        raise HTTPException(status_code=400, detail="No UW API key is configured yet. Save and test one first.")
    result = ai_mcp_tool_client.test_mcp_connection(uw_key)
    return {**result, "proves_ai_synthesis_works": False}


@app.get("/api/diagnostics/uw-requests", dependencies=[Depends(require_auth)])
def uw_request_diagnostics():
    """Phase B: READ-ONLY UW request-handling telemetry (per-endpoint
    counts/success/retried/failed/avg-latency/outcome breakdown) --
    behind require_auth like every other sensitive route, never a new
    unauthenticated endpoint. Exposes zero credentials (no API key, no
    header values) -- counters and latencies only. Empty/zero-valued
    when running in demo mode (SyntheticDataAdapter has no HTTP client
    at all) or before any live UW call has happened yet this process."""
    adapter = get_adapter()
    client = getattr(adapter, "client", None)
    telemetry = getattr(client, "telemetry", None)
    scheduler_mode = get_scheduler_runner().store.get_mode()
    if telemetry is None:
        return {
            "mode": getattr(adapter, "mode", "unknown"), "by_path": {},
            "max_concurrent_requests": None, "note": "No live UW HTTP client in this mode.",
            "collection_mode": scheduler_mode,
            "counts_reset_on": "process restart (in-memory only, never persisted)",
        }
    gate = uw_concurrency.get_shared_gate(client.max_concurrent_requests)
    by_path = telemetry.summary()
    # QA remediation D10: aggregate, bounded totals across the whole
    # by_path breakdown -- never a second high-cardinality store, just a
    # sum over the SAME fixed-size per-endpoint counters above.
    total_logical = sum(e["logical_requests"] for e in by_path.values())
    total_http_attempts = sum(e["http_attempts"] for e in by_path.values())
    total_retried = sum(e["retried"] for e in by_path.values())
    return {
        "mode": adapter.mode,
        # QA remediation D9/D10: the ACTUAL collection mode this process
        # is configured with right now (disabled/dry_run/enabled) -- lets
        # a tester correlate request volume with whether the scheduler
        # was actually doing anything, without a separate call.
        "collection_mode": scheduler_mode,
        "by_path": by_path,
        "totals": {
            "logical_requests": total_logical,
            "http_attempts": total_http_attempts,
            "retried_logical_requests": total_retried,
        },
        "max_concurrent_requests": client.max_concurrent_requests,
        "max_retries": client.max_retries,
        # Live, real-time in-flight counts from the ONE shared
        # PriorityGate every call path funnels through (core/
        # uw_concurrency.py) -- "currently observed" is literal: these
        # change from one call to the next, unlike the by_path counters
        # above, which only ever grow until a restart.
        "in_flight": {
            "interactive": gate.interactive_in_flight,
            "background": gate.background_in_flight,
            "max_observed_concurrent": gate.max_observed_in_flight,
        },
        # Bounded-by-design disclosure (Chad's explicit D10 requirement):
        # this is a small, fixed-size aggregate -- never keyed by raw
        # ticker/timestamp, never unbounded per-request history -- and it
        # resets to zero on every process restart, not on any rolling
        # schedule. A tester seeing small numbers right after a redeploy
        # is seeing a genuinely fresh process, not a bug.
        "counts_reset_on": "process restart (in-memory only, never persisted)",
    }


class SchedulerModeBody(BaseModel):
    mode: str


@app.get("/api/scheduler/status", dependencies=[Depends(require_auth)])
def scheduler_status():
    """Phase D: read-only scheduler status -- current mode, last/next
    run, how many candidates the last cycle evaluated, and the pause
    reason if any (e.g. a storage_guard pause). Behind require_auth like
    every other diagnostics-shaped route (same pattern as
    /api/diagnostics/uw-requests)."""
    runner = get_scheduler_runner()
    state = runner.store.get_state()
    lease = runner.store.get_lease()
    adapter = get_adapter()
    uw_client = getattr(adapter, "client", None)
    return {
        "mode": state["mode"],
        "updated_at": state["updated_at"],
        "updated_by": state["updated_by"],
        "last_run_started_at": state["last_run_started_at"],
        "last_run_finished_at": state["last_run_finished_at"],
        "next_run_at": state["next_run_at"],
        "last_cycle_candidates_evaluated": state["last_cycle_candidates_evaluated"],
        "pause_reason": state["pause_reason"],
        "lease_held_by": lease["worker_id"] or None,
        "top_n": runner.top_n,
        "scan_interval_s": runner.scan_interval_s,
        "freshness_window_s": scheduler_freshness.freshness_window_s(),
        # QA remediation D9: the EFFECTIVE running configuration -- the
        # actual values this process is using right now, not just the
        # documented defaults/env-var names. uw_max_concurrent_requests
        # is None (never 0 or a guessed number) when there is no live UW
        # HTTP client at all in the current mode (demo mode).
        "uw_max_concurrent_requests": getattr(uw_client, "max_concurrent_requests", None),
        "lease_ttl_s": runner.lease_ttl_s,
        "poll_interval_s": runner.poll_interval_s,
        # Whether a run-once (or automatic) cycle is actively executing
        # right now -- lets the UI disable the "Run once" button instead
        # of letting a user fire a call that's guaranteed to be rejected.
        "cycle_in_progress": runner._cycle_lock.locked(),  # noqa: SLF001 -- read-only status disclosure
    }


class RunOnceBody(BaseModel):
    # Final round (2026-10-16): optional execution-depth selector. None
    # (the default -- every pre-existing caller/test) preserves the
    # EXACT legacy behavior: runner.run_once(), which refuses while the
    # persisted recurring mode is 'disabled'. Passing an explicit depth
    # routes to runner.run_once_with_depth(), the new path that is fully
    # decoupled from the persisted mode -- this is what the two new
    # "Discover Once" / "Discover & Evaluate Once" UI buttons call, and
    # the only way a one-time run is possible while mode stays Disabled.
    depth: Optional[str] = None


@app.post("/api/scheduler/run-once", dependencies=[Depends(require_auth)])
def scheduler_run_once(body: RunOnceBody = RunOnceBody()):
    """QA remediation D9 (legacy, mode-gated path) + final-round item
    (decoupled one-time path): see scheduler.SchedulerRunner.run_once /
    run_once_with_depth docstrings for the full set of guarantees (lease
    + in-process lock, zero AI import, bounded top_n, returns to idle
    afterward, a second concurrent call -- via EITHER path, or a
    genuinely different worker/process on the recurring path -- is
    rejected rather than queued, no RTH/market-hours gate applied here).
    """
    runner = get_scheduler_runner()
    if body.depth is not None:
        if body.depth not in scheduler_module.RUN_ONCE_DEPTHS:
            raise HTTPException(
                status_code=400,
                detail=f"depth must be one of {scheduler_module.RUN_ONCE_DEPTHS}, got {body.depth!r}",
            )
        result = runner.run_once_with_depth(body.depth)
    else:
        result = runner.run_once()
    if result.get("reason") == "cycle_already_in_progress":
        raise HTTPException(status_code=409, detail="A scheduler cycle is already in progress. Try again shortly.")
    return result


@app.post("/api/scheduler/mode", dependencies=[Depends(require_auth)])
def set_scheduler_mode(body: SchedulerModeBody):
    """Phase D: explicit, authenticated mode change -- disabled / dry_run
    / enabled. NEVER auto-called by anything else in this app; this is
    the only way the mode ever changes other than an upgrade preserving
    whatever was already set (see core/scheduler_store.py)."""
    if body.mode not in SCHEDULER_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {SCHEDULER_MODES}")
    runner = get_scheduler_runner()
    state = runner.store.set_mode(body.mode, actor="authenticated_user")
    return {"mode": state["mode"], "updated_at": state["updated_at"], "updated_by": state["updated_by"]}


@app.get("/api/radar/market-wide", dependencies=[Depends(require_auth)])
def market_wide_radar(limit: int = DISCOVERY_LIMIT_CAP):
    """Phase D: the Market-wide Radar view -- the most recent discovery
    snapshot's candidates, each tagged with its real freshness state
    (current / stale / unevaluated) per core/scheduler_freshness.py.
    Candidates with NO cataloged discovery snapshot yet (e.g. the
    scheduler has never run, or is disabled) return an empty list with
    an honest `note`, never a fabricated placeholder.

    Honest coverage disclosure (reusing Phase C's existing language
    pattern, not inventing new wording): this reflects whatever the
    MOST RECENT discovery snapshot + per-ticker evaluation already
    cataloged -- it is never a live re-fetch triggered by this GET, and
    it never implies broader coverage than the bounded top-N the
    scheduler actually evaluated (see /api/scheduler/status for exactly
    how many candidates the last cycle evaluated).

    UX round (2026-10-16), item 1 root-cause fix: the frontend's Market
    Intelligence summary shows `candidate_count_in_snapshot` (the FULL
    count recorded by catalog_discovery_snapshot at write time) right
    next to the rendered candidate rows (this route's own `candidates`
    list) -- a real, user-visible discrepancy whenever this route's own
    SQL LIMIT truncated the rows below the full snapshot size. The
    default here used to be a plain, undocumented 20, completely
    decoupled from discovery.py's own DISCOVERY_LIMIT_CAP (50) that
    actually bounds how many candidates a snapshot can contain -- so a
    normal, uncapped scan (up to 50 candidates) silently showed only
    its first 20 rows while the summary honestly reported the real,
    larger total. Defaulting this route's own limit to that SAME cap
    makes the common case match again; `candidates_shown` and
    `rows_truncated_note` below are the explicit, honest fallback for
    any remaining case (an explicit smaller `limit` query param, or a
    pre-existing snapshot recorded under an older/looser cap) where a
    real truncation still happens -- never a silent, undisclosed one."""
    catalog = get_catalog()
    with catalog._conn() as conn:  # noqa: SLF001 -- read-only, same pattern IngestionCatalog's own methods use internally
        row = conn.execute(
            "SELECT id, event_time, filters_json, candidate_count FROM discovery_snapshots "
            "ORDER BY event_time DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {
                "candidates": [], "snapshot_at": None, "note": "No discovery snapshot cataloged yet -- "
                "the scheduler has not run a cycle yet (or is disabled). This is never a live fetch; "
                "enable dry_run or enabled mode, or run a manual Market Discovery scan, to populate this view.",
            }
        snapshot_id, event_time, filters_json, candidate_count = row
        cand_rows = conn.execute(
            "SELECT ticker, raw_json FROM discovery_candidates WHERE snapshot_id = ? "
            "ORDER BY id LIMIT ?",
            (snapshot_id, limit),
        ).fetchall()
    import json as _json

    candidates = []
    for ticker, raw_json in cand_rows:
        fresh = scheduler_freshness.classify_candidate(catalog, ticker)
        candidates.append({
            "ticker": ticker,
            "raw": _json.loads(raw_json) if raw_json else None,
            "freshness": fresh.state,
            "evaluation": fresh.evaluation,
            "age_s": fresh.age_s,
            "freshness_window_s": fresh.window_s,
        })
    candidates_shown = len(candidates)
    rows_truncated_note = None
    if candidate_count is not None and candidates_shown < candidate_count:
        rows_truncated_note = (
            f"Showing {candidates_shown} of {candidate_count} discovered candidates from this "
            f"snapshot -- the remaining {candidate_count - candidates_shown} aren't rendered below "
            "(display limit), not missing data."
        )
    return {
        "candidates": candidates,
        "snapshot_at": event_time,
        "candidate_count_in_snapshot": candidate_count,
        "candidates_shown": candidates_shown,
        "rows_truncated_note": rows_truncated_note,
        "scan_scope": "market_wide_screener",
        "coverage_note": "Freshness reflects the most recent cataloged signal_evaluations row for each "
        "ticker, from EITHER a watchlist scan or a discovery/scheduler evaluation -- whichever is more "
        "recent wins; this view never shows two conflicting entries for the same ticker.",
    }


@app.get("/api/settings/version")
def get_version():
    """Build-time version/commit info (see version.py docstring for why
    this can't just shell out to `git` inside the running container). No
    network call -- safe to fetch on every Settings page load."""
    info = _version.get_build_info()
    return {**info, "repo_url": f"https://github.com/{_update_check.GITHUB_REPO}"}


@app.get("/api/settings/update-check")
def get_update_check():
    """Checks the PUBLIC GitHub repo for newer commits than the baked-in
    installed commit. ONE unauthenticated outbound call to GitHub per
    request -- no credential of this app's is ever sent. Deliberately not
    called automatically by the frontend; the user must click "Check for
    Updates". Never raises: any network/rate-limit/shape failure comes
    back as status=unable_to_check, see update_check.py."""
    info = _version.get_build_info()
    return _update_check.check_for_updates(info.get("commit"))


def _cached_scan_for_ticker(catalog: IngestionCatalog, store: QualifyingDaysStore, ticker: str) -> tuple[Optional[dict], scheduler_freshness.FreshnessResult]:
    """W2 cache-first read: the EXACT scan dict shape `_run_scan` already
    returns, rebuilt PURELY from the most recent `signal_evaluations` row
    already cataloged for `ticker` -- zero live adapter calls. See
    `_catalog_scan_best_effort`: the `result` it persists is the same
    dict `_run_scan` returns (gates/state/confidence/stock_state/raw all
    included), so `raw_json` here IS a valid `scan` dict, not a re-derived
    approximation. Returns (None, freshness) when nothing has ever been
    cataloged for this ticker yet (freshness.state == "unevaluated") --
    callers must handle that case without ever inventing data for it."""
    latest = catalog.get_latest_signal_evaluation(ticker)
    freshness = scheduler_freshness.classify_freshness(latest, ticker)
    if latest is None or not latest.get("raw_json"):
        return None, freshness
    return latest["raw_json"], freshness


def _not_evaluated_evidence_item(ticker: str, state: Optional[str] = None) -> dict:
    """Placeholder evidence item for a watchlist ticker that has never
    been evaluated yet (no cataloged signal_evaluations row at all) --
    rendered honestly as "not evaluated" (no gates/price/evidence
    invented), while still surfacing the ticker's real qualifying-days
    `state` (from QualifyingDaysStore, not the UW-call-costing catalog)
    when one is already known."""
    return {
        "ticker": ticker, "state": state, "gates_passed": [], "gates_metrics": {},
        "dp": None, "options": None, "options_partial": False, "price": None,
        "alerts": None, "context": None, "next_earnings_date": None, "research": [],
        "catalyst_identified": False,
    }


@app.get("/api/radar")
def radar():
    """Market Radar: the user's personal watchlist -- qualifying tickers,
    highest-attention first.

    S1 SECURITY FIX (2026-10-16): this route is now UNCONDITIONALLY
    cache-only/read-only -- it takes NO `refresh` parameter at all (not
    "accepts but ignores", literally not in the function signature, so
    FastAPI never even looks at a `?refresh=` query string). The prior
    version accepted `refresh: bool = False` and performed a real live
    UW scan whenever `refresh=true` was present on a plain GET -- a
    foreign/malicious page can trigger a real GET cross-origin via a
    no-cors fetch or an <img>/<script> src with no CORS preflight and no
    way for this app to ever see or reject it, REGARDLESS of any
    Origin/Sec-Fetch-Site check, because the browser never sends a
    CORS-relevant header on that class of request in the first place.
    The only structurally safe fix is removing the side effect from GET
    entirely -- see `_radar_impl` below, which now takes `refresh` as a
    plain Python argument never exposed on this route, and
    POST /api/radar/refresh (authenticated, concurrency-locked), the
    SOLE remaining way to trigger a real scan.
    """
    return _radar_impl(refresh=False)


def _radar_impl(refresh: bool):
    """Shared implementation for the cache-only GET above and the
    authenticated, explicit POST /api/radar/refresh below. `refresh` is
    a plain internal argument, never sourced from request input on the
    GET path (see radar() above) -- this is what makes GET's cache-only
    behavior unconditional rather than merely parameter-gated.

    W2 fix (cache-first watchlist experience): for a real LiveUWAdapter,
    an ordinary GET (the default -- page load, refresh, nav-back, auth-
    state-change, history restoration) makes ZERO live UW HTTP calls. It
    reads whatever was most recently cataloged for each watchlist ticker
    (IngestionCatalog.get_latest_signal_evaluation -- the SAME table the
    scheduler/discovery pipeline and every prior on-demand scan already
    write to, never a second store) and reports each ticker's freshness
    (current/stale/unevaluated, scheduler_freshness.classify_freshness).
    A real live rescan only happens when the caller is POST /api/radar/
    refresh (refresh=True) or when the adapter isn't live at all
    (SyntheticDataAdapter/demo mode: there is no real HTTP cost to a demo
    scan, so Demo Mode's existing always-fresh behavior is intentionally
    left unchanged here).

    Market Radar (2026-10-14, Phase A): records when this scan ran and
    how many tickers it covered (persisted -- survives container
    recreation), and explicitly discloses `scan_scope: "watchlist"` so
    the UI never implies broader-market discovery that isn't actually
    happening. W2-a fix (2026-10-16): that record (and the
    `last_scan_at`/`last_scan_count` it reports) is now only ever WRITTEN
    on a real live_fetch -- a cache-only read reports the last GENUINE
    scan's timestamp (read-only, from SettingsStore.get_all()) rather
    than overwriting it with "now", so repeated cache reads (F5,
    ordinary nav) never fabricate a fresh-looking scan time for data that
    was not actually just re-fetched."""
    adapter = get_adapter()
    store = get_store()
    catalog = get_catalog()
    universe = adapter.get_universe()
    if not universe:
        return {"highest": [], "other": [], "scanned": 0, "scan_scope": "watchlist", "last_scan_at": None}

    is_live = isinstance(adapter, LiveUWAdapter)
    live_fetch = refresh or not is_live

    items = []
    scans_by_ticker = {}
    freshness_by_ticker: dict[str, scheduler_freshness.FreshnessResult] = {}
    for ticker in universe:
        if live_fetch:
            scan = _run_scan(adapter, store, ticker)
            # Freshness after a real fetch is always "current" by definition
            # (it was just retrieved) -- avoid a second catalog round-trip.
            freshness_by_ticker[ticker] = scheduler_freshness.FreshnessResult(
                ticker=ticker, state=scheduler_freshness.CURRENT, evaluation=None, age_s=0.0,
                window_s=scheduler_freshness.freshness_window_s(),
            )
        else:
            scan, freshness = _cached_scan_for_ticker(catalog, store, ticker)
            freshness_by_ticker[ticker] = freshness
            if scan is None:
                scan = _not_evaluated_evidence_item(ticker)
                # Give the placeholder the real current qualifying-days
                # history (QualifyingDaysStore is independent of the UW-
                # call-costing catalog -- reading it is not a live fetch).
                scan["state"] = store.current_state(ticker) or "NEW"
                scan["confidence"] = None
                scan["signal_type"] = None
                scan["qualifying_days_count"] = store.qualifying_days_count(ticker)
                scan["gates"] = {}
        scans_by_ticker[ticker] = scan
        items.append(_to_evidence_item(scan) if scan.get("gates") else _not_evaluated_evidence_item(ticker, scan.get("state")))

    highest, other = tip_evidence.present(items, next_session=date.today() + timedelta(days=1))

    def _attach(entry: dict) -> dict:
        s = scans_by_ticker[entry["ticker"]]
        fr = freshness_by_ticker.get(entry["ticker"])
        # Pre-existing gap found while building the Stage 2 signal-state
        # filter: tip_evidence.present()'s entry dict has never carried a
        # "state" key at all (it only tracks state internally for
        # ordering/STATE_RANK) -- frontend/app.core.js's renderCard()/
        # renderRow() have always rendered `state-${entry.state}` against
        # an undefined value, so the state tag on every Market Radar
        # card/row has always been visually blank. Fixing here (real data
        # already computed in `scan`, just never attached) since the new
        # client-side state filter needs a real field to filter on.
        entry["state"] = s.get("state")
        entry["confidence"] = s.get("confidence")
        entry["signal_type"] = s.get("signal_type")
        entry["qualifying_days_count"] = s.get("qualifying_days_count")
        # Compact signal-history indicator (Stage 2 / P3): only non-empty
        # when this ticker genuinely has more than one real recorded scan
        # (store.history() -- never fabricated). On a freshly started
        # process that's only PRSX (seeded + one live scan = 3 real
        # entries); every other ticker stays [] until it's actually been
        # scanned more than once. The frontend only renders a chip when
        # this list has 2+ entries.
        hist = store.history(entry["ticker"])
        entry["history_states"] = [h["state"] for h in hist] if len(hist) > 1 else []
        # W2: evaluation-freshness is a structurally DISTINCT concept from
        # signal lifecycle (state) and confidence -- never conflated. One
        # of "current" / "stale" / "unevaluated"; `last_evaluated_at` is
        # the real cataloged retrieved_at (None when never evaluated).
        if fr is not None:
            entry["eval_freshness"] = fr.state
            entry["last_evaluated_at"] = (fr.evaluation or {}).get("retrieved_at") if fr.evaluation else None
        else:
            entry["eval_freshness"] = "current"
            entry["last_evaluated_at"] = None
        return entry

    # W2-a fix: only a REAL live fetch may write a new last_scan_at/
    # last_scan_count -- a cache-only read must report the existing
    # persisted values untouched, never overwrite them with "now".
    if live_fetch:
        scan_record = _settings_store.record_scan(len(universe))
    else:
        scan_record = _settings_store.get_all()
    return {
        "highest": [_attach(e) for e in highest],
        "other": [_attach(e) for e in other],
        "scanned": len(universe),
        # Honest disclosure (2026-10-14, Phase A): this is a scan of the
        # user's configured watchlist, never broader-market discovery --
        # see this function's docstring for why that distinction matters.
        "scan_scope": "watchlist",
        "last_scan_at": scan_record.get("last_scan_at"),
        # W2: lets the UI render "cached data, no live fetch happened" vs
        # "this response just performed a real refresh" without guessing
        # from eval_freshness alone (e.g. a brand-new live-mode watchlist
        # with nothing cataloged yet is correctly "unevaluated" either way).
        "served_from_cache": not live_fetch,
    }


_radar_refresh_lock = threading.Lock()


@app.post("/api/radar/refresh", dependencies=[Depends(require_auth)])
def radar_refresh():
    """W2's explicit "Refresh Data" action: the ONLY route that makes a
    real live UW fetch for the personal watchlist. Structurally separate
    from the market-wide scheduler (/api/scheduler/*) -- different
    trigger (an explicit user click here vs. a scheduled/run-once cycle
    there), different scope (the user's own watchlist vs. broader-market
    discovery) -- sharing only the same underlying UW concurrency gate
    (core/uw_concurrency.py, via the one shared adapter/client every
    route already funnels through) and the same catalog tables
    (IngestionCatalog, via the exact same `_run_scan`/
    `_catalog_scan_best_effort` path GET /api/radar?refresh=true already
    uses -- never a second pipeline).

    Duplicate-concurrent-refresh prevention: a non-blocking lock acquire;
    a second call while one is already in flight gets a clean 409 rather
    than silently queuing, double-fetching, or double-counting telemetry."""
    if not _radar_refresh_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A watchlist refresh is already in progress. Try again shortly.")
    try:
        return _radar_impl(refresh=True)
    finally:
        _radar_refresh_lock.release()


class DiscoverFiltersBody(BaseModel):
    # Market Discovery (C2b/C2c, 2026-10-15): all optional named filters
    # over UW's real /api/screener/stocks fields -- no raw UW query DSL
    # exposed to the end user. `limit` is clamped server-side regardless
    # of what's sent here (see live_uw_adapter.DISCOVERY_LIMIT_CAP).
    min_premium: Optional[float] = None
    min_call_volume: Optional[int] = None
    min_put_volume: Optional[int] = None
    min_iv_rank: Optional[float] = None
    min_change: Optional[float] = None
    max_change: Optional[float] = None
    limit: int = 20  # also hard-clamped server-side by the adapter regardless of this value


@app.post("/api/radar/discover", dependencies=[Depends(require_auth)])
def radar_discover(body: DiscoverFiltersBody):
    """Market Discovery (C2b/C2c): EXACTLY ONE bounded call to the
    adapter's discover_candidates per invocation -- no automatic
    pagination, no automatic per-ticker follow-up scan. Returns raw
    screener 'candidates' (state = zero gate evaluation has happened),
    ranked, plus a snapshot timestamp and an explicit quota-cost
    disclosure (screener_requests_used). A candidate only becomes
    'evaluated' when the user explicitly calls GET /api/ticker/{ticker}
    (the 'Evaluate in TIP' action) -- never automatically here.

    Phase D: the actual normalize/rank/annotate logic now lives in
    core/discovery.run_discovery -- moved there verbatim so the Phase D
    background scheduler calls through this EXACT SAME pipeline rather
    than a second, duplicate one. This route is now a thin wrapper."""
    adapter = get_adapter()
    filters = {
        "min_premium": body.min_premium,
        "min_call_volume": body.min_call_volume,
        "min_put_volume": body.min_put_volume,
        "min_iv_rank": body.min_iv_rank,
        "min_change": body.min_change,
        "max_change": body.max_change,
        "limit": body.limit,
    }
    result = discovery.run_discovery(adapter, filters)
    # QA remediation D3: this used to be the ONLY difference between this
    # route and the Phase D scheduler's background discovery cycle --
    # the scheduler persisted every snapshot via IngestionCatalog.
    # catalog_discovery_snapshot (see scheduler.py's _run_cycle), this
    # manual route never did, so a manually-triggered real discovery scan
    # never produced a discovery_snapshots/discovery_candidates row and
    # therefore never showed up on Market-wide Radar (which only ever
    # reads the most recent cataloged snapshot -- never an in-memory
    # response). Fix: persist through the EXACT SAME catalog call the
    # scheduler already uses -- see _catalog_discovery_best_effort below,
    # which both this route and scheduler.py now call, so there is still
    # only one write path, never a second one invented for this route.
    _catalog_discovery_best_effort(adapter, filters, result)
    return result


def _validate_ticker_for_analysis(adapter: UWDataAdapter, ticker: str) -> str:
    ticker = ticker.upper()
    if isinstance(adapter, SyntheticDataAdapter):
        if ticker not in adapter.get_universe():
            raise HTTPException(status_code=404, detail=f"{ticker} is not one of the demo scenarios")
        return ticker
    try:
        return _validate_ticker_for_route(ticker)
    except AiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _not_evaluated_ticker_analysis_response(ticker: str, store: QualifyingDaysStore) -> dict:
    """W2-b honest cache-miss response: a watchlist-style placeholder for
    Ticker Analysis, never fabricating gates/evidence/confidence for a
    ticker that genuinely has no cataloged evaluation yet. Mirrors
    _not_evaluated_evidence_item's honesty but shaped for this route's
    response contract."""
    return {
        "ticker": ticker,
        "state": store.current_state(ticker) or "NEW",
        "previous_state": None,
        "transition_rule": None,
        "confidence": None,
        "signal_type": None,
        "confidence_reason": None,
        "qualifies": False,
        "qualifying_days_count": store.qualifying_days_count(ticker),
        "gates": {},
        "headline": "Not yet evaluated",
        "current_read": "This ticker has not been scanned yet in this session. Use the explicit "
                         "Refresh action to run a real evaluation.",
        "evidence": [],
        "stock_state": None,
        "history": store.history(ticker),
        "raw": None,
        "eval_freshness": "unevaluated",
        "last_evaluated_at": None,
        "served_from_cache": True,
    }


def _ticker_analysis_impl(ticker: str, refresh: bool) -> dict:
    """Shared implementation for the cache-only GET below and the
    authenticated, explicit POST /api/ticker/{ticker}/refresh. `refresh`
    is a plain internal argument, never sourced from GET query input --
    same structural pattern as _radar_impl, applied here for W2-b (same
    bug class as W2/S1: a plain GET must never cost a real UW call or
    write a new evaluation).

    REAL BUG FIX (2026-10-14, preserved): the fixed-universe check only
    applies to the synthetic/demo adapter; Live Mode validates the
    SYMBOL FORMAT and lets the real UW call be the source of truth on
    whether the ticker exists.

    W2-b fix (2026-10-16): for a real LiveUWAdapter, an ordinary GET
    (open/F5/Back/direct-link) is now cache-first -- it reads the most
    recently cataloged evaluation for this ticker (the SAME
    IngestionCatalog table Market Radar already reads) and makes ZERO
    live UW calls and writes NO new evaluation. A real rescan only
    happens via the explicit POST refresh route, or when the adapter
    isn't live at all (Demo Mode keeps its existing always-fresh
    behavior, same rationale as _radar_impl)."""
    adapter = get_adapter()
    store = get_store()
    catalog = get_catalog()
    ticker = _validate_ticker_for_analysis(adapter, ticker)

    is_live = isinstance(adapter, LiveUWAdapter)
    live_fetch = refresh or not is_live

    if live_fetch:
        scan = _run_scan(adapter, store, ticker)
        eval_freshness = "current"
        last_evaluated_at = None
        served_from_cache = False
    else:
        cached, freshness = _cached_scan_for_ticker(catalog, store, ticker)
        if cached is None:
            return _not_evaluated_ticker_analysis_response(ticker, store)
        scan = cached
        eval_freshness = freshness.state
        last_evaluated_at = (freshness.evaluation or {}).get("retrieved_at") if freshness.evaluation else None
        served_from_cache = True

    item = _to_evidence_item(scan)
    return {
        "ticker": ticker,
        "state": scan["state"],
        "previous_state": scan["previous_state"],
        "transition_rule": scan["transition_rule"],
        "confidence": scan["confidence"],
        "signal_type": scan["signal_type"],
        "confidence_reason": scan["confidence_reason"],
        "qualifies": scan["qualifies"],
        "qualifying_days_count": scan["qualifying_days_count"],
        "gates": scan["gates"],
        "headline": tip_evidence.headline(item),
        "current_read": tip_evidence.current_read(item),
        "evidence": tip_evidence.evidence(item),
        "stock_state": scan.get("stock_state"),
        "history": store.history(ticker),
        "raw": scan["raw"],
        "eval_freshness": eval_freshness,
        "last_evaluated_at": last_evaluated_at,
        "served_from_cache": served_from_cache,
    }


@app.get("/api/ticker/{ticker}")
def ticker_analysis(ticker: str):
    """Ticker Analysis: full evidence drill-down for one ticker -- gates,
    confidence, state transition, and the tip_evidence explanation.

    W2-b fix (2026-10-16): strictly cache-only, same structural pattern
    as GET /api/radar's S1 fix -- no `refresh` parameter is accepted
    here at all. See _ticker_analysis_impl's docstring."""
    return _ticker_analysis_impl(ticker, refresh=False)


_ticker_refresh_lock = threading.Lock()


@app.post("/api/ticker/{ticker}/refresh", dependencies=[Depends(require_auth)])
def ticker_analysis_refresh(ticker: str):
    """W2-b's explicit, user-triggered refresh action for Ticker Analysis
    specifically -- deliberately a SEPARATE control from the watchlist's
    POST /api/radar/refresh (different scope: one ticker a user is
    actively drilling into vs. the whole watchlist; a shared control
    would force refreshing the whole watchlist just to re-check one
    ticker's chart). Authenticated, and concurrency-locked the same way
    as the watchlist refresh -- a second concurrent refresh gets a clean
    409 rather than double-fetching or double-cataloging."""
    if not _ticker_refresh_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A refresh for this ticker is already in progress. Try again shortly.")
    try:
        return _ticker_analysis_impl(ticker, refresh=True)
    finally:
        _ticker_refresh_lock.release()


# Phase B (2026-10-14): maps a user-selectable chart range to an actual
# number of DAILY bars to request. UW's stock_ohlc endpoint takes a bar
# COUNT, not a calendar date range, so "1Y" means "request ~252 trading
# days of daily bars" -- these are standard US trading-day approximations
# (not exact, since holidays vary year to year), used only to decide how
# many bars to ask for; actual coverage is verified from what's returned
# (see range_fully_satisfied below), never assumed from this table alone.
RANGE_TO_TRADING_DAYS = {"1M": 22, "3M": 65, "6M": 130, "1Y": 252}

# 2026-10-15 (Chad-approved, item #3 -- 5D explicitly NOT included this
# pass): genuine intraday range. candle_size="5m" (distinct from the
# daily ranges' "1d") + UW's own timeframe=1D query param -- live-
# verified to return ~191 real 5-minute bars spanning pre-market through
# post-market for one real trading day (NOT a handful of daily bars
# relabeled). No raw-limit multiplier needed here: unlike daily bars,
# each intraday bucket already has its own distinct start_time, so there
# is no multi-session-per-timestamp collision to over-fetch around.
INTRADAY_RANGES = {"1D": {"candle_size": "5m", "limit": 200}}


@app.get("/api/ticker/{ticker}/ohlc")
def ticker_ohlc(ticker: str, timeframe: str = "1d", limit: int = 180, range: Optional[str] = None):
    """Candlestick bars + evidence markers for the Ticker Analysis chart.
    Markers come from the SAME flow_alerts/darkpool_trades the detection
    engine already uses -- never a new data source, never an invented
    price (see backend/app/core/candles.py::build_markers). `unavailable`
    is a structured, non-crashing result (e.g. an unentitled live key, or
    a demo scenario with no captured OHLC fixture) -- the frontend must
    show an explicit "chart data unavailable" state, not silently render
    nothing.

    Phase B (2026-10-14): optional `range` (1M/3M/6M/1Y) is a convenience
    that maps to `limit` via RANGE_TO_TRADING_DAYS and forces daily bars
    -- this is a REAL re-fetch requesting a different bar count, not a
    client-side zoom of a fixed dataset. `timeframe`/`limit` remain
    directly usable for any other caller (e.g. the marker-building logic
    elsewhere in this route already relies on the existing defaults)."""
    if range is not None:
        range = range.strip().upper()
        all_ranges = set(RANGE_TO_TRADING_DAYS) | set(INTRADAY_RANGES)
        if range not in all_ranges:
            raise HTTPException(status_code=400, detail=f"range must be one of {sorted(all_ranges)}")
        if range in INTRADAY_RANGES:
            timeframe = INTRADAY_RANGES[range]["candle_size"]
            limit = INTRADAY_RANGES[range]["limit"]
        else:
            timeframe = "1d"
            limit = RANGE_TO_TRADING_DAYS[range]
    adapter = get_adapter()
    ticker = ticker.upper()
    # Same real fix as ticker_analysis() above (2026-10-14): only the
    # demo/synthetic adapter enforces a fixed ticker universe (fixtures
    # genuinely only exist for the 11 named scenarios); Live Mode accepts
    # any format-valid ticker and lets the real UW call decide if it has
    # data, instead of hard-gating on TIP_LIVE_WATCHLIST.
    if isinstance(adapter, SyntheticDataAdapter):
        if ticker not in adapter.get_universe():
            raise HTTPException(status_code=404, detail=f"{ticker} is not one of the demo scenarios")
    else:
        try:
            ticker = _validate_ticker_for_route(ticker)
        except AiValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # `window` (Phase B final fix, 2026-10-14): UW's REAL date-window
    # selector -- our `range` strings ("1M"/"3M"/"6M"/"1Y") are already in
    # UW's own vocabulary for this parameter, so pass it straight through.
    # `limit` stays as a sane safety cap (RANGE_TO_TRADING_DAYS), not the
    # primary selector -- see live_uw_adapter.stock_ohlc()'s docstring for
    # the full bug writeup.
    ohlc = adapter.get_ohlc(ticker, timeframe=timeframe, limit=limit, window=range)
    flow_alerts = adapter.get_flow_alerts(ticker)
    darkpool_trades = adapter.get_darkpool_trades(ticker)
    markers = build_markers(flow_alerts, darkpool_trades)
    candles = ohlc.get("candles", [])
    # Phase B: added persistence layer only -- catalogs the SAME
    # already-session-classified/delta-corrected candles this route is
    # about to return, plus this call's flow/dark-pool inputs. Never
    # changes `candles`/`markers`/anything else returned below (see
    # _catalog_scan_best_effort's docstring for the invariant this
    # mirrors; failures here are swallowed, never surfaced to the
    # caller).
    if isinstance(adapter, LiveUWAdapter):
        try:
            catalog = get_catalog()
            catalog.catalog_price_bars(ticker, timeframe, candles)
            catalog.catalog_options_flow_observations(ticker, flow_alerts)
            catalog.catalog_darkpool_observations(ticker, darkpool_trades)
        except Exception:  # noqa: BLE001 -- never propagate, see module docstring
            logging.getLogger(__name__).info(
                "Phase B ingestion catalog write skipped for %s OHLC (non-fatal, response unaffected)", ticker
            )
    # Phase B (2026-10-14) honest coverage disclosure -- production-
    # inspired (its own range_fully_satisfied/available_span_days
    # pattern): never claim a range is fully covered when UW simply
    # doesn't have that much history for this ticker (recent IPO, etc.).
    # `available_span_days` is measured from the REAL returned candles,
    # not assumed from the request -- a short-history ticker is reported
    # honestly rather than silently padded or mislabeled.
    earliest_candle = candles[0]["t"] if candles else None
    latest_candle = candles[-1]["t"] if candles else None
    available_span_days = None
    range_fully_satisfied = None
    if range is not None and range in INTRADAY_RANGES:
        # Intraday coverage is "did we get a real continuous session's
        # worth of 5m bars" (live-verified ~191 for a full day), not a
        # trading-day count -- a much looser floor (a half-day/holiday
        # genuinely has fewer bars; never treat that as a bug).
        range_fully_satisfied = len(candles) >= 50
    elif range is not None:
        expected_bars = RANGE_TO_TRADING_DAYS[range]
        # Allow a small tolerance for holidays/early closes -- 90% of the
        # expected trading-day COUNT, not a calendar-day comparison,
        # since that's what was actually requested (a bar count).
        range_fully_satisfied = len(candles) >= int(expected_bars * 0.9)
    if range is not None:
        if candles:
            try:
                from datetime import datetime
                d0 = datetime.fromisoformat(str(earliest_candle)[:10])
                d1 = datetime.fromisoformat(str(latest_candle)[:10])
                available_span_days = (d1 - d0).days
            except (ValueError, TypeError):
                available_span_days = None
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "range": range,
        "candles": candles,
        "source": ohlc.get("source"),
        "negative_volume_bars_clamped": ohlc.get("negative_volume_bars_clamped", 0),
        "unavailable": ohlc.get("unavailable", False),
        "reason": ohlc.get("reason"),
        "markers": markers,
        "candle_count": len(candles),
        "earliest_candle": earliest_candle,
        "latest_candle": latest_candle,
        "range_fully_satisfied": range_fully_satisfied,
        "available_span_days": available_span_days,
        # 2026-10-15 session-handling fix: real coverage disclosure, not
        # just the final candle count -- lets a caller verify (not just
        # trust) that the raw-record-to-unique-trading-day math actually
        # worked, per Chad's requirement to show this explicitly.
        "raw_records_requested": ohlc.get("raw_records_requested"),
        "raw_records_returned": ohlc.get("raw_records_returned"),
        "regular_session_records_retained": ohlc.get("regular_session_records_retained"),
    }


@app.get("/api/investigate/availability")
def investigate_availability():
    """Tells the frontend whether to show/enable the 'Investigate with AI'
    button, and why not, without triggering any network call. Demo mode is
    always available (it only ever serves a static simulated transcript);
    live mode additionally needs both an AI provider key and a UW key."""
    # Item 5 (2026-10-15): expose the CONFIGURED transport (mcp/rest) here,
    # before any investigation runs, so the UI can tell the user plainly
    # which backend will actually execute the real tool calls -- a plain
    # transport name carries no credential and is safe to disclose. None
    # for the simulated path (no transport is used -- see simulated.py).
    data = _settings_store.get_all()
    mode = data["mode"]
    if settings.public_demo or mode != "live":
        return {"available": True, "simulated": True, "reason": None, "transport": None}
    ai_key = data.get("ai_api_key")
    if not ai_key:
        return {
            "available": False,
            "simulated": False,
            "reason": "No AI provider key is configured. Add one in Settings to enable Investigate with AI.",
            "transport": settings.investigate_transport,
        }
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        return {
            "available": False,
            "simulated": False,
            "reason": "Live mode requires a Unusual Whales API key (Settings) before Investigate with AI can run.",
            "transport": settings.investigate_transport,
        }
    return {"available": True, "simulated": False, "reason": None, "transport": settings.investigate_transport}


@app.post("/api/investigate/{ticker}", dependencies=[Depends(require_auth)])
def investigate_ticker(ticker: str):
    """Bounded 'Investigate with AI' for one ticker. Demo mode / PUBLIC_DEMO
    always returns a pre-written, clearly-labeled simulated transcript and
    NEVER calls a real LLM or UW endpoint. Live mode makes the real bounded
    MCP-allowlist-equivalent REST calls plus two LLM calls described in
    backend/app/ai/orchestrator.py, entirely from this local backend."""
    try:
        safe_ticker = _validate_ticker_for_route(ticker)
    except AiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data = _settings_store.get_all()
    mode = data["mode"]

    if settings.public_demo or mode != "live":
        return get_simulated_transcript(settings.fixtures_dir, safe_ticker)

    ai_provider = data.get("ai_provider")
    ai_key = data.get("ai_api_key")
    if not ai_key or not ai_provider:
        raise HTTPException(
            status_code=400,
            detail="No AI provider key is configured. Add one in Settings to use Investigate with AI.",
        )
    uw_key, _source = _effective_uw_key(data)
    if not uw_key:
        raise HTTPException(
            status_code=400,
            detail="Live mode requires a Unusual Whales API key (Settings) before Investigate with AI can run.",
        )

    adapter = get_adapter()
    store = get_store()
    try:
        scan = _run_scan(adapter, store, safe_ticker)
        evidence = _to_evidence_item(scan)
        evidence["qualifying_days_count"] = scan["qualifying_days_count"]
        evidence["confidence"] = scan["confidence"]
    except Exception:
        evidence = {"ticker": safe_ticker, "note": "Market Radar evidence unavailable; investigating ticker alone."}

    try:
        result = ai_orchestrator.investigate(
            ticker=safe_ticker,
            evidence=evidence,
            ai_provider=ai_provider,
            ai_api_key=ai_key,
            uw_api_key=uw_key,
            uw_base_url=settings.uw_base_url,
            uw_client_api_id=settings.uw_client_api_id,
            transport=settings.investigate_transport,
        )
    except AiValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ai_orchestrator.NoValidToolsSelectedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except MalformedAIResponse as e:
        raise HTTPException(status_code=502, detail=f"AI returned a malformed response: {e}")
    except LLMTruncatedOutputError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"AI provider response was cut off before it finished (token limit reached), not malformed: {e} "
                "Try again -- if this keeps happening, the response needed more room than the bounded limit allows."
            ),
        )
    except LLMCallError as e:
        raise HTTPException(status_code=502, detail=f"AI provider call failed: {e}")
    except MCPSubprocessError as e:
        raise HTTPException(status_code=502, detail=f"MCP transport unavailable: {e}")

    return result.model_dump()


def _validate_ticker_for_route(ticker: str):
    from .ai.validation import validate_ticker

    return validate_ticker(ticker)


@app.get("/api/scenarios")
def scenarios():
    """Demo-mode only: lists the synthetic scenarios available, so the
    frontend can show what each fixture is meant to demonstrate."""
    adapter = get_adapter()
    if isinstance(adapter, SyntheticDataAdapter):
        return {"scenarios": adapter.list_scenarios()}
    return {"scenarios": [], "note": "scenario listing only available in TIP_MODE=demo"}


# Serve the plain HTML/CSS/JS frontend at "/" when its directory is present
# (true in the Docker image; optional for local `uvicorn app.main:app` runs
# started from backend/ without the sibling frontend/ dir mounted).
_FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND_DIR.exists():
    from fastapi.responses import HTMLResponse
    import re as _re

    def _index_html_with_cache_busting() -> str:
        """REAL BUG FIX (2026-10-15, Priority 4 -- asset cache invalidation):
        there was no versioned/cache-busting mechanism for app.core.js /
        app.live.js at all -- a browser could keep serving a stale cached
        bundle after a real deploy indefinitely, requiring a user to know to
        manually hard-reload or append their own "?v=". Appends a real
        `?v=<build commit>` (falling back to the file's own mtime when no
        BUILD_COMMIT is baked in -- e.g. a non-Docker local run) to every
        same-origin <script src="..."> tag in index.html, derived from the
        SAME version.get_build_info() the Application Updates feature
        already uses, so a new deploy's baked-in commit automatically busts
        the cache with no manual ?v= needed."""
        html = (_FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
        build = _version.get_build_info()
        cache_key = build.get("commit") or str(int((_FRONTEND_DIR / "app.core.js").stat().st_mtime))

        def _bust(match: "_re.Match") -> str:
            src = match.group(1)
            if src.startswith(("http://", "https://", "//")):
                return match.group(0)  # never touch an external URL
            sep = "&" if "?" in src else "?"
            return f'src="{src}{sep}v={cache_key}"'

        return _re.sub(r'src="([^"]+\.js)"', _bust, html)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def _serve_index():
        return HTMLResponse(_index_html_with_cache_busting())

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")
