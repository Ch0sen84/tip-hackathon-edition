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

    const restOfHighest = data.highest.slice(1);
    document.getElementById("highestGrid").innerHTML = restOfHighest.map(renderCard).join("") || "";
    const tbody = document.querySelector("#otherTable tbody");
    tbody.innerHTML = data.other.map(renderRow).join("");
    document.getElementById("radarEmpty").style.display =
      (data.highest.length === 0 && data.other.length === 0) ? "block" : "none";

    const sel = document.getElementById("tickerSelect");
    const allTickers = [...data.highest, ...data.other].map((e) => e.ticker);
    sel.innerHTML = allTickers.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");

    wireInvestigateButtons(loadTickerDetail);
    document.querySelectorAll('[data-ticker]').forEach((card) => {
      card.addEventListener("click", (ev) => {
        if (ev.target.closest("[data-investigate-ticker]")) return;
        const t = card.dataset.ticker;
        const entry = [...data.highest, ...data.other].find((e) => e.ticker === t);
        if (entry) document.getElementById("breakdownSlot").innerHTML = renderSignalBreakdown(entry);
      });
    });
  } catch (err) {
    statusEl.textContent = `Error loading Market Radar: ${err.message}`;
  }
}

async function loadTickerDetail(ticker) {
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)} (simulated)...</div>`;
  try {
    const d = await loadJson(`data/ticker/${encodeURIComponent(ticker)}.json`);
    el.innerHTML = renderTickerDetail(d);
    await loadInvestigateSection(ticker);
  } catch (err) {
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
}

// --- Investigate with AI (always simulated on this static build) --------

async function runInvestigate(ticker) {
  const body = document.getElementById("investigateBody");
  body.innerHTML = `<div class="empty">Loading simulated investigation for ${esc(ticker)}...</div>`;
  try {
    const d = await loadJson(`data/investigate/${encodeURIComponent(ticker)}.json`);
    body.innerHTML = renderInvestigateResult(d);
  } catch (err) {
    body.innerHTML = `<div class="empty">Investigation failed: ${esc(err.message)}</div>`;
  }
}

async function loadInvestigateSection(ticker) {
  const el = document.getElementById("investigateSection");
  el.innerHTML = `
    <div class="ai-card">
      <h3>Investigate with AI</h3>
      <p class="ai-disabled-note">Public demo: this always shows a pre-written simulated transcript
        captured from a real run of the local app's bounded AI-investigation feature -- no live AI
        or MCP call happens on this page. The downloadable local edition can run a bounded, read-only
        investigation against UW's real MCP server plus your own AI provider key when live; the full
        LLM-directed tool-selection/synthesis workflow has not yet been verified end-to-end with a real
        provider -- see the GitHub README's documented status.</p>
      <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI (simulated)</button>
      <div id="investigateBody" style="margin-top:10px"></div>
    </div>`;
  document.getElementById("runInvestigateBtn").addEventListener("click", () => runInvestigate(ticker));
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
  b.addEventListener("click", () => switchView(b.dataset.view));
});
document.getElementById("refreshRadar").addEventListener("click", loadRadar);
document.getElementById("loadTicker").addEventListener("click", () => {
  const t = document.getElementById("tickerSelect").value;
  if (t) loadTickerDetail(t);
});

(async function bootstrap() {
  await loadSettings();
  await loadRadar();
  const hashTicker = getTickerFromHash();
  if (hashTicker) {
    navigateToTicker(hashTicker, loadTickerDetail);
  }
})();
