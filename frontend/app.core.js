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
const TIP_STATE = { currentTicker: null, currentView: "radar" };

// --- URL/history scheme (P0 navigation fix) --------------------------------
//
// Format: a hash of the form `#view=<radar|ticker|settings|about>&ticker=<T>`
// (ticker only present/meaningful when view=ticker). Chosen over a real
// query string (`?view=...`) because BOTH editions (frontend/ and the
// zero-backend static-demo/) are single index.html files with no
// server-side route handling -- a hash change never issues a network
// request and always resolves against the one index.html that's already
// loaded, so a freshly-pasted deep link or a refresh behaves identically
// whether or not there's a real server behind it. A bare query string
// would 404 on static-demo/ hosting without URL-rewrite rules it doesn't
// have. This extends (does not replace) the prior session's bare
// `#ticker=XXX` convention: that exact old format is still recognized on
// load (see parseNavHash) so old bookmarks/links keep working.
const NAV_VIEWS = ["radar", "ticker", "settings", "about"];

function parseNavHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) return { view: null, ticker: null };
  const params = new URLSearchParams(raw);
  // Legacy format from the prior session: a bare `#ticker=XXX` with no
  // `view=` key at all.
  if (!params.has("view") && params.has("ticker")) {
    const t = params.get("ticker");
    return { view: "ticker", ticker: t ? t.toUpperCase() : null };
  }
  let view = params.get("view");
  if (!NAV_VIEWS.includes(view)) view = null;
  const ticker = params.get("ticker");
  return { view, ticker: ticker ? ticker.toUpperCase() : null };
}

function buildNavHash(view, ticker) {
  const params = new URLSearchParams();
  params.set("view", view);
  if (view === "ticker" && ticker) params.set("ticker", ticker);
  return `#${params.toString()}`;
}

// pushNav: a REAL navigation (nav-item click, Investigate click, ticker
// select+Analyze) -- creates a new history entry so Back/Forward has
// somewhere to go. replaceNav: an in-place update to the same logical view
// (bootstrap-time URL normalization, popstate-driven restores, and the
// mode-switch ticker-invalidation case below) -- never adds a Back stop.
function pushNav(view, ticker) {
  TIP_STATE.currentView = view;
  TIP_STATE.currentTicker = ticker || null;
  try {
    history.pushState({ view, ticker: ticker || null }, "", buildNavHash(view, ticker));
  } catch (e) {
    /* history update is a convenience, never fatal */
  }
}

function replaceNav(view, ticker) {
  TIP_STATE.currentView = view;
  TIP_STATE.currentTicker = ticker || null;
  try {
    history.replaceState({ view, ticker: ticker || null }, "", buildNavHash(view, ticker));
  } catch (e) {
    /* history update is a convenience, never fatal */
  }
}

// navigateToView: nav-item clicks (Market Radar / Ticker Analysis /
// Settings / About). Pushes a new entry by default; pass push:false for
// the mode-switch-invalidation case, which must REPLACE the current entry
// rather than add a new one.
function navigateToView(view, opts = {}) {
  const { push = true } = opts;
  const ticker = view === "ticker" ? TIP_STATE.currentTicker : null;
  if (push) pushNav(view, ticker); else replaceNav(view, ticker);
  switchView(view);
}

// formatAgo: compact relative-time string for a unix-seconds timestamp
// (e.g. Market Radar's last_scan_at) -- production-inspired pattern
// (its own `ago()` helper), reimplemented here rather than imported
// since production's version isn't a standalone module.
function formatAgo(unixSeconds) {
  if (unixSeconds == null) return "never";
  const deltaS = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (deltaS < 60) return "just now";
  if (deltaS < 3600) return `${Math.floor(deltaS / 60)}m ago`;
  if (deltaS < 86400) return `${Math.floor(deltaS / 3600)}h ago`;
  return `${Math.floor(deltaS / 86400)}d ago`;
}

// renderTickerUnavailable: shown in #tickerDetail when the URL names a
// ticker that doesn't exist in the current mode's universe/scenario set
// (typo, or a ticker valid in Demo but not Live or vice versa) -- never a
// silent blank page or a thrown error. The already-rendered #tickerSelect
// above (populated with every currently-valid ticker) is "a way to pick a
// valid one"; this message doesn't duplicate that control.
function renderTickerUnavailable(ticker) {
  return `<div class="empty">"${esc(ticker)}" is not available in demo mode. Use the ticker search above to pick a valid scenario.</div>`;
}

// applyNavState: the one shared function both adapters' bootstrap AND
// popstate handler call to make the DOM match a parsed {view, ticker}
// state. `handlers.getKnownTickers()` returns the adapter's current list
// of valid tickers (may be null before Radar/scenarios have loaded, in
// which case ticker validity isn't checked yet). `push` controls whether
// this resulting state is pushed (never used here -- bootstrap/popstate
// both normalize via replace) or replaced; kept as a parameter for clarity
// at call sites rather than hardcoding replaceNav calls three times.
function applyNavState(state, handlers, opts = {}) {
  const { push = false } = opts;
  const setHistory = (view, ticker) => { if (push) pushNav(view, ticker); else replaceNav(view, ticker); };

  if (!state || !state.view || state.view === "radar") {
    setHistory("radar", null);
    switchView("radar");
    return;
  }
  if (state.view === "settings" || state.view === "about") {
    setHistory(state.view, null);
    switchView(state.view);
    return;
  }
  // state.view === "ticker"
  if (!state.ticker) {
    setHistory("ticker", null);
    switchView("ticker");
    return;
  }
  const known = handlers && handlers.getKnownTickers ? handlers.getKnownTickers() : null;
  // REAL BUG FIX (2026-10-14): `known` only ever contains Market Radar's
  // scanned/scenario tickers -- it was never meant to be a hard allowlist
  // for arbitrary Live Mode lookups (see the backend fix in main.py's
  // ticker_analysis()/ticker_ohlc(), which already accepts any format-
  // valid ticker in Live Mode). Without this, a manually-typed ticker
  // worked on click but broke on refresh/deep-link, since this function
  // runs on both. `isArbitraryTickerAllowed` (Live Mode only, set by
  // app.live.js) lets a format-valid ticker through here the same way.
  const allowArbitrary = handlers && handlers.isArbitraryTickerAllowed && handlers.isArbitraryTickerAllowed();
  const formatValid = /^[A-Z]{1,5}(\.[A-Z])?$/.test(state.ticker || "");
  if (known && !known.includes(state.ticker) && !(allowArbitrary && formatValid)) {
    setHistory("ticker", null);
    switchView("ticker");
    const el = document.getElementById("tickerDetail");
    if (el) el.innerHTML = renderTickerUnavailable(state.ticker);
    const inv = document.getElementById("investigateSection");
    if (inv) inv.innerHTML = "";
    return;
  }
  navigateToTicker(state.ticker, handlers.loadTickerDetail, { push });
}

// wireHistoryHandling: call once at adapter bootstrap AFTER the adapter's
// own `window.TIP_NAV_HANDLERS` has been assigned. Handles Back/Forward:
// popstate always REPLACEs-in-place (the browser already moved the
// history pointer; we're just making the DOM agree with it), and the
// existing request-token/sequence-number guard in each adapter's
// loadTickerDetail (see app.live.js _navSeq) is exercised the same way
// here as any other navigateToTicker call, so a late response for a
// ticker the user has since navigated away from via Back/Forward is
// still dropped, not rendered.
function wireHistoryHandling() {
  window.addEventListener("popstate", (ev) => {
    const state = (ev.state && ev.state.view) ? ev.state : parseNavHash(location.hash);
    applyNavState(state, window.TIP_NAV_HANDLERS, { push: false });
  });
}

// clearTickerView: the one place that resets ALL state tied to "whichever
// ticker is currently loaded" -- TIP_STATE.currentTicker, the <select>
// (back to its placeholder), and the two DOM panes that render
// ticker-specific content (#tickerDetail, #investigateSection). Needed
// whenever the underlying data source changes out from under an
// already-rendered ticker (e.g. a Demo<->Live mode switch) so stale
// content from the OLD mode/adapter never stays on screen referencing a
// ticker that may not even exist in the new mode. Deliberately does NOT
// touch history itself -- the caller (app.live.js's setMode) decides
// whether/how to replace the current history entry, since that depends on
// which view the user was on (see setMode's comment). Both adapters can
// call this; the static demo has no mode switch so it currently has no
// caller, but it is kept here (not duplicated) since app.core.js is the
// one shared, byte-identical file.
function clearTickerView() {
  TIP_STATE.currentTicker = null;
  setTickerComboValue("");
  const detailEl = document.getElementById("tickerDetail");
  if (detailEl) detailEl.innerHTML = `<div class="empty">Select a ticker to analyze.</div>`;
  const investigateEl = document.getElementById("investigateSection");
  if (investigateEl) investigateEl.innerHTML = "";
}

// --- Unified ticker combobox (2026-10-14) -----------------------------
//
// REPLACES the old separate <select id="tickerSelect"> dropdown + a
// second manual-entry <input> pair (Chad tested both and found the
// combination awkward/inconsistent with the rest of TIP). ONE editable,
// searchable text field now does both jobs: pick a suggested ticker OR
// type any symbol directly. Shared between the local (app.live.js) and
// static-demo (app.static.js) editions -- both wire it the same way,
// only their `onSubmit` validation differs (Live allows an arbitrary
// format-valid ticker; Demo's unknown-ticker rejection already happens
// downstream in applyNavState/the 404 path, unchanged).
//
// Persistence: "recently analyzed" is genuinely a client-side concept
// (which tickers THIS browser looked at), so it lives in localStorage,
// not the backend -- guarded with try/catch since some browsers refuse
// storage access entirely (private mode, locked-down embeds) and that
// must never break ticker analysis itself, just silently skip the
// recency feature.
const RECENT_TICKERS_KEY = "tip_recent_tickers";
const MAX_RECENT_TICKERS = 8;
const MAX_SUGGESTIONS_SHOWN = 8;

function getRecentTickers() {
  try {
    const raw = localStorage.getItem(RECENT_TICKERS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr.filter((t) => typeof t === "string") : [];
  } catch (e) {
    return [];
  }
}

function addRecentTicker(ticker) {
  if (!ticker) return;
  try {
    const existing = getRecentTickers().filter((t) => t !== ticker);
    const updated = [ticker, ...existing].slice(0, MAX_RECENT_TICKERS);
    localStorage.setItem(RECENT_TICKERS_KEY, JSON.stringify(updated));
  } catch (e) {
    /* storage unavailable -- recency suggestions just won't appear, not fatal */
  }
}

// Module-level combo state -- one combo exists per page (Ticker Analysis
// has exactly one), so this doesn't need to be instance-scoped.
let _comboKnownTickers = [];
let _comboFilteredSuggestions = [];
let _comboActiveIndex = -1;
let _comboOnSubmit = null;

// setTickerComboOptions: sets the pool of "known" suggestable tickers
// (Radar-qualifying + full scenario/watchlist universe) -- replaces the
// old renderTickerOptions()/<select>.innerHTML assignment. Does NOT
// touch the input's current typed value or open the dropdown.
function setTickerComboOptions(tickers) {
  _comboKnownTickers = Array.from(new Set(tickers)).sort();
}

// setTickerComboValue: sets the input's displayed text only (used by
// navigateToTicker/clearTickerView on bootstrap/popstate/programmatic
// navigation) -- never opens the suggestion dropdown or triggers onSubmit.
function setTickerComboValue(ticker) {
  const input = document.getElementById("tickerInput");
  if (input) input.value = ticker || "";
}

function _comboPool() {
  // Recent tickers first (most-recently-analyzed first), then the
  // remaining known tickers alphabetically -- deduped either way.
  const seen = new Set();
  const pool = [];
  for (const t of getRecentTickers()) {
    if (!seen.has(t)) { pool.push({ ticker: t, recent: true }); seen.add(t); }
  }
  for (const t of _comboKnownTickers) {
    if (!seen.has(t)) { pool.push({ ticker: t, recent: false }); seen.add(t); }
  }
  return pool;
}

function _comboFilter(query) {
  const q = (query || "").trim().toUpperCase();
  const pool = _comboPool();
  const matched = q ? pool.filter((e) => e.ticker.toUpperCase().startsWith(q)) : pool;
  return matched.slice(0, MAX_SUGGESTIONS_SHOWN);
}

function _comboRenderSuggestions() {
  const list = document.getElementById("tickerSuggestions");
  const input = document.getElementById("tickerInput");
  if (!list) return;
  if (!_comboFilteredSuggestions.length) {
    list.hidden = true;
    list.innerHTML = "";
    if (input) input.setAttribute("aria-expanded", "false");
    return;
  }
  list.hidden = false;
  if (input) input.setAttribute("aria-expanded", "true");
  list.innerHTML = _comboFilteredSuggestions.map((e, i) => `
    <li role="option" data-ticker="${esc(e.ticker)}" class="${i === _comboActiveIndex ? "active" : ""}"
        aria-selected="${i === _comboActiveIndex}">
      <span>${esc(e.ticker)}</span>${e.recent ? '<span class="ticker-suggestion-tag">recent</span>' : ""}
    </li>`).join("");
}

function _comboOpen(query) {
  _comboFilteredSuggestions = _comboFilter(query);
  _comboActiveIndex = -1;
  _comboRenderSuggestions();
}

function _comboClose() {
  _comboFilteredSuggestions = [];
  _comboActiveIndex = -1;
  _comboRenderSuggestions();
}

function _comboSelect(ticker) {
  setTickerComboValue(ticker);
  _comboClose();
  if (_comboOnSubmit) _comboOnSubmit(ticker);
}

// initTickerCombo: wires the combobox's events exactly once. `onSubmit`
// is called with the raw text to analyze whenever the user presses Enter
// (with no suggestion highlighted), clicks a suggestion, or clicks the
// Analyze button -- callers (app.live.js/app.static.js) own validation
// (Live Mode's arbitrary-ticker format check; Demo's unknown-ticker
// rejection already happens downstream, unchanged) and the actual
// navigateToTicker() call.
function initTickerCombo(onSubmit) {
  _comboOnSubmit = onSubmit;
  const input = document.getElementById("tickerInput");
  const list = document.getElementById("tickerSuggestions");
  const analyzeBtn = document.getElementById("loadTicker");
  if (!input || !list) return;

  input.addEventListener("input", () => _comboOpen(input.value));
  input.addEventListener("focus", () => _comboOpen(input.value));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (!_comboFilteredSuggestions.length) { _comboOpen(input.value); return; }
      _comboActiveIndex = (_comboActiveIndex + 1) % _comboFilteredSuggestions.length;
      _comboRenderSuggestions();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!_comboFilteredSuggestions.length) return;
      _comboActiveIndex = (_comboActiveIndex - 1 + _comboFilteredSuggestions.length) % _comboFilteredSuggestions.length;
      _comboRenderSuggestions();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (_comboActiveIndex >= 0 && _comboFilteredSuggestions[_comboActiveIndex]) {
        _comboSelect(_comboFilteredSuggestions[_comboActiveIndex].ticker);
      } else {
        _comboClose();
        if (_comboOnSubmit) _comboOnSubmit(input.value);
      }
    } else if (ev.key === "Escape") {
      _comboClose();
    }
  });
  // mousedown (not click) on a suggestion: fires BEFORE the input's blur
  // event, so the list is still open and readable when the selection is
  // made -- a plain click handler here would lose the race to blur's
  // close, a classic combobox pitfall.
  list.addEventListener("mousedown", (ev) => {
    const li = ev.target.closest("li[data-ticker]");
    if (!li) return;
    ev.preventDefault();
    _comboSelect(li.dataset.ticker);
  });
  input.addEventListener("blur", () => {
    // Small delay so a suggestion's mousedown (which also blurs the
    // input) still gets to run its handler before the list disappears.
    setTimeout(_comboClose, 120);
  });
  if (analyzeBtn) {
    analyzeBtn.addEventListener("click", () => {
      _comboClose();
      if (_comboOnSubmit) _comboOnSubmit(input.value);
    });
  }
}

// navigateToTicker: switches to the Ticker Analysis view, pre-selects the
// ticker in the <select>, records the navigation in history (push by
// default; pass push:false for a non-navigating restore e.g. bootstrap/
// popstate), and invokes the adapter-supplied loader. opts.focusInvestigate
// scrolls #investigateSection into view once loadTickerDetailFn's promise
// resolves -- used by the Radar/Ticker-Analysis "Investigate with AI"
// affordances so that entry point visibly lands on the Investigate section
// rather than just the top of Ticker Analysis (there is no separate
// Investigate view to navigate to; it's a section within this one).
function navigateToTicker(ticker, loadTickerDetailFn, opts = {}) {
  const { push = true, focusInvestigate = false } = opts;
  if (push) pushNav("ticker", ticker); else replaceNav("ticker", ticker);
  switchView("ticker");
  setTickerComboValue(ticker);
  // Record every successfully-navigated ticker as "recently analyzed"
  // regardless of entry point (combo submit, a suggestion click, Market
  // Radar's row/Investigate links, a deep link/refresh) -- the recency
  // list should reflect everything this browser actually looked at, not
  // just one specific UI path into this same function.
  addRecentTicker(ticker);
  const result = loadTickerDetailFn(ticker);
  if (focusInvestigate && result && typeof result.then === "function") {
    result.then(() => {
      const el = document.getElementById("investigateSection");
      if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
  return result;
}

function switchView(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  TIP_STATE.currentView = view;
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

// P1c (UX-correction pass): consistent card height/hierarchy -- ticker,
// current state, confidence, ONE primary signal/headline line, the
// compact P0 lifecycle summary, then Investigate. The prior version
// rendered the full entry.evidence[] list inline (2-4 items per card,
// variable length, and one of those items was routinely just "TIP signal
// state NEW" -- a plain duplicate of the state tag already shown above
// it) plus a separate current_read line; that is exactly the kind of
// per-card length variance and duplicated content Chad flagged. The full
// per-category evidence remains real and available -- in the Signal
// Breakdown panel (click-through, renderSignalBreakdown, unchanged) and
// in Ticker Analysis's full 4-way classification -- just not duplicated
// inline on every card in the grid.
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
      <div class="read card-headline">${esc(entry.headline)}</div>
      ${renderHistoryChip(entry.history_states)}
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

// --- Stage 2 (P3) Market Radar enhancement: evidence-family / signal-
// state filters, compact per-card signal-history, and a clear
// all-scanned-vs-qualifying-shown count. Pure client-side filtering over
// data already present in the /api/radar response -- no new backend
// endpoint, no new fetch. See fixtures/scenario_persistence_progression.json
// / backend/app/seed_demo_history.py for the one scenario (PRSX) that
// genuinely has multi-entry history to show; every other ticker's
// history_states is [] on a fresh process (real, not fabricated).

// Same evidence-family vocabulary as backend/app/core/tip_evidence.py's
// CATEGORY_LABELS, plus "multi-signal" -- a derived tag (not a backend
// category) for any entry with 2+ distinct categories, matching
// dominant_label()'s own MULTI-SOURCE definition.
const RADAR_FAMILY_LABELS = {
  dark_pool: "Dark pool", options: "Options", uw_flow: "UW flow", "multi-signal": "Multi-signal",
};

function entryFamilyTags(entry) {
  const cats = entry.categories || [];
  const tags = [...cats];
  if (cats.length >= 2 && !tags.includes("multi-signal")) tags.push("multi-signal");
  return tags;
}

function filterRadarEntries(entries, familyFilter, stateFilter) {
  return (entries || []).filter((e) => {
    if (familyFilter && !entryFamilyTags(e).includes(familyFilter)) return false;
    if (stateFilter && e.state !== stateFilter) return false;
    return true;
  });
}

// Compact signal-history indicator: only rendered when the caller
// genuinely has 2+ real recorded states for this ticker (entry.history_states
// from /api/radar, sourced from backend/app/storage.py's real scan log --
// see main.py's radar() _attach()). An empty/short list renders nothing,
// never a fabricated single-step "history".
// --- Shared history collapsing (UX-correction pass) -------------------------
//
// Root cause of the "dozens of repeated badges" complaint: both the Radar
// history chip (above) and the Ticker Analysis lifecycle timeline (below)
// used to render ONE element per raw scan-log row with zero collapsing,
// and on a long-running dev instance every manual/test GET /api/ticker/{t}
// triggers a real scan+log write, so the raw list only grows across
// sessions. collapseHistory() is the single shared fix: a pure function
// that walks the REAL history array (accepts either Radar's bare state-
// string list [{...}] or Ticker Analysis's richer
// {scan_date, any_gate_passed, state, created_at} objects) and merges
// consecutive entries with the SAME state into one segment. A genuine
// transition (state actually changes) always starts a new segment -- this
// never fabricates or merges across a real transition, and every raw
// entry is retained verbatim inside its segment's `entries` array so
// re-expanding (the <details> panels below) always shows the exact real
// data, never a derived approximation.
function collapseHistory(history) {
  const entries = Array.isArray(history) ? history : [];
  const segments = [];
  entries.forEach((raw) => {
    const e = (typeof raw === "string") ? { state: raw } : (raw || {});
    const last = segments[segments.length - 1];
    if (last && last.state === e.state) {
      last.scanCount += 1;
      if (e.scan_date != null) last.lastDate = e.scan_date;
      if (e.created_at != null) last.lastCreatedAt = e.created_at;
      last.entries.push(e);
    } else {
      segments.push({
        state: e.state,
        firstDate: e.scan_date ?? null,
        lastDate: e.scan_date ?? null,
        firstCreatedAt: e.created_at ?? null,
        lastCreatedAt: e.created_at ?? null,
        scanCount: 1,
        entries: [e],
      });
    }
  });
  return segments;
}

// Short "Oct 10" style label from a real ISO scan_date -- never invents a
// date when one wasn't supplied (Radar's history_states has none today).
function fmtSegDate(iso) {
  if (!iso) return null;
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

// One segment -> "CONFIRMED (8 scans, Oct 11-13, current)" / "NEW (1 scan)"
// when no real dates exist. `current` is only ever true for the LAST real
// segment of a non-empty history -- never asserted elsewhere.
function formatSegmentLabel(seg, isCurrent) {
  const scanTxt = seg.scanCount === 1 ? "1 scan" : `${seg.scanCount} scans`;
  const d1 = fmtSegDate(seg.firstDate);
  const d2 = fmtSegDate(seg.lastDate);
  let dateTxt = "";
  if (d1 && d2) dateTxt = d1 === d2 ? `, ${d1}` : `, ${d1}\u2013${d2}`;
  return `${seg.state} (${scanTxt}${dateTxt}${isCurrent ? ", current" : ""})`;
}

// Compact horizontal progression over COLLAPSED segments -- shared by both
// the Radar history chip and the Ticker Analysis lifecycle summary.
// maxVisible caps how many segments render directly; anything older is
// folded into a single "+N earlier" affordance rather than ever dumping
// the full list into the main page flow. Segments are chronological
// oldest-first (see storage.py's history() ORDER BY id ASC), so the
// "earlier" ones trimmed are the OLDEST, and the rightmost rendered
// segment is always the most recent (current) state.
function renderSegmentProgression(segments, { maxVisible = 5 } = {}) {
  if (!segments.length) return "";
  const hiddenCount = Math.max(0, segments.length - maxVisible);
  const visible = hiddenCount > 0 ? segments.slice(segments.length - maxVisible) : segments;
  const moreChip = hiddenCount > 0
    ? `<span class="hp-more" title="${hiddenCount} earlier segment(s) not shown -- see full history below">+${hiddenCount} earlier</span><span class="hc-arrow">\u2192</span>`
    : "";
  const steps = visible.map((seg, i) => {
    const isCurrent = i === visible.length - 1;
    return `<span class="hc-step state-${esc(seg.state)}">${esc(formatSegmentLabel(seg, isCurrent))}</span>${i < visible.length - 1 ? '<span class="hc-arrow">\u2192</span>' : ""}`;
  }).join("");
  return `<div class="history-chip" title="Collapsed from ${segments.reduce((n, s) => n + s.scanCount, 0)} real recorded scan(s)">${moreChip}${steps}</div>`;
}

// Radar card chip: unchanged behavior for <2 raw entries (renders nothing
// -- never a fabricated single-step "history"); 2+ real entries now render
// as a collapsed, capped progression instead of one badge per raw scan.
function renderHistoryChip(historyStates) {
  if (!historyStates || historyStates.length < 2) return "";
  const segments = collapseHistory(historyStates);
  return renderSegmentProgression(segments, { maxVisible: 5 });
}

function renderRadarFilterOptions(allEntries) {
  const families = new Set();
  const states = new Set();
  (allEntries || []).forEach((e) => {
    entryFamilyTags(e).forEach((f) => families.add(f));
    if (e.state) states.add(e.state);
  });
  const famOpts = `<option value="">All evidence families</option>` +
    Array.from(families).sort().map((f) => `<option value="${esc(f)}">${esc(RADAR_FAMILY_LABELS[f] || f)}</option>`).join("");
  const stateOpts = `<option value="">All signal states</option>` +
    Array.from(states).sort().map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  return { famOpts, stateOpts };
}

// Re-renders Highest Attention / Other Noteworthy from the SAME already-
// fetched /api/radar payload (never a new fetch), applying the current
// filter selection. Also keeps the legacy #otherTable (hidden via CSS,
// see styles.css) populated unfiltered, since app.live.js/app.static.js's
// older DOM-stub-based test harness still queries it directly.
function renderRadarLists(data, filters, loadTickerDetailFn) {
  const familyFilter = (filters && filters.family) || "";
  const stateFilter = (filters && filters.state) || "";
  const allHighest = data.highest || [];
  const allOther = data.other || [];
  const featured = allHighest[0] || null;
  const restOfHighest = allHighest.slice(1);
  const filteredRest = filterRadarEntries(restOfHighest, familyFilter, stateFilter);
  const filteredOther = filterRadarEntries(allOther, familyFilter, stateFilter);

  const highestGridEl = document.getElementById("highestGrid");
  if (highestGridEl) highestGridEl.innerHTML = filteredRest.map(renderCard).join("") || "";
  const otherGridEl = document.getElementById("otherGrid");
  if (otherGridEl) otherGridEl.innerHTML = filteredOther.map(renderCard).join("") || "";
  const tbody = document.querySelector("#otherTable tbody");
  if (tbody) tbody.innerHTML = allOther.map(renderRow).join("");

  // Counts: "qualifying" (highest+other, i.e. tip_evidence.qualifies()
  // entries) is always kept distinct from "scanned" (every synthetic
  // scenario including non-qualifying controls like NADA/DRFT) -- never
  // blur the two. When a filter is active, also show how many of the
  // qualifying set are currently shown.
  const totalQualifying = allHighest.length + allOther.length;
  const totalShown = (featured ? 1 : 0) + filteredRest.length + filteredOther.length;
  const countsEl = document.getElementById("radarFilterCounts");
  if (countsEl) {
    countsEl.textContent = (familyFilter || stateFilter)
      ? `Showing ${totalShown} of ${totalQualifying} qualifying signal(s) after filters \u2014 ${data.scanned} scenario(s) scanned in total (including non-qualifying controls)`
      : `${totalQualifying} qualifying signal(s) shown \u2014 ${data.scanned} scenario(s) scanned in total (including non-qualifying controls)`;
  }
  const radarEmptyEl = document.getElementById("radarEmpty");
  if (radarEmptyEl) {
    radarEmptyEl.textContent = (familyFilter || stateFilter) && totalQualifying > 0 && totalShown === 0
      ? "No qualifying signals match the current filters."
      : "No tickers qualify right now.";
    radarEmptyEl.style.display = (totalShown === 0) ? "block" : "none";
  }

  wireInvestigateButtons(loadTickerDetailFn);
  document.querySelectorAll("[data-ticker]").forEach((card) => {
    card.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-investigate-ticker]")) return;
      const t = card.dataset.ticker;
      const entry = [...allHighest, ...allOther].find((e) => e.ticker === t);
      if (entry) {
        const slot = document.getElementById("breakdownSlot");
        if (slot) slot.innerHTML = renderSignalBreakdown(entry);
      }
    });
  });
}

// Builds the filter <select> option lists from the full qualifying pool,
// wires their onchange to re-render via renderRadarLists (no re-fetch),
// and renders the initial (unfiltered) state. Call once per loadRadar().
function setupRadarFiltersAndRender(data, loadTickerDetailFn) {
  const allEntries = [...(data.highest || []), ...(data.other || [])];
  const { famOpts, stateOpts } = renderRadarFilterOptions(allEntries);
  const famSel = document.getElementById("filterFamily");
  const stateSel = document.getElementById("filterState");
  if (famSel) famSel.innerHTML = famOpts;
  if (stateSel) stateSel.innerHTML = stateOpts;
  const rerender = () => renderRadarLists(data, {
    family: famSel ? famSel.value : "",
    state: stateSel ? stateSel.value : "",
  }, loadTickerDetailFn);
  if (famSel) famSel.onchange = rerender;
  if (stateSel) stateSel.onchange = rerender;
  rerender();
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
      ${renderHistoryChip(entry.history_states)}
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
  historical_percentile: "This gate's percentile estimate is a SYNTHETIC static-bucket model, not the production trailing-window historical-percentile calculation -- it is not a claim about real historical rarity.",
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

// gateWhyItMatters: a plain-English "why this matters" sentence computed
// from the gate's OWN real metrics (never a canned string unrelated to
// the actual numbers) -- this is the fix for the known gap where the
// CONFLICTING group rendered with no explanation. Every branch below
// reads values straight out of `gate.metrics` (price_move_pct,
// direction_heuristic, ratio, z_score, percentile, etc.) exactly as
// evaluate_gates() in signal_engine.py computed them; nothing here is
// invented or specific to any one scenario ticker.
function gateWhyItMatters(name, gate) {
  const m = gate.metrics || {};
  switch (name) {
    case "price_confirmation": {
      const pct = typeof m.price_move_pct === "number" ? Math.abs(m.price_move_pct).toFixed(1) : null;
      const leanWord = m.direction_heuristic === "bullish-lean" ? "bullish"
        : m.direction_heuristic === "bearish-lean" ? "bearish" : null;
      if (m.confirmed === true && leanWord && pct != null) {
        const moveWord = m.price_move_pct > 0 ? `rose ${pct}%` : `fell ${pct}%`;
        return `Flow suggested a ${leanWord} lean, and the stock actually ${moveWord} -- price action moved WITH the flow lean.`;
      }
      if (m.confirmed === false && leanWord && pct != null) {
        const moveWord = m.price_move_pct > 0 ? `rose ${pct}%` : `fell ${pct}%`;
        return `Options/dark-pool flow suggested a ${leanWord} lean, but the stock actually ${moveWord} -- these observations point in opposite directions.`;
      }
      return "No price-confirmation check could be run this scan (missing a price move or a directional lean to compare it against).";
    }
    case "dark_pool_anomaly": {
      if (gate.passed) {
        const ratioTxt = typeof m.ratio === "number" ? `${m.ratio.toFixed(1)}x its baseline` : "well above its baseline";
        const zTxt = typeof m.z_score === "number" ? ` (z=${m.z_score.toFixed(1)})` : "";
        return `Dark-pool print volume measured ${ratioTxt}${zTxt} -- a statistical anomaly, not just a large raw number.`;
      }
      if (m.bootstrap) return "Not enough trailing history yet to compute a real baseline ratio/z-score for this gate.";
      return "Dark-pool print volume stayed within its normal baseline range this scan.";
    }
    case "options_flow_anomaly":
      return gate.passed
        ? "Options order flow showed a pattern flagged as unusual this scan (see raw metrics in the details below)."
        : "No unusual options order-flow pattern was observed this scan.";
    case "historical_percentile": {
      const pctile = typeof m.percentile === "number" ? `${(m.percentile * 100).toFixed(0)}th percentile` : null;
      if (gate.passed && pctile) return `Today's premium ranks in the ${pctile} of the trailing ${m.window_days || 30}-day window.`;
      if (m.reason === "not evaluated (no today_premium supplied)") return "This gate was not evaluated -- no premium figure was supplied for this scan.";
      return pctile ? `Today's premium ranked only in the ${pctile} of its trailing window -- not high enough to qualify.` : "Today's premium did not rank high enough in its trailing window to qualify.";
    }
    case "persistence": {
      const n = m.tier2_qualifying_prints || 0;
      const pct = typeof m.tier2_max_pct === "number" ? `${(m.tier2_max_pct * 100).toFixed(0)}%` : "n/a";
      return gate.passed
        ? `${n} qualifying print(s) seen so far, the largest at ${pct} of that day's volume.`
        : "No qualifying persistent print pattern (repeated large prints) was seen this scan.";
    }
    case "multi_signal_confirmation": {
      const contrib = m.contributing_gates || [];
      return gate.passed
        ? `${contrib.length} independent gates fired together this scan: ${contrib.map((g) => g.replace(/_/g, " ")).join(", ")}.`
        : "Fewer than 2 independent gates fired together this scan.";
    }
    case "catalyst_identified":
      return gate.passed
        ? "A real catalyst was explicitly tagged for this ticker."
        : "No catalyst has been manually tagged for this ticker -- an absent input, not evidence against one existing.";
    case "data_quality":
      return (m.api_errors === 0 && !m.bootstrap)
        ? "No API errors and no bootstrap flag this scan -- the data feeding the other gates is trustworthy."
        : `A data-quality concern was flagged this scan (api_errors=${m.api_errors ?? "n/a"}, bootstrap=${m.bootstrap ? "yes" : "no"}) -- weigh the other gates' numbers with that in mind.`;
    default:
      return "";
  }
}

function renderGateItem(name, gate) {
  const why = gateWhyItMatters(name, gate);
  const whyHtml = why ? `<div class="gate-why">${esc(why)}</div>` : "";
  const note = GATE_LIMITATION_NOTES[name]
    ? `<div class="gate-note">${esc(GATE_LIMITATION_NOTES[name])}</div>`
    : "";
  return `
    <div class="gate-item">
      <div class="gate-item-head">
        <span class="gname">${esc(name.replace(/_/g, " "))}</span>
        <span class="gpass ${gate.passed ? "yes" : "no"}">${gate.passed ? "PASS" : "no"}</span>
      </div>
      ${whyHtml}
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
//
// REAL BUG FIX (2026-10-14, reported live on NVDA): a marker dated AFTER
// every fetched candle (e.g. today's real-time flow alerts/dark-pool
// prints, timestamped today, while the daily OHLC series' last bar is
// yesterday's close because today's session hasn't finished/finalized
// yet) used to silently fall through this same loop and land on the
// LAST candle -- every single out-of-range marker collapsing onto one
// bar at the right edge, exactly the "stacked at the right boundary" bug.
// The loop logic for "marker predates the series" (falling back to the
// FIRST candle) was intentional and stays; "marker postdates the series"
// must NOT get the same treatment -- there is no real candle that event
// happened on. Returns null in that case so the caller excludes the
// marker rather than inventing/clamping its position.
function nearestBarTime(markerDate, sortedCandleDates) {
  if (!sortedCandleDates.length) return null;
  const lastCandle = sortedCandleDates[sortedCandleDates.length - 1];
  if (markerDate > lastCandle) return null; // genuinely outside the fetched range -- do not clamp
  let best = null;
  for (const t of sortedCandleDates) {
    if (t <= markerDate) best = t;
    else break;
  }
  return best || sortedCandleDates[0];
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

  // REAL BUG FIX (2026-10-14, re-reported live on NVDA + PLTR -- the
  // previous fix did NOT resolve this): measured directly against N5,
  // real live NVDA evidence returned 600 markers, every single one
  // dated on the SAME single most-recent candle (today -- real, high
  // flow-alert volume, not a bug in itself). The previous fix correctly
  // stopped markers landing on the WRONG day; it did nothing for many
  // markers correctly landing on the SAME day, which is exactly what
  // produces a visual vertical stack -- lightweight-charts does not
  // offset multiple markers sharing one x-position, so N real markers
  // on one bar render as N literally-overlapping circles. Root cause
  // was volume, not timestamp mapping.
  //
  // Fix: GROUP markers by (bar, kind) into ONE chart marker per group,
  // labeled with a real count ("23 flow alerts, latest $187.20") instead
  // of plotting every individual event -- this is honest aggregation,
  // not hiding evidence: every underlying event is still listed in the
  // "Evidence on this chart" detail table rendered below (see
  // renderMarkerDetailTable), so nothing is dropped, just not each
  // individually plotted as a separate overlapping dot.
  let excludedCount = 0;
  const groups = new Map(); // key: `${barTime}|${kind}` -> { barTime, kind, events: [...] }
  for (const m of ohlc.markers || []) {
    const barTime = nearestBarTime(dateOnly(m.time), candleDates);
    if (!barTime) {
      excludedCount += 1;
      continue;
    }
    const key = `${barTime}|${m.kind}`;
    if (!groups.has(key)) groups.set(key, { barTime, kind: m.kind, events: [] });
    groups.get(key).events.push(m);
  }
  const allEvents = []; // flat list for the detail table, newest-first within each group preserved
  const markers = Array.from(groups.values()).map((g) => {
    allEvents.push(...g.events);
    const n = g.events.length;
    const withPrice = g.events.filter((e) => e.price != null);
    const latestPrice = withPrice.length ? Number(withPrice[withPrice.length - 1].price) : null;
    const kindLabel = g.kind === "darkpool_trade" ? "dark-pool trade" : "flow alert";
    const text = n === 1
      ? (g.events[0].price != null ? `${g.events[0].label} @ ${Number(g.events[0].price).toFixed(2)}` : g.events[0].label)
      : `${n} ${kindLabel}${n === 1 ? "" : "s"}${latestPrice != null ? ` (latest ${latestPrice.toFixed(2)})` : ""}`;
    return {
      time: g.barTime,
      position: g.kind === "darkpool_trade" ? "belowBar" : "aboveBar",
      color: g.kind === "darkpool_trade" ? "#60a5fa" : "#f59e0b",
      shape: "circle",
      text,
    };
  });
  series.setMarkers(markers);
  chart.timeScale().fitContent();

  // Info panel below the chart (2026-10-14 fix): both the excluded-marker
  // disclosure AND the full, ungrouped event list. Grouping markers on
  // the chart above (into one dot per bar+kind, with a count) fixes the
  // visual stacking bug, but every real event must still be genuinely
  // visible somewhere -- this is that "somewhere", not a cosmetic extra.
  // Lives OUTSIDE `root` as a single sibling container (root.innerHTML is
  // cleared on every call, which would otherwise wipe it immediately);
  // removed and rebuilt wholesale each render so repeated calls never
  // accumulate duplicate siblings.
  const existingInfo = root.nextElementSibling;
  if (existingInfo && existingInfo.classList.contains("chart-markers-info")) {
    existingInfo.remove();
  }
  if (excludedCount > 0 || allEvents.length > 0) {
    const info = document.createElement("div");
    info.className = "chart-markers-info";
    info.style.marginTop = "6px";
    let html = "";
    if (excludedCount > 0) {
      html += `<div class="muted">${excludedCount} evidence marker(s) dated after this chart's most recent bar are not shown (no real candle exists yet for that date) -- not clamped or estimated.</div>`;
    }
    if (allEvents.length > 0) {
      const sorted = [...allEvents].sort((a, b) => (a.time < b.time ? 1 : -1)); // newest first
      const rows = sorted.map((e) => `
        <tr>
          <td>${esc(e.time)}</td>
          <td>${esc(e.kind === "darkpool_trade" ? "Dark-pool trade" : "Flow alert")}</td>
          <td>${esc(e.label || "")}</td>
          <td>${e.price != null ? Number(e.price).toFixed(2) : "--"}</td>
        </tr>`).join("");
      html += `
        <details class="chart-markers-detail">
          <summary class="muted">Evidence on this chart (${allEvents.length} event${allEvents.length === 1 ? "" : "s"}, grouped above by day/kind -- every individual event below)</summary>
          <table class="chart-markers-table"><thead><tr><th>Time</th><th>Kind</th><th>Label</th><th>Price</th></tr></thead>
          <tbody>${rows}</tbody></table>
        </details>`;
    }
    info.innerHTML = html;
    root.after(info);
  }

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

// --- P7: Signal lifecycle timeline ------------------------------------------
//
// Built purely from the REAL `history` array already present in
// /api/ticker/{t}'s response (store.history(ticker) -- scan_date/
// any_gate_passed/state/created_at, see backend/app/storage.py). Most of
// the 11 scenarios only have 0-1 real recorded scans on a freshly started
// process; only VOLX/PRSX were seeded with genuine multi-entry history
// (see seed_demo_history.py). This renders exactly what's there --
// 0/1-entry history gets an explicitly sparse, minimal state, never a
// padded-looking timeline implying a longer track record than is real.
// Renders ONE raw history entry's full detail row -- used ONLY inside the
// bounded-height expandable <details> panel below, never directly in the
// main page flow regardless of how long `entries` is.
function renderRawHistoryStep(e, i, entries) {
  const gloss = STATE_GLOSS[e.state] || "";
  const prev = i > 0 ? entries[i - 1].state : null;
  const changed = prev && prev !== e.state;
  return `
    <div class="timeline-step">
      <div class="timeline-dot state-${esc(e.state)}"></div>
      <div class="timeline-step-body">
        <div class="timeline-step-head">
          <span class="tag state-${esc(e.state)}">${esc(e.state)}</span>
          <span class="muted">${esc(e.scan_date)}</span>
          ${changed ? `<span class="muted timeline-change">(${esc(prev)} \u2192 ${esc(e.state)})</span>` : ""}
        </div>
        <div class="timeline-step-gloss muted">${esc(gloss)}</div>
        <div class="timeline-step-meta muted">gate passed this scan: ${e.any_gate_passed ? "yes" : "no"}</div>
      </div>
    </div>`;
}

// P0 UX-correction pass: the full per-scan vertical list (one row per raw
// history entry) is exactly what Chad flagged as overwhelming on a long-
// running dev/N5 instance where every manual/test GET /api/ticker/{t}
// triggers a real scan+log write. The fix is NOT to show less real data --
// it's to show the real COLLAPSED progression (collapseHistory(), shared
// with the Radar chip) prominently, and move the full uncollapsed list
// into a bounded-height, scrollable <details> panel that is lossless on
// expand (exactly the real entries, nothing re-derived). 0/1-entry history
// keeps its prior minimal-state rendering unchanged.
function renderLifecycleTimeline(history, currentState) {
  const entries = Array.isArray(history) ? history : [];
  if (entries.length === 0) {
    return `
      <div class="card timeline-card">
        <div class="card-head"><strong>Signal Lifecycle</strong></div>
        <div class="empty">No scan history recorded yet for this ticker &mdash; this is its first scan.</div>
      </div>`;
  }
  if (entries.length === 1) {
    const e = entries[0];
    const gloss = STATE_GLOSS[e.state] || "";
    return `
      <div class="card timeline-card">
        <div class="card-head"><strong>Signal Lifecycle</strong>
          <span class="muted">(1 recorded scan &mdash; too early for a track record)</span></div>
        <div class="timeline-single">
          <span class="tag state-${esc(e.state)}">${esc(e.state)}</span>
          <span class="muted">${esc(e.scan_date)}${gloss ? ` \u2014 ${esc(gloss)}` : ""}</span>
        </div>
      </div>`;
  }
  const segments = collapseHistory(entries);
  const progressionHtml = renderSegmentProgression(segments, { maxVisible: 5 });
  const rawSteps = entries.map((e, i) => renderRawHistoryStep(e, i, entries)).join("");
  return `
    <div class="card timeline-card">
      <div class="card-head"><strong>Signal Lifecycle</strong>
        <span class="muted">(${segments.length} segment(s) from ${entries.length} recorded scans &mdash; current state: ${esc(currentState || "")})</span></div>
      ${progressionHtml}
      <details class="how history-details">
        <summary>Full real scan-by-scan history (${entries.length} entries)</summary>
        <div class="timeline history-details-body">${rawSteps}</div>
      </details>
    </div>`;
}

// --- P5: evidence-first Ticker Analysis -------------------------------------
//
// Layout order, deliberately: (1) candlestick chart + evidence markers,
// (2) the 4-way evidence classification (Supporting/Conflicting/Unmet/Data
// Limitation) immediately beside/below it, (3) the lifecycle timeline, then
// (4) ticker-level state/confidence summary, with raw JSON/internal codes
// staying inside the EXISTING collapsed <details> at the bottom. Detection
// confidence language below is deliberately never framed as a predicted
// trade outcome/success rate -- "confidence" describes how clearly gates
// fired this scan, nothing about what happens next.
// P1a layout order (UX-correction pass, deliberately exact): (1) selected
// ticker + current signal state, prominently, at the very top; (2) the
// candlestick chart with evidence markers; (3) the concise 4-way evidence
// classification immediately below the chart (both together must visually
// dominate the page); (4) a placeholder slot for the "Investigate with AI"
// workspace -- app.live.js/app.static.js's loadInvestigateSection() fills
// #investigateSection wherever it is in the DOM, so simply positioning
// this div here (instead of as a page-bottom sibling in index.html, see
// that file's comment) promotes Investigate near the top with zero change
// to the existing fetch/render wiring; (5) the NEW compact collapsed
// lifecycle summary from P0. Raw JSON / internal rule ids stay inside the
// existing bottom <details> panel, unchanged in content, just de-prioritized
// by position.
// Accurate, non-confusing wording for the state-transition line: a real
// change shows "Transition: OLD -> NEW", the very first observation (no
// previous_state at all) shows "Initial detection: NEW", and -- the actual
// fix requested here -- a scan that left the state unchanged shows
// "Status: Still X" instead of the confusing "Transition: X -> X". Purely
// presentation: does not touch what previous_state/state actually are.
function transitionLabel(previousState, state) {
  if (!previousState) return `Initial detection: ${state}`;
  if (previousState === state) return `Status: Still ${state}`;
  return `Transition: ${previousState} \u2192 ${state}`;
}

function renderTickerDetail(d) {
  const gatesHtml = renderGateGroups(d.gates);
  const stateGloss = STATE_GLOSS[d.state] || "";
  const ruleGloss = TRANSITION_RULE_GLOSS[d.transition_rule] || "internal rule identifier";
  const timelineHtml = renderLifecycleTimeline(d.history, d.state);
  return `
    <div class="card ticker-header-card">
      <div class="card-head"><span class="tk tk-lg">${esc(d.ticker)}</span>
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
      <div class="read">${esc(transitionLabel(d.previous_state, d.state))}</div>
      <div class="read">Confidence reason: ${esc(d.confidence_reason)}</div>
    </div>
    <div class="card chart-card">
      <div class="card-head"><strong>Price Chart &mdash; ${esc(d.ticker)}</strong>
        <span class="muted">evidence markers from the same flow_alerts/darkpool_trades the gates use</span></div>
      <div id="ohlcChartContainer" class="chart-container"><div class="empty">Loading chart...</div></div>
    </div>
    <div class="card evidence-primary-card">
      <div class="card-head"><strong>Evidence &mdash; what the chart above is based on</strong></div>
      <p class="muted gate-group-intro">A gate that did not pass is not automatically "conflicting evidence." Grouped below by what it actually represents, each with a plain-English note on what it observed. Confidence below describes how clearly gates fired this scan &mdash; it is never a prediction of trade outcome.</p>
      ${gatesHtml}
    </div>
    <div id="investigateSection"></div>
    ${timelineHtml}
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

// renderInvestigateHeader: ALWAYS display the ticker + active mode inside
// this workspace itself, not just inferred from the page header above it
// (P6 requirement) -- it's the one thing that must stay correct even if
// the user scrolled down or the AI card is viewed out of page-header
// context. modeLabel is built by the adapter (it knows demo/live and
// simulated/real); this file only renders the string handed to it.
function renderInvestigateHeader(ticker, modeLabel) {
  return `
    <div class="investigate-context">
      <span class="investigate-context-ticker">${esc(ticker)}</span>
      <span class="investigate-context-mode">${esc(modeLabel)}</span>
    </div>`;
}

// Research-process step strip: the ACTUAL bounded workflow this feature
// runs (tool selection -> bounded read-only UW lookups -> synthesis),
// never implying more/different steps than the backend's real bounded
// design (max 4 tool calls, max 1 extra reasoning round -- see
// backend/app/ai/orchestrator.py). Purely descriptive of the process;
// the REAL tools_used list (rendered separately via renderToolsUsed) is
// what shows what actually happened this run.
function renderInvestigateProcessSteps() {
  return `
    <div class="investigate-steps">
      <div class="investigate-step"><span class="investigate-step-n">1</span>Bounded tool selection (read-only UW lookups, max 4 calls)</div>
      <div class="investigate-step"><span class="investigate-step-n">2</span>Evidence gathered from the tools actually called this run</div>
      <div class="investigate-step"><span class="investigate-step-n">3</span>Synthesis: facts vs. interpretation kept separate (see below)</div>
    </div>`;
}

// Plain-language label for InvestigationResult.transport (schemas.py).
// "mcp"/"rest" are both REAL (non-simulated) -- this only states which
// backend executed the UW tool calls, so the UI never implies genuine
// MCP activity happened when the REST fallback actually ran, or vice versa.
function transportLabel(transport) {
  if (transport === "mcp") return "Real MCP-protocol calls to UW's hosted server";
  if (transport === "rest") return "Real REST calls (MCP fallback transport)";
  return "Unknown (older response or unset)";
}

function renderInvestigateResult(d, ticker, modeLabel) {
  const simBanner = d.simulated
    ? `<div class="ai-simulated-banner ai-simulated-banner-strong">
         <span class="ai-simulated-badge">SIMULATED</span>
         ${esc(d.simulated_label || "Simulated agent walkthrough -- not live UW data, not a real AI call")}
       </div>`
    : `<div class="ai-live-banner">LIVE: this investigation made real calls using your configured AI provider and UW key.
         Bounded, read-only tool selection and synthesis -- see README for the one documented gap:
         full LLM-directed tool-selection/synthesis has not yet been verified end-to-end with a real provider.
         <br><strong>Transport: ${esc(transportLabel(d.transport))}</strong></div>`;
  const headerHtml = ticker ? renderInvestigateHeader(ticker, modeLabel || "") : "";
  return `
    ${headerHtml}
    ${simBanner}
    ${renderInvestigateProcessSteps()}
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

  // Market Radar watchlist editor (2026-10-14, Phase A): not shown in
  // the public demo build (its 11 synthetic scenarios are fixed, not a
  // user-editable watchlist concept at all). Chips + an add input,
  // matching the mode/key sections' settings-card style. Each
  // add/remove PUTs the FULL updated list (see initWatchlistEditor() in
  // app.live.js) -- simpler and atomic, no partial-update races.
  const watchlist = d.watchlist || { tickers: [], max_size: 20 };
  const watchlistSection = publicDemo ? "" : `
    <div class="settings-card">
      <h3>Market Radar Watchlist</h3>
      <p class="muted">Each ticker costs a real UW API call per Scan Now -- limit ${watchlist.max_size} symbols.
        This is watchlist-based scanning only, not broader-market discovery
        (Unusual Whales does offer a market-wide screener; it is not wired up here yet).</p>
      <div class="watchlist-chips" id="watchlistChips">
        ${watchlist.tickers.map((t) => `
          <span class="watchlist-chip" data-ticker="${esc(t)}">
            ${esc(t)}<button class="watchlist-chip-remove" data-remove-ticker="${esc(t)}" title="Remove ${esc(t)}">&times;</button>
          </span>`).join("")}
        ${watchlist.tickers.length === 0 ? '<span class="muted">No tickers configured -- Market Radar has nothing to scan.</span>' : ""}
      </div>
      <div class="settings-row" style="margin-top:8px">
        <input type="text" id="watchlistAddInput" placeholder="Add ticker (e.g. AMD)" autocomplete="off" maxlength="7" />
        <button class="btn" id="watchlistAddBtn" ${watchlist.tickers.length >= watchlist.max_size ? "disabled" : ""}>Add</button>
      </div>
      <div id="watchlistStatus" class="muted" style="margin-top:6px"></div>
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
        <hr class="settings-divider" />
        <p class="muted"><strong>Test AI Provider</strong> -- makes ONE short, real request to the saved
          provider+model to confirm the credential works. <strong>This is a real, individually-billed API
          call</strong> (a few cents at most, bounded to a tiny response) -- it does NOT call UW or MCP at
          all, so a pass here is <em>not</em> proof that MCP/tool execution works.</p>
        <div class="settings-row">
          <button class="btn" id="testAiKeyBtn" ${ai.configured ? "" : "disabled"}>Test AI Provider (incurs a small charge)</button>
        </div>
        <div id="aiTestStatus" class="muted" style="margin-top:6px"></div>
        <hr class="settings-divider" />
        <p class="muted"><strong>Test UW MCP Connection</strong> -- a genuine MCP-protocol handshake
          (connect, authenticate, discover tools) plus one bounded read-only tool call against your saved UW
          key. <strong>No LLM/AI provider is called at all</strong> -- it only uses your existing UW API
          quota, so a pass here is <em>not</em> proof that AI-directed investigation works end-to-end.</p>
        <div class="settings-row">
          <button class="btn" id="testMcpBtn" ${uw.configured ? "" : "disabled"}>Test UW MCP Connection</button>
        </div>
        <div id="mcpTestStatus" class="muted" style="margin-top:6px"></div>
      </div>`;

  root.innerHTML = `${banner}${modeSection}${watchlistSection}${uwSection}${aiSection}`;
  return { publicDemo };
}

// Wires click-anywhere "Investigate" affordances (cards/rows on Market
// Radar) to navigateToTicker, given the adapter's own loadTickerDetail.
// Call this once after rendering the radar grid/table.
function wireInvestigateButtons(loadTickerDetailFn) {
  document.querySelectorAll("[data-investigate-ticker]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      navigateToTicker(btn.dataset.investigateTicker, loadTickerDetailFn, { focusInvestigate: true });
    });
  });
}
