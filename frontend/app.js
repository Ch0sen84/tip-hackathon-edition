// Plain JS frontend calling the new FastAPI backend's own routes.
// No build step. API_BASE is same-origin by default; override via
// ?api=http://host:port for local dev when frontend is served separately.
const API_BASE = new URLSearchParams(location.search).get("api") || "";

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
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
  } catch (err) {
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
}

async function loadMode() {
  try {
    const d = await apiGet("/api/mode");
    document.getElementById("modePill").textContent = `mode: ${d.mode}`;
  } catch (err) {
    document.getElementById("modePill").textContent = "mode: unknown";
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

loadMode();
loadRadar();
