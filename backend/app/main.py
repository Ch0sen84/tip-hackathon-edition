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
from .adapters.live_uw_adapter import ERROR, UNAUTHORIZED, UNAVAILABLE, LiveUWAdapter, UWRestClient
from .adapters.synthetic_adapter import SyntheticDataAdapter
from .ai import orchestrator as ai_orchestrator
from .ai import llm_provider as ai_llm_provider
from .ai import mcp_tool_client as ai_mcp_tool_client
from .ai.llm_provider import LLMCallError, LLMTruncatedOutputError
from .ai.mcp_tool_client import MCPSubprocessError
from .ai.schemas import MalformedAIResponse
from .ai.simulated import get_simulated_transcript
from .ai.validation import ValidationError as AiValidationError
from .auth import TOKEN_HEADER, AuthTokenStore, default_token_path, is_loopback
from .config import settings
from .core import dark_pool_baseline, scan_service, tip_evidence
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
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", f"http://{_BIND_HOST}:{_BIND_PORT}")
ALLOWED_HOST = os.environ.get("ALLOWED_HOST", _BIND_HOST)

_ALLOWED_HOSTS = [ALLOWED_HOST]
# Test-only widening: fastapi.testclient.TestClient has no real network
# interface, so its requests' Host header is whatever base_url the test
# gave it. This can ONLY ever add hosts during an actual pytest run (same
# guard pattern as app/storage.py's _running_under_pytest) -- it is a
# no-op for the real deployed app (uvicorn/Docker).
if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
    _ALLOWED_HOSTS += ["testserver", "testclient"]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)

_adapter: Optional[UWDataAdapter] = None
_adapter_sig: Optional[tuple] = None
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


def require_auth(request: Request) -> None:
    """FastAPI dependency applied to every mutating (POST/PUT/DELETE)
    route under /api/ except /api/auth/bootstrap itself. Compares the
    X-TIP-Auth header against the real stored token with
    hmac.compare_digest (constant-time) -- see AuthTokenStore.matches()."""
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


def get_adapter() -> UWDataAdapter:
    global _adapter, _adapter_sig
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
            _adapter = LiveUWAdapter(
                api_key=key or "",
                base_url=settings.uw_base_url,
                client_api_id=settings.uw_client_api_id,
                watchlist=list(watchlist),
            )
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


def _price_move_pct(stock_state: Optional[dict]) -> Optional[float]:
    if not stock_state or stock_state.get("unavailable"):
        return None
    for key in ("change_percent", "chg_pct", "percent_change"):
        if stock_state.get(key) is not None:
            return float(stock_state[key])
    return None


def _catalog_scan_best_effort(adapter: UWDataAdapter, ticker: str, flow_alerts: list,
                               darkpool_trades: list, result: dict) -> None:
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
    history already simulates multi-day history for it)."""
    if not isinstance(adapter, LiveUWAdapter):
        return
    try:
        catalog = get_catalog()
        n_flow = catalog.catalog_options_flow_observations(ticker, flow_alerts)
        n_dp = catalog.catalog_darkpool_observations(ticker, darkpool_trades)
        catalog.catalog_signal_evaluation(ticker, result)
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


def _run_scan(adapter: UWDataAdapter, store: QualifyingDaysStore, ticker: str) -> dict:
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
        store.record_scan(ticker, any_gate_passed, result["state"])
    except StorageGuardPausedError as e:
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
    _catalog_scan_best_effort(adapter, ticker, flow_alerts, darkpool_trades, result)
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
    if telemetry is None:
        return {"mode": getattr(adapter, "mode", "unknown"), "by_path": {},
                "max_concurrent_requests": None, "note": "No live UW HTTP client in this mode."}
    return {
        "mode": adapter.mode,
        "by_path": telemetry.summary(),
        "max_concurrent_requests": client.max_concurrent_requests,
        "max_retries": client.max_retries,
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


@app.get("/api/radar")
def radar():
    """Market Radar: scan every ticker in the adapter's universe and return
    the ones that qualify (tip_evidence.qualifies), highest-attention first.

    Market Radar (2026-10-14, Phase A): records when this scan ran and
    how many tickers it covered (persisted -- survives container
    recreation), and explicitly discloses `scan_scope: "watchlist"` so
    the UI never implies broader-market discovery that isn't actually
    happening. UW's REST API does offer a genuine stock/options screener
    capable of real market-wide discovery (confirmed, not assumed) --
    that is NOT wired up here; this remains honest watchlist-only
    scanning until that's a separately-scoped feature."""
    adapter = get_adapter()
    store = get_store()
    universe = adapter.get_universe()
    if not universe:
        return {"highest": [], "other": [], "scanned": 0, "scan_scope": "watchlist", "last_scan_at": None}

    items = []
    scans_by_ticker = {}
    for ticker in universe:
        scan = _run_scan(adapter, store, ticker)
        scans_by_ticker[ticker] = scan
        items.append(_to_evidence_item(scan))

    highest, other = tip_evidence.present(items, next_session=date.today() + timedelta(days=1))

    def _attach(entry: dict) -> dict:
        s = scans_by_ticker[entry["ticker"]]
        # Pre-existing gap found while building the Stage 2 signal-state
        # filter: tip_evidence.present()'s entry dict has never carried a
        # "state" key at all (it only tracks state internally for
        # ordering/STATE_RANK) -- frontend/app.core.js's renderCard()/
        # renderRow() have always rendered `state-${entry.state}` against
        # an undefined value, so the state tag on every Market Radar
        # card/row has always been visually blank. Fixing here (real data
        # already computed in `scan`, just never attached) since the new
        # client-side state filter needs a real field to filter on.
        entry["state"] = s["state"]
        entry["confidence"] = s["confidence"]
        entry["signal_type"] = s["signal_type"]
        entry["qualifying_days_count"] = s["qualifying_days_count"]
        # Compact signal-history indicator (Stage 2 / P3): only non-empty
        # when this ticker genuinely has more than one real recorded scan
        # (store.history() -- never fabricated). On a freshly started
        # process that's only PRSX (seeded + one live scan = 3 real
        # entries); every other ticker stays [] until it's actually been
        # scanned more than once. The frontend only renders a chip when
        # this list has 2+ entries.
        hist = store.history(entry["ticker"])
        entry["history_states"] = [h["state"] for h in hist] if len(hist) > 1 else []
        return entry

    scan_record = _settings_store.record_scan(len(universe))
    return {
        "highest": [_attach(e) for e in highest],
        "other": [_attach(e) for e in other],
        "scanned": len(universe),
        # Honest disclosure (2026-10-14, Phase A): this is a scan of the
        # user's configured watchlist, never broader-market discovery --
        # see this function's docstring for why that distinction matters.
        "scan_scope": "watchlist",
        "last_scan_at": scan_record["last_scan_at"],
    }


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


def _coerce_number(v):
    """UW's live screener sometimes returns numeric fields as strings
    (confirmed from Chad's real Live Mode test) -- never trust a raw
    value's Python type. Returns a float, or None when the value is
    missing/unparseable (never a guessed 0)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _normalize_discovery_candidate(raw: dict) -> dict:
    """Normalizes ONE raw /api/screener/stocks row into the shape this
    app's ranking/frontend rely on. Fixes two real bugs found in Chad's
    live-mode test:
      * UW's real per-day percent-change field is `one_day_perc`, not
        `change` (this app previously read a field UW doesn't send,
        which is why change always rendered n/a in live mode). `change`
        is kept as a defensive fallback only, in case an older/synthetic
        shape still uses it.
      * UW returns some numeric fields (net_premium, call/put volume,
        iv_rank, repeated_hits) as strings on at least some responses --
        coerced to float here rather than left to break arithmetic later.
    Also carries through `is_index` (real screener rows can be index
    instruments, e.g. SPXW) and UW's own per-row snapshot `date` (renamed
    `source_date` so it's never confused with this app's OWN fetch
    timestamp, `snapshot_at`, set by discover_candidates() -- see Chad's
    item #8). Never invents a field the row didn't actually have."""
    out = dict(raw)
    out["ticker"] = raw.get("ticker") or raw.get("symbol")
    out["net_premium"] = _coerce_number(raw.get("net_premium"))
    out["call_volume"] = _coerce_number(raw.get("call_volume"))
    out["put_volume"] = _coerce_number(raw.get("put_volume"))
    out["iv_rank"] = _coerce_number(raw.get("iv_rank"))
    out["repeated_hits"] = _coerce_number(raw.get("repeated_hits"))
    change_pct = raw.get("one_day_perc")
    if change_pct is None:
        change_pct = raw.get("change")  # defensive fallback only -- one_day_perc is the real field
    out["change_percent"] = _coerce_number(change_pct)
    out["is_index"] = bool(raw.get("is_index")) if raw.get("is_index") is not None else False
    out["source_date"] = raw.get("date")
    return out


def _rank_discovery_candidates(candidates: list) -> list:
    """Rank by |net_premium| magnitude primarily, repeated_hits as a
    tiebreaker -- a sensible, documented, non-mysterious ranking. Never
    alters the raw candidate dicts; only reorders them. Candidates are
    expected to already be normalized (see _normalize_discovery_candidate)
    so net_premium/repeated_hits are real floats, never a raw string."""
    def _key(c: dict):
        return (abs(float(c.get("net_premium") or 0)), float(c.get("repeated_hits") or 0))
    return sorted(candidates, key=_key, reverse=True)


@app.post("/api/radar/discover", dependencies=[Depends(require_auth)])
def radar_discover(body: DiscoverFiltersBody):
    """Market Discovery (C2b/C2c): EXACTLY ONE bounded call to the
    adapter's discover_candidates per invocation -- no automatic
    pagination, no automatic per-ticker follow-up scan. Returns raw
    screener 'candidates' (state = zero gate evaluation has happened),
    ranked, plus a snapshot timestamp and an explicit quota-cost
    disclosure (screener_requests_used). A candidate only becomes
    'evaluated' when the user explicitly calls GET /api/ticker/{ticker}
    (the 'Evaluate in TIP' action) -- never automatically here."""
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
    result = adapter.discover_candidates(filters)
    normalized = [_normalize_discovery_candidate(c) for c in (result.get("candidates") or [])]
    candidates = _rank_discovery_candidates(normalized)
    for c in candidates:
        # Explicit state model (item 1c of the Market Discovery spec):
        # a raw screener row is ALWAYS "candidate" here -- never
        # "evaluating"/"evaluated"/a signal_engine state. Those only
        # ever come from a real GET /api/ticker/{ticker} call.
        c["discovery_state"] = "candidate"
        # Chad's item #10: a real screener row can be an index instrument
        # (observed: SPXW with is_index=true) -- the per-ticker
        # scan_service/signal_engine pipeline this app's "Evaluate in TIP"
        # reuses is built and tested against equities (flow-alert/dark-
        # pool/stock-state calls keyed by a stock ticker), never verified
        # against an index/derivative symbol. Rather than silently offer
        # an action that may error or produce meaningless gates for an
        # unsupported instrument type, mark it explicitly so the frontend
        # can disable Evaluate for it instead of guessing it's safe.
        c["evaluate_supported"] = not c.get("is_index")
    return {
        "candidates": candidates,
        "count": len(candidates),
        "snapshot_at": result.get("snapshot_at"),
        "screener_requests_used": result.get("screener_requests_used", 1),
        "source": result.get("source"),
        "unavailable": result.get("unavailable", False),
        "reason": result.get("reason"),
        "scan_scope": "market_wide_screener",
        "limit_applied": body.limit,
        "limit_cap": 50,
    }


@app.get("/api/ticker/{ticker}")
def ticker_analysis(ticker: str):
    """Ticker Analysis: full evidence drill-down for one ticker -- gates,
    confidence, state transition, and the tip_evidence explanation.

    REAL BUG FIX (2026-10-14): this used to reject ANY ticker not already
    in adapter.get_universe() -- for LiveUWAdapter that's exactly
    TIP_LIVE_WATCHLIST (a short fixed list), so a live user could only
    ever analyze whatever 4 tickers happened to be configured there, with
    no way to type in a different real ticker. That watchlist exists to
    scope Market Radar's proactive scanning, not to gate direct lookup --
    UW's REST API will answer for any real ticker. Fix: the fixed-universe
    check now applies ONLY to the synthetic/demo adapter (where it's
    correct -- fixtures genuinely only exist for the 11 named scenarios);
    Live Mode instead validates the SYMBOL FORMAT (same TICKER_RE already
    used for AI Investigation) and lets the real UW call itself be the
    source of truth on whether that ticker actually exists/has data."""
    adapter = get_adapter()
    store = get_store()
    ticker = ticker.upper()
    if isinstance(adapter, SyntheticDataAdapter):
        if ticker not in adapter.get_universe():
            raise HTTPException(status_code=404, detail=f"{ticker} is not one of the demo scenarios")
    else:
        try:
            ticker = _validate_ticker_for_route(ticker)
        except AiValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

    scan = _run_scan(adapter, store, ticker)
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
    }


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
