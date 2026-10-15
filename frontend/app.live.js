// Thin adapter for the backend-connected local/Docker edition. Owns all
// fetch() calls, state-mutating button handlers, and the page-load
// bootstrap sequence. Depends on app.core.js being loaded first (rendering
// lives there, not here). API_BASE lets ?api=http://host:port override the
// same-origin default for local dev when frontend is served separately.
const API_BASE = new URLSearchParams(location.search).get("api") || "";

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// NOTE on credential handling: these helpers submit a key ONCE to the
// backend to save it. The backend never echoes the real value back --
// only a `configured` boolean and the key's last 4 characters. Nothing
// here (or anywhere in this file) writes a key to localStorage,
// sessionStorage, or a cookie.
async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${path} -> HTTP ${res.status}`);
  return data;
}

async function apiDelete(path) {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
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
    statusEl.textContent = `Scanned ${data.scanned} ticker(s) · ${data.highest.length} highest attention · ${data.other.length} other`;
    document.getElementById("activityStrip").innerHTML = renderActivitySummary(data);

    const featured = data.highest[0] || null;
    document.getElementById("featuredSignalSlot").innerHTML = renderFeaturedSignal(featured);
    document.getElementById("breakdownSlot").innerHTML = renderSignalBreakdown(featured);

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
    let scenarioTickers = [];
    try {
      const scenariosResp = await apiGet("/api/scenarios");
      scenarioTickers = (scenariosResp.scenarios || []).map((s) => s.ticker);
    } catch (e) {
      /* non-fatal: select falls back to Radar-qualifying tickers only */
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

async function loadTickerDetail(ticker) {
  const mySeq = ++_navSeq;
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)}...</div>`;
  try {
    const d = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}`);
    if (mySeq !== _navSeq) return; // superseded by a newer ticker/mode switch
    el.innerHTML = renderTickerDetail(d);
    try {
      const ohlc = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}/ohlc`);
      if (mySeq !== _navSeq) return;
      mountCandlestickChart("ohlcChartContainer", ohlc);
    } catch (chartErr) {
      if (mySeq !== _navSeq) return;
      mountCandlestickChart("ohlcChartContainer", { unavailable: true, reason: { message: chartErr.message } });
    }
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
function modeLabelFor(simulated) {
  const modePart = _currentMode === "live" ? "LIVE mode" : "DEMO mode";
  const aiPart = simulated ? "simulated AI transcript" : "live AI + MCP calls";
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
    const res = await fetch(`${API_BASE}/api/investigate/${encodeURIComponent(ticker)}`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    if (mySeq !== _navSeq) return; // user moved on before this resolved -- never render a stale ticker's result
    body.innerHTML = renderInvestigateResult(d, ticker, modeLabel || modeLabelFor(d.simulated));
  } catch (err) {
    if (mySeq !== _navSeq) return;
    body.innerHTML = `<div class="empty">Investigation failed: ${esc(err.message)}</div>`;
  }
}

async function loadInvestigateSection(ticker, navSeq) {
  const el = document.getElementById("investigateSection");
  try {
    const avail = await apiGet("/api/investigate/availability");
    if (navSeq != null && navSeq !== _navSeq) return; // superseded mid-fetch -- never render for a ticker the user has since left
    const modeLabel = modeLabelFor(avail.simulated);
    if (!avail.available) {
      el.innerHTML = `
        <div class="ai-card">
          <h3>Investigate with AI</h3>
          ${renderInvestigateHeader(ticker, modeLabel)}
          <p class="ai-disabled-note">${esc(avail.reason || "This feature is not available right now.")}</p>
        </div>`;
      return;
    }
    const simNote = avail.simulated
      ? `<p class="ai-disabled-note">Demo mode: running this will show a pre-written simulated transcript, not a real AI/UW call.</p>`
      : `<p class="ai-disabled-note">Live mode: this will make a few bounded, read-only Unusual Whales lookups plus two AI calls using your saved keys, entirely from your local backend. Bounded, read-only investigation; the full LLM-directed tool-selection/synthesis workflow has not yet been verified end-to-end with a real provider -- see README.</p>`;
    el.innerHTML = `
      <div class="ai-card">
        <h3>Investigate with AI</h3>
        ${renderInvestigateHeader(ticker, modeLabel)}
        ${simNote}
        <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI</button>
        <div id="investigateBody" style="margin-top:10px"></div>
      </div>`;
    document.getElementById("runInvestigateBtn").addEventListener("click", () => runInvestigate(ticker, navSeq, modeLabel));
  } catch (err) {
    el.innerHTML = `<div class="ai-card"><h3>Investigate with AI</h3><p class="ai-disabled-note">Unavailable: ${esc(err.message)}</p></div>`;
  }
}

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
    return d;
  } catch (err) {
    document.getElementById("modePill").textContent = "mode: unknown";
  }
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

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => navigateToView(b.dataset.view));
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
  await loadSettings();
  await loadRadar();
  const initialState = parseNavHash(location.hash);
  applyNavState(initialState, window.TIP_NAV_HANDLERS, { push: false });
})();
