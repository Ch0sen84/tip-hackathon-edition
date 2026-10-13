// Plain JS frontend calling the new FastAPI backend's own routes.
// No build step. API_BASE is same-origin by default; override via
// ?api=http://host:port for local dev when frontend is served separately.
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
  statusEl.textContent = "Loading Market Radar...";
  try {
    const data = await apiGet("/api/radar");
    statusEl.textContent = `Scanned ${data.scanned} ticker(s) · ${data.highest.length} highest attention · ${data.other.length} other`;
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
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)}...</div>`;
  try {
    const d = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}`);
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
        <summary>Raw adapter data (what fed the gates above)</summary>
        <pre class="raw">${esc(JSON.stringify(d.raw, null, 2))}</pre>
      </details>`;
    await loadInvestigateSection(ticker);
  } catch (err) {
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
}

// --- Investigate with AI (bounded, optional) -----------------------------
//
// Round-trip shape matches backend/app/ai/schemas.py's InvestigationResult:
// observed_facts (each tagged with source_tool), supporting_evidence,
// conflicting_observations, limitations, and a visually-separated
// ai_interpretation. Demo mode / PUBLIC_DEMO always returns a static,
// clearly-labeled simulated transcript -- this file never makes that
// distinction itself, it just renders whatever the backend returned,
// including the prominent "simulated" banner when present.

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
  const simBanner = d.simulated
    ? `<div class="ai-simulated-banner">${esc(d.simulated_label || "Simulated agent walkthrough -- not live UW data, not a real AI call")}</div>`
    : "";
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
  body.innerHTML = `<div class="empty">Investigating ${esc(ticker)}... this makes a few bounded read-only calls and can take several seconds.</div>`;
  try {
    const res = await fetch(`${API_BASE}/api/investigate/${encodeURIComponent(ticker)}`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    body.innerHTML = renderInvestigateResult(d);
  } catch (err) {
    body.innerHTML = `<div class="empty">Investigation failed: ${esc(err.message)}</div>`;
  }
}

async function loadInvestigateSection(ticker) {
  const el = document.getElementById("investigateSection");
  try {
    const avail = await apiGet("/api/investigate/availability");
    if (!avail.available) {
      el.innerHTML = `
        <div class="ai-card">
          <h3>Investigate with AI</h3>
          <p class="ai-disabled-note">${esc(avail.reason || "This feature is not available right now.")}</p>
        </div>`;
      return;
    }
    const simNote = avail.simulated
      ? `<p class="ai-disabled-note">Demo mode: running this will show a pre-written simulated transcript, not a real AI/UW call.</p>`
      : `<p class="ai-disabled-note">Live mode: this will make a few bounded, read-only Unusual Whales lookups plus two AI calls using your saved keys, entirely from your local backend.</p>`;
    el.innerHTML = `
      <div class="ai-card">
        <h3>Investigate with AI</h3>
        ${simNote}
        <button class="btn" id="runInvestigateBtn">Investigate ${esc(ticker)} with AI</button>
        <div id="investigateBody" style="margin-top:10px"></div>
      </div>`;
    document.getElementById("runInvestigateBtn").addEventListener("click", () => runInvestigate(ticker));
  } catch (err) {
    el.innerHTML = `<div class="ai-card"><h3>Investigate with AI</h3><p class="ai-disabled-note">Unavailable: ${esc(err.message)}</p></div>`;
  }
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
}

// --- Mode badge (visible on every view, not just Settings) + Settings view

function renderModePill(mode) {
  const pill = document.getElementById("modePill");
  pill.classList.remove("mode-demo", "mode-live");
  if (mode === "live") {
    pill.textContent = "LIVE — Unusual Whales API";
    pill.classList.add("mode-live");
  } else {
    pill.textContent = "DEMO MODE — Synthetic Data";
    pill.classList.add("mode-demo");
  }
}

function renderSettingsView(d) {
  const root = document.getElementById("view-settings");
  const publicDemo = d.public_demo;
  const uw = d.uw;
  const ai = d.ai;

  const banner = publicDemo
    ? `<div class="public-demo-banner">Live UW mode and credential entry are available in the
       downloadable local edition — this public demo runs on synthetic data only.</div>`
    : "";

  const modeSection = `
    <div class="settings-card">
      <h3>Mode</h3>
      <div class="settings-row">
        <button class="btn ${d.mode === "demo" ? "btn-active" : ""}" id="btnModeDemo">Demo</button>
        <button class="btn ${d.mode === "live" ? "btn-active" : ""}" id="btnModeLive" ${publicDemo ? "disabled" : ""}>Live</button>
      </div>
    </div>`;

  const uwStatusLine = uw.configured
    ? `configured (source: ${esc(uw.source)}, ending •••${esc(uw.last4 || "")})${uw.last_test_result ? ` · last test: ${esc(uw.last_test_result)}` : ""}`
    : "not configured";

  const uwSection = publicDemo
    ? `<div class="settings-card">
        <h3>Unusual Whales API Key</h3>
        <p class="muted">This is what Live Mode looks like in the local edition.</p>
        <input type="password" placeholder="UW API key" disabled />
        <div class="settings-row">
          <button class="btn" disabled>Save</button>
          <button class="btn" disabled>Test Connection</button>
          <button class="btn" disabled>Disconnect</button>
        </div>
      </div>`
    : `<div class="settings-card">
        <h3>Unusual Whales API Key</h3>
        <p class="muted">Status: ${uwStatusLine}</p>
        <input type="password" id="uwKeyInput" placeholder="Paste UW API key" autocomplete="off" />
        <div class="settings-row">
          <button class="btn" id="saveUwKeyBtn">Save</button>
          <button class="btn" id="testUwKeyBtn">Test Connection</button>
          <button class="btn" id="disconnectUwKeyBtn">Disconnect</button>
        </div>
        <div id="uwKeyStatus" class="muted" style="margin-top:6px"></div>
      </div>`;

  const aiStatusLine = ai.configured
    ? `configured (${esc(ai.provider)}, ending •••${esc(ai.last4 || "")})`
    : "not configured";

  const aiSection = publicDemo
    ? `<div class="settings-card">
        <h3>AI / MCP (optional) <span class="muted">(agent feature coming in the next build)</span></h3>
        <p class="muted">Not available in the public demo build.</p>
      </div>`
    : `<div class="settings-card">
        <h3>AI / MCP (optional) <span class="muted">(agent feature coming in the next build)</span></h3>
        <p class="muted">Status: ${aiStatusLine}</p>
        <select id="aiProviderSelect">
          <option value="anthropic" ${ai.provider === "anthropic" ? "selected" : ""}>Anthropic</option>
          <option value="openai" ${ai.provider === "openai" ? "selected" : ""}>OpenAI</option>
        </select>
        <input type="password" id="aiKeyInput" placeholder="Paste provider API key" autocomplete="off" />
        <div class="settings-row">
          <button class="btn" id="saveAiKeyBtn">Save</button>
          <button class="btn" id="disconnectAiKeyBtn">Disconnect</button>
        </div>
        <div id="aiKeyStatus" class="muted" style="margin-top:6px"></div>
      </div>`;

  root.innerHTML = `
    ${banner}
    ${modeSection}
    ${uwSection}
    ${aiSection}`;

  document.getElementById("btnModeDemo").addEventListener("click", () => setMode("demo"));
  if (!publicDemo) {
    document.getElementById("btnModeLive").addEventListener("click", () => setMode("live"));
    document.getElementById("saveUwKeyBtn").addEventListener("click", saveUwKey);
    document.getElementById("testUwKeyBtn").addEventListener("click", testUwKey);
    document.getElementById("disconnectUwKeyBtn").addEventListener("click", disconnectUwKey);
    document.getElementById("saveAiKeyBtn").addEventListener("click", saveAiKey);
    document.getElementById("disconnectAiKeyBtn").addEventListener("click", disconnectAiKey);
  }
}

async function loadSettings() {
  try {
    const d = await apiGet("/api/settings");
    renderModePill(d.mode);
    renderSettingsView(d);
    return d;
  } catch (err) {
    document.getElementById("modePill").textContent = "mode: unknown";
  }
}

async function setMode(mode) {
  try {
    await apiPost("/api/settings/mode", { mode });
    await loadSettings();
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
  const statusEl = document.getElementById("uwKeyStatus");
  statusEl.textContent = "Testing connection...";
  try {
    const d = await apiPost("/api/settings/uw-key/test", {});
    statusEl.textContent = `Test result: ${d.result} — ${d.message}`;
  } catch (err) {
    statusEl.textContent = `Test failed: ${err.message}`;
  }
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
