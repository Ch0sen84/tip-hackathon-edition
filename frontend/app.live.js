// Thin adapter for the backend-connected local/Docker edition. Owns all
// fetch() calls, state-mutating button handlers, and the page-load
// bootstrap sequence. Depends on app.core.js being loaded first (rendering
// lives there, not here). API_BASE lets ?api=http://host:port override the
// same-origin default for local dev when frontend is served separately.
const API_BASE = new URLSearchParams(location.search).get("api") || "";

// --- Phase A: real browser-usable auth token handling -----------------
//
// The backend requires a matching X-TIP-Auth header on every mutating
// (POST/PUT/DELETE) /api/ route (see backend/app/main.py's require_auth
// dependency) -- this is the ONE choke point (apiPost/apiPut/apiDelete
// below) that attaches it, rather than patching every individual call
// site. The token itself is never embedded in this source; it is
// fetched once (GET /api/auth/bootstrap, which only succeeds for a
// same-machine/localhost caller) or pasted in manually by the user for a
// LAN/remote deployment, then cached in localStorage -- see
// ensureAuthToken()/wireAuthSetupBanner() below.
const AUTH_TOKEN_KEY = "tip_auth_token";

function getAuthToken() {
  try {
    return localStorage.getItem(AUTH_TOKEN_KEY);
  } catch (e) {
    return null; // localStorage unavailable (e.g. private-mode edge case) -- degrade, don't crash
  }
}

function setAuthToken(token) {
  try {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } catch (e) {
    /* best-effort only */
  }
}

function clearAuthToken() {
  try {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch (e) {
    /* best-effort only */
  }
}

function _authHeaders(extra) {
  const token = getAuthToken();
  return token ? { ...extra, "X-TIP-Auth": token } : { ...extra };
}

// Shows the Phase A setup/re-auth banner (app.core.js's
// renderAuthSetupBanner) and wires its button/fallback-paste controls.
// Safe to call repeatedly (e.g. once on load, again on every 401) --
// always re-renders from scratch, never stacks duplicate banners.
function showAuthSetupBanner(variant) {
  const el = document.getElementById("authSetupBanner");
  if (!el) return;
  el.style.display = "";
  el.innerHTML = renderAuthSetupBanner(variant);
  const bootstrapBtn = document.getElementById("authBootstrapBtn");
  const statusEl = document.getElementById("authBootstrapStatus");
  if (bootstrapBtn) {
    bootstrapBtn.addEventListener("click", async () => {
      statusEl.textContent = "Requesting token...";
      const ok = await tryAutoBootstrapToken();
      if (ok) {
        statusEl.textContent = "";
        hideAuthSetupBanner();
        await onAuthRestored();
      } else {
        // QA remediation D6: a CLEAR, actionable status explaining what
        // happened and what to do next -- re-rendering the banner with
        // the "bootstrap_failed" variant (see renderAuthSetupBanner),
        // not just this one status line. This is the real symptom D1
        // fixes: a Docker Desktop browser's auto-bootstrap reliably
        // fails here, and the pairing-code box below is the fix.
        showAuthSetupBanner("bootstrap_failed");
      }
    });
  }
  // QA remediation D1: the pairing-code submission flow (the new,
  // fully-secure fallback for exactly the Docker Desktop case the
  // button above cannot auto-resolve) -- POST /api/auth/pair, same
  // success handling (hide banner + reload whatever view was stuck on
  // an auth error) as every other successful-auth path below.
  const pairingInput = document.getElementById("authPairingCodeInput");
  const pairingBtn = document.getElementById("authPairingSubmitBtn");
  const pairingStatusEl = document.getElementById("authPairingStatus");
  if (pairingBtn && pairingInput) {
    pairingBtn.addEventListener("click", async () => {
      const code = (pairingInput.value || "").trim();
      if (!code) return;
      pairingStatusEl.textContent = "Pairing...";
      try {
        const res = await fetch(`${API_BASE}/api/auth/pair`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pairing_code: code }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.token) {
          pairingStatusEl.textContent =
            data.detail || "Invalid or expired pairing code -- check `docker logs <container>` for the current one.";
          return;
        }
        setAuthToken(data.token);
        pairingStatusEl.textContent = "";
        hideAuthSetupBanner();
        await onAuthRestored();
      } catch (e) {
        pairingStatusEl.textContent = "Could not reach the backend to pair this browser.";
      }
    });
  }
  // QA remediation D5: masked by default (the input itself is
  // type="password" in the markup); this toggle is the ONLY way its
  // value is ever shown in plaintext, and only in response to an
  // explicit user click -- never automatically, never logged.
  const manualInput = document.getElementById("authManualTokenInput");
  const revealBtn = document.getElementById("authManualTokenRevealBtn");
  if (revealBtn && manualInput) {
    revealBtn.addEventListener("click", () => {
      const showing = manualInput.type === "text";
      manualInput.type = showing ? "password" : "text";
      revealBtn.textContent = showing ? "Show" : "Hide";
    });
  }
  const manualSaveBtn = document.getElementById("authManualTokenSaveBtn");
  if (manualSaveBtn && manualInput) {
    manualSaveBtn.addEventListener("click", async () => {
      const pasted = (manualInput.value || "").trim();
      if (!pasted) return;
      setAuthToken(pasted);
      hideAuthSetupBanner();
      await onAuthRestored();
    });
  }
}

// QA remediation D4: after ANY successful token acquisition (auto-
// bootstrap, D1's pairing flow, or a manually-pasted token), re-fetch
// whatever view was previously stuck showing a stale auth-error state
// -- without a full page reload. Market-wide Radar is the view D4's
// report specifically names (it is require_auth-gated end to end, so
// it is the one most likely to be showing a stuck 401 from before the
// token existed at all); best-effort re-fetch of Market Radar too,
// since it can independently 401 on the watchlist-scan-count write.
// Every call here is wrapped so one view's failure never blocks
// another's retry, and never throws back into the caller (a banner
// click handler).
async function onAuthRestored() {
  const tasks = [];
  if (document.getElementById("marketWideCandidateList")) {
    tasks.push(loadMarketWideRadar());
  }
  if (document.getElementById("radarStatus") && TIP_STATE.currentView === "radar") {
    tasks.push(loadRadar());
  }
  await Promise.allSettled(tasks);
}

function hideAuthSetupBanner() {
  const el = document.getElementById("authSetupBanner");
  if (el) {
    el.style.display = "none";
    el.innerHTML = "";
  }
}

// Only ever succeeds for a browser running on the SAME machine as the
// backend (the server checks request.client.host, not anything this
// function sends) -- works automatically for the common localhost/
// Docker-Desktop case with zero manual steps. Returns false (never
// throws) for a LAN/remote deployment, where the fallback paste-box is
// the documented path instead.
async function tryAutoBootstrapToken() {
  try {
    const res = await fetch(`${API_BASE}/api/auth/bootstrap`);
    if (!res.ok) return false;
    const data = await res.json();
    if (!data.token) return false;
    setAuthToken(data.token);
    return true;
  } catch (e) {
    return false;
  }
}

// Called once on page load (see bootstrap() at the bottom of this file).
// If a token is already cached, this is a silent no-op. Otherwise it
// tries the automatic same-machine bootstrap; if that fails (non-
// localhost deployment), it shows the one-time setup banner instead of
// silently leaving every mutating action broken.
async function ensureAuthToken() {
  if (getAuthToken()) return true;
  const ok = await tryAutoBootstrapToken();
  // QA remediation D6: distinguish "never tried yet" from "auto-bootstrap
  // was actually attempted and failed" -- the latter gets the specific,
  // actionable "bootstrap_failed" banner (pairing-code instructions),
  // not the generic first-run copy, so a genuine Docker Desktop user
  // sees exactly what happened and what to do next on first page load,
  // not only after clicking the button themselves.
  if (!ok) showAuthSetupBanner("bootstrap_failed");
  return ok;
}

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// Phase D: an authed-GET variant for the new require_auth-protected
// read-only routes (/api/scheduler/status, /api/radar/market-wide) --
// same 401 handling as apiPost/apiPut/apiDelete below (stale/missing
// token clears + re-shows the setup banner), just for GET.
async function apiGetAuthed(path) {
  const res = await fetch(`${API_BASE}${path}`, { headers: _authHeaders({}) });
  if (res.status === 401) _throwAuthError(path);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// NOTE on credential handling: these helpers submit a key ONCE to the
// backend to save it. The backend never echoes the real value back --
// only a `configured` boolean and the key's last 4 characters. Nothing
// here (or anywhere in this file) writes a UW/AI key to localStorage,
// sessionStorage, or a cookie -- ONLY the app's own X-TIP-Auth access
// token (see AUTH_TOKEN_KEY above) is ever cached client-side, and it
// carries no UW/AI credential of any kind.
//
// Phase A 401 handling: a 401 here means the saved X-TIP-Auth token is
// missing or no longer valid server-side (e.g. a container recreation
// regenerated the token file) -- NOT a generic HTTP-status message. The
// stale token is cleared and the setup banner is re-shown so the user has
// an immediate, obvious way to re-authenticate, and the thrown error says
// so explicitly rather than just "HTTP 401".
function _throwAuthError(path) {
  clearAuthToken();
  showAuthSetupBanner("error");
  throw new Error(
    `Authentication required for ${path}: your access token is missing or no longer valid. ` +
      `Use the "Set up access" banner above to re-authenticate.`
  );
}

async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: _authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) _throwAuthError(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> HTTP ${res.status}`);
  return data;
}

async function apiPut(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: _authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) _throwAuthError(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> HTTP ${res.status}`);
  return data;
}

async function apiDelete(path) {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE", headers: _authHeaders({}) });
  if (res.status === 401) _throwAuthError(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> HTTP ${res.status}`);
  return data;
}

let _lastRadarData = null;
// Tracks the backend's currently-active mode (demo/live) so the
// Investigate workspace can ALWAYS show it prominently (P6 requirement:
// never leave this only inferable from the page header) -- set once per
// loadSettings() call, which already fetches it from /api/settings.
let _currentMode = "demo";
// Tracks every currently-selectable ticker (Radar-qualifying + full
// scenario universe), rebuilt each loadRadar() call. Exposed via
// window.TIP_NAV_HANDLERS.getKnownTickers() so app.core.js's
// applyNavState() can validate a URL-supplied ticker without this file
// needing to know anything about history/popstate itself.
let _knownTickers = [];

async function loadRadar() {
  const statusEl = document.getElementById("radarStatus");
  statusEl.textContent = "Loading Market Radar...";
  try {
    const data = await apiGet("/api/radar");
    _lastRadarData = data;
    // Honest scan-scope + freshness disclosure (2026-10-14, Phase A):
    // never implies broader-market discovery that isn't happening, and
    // states plainly when this scan actually ran (persisted server-side,
    // survives container recreation -- see settings_store.record_scan()).
    const scopeLabel = data.scan_scope === "watchlist" ? "watchlist scan" : data.scan_scope || "scan";
    statusEl.textContent =
      `${scopeLabel}: ${data.scanned} ticker(s) · ${data.highest.length} highest attention · ` +
      `${data.other.length} other · last scanned ${formatAgo(data.last_scan_at)}`;
    document.getElementById("activityStrip").innerHTML = renderActivitySummary(data);

    // 2026-10-15 fix: renderFeaturedArea() (app.core.js) now owns the
    // Featured Signal heading + hero/breakdown grid + compact-notice
    // decision for all three real states (genuine featured ticker /
    // Highest-Attention-empty-but-Other-Noteworthy-has-tickers / both
    // empty) through the one shared, test-covered function -- replaces
    // the previous inline logic that only separately fixed the
    // both-empty case.
    renderFeaturedArea(data);

    // Stage 2 (P3): filters + cards + counts + history chips, all
    // rendered from this same already-fetched payload -- see
    // app.core.js's setupRadarFiltersAndRender/renderRadarLists. This
    // also populates #highestGrid/#otherGrid/#otherTable and wires the
    // Investigate buttons + breakdown-click handlers (replaces the old
    // inline logic that used to live here).
    setupRadarFiltersAndRender(data, loadTickerDetail);

    // Ticker Analysis's <select> must offer EVERY known scenario ticker,
    // not just the ones that happen to qualify for Radar display today --
    // /api/radar's highest+other only ever contains Radar-qualifying
    // tickers (by design, see tip_evidence.qualifies), so a ticker like
    // DRFT ("Fading signal") or NADA ("Quiet control") that legitimately
    // has zero qualifying gates right now would otherwise be completely
    // unreachable from this view. /api/scenarios lists all known tickers
    // regardless of qualification; merge it in, deduped, so they stay
    // selectable and loadable via /api/ticker/{t} (which already works
    // for any ticker in the adapter's universe).
    // REAL BUG FIX (2026-10-15, Chad's live-mode item #13): /api/scenarios
    // is explicitly demo-mode-only server-side (see its docstring in
    // main.py) -- calling it in Live Mode was a pointless extra request
    // every single Radar load, always resulting in nothing useful (no
    // demo scenarios exist to merge in while running against the real
    // UW API). Skipped entirely when _currentMode is "live"; Demo Mode
    // behavior is completely unchanged.
    let scenarioTickers = [];
    if (_currentMode !== "live") {
      try {
        const scenariosResp = await apiGet("/api/scenarios");
        scenarioTickers = (scenariosResp.scenarios || []).map((s) => s.ticker);
      } catch (e) {
        /* non-fatal: select falls back to Radar-qualifying tickers only */
      }
    }
    const radarTickers = [...data.highest, ...data.other].map((e) => e.ticker);
    const allTickers = Array.from(new Set([...radarTickers, ...scenarioTickers])).sort();
    _knownTickers = allTickers;
    setTickerComboOptions(allTickers);
  } catch (err) {
    statusEl.textContent = `Error loading Market Radar: ${err.message}`;
  }
}

// Request-token/sequence-number guard: if the user switches ticker or
// mode while a previous /api/ticker/{t} or /api/investigate/{t} request is
// still in flight, the late-arriving response for the OLD ticker/mode
// must never overwrite the UI once the user has moved on. _navSeq is
// incremented on every navigation (loadTickerDetail call); each in-flight
// request captures its own sequence number in closure and checks it
// against the CURRENT _navSeq right before writing to the DOM -- if they
// no longer match, a newer navigation has superseded this one and the
// response is silently dropped instead of clobbering newer state.
let _navSeq = 0;

// Phase B (2026-10-14) chart timeframe persistence -- production-
// inspired (its own sessionStorage "tip_eq_range" pattern): survives
// ticker switches AND a page refresh (sessionStorage, not a JS
// variable), but intentionally NOT across browser restarts/new tabs
// (sessionStorage's actual scope) -- matches production's own choice.
const CHART_RANGE_KEY = "tip_chart_range";
function getChartRange() {
  try { return sessionStorage.getItem(CHART_RANGE_KEY) || "3M"; } catch (e) { return "3M"; }
}
function setChartRange(range) {
  try { sessionStorage.setItem(CHART_RANGE_KEY, range); } catch (e) { /* storage unavailable -- just won't persist, not fatal */ }
}

// loadOhlcChart: the REAL per-range fetch (not a zoom) -- GET
// /api/ticker/{t}/ohlc?range=X returns a different `limit`-sized set of
// candles per range (see backend RANGE_TO_TRADING_DAYS), and honestly
// discloses when UW doesn't have that much real history for this
// ticker (range_fully_satisfied/available_span_days) instead of ever
// implying full coverage it doesn't have.
async function loadOhlcChart(ticker, range, mySeq) {
  const noteEl = document.getElementById("chartRangeNote");
  try {
    const ohlc = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}/ohlc?range=${encodeURIComponent(range)}`);
    if (mySeq !== _navSeq) return;
    mountCandlestickChart("ohlcChartContainer", ohlc);
    if (!noteEl) return;
    const unit = range === "1D" ? "5-minute bar(s)" : "trading day(s)";
    // 2026-10-15 (Chad's item #4): real raw-record disclosure, not just
    // the final candle count -- lets you verify (not just trust) the
    // raw-over-fetch/session-selection math on screen.
    const rawNote = (ohlc.raw_records_requested != null)
      ? ` [raw: ${ohlc.raw_records_returned}/${ohlc.raw_records_requested} requested, ` +
        `${ohlc.regular_session_records_retained} retained]`
      : "";
    if (ohlc.unavailable || !ohlc.candle_count) {
      noteEl.textContent = "";
    } else if (ohlc.range_fully_satisfied === false) {
      noteEl.textContent =
        `${range} requested \u00b7 only ${ohlc.candle_count} ${unit} of real history available` +
        `${range !== "1D" && ohlc.available_span_days != null ? ` (~${ohlc.available_span_days} calendar days)` : ""} -- ` +
        `showing everything UW has for ${esc(ticker)}, not padded to the full requested range.${rawNote}`;
    } else {
      const negNote = ohlc.negative_volume_bars_clamped
        ? ` \u00b7 ${ohlc.negative_volume_bars_clamped} bar(s) had an upstream negative-volume artifact (clamped to 0 for display)`
        : "";
      const kind = range === "1D" ? "5-minute candles (pre/regular/post-market)" : "daily candles";
      noteEl.textContent = `${range} \u00b7 ${ohlc.candle_count} ${kind}${negNote}${rawNote}`;
    }
  } catch (chartErr) {
    if (mySeq !== _navSeq) return;
    mountCandlestickChart("ohlcChartContainer", { unavailable: true, reason: { message: chartErr.message } });
    if (noteEl) noteEl.textContent = "";
  }
}

async function loadTickerDetail(ticker) {
  const mySeq = ++_navSeq;
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)}...</div>`;
  try {
    const d = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}`);
    if (mySeq !== _navSeq) return; // superseded by a newer ticker/mode switch
    el.innerHTML = renderTickerDetail(d);
    const range = getChartRange();
    const rangeRow = document.getElementById("chartRangeRow");
    if (rangeRow) {
      rangeRow.innerHTML = renderChartRangeRow(range);
      wireChartRangeRow("chartRangeRow", (newRange) => {
        setChartRange(newRange);
        loadOhlcChart(ticker, newRange, mySeq);
      });
    }
    await loadOhlcChart(ticker, range, mySeq);
    await loadInvestigateSection(ticker, mySeq);
  } catch (err) {
    if (mySeq !== _navSeq) return;
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
}

// modeLabelFor: builds the "active mode" string shown prominently inside
// the Investigate workspace itself (P6: must never be inferred only from
// the page header). Combines the real backend mode (_currentMode, set by
// loadSettings) with whether THIS investigation will be/was simulated
// (avail.simulated / d.simulated) -- both real, fetched flags, nothing
// invented.
// confirmed: true when `transport` reflects an investigation that has
// ACTUALLY RUN (d.transport, post-execution -- the default, for every
// existing post-run call site). false when `transport` is merely the
// CONFIGURED value from the pre-run availability check (avail.transport)
// -- 2026-10-15 fix: these are two different facts ("this is what's
// configured to run" vs "this is what actually ran") and must say so
// distinctly rather than both using the same unqualified phrasing.
function modeLabelFor(simulated, transport, confirmed = true) {
  const modePart = _currentMode === "live" ? "LIVE mode" : "DEMO mode";
  // REAL BUG FIX (2026-10-15, Priority 3): this used to hardcode "live AI +
  // MCP calls" for every non-simulated investigation, regardless of which
  // transport actually ran (INVESTIGATE_TRANSPORT defaults to "rest" --
  // plain REST calls over the same allowlisted endpoints, NOT genuine MCP
  // protocol -- see app.ai.mcp_tool_client/allowlist.py). The backend
  // already discloses the real transport it used (result.transport); use
  // the SAME transportLabel() helper the Investigate result panel already
  // uses, so this summary line can never claim MCP ran when REST did.
  // transport is unknown before a real investigation has actually run
  // (e.g. the pre-run availability check) -- say so plainly rather than
  // calling transportLabel(undefined), which would print a confusing
  // "Unknown (older response or unset)" before anything has even started.
  const transportPhrase = transport
    ? (confirmed
        ? `live AI + ${transportLabel(transport).toLowerCase()}`
        : `live AI + configured transport: ${transportLabel(transport).toLowerCase()}`)
    : null;
  const aiPart = simulated
    ? "simulated AI transcript"
    : transportPhrase || "live AI + UW tool calls (transport confirmed once the investigation runs)";
  return `${modePart} \u2014 ${aiPart}`;
}

async function runInvestigate(ticker, navSeq, modeLabel) {
  const mySeq = navSeq != null ? navSeq : _navSeq;
  const body = document.getElementById("investigateBody");
  // Per explicit instruction: prefer accurate progress states over a fake
  // multi-second "thinking" animation when the result (simulated or
  // cached) is already fully captured -- this loading line is honest
  // about what's actually happening (a real bounded network call that can
  // take real seconds), not a staged show.
  body.innerHTML = `<div class="empty">Investigating ${esc(ticker)}... this makes a few bounded read-only calls and can take several seconds.</div>`;
  try {
    // Phase A: routed through the shared apiPost() choke point (not a
    // standalone fetch) so this paid, state-changing call gets the same
    // X-TIP-Auth header + 401 handling as every other mutating call --
    // see apiPost's docstring above.
    const d = await apiPost(`/api/investigate/${encodeURIComponent(ticker)}`, {});
    if (mySeq !== _navSeq) return; // user moved on before this resolved -- never render a stale ticker's result
    body.innerHTML = renderInvestigateResult(d, ticker, modeLabel || modeLabelFor(d.simulated, d.transport));
  } catch (err) {
    if (mySeq !== _navSeq) return;
    body.innerHTML = `<div class="empty">Investigation failed: ${esc(err.message)}</div>`;
  }
}

// Item 5 (2026-10-15): a confirmation step before the paid "Investigate
// with AI" action actually fires -- a real two-click confirm, not just a
// disabled-state check, and NOT a once-per-session dismissal: every single
// click sequence starts back at the unconfirmed state. First click only
// arms the button (changes its own label/state); the real fetch only
// happens on the SECOND click while still armed. Any other UI action that
// re-renders this card (a fresh ticker load, a mode switch) naturally
// resets this by re-running loadInvestigateSection from scratch -- there is
// no persisted "already confirmed" flag anywhere.
function wireInvestigateConfirm(ticker, navSeq, modeLabel) {
  const btn = document.getElementById("runInvestigateBtn");
  if (!btn) return;
  const originalLabel = btn.textContent;
  let armed = false;
  const disarm = () => {
    armed = false;
    btn.textContent = originalLabel;
    btn.classList.remove("btn-confirm-armed");
  };
  btn.addEventListener("click", () => {
    if (!armed) {
      armed = true;
      btn.textContent = `Confirm: this makes a real call (click again to run)`;
      btn.classList.add("btn-confirm-armed");
      return;
    }
    disarm();
    runInvestigate(ticker, navSeq, modeLabel);
  });
}

async function loadInvestigateSection(ticker, navSeq) {
  const el = document.getElementById("investigateSection");
  try {
    const avail = await apiGet("/api/investigate/availability");
    if (navSeq != null && navSeq !== _navSeq) return; // superseded mid-fetch -- never render for a ticker the user has since left
    // 2026-10-15 fix: avail.transport already carries the CONFIGURED
    // transport (mcp/rest) from the same availability check used two
    // lines below for transportNote -- pass it through here too so the
    // mode-label line never falls back to the vague "transport confirmed
    // once the investigation runs" wording when the configured transport
    // is already known before anything has run. Reusing existing
    // availability data, no new backend field.
    const modeLabel = modeLabelFor(avail.simulated, avail.transport, false);
    if (!avail.available) {
      el.innerHTML = `
        <div class="ai-card">
          <h3>Investigate with AI</h3>
          ${renderInvestigateHeader(ticker, modeLabel)}
          <p class="ai-disabled-note">${esc(avail.reason || "This feature is not available right now.")}</p>
        </div>`;
      return;
    }
    // Item 5: disclose the CONFIGURED transport before anything runs, so
    // the user knows whether genuine MCP or the REST fallback will
    // actually execute the real UW tool calls -- avail.transport carries
    // no credential, just the "mcp"/"rest" config switch (main.py).
    const transportNote = avail.simulated
      ? ""
      : `<p class="ai-disabled-note">Configured transport: <strong>${esc(transportLabel(avail.transport))}</strong></p>`;
    const simNote = avail.simulated
      ? `<p class="ai-disabled-note">Demo mode: running this will show a pre-written simulated transcript, not a real AI/UW call.</p>`
      : `<p class="ai-disabled-note">Live mode: this will make a few bounded, read-only Unusual Whales lookups plus two AI calls using your saved keys, entirely from your local backend. A real end-to-end AI-directed investigation over genuine MCP tool calls has been independently verified (a real NVDA run, mcp transport) -- see README for the verification writeup.</p>`;
    el.innerHTML = `
      <div class="ai-card">
        <h3>Investigate with AI</h3>
        ${renderInvestigateHeader(ticker, modeLabel)}
        ${simNote}
        ${transportNote}
        <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI</button>
        <div id="investigateBody" style="margin-top:10px"></div>
      </div>`;
    wireInvestigateConfirm(ticker, navSeq, modeLabel);
  } catch (err) {
    el.innerHTML = `<div class="ai-card"><h3>Investigate with AI</h3><p class="ai-disabled-note">Unavailable: ${esc(err.message)}</p></div>`;
  }
}

// --- Market Discovery (C2b/C2c, 2026-10-15) --------------------------------
//
// Owns the ONE POST /api/radar/discover call per Scan click, and the
// per-candidate "Evaluate in TIP" action, which re-uses the EXISTING
// per-ticker pipeline (GET /api/ticker/{ticker}, the exact same call
// loadTickerDetail already makes for My Watchlist) -- never a parallel
// evaluation pipeline. Tracks each candidate's local UI state
// (candidate -> evaluating -> evaluated) purely for display; the
// backend never persists a "discovery evaluation" concept separately
// from a normal ticker scan.
let _lastDiscoveryData = null;
const _discoveryStates = {}; // ticker -> "candidate" | "evaluating" | "evaluated"

function renderDiscoveryFilterFormOnce() {
  const el = document.getElementById("discoveryFilterForm");
  if (!el || el.dataset.wired === "1") return;
  el.innerHTML = renderDiscoveryFilterForm(20);
  el.dataset.wired = "1";
  document.getElementById("discoveryFilterFormEl").addEventListener("submit", (ev) => {
    ev.preventDefault();
    runDiscoveryScan();
  });
  // Intentional, legible "no scan run yet" state -- never auto-runs a
  // scan and never fills this with fabricated example rows; it stays
  // exactly this until the user explicitly clicks Scan (see P3 scope).
  const listEl = document.getElementById("discoveryCandidateList");
  if (listEl && !_lastDiscoveryData) listEl.innerHTML = renderDiscoveryEmptyState();
}

async function runDiscoveryScan() {
  const statusEl = document.getElementById("discoveryStatus");
  const listEl = document.getElementById("discoveryCandidateList");
  const filters = readDiscoveryFilterForm();
  statusEl.textContent = "Scanning (1 bounded screener request)...";
  try {
    // Exactly ONE backend call per Scan click -- no polling, no
    // automatic retry loop, no per-candidate follow-up triggered here.
    const data = await apiPost("/api/radar/discover", filters);
    _lastDiscoveryData = data;
    data.candidates.forEach((c) => { _discoveryStates[c.ticker] = "candidate"; });
    // Priority 5 fix (2026-10-15 round 3): never phrase a synthetic-fixture
    // Demo Mode read as if it consumed real UW screener quota -- see the
    // matching fix in renderDiscoveryCandidateList() for the same bug in
    // the per-scan summary line below the table.
    const isSyntheticSource = data.source === "synthetic_discovery_fixture";
    statusEl.textContent = data.unavailable
      ? `Screener unavailable: ${(data.reason && data.reason.message) || "see Settings/UW key"}`
      : isSyntheticSource
        ? `Scan complete (synthetic demo data, no real UW API call) \u00b7 snapshot ${data.snapshot_at || "n/a"}`
        : `Scan complete \u2014 used ${data.screener_requests_used} screener request(s) \u00b7 snapshot ${data.snapshot_at || "n/a"}`;
    listEl.innerHTML = renderDiscoveryCandidateList(data.candidates, data);
    wireDiscoveryEvaluateButtons();
  } catch (err) {
    statusEl.textContent = `Scan failed: ${err.message}`;
    listEl.innerHTML = "";
  }
}

function wireDiscoveryEvaluateButtons() {
  document.querySelectorAll("[data-evaluate-ticker]").forEach((btn) => {
    btn.addEventListener("click", () => evaluateDiscoveryCandidate(btn.dataset.evaluateTicker));
  });
}

function rerenderDiscoveryList() {
  const listEl = document.getElementById("discoveryCandidateList");
  if (!listEl || !_lastDiscoveryData) return;
  const candidates = _lastDiscoveryData.candidates.map((c) => ({ ...c, discovery_state: _discoveryStates[c.ticker] || "candidate" }));
  listEl.innerHTML = renderDiscoveryCandidateList(candidates, _lastDiscoveryData);
  wireDiscoveryEvaluateButtons();
}

// "Evaluate in TIP": transitions ONE candidate's displayed state
// candidate -> evaluating -> evaluated, calling the SAME GET
// /api/ticker/{ticker} endpoint (via loadTickerDetail) that My
// Watchlist's Investigate/Analyze flow already uses -- this is the real
// scan_service/signal_engine pipeline, not a second implementation of
// it. On success, navigates to Ticker Analysis so the user sees the
// real computed gates/state immediately.
async function evaluateDiscoveryCandidate(ticker) {
  _discoveryStates[ticker] = "evaluating";
  rerenderDiscoveryList();
  try {
    await navigateToTicker(ticker, loadTickerDetail);
    _discoveryStates[ticker] = "evaluated";
  } catch (err) {
    _discoveryStates[ticker] = "candidate";
    alert(`Evaluation failed for ${ticker}: ${err.message}`);
  }
  rerenderDiscoveryList();
}

function initDiscoveryMode() {
  renderDiscoveryFilterFormOnce();
}

wireRadarModeToggle(initDiscoveryMode);

async function loadSettings() {
  try {
    const d = await apiGet("/api/settings");
    _currentMode = d.mode || "demo";
    renderModePill(d.mode, d.public_demo);
    const { publicDemo } = renderSettingsView(d);
    document.getElementById("btnModeDemo").addEventListener("click", () => setMode("demo"));
    if (!publicDemo) {
      document.getElementById("btnModeLive").addEventListener("click", () => setMode("live"));
      document.getElementById("saveUwKeyBtn").addEventListener("click", saveUwKey);
      document.getElementById("testUwKeyBtn").addEventListener("click", testUwKey);
      document.getElementById("disconnectUwKeyBtn").addEventListener("click", disconnectUwKey);
      document.getElementById("saveAiKeyBtn").addEventListener("click", saveAiKey);
      document.getElementById("disconnectAiKeyBtn").addEventListener("click", disconnectAiKey);
      const testAiBtn = document.getElementById("testAiKeyBtn");
      if (testAiBtn) testAiBtn.addEventListener("click", testAiKey);
      const testMcpBtn = document.getElementById("testMcpBtn");
      if (testMcpBtn) testMcpBtn.addEventListener("click", testMcpConnection);
    }
    wireUpdatesSection(d);
    // Watchlist editor now lives on Market Radar's "My Watchlist" panel,
    // not Settings (2026-10-15 move) -- re-rendered from this same
    // /api/settings payload every time loadSettings() runs (including
    // after every add/remove save, see initWatchlistEditor() below).
    const wlRoot = document.getElementById("radarWatchlistEditor");
    if (wlRoot) {
      wlRoot.innerHTML = renderWatchlistEditor(d);
      initWatchlistEditor();
    }
    return d;
  } catch (err) {
    document.getElementById("modePill").textContent = "mode: unknown";
  }
}

// Application Updates (Settings): "Check for Updates" makes ONE explicit,
// user-triggered GET /api/settings/update-check call -- never fetched
// automatically on page load, so this app never makes an unprompted
// outbound call to GitHub. Copy-prompt uses the clipboard API with a
// manual-select fallback for browsers/contexts where it's unavailable.
function wireUpdatesSection(d) {
  const checkBtn = document.getElementById("checkUpdatesBtn");
  const resultEl = document.getElementById("updateCheckResult");
  if (checkBtn && resultEl) {
    checkBtn.addEventListener("click", async () => {
      checkBtn.disabled = true;
      resultEl.textContent = "Checking GitHub for newer commits...";
      try {
        const result = await apiGet("/api/settings/update-check");
        resultEl.innerHTML = renderUpdateCheckResult(result);
      } catch (err) {
        resultEl.textContent = `Check failed: ${err.message}`;
      } finally {
        checkBtn.disabled = false;
      }
    });
  }
  const copyBtn = document.getElementById("copyUpdatePromptBtn");
  const copyStatus = document.getElementById("copyUpdatePromptStatus");
  if (copyBtn && copyStatus) {
    copyBtn.addEventListener("click", async () => {
      const text = buildAgentUpdatePrompt(d.build);
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          copyStatus.textContent = "Copied to clipboard.";
        } else {
          throw new Error("Clipboard API unavailable");
        }
      } catch (err) {
        copyStatus.textContent = "Could not copy automatically -- select and copy the prompt manually.";
      }
    });
  }
}

// Market Radar watchlist editor (2026-10-14, Phase A). Whole-list PUT on
// every add/remove -- reads the current chips from the DOM (already the
// source of truth after each render) rather than tracking a separate
// JS-side array, so there's exactly one place the current list lives.
function initWatchlistEditor() {
  const addBtn = document.getElementById("watchlistAddBtn");
  const addInput = document.getElementById("watchlistAddInput");
  const chipsEl = document.getElementById("watchlistChips");
  const statusEl = document.getElementById("watchlistStatus");
  if (!addBtn || !chipsEl) return; // public demo build has no watchlist editor at all

  function currentTickers() {
    return Array.from(chipsEl.querySelectorAll("[data-ticker]")).map((el) => el.dataset.ticker);
  }

  async function saveWatchlist(tickers) {
    statusEl.textContent = "Saving...";
    try {
      await apiPut("/api/settings/watchlist", { watchlist: tickers });
      statusEl.textContent = "";
      await loadSettings(); // re-render chips from the server's normalized/persisted list
    } catch (err) {
      statusEl.textContent = `Error: ${err.message}`;
    }
  }

  addBtn.addEventListener("click", () => {
    const raw = (addInput.value || "").trim().toUpperCase();
    if (!raw) return;
    if (!/^[A-Z]{1,5}(\.[A-Z])?$/.test(raw)) {
      statusEl.textContent = `"${raw}" doesn't look like a valid ticker symbol.`;
      return;
    }
    const updated = Array.from(new Set([...currentTickers(), raw]));
    addInput.value = "";
    saveWatchlist(updated);
  });
  addInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); addBtn.click(); }
  });
  chipsEl.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-remove-ticker]");
    if (!btn) return;
    const updated = currentTickers().filter((t) => t !== btn.dataset.removeTicker);
    saveWatchlist(updated);
  });
}

async function setMode(mode) {
  try {
    await apiPost("/api/settings/mode", { mode });
    await loadSettings();
    // The OLD mode's ticker/Investigate state must never survive onto the
    // NEW mode's adapter -- a ticker loaded under Demo (e.g. a synthetic-
    // only scenario) can be meaningless or simply absent under Live, and
    // vice versa. Clear first (covers the user being on Ticker Analysis
    // right now), then reload Market Radar so the <select> is rebuilt
    // from the new mode's own universe/scenarios instead of the stale one.
    _navSeq++; // invalidate any in-flight ticker/investigate request from the OLD mode
    clearTickerView();
    // History: if the user was on Ticker Analysis, that history entry's
    // ticker may not exist in the new mode -- REPLACE it (not push) with a
    // plain Market Radar entry so Back from here never lands on a
    // dangling ticker=X that's invalid under the new mode. If they were on
    // Settings/About/Radar, those entries never carried a ticker, so there
    // is nothing stale to fix and we leave history alone.
    if (TIP_STATE.currentView === "ticker") {
      navigateToView("radar", { push: false });
    }
    await loadRadar();
  } catch (err) {
    alert(`Could not switch mode: ${err.message}`);
  }
}

async function saveUwKey() {
  const input = document.getElementById("uwKeyInput");
  const statusEl = document.getElementById("uwKeyStatus");
  const key = input.value.trim();
  if (!key) {
    statusEl.textContent = "Enter a key first.";
    return;
  }
  try {
    await apiPost("/api/settings/uw-key", { api_key: key });
    input.value = "";
    await loadSettings();
    document.getElementById("uwKeyStatus").textContent = "Saved.";
  } catch (err) {
    statusEl.textContent = `Save failed: ${err.message}`;
  }
}

async function testUwKey() {
  // FIX (real user-reported bug, 2026-10-14): Test Connection previously
  // tested whatever key was already "effective" server-side (UI-saved, or
  // the .env fallback) WITHOUT saving whatever the user had just typed
  // into the input first. Combined with the input always rendering empty
  // after any Settings re-render (by design -- a saved key is never
  // echoed back), this made it look like "I pasted a key, clicked Test,
  // it passed, the box is now empty" meant the TYPED key was saved and
  // verified -- when it may actually have tested a DIFFERENT, already-
  // stored key while the just-typed one was silently never saved. Fix:
  // if there's text in the box, Save it first (so Test always verifies
  // what's actually in the box), then Test, with an explicit status
  // message distinguishing "Saved + tested" from "tested the already-
  // saved key" so no step is ever implied without having actually run.
  const input = document.getElementById("uwKeyInput");
  const statusEl = document.getElementById("uwKeyStatus");
  const typed = input.value.trim();
  let message = null;
  try {
    if (typed) {
      statusEl.textContent = "Saving key...";
      await apiPost("/api/settings/uw-key", { api_key: typed });
      input.value = "";
    }
    statusEl.textContent = typed ? "Saved. Testing connection..." : "Testing connection (using the already-saved key)...";
    const d = await apiPost("/api/settings/uw-key/test", {});
    message = `${typed ? "Saved, then " : ""}Test result: ${d.result} — ${d.message}`;
  } catch (err) {
    message = `${typed ? "Save/test" : "Test"} failed: ${err.message}`;
  }
  // Re-render Settings FIRST so the Activate Live Mode button's disabled
  // state (and the "verified" status line) reflect the just-updated
  // server-side verification record -- a successful test is the ONLY
  // thing that can enable that button, per the real backend gate. Then
  // set the status message, since re-rendering replaces #uwKeyStatus.
  await loadSettings();
  const freshStatusEl = document.getElementById("uwKeyStatus");
  if (freshStatusEl) freshStatusEl.textContent = message;
}

async function disconnectUwKey() {
  try {
    await apiDelete("/api/settings/uw-key");
    await loadSettings();
  } catch (err) {
    alert(`Disconnect failed: ${err.message}`);
  }
}

async function saveAiKey() {
  const provider = document.getElementById("aiProviderSelect").value;
  const input = document.getElementById("aiKeyInput");
  const statusEl = document.getElementById("aiKeyStatus");
  const key = input.value.trim();
  if (!key) {
    statusEl.textContent = "Enter a key first.";
    return;
  }
  try {
    await apiPost("/api/settings/ai-key", { provider, api_key: key });
    input.value = "";
    await loadSettings();
    document.getElementById("aiKeyStatus").textContent = "Saved.";
  } catch (err) {
    statusEl.textContent = `Save failed: ${err.message}`;
  }
}

async function disconnectAiKey() {
  try {
    await apiDelete("/api/settings/ai-key");
    await loadSettings();
  } catch (err) {
    alert(`Disconnect failed: ${err.message}`);
  }
}

async function testAiKey() {
  // Real, individually-billed API call -- requires an explicit confirm()
  // dialog with the cost disclosure BEFORE the request fires, on top of
  // the backend's own confirm=true requirement. Never triggered except by
  // this direct user click.
  const statusEl = document.getElementById("aiTestStatus");
  const proceed = confirm(
    "This sends one short, real request to your configured AI provider to confirm the credential works.\n\n" +
    "This WILL incur a small, real API charge (typically a few cents, bounded to a tiny response).\n\n" +
    "It does NOT call Unusual Whales or MCP at all -- a pass here only confirms the AI provider credential, " +
    "nothing about MCP/tool execution.\n\nProceed?"
  );
  if (!proceed) {
    statusEl.textContent = "Cancelled -- no request was sent.";
    return;
  }
  statusEl.textContent = "Testing AI provider connection (real, billed request in progress)...";
  try {
    const d = await apiPost("/api/settings/ai-key/test", { confirm: true });
    statusEl.textContent = d.ok
      ? `OK -- ${d.provider} (${d.model}) responded successfully. This does not test MCP/tool execution.`
      : `Failed -- ${d.error}`;
  } catch (err) {
    statusEl.textContent = `Test failed: ${err.message}`;
  }
}

async function testMcpConnection() {
  // UW-quota-only, no LLM/AI provider involved -- still a real network
  // call, so still gated behind an explicit click, but no paid-charge
  // confirm dialog since there is no AI provider cost here.
  const statusEl = document.getElementById("mcpTestStatus");
  statusEl.textContent = "Testing MCP connection (real, bounded UW call in progress)...";
  try {
    const d = await apiPost("/api/settings/mcp/test", {});
    if (d.ok) {
      const serverInfo = d.server_info ? `${d.server_info.name} v${d.server_info.version}` : "unknown server";
      statusEl.textContent = `OK -- connected to ${serverInfo}. Test tool call: ${d.tool_ok ? "succeeded" : `failed (${d.error || "no detail"})`}. This does not test AI-directed synthesis.`;
    } else {
      statusEl.textContent = `Failed -- ${d.error}`;
    }
  } catch (err) {
    statusEl.textContent = `Test failed: ${err.message}`;
  }
}

// --- Phase D: Market-wide Radar -- lazily loaded the first time its nav
// item is selected (same pattern as Market Discovery's loadDiscovery()
// above), so a user who never opens this view triggers zero extra
// requests. GET /api/scheduler/status + GET /api/radar/market-wide are
// both read-only (never a UW call themselves -- see main.py's route
// docstrings); POST /api/scheduler/mode is the one mutating action.
let _marketWideLoaded = false;

async function loadMarketWideRadar() {
  const statusEl = document.getElementById("marketWideStatus");
  const listEl = document.getElementById("marketWideCandidateList");
  const controlsEl = document.getElementById("schedulerControls");
  try {
    const status = await apiGetAuthed("/api/scheduler/status");
    if (controlsEl) {
      controlsEl.innerHTML = renderSchedulerControls(status);
      wireSchedulerModeToggle();
    }
    const data = await apiGetAuthed("/api/radar/market-wide");
    if (listEl) listEl.innerHTML = renderMarketWideRadar(data);
    if (statusEl) statusEl.textContent = "";
  } catch (err) {
    if (statusEl) statusEl.textContent = `Failed to load Market-wide Radar: ${err.message}`;
  }
}

function wireSchedulerModeToggle() {
  const toggle = document.getElementById("schedulerModeToggle");
  if (!toggle) return;
  toggle.querySelectorAll("[data-scheduler-mode]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const mode = btn.dataset.schedulerMode;
      const statusEl = document.getElementById("marketWideStatus");
      try {
        await apiPost("/api/scheduler/mode", { mode });
        await loadMarketWideRadar();
      } catch (err) {
        if (statusEl) statusEl.textContent = `Failed to change mode: ${err.message}`;
      }
    });
  });
}

function initMarketWideRadar() {
  if (_marketWideLoaded) return;
  _marketWideLoaded = true;
  loadMarketWideRadar();
}

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => navigateToView(b.dataset.view));
  if (b.dataset.view === "marketwide") b.addEventListener("click", initMarketWideRadar);
});
document.getElementById("refreshRadar").addEventListener("click", loadRadar);
// Unified ticker combobox (2026-10-14, replaces the old separate
// <select> + manual-entry <input>): initTickerCombo() (app.core.js)
// wires the one input's typing/suggestions/keyboard-nav/Analyze-click,
// and calls this with whatever raw text the user wants analyzed. In
// Live Mode any format-valid ticker is allowed (same TICKER_RE the
// backend's validate_ticker() enforces for real -- this is just an
// immediate, friendly client-side error, not the actual enforcement
// point). In Demo Mode an unknown ticker is simply left to the existing
// 404 -> renderTickerUnavailable() path, unchanged.
initTickerCombo((rawText) => {
  const t = (rawText || "").trim().toUpperCase();
  if (!t) return;
  if (_currentMode === "live" && !/^[A-Z]{1,5}(\.[A-Z])?$/.test(t)) {
    alert(`"${t}" doesn't look like a valid ticker symbol (1-5 letters, optional .suffix like BRK.A).`);
    return;
  }
  navigateToTicker(t, loadTickerDetail);
});

// Sidebar ticker-jump search (production's .cmd-search pattern): reuses
// the EXACT same validation + navigateToTicker() call as the Ticker
// Analysis "Analyze" field above -- not a parallel navigation path, just
// a second entry point into it.
const sidebarJumpInput = document.getElementById("sidebarTickerJump");
if (sidebarJumpInput) {
  sidebarJumpInput.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const t = (sidebarJumpInput.value || "").trim().toUpperCase();
    if (!t) return;
    if (_currentMode === "live" && !/^[A-Z]{1,5}(\.[A-Z])?$/.test(t)) {
      alert(`"${t}" doesn't look like a valid ticker symbol (1-5 letters, optional .suffix like BRK.A).`);
      return;
    }
    navigateToTicker(t, loadTickerDetail);
    sidebarJumpInput.value = "";
  });
}

// Bootstrap: read the URL (view=.../ticker=..., or the legacy bare
// #ticker=XXX) and land on THAT view+ticker instead of unconditionally
// defaulting to Market Radar -- covers both a fresh deep link and a
// browser refresh. Radar is always loaded first regardless of the target
// view because it's what populates _knownTickers (needed to validate a
// URL-supplied ticker) and the <select> options Ticker Analysis uses.
window.TIP_NAV_HANDLERS = {
  loadTickerDetail,
  getKnownTickers: () => _knownTickers,
  // REAL BUG FIX (2026-10-14): lets applyNavState's refresh/deep-link
  // path accept a manually-typed, format-valid Live Mode ticker the same
  // way the Analyze button's click handler already does -- otherwise a
  // ticker outside TIP_LIVE_WATCHLIST worked once, then broke on refresh.
  isArbitraryTickerAllowed: () => _currentMode === "live",
};
wireHistoryHandling();
(async function bootstrap() {
  // Phase A: establish the X-TIP-Auth token before anything else runs.
  // loadSettings()/loadRadar() below are GET-only and work regardless,
  // but doing this first means the one-time setup banner (if needed)
  // appears immediately on load rather than only after the user's first
  // click on a mutating action.
  await ensureAuthToken();
  await loadSettings();
  await loadRadar();
  const initialState = parseNavHash(location.hash);
  applyNavState(initialState, window.TIP_NAV_HANDLERS, { push: false });
})();
