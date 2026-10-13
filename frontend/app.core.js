// Shared rendering/view logic for BOTH the backend-connected local/Docker
// edition (frontend/) and the credential-free public demo (static-demo/).
// This file is backend-agnostic: it contains NO fetch() calls of any kind
// and no knowledge of how data arrived. Each edition has its own thin
// adapter (app.live.js or app.static.js) that loads data and calls the
// functions below to render it.
//
// IMPORTANT: this exact file must be byte-identical in frontend/ and
// static-demo/ -- it is copied by tools/build_static_demo.sh, not hand
// duplicated. Edit it only in frontend/app.core.js, then re-run that script.

// --- small shared state: "current ticker" (see section 2 of the redesign) -
// Plain module-level state + a URL hash fragment (#ticker=XXX), no router
// library. navigateToTicker() is the one function both adapters call from
// a Market Radar "Investigate" click so the user never has to reselect a
// ticker they already clicked on.
const TIP_STATE = { currentTicker: null };

function setCurrentTicker(ticker) {
  TIP_STATE.currentTicker = ticker;
  try {
    history.replaceState(null, "", `#ticker=${encodeURIComponent(ticker)}`);
  } catch (e) {
    /* hash update is a convenience, never fatal */
  }
}

function getTickerFromHash() {
  const m = /ticker=([A-Za-z0-9.\-]+)/.exec(location.hash || "");
  return m ? m[1].toUpperCase() : null;
}

// clearTickerView: the one place that resets ALL state tied to "whichever
// ticker is currently loaded" -- TIP_STATE.currentTicker, the #ticker=...
// hash, the <select> (back to its placeholder), and the two DOM panes that
// render ticker-specific content (#tickerDetail, #investigateSection).
// Needed whenever the underlying data source changes out from under an
// already-rendered ticker (e.g. a Demo<->Live mode switch) so stale
// content from the OLD mode/adapter never stays on screen referencing a
// ticker that may not even exist in the new mode. Both adapters can call
// this; the static demo has no mode switch so it currently has no caller,
// but it is kept here (not duplicated) since app.core.js is the one
// shared, byte-identical file.
function clearTickerView() {
  TIP_STATE.currentTicker = null;
  try {
    history.replaceState(null, "", location.pathname + location.search);
  } catch (e) {
    /* hash update is a convenience, never fatal */
  }
  const sel = document.getElementById("tickerSelect");
  if (sel) sel.value = "";
  const detailEl = document.getElementById("tickerDetail");
  if (detailEl) detailEl.innerHTML = `<div class="empty">Select a ticker to analyze.</div>`;
  const investigateEl = document.getElementById("investigateSection");
  if (investigateEl) investigateEl.innerHTML = "";
}

// renderTickerOptions: builds the Ticker Analysis <select> options from a
// merged/deduped ticker list, always with a leading placeholder so "no
// ticker selected" is a real, visible state instead of silently defaulting
// to whatever option happened to be first.
function renderTickerOptions(tickers, selected) {
  const opts = tickers
    .map((t) => `<option value="${esc(t)}" ${t === selected ? "selected" : ""}>${esc(t)}</option>`)
    .join("");
  return `<option value="">-- select a ticker --</option>${opts}`;
}

// navigateToTicker: switches to the Ticker Analysis view, pre-selects the
// ticker in the <select>, and invokes the adapter-supplied loader. Kept
// deliberately dumb -- no route table, no history stack beyond the hash.
function navigateToTicker(ticker, loadTickerDetailFn) {
  setCurrentTicker(ticker);
  switchView("ticker");
  const sel = document.getElementById("tickerSelect");
  if (sel) sel.value = ticker;
  loadTickerDetailFn(ticker);
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
}

// --- basic formatting helpers ----------------------------------------------

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

// Fixes the previous "n/a n/a" double-fallback: when price data truly is
// absent, show ONE clear line instead of stitching two separate n/a tokens
// together.
function fmtPriceLine(px) {
  if (!px || (px.last == null && px.chg_pct == null)) {
    return `<span class="px-unavailable">price data unavailable in this scenario</span>`;
  }
  const chg = px.chg_pct;
  const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
  const priceTxt = px.last != null ? px.last.toFixed(2) : "n/a";
  return `<span class="px">${priceTxt}</span><span class="pxchg ${chgClass}">${fmtPct(chg)}</span>`;
}

// --- Market Radar: cards / rows ---------------------------------------------

function renderCard(entry) {
  const px = entry.price || {};
  return `
    <div class="card" data-ticker="${esc(entry.ticker)}">
      <div class="card-head">
        <div><span class="tk">${esc(entry.ticker)}</span></div>
        <div>${fmtPriceLine(px)}</div>
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
      <button class="btn btn-investigate" data-investigate-ticker="${esc(entry.ticker)}">Investigate ${esc(entry.ticker)} &rarr;</button>
    </div>`;
}

function renderRow(entry) {
  const px = entry.price || {};
  return `
    <tr data-ticker="${esc(entry.ticker)}">
      <td><strong>${esc(entry.ticker)}</strong></td>
      <td><span class="tag state-${esc(entry.state)}">${esc(entry.state || "")}</span></td>
      <td>${esc(entry.confidence)}</td>
      <td class="col-headline">${esc(entry.headline)}</td>
      <td class="col-read">${esc(entry.current_read)}</td>
      <td><button class="btn-link btn-investigate" data-investigate-ticker="${esc(entry.ticker)}">Investigate &rarr;</button></td>
    </tr>`;
}

// Activity-summary strip above the grid. Built purely from fields the
// /api/radar response (and its static-demo capture) already has:
// scanned, highest.length, other.length -- no new backend fields.
function renderActivitySummary(data) {
  return `
    <div class="activity-strip">
      <div class="activity-stat"><span class="activity-num">${esc(data.scanned)}</span><span class="activity-label">tickers scanned</span></div>
      <div class="activity-stat"><span class="activity-num accent-bull">${esc((data.highest || []).length)}</span><span class="activity-label">highest attention</span></div>
      <div class="activity-stat"><span class="activity-num">${esc((data.other || []).length)}</span><span class="activity-label">other noteworthy</span></div>
    </div>`;
}

// Featured Signal: large treatment for the single highest-confidence /
// highest-attention entry (highest[0] -- tip_evidence.present() already
// orders "highest" strongest-first, see backend/app/core/tip_evidence.py
// rank_key()). Only real fields: label/categories/signal_type/confidence,
// current_read/headline/evidence. Nothing invented.
function renderFeaturedSignal(entry) {
  if (!entry) return `<div class="empty">No featured signal right now -- no ticker currently qualifies for Highest Attention.</div>`;
  const px = entry.price || {};
  const cats = (entry.categories || []).join(", ") || "n/a";
  return `
    <div class="featured-signal" data-ticker="${esc(entry.ticker)}">
      <div class="featured-head">
        <div class="featured-ticker">${esc(entry.ticker)}</div>
        <div class="featured-price">${fmtPriceLine(px)}</div>
      </div>
      <div class="tags">
        <span class="tag tag-lg">${esc(entry.label)}</span>
        <span class="tag state-${esc(entry.state)}">${esc(entry.state || "")}</span>
        <span class="tag">signal type: ${esc(entry.signal_type)}</span>
        <span class="tag">evidence categories: ${esc(cats)}</span>
      </div>
      <div class="featured-confidence conf-${esc(entry.confidence)}">
        <span class="conf-label">confidence</span>
        <span class="conf-value">${esc((entry.confidence || "").toUpperCase())}</span>
      </div>
      <div class="featured-headline">${esc(entry.headline)}</div>
      <div class="featured-read">${esc(entry.current_read)}</div>
      <button class="btn btn-investigate btn-featured-investigate" data-investigate-ticker="${esc(entry.ticker)}">Investigate ${esc(entry.ticker)} with AI &rarr;</button>
    </div>`;
}

// Signal Breakdown: contextual panel for whichever candidate is currently
// selected/featured -- shows its REAL evidence[] entries (family/text).
function renderSignalBreakdown(entry) {
  if (!entry) {
    return `<div class="breakdown-panel empty">Select a ticker above to see its evidence breakdown.</div>`;
  }
  const rows = (entry.evidence || []).map((e) => `
    <div class="breakdown-row">
      <span class="breakdown-family">${esc((e.family || "").replace(/_/g, " "))}</span>
      <span class="breakdown-text">${esc(e.text)}</span>
    </div>`).join("") || `<div class="breakdown-row"><span class="breakdown-text muted">No itemized evidence entries for this ticker.</span></div>`;
  return `
    <div class="breakdown-panel">
      <div class="breakdown-title">Signal Breakdown &mdash; ${esc(entry.ticker)}</div>
      ${rows}
    </div>`;
}

// --- Evidence classification (real fix, see signal_engine.py gate semantics) -
//
// A gate that did not pass is NOT automatically "conflicting evidence."
// Four real categories:
//   SUPPORTING      - passed=true on a gate whose metrics are a real market
//                      observation (dark_pool_anomaly, options_flow_anomaly,
//                      historical_percentile, persistence, price_confirmation,
//                      multi_signal_confirmation, catalyst_identified).
//   CONFLICTING     - price_confirmation specifically, when metrics.confirmed
//                      is explicitly false (price moved against the expected
//                      direction -- the one gate that can genuinely contradict).
//   UNMET_CRITERIA  - passed=false on a gate that simply found no qualifying
//                      observation this scan (an absent signal, not absent data).
//   DATA_LIMITATION - data_quality (always, both true and false -- it's
//                      provenance/meta, not market evidence), catalyst_identified
//                      when false (no catalyst manually tagged -- an input gap,
//                      not a market observation), historical_percentile when it
//                      was never evaluated (no today_premium supplied), and
//                      dark_pool_anomaly when its bootstrap flag is set.
const GATE_CATEGORY = {
  SUPPORTING: { key: "SUPPORTING", label: "Supporting evidence", cls: "gc-supporting" },
  CONFLICTING: { key: "CONFLICTING", label: "Conflicting observation", cls: "gc-conflicting" },
  UNMET_CRITERIA: { key: "UNMET_CRITERIA", label: "Unmet criteria this scan", cls: "gc-unmet" },
  DATA_LIMITATION: { key: "DATA_LIMITATION", label: "Data limitation / input gap", cls: "gc-datalimit" },
};

// Real limitation text sourced from signal_engine.py's own docstrings/notes
// (not newly-invented claims) -- surfaced next to the gate it documents.
const GATE_LIMITATION_NOTES = {
  historical_percentile: "This gate's percentile estimate is a SYNTHETIC static-bucket model (see synthetic_percentile_gate() in signal_engine.py), not the production trailing-window historical-percentile calculation -- it is not a claim about real historical rarity.",
  persistence: "Persistence counts qualifying prints seen so far in this app's own short local history; with only a few sessions of data, \"persistence\" cannot yet mean the same thing it does in production's longer trailing window.",
  dark_pool_anomaly: "When this gate's bootstrap flag is set, there isn't yet enough trailing history for a real baseline ratio/z-score -- the gate reports non-qualifying rather than asserting an anomaly.",
  catalyst_identified: "Not auto-computed from any data feed -- this gate is only true when a catalyst has been explicitly, manually tagged. False means absent input, not evidence against a catalyst.",
  data_quality: "Reflects API/bootstrap health for this scan, not a market observation about the ticker.",
};

function classifyGate(name, gate) {
  const passed = !!gate.passed;
  const m = gate.metrics || {};
  switch (name) {
    case "price_confirmation":
      if (m.confirmed === true) return GATE_CATEGORY.SUPPORTING;
      if (m.confirmed === false) return GATE_CATEGORY.CONFLICTING;
      return GATE_CATEGORY.DATA_LIMITATION; // never evaluated (no direction heuristic / no price move)
    case "dark_pool_anomaly":
      if (passed) return GATE_CATEGORY.SUPPORTING;
      if (m.bootstrap) return GATE_CATEGORY.DATA_LIMITATION;
      return GATE_CATEGORY.UNMET_CRITERIA;
    case "historical_percentile":
      if (passed) return GATE_CATEGORY.SUPPORTING;
      if (m.reason === "not evaluated (no today_premium supplied)") return GATE_CATEGORY.DATA_LIMITATION;
      return GATE_CATEGORY.UNMET_CRITERIA;
    case "options_flow_anomaly":
    case "persistence":
    case "multi_signal_confirmation":
      return passed ? GATE_CATEGORY.SUPPORTING : GATE_CATEGORY.UNMET_CRITERIA;
    case "catalyst_identified":
      return passed ? GATE_CATEGORY.SUPPORTING : GATE_CATEGORY.DATA_LIMITATION;
    case "data_quality":
      return GATE_CATEGORY.DATA_LIMITATION; // provenance/meta either way, never market evidence
    default:
      return passed ? GATE_CATEGORY.SUPPORTING : GATE_CATEGORY.UNMET_CRITERIA;
  }
}

function renderGateItem(name, gate) {
  const note = GATE_LIMITATION_NOTES[name]
    ? `<div class="gate-note">${esc(GATE_LIMITATION_NOTES[name])}</div>`
    : "";
  return `
    <div class="gate-item">
      <div class="gate-item-head">
        <span class="gname">${esc(name.replace(/_/g, " "))}</span>
        <span class="gpass ${gate.passed ? "yes" : "no"}">${gate.passed ? "PASS" : "no"}</span>
      </div>
      ${note}
    </div>`;
}

// Four visually distinct groups instead of one flat PASS/no list.
function renderGateGroups(gates) {
  const groups = {
    SUPPORTING: [], CONFLICTING: [], UNMET_CRITERIA: [], DATA_LIMITATION: [],
  };
  Object.entries(gates).forEach(([name, gate]) => {
    const c = classifyGate(name, gate);
    groups[c.key].push(renderGateItem(name, gate));
  });
  const order = ["SUPPORTING", "CONFLICTING", "UNMET_CRITERIA", "DATA_LIMITATION"];
  return order.map((key) => {
    const meta = GATE_CATEGORY[key];
    const items = groups[key];
    if (!items.length) return "";
    return `
      <div class="gate-group ${meta.cls}">
        <div class="gate-group-title">${esc(meta.label)} <span class="gate-group-count">(${items.length})</span></div>
        ${items.join("")}
      </div>`;
  }).join("") || `<div class="empty">No gates evaluated.</div>`;
}

// --- Ticker Analysis detail --------------------------------------------------

// Plain-language glosses for the internal state-machine value and
// transition-rule-id strings surfaced below. These are copy-only additions
// placed NEXT TO the technical value (never replacing it) -- see
// determine_transition()'s own rule-id strings in signal_engine.py, which
// this map mirrors 1:1 so it never invents a reason the engine didn't give.
const STATE_GLOSS = {
  NEW: "just started qualifying",
  BUILDING: "one qualifying signal so far, not yet confirmed",
  CONFIRMED: "multiple signals, or repeated qualifying days",
  STABLE: "confirmed and still qualifying after 3+ days",
  FADING: "was qualifying; no gate passed this scan",
  INVALIDATED: "price moved against the signal (terminal state)",
};

const TRANSITION_RULE_GLOSS = {
  first_qualifying_gate: "First time this ticker qualified.",
  multi_signal_or_persistence_on_first_reeval: "Two+ signals fired together, or this is at least the 2nd qualifying day.",
  single_signal_reevaluated: "Still only one signal firing on re-evaluation.",
  no_gate_passed_while_building: "No gate passed this scan while still BUILDING.",
  still_single_signal: "Still only one signal firing; stayed BUILDING.",
  no_gate_passed_while_confirmed: "No gate passed this scan while CONFIRMED.",
  still_confirmed: "Kept qualifying; stayed CONFIRMED.",
  no_gate_passed_while_stable: "No gate passed this scan while STABLE.",
  still_stable: "Kept qualifying; stayed STABLE.",
  reheated_multi_signal: "Multiple signals fired again after fading.",
  reheated_single_signal: "One signal fired again after fading.",
  still_fading: "Still no gate passing since fading started.",
  terminal_state: "INVALIDATED is terminal; no further automatic transitions.",
  no_rule_matched_state_unchanged: "No transition rule matched; state left unchanged.",
};

// --- Candlestick chart + evidence markers (Priority 1/2) --------------------
//
// Uses the vendored TradingView Lightweight Charts v4.2.0 (Apache 2.0,
// license header preserved in lightweight-charts.js) loaded as a global
// `LightweightCharts` before this file. Candle/marker data is ALWAYS
// supplied by the caller (app.live.js's /api/ticker/{t}/ohlc fetch, or
// app.static.js's data/ohlc/{t}.json load) -- this function renders
// whatever it is given and never fetches anything itself, consistent
// with the rest of this file's "no fetch() here" rule.

function dateOnly(ts) {
  return String(ts).slice(0, 10);
}

// Finds the latest candle date <= the marker's date (markers can land on
// a non-trading day -- e.g. an alert timestamped intraday maps onto that
// trading day's daily bar). Falls back to the first candle if the marker
// predates the whole series, rather than silently dropping it.
function nearestBarTime(markerDate, sortedCandleDates) {
  let best = null;
  for (const t of sortedCandleDates) {
    if (t <= markerDate) best = t;
    else break;
  }
  return best || sortedCandleDates[0] || null;
}

function renderChartUnavailable(reason) {
  const detail = reason && reason.message ? reason.message
    : reason && reason.status ? `UW status: ${reason.status}`
    : "No OHLC data available for this ticker in the current mode.";
  return `<div class="chart-unavailable">Chart data unavailable &mdash; ${esc(detail)}</div>`;
}

// Mounts (or re-renders, if called again) a candlestick chart with
// evidence markers into the DOM node with id `containerId`. `ohlc` is the
// exact shape returned by /api/ticker/{t}/ohlc (live) or
// data/ohlc/{t}.json (static demo) -- see backend/app/core/candles.py.
function mountCandlestickChart(containerId, ohlc) {
  const root = document.getElementById(containerId);
  if (!root) return;

  if (!ohlc || ohlc.unavailable || !ohlc.candles || !ohlc.candles.length) {
    root.innerHTML = renderChartUnavailable(ohlc ? ohlc.reason : null);
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    root.innerHTML = `<div class="chart-unavailable">Chart library failed to load (lightweight-charts.js missing or blocked).</div>`;
    return;
  }

  root.innerHTML = "";
  const chart = LightweightCharts.createChart(root, {
    height: 320,
    layout: { background: { color: "transparent" }, textColor: "#9aa4b2" },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.06)" },
      horzLines: { color: "rgba(255,255,255,0.06)" },
    },
    timeScale: { borderColor: "rgba(255,255,255,0.12)" },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.12)" },
  });
  const series = chart.addCandlestickSeries({
    upColor: "#22d3a8", downColor: "#ef4444", borderVisible: false,
    wickUpColor: "#22d3a8", wickDownColor: "#ef4444",
  });

  const bars = ohlc.candles.map((c) => ({
    time: dateOnly(c.t), open: c.o, high: c.h, low: c.l, close: c.c,
  }));
  series.setData(bars);
  const candleDates = bars.map((b) => b.time);

  const markers = (ohlc.markers || []).map((m) => {
    const barTime = nearestBarTime(dateOnly(m.time), candleDates);
    if (!barTime) return null;
    const text = m.price != null
      ? `${m.label} @ ${Number(m.price).toFixed(2)}`
      : `${m.label} (time only -- no price field in source data)`;
    return {
      time: barTime,
      position: m.kind === "darkpool_trade" ? "belowBar" : "aboveBar",
      color: m.kind === "darkpool_trade" ? "#60a5fa" : "#f59e0b",
      shape: "circle",
      text,
    };
  }).filter(Boolean);
  series.setMarkers(markers);
  chart.timeScale().fitContent();

  root._tipChart = chart;
  if (!root._tipResizeObserverAttached && typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (root._tipChart && root.clientWidth > 0) {
        root._tipChart.applyOptions({ width: root.clientWidth });
      }
    });
    ro.observe(root);
    root._tipResizeObserverAttached = true;
  }
}

function renderTickerDetail(d) {
  const gatesHtml = renderGateGroups(d.gates);
  const stateGloss = STATE_GLOSS[d.state] || "";
  const ruleGloss = TRANSITION_RULE_GLOSS[d.transition_rule] || "internal rule identifier";
  return `
    <div class="card chart-card">
      <div class="card-head"><strong>Price Chart &mdash; ${esc(d.ticker)}</strong>
        <span class="muted">evidence markers from the same flow_alerts/darkpool_trades the gates use</span></div>
      <div id="ohlcChartContainer" class="chart-container"><div class="empty">Loading chart...</div></div>
    </div>
    <div class="detail-grid">
      <div class="card">
        <div class="card-head"><span class="tk">${esc(d.ticker)}</span>
          <span class="tag state-${esc(d.state)}">${esc(d.state)}</span>
          ${stateGloss ? `<span class="muted state-gloss">(${esc(stateGloss)})</span>` : ""}
        </div>
        <div class="tags">
          <span class="tag">confidence: ${esc(d.confidence)}</span>
          <span class="tag">${esc(d.signal_type)}</span>
          <span class="tag">qualifying days: ${d.qualifying_days_count}</span>
        </div>
        <div class="read">${esc(d.headline)}</div>
        <div class="read">${esc(d.current_read)}</div>
        <div class="read">Transition: ${esc(d.previous_state || "none")} &rarr; ${esc(d.state)}</div>
        <div class="read">Confidence reason: ${esc(d.confidence_reason)}</div>
      </div>
      <div class="card">
        <div class="card-head"><strong>Named gates &mdash; evidence classification</strong></div>
        <p class="muted gate-group-intro">A gate that did not pass is not automatically "conflicting evidence." Grouped below by what it actually represents.</p>
        ${gatesHtml}
      </div>
    </div>
    <details class="how" style="margin-top:14px">
      <summary>Raw adapter data (what fed the gates above)</summary>
      <p class="muted">Transition rule id: <code>${esc(d.transition_rule)}</code> &mdash; ${esc(ruleGloss)}</p>
      <pre class="raw">${esc(JSON.stringify(d.raw, null, 2))}</pre>
    </details>`;
}

// --- Investigate with AI (bounded, optional) ---------------------------------
//
// Round-trip shape matches backend/app/ai/schemas.py's InvestigationResult:
// observed_facts (each tagged with source_tool), supporting_evidence,
// conflicting_observations, limitations, and a visually-separated
// ai_interpretation. Demo mode / PUBLIC_DEMO / no AI key always returns a
// static, clearly-labeled simulated transcript. This file never decides
// that distinction itself -- it only renders what it was given, with the
// "simulated" banner kept the MOST prominent element whenever simulated=true.

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

// Visible step sequence of the ACTUAL tools_used list. Per-tool
// success/failure detail is NOT part of backend/app/ai/schemas.py's
// InvestigationResult -- rather than inventing a field that doesn't
// exist, that absence is surfaced as an honest, explicit gap.
function renderToolsUsed(toolsUsed) {
  const tools = toolsUsed || [];
  if (!tools.length) {
    return `<div class="tools-used"><span class="tools-used-label">Tools queried:</span> <span class="muted">none</span></div>`;
  }
  const steps = tools.map((t, i) => `<li class="tool-step"><span class="tool-step-n">${i + 1}</span>${esc(t)}</li>`).join("");
  return `
    <div class="tools-used">
      <span class="tools-used-label">Tools queried (${tools.length}):</span>
      <ol class="tool-steps">${steps}</ol>
      <p class="muted tools-used-gap">Per-tool success/failure detail is not part of the API response schema
        (backend/app/ai/schemas.py::InvestigationResult) -- that is a real gap in what's surfaced today, not
        rendered here as if it existed.</p>
    </div>`;
}

function renderInvestigateResult(d) {
  const simBanner = d.simulated
    ? `<div class="ai-simulated-banner ai-simulated-banner-strong">
         <span class="ai-simulated-badge">SIMULATED</span>
         ${esc(d.simulated_label || "Simulated agent walkthrough -- not live UW data, not a real AI call")}
       </div>`
    : `<div class="ai-live-banner">LIVE: this investigation made real calls using your configured AI provider and UW key.
         Bounded, read-only tool selection and synthesis -- see README for the one documented gap:
         full LLM-directed tool-selection/synthesis has not yet been verified end-to-end with a real provider.</div>`;
  return `
    ${simBanner}
    ${renderToolsUsed(d.tools_used)}
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

// --- Settings view (shared markup; publicDemo flag controls disabled state) -
// Click-handler wiring (saveUwKey/testUwKey/etc.) is done by the live
// adapter after calling this; the static adapter calls it with publicDemo
// always true and wires nothing.

function renderModePill(mode, publicDemo) {
  const pill = document.getElementById("modePill");
  pill.classList.remove("mode-demo", "mode-live");
  if (mode === "live") {
    pill.textContent = "LIVE — Unusual Whales API";
    pill.classList.add("mode-live");
  } else {
    pill.textContent = publicDemo ? "DEMO MODE — Synthetic Data (public demo)" : "DEMO MODE — Synthetic Data";
    pill.classList.add("mode-demo");
  }
}

function renderSettingsView(d) {
  const root = document.getElementById("view-settings");
  const publicDemo = !!d.public_demo;
  const uw = d.uw || {};
  const ai = d.ai || {};

  const banner = publicDemo
    ? `<div class="public-demo-banner">Live UW mode and credential entry are available in the
       downloadable local edition — this public demo runs on synthetic data only.</div>`
    : "";

  // The real enforcement of "Live Mode requires a passed Test Connection"
  // lives server-side in the local-edition backend's mode-switch route
  // (rejects with HTTP 400 otherwise -- see backend/app/main.py's set_mode
  // handler + settings_store.is_key_verified; never called from here, this
  // file has zero fetch() calls, see the module docstring above). This
  // disables the Activate button as a UX nicety ONLY; it is never the
  // actual security boundary, and the server re-checks regardless of
  // whether this disabled attribute was somehow bypassed client-side.
  const liveDisabled = publicDemo || !uw.verified;
  const liveTitle = !publicDemo && !uw.verified
    ? "Activate Live Mode requires Test Connection to succeed for the current key first."
    : "";
  const modeSection = `
    <div class="settings-card">
      <h3>Mode</h3>
      <div class="settings-row">
        <button class="btn ${d.mode === "demo" ? "btn-active" : ""}" id="btnModeDemo" ${publicDemo ? "disabled" : ""}>Demo</button>
        <button class="btn ${d.mode === "live" ? "btn-active" : ""}" id="btnModeLive" ${liveDisabled ? "disabled" : ""} title="${esc(liveTitle)}">Activate Live Mode</button>
      </div>
      ${!publicDemo && !uw.verified ? `<p class="muted">Activate Live Mode is disabled until Test Connection succeeds for the current key.</p>` : ""}
    </div>`;

  const uwStatusLine = uw.configured
    ? `configured (source: ${esc(uw.source)}, ending •••${esc(uw.last4 || "")})${uw.verified ? " · verified (passed Test Connection)" : " · not yet verified"}${uw.last_test_result ? ` · last test: ${esc(uw.last_test_result)}` : ""}`
    : "not configured";
  const envDisclosure = uw.env_fallback_disclosure
    ? `<p class="muted env-disclosure">${esc(uw.env_fallback_disclosure)}</p>`
    : "";

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
        ${envDisclosure}
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
        <h3>AI / MCP (optional) <span class="muted">(real in the local edition)</span></h3>
        <p class="muted">Not available in the public demo build. The local edition supports a
          bounded, read-only "Investigate with AI" feature: tool-selection and synthesis via your
          configured AI provider when live. The full LLM-directed workflow has not yet been
          verified end-to-end with a real provider -- see the GitHub README's documented status.</p>
      </div>`
    : `<div class="settings-card">
        <h3>AI / MCP (optional)</h3>
        <p class="muted">Status: ${aiStatusLine}</p>
        <p class="muted">Bounded, read-only investigation: tool-selection and synthesis via your configured AI
          provider when live. The full LLM-directed tool-selection/synthesis workflow has not yet been verified
          end-to-end with a real provider -- see README.</p>
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

  root.innerHTML = `${banner}${modeSection}${uwSection}${aiSection}`;
  return { publicDemo };
}

// Wires click-anywhere "Investigate" affordances (cards/rows on Market
// Radar) to navigateToTicker, given the adapter's own loadTickerDetail.
// Call this once after rendering the radar grid/table.
function wireInvestigateButtons(loadTickerDetailFn) {
  document.querySelectorAll("[data-investigate-ticker]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      navigateToTicker(btn.dataset.investigateTicker, loadTickerDetailFn);
    });
  });
}
