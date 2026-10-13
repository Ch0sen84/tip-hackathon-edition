// Static public demo build. NO backend, NO network calls other than loading
// this page's own same-origin static JSON files under data/. Every payload
// in data/ was captured verbatim from a real run of the local app's demo
// mode (SyntheticDataAdapter feeding the real signal_engine.py logic) --
// these are genuine computed engine outputs on fabricated input, not
// hand-written sample text. See data/README.md for provenance.
//
// This file intentionally mirrors frontend/app.js's rendering functions
// (renderCard/renderRow/renderGate/renderInvestigate*/renderSettingsView)
// so the two editions never drift into two different implementations of
// how results are displayed -- only the data-loading layer differs.

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtPct(x) {
  if (x === null || x === undefined) return "n/a";
  const v = Number(x);
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

async function loadJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

function renderCard(entry) {
  const px = entry.price || {};
  const chg = px.chg_pct;
  const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
  return `
    <div class="card">
      <div class="card-head">
        <div><span class="tk">${esc(entry.ticker)}</span></div>
        <div>
          <span class="px">${px.last != null ? px.last.toFixed(2) : "n/a"}</span>
          <span class="pxchg ${chgClass}">${fmtPct(chg)}</span>
        </div>
      </div>
      <div class="tags">
        <span class="tag">${esc(entry.label)}</span>
        <span class="tag state-${esc(entry.state)}">${esc(entry.state || "")}</span>
        <span class="tag">confidence: ${esc(entry.confidence)}</span>
      </div>
      <ul class="ev-list">
        ${(entry.evidence || []).map((e) => `<li>${esc(e.text)}</li>`).join("")}
      </ul>
      <div class="read">${esc(entry.current_read)}</div>
    </div>`;
}

function renderRow(entry) {
  const px = entry.price || {};
  const chg = px.chg_pct;
  const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
  return `
    <tr>
      <td><strong>${esc(entry.ticker)}</strong></td>
      <td><span class="tag state-${esc(entry.state)}">${esc(entry.state || "")}</span></td>
      <td>${esc(entry.confidence)}</td>
      <td class="col-headline">${esc(entry.headline)}</td>
      <td class="col-read">${esc(entry.current_read)}</td>
    </tr>`;
}

async function loadRadar() {
  const statusEl = document.getElementById("radarStatus");
  statusEl.textContent = "Loading Market Radar (simulated)...";
  try {
    const data = await loadJson("data/radar.json");
    statusEl.textContent =
      `Scanned ${data.scanned} ticker(s) · ${data.highest.length} highest attention · ${data.other.length} other (simulated data)`;
    document.getElementById("highestGrid").innerHTML = data.highest.map(renderCard).join("") || "";
    const tbody = document.querySelector("#otherTable tbody");
    tbody.innerHTML = data.other.map(renderRow).join("");
    document.getElementById("radarEmpty").style.display =
      (data.highest.length === 0 && data.other.length === 0) ? "block" : "none";

    const sel = document.getElementById("tickerSelect");
    const allTickers = [...data.highest, ...data.other].map((e) => e.ticker);
    sel.innerHTML = allTickers.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  } catch (err) {
    statusEl.textContent = `Error loading Market Radar: ${err.message}`;
  }
}

function renderGate(name, gate) {
  return `
    <div class="gate-row">
      <span class="gname">${esc(name.replace(/_/g, " "))}</span>
      <span class="gpass ${gate.passed ? "yes" : "no"}">${gate.passed ? "PASS" : "no"}</span>
    </div>`;
}

async function loadTickerDetail(ticker) {
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)} (simulated)...</div>`;
  try {
    const d = await loadJson(`data/ticker/${encodeURIComponent(ticker)}.json`);
    const gatesHtml = Object.entries(d.gates).map(([name, g]) => renderGate(name, g)).join("");
    el.innerHTML = `
      <div class="detail-grid">
        <div class="card">
          <div class="card-head"><span class="tk">${esc(d.ticker)}</span>
            <span class="tag state-${esc(d.state)}">${esc(d.state)}</span>
          </div>
          <div class="tags">
            <span class="tag">confidence: ${esc(d.confidence)}</span>
            <span class="tag">${esc(d.signal_type)}</span>
            <span class="tag">qualifying days: ${d.qualifying_days_count}</span>
          </div>
          <div class="read">${esc(d.headline)}</div>
          <div class="read">${esc(d.current_read)}</div>
          <div class="read">Transition: ${esc(d.previous_state || "none")} &rarr; ${esc(d.state)} (<code>${esc(d.transition_rule)}</code>)</div>
          <div class="read">Confidence reason: ${esc(d.confidence_reason)}</div>
        </div>
        <div class="card">
          <div class="card-head"><strong>Named gates</strong></div>
          ${gatesHtml}
        </div>
      </div>
      <details class="how" style="margin-top:14px">
        <summary>Raw adapter data (what fed the gates above -- simulated)</summary>
        <pre class="raw">${esc(JSON.stringify(d.raw, null, 2))}</pre>
      </details>`;
    await loadInvestigateSection(ticker);
  } catch (err) {
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
}

// --- Investigate with AI (always simulated on this static build) --------

function renderInvestigateFacts(facts) {
  if (!facts || !facts.length) return `<p class="muted">No observed facts were retained.</p>`;
  return facts.map((f) => `
    <div class="ai-fact">
      ${esc(f.fact)}
      <span class="src">source: ${esc(f.source_tool)}</span>
    </div>`).join("");
}

function renderInvestigateList(items) {
  if (!items || !items.length) return `<p class="muted">None noted.</p>`;
  return `<ul class="ai-list">${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;
}

function renderInvestigateResult(d) {
  const simBanner = `<div class="ai-simulated-banner">${esc(d.simulated_label || "Simulated agent walkthrough -- not live UW data, not a real AI call")}</div>`;
  return `
    ${simBanner}
    <div class="tags"><span class="tag">tools used: ${esc((d.tools_used || []).join(", ") || "none")}</span></div>
    <div class="ai-section">
      <h4>Observed Facts</h4>
      ${renderInvestigateFacts(d.observed_facts)}
    </div>
    <div class="ai-section">
      <h4>Supporting Evidence</h4>
      ${renderInvestigateList(d.supporting_evidence)}
    </div>
    <div class="ai-section">
      <h4>Conflicting Observations</h4>
      ${renderInvestigateList(d.conflicting_observations)}
    </div>
    <div class="ai-section">
      <h4>Limitations</h4>
      ${renderInvestigateList(d.limitations)}
    </div>
    <div class="ai-interpretation">
      <h4>AI Interpretation (model's reasoning, not a retrieved fact)</h4>
      <p>${esc(d.ai_interpretation)}</p>
    </div>`;
}

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
        or MCP call happens on this page. The downloadable local edition can run this for real
        against UW's MCP server plus your own AI provider key.</p>
      <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI (simulated)</button>
      <div id="investigateBody" style="margin-top:10px"></div>
    </div>`;
  document.getElementById("runInvestigateBtn").addEventListener("click", () => runInvestigate(ticker));
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
}

// --- Mode badge + Settings view (hardcoded public-demo state) -----------
// This static build has no backend and no credentials, so Settings always
// renders in the same explanatory-only shape the real app's PUBLIC_DEMO=1
// server flag produces -- reusing the exact same rendering function/markup
// as the local edition so the two never visually diverge.

function renderModePill() {
  const pill = document.getElementById("modePill");
  pill.classList.remove("mode-demo", "mode-live");
  pill.textContent = "DEMO MODE — Synthetic Data (public demo)";
  pill.classList.add("mode-demo");
}

function renderSettingsView(d) {
  const root = document.getElementById("view-settings");
  const banner = `<div class="public-demo-banner">Live UW mode and credential entry are available in the
     downloadable local edition — this public demo runs on synthetic data only.</div>`;

  const modeSection = `
    <div class="settings-card">
      <h3>Mode</h3>
      <div class="settings-row">
        <button class="btn btn-active" disabled>Demo</button>
        <button class="btn" disabled>Live</button>
      </div>
    </div>`;

  const uwSection = `<div class="settings-card">
      <h3>Unusual Whales API Key</h3>
      <p class="muted">This is what Live Mode looks like in the local edition.</p>
      <input type="password" placeholder="UW API key" disabled />
      <div class="settings-row">
        <button class="btn" disabled>Save</button>
        <button class="btn" disabled>Test Connection</button>
        <button class="btn" disabled>Disconnect</button>
      </div>
    </div>`;

  const aiSection = `<div class="settings-card">
      <h3>AI / MCP (optional) <span class="muted">(real in the local edition)</span></h3>
      <p class="muted">Not available in the public demo build. The local edition supports a
        bounded, read-only "Investigate with AI" feature using UW's real MCP server plus your own
        AI provider key -- see the GitHub README for setup.</p>
    </div>`;

  root.innerHTML = `
    ${banner}
    ${modeSection}
    ${uwSection}
    ${aiSection}`;
}

async function loadSettings() {
  renderModePill();
  renderSettingsView({});
}

document.querySelectorAll(".nav-item").forEach((b) => {
  b.addEventListener("click", () => switchView(b.dataset.view));
});
document.getElementById("refreshRadar").addEventListener("click", loadRadar);
document.getElementById("loadTicker").addEventListener("click", () => {
  const t = document.getElementById("tickerSelect").value;
  if (t) loadTickerDetail(t);
});

loadSettings();
loadRadar();
