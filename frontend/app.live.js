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
    if (featured) {
      document.querySelectorAll('[data-ticker]').forEach((card) => {
        card.addEventListener("click", (ev) => {
          if (ev.target.closest("[data-investigate-ticker]")) return;
          const t = card.dataset.ticker;
          const entry = [...data.highest, ...data.other].find((e) => e.ticker === t);
          if (entry) document.getElementById("breakdownSlot").innerHTML = renderSignalBreakdown(entry);
        });
      });
    }
  } catch (err) {
    statusEl.textContent = `Error loading Market Radar: ${err.message}`;
  }
}

async function loadTickerDetail(ticker) {
  const el = document.getElementById("tickerDetail");
  el.innerHTML = `<div class="empty">Loading ${esc(ticker)}...</div>`;
  try {
    const d = await apiGet(`/api/ticker/${encodeURIComponent(ticker)}`);
    el.innerHTML = renderTickerDetail(d);
    await loadInvestigateSection(ticker);
  } catch (err) {
    el.innerHTML = `<div class="empty">Error loading ${esc(ticker)}: ${esc(err.message)}</div>`;
  }
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
      : `<p class="ai-disabled-note">Live mode: this will make a few bounded, read-only Unusual Whales lookups plus two AI calls using your saved keys, entirely from your local backend. Bounded, read-only investigation; the full LLM-directed tool-selection/synthesis workflow has not yet been verified end-to-end with a real provider -- see README.</p>`;
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

async function loadSettings() {
  try {
    const d = await apiGet("/api/settings");
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

// Bootstrap: if the page was opened with #ticker=XXX (e.g. a link from
// Market Radar's Investigate affordance surviving a reload), go straight
// to Ticker Analysis with it loaded instead of defaulting to Market Radar.
(async function bootstrap() {
  await loadSettings();
  await loadRadar();
  const hashTicker = getTickerFromHash();
  if (hashTicker) {
    navigateToTicker(hashTicker, loadTickerDetail);
  }
})();
