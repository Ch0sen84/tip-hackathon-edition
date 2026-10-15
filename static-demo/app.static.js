// Thin adapter for the credential-free public demo. Loads pre-baked JSON
// from data/*.json instead of a backend; makes NO mutating calls of any
// kind. Depends on the SAME app.core.js used by the local edition (copied
// in verbatim by tools/build_static_demo.sh -- never hand-edited here).
//
// Every payload in data/ was captured verbatim from a real run of the
// local app's demo mode (SyntheticDataAdapter feeding the real
// signal_engine.py logic) -- see data/README.md for provenance.

async function loadJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

// Same tracking as app.live.js's _knownTickers/_navSeq (this file has no
// mode switch, but it DOES have the same async-ticker-navigation race the
// live edition has -- a user can click a second ticker before the first
// one's data/ticker/{T}.json fetch resolves, and Back/Forward can trigger
// a navigation while a fetch for the old target is still in flight).
let _knownTickers = [];
let _navSeq = 0;

async function loadRadar() {
  const statusEl = document.getElementById("radarStatus");
  statusEl.textContent = "Loading Market Radar (simulated)...";
  try {
    const data = await loadJson("data/radar.json");
    statusEl.textContent =
      `Scanned ${data.scanned} ticker(s) · ${data.highest.length} highest attention · ${data.other.length} other (simulated data)`;
    document.getElementById("activityStrip").innerHTML = renderActivitySummary(data);

    const featured = data.highest[0] || null;
    document.getElementById("featuredSignalSlot").innerHTML = renderFeaturedSignal(featured);
    document.getElementById("breakdownSlot").innerHTML = renderSignalBreakdown(featured);

    // Stage 2 (P3): same filters/cards/counts/history-chip rendering as
    // the local edition -- see app.core.js's
    // setupRadarFiltersAndRender/renderRadarLists (byte-identical file,
    // copied in by tools/build_static_demo.sh).
    setupRadarFiltersAndRender(data, loadTickerDetail);

    // Same fix as the local edition's app.live.js: data/radar.json only
    // ever contains Radar-qualifying tickers, so DRFT/NADA (zero
    // qualifying gates by design) would otherwise never appear in this
    // <select>. Merge in data/scenarios.json, deduped, so all 5 captured
    // scenarios stay selectable and loadable via data/ticker/{t}.json.
    let scenarioTickers = [];
    try {
      const scenariosResp = await loadJson("data/scenarios.json");
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

async function loadTickerDetail(ticker) {
  const mySeq = ++_navSeq;
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)} (simulated)...</div>`;
  try {
    const d = await loadJson(`data/ticker/${encodeURIComponent(ticker)}.json`);
    if (mySeq !== _navSeq) return; // superseded by a newer ticker navigation (click or Back/Forward)
    el.innerHTML = renderTickerDetail(d);
    try {
      const ohlc = await loadJson(`data/ohlc/${encodeURIComponent(ticker)}.json`);
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

// --- Investigate with AI (always simulated on this static build) --------

// This build has no mode switch and never makes a live AI/UW call -- the
// mode label is always this one constant string (never inferred from a
// fetch), matching the real, fixed nature of the static demo.
const STATIC_MODE_LABEL = "DEMO mode \u2014 simulated AI transcript";

async function runInvestigate(ticker, navSeq) {
  const mySeq = navSeq != null ? navSeq : _navSeq;
  const body = document.getElementById("investigateBody");
  body.innerHTML = `<div class="empty">Loading simulated investigation for ${esc(ticker)}...</div>`;
  try {
    const d = await loadJson(`data/investigate/${encodeURIComponent(ticker)}.json`);
    if (mySeq !== _navSeq) return; // user navigated elsewhere before this resolved -- never render a stale ticker's result
    body.innerHTML = renderInvestigateResult(d, ticker, STATIC_MODE_LABEL);
  } catch (err) {
    if (mySeq !== _navSeq) return;
    body.innerHTML = `<div class="empty">Investigation failed: ${esc(err.message)}</div>`;
  }
}

async function loadInvestigateSection(ticker, navSeq) {
  const el = document.getElementById("investigateSection");
  if (navSeq != null && navSeq !== _navSeq) return; // superseded mid-load -- never render for a ticker the user has since left
  el.innerHTML = `
    <div class="ai-card">
      <h3>Investigate with AI</h3>
      ${renderInvestigateHeader(ticker, STATIC_MODE_LABEL)}
      <p class="ai-disabled-note">Public demo: this always shows a pre-written simulated transcript
        captured from a real run of the local app's bounded AI-investigation feature -- no live AI
        or MCP call happens on this page. The downloadable local edition can run a bounded, read-only
        investigation against UW's real MCP server plus your own AI provider key when live; the full
        LLM-directed tool-selection/synthesis workflow has not yet been verified end-to-end with a real
        provider -- see the GitHub README's documented status.</p>
      <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI (simulated)</button>
      <div id="investigateBody" style="margin-top:10px"></div>
    </div>`;
  document.getElementById("runInvestigateBtn").addEventListener("click", () => runInvestigate(ticker, navSeq));
}

// --- Settings view (hardcoded public-demo state) -------------------------
// This static build has no backend and no credentials, so Settings always
// renders in the same explanatory-only shape the real app's PUBLIC_DEMO=1
// server flag produces -- reusing the exact same rendering function/markup
// as the local edition so the two never visually diverge.

async function loadSettings() {
  renderModePill("demo", true);
  renderSettingsView({ mode: "demo", public_demo: true, uw: {}, ai: {} });
}

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => navigateToView(b.dataset.view));
});
document.getElementById("refreshRadar").addEventListener("click", loadRadar);
// Unified ticker combobox (2026-10-14, shared logic/markup with the
// local edition, see app.core.js's initTickerCombo()). This build has
// no Live Mode, so there's no arbitrary-ticker format check here --
// typing an unknown ticker simply hits the existing 404 ->
// renderTickerUnavailable() path below, unchanged.
initTickerCombo((rawText) => {
  const t = (rawText || "").trim().toUpperCase();
  if (t) navigateToTicker(t, loadTickerDetail);
});

// Bootstrap: same URL-first scheme as the live edition (app.live.js) --
// read the URL (view=.../ticker=..., or the legacy bare #ticker=XXX) and
// land on that view+ticker, so a refresh or a directly-pasted deep link
// into this zero-backend static build behaves identically to the local
// edition, with zero server/routing involved.
window.TIP_NAV_HANDLERS = { loadTickerDetail, getKnownTickers: () => _knownTickers };
wireHistoryHandling();
(async function bootstrap() {
  await loadSettings();
  await loadRadar();
  const initialState = parseNavHash(location.hash);
  applyNavState(initialState, window.TIP_NAV_HANDLERS, { push: false });
})();
