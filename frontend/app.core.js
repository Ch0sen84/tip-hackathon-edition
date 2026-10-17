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
// UX remediation item 4 (2026-10-16): `lastTicker` is a SEPARATE,
// deliberately-never-cleared-by-plain-navigation memory of "the most
// recent real ticker the user was looking at", distinct from
// `currentTicker` (which means "the ticker relevant to the CURRENT
// view" and is legitimately null on every non-ticker view). Real bug
// this fixes: navigateToView() below used to null out `currentTicker`
// the moment the user navigated to any other view, so clicking the
// "Ticker Analysis" NAV ITEM again afterward (as opposed to browser
// Back/Forward, which replays the real stored history entry and
// therefore already worked) landed on a blank "select a ticker"
// state instead of the ticker the user had actually been analyzing.
// Only an explicit mode-invalidation (clearTickerView, when the old
// ticker may not even exist under the new adapter) clears this.
const TIP_STATE = { currentTicker: null, lastTicker: null, currentView: "radar" };

// --- Phase A: auth setup banner (pure render, no fetch) --------------------
//
// app.live.js owns the actual localStorage/fetch/bootstrap logic (see its
// ensureAuthToken()/wireAuthSetupBanner()); this is just the markup for
// the one-time "Setup" prompt it shows, consistent with this file's
// "no fetch() here" rule. Two variants:
//   "missing" -- first run / no token yet, shown before any mutating
//                action has been attempted.
//   "error"   -- a mutating call just got a real 401 (token missing or no
//                longer valid server-side, e.g. after a container
//                recreation regenerated it) -- same controls, with an
//                explicit "authentication failed" message up top instead
//                of a generic exception.
function renderAuthSetupBanner(variant) {
  // QA remediation D6: the auto-bootstrap failure case gets its OWN
  // distinct heading/explanation (not the same generic copy as a stale-
  // token 401) -- this is the real actionable status the user needs,
  // not just a browser-console message. "success" never reaches this
  // function at all (see app.live.js's hideAuthSetupBanner(), called
  // immediately on any successful bootstrap/pairing/manual-save) -- the
  // banner genuinely never renders on the success path.
  const heading =
    variant === "error"
      ? "Authentication required"
      : variant === "bootstrap_failed"
      ? "Couldn't automatically set up access"
      : "One-time setup required before you can change settings or run Investigate with AI";
  const subMessage =
    variant === "error"
      ? "Your saved access token is missing or no longer valid (the server rejected it with 401)."
      : variant === "bootstrap_failed"
      ? "This is expected on Docker Desktop (Windows/Mac) -- a browser request from the host " +
        "arrives at the container through Docker Desktop's network bridge, so the automatic " +
        "same-machine check can't confirm it came from this computer. Use the pairing code " +
        "below instead (see 'Pair this browser')."
      : "This app requires a one-time access token before any setting, watchlist change, or Investigate with AI call will work.";
  return `
    <div class="auth-setup-card">
      <strong>${esc(heading)}</strong>
      <p>${esc(subMessage)}</p>
      <button class="btn" id="authBootstrapBtn">Set up access (same machine only)</button>
      <span id="authBootstrapStatus" class="auth-setup-status"></span>
      <div class="auth-setup-pairing">
        <strong>Pair this browser (Docker Desktop -- QA remediation D1)</strong>
        <p>
          Run <code>docker logs &lt;container&gt;</code> on the Docker host and look for the most
          recent "Docker Desktop / browser pairing code" line, then paste it here. One-time use;
          rotates automatically every 15 minutes.
        </p>
        <input type="text" id="authPairingCodeInput" placeholder="Paste pairing code here" autocomplete="off" />
        <button class="btn" id="authPairingSubmitBtn">Pair</button>
        <span id="authPairingStatus" class="auth-setup-status"></span>
      </div>
      <details class="auth-setup-fallback">
        <summary>Still stuck, or on a different machine entirely?</summary>
        <p>
          Token bootstrap only works for a browser running on the SAME machine as the
          Docker host (it is refused for any other IP, by design -- see README.md
          "Authentication"). For a LAN/remote setup (not Docker Desktop), retrieve the token
          yourself and paste it below:
        </p>
        <pre>docker exec &lt;container&gt; cat /srv/data/.auth_token</pre>
        <div class="auth-token-field-row">
          <input type="password" id="authManualTokenInput" placeholder="Paste token here" autocomplete="off" />
          <button type="button" class="btn-link" id="authManualTokenRevealBtn" aria-label="Show token">Show</button>
        </div>
        <button class="btn-link" id="authManualTokenSaveBtn">Save token</button>
      </details>
    </div>`;
}

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
// URL-backed navigation fix (2026-10-16): "marketwide" (Market
// Intelligence) was missing from this list entirely -- a REAL bug,
// since parseNavHash() silently rejects any view not in NAV_VIEWS,
// falling back to the default Market Radar view. That meant F5 (or
// restarting the browser) on Market Intelligence always bounced back
// to My Watchlist instead of staying put -- unsupported-route handling
// working "correctly" by accident for a view that WAS supposed to be
// supported. See applyNavState below for the matching restore branch.
const NAV_VIEWS = ["radar", "ticker", "settings", "about", "marketwide"];

function parseNavHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) return { view: null, ticker: null, mode: null };
  const params = new URLSearchParams(raw);
  // Legacy format from the prior session: a bare `#ticker=XXX` with no
  // `view=` key at all.
  if (!params.has("view") && params.has("ticker")) {
    const t = params.get("ticker");
    return { view: "ticker", ticker: t ? t.toUpperCase() : null, mode: null };
  }
  let view = params.get("view");
  if (!NAV_VIEWS.includes(view)) view = null;
  const ticker = params.get("ticker");
  // UX round (2026-10-16 round 5), item 2: `mode` only ever means
  // something on the "radar" view (My Watchlist vs Market Discovery
  // sub-panel) -- see setRadarMode/applyNavState's "radar" branch.
  const mode = params.get("mode") === "discovery" ? "discovery" : null;
  return { view, ticker: ticker ? ticker.toUpperCase() : null, mode };
}

function buildNavHash(view, ticker) {
  const params = new URLSearchParams();
  params.set("view", view);
  if (view === "ticker" && ticker) params.set("ticker", ticker);
  // UX round (2026-10-16 round 5), item 2: reads the CURRENT _radarMode
  // directly (module-level state in this same file, already set by the
  // time any real navigation runs) rather than threading a new
  // parameter through every pushNav/replaceNav call site -- so Radar's
  // own hash always reflects whichever sub-panel is actually showing,
  // the same way every other view's hash already reflects its own
  // current state.
  if (view === "radar" && typeof _radarMode !== "undefined" && _radarMode === "discovery") {
    params.set("mode", "discovery");
  }
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
  if (ticker) TIP_STATE.lastTicker = ticker; // see TIP_STATE's own comment above
  try {
    history.pushState({ view, ticker: ticker || null }, "", buildNavHash(view, ticker));
  } catch (e) {
    /* history update is a convenience, never fatal */
  }
}

function replaceNav(view, ticker) {
  TIP_STATE.currentView = view;
  TIP_STATE.currentTicker = ticker || null;
  if (ticker) TIP_STATE.lastTicker = ticker; // see TIP_STATE's own comment above
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
//
// UX remediation item 4 (2026-10-16): falls back to TIP_STATE.lastTicker
// when navigating TO "ticker" with nothing more specific already in
// play (currentTicker is null here on every real-world call to this
// branch, since it was just nulled by whatever navigateToView call took
// the user away from Ticker Analysis in the first place) -- this is
// what makes clicking the "Ticker Analysis" nav item again actually
// return to the ticker the user was looking at, instead of a blank
// "select a ticker" state. Browser Back/Forward never hit this path at
// all (popstate restores the exact stored {view, ticker} from history
// directly via applyNavState), so this fix is scoped to the nav-item-
// click case specifically, which is the gap that was actually missing.
function navigateToView(view, opts = {}) {
  const { push = true } = opts;
  const ticker = view === "ticker" ? (TIP_STATE.currentTicker || TIP_STATE.lastTicker) : null;
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
    // UX round (2026-10-16 round 5), item 2: restore the Market
    // Discovery/My Watchlist sub-panel from the URL FIRST (persist:
    // false -- setHistory right below writes the one real history
    // entry, reading this now-current _radarMode via buildNavHash), so
    // an F5 refresh/deep-link/Back-Forward onto "radar" lands on
    // whichever sub-panel was actually showing, not always back to the
    // My Watchlist default -- same restore-from-URL pattern Market
    // Intelligence's own "marketwide" branch below already established.
    setRadarMode((state && state.mode) || "watchlist", { persist: false });
    setHistory("radar", null);
    switchView("radar");
    return;
  }
  if (state.view === "settings" || state.view === "about") {
    setHistory(state.view, null);
    switchView(state.view);
    return;
  }
  if (state.view === "marketwide") {
    setHistory("marketwide", null);
    switchView("marketwide");
    // URL-backed nav fix: restoring this view from a refresh/deep-link/
    // Back-Forward/history event must behave EXACTLY like the real nav
    // button click does (which lazily loads it via initMarketWideRadar,
    // see app.live.js) -- never a second, parallel load path, and never
    // a duplicate fetch if it was already loaded this session
    // (initMarketWideRadar's own _marketWideLoaded guard makes repeat
    // calls here a no-op, so this is safe to call unconditionally).
    if (handlers && handlers.onMarketWideRestore) handlers.onMarketWideRestore();
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
  // UX remediation item 4 (2026-10-16): this explicit invalidation path
  // (a Demo<->Live mode switch) is the one case that SHOULD also wipe
  // the "return to this ticker" memory -- the old ticker may not even
  // exist under the new adapter, so there is nothing honest to return
  // to. Plain navigation to another view must never reach this
  // function at all (see navigateToView's own comment).
  TIP_STATE.lastTicker = null;
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
// 2026-10-15: raised from 8 -- the demo fixture set grew to 11 scenario
// tickers since this cap was chosen, which silently pushed the
// alphabetically-last 3 (TEPID/VOLX/ZYNC) out of the unfiltered
// suggestion view (test_manual_bugfixes.mjs caught it). 16 comfortably
// covers today's 11 plus headroom without needing another bump soon.
const MAX_SUGGESTIONS_SHOWN = 16;

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

// Market Radar mode toggle (C2b/C2c, 2026-10-15): switches which of the
// two sibling panels is visible. "watchlist" is the default/unchanged
// panel; "discovery" is lazily loaded (onDiscoverySelected callback)
// only the first time it's selected, never pre-fetched on page load.
//
// UX round (2026-10-16 round 5), item 2: this sub-view selection used to
// live ONLY in the module-level _radarMode variable -- invisible to the
// URL, so an F5 refresh (or a deep link) always bounced back to
// "watchlist" even if the user had explicitly switched to "Market
// Discovery". Fixed the EXACT SAME way Market Intelligence's own view
// was already fixed (see NAV_VIEWS/buildNavHash/parseNavHash/
// applyNavState's "marketwide" branch, plus that section's own comment
// explaining the original bug): the sub-mode now rides along in the
// `#view=radar&mode=discovery` hash, restored on bootstrap/F5/Back-
// Forward via applyNavState's "radar" branch below, through this one
// shared setRadarMode() function (never a second, parallel switch-panel
// implementation for the restore path vs the click path).
let _radarMode = "watchlist";
let _radarModeDiscoveryLoaded = false;
let _onDiscoverySelectedCallback = null;

function setRadarMode(mode, opts = {}) {
  const { persist = true } = opts;
  mode = mode === "discovery" ? "discovery" : "watchlist";
  _radarMode = mode;
  const toggle = document.getElementById("radarModeToggle");
  if (toggle) {
    toggle.querySelectorAll("[data-radar-mode]").forEach((b) => b.classList.toggle("active", b.dataset.radarMode === mode));
  }
  const wl = document.getElementById("watchlistRadarSection");
  const disc = document.getElementById("discoveryRadarSection");
  if (wl) wl.style.display = mode === "watchlist" ? "" : "none";
  if (disc) disc.style.display = mode === "discovery" ? "" : "none";
  if (mode === "discovery" && !_radarModeDiscoveryLoaded) {
    _radarModeDiscoveryLoaded = true;
    if (typeof _onDiscoverySelectedCallback === "function") _onDiscoverySelectedCallback();
  }
  // A plain sub-view toggle updates the CURRENT history entry in place
  // (same "replace, never push" convention applyNavState/bootstrap/
  // popstate restores already use) -- never adds a Back stop just for
  // flipping between the two Radar panels.
  if (persist) replaceNav("radar", null);
}

function wireRadarModeToggle(onDiscoverySelected) {
  _onDiscoverySelectedCallback = onDiscoverySelected;
  const toggle = document.getElementById("radarModeToggle");
  if (!toggle) return;
  toggle.querySelectorAll("[data-radar-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setRadarMode(btn.dataset.radarMode));
  });
}

// --- Phase D: Market-wide Radar -- scheduler controls + freshness-tagged
// candidate list. Pure render functions (no fetch(), no DOM lookups by
// id other than the DOM structure they're handed) -- app.live.js owns
// the fetch calls and button wiring, same split as every other
// render*/wire* pair in this file.

// UX round (2026-10-16), item 5: made understandable without reading
// docs -- what each mode actually does, framed as an action (never
// implies this controls the personal watchlist, which has its own
// separate, manual-only "Refresh Data" action -- see item 9/W2).
const SCHEDULER_MODE_LABELS = {
  disabled: "Disabled -- no scheduled broader-market scans run automatically. Zero background UW calls.",
  dry_run: "Dry Run -- automatically discovers candidates across the broader market on a schedule, without deeper per-ticker evaluation.",
  enabled: "Enabled -- automatically discovers candidates AND evaluates the configured top-ranked selections on a schedule.",
};

// QA remediation D7: the mode TOGGLE BUTTONS below used to render the
// raw backend identifier (`dry_run`, `enabled`) directly as their label
// -- readable only to someone who already knows the API's vocabulary.
// This short-label map is ONLY for display; data-scheduler-mode
// attributes (and every API call) still use the real backend values
// (disabled/dry_run/enabled) unchanged.
const SCHEDULER_MODE_SHORT_LABELS = { disabled: "Disabled", dry_run: "Dry Run", enabled: "Enabled" };

function schedulerModeShortLabel(mode) {
  return SCHEDULER_MODE_SHORT_LABELS[mode] || mode;
}

function renderSchedulerControls(status) {
  const modes = ["disabled", "dry_run", "enabled"];
  const buttons = modes
    .map(
      (m) =>
        `<button class="radar-mode-btn${m === status.mode ? " active" : ""}" data-scheduler-mode="${m}">${esc(
          schedulerModeShortLabel(m)
        )}</button>`
    )
    .join("");
  const pauseNote = status.pause_reason
    ? `<div class="muted" style="color:#b45309">Paused: ${esc(status.pause_reason)}</div>`
    : "";
  // UX remediation item 4 (2026-10-16): the old "Run Once now" button
  // (disabled whenever mode === "disabled") is REMOVED entirely here --
  // it was a now-contradictory UI remnant left over from before the
  // Run Once decoupling round: a user staring at a disabled "Run Once
  // now" button right next to two perfectly enabled "Discover Once" /
  // "Discover & Evaluate Once" buttons that do the equivalent job while
  // Disabled looked like a bug, not a deliberate second control. The
  // decoupled buttons (renderRunOnceDepthActions below) are now the
  // ONLY one-time-run UI; `runningNow` is still needed so those buttons
  // correctly disable while a cycle is in progress.
  const runningNow = !!status.cycle_in_progress;
  const topN = status.top_n_configured ?? status.top_n ?? "n/a";
  return `
    <div class="radar-mode-toggle" id="schedulerModeToggle">${buttons}</div>
    <div class="muted" style="margin-top:6px">${esc(SCHEDULER_MODE_LABELS[status.mode] || status.mode)}</div>
    <div class="muted" style="margin-top:2px;font-size:12px">
      This controls <strong>broader market discovery</strong> only -- your personal watchlist has its own
      separate, manual-only <strong>Refresh Data</strong> action on My Watchlist, never this scheduler.
    </div>
    <div class="muted" style="font-size:12px;margin-top:4px">
      Last run: ${esc(status.last_run_finished_at && typeof formatTimestampFull === "function" ? formatTimestampFull(status.last_run_finished_at) : (status.last_run_finished_at || "never"))} &middot;
      Next run: ${esc(status.next_run_at && typeof formatTimestampFull === "function" ? formatTimestampFull(status.next_run_at) : (status.next_run_at || "n/a"))} &middot;
      Candidates evaluated last cycle: ${status.last_cycle_candidates_evaluated ?? "n/a"} &middot;
      Freshness window: ${Math.round((status.freshness_window_s || 0) / 60)}m &middot;
      Top-N configured: ${esc(String(topN))}
    </div>
    ${pauseNote}
    ${renderRunOnceDepthActions(status, runningNow, topN)}
  `;
}

// Final remediation round (2026-10-16): the actual fix for "Run Once
// currently requires arming Dry Run/Enabled first". These two buttons
// call the NEW backend depth-based one-time path
// (POST /api/scheduler/run-once with an explicit `depth`), which is
// fully decoupled from the mode toggle above -- they work (and are the
// ONLY way to run a one-time scan) while mode stays Disabled, and work
// identically regardless of whatever mode is currently selected.
// Deliberately rendered as its OWN bordered card with a "ONE-TIME"
// .tag badge, visually and structurally separate from
// .radar-mode-toggle above it, so a user cannot mistake clicking one
// of these for turning on recurring scheduling -- that mode toggle is
// untouched, unmodified, and still the only thing that writes a
// recurring mode.
function renderRunOnceDepthActions(status, runningNow, topN) {
  const modeShort = esc(schedulerModeShortLabel(status.mode));
  return `
    <div class="card" id="runOnceActionsCard" style="margin-top:14px;padding:12px 14px;gap:8px;border-color:var(--accent2)">
      <div class="card-head" style="margin-bottom:0">
        <span class="tag" style="color:var(--accent)">ONE-TIME SCAN</span>
        <span class="muted" style="font-size:11.5px">Independent of the recurring mode above -- works even while it's <strong>${modeShort}</strong>, and never changes it</span>
      </div>
      <div class="muted" style="font-size:12px">
        Run a single market-wide scan right now without enabling recurring background collection. The recurring
        mode stays exactly as set (currently <strong>${modeShort}</strong>) before, during, and after either action below.
      </div>
      <div class="settings-row" style="gap:10px;align-items:flex-start">
        <button class="btn run-once-depth-btn" id="discoverOnceBtn" data-depth="discovery_only" ${runningNow ? "disabled" : ""}>
          Discover Once
        </button>
        <span class="muted" style="font-size:11.5px;max-width:230px">Runs one market screener cycle, catalogs discovered candidates, then stops. No per-ticker evaluation.</span>
      </div>
      <div class="settings-row" style="gap:10px;align-items:flex-start;margin-top:2px">
        <button class="btn run-once-depth-btn" id="discoverEvaluateOnceBtn" data-depth="discovery_plus_evaluation" ${runningNow ? "disabled" : ""}>
          Discover &amp; Evaluate Once
        </button>
        <span class="muted" style="font-size:11.5px;max-width:230px">Runs one market screener cycle, then evaluates the top <strong>${esc(String(topN))}</strong> ranked candidates (configured top-N), then stops.</span>
      </div>
      <div id="runOnceDepthProgress" class="muted" style="font-size:12px;min-height:16px"></div>
    </div>
  `;
}

const FRESHNESS_LABELS = {
  current: "Current",
  stale: "Stale",
  unevaluated: "Unevaluated",
};

// UX round (2026-10-16 round 5), item 1: "Why TIP surfaced this" --
// REAL discovery-pipeline fields only (see backend/app/core/discovery.py's
// normalize_discovery_candidate, the ONLY place this raw_json shape is
// produced), never a fabricated number or evidence string. Each field is
// rendered in one of exactly two visually/textually distinct states:
//   * missing/None ("not captured by the screener for this snapshot") --
//     the field genuinely was never reported -- styled with the SAME
//     .px-unavailable convention fmtPriceLine() already established
//     (italic, muted) so "unavailable" means the same thing everywhere
//     in this app, never a second invented look.
//   * present, including a real 0 ("checked and found nothing", e.g.
//     put_volume: 0) -- rendered as a normal, non-italic value, exactly
//     like any other real number -- a genuine zero must never look the
//     same as "we don't know".
// D3 (ux-nav-watchlist-reliability round 9): this used to hand-roll its
// own `$${...toLocaleString()}` formatting, which (unlike the rest of
// the app) never abbreviated magnitude and, worse, put the sign INSIDE
// the "$" for a negative net_premium (raw "$-89,231,250" instead of the
// established, already-fixed "\u2212$89.2M" pattern -- see fmtMoney()'s
// own module comment for the full prior writeup of that exact bug class).
// Market Discovery's row/table already use fmtMoney() for this same
// net_premium field (lines below, renderDiscoveryCandidateRow); this is
// the one remaining display path that wasn't using it. Extends the
// existing helper rather than inventing a second formatter.
const WHY_SURFACED_FIELDS = [
  { key: "net_premium", label: "Net premium", fmt: (v) => fmtMoney(Number(v)) ?? "$0" },
  { key: "call_volume", label: "Call volume", fmt: (v) => Number(v).toLocaleString() },
  { key: "put_volume", label: "Put volume", fmt: (v) => Number(v).toLocaleString() },
  { key: "iv_rank", label: "IV rank", fmt: (v) => Number(v).toFixed(1) },
  { key: "change_percent", label: "1D change", fmt: (v) => fmtPct(v) },
];

function renderWhySurfacedBlock(candidate) {
  const raw = candidate.raw;
  if (raw == null) {
    // The whole raw_json is missing (no discovery record cataloged at
    // all for this candidate) -- distinct from any one field being
    // missing; say so plainly rather than rendering five empty fields.
    return `<div class="why-surfaced muted" style="font-size:11.5px;margin-top:4px">
      <strong>Why TIP surfaced this:</strong>
      <span class="px-unavailable">no discovery details were captured for this snapshot</span>
    </div>`;
  }
  const parts = WHY_SURFACED_FIELDS.map((f) => {
    const v = raw[f.key];
    if (v === null || v === undefined) {
      return `<span title="Not captured by the screener for this snapshot">${esc(f.label)}: `
        + `<span class="px-unavailable">not captured</span></span>`;
    }
    return `<span>${esc(f.label)}: <strong>${esc(f.fmt(v))}</strong></span>`;
  }).join(" &middot; ");
  const sourceDateNote = raw.source_date
    ? ` <span class="muted" style="font-size:11px">(screener date: ${esc(raw.source_date)})</span>`
    : "";
  return `<div class="why-surfaced" style="font-size:11.5px;margin-top:4px">
    <strong>Why TIP surfaced this:</strong> ${parts}${sourceDateNote}
  </div>`;
}

function renderMarketWideCandidateRow(candidate) {
  const badgeClass = `freshness-badge freshness-${candidate.freshness}`;
  const evalState = candidate.evaluation
    ? `${esc(candidate.evaluation.state || "n/a")} (${esc(candidate.evaluation.confidence || "n/a")})`
    : "no evaluation yet";
  // UX round (2026-10-16), item 6: human-readable ET + relative time via
  // the new centralized frontend/timefmt.js, instead of a raw age-in-
  // minutes computed from age_s -- "Not evaluated" when there's genuinely
  // no cataloged evaluation at all (never a fabricated "0m old").
  const retrievedAt = candidate.evaluation ? candidate.evaluation.retrieved_at : null;
  const ageNote = typeof formatRelative === "function" ? formatRelative(retrievedAt) : (candidate.age_s != null ? `${Math.round(candidate.age_s / 60)}m old` : "never evaluated");
  // UX round (2026-10-16 round 5), item 1: "View Details" -- reuses the
  // EXACT SAME data-investigate-ticker attribute + wireInvestigateButtons
  // wiring Market Radar's own Investigate buttons already use (see
  // wireInvestigateButtons below), so it opens Ticker Analysis through
  // the identical, already-tested URL-backed ticker-context mechanism --
  // never a second, parallel navigation path invented for this view.
  //
  // UX remediation item 4 (2026-10-16): a market-wide/index row (e.g.
  // SPXW, the same real is_index field renderDiscoveryCandidateRow
  // already checks -- never a second, independently-guessed detection)
  // cannot be sent through the per-ticker Ticker Analysis pipeline at
  // all, so "View Details" must not be offered for it -- matches the
  // discovery table's existing "Evaluate not supported (index/
  // derivative)" treatment rather than inventing a second convention.
  const isIndexRow = !!(candidate.raw && candidate.raw.is_index);
  const viewDetailsAction = isIndexRow
    ? `<span class="muted" title="Index/derivative instruments are not supported by the per-ticker evaluation pipeline yet">Not supported (index/derivative)</span>`
    : `<button class="btn-link btn-investigate" data-investigate-ticker="${esc(candidate.ticker)}" aria-label="View Details for ${esc(candidate.ticker)} in Ticker Analysis">View Details &rarr;</button>`;
  return `
    <div class="candidate-row market-wide-row" data-ticker="${esc(candidate.ticker)}">
      <div class="market-wide-row-main">
        <span class="ticker">${esc(candidate.ticker)}</span>
        <span class="${badgeClass}">${esc(FRESHNESS_LABELS[candidate.freshness] || candidate.freshness)}</span>
        <span class="muted">${evalState}</span>
        <span class="muted" style="font-size:11px" title="${esc(typeof formatTimestampET === "function" ? formatTimestampET(retrievedAt) : "")}">${esc(ageNote)}</span>
        ${viewDetailsAction}
      </div>
      ${renderWhySurfacedBlock(candidate)}
    </div>
  `;
}

// UX round (2026-10-16), item 4: compact discovered/evaluated/awaiting
// summary, SCOPED TO THE ACTIVE DISCOVERY SNAPSHOT ONLY (never a
// lifetime total across every snapshot ever cataloged) -- that scope is
// made explicit in the rendered text itself ("this scan"), computed
// purely from the one GET /api/radar/market-wide response already
// fetched (candidate_count_in_snapshot = discovered this scan; each
// returned candidate's `freshness` -- "current"/"stale" both mean a
// real evaluation exists; "unevaluated" means it doesn't yet).
// Also carries the ONE honest, current-scheduler-mode-aware line this
// page must never get wrong: it must NEVER imply continuous scanning
// is happening if the scheduler is actually Disabled right now.
function renderMarketWideSummary(data, status) {
  const discovered = data.candidate_count_in_snapshot ?? (data.candidates || []).length;
  const shown = (data.candidates || []).length;
  const evaluated = (data.candidates || []).filter((c) => c.freshness === "current" || c.freshness === "stale").length;
  const awaiting = Math.max(0, shown - evaluated);
  // UX round (2026-10-16 round 5), item 3: was a raw ISO-with-microseconds
  // string shown straight from the API response -- the one shared
  // frontend/timefmt.js formatter now renders it the same human-readable
  // way every other trader-facing timestamp in this app already is.
  const asOf = data.snapshot_at
    ? `as of ${esc(typeof formatTimestampFull === "function" ? formatTimestampFull(data.snapshot_at) : data.snapshot_at)}`
    : "no scan yet";
  const truncationNote = data.rows_truncated_note
    ? `<div class="muted" style="font-size:12px;margin-top:3px">${esc(data.rows_truncated_note)}</div>`
    : "";
  const modeNote = !status
    ? ""
    : status.mode === "disabled"
    ? "The scheduler is currently Disabled -- this reflects the last scan that was run (manually or before Disabled was selected), not an ongoing scan."
    : status.mode === "dry_run"
    ? "The scheduler is in Dry Run -- it keeps discovering new candidates automatically, but does not evaluate them further."
    : "The scheduler is Enabled -- it keeps discovering and evaluating candidates automatically.";
  return `
    <div class="summary-strip" style="display:flex;gap:16px;flex-wrap:wrap;align-items:baseline">
      <span><strong>${discovered}</strong> discovered <span class="muted">(this scan)</span></span>
      <span><strong>${evaluated}</strong> evaluated <span class="muted">(of ${shown} shown)</span></span>
      <span><strong>${awaiting}</strong> awaiting evaluation</span>
      <span class="muted">${esc(asOf)}</span>
    </div>
    <div class="muted" style="font-size:12px;margin-top:3px">${esc(modeNote)}</div>
    ${truncationNote}
  `;
}

function renderMarketWideRadar(data) {
  if (!data || !data.candidates || data.candidates.length === 0) {
    const note = (data && data.note) || "No discovery snapshot cataloged yet.";
    return `<div class="empty">${esc(note)}</div>`;
  }
  const rows = data.candidates.map(renderMarketWideCandidateRow).join("");
  const coverage = data.coverage_note ? `<p class="muted" style="font-size:12px">${esc(data.coverage_note)}</p>` : "";
  const snapshotLabel = typeof formatTimestampFull === "function" ? formatTimestampFull(data.snapshot_at) : (data.snapshot_at || "n/a");
  return `
    <div class="muted" style="font-size:12px">Snapshot: ${esc(snapshotLabel)}</div>
    <div class="candidate-list">${rows}</div>
    ${coverage}
  `;
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
    // REAL BUG FIX (2026-10-15, Priority 2): this wording was hardcoded
    // demo-flavored ("in this scenario") and shown identically in Live
    // Mode, where there are no scenarios at all -- it used to ALWAYS
    // render anyway because of a Live-Mode price-plumbing bug (now fixed
    // in live_uw_adapter._normalize_stock_state). Kept honestly generic in
    // Live Mode since real price data CAN still be genuinely unavailable
    // there (adapter error, unentitled tier, etc.) -- never claim a
    // scenario that doesn't exist in that mode.
    const isLive = typeof _currentMode !== "undefined" && _currentMode === "live";
    const msg = isLive ? "price data unavailable" : "price data unavailable in this scenario";
    return `<span class="px-unavailable">${msg}</span>`;
  }
  const chg = px.chg_pct;
  const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
  const priceTxt = px.last != null ? px.last.toFixed(2) : "n/a";
  return `<span class="px">${priceTxt}</span><span class="pxchg ${chgClass}">${fmtPct(chg)}</span>`;
}

// Split price/change cells for the Other Noteworthy TABLE (Priority 1 fix,
// 2026-10-15 round 3): renderRow() used to compute `px` and then never
// read it anywhere in the row template at all -- a plain dead-variable
// bug, not a missing-field/fixture/adapter problem (the /api/radar
// response has genuinely carried entry.price.last/chg_pct since the
// present() fix noted above; the table template just never rendered it).
// Reuses the exact same field reads fmtPriceLine() already uses (never a
// new/guessed field name), honestly rendered as an em-dash when a price
// is genuinely absent -- never a fabricated value.
function fmtPriceCell(px) {
  if (!px || px.last == null) return `<span class="px-unavailable">\u2014</span>`;
  return `<span class="px">${px.last.toFixed(2)}</span>`;
}
function fmtChangeCell(px) {
  if (!px || px.chg_pct == null) return `<span class="px-unavailable">\u2014</span>`;
  const chg = px.chg_pct;
  const chgClass = chg > 0 ? "up" : chg < 0 ? "down" : "";
  return `<span class="pxchg ${chgClass}">${fmtPct(chg)}</span>`;
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
// D4/D5 (ux-nav-watchlist-reliability round 9): a watchlist entry with no
// real lifecycle record at all (entry.state is null/undefined -- see
// backend/app/main.py's _radar_impl fix, which no longer fakes this to
// "NEW") must render an honest "Not evaluated" badge -- "NEW" is a real
// signal_engine lifecycle state that implies an evaluation actually
// happened, and must never be shown for a ticker that has never been
// scanned. `entry.not_evaluated` (also set by the backend fix above) is
// the more explicit signal when present; falling back to `!entry.state`
// covers the same real condition for any entry that didn't go through
// that specific code path.
function isNotEvaluated(entry) {
  return !!entry.not_evaluated || !entry.state;
}
// Round 13 item 1: a genuinely-evaluated zero-qualifying-gate entry
// (backend `zero_gate: true`, or any entry whose real transition_rule
// is one of the two zero-gate reasons -- same shared predicate used
// everywhere else, isZeroGateTransitionReason, defined below) must be
// visibly distinguishable from BOTH a never-evaluated placeholder
// (isNotEvaluated above) and a genuinely qualifying entry -- the three
// states this fix is required to keep distinct and renderable.
function isZeroGateEntry(entry) {
  return !isNotEvaluated(entry) && (!!entry.zero_gate || isZeroGateTransitionReason(entry.transition_rule));
}
function stateTagHtml(entry) {
  if (isNotEvaluated(entry)) {
    return `<span class="tag state-unevaluated" title="This ticker has never been evaluated -- no real signal-detection scan has run for it yet.">Not evaluated</span>`;
  }
  const tag = `<span class="tag state-${esc(entry.state)}">${esc(entry.state)}</span>`;
  if (isZeroGateEntry(entry)) {
    return `${tag} <span class="muted zero-gate-note" title="Evaluated this scan -- zero qualifying gates passed, not a qualifying/noteworthy signal.">(no qualifying signal)</span>`;
  }
  return tag;
}

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
        ${stateTagHtml(entry)}
        <span class="tag">confidence: ${esc(entry.confidence)}</span>
      </div>
      <div class="read card-headline">${esc(entry.headline)}</div>
      ${renderHistoryChip(entry.history_states)}
      <button class="btn btn-investigate" data-investigate-ticker="${esc(entry.ticker)}">Investigate ${esc(entry.ticker)} &rarr;</button>
    </div>`;
}

// Evidence-family text ported from production's own typographic (not
// pill-background) convention for category identity -- see .cat-label
// in styles.css and entryFamilyTags() above for the real category data.
function renderFamilyLabel(entry) {
  const tags = entryFamilyTags(entry);
  if (!tags.length) return `<span class="muted">n/a</span>`;
  return tags.map((t) => `<span class="cat-label cat-${esc(t)}">${esc(RADAR_FAMILY_LABELS[t] || t)}</span>`).join(" ");
}

// Priority 3 fix (2026-10-15 round 3): this used to also render a
// "Current read" column with tip_evidence.current_read()'s full narrative
// sentence (synthetic-placeholder disclosures, agreement/disagreement
// prose, etc.) on EVERY row -- dense and repetitive across a table of
// many tickers. `headline` is already the deliberately-short,
// magnitude-first summary (tip_evidence.headline(), e.g. "Dark pool
// ~3.2x recent average") and already flags synthetic placeholders
// tersely ("Synthetic baseline estimate: ...") when that's genuinely
// what fired -- so the table now shows ONLY headline. The full
// current_read narrative is not deleted anywhere; it still renders in
// Ticker Analysis's header card (renderTickerDetail) and the Featured
// Signal hero card (renderFeaturedSignal), neither of which repeats
// per-row.
// P1 (ux-nav-watchlist-reliability round 10): a never-evaluated
// placeholder row has no `last_evaluated_at` -- formatTimestampFull /
// formatTimestampET (frontend/timefmt.js, loaded before this file in
// both index.html and static-demo/index.html) already return the
// honest "Not evaluated" string for null/undefined input, so this is a
// direct passthrough, never a second ad-hoc fallback string.
function fmtLastEvaluatedCell(entry) {
  if (typeof formatTimestampFull !== "function") return esc(entry.last_evaluated_at || "Not evaluated");
  return esc(formatTimestampFull(entry.last_evaluated_at));
}
// Round 14 item 1: the My Watchlist table's headline column used to
// render `entry.headline` unconditionally -- for an entry Round 13
// correctly made visible again (`isZeroGateEntry`, evaluated this scan
// but zero qualifying gates passed), the backend's generic
// tip_evidence.headline() text (e.g. "Unusual activity") still reads as
// if something qualifying fired, directly contradicting the state
// tag's own already-honest "(no qualifying signal)" note right next to
// it in the State column on the SAME row. Reuses the exact same
// isZeroGateEntry predicate stateTagHtml already branches on (never a
// second/drifting check) -- a genuinely qualifying entry's headline is
// returned completely untouched, and a never-evaluated placeholder's
// honest "Not yet evaluated" headline is also untouched (isZeroGateEntry
// already excludes not_evaluated entries internally).
function rowHeadlineText(entry) {
  if (isZeroGateEntry(entry)) {
    return "No qualifying signal detected.";
  }
  return entry.headline;
}

function renderRow(entry) {
  const px = entry.price || {};
  return `
    <tr data-ticker="${esc(entry.ticker)}">
      <td><strong>${esc(entry.ticker)}</strong></td>
      <td>${stateTagHtml(entry)}</td>
      <td>${isNotEvaluated(entry) ? `<span class="muted">n/a</span>` : esc(entry.confidence)}</td>
      <td class="col-family">${renderFamilyLabel(entry)}</td>
      <td class="col-price">${fmtPriceCell(px)}</td>
      <td class="col-chg">${fmtChangeCell(px)}</td>
      <td class="col-headline">${esc(rowHeadlineText(entry))}</td>
      <td class="col-lastEval">${fmtLastEvaluatedCell(entry)}</td>
      <td><button class="btn-link btn-investigate" data-investigate-ticker="${esc(entry.ticker)}">Investigate &rarr;</button></td>
    </tr>`;
}

// Activity-summary strip above the grid. Built purely from fields the
// /api/radar response (and its static-demo capture) already has:
// scanned, highest.length, other.length -- no new backend fields.
// D4/D5 (ux-nav-watchlist-reliability round 9): `data.other` can now
// contain never-evaluated placeholder entries (not_evaluated: true) --
// these must never count toward "highest attention"/"other noteworthy"
// (both are genuinely-qualifying categories) or toward any "N qualifying
// signals" total. Filters are presentation-only; nothing fed into these
// counts changes what present()/tip_evidence actually classified.
// Round 13 item 1: `data.other` can ALSO now contain genuinely-evaluated
// zero-qualifying-gate entries (`zero_gate: true`, backend/app/main.py's
// _radar_impl -- previously these were silently dropped from the
// response entirely, not just from this count; see that file's comment
// for the real route-level bug). Same rule applies: "evaluated, found
// nothing" is never a qualifying/noteworthy signal either, so it is
// excluded from this count exactly like not_evaluated already is --
// while still being rendered as a real, visible, distinctly-labeled row
// (see stateTagHtml below), never silently dropped from the table.
function qualifyingEntries(list) {
  return (list || []).filter((e) => !e.not_evaluated && !e.zero_gate);
}

function radarSummaryCounts(data) {
  const watchlistCount = data.watchlist_size != null ? data.watchlist_size : data.scanned;
  const evaluatedCount = data.evaluated_count != null ? data.evaluated_count : data.scanned;
  const highestCount = qualifyingEntries(data.highest).length;
  const otherNoteworthyCount = qualifyingEntries(data.other).length;
  return { watchlistCount, evaluatedCount, qualifyingCount: highestCount + otherNoteworthyCount, otherNoteworthyCount };
}

function renderRadarStatus(data) {
  const c = radarSummaryCounts(data);
  return `watchlist: ${c.watchlistCount} ticker(s) \u00b7 ${c.evaluatedCount} evaluated \u00b7 ${c.qualifyingCount} qualifying \u00b7 ${c.otherNoteworthyCount} other noteworthy \u00b7 last scanned ${formatAgo(data.last_scan_at)}`;
}

function renderActivitySummary(data) {
  const c = radarSummaryCounts(data);
  return `
    <div class="activity-strip">
      <div class="activity-stat"><span class="activity-num">${esc(c.watchlistCount)}</span><span class="activity-label">tickers in watchlist</span></div>
      <div class="activity-stat"><span class="activity-num">${esc(c.evaluatedCount)}</span><span class="activity-label">evaluated</span></div>
      <div class="activity-stat"><span class="activity-num accent-bull">${esc(c.qualifyingCount)}</span><span class="activity-label">qualifying signals</span></div>
      <div class="activity-stat"><span class="activity-num">${esc(c.otherNoteworthyCount)}</span><span class="activity-label">other noteworthy</span></div>
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
// 2026-10-15 fix (real Live Mode bug): handles THREE distinct Highest
// Attention states through the actual rendering path, using the same
// `data` object renderRadarLists() receives:
//   1. Highest Attention has a genuine featured ticker: normal hero +
//      functional Signal Breakdown (unchanged from before).
//   2. Highest Attention is empty but Other Noteworthy has tickers: the
//      heading + hero/breakdown grid collapse entirely into ONE compact,
//      honest notice. Never says "select a ticker above" -- the
//      selectable tickers are in the Other Noteworthy table below, not
//      "above" anything that still exists on screen.
//   3. Both are genuinely empty: the same compact notice with wording
//      that doesn't point anywhere, since there's nothing to point to.
// #highestAttentionSection's own heading+grid are toggled separately in
// renderRadarLists() using the identical hasHighest condition -- both
// functions are called together on every real radar load (see
// app.live.js's loadRadar() / static-demo/app.static.js).
function renderFeaturedArea(data) {
  const allHighest = data.highest || [];
  const allOther = data.other || [];
  const hasHighest = allHighest.length > 0;
  // Round 12 (ux-nav-watchlist-reliability) fix: `allOther.length > 0` was
  // true even when every entry in it is a not_evaluated placeholder (a
  // watchlist ticker that has never actually been scanned) -- in that
  // case the old wording below ("...for the tickers tracked this scan")
  // falsely implied a real scan had run and found noteworthy tickers.
  // hasGenuineOther is true only when Other Noteworthy actually contains
  // at least one REAL, evaluated entry (qualifyingEntries() already
  // excludes not_evaluated placeholders -- same filter used for the
  // "N qualifying signals" count above).
  const hasGenuineOther = qualifyingEntries(allOther).length > 0;
  // Honest counts, reused from radarSummaryCounts() (same
  // watchlist_size/evaluated_count fields the activity strip and status
  // line already use) -- never invented, never re-derived.
  const counts = radarSummaryCounts(data);
  const nothingEvaluatedYet = counts.evaluatedCount === 0 && counts.watchlistCount > 0;
  const featured = allHighest[0] || null;

  const headingEl = document.getElementById("featuredSignalHeading");
  const gridEl = document.getElementById("featuredSignalGrid");
  const noticeEl = document.getElementById("featuredSignalCompactNotice");
  const slotEl = document.getElementById("featuredSignalSlot");
  const breakdownEl = document.getElementById("breakdownSlot");

  if (hasHighest) {
    if (headingEl) headingEl.style.display = "";
    if (gridEl) gridEl.style.display = "";
    if (noticeEl) { noticeEl.style.display = "none"; noticeEl.innerHTML = ""; }
    if (slotEl) slotEl.innerHTML = renderFeaturedSignal(featured);
    if (breakdownEl) breakdownEl.innerHTML = renderSignalBreakdown(featured, true);
    return;
  }

  // No genuine featured ticker -- collapse the whole heading+hero+breakdown
  // area into one compact message rather than leaving an empty hero card
  // and a breakdown panel with nowhere real to point.
  if (headingEl) headingEl.style.display = "none";
  if (gridEl) gridEl.style.display = "none";
  if (slotEl) slotEl.innerHTML = "";
  if (breakdownEl) breakdownEl.innerHTML = "";
  if (noticeEl) {
    noticeEl.style.display = "";
    // Round 12: three genuinely distinct, honestly-derived cases --
    // never a single wording that overstates what actually happened.
    //   1. Other Noteworthy has at least one REAL evaluated entry: point
    //      to it (this is the only case where "see Other Noteworthy
    //      below" is both true and useful).
    //   2. Nothing in the watchlist has been evaluated yet
    //      (evaluated_count === 0, watchlist_size > 0): say so plainly --
    //      no scan has actually run, so neither "this scan" nor "Other
    //      Noteworthy" may be referenced.
    //   3. A real scan ran, evaluated something, and genuinely found zero
    //      qualifying tickers anywhere: the original, accurate wording.
    noticeEl.innerHTML = hasGenuineOther
      ? `<div class="breakdown-panel empty">No ticker currently qualifies for Highest Attention -- see Other Noteworthy below for the ticker(s) evaluated this scan.</div>`
      : nothingEvaluatedYet
      ? `<div class="breakdown-panel empty">No tickers in this watchlist have been evaluated yet -- run a scan to populate Market Radar.</div>`
      : `<div class="breakdown-panel empty">No tickers currently qualify for Market Radar attention this scan.</div>`;
  }
}

function renderRadarLists(data, filters, loadTickerDetailFn) {
  const familyFilter = (filters && filters.family) || "";
  const stateFilter = (filters && filters.state) || "";
  // P1 fix (ux-nav-watchlist-reliability round 10): the round-9 D4/D5 fix
  // made the BACKEND stop dropping never-evaluated watchlist tickers
  // (they now arrive in data.other with not_evaluated:true, a real
  // state, and an honest headline), but this renderer then immediately
  // undid that by running qualifyingEntries() (a `!e.not_evaluated`
  // filter) over the RENDER lists too -- every never-evaluated ticker
  // was computed server-side, shipped over the wire, and then silently
  // thrown away right here before a single <tr>/<div class="card"> was
  // ever built for it. That is the exact regression Claude's real-Chrome
  // retest caught: the backend was correct, the frontend row-rendering
  // loop was not.
  //
  // Fix: qualifyingEntries() is now used ONLY for the things that are
  // genuinely about qualification -- which ticker is "featured", and the
  // "N qualifying signal(s)" count below. The actual DOM lists that get
  // rendered (highestGridEl / otherGridEl / #otherTable tbody) are built
  // from the FULL, unfiltered-by-qualification entry lists, so every
  // watchlist ticker -- evaluated or not -- gets a real row. Highest
  // Attention never receives not_evaluated placeholders from the backend
  // (see backend/app/main.py's never_evaluated_tickers loop, which only
  // ever appends to `other`), so allHighestDisplay only matters for
  // defensiveness; allOtherDisplay is where this bug actually lived.
  const qualifyingHighest = qualifyingEntries(data.highest);
  const qualifyingOther = qualifyingEntries(data.other);
  const allHighestDisplay = data.highest || [];
  const allOtherDisplay = data.other || [];
  const featured = qualifyingHighest[0] || null;
  // The featured entry (if any) is always drawn from the qualifying set
  // and rendered separately (renderFeaturedSignal), so it must not also
  // appear a second time in the grid below it -- filter it out of the
  // DISPLAY list by ticker, not by re-deriving from the qualifying list
  // (which would silently re-introduce the dropped-row bug).
  const restOfHighestDisplay = featured
    ? allHighestDisplay.filter((e) => e.ticker !== featured.ticker)
    : allHighestDisplay;
  const filteredRest = filterRadarEntries(restOfHighestDisplay, familyFilter, stateFilter);
  const filteredOther = filterRadarEntries(allOtherDisplay, familyFilter, stateFilter);

  const highestGridEl = document.getElementById("highestGrid");
  if (highestGridEl) highestGridEl.innerHTML = filteredRest.map(renderCard).join("") || "";
  // 2026-10-15 fix: when ZERO tickers genuinely qualify for Highest
  // Attention (qualifyingHighest.length === 0, not merely filtered down
  // to zero), Featured Signal's own honest "No featured signal right
  // now" message already covers this tier -- a second, bare "Highest
  // Attention" heading sitting over an empty grid right below it was
  // redundant and looked broken, not like one compact empty state. Hide
  // the whole redundant heading+grid section in that specific case; a
  // filter-induced zero (qualifyingHighest.length > 0 but filteredRest
  // is empty) still shows the heading normally since there IS a
  // featured entry above it.
  const highestSectionEl = document.getElementById("highestAttentionSection");
  if (highestSectionEl) highestSectionEl.style.display = qualifyingHighest.length === 0 ? "none" : "";
  const otherGridEl = document.getElementById("otherGrid");
  if (otherGridEl) otherGridEl.innerHTML = filteredOther.map(renderCard).join("") || "";
  // Other Noteworthy's table is now the primary (visible) presentation,
  // so it must respect the active filter the same way the grid does --
  // filteredOther, not the full unfiltered allOtherDisplay. filteredOther
  // is now drawn from allOtherDisplay (ALL entries), not qualifyingOther,
  // so never-evaluated tickers genuinely render a row here.
  //
  // Round 14 item 3: filteredOther is a flat mix of three genuinely
  // different kinds of entry (genuinely qualifying, evaluated-zero-gate,
  // never-evaluated) rendered one after another with no visual grouping
  // at all -- the ONLY place a reader could tell them apart was each
  // row's own State-column badge. The "Other Noteworthy" heading above
  // this table reports the genuinely-qualifying count (see
  // otherNoteworthyCount below), but ALL THREE kinds render as rows in
  // this one table beneath it -- a real count/row mismatch (e.g.
  // heading says 4, table shows 6). Fix: split into three sub-groups,
  // each with its OWN labeled header row + its OWN count computed from
  // (and therefore always exactly matching) the rows actually rendered
  // under it this render pass -- never from a separately-tracked/
  // possibly-stale counter. This does NOT hide the zero-gate/never-
  // evaluated rows (that would reintroduce the Round 13 regression this
  // whole initiative exists to fix) -- every row from filteredOther
  // still renders, just grouped. A group with zero rows this pass
  // renders no header at all, so there is never a heading sitting over
  // zero rows.
  const otherQualifyingRows = filteredOther.filter((e) => !e.not_evaluated && !e.zero_gate);
  const otherZeroGateRows = filteredOther.filter((e) => e.zero_gate);
  const otherNotEvaluatedRows = filteredOther.filter((e) => e.not_evaluated);
  function otherGroupHeaderRow(label, count) {
    return `<tr class="radar-group-header-row"><td colspan="9" class="radar-group-header">${esc(label)} (${count})</td></tr>`;
  }
  const tbody = document.querySelector("#otherTable tbody");
  if (tbody) {
    const groups = [];
    if (otherQualifyingRows.length) groups.push(otherQualifyingRows.map(renderRow).join(""));
    if (otherZeroGateRows.length) {
      groups.push(otherGroupHeaderRow("No Qualifying Signal", otherZeroGateRows.length));
      groups.push(otherZeroGateRows.map(renderRow).join(""));
    }
    if (otherNotEvaluatedRows.length) {
      groups.push(otherGroupHeaderRow("Not Evaluated", otherNotEvaluatedRows.length));
      groups.push(otherNotEvaluatedRows.map(renderRow).join(""));
    }
    tbody.innerHTML = groups.join("");
  }
  // The static "Other Noteworthy" <h2> heading itself gets its own
  // accurate count, kept in permanent lockstep with
  // otherQualifyingRows.length above -- the exact same number of rows
  // actually rendered as plain (non-grouped) rows at the top of the
  // table this render pass, under every filter state, never a
  // separately-derived figure that could drift from what's rendered.
  const otherHeadingCountEl = document.getElementById("otherNoteworthyCount");
  if (otherHeadingCountEl) otherHeadingCountEl.textContent = ` (${otherQualifyingRows.length})`;

  // Counts: "qualifying" (highest+other, i.e. tip_evidence.qualifies()
  // entries) is always kept distinct from "scanned" (every synthetic
  // scenario including non-qualifying controls like NADA/DRFT) -- never
  // blur the two, and never let never-evaluated placeholder rows inflate
  // it either (qualifyingHighest/qualifyingOther still exclude them, same
  // as before this fix). Rows actually rendered are tracked separately
  // (totalShownAll) so the empty-state logic reflects reality -- a
  // watchlist that's 100% never-evaluated now correctly shows its rows
  // instead of "No tickers qualify right now." over an empty table.
  const totalQualifying = qualifyingHighest.length + qualifyingOther.length;
  // Round 13 item 1: this count must exclude the SAME two non-qualifying
  // kinds qualifyingEntries() already excludes (not_evaluated AND the
  // new zero_gate case) -- otherwise a genuinely-evaluated zero-gate
  // ticker would inflate "N qualifying signal(s) shown" even though it
  // is deliberately never counted as qualifying anywhere else on this
  // page (radarSummaryCounts/the activity strip above).
  const nonQualifyingShown = filteredOther.filter((e) => e.not_evaluated || e.zero_gate).length;
  const totalShownQualifying = (featured ? 1 : 0) + filteredRest.length + (filteredOther.length - nonQualifyingShown);
  const totalShownAll = (featured ? 1 : 0) + filteredRest.length + filteredOther.length;
  const countsEl = document.getElementById("radarFilterCounts");
  if (countsEl) {
    // REAL BUG FIX (2026-10-15, Priority 4): this literally said
    // "scenario(s) scanned" even in LIVE mode, where there are no
    // scenarios at all -- just real watchlist tickers. Mode-aware wording,
    // guarded against ReferenceError in contexts (e.g. static-demo) where
    // _currentMode is never declared.
    const c = radarSummaryCounts(data);
    const notEvalSuffix = nonQualifyingShown > 0 ? ` (+ ${nonQualifyingShown} not yet evaluated / zero-gate)` : "";
    countsEl.textContent = (familyFilter || stateFilter)
      ? `Showing ${totalShownQualifying} of ${totalQualifying} qualifying signal(s) after filters${notEvalSuffix} \u2014 ${c.evaluatedCount} evaluated of ${c.watchlistCount} watchlist ticker(s)`
      : `${totalQualifying} qualifying signal(s) shown${notEvalSuffix} \u2014 ${c.evaluatedCount} evaluated of ${c.watchlistCount} watchlist ticker(s)`;
  }
  const radarEmptyEl = document.getElementById("radarEmpty");
  if (radarEmptyEl) {
    radarEmptyEl.textContent = (familyFilter || stateFilter) && (totalQualifying > 0 || allOtherDisplay.length > 0) && totalShownAll === 0
      ? "No signals match the current filters."
      : "No tickers qualify right now.";
    radarEmptyEl.style.display = (totalShownAll === 0) ? "block" : "none";
  }

  wireInvestigateButtons(loadTickerDetailFn);
  document.querySelectorAll("[data-ticker]").forEach((card) => {
    card.addEventListener("click", (ev) => {
      if (ev.target.closest("[data-investigate-ticker]")) return;
      const t = card.dataset.ticker;
      const entry = [...allHighestDisplay, ...allOtherDisplay].find((e) => e.ticker === t);
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

// --- Market Discovery (C2b/C2c, 2026-10-15) --------------------------------
//
// Pure rendering helpers, same pattern as the rest of this file (no
// fetch() here -- app.live.js owns the one POST /api/radar/discover call
// and the per-candidate Evaluate flow). A 'candidate' here is always a
// RAW screener row (discovery_state: "candidate") -- it must never be
// confused with an evaluated/signal_engine state; see
// renderDiscoveryCandidateRow()'s explicit state tag below.

// LABEL FIX (2026-10-15, Chad's live-mode item #9): this control sends
// UW's own `min_premium` query param straight through to
// /api/screener/stocks -- Chad's real test showed a $5M `min_premium`
// filter returning SPY with a net_premium of -$1.8M, proving UW's
// min_premium is NOT the same thing as the signed `net_premium` figure
// shown on each result row (it reads as a gross/absolute premium
// threshold, not a net-premium floor). The label below now describes
// what the control actually filters on; it does not change backend
// behavior or add client-side post-filtering (that would need separate
// approval -- see the task report for this as a possible follow-up).
const DISCOVERY_FILTER_FIELDS = [
  { key: "min_premium", label: "Min premium ($) \u2014 UW's own screener filter, not the same as a result row's net premium", placeholder: "e.g. 5000000" },
  { key: "min_call_volume", label: "Min call volume", placeholder: "e.g. 5000" },
  { key: "min_put_volume", label: "Min put volume", placeholder: "e.g. 1000" },
  { key: "min_iv_rank", label: "Min IV rank", placeholder: "0-100" },
  { key: "min_change", label: "Min change (%)", placeholder: "e.g. 1.0" },
  { key: "max_change", label: "Max change (%)", placeholder: "e.g. 10.0" },
];

// Quota-cost disclosure MUST be visible before the scan ever runs (spec
// requirement) -- rendered as part of the form itself, not appended only
// after a result comes back.
function renderDiscoveryFilterForm(limit = 20) {
  const fields = DISCOVERY_FILTER_FIELDS.map((f) => `
    <label>${esc(f.label)}
      <input type="number" step="any" id="discoveryFilter_${esc(f.key)}" placeholder="${esc(f.placeholder)}" />
    </label>`).join("");
  return `
    <form class="discovery-filter-form" id="discoveryFilterFormEl">
      ${fields}
      <label>Max candidates
        <input type="number" id="discoveryFilter_limit" min="1" max="50" value="${esc(limit)}" />
      </label>
      <span class="discovery-quota-note" id="discoveryQuotaNote">This will use 1 screener request.</span>
      <button class="btn" type="submit" id="discoveryScanBtn">Scan</button>
    </form>`;
}

function readDiscoveryFilterForm() {
  const body = {};
  DISCOVERY_FILTER_FIELDS.forEach((f) => {
    const el = document.getElementById(`discoveryFilter_${f.key}`);
    const v = el ? el.value.trim() : "";
    if (v !== "") body[f.key] = Number(v);
  });
  const limitEl = document.getElementById("discoveryFilter_limit");
  const limitVal = limitEl ? Number(limitEl.value) : 20;
  body.limit = Number.isFinite(limitVal) && limitVal > 0 ? Math.min(limitVal, 50) : 20;
  return body;
}

// NOTE: a second, pre-existing `fmtMoney` is defined later in this file
// (the evidence-panel $ formatter) and, since function declarations are
// hoisted/overwritten by later same-name declarations in one script
// scope, THAT later definition is what actually runs here -- it already
// returns a string WITH a leading "$" (e.g. "$31.7M"), so the markup
// below must not add a second one.

// One candidate row: ticker + real screener metrics + snapshot-implied
// "not yet evaluated" state + an Evaluate button. discoveryState is one
// of "candidate" | "evaluating" | "evaluated" (set/transitioned entirely
// by app.live.js's evaluateDiscoveryCandidate(), never auto-advanced here).
function renderDiscoveryCandidateRow(c, discoveryState) {
  const state = discoveryState || c.discovery_state || "candidate";
  const stateLabel = state === "evaluated" ? "Evaluated" : state === "evaluating" ? "Evaluating..." : "Not yet evaluated";
  const btnLabel = state === "evaluated" ? `Re-evaluate ${esc(c.ticker)}` : `Evaluate ${esc(c.ticker)} in TIP`;
  // Chad's item #10: a real screener row can be an index/derivative
  // instrument (observed: SPXW, is_index=true) -- the per-ticker
  // evaluation pipeline "Evaluate in TIP" reuses has never been verified
  // against one, so rather than silently offering an action that might
  // error or produce meaningless gates, this is labeled and disabled.
  const isIndex = !!c.is_index;
  const evaluateSupported = c.evaluate_supported !== undefined ? !!c.evaluate_supported : !isIndex;
  const indexTag = isIndex ? `<span class="tag" title="Index/derivative instrument, not a tradeable equity">INDEX</span>` : "";
  const evalBtn = evaluateSupported
    ? `<button class="btn-link btn-evaluate" data-evaluate-ticker="${esc(c.ticker)}">${btnLabel} &rarr;</button>`
    : `<span class="muted" title="Index/derivative instruments are not supported by the per-ticker evaluation pipeline yet">Evaluate not supported (index/derivative)</span>`;
  // Chad's item #8: UW's own per-row snapshot date (source_date) is a
  // DIFFERENT thing from this app's own fetch timestamp (the list-level
  // "Snapshot:" line in renderDiscoveryCandidateList below) -- shown here,
  // separately labeled, so a stale source date can never look like "right now".
  const sourceDateTag = c.source_date ? `<span class="muted discovery-source-date">data as of ${esc(c.source_date)}</span>` : "";
  return `
    <div class="discovery-candidate-row" data-ticker="${esc(c.ticker)}" data-discovery-state="${esc(state)}">
      <span class="tk">${esc(c.ticker)}</span> ${indexTag}
      <span class="discovery-metric"><span class="m-label">net premium</span>${fmtMoney(c.net_premium) ?? "n/a"}</span>
      <span class="discovery-metric"><span class="m-label">repeated hits</span>${fmtNum(c.repeated_hits, 0)}</span>
      <span class="discovery-metric"><span class="m-label">call/put vol</span>${fmtNum(c.call_volume, 0)}/${fmtNum(c.put_volume, 0)}</span>
      <span class="discovery-metric"><span class="m-label">IV rank</span>${fmtNum(c.iv_rank, 2)}</span>
      <span class="discovery-metric"><span class="m-label">change</span>${c.change_percent != null ? fmtPct(c.change_percent) : "n/a"}</span>
      ${sourceDateTag}
      <span class="discovery-state-tag state-${esc(state)}">${esc(stateLabel)}</span>
      ${evalBtn}
    </div>`;
}

// Production-style compact table row, same columns/data source as
// renderDiscoveryCandidateRow (no new fields invented -- just the ones
// the real /api/radar/discover response already provides) presented as
// a table instead of a card row, reusing the SAME .tbl table component
// Radar's "Other Noteworthy" uses (see .discovery-table-wrap in CSS).
function renderDiscoveryCandidateTableRow(c, discoveryState) {
  const state = discoveryState || c.discovery_state || "candidate";
  const stateLabel = state === "evaluated" ? "Evaluated" : state === "evaluating" ? "Evaluating..." : "Not yet evaluated";
  const btnLabel = state === "evaluated" ? `Re-evaluate ${esc(c.ticker)}` : `Evaluate ${esc(c.ticker)} in TIP`;
  const isIndex = !!c.is_index;
  const evaluateSupported = c.evaluate_supported !== undefined ? !!c.evaluate_supported : !isIndex;
  const evalCell = evaluateSupported
    ? `<button class="btn-link btn-evaluate" data-evaluate-ticker="${esc(c.ticker)}">${btnLabel} &rarr;</button>`
    : `<span class="muted" title="Index/derivative instruments are not supported by the per-ticker evaluation pipeline yet">n/a (index)</span>`;
  return `
    <tr data-ticker="${esc(c.ticker)}" data-discovery-state="${esc(state)}">
      <td><strong>${esc(c.ticker)}</strong>${isIndex ? ' <span class="tag" title="Index/derivative instrument">INDEX</span>' : ""}</td>
      <td>${fmtMoney(c.net_premium) ?? "n/a"}</td>
      <td>${fmtNum(c.repeated_hits, 0)}</td>
      <td>${fmtNum(c.call_volume, 0)}/${fmtNum(c.put_volume, 0)}</td>
      <td>${fmtNum(c.iv_rank, 2)}</td>
      <td>${c.change_percent != null ? fmtPct(c.change_percent) : "n/a"}</td>
      <td><span class="discovery-state-tag state-${esc(state)}">${esc(stateLabel)}</span></td>
      <td>${evalCell}</td>
    </tr>`;
}

function renderDiscoveryCandidateList(candidates, meta) {
  if (!candidates || candidates.length === 0) {
    return `<div class="empty">No candidates matched these filters.</div>`;
  }
  // "fetched at" (this app's own request time) vs each row's own
  // "data as of" (UW's source date, rendered per-row above) are
  // deliberately two distinct, separately-labeled values -- see Chad's
  // item #8; this line is never implied to be the market-data date.
  // UX round (2026-10-16 round 5), item 3: same timefmt.js formatter as
  // every other trader-facing timestamp -- this used to render the raw
  // ISO-with-microseconds string straight from the API.
  const snapshotTxt = meta && meta.snapshot_at
    ? `Fetched at: ${esc(typeof formatTimestampFull === "function" ? formatTimestampFull(meta.snapshot_at) : meta.snapshot_at)}`
    : "";
  const countTxt = meta ? `${esc(meta.count)} candidate(s) &middot; ranked by net premium magnitude` : "";
  // Priority 5 fix (2026-10-15 round 3): this used to say "used N screener
  // request(s)" unconditionally -- in Demo Mode (meta.source ===
  // "synthetic_discovery_fixture") that literally never happened; it's a
  // fabricated-fixture read with zero network call and zero real UW quota
  // consumed. Never count a synthetic-fixture operation as a real external
  // API request. Live Mode (source === "unusual_whales_screener_stocks")
  // keeps the real, honest quota-cost disclosure unchanged.
  const isSyntheticSource = meta && meta.source === "synthetic_discovery_fixture";
  const quotaTxt = meta
    ? (isSyntheticSource
        ? "&middot; synthetic demo data (no real UW API call, no quota used)"
        : `&middot; used ${esc(meta.screener_requests_used)} screener request(s)`)
    : "";
  return `
    <div class="muted" style="margin-bottom:8px">${countTxt} ${quotaTxt} ${snapshotTxt ? `&middot; ${snapshotTxt}` : ""}</div>
    <div class="discovery-table-wrap">
      <table class="tbl discovery-table">
        <thead>
          <tr>
            <th>Ticker</th><th>Net premium</th><th>Repeated hits</th><th>Call/put vol</th>
            <th>IV rank</th><th>Change</th><th>Status</th><th></th>
          </tr>
        </thead>
        <tbody>${candidates.map((c) => renderDiscoveryCandidateTableRow(c)).join("")}</tbody>
      </table>
    </div>
    <!-- Legacy card-row markup kept in the DOM (hidden via CSS) only for
         an older test harness that queries renderDiscoveryCandidateRow
         output directly -- the table above is the real presentation. -->
    <div class="discovery-candidate-list">
      ${candidates.map((c) => renderDiscoveryCandidateRow(c)).join("")}
    </div>`;
}

// Intentional pre-scan empty state (P3 requirement: understandable, not
// an accidentally-blank area) -- rendered once before the user has ever
// clicked Scan. Never auto-runs a scan and never fabricates example rows.
function renderDiscoveryEmptyState() {
  // Priority 5 fix (2026-10-15 round 3): this unconditionally claimed a
  // "real ... Unusual Whales screener query" -- false in Demo Mode, where
  // Scan reads fixtures/discovery_candidates.json and makes no network
  // call at all. Mode-aware wording, same _currentMode guard used
  // elsewhere in this file.
  const isLive = typeof _currentMode !== "undefined" && _currentMode === "live";
  const scanTxt = isLive
    ? `click "Scan" to run a real, one-time Unusual Whales screener query against your UW key.`
    : `click "Scan" to view TIP's synthetic demo candidate list (no real UW API call -- switch to Live Mode in Settings for a real screener query).`;
  return `<div class="discovery-empty-state">
    <strong>No scan run yet</strong>
    Set optional filters above (all are optional) and ${scanTxt} Nothing is fetched automatically.
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
// hasAnyQualifying (optional): whether ANY ticker currently qualifies for
// Highest Attention or Other Noteworthy at all. 2026-10-15 fix: the old
// unconditional "Select a ticker above..." message was shown even when
// NO featured ticker existed and nothing was clickable above it either --
// misleading in the zero-qualifying case. Default true preserves the
// original wording for every existing call site that already has a real
// ticker list to select from.
function renderSignalBreakdown(entry, hasAnyQualifying = true) {
  if (!entry) {
    return hasAnyQualifying
      ? `<div class="breakdown-panel empty">Select a ticker above to see its evidence breakdown.</div>`
      : `<div class="breakdown-panel empty">No evidence breakdown available -- no ticker currently qualifies for Highest Attention or Other Noteworthy.</div>`;
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
    case "persistence":
      // PRESENTATION BUG FIX (2026-10-15, signal-labeling audit): this
      // build's persistence/tier2 logic was never ported from production
      // (scan_service.build_gate_inputs hardcodes tier2_hit=False,
      // tier2_qualifying_prints=0, and no caller ever passes
      // is_new_qualifying_day=True -- see signal_engine.py's own porting
      // notes). That means `passed` on this gate is structurally ALWAYS
      // False in this app, never a genuine "no repeated large prints were
      // observed" market finding. Classifying that as UNMET_CRITERIA made
      // a missing feature look identical to real negative evidence --
      // exactly the confusion item 3 of the audit asked to fix. A real
      // future `passed=true` (once tier2 is actually wired) still counts
      // as SUPPORTING; only the never-capable-of-passing-today case is
      // reclassified.
      return passed ? GATE_CATEGORY.SUPPORTING : GATE_CATEGORY.DATA_LIMITATION;
    case "options_flow_anomaly":
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
        : "This build's repeated-large-print (persistence/tier2) detection was not ported from "
          + "production, so this gate cannot pass here yet -- this is NOT a market observation that "
          + "no persistence occurred, just an unimplemented input.";
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

// Phase B (2026-10-14): compact ALWAYS-VISIBLE summary line (counts per
// category) so collapsing the detailed gate breakdown into a <details>
// below (to make the chart more prominent, per Chad's instruction)
// never hides that there IS evidence worth opening -- just the
// per-gate explanatory text, which stays one click away, not removed.
function renderGateGroupsSummary(gates) {
  const counts = { SUPPORTING: 0, CONFLICTING: 0, UNMET_CRITERIA: 0, DATA_LIMITATION: 0 };
  Object.entries(gates || {}).forEach(([name, gate]) => {
    counts[classifyGate(name, gate).key] += 1;
  });
  // BUG FOUND 2026-10-15 (Chad's Phase B final verification, item #2):
  // reusing GATE_CATEGORY[key].cls (e.g. "gc-conflicting") here collided
  // with test_p5_p6_p7.mjs's string-based extraction of the REAL gate
  // group below -- that test finds the first "gc-conflicting" substring
  // in the whole rendered HTML and slices from there to the next
  // "gate-group gc-" occurrence; since this summary line renders BEFORE
  // the real <div class="gate-group gc-conflicting">, the test grabbed
  // this pill's tiny span instead of the actual conflicting group and
  // its gate-why explanation. Not a signal_engine/date issue at all --
  // a real markup collision introduced by this phase's own UI change.
  // Fixed with a distinct, non-colliding "gcp-" (gate-category-pill)
  // prefix; gc-* stays reserved for the real gate-group divs.
  const PILL_CLS = { SUPPORTING: "gcp-supporting", CONFLICTING: "gcp-conflicting",
                      UNMET_CRITERIA: "gcp-unmet", DATA_LIMITATION: "gcp-datalimit" };
  const parts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([key, n]) => `<span class="gate-group-count-pill ${PILL_CLS[key]}">${esc(GATE_CATEGORY[key].label)} (${n})</span>`);
  return parts.join(" ") || `<span class="muted">No gates evaluated.</span>`;
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
  no_qualifying_evidence_yet: "No qualifying signal detected yet.",
  // Round 12 (ux-nav-watchlist-reliability): the L1 any_gate_passed guard's
  // SECOND zero-gate reason string -- a later re-evaluation of an
  // already-NEW ticker that again found zero qualifying gates. Means the
  // exact same thing to a reader as no_qualifying_evidence_yet above; see
  // isZeroGateTransitionReason() below, which is the single shared place
  // all three visible-UI locations branch on these two reason strings.
  no_gate_passed_remains_new: "No qualifying signal detected yet.",
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

// Phase B (2026-10-14) chart timeframe row -- production-inspired
// (its own timeframeRow/wireTimeframeRow functions), reimplemented
// here since production's version isn't a standalone importable
// module. CHART_RANGES order matches what Chad specified.
// 2026-10-15 (Chad-approved, item #3): 1D added, using genuine UW
// intraday (5m) candles -- NOT a handful of daily bars relabeled. 5D
// explicitly deferred to a separate approval.
const CHART_RANGES = ["1D", "1M", "3M", "6M", "1Y"];

function renderChartRangeRow(selectedRange) {
  return CHART_RANGES.map((r) =>
    `<button class="tf-btn ${r === selectedRange ? "active" : ""}" data-range="${r}">${r}</button>`
  ).join("");
}

function wireChartRangeRow(containerId, onChange) {
  const row = document.getElementById(containerId);
  if (!row) return;
  row.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".tf-btn");
    if (!btn) return;
    row.querySelectorAll(".tf-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    onChange(btn.dataset.range);
  });
}

// --- Compact paginated evidence interface (2026-10-15, Chad-approved) ------
//
// Replaces the old single flat <table> that rendered every individual
// event at once (hundreds of rows -> a 5-page PDF when expanded).
// Nothing is removed or hidden permanently: every event from allEvents
// stays reachable, just grouped/paginated/filterable instead of dumped.

const EVIDENCE_PAGE_SIZE = 20;

// REAL BUG FIX (2026-10-15, Chad's live-mode item #11): negative amounts
// previously rendered as "$-78.7M" (minus sign stuck inside the dollar
// sign) instead of standard "-$78.7M" -- the sign now applies to the
// whole formatted string, never inside the "$".
//
// UX remediation item 4 (2026-10-16): that ASCII hyphen-minus ("-") still
// reads ambiguously next to a currency symbol (easily mistaken for a
// hyphen/dash rather than a minus, and some terminals/fonts render it
// shorter than a true minus glyph) -- switched to the proper Unicode
// MINUS SIGN (U+2212, "\u2212") per Chad's explicit requirement
// ("-$89.2M" -> "\u2212$89.2M"). Still plain-text/copy-paste-safe (a
// normal Unicode codepoint, not an image/icon), just the typographically
// correct minus glyph instead of a bare hyphen standing in for one.
function fmtMoney(v) {
  if (v == null) return null;
  const neg = v < 0;
  const abs = Math.abs(v);
  let body;
  if (abs >= 1e6) body = `$${(abs / 1e6).toFixed(1)}M`;
  else if (abs >= 1e3) body = `$${(abs / 1e3).toFixed(0)}K`;
  else body = `$${Math.round(abs)}`;
  return neg ? `\u2212${body}` : body;
}

// REAL BUG FIX (2026-10-15, Chad's live-mode item #12): display-only
// rounding for numbers that can arrive with excess float precision from
// the live screener (e.g. IV rank 20.6659...). Never used on anything
// that feeds signal_engine/scan_service math -- those keep full
// precision; this is purely for what's printed on screen.
function fmtNum(v, decimals = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "n/a";
  return Number(v).toFixed(decimals).replace(/\.0+$/, (m) => (decimals === 0 ? "" : m));
}

// Groups by REAL event date + category -- never fabricates a day with
// no real events, never pads a sparse day to look busier than it is.
function buildEvidenceDaySummary(allEvents) {
  const byDate = new Map();
  for (const e of allEvents) {
    const d = dateOnly(e.time);
    if (!byDate.has(d)) byDate.set(d, { date: d, flowCount: 0, flowValue: 0, dpCount: 0, dpValue: 0 });
    const row = byDate.get(d);
    if (e.kind === "flow_alert") {
      row.flowCount += 1;
      if (e.value != null) row.flowValue += e.value;
    } else {
      row.dpCount += 1;
      if (e.value != null) row.dpValue += e.value;
    }
  }
  return Array.from(byDate.values()).sort((a, b) => (a.date < b.date ? 1 : -1)); // newest day first
}

function renderEvidenceDaySummaryTable(byDay) {
  if (!byDay.length) return `<div class="empty">No evidence recorded for this chart's period.</div>`;
  const rows = byDay.map((d) => `
    <tr>
      <td>${esc(d.date)}</td>
      <td>${d.flowCount ? `${d.flowCount}${d.flowValue ? ` (${esc(fmtMoney(d.flowValue))})` : ""}` : "\u2014"}</td>
      <td>${d.dpCount ? `${d.dpCount}${d.dpValue ? ` (${esc(fmtMoney(d.dpValue))})` : ""}` : "\u2014"}</td>
    </tr>`).join("");
  return `
    <table class="evidence-summary-table">
      <thead><tr><th>Date</th><th>Flow alerts (premium)</th><th>Dark-pool prints (premium)</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// renderEvidencePanel: the always-visible concise summary PLUS the
// collapsed-by-default detail panel shell (filter/sort controls + an
// empty table body -- wireEvidencePanel fills it with only ONE page's
// worth of rows at a time, never all events at once).
function renderEvidencePanel(allEvents, excludedCount) {
  const byDay = buildEvidenceDaySummary(allEvents);
  const excludedNote = excludedCount > 0
    ? `<div class="muted">${excludedCount} evidence marker(s) dated after this chart's most recent bar are not shown on the chart (no real candle exists yet for that date) -- still counted and listed below, never clamped or estimated.</div>`
    : "";
  if (!allEvents.length) {
    return `${excludedNote}<div class="empty">No evidence recorded for this chart's period.</div>`;
  }
  return `
    <div class="evidence-summary">
      <div class="muted">Evidence by day (${allEvents.length} real event${allEvents.length === 1 ? "" : "s"} total)</div>
      ${renderEvidenceDaySummaryTable(byDay)}
    </div>
    ${excludedNote}
    <details class="evidence-detail-panel">
      <summary class="muted">Individual observations (${allEvents.length}) \u2014 filter, sort, paginated</summary>
      <div class="evidence-controls">
        <select id="evFilterType">
          <option value="">All types</option>
          <option value="flow_alert">Flow alerts</option>
          <option value="darkpool_trade">Dark-pool trades</option>
        </select>
        <input type="date" id="evFilterDate" title="Filter to one date" />
        <input type="number" id="evFilterMinValue" placeholder="Min value ($)" title="Minimum premium/value" />
        <select id="evSort">
          <option value="time_desc">Newest first</option>
          <option value="time_asc">Oldest first</option>
          <option value="value_desc">Largest value first</option>
        </select>
      </div>
      <table class="chart-markers-table">
        <thead><tr><th>Time</th><th>Kind</th><th>Label</th><th>Price</th><th>Value</th></tr></thead>
        <tbody id="evTableBody"></tbody>
      </table>
      <div class="evidence-pagination">
        <button id="evPrevPage" class="btn-sm">\u2190 Prev</button>
        <span id="evPageInfo" class="muted"></span>
        <button id="evNextPage" class="btn-sm">Next \u2192</button>
      </div>
    </details>`;
}

// wireEvidencePanel: owns filter/sort/pagination state entirely
// client-side over the ALREADY-FETCHED allEvents array (no new
// requests -- every event was already delivered by the ohlc response).
// Re-renders only the current page's rows (<=EVIDENCE_PAGE_SIZE) into
// #evTableBody on every control change, so the DOM never holds more
// than one page of event rows regardless of how many real events exist.
function wireEvidencePanel(allEvents) {
  let page = 0;
  function currentFiltered() {
    const typeEl = document.getElementById("evFilterType");
    if (!typeEl) return [];
    const dateEl = document.getElementById("evFilterDate");
    const minValEl = document.getElementById("evFilterMinValue");
    const sortEl = document.getElementById("evSort");
    let filtered = allEvents.filter((e) => {
      if (typeEl.value && e.kind !== typeEl.value) return false;
      if (dateEl.value && dateOnly(e.time) !== dateEl.value) return false;
      if (minValEl.value && (e.value == null || e.value < Number(minValEl.value))) return false;
      return true;
    });
    const mode = sortEl.value;
    filtered.sort((a, b) => {
      if (mode === "time_asc") return a.time < b.time ? -1 : 1;
      if (mode === "value_desc") return (b.value || 0) - (a.value || 0);
      return a.time < b.time ? 1 : -1; // time_desc, default
    });
    return filtered;
  }
  function render() {
    const filtered = currentFiltered();
    const totalPages = Math.max(1, Math.ceil(filtered.length / EVIDENCE_PAGE_SIZE));
    page = Math.min(page, totalPages - 1);
    const pageItems = filtered.slice(page * EVIDENCE_PAGE_SIZE, (page + 1) * EVIDENCE_PAGE_SIZE);
    const tbody = document.getElementById("evTableBody");
    if (!tbody) return;
    tbody.innerHTML = pageItems.map((e) => `
      <tr>
        <td>${esc(e.time)}</td>
        <td>${esc(e.kind === "darkpool_trade" ? "Dark-pool trade" : "Flow alert")}</td>
        <td>${esc(e.label || "")}</td>
        <td>${e.price != null ? Number(e.price).toFixed(2) : "\u2014"}</td>
        <td>${e.value != null ? esc(fmtMoney(e.value)) : "\u2014"}</td>
      </tr>`).join("");
    const pageInfoEl = document.getElementById("evPageInfo");
    if (pageInfoEl) pageInfoEl.textContent = `Page ${page + 1} of ${totalPages} (${filtered.length} matching)`;
    const prevBtn = document.getElementById("evPrevPage");
    const nextBtn = document.getElementById("evNextPage");
    if (prevBtn) prevBtn.disabled = page === 0;
    if (nextBtn) nextBtn.disabled = page >= totalPages - 1;
  }
  ["evFilterType", "evFilterDate", "evFilterMinValue", "evSort"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", () => { page = 0; render(); });
  });
  const prevBtn = document.getElementById("evPrevPage");
  const nextBtn = document.getElementById("evNextPage");
  if (prevBtn) prevBtn.addEventListener("click", () => { page = Math.max(0, page - 1); render(); });
  if (nextBtn) nextBtn.addEventListener("click", () => { page += 1; render(); });
  render();
}

// --- C1 activity timeline (2026-10-15) --------------------------------
//
// DATA-COVERAGE FINDING (verified live before writing any of this,
// per Chad's explicit instruction): UW's flow-alerts/darkpool-trades
// endpoints, as this adapter calls them, return only the MOST RECENT N
// events with no date filtering -- empirically confirmed across NVDA,
// PLTR, TSLA, QQQ, SPY: ALL 600 markers (100 flow alerts + 500 dark-
// pool trades) land on the single most recent trading day, every
// single time. Real multi-day coverage exists on UW's side but would
// cost either one call PER CALENDAR DAY (darkpool's `date` param) or a
// paginated `older_than` cursor walk of uncertain depth (flow-alerts)
// -- exactly the "high-volume backfill" shape Chad said not to start
// without approval. So a histogram spanning the full 1M/1Y chart range
// would be ~97% empty bars and one real bar -- honest, but not useful,
// and risks looking like a bug rather than a real finding.
//
// Built instead: a single-SESSION intraday activity timeline (30-minute
// buckets, using the real event timestamps we already have) -- this is
// genuinely useful ("when today did activity cluster") without ever
// implying multi-day coverage we don't have. Clearly labeled as such.
const ACTIVITY_BUCKET_MINUTES = 30;

function buildActivityBuckets(allEvents, bucketMinutes = ACTIVITY_BUCKET_MINUTES) {
  const bucketMs = bucketMinutes * 60 * 1000;
  const byBucket = new Map();
  for (const e of allEvents) {
    const t = new Date(e.time).getTime();
    if (Number.isNaN(t)) continue;
    const bucketStart = Math.floor(t / bucketMs) * bucketMs;
    if (!byBucket.has(bucketStart)) {
      byBucket.set(bucketStart, { bucketStart, flowCount: 0, flowValue: 0, dpCount: 0, dpValue: 0 });
    }
    const row = byBucket.get(bucketStart);
    if (e.kind === "flow_alert") {
      row.flowCount += 1;
      if (e.value != null) row.flowValue += e.value;
    } else {
      row.dpCount += 1;
      if (e.value != null) row.dpValue += e.value;
    }
  }
  return Array.from(byBucket.values()).sort((a, b) => a.bucketStart - b.bucketStart);
}

// Real distinct dates present in the event set -- drives the honest
// single-session vs multi-session disclosure text, never assumed.
function distinctEventDates(allEvents) {
  return Array.from(new Set(allEvents.map((e) => dateOnly(e.time)))).sort();
}

function mountActivityTimeline(containerId, allEvents) {
  const root = document.getElementById(containerId);
  if (!root) return;
  if (!allEvents.length) {
    root.innerHTML = `<div class="empty">No flow-alert or dark-pool activity in the currently fetched evidence to chart.</div>`;
    return;
  }
  if (typeof LightweightCharts === "undefined") {
    root.innerHTML = `<div class="chart-unavailable">Chart library unavailable.</div>`;
    return;
  }
  const dates = distinctEventDates(allEvents);
  const coverageNote = dates.length === 1
    ? `Today's activity only (${esc(dates[0])}) \u2014 UW's flow-alert/dark-pool endpoints return the most recent events, not a multi-day history; see the Evidence table above for the full underlying record.`
    : `Activity across ${dates.length} real distinct date(s) actually present in the fetched evidence.`;

  root.innerHTML = `<div id="${containerId}-chart" class="activity-timeline-chart"></div><div class="muted" style="margin-top:4px;font-size:11px">${coverageNote}</div>`;
  const chartEl = document.getElementById(`${containerId}-chart`);
  const chart = LightweightCharts.createChart(chartEl, {
    height: 110,
    layout: { background: { color: "transparent" }, textColor: "#9aa4b2" },
    grid: { vertLines: { color: "rgba(255,255,255,0.04)" }, horzLines: { color: "rgba(255,255,255,0.04)" } },
    timeScale: { borderColor: "rgba(255,255,255,0.12)", timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: "rgba(255,255,255,0.12)" },
  });
  const buckets = buildActivityBuckets(allEvents);
  const flowSeries = chart.addHistogramSeries({ color: "rgba(245,158,11,0.75)", priceScaleId: "right" });
  flowSeries.setData(buckets.map((b) => ({ time: Math.floor(b.bucketStart / 1000), value: b.flowCount })));
  const dpSeries = chart.addHistogramSeries({ color: "rgba(96,165,250,0.6)", priceScaleId: "right" });
  dpSeries.setData(buckets.map((b) => ({ time: Math.floor(b.bucketStart / 1000), value: b.dpCount })));
  chart.timeScale().fitContent();
  root._tipActivityChart = chart;
  if (!root._tipResizeObserverAttached && typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (root._tipActivityChart && chartEl.clientWidth > 0) {
        root._tipActivityChart.applyOptions({ width: chartEl.clientWidth });
      }
    });
    ro.observe(root);
    root._tipResizeObserverAttached = true;
  }
}

// D1 (ux-nav-watchlist-reliability round 9): honest empty-cache state +
// an obvious "Load Chart Data" action, rather than a dead-end message.
// `opts.showLoadButton` is true only when the caller (app.live.js, a real
// backend) has a real authenticated POST /api/ticker/{t}/ohlc/refresh to
// wire the button to -- the static demo (zero backend) never sets it, so
// it still gets the plain honest message with no button that would 404.
// This function still makes zero fetch() calls itself (the click handler
// is wired by the caller after this HTML lands in the DOM), consistent
// with the rest of this file's "no fetch() here" rule.
function renderChartUnavailable(reason, opts = {}) {
  const detail = reason && reason.message ? reason.message
    : reason && reason.status ? `UW status: ${reason.status}`
    : "No OHLC data available for this ticker in the current mode.";
  const btn = opts.showLoadButton
    ? `<div style="margin-top:10px"><button id="loadChartDataBtn" type="button" class="btn-refresh">Load Chart Data</button>
        <div id="loadChartDataStatus" class="muted" style="margin-top:6px;font-size:11px"></div></div>`
    : "";
  return `<div class="chart-unavailable">No chart data cached yet for this ticker &mdash; ${esc(detail)}${btn}</div>`;
}

// --- Compact evidence marker overlay (2026-10-17 fix) ------------------
//
// Replaces lightweight-charts' native series.setMarkers() text labels
// (which overlapped unreadably near the chart's right edge -- see the
// root-cause comment inside mountCandlestickChart above) with a custom,
// absolutely-positioned HTML overlay: one small fixed-size colored dot
// per (bar, kind) group, a hover/click tooltip with the real aggregate +
// per-event detail, and a legend with Flow only / Dark Pool only / Both
// / None toggle buttons. Positions come from the SAME
// chart.timeScale().timeToCoordinate() the candlesticks themselves use,
// so dots stay correctly aligned through pan/zoom/resize/range changes
// -- re-rendered on chart.timeScale().subscribeVisibleTimeRangeChange
// and on the existing ResizeObserver callback (see call sites).
//
// Every number shown anywhere in the tooltip (count, summed premium,
// each listed timestamp/price) is read directly from the real
// marker/event objects the backend returned -- nothing here fabricates,
// interpolates, or repositions a real event's time to "look cleaner";
// grouping only changes what's drawn on the chart canvas, never the
// real timestamps a user can see in the tooltip or the (unchanged)
// evidence table below the chart.
//
// Degrades to a safe no-op under the repo's minimal getElementById-only
// test DOM stubs (no real appendChild/getBoundingClientRect/
// timeToCoordinate) -- same defensive-guard style this file already
// uses for `typeof ResizeObserver !== "undefined"`.
const EVIDENCE_MARKER_COLORS = { flow_alert: "#f59e0b", darkpool_trade: "#60a5fa" };
const EVIDENCE_MARKER_LABELS = { flow_alert: "Flow alert", darkpool_trade: "Dark-pool trade" };
const EVIDENCE_VISIBILITY_KEY = "tipEvidenceMarkerVisibility"; // "flow" | "dp" | "both" | "none"

function getEvidenceVisibilityMode() {
  try {
    const v = typeof localStorage !== "undefined" ? localStorage.getItem(EVIDENCE_VISIBILITY_KEY) : null;
    return v === "flow" || v === "dp" || v === "none" ? v : "both";
  } catch (e) { return "both"; }
}
function setEvidenceVisibilityMode(v) {
  try { if (typeof localStorage !== "undefined") localStorage.setItem(EVIDENCE_VISIBILITY_KEY, v); } catch (e) { /* ignore */ }
}
function evidenceModeShowsKind(mode, kind) {
  if (mode === "none") return false;
  if (mode === "flow") return kind === "flow_alert";
  if (mode === "dp") return kind === "darkpool_trade";
  return true; // "both" (default)
}

// Real per-group summary -- every field here traces directly back to
// g.events (the real backend marker objects); never invented.
function summarizeEvidenceGroup(g) {
  const n = g.events.length;
  const withPrice = g.events.filter((e) => e.price != null);
  const withValue = g.events.filter((e) => e.value != null);
  const sumValue = withValue.length ? withValue.reduce((s, e) => s + e.value, 0) : null;
  return {
    count: n,
    sumValue,
    latestPrice: withPrice.length ? Number(withPrice[withPrice.length - 1].price) : null,
    earliestTime: g.events[0].time,
    latestTime: g.events[n - 1].time,
  };
}

// Tooltip HTML for one group -- aggregate line first, then up to the 8
// most recent REAL events with their exact recorded time/price/value
// (never rounded/repositioned for display), then a pointer to the full
// list in the preserved evidence table for anything beyond 8.
function evidenceGroupTooltipHtml(g) {
  const s = summarizeEvidenceGroup(g);
  const label = EVIDENCE_MARKER_LABELS[g.kind] || g.kind;
  const rows = g.events.slice(-8).reverse().map((e) => `
    <div class="cet-row"><span>${esc(e.time)}</span>${e.price != null ? `<span>@ ${Number(e.price).toFixed(2)}</span>` : ""}${e.value != null ? `<span>${esc(fmtMoney(e.value))}</span>` : ""}</div>`).join("");
  const more = s.count > 8 ? `<div class="cet-row muted">+${s.count - 8} more \u2014 see table below</div>` : "";
  return `
    <div class="cet-head" style="color:${EVIDENCE_MARKER_COLORS[g.kind] || "#fff"}">${s.count} ${esc(label)}${s.count === 1 ? "" : "s"}</div>
    <div class="cet-sub">${esc(dateOnly(s.latestTime))}${s.sumValue != null ? ` \u00b7 total ${esc(fmtMoney(s.sumValue))}` : ""}${s.latestPrice != null ? ` \u00b7 latest px ${s.latestPrice.toFixed(2)}` : ""}</div>
    ${rows}${more}`;
}

// One shared tooltip element reused across every chart mount/dot (a
// single floating div is simpler and cheaper than one per dot). Created
// lazily, only when document.body is really appendChild-able.
let _evidenceTooltipEl = null;
function getEvidenceTooltipEl() {
  if (!_evidenceTooltipEl
      && typeof document !== "undefined"
      && typeof document.createElement === "function"
      && document.body && typeof document.body.appendChild === "function") {
    _evidenceTooltipEl = document.createElement("div");
    _evidenceTooltipEl.className = "chart-evidence-tooltip";
    _evidenceTooltipEl.style.display = "none";
    document.body.appendChild(_evidenceTooltipEl);
  }
  return _evidenceTooltipEl;
}
function showEvidenceTooltip(anchorEl, html) {
  const tip = getEvidenceTooltipEl();
  if (!tip || typeof anchorEl.getBoundingClientRect !== "function") return;
  tip.innerHTML = html;
  tip.style.display = "block";
  const rect = anchorEl.getBoundingClientRect();
  const tipRect = tip.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - tipRect.width / 2;
  const maxLeft = (typeof window !== "undefined" ? window.innerWidth : left + tipRect.width) - tipRect.width - 4;
  left = Math.max(4, Math.min(left, maxLeft));
  let top = rect.top - tipRect.height - 8;
  if (top < 4) top = rect.bottom + 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
function hideEvidenceTooltip() {
  if (_evidenceTooltipEl) _evidenceTooltipEl.style.display = "none";
}

// Mounts the legend/toggle row + absolutely-positioned dot overlay as
// extra children of `root` (appended AFTER the chart library has
// already populated it -- never replaces root.innerHTML again, so the
// chart's own DOM is left untouched). `groups` is the same Map built in
// mountCandlestickChart: key `${barTime}|${kind}` -> { barTime, kind,
// events }.
function mountEvidenceMarkerOverlay(root, chart, groups) {
  const timeScale = chart.timeScale();
  if (typeof root.appendChild !== "function" || typeof timeScale.timeToCoordinate !== "function") {
    return; // minimal test DOM stub / non-browser environment -- safe no-op
  }
  const groupList = Array.from(groups.values());
  if (!groupList.length) return; // nothing real to show -- no legend for an empty chart

  if (root.style && !root.style.position) root.style.position = "relative";

  let mode = getEvidenceVisibilityMode();

  const legend = document.createElement("div");
  legend.className = "chart-evidence-legend";
  legend.innerHTML = `
    <span class="cel-item"><span class="cel-dot cel-dot-flow"></span>Flow alerts</span>
    <span class="cel-item"><span class="cel-dot cel-dot-dp"></span>Dark-pool</span>
    <span class="cel-toggles">
      <button type="button" class="cel-btn" data-mode="flow">Flow only</button>
      <button type="button" class="cel-btn" data-mode="dp">Dark Pool only</button>
      <button type="button" class="cel-btn" data-mode="both">Both</button>
      <button type="button" class="cel-btn" data-mode="none">None</button>
    </span>`;
  root.appendChild(legend);

  const overlay = document.createElement("div");
  overlay.className = "chart-evidence-overlay";
  root.appendChild(overlay);

  function updateLegendActiveState() {
    if (typeof legend.querySelectorAll !== "function") return;
    legend.querySelectorAll(".cel-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  }

  function renderDots() {
    overlay.innerHTML = "";
    const h = root.clientHeight || 320;
    const flowLaneY = 12;
    const dpLaneY = Math.max(24, h - 30);
    for (const g of groupList) {
      if (!evidenceModeShowsKind(mode, g.kind)) continue;
      const x = timeScale.timeToCoordinate(g.barTime);
      if (x == null) continue; // genuinely outside the currently-visible range -- correctly hidden, not misplaced
      const dot = document.createElement("div");
      const n = g.events.length;
      const kindClass = g.kind === "darkpool_trade" ? "dp" : "flow";
      dot.className = `chart-evidence-dot chart-evidence-dot-${kindClass}`;
      dot.style.left = `${x}px`;
      dot.style.top = `${g.kind === "darkpool_trade" ? dpLaneY : flowLaneY}px`;
      if (n > 1) dot.textContent = n > 99 ? "99+" : String(n);
      dot.title = ""; // never rely on the native title tooltip -- our own is the real UI
      overlay.appendChild(dot);
      if (typeof dot.addEventListener === "function") {
        dot.addEventListener("mouseenter", () => showEvidenceTooltip(dot, evidenceGroupTooltipHtml(g)));
        dot.addEventListener("mouseleave", () => { if (!dot._pinned) hideEvidenceTooltip(); });
        dot.addEventListener("click", (ev) => {
          ev.stopPropagation();
          if (typeof overlay.querySelectorAll === "function") {
            overlay.querySelectorAll(".chart-evidence-dot.pinned").forEach((d) => {
              if (d !== dot) { d.classList.remove("pinned"); d._pinned = false; }
            });
          }
          dot._pinned = !dot._pinned;
          dot.classList.toggle("pinned", dot._pinned);
          if (dot._pinned) showEvidenceTooltip(dot, evidenceGroupTooltipHtml(g)); else hideEvidenceTooltip();
        });
      }
    }
  }

  if (typeof legend.addEventListener === "function") {
    legend.addEventListener("click", (ev) => {
      const btn = typeof ev.target.closest === "function" ? ev.target.closest(".cel-btn") : null;
      if (!btn) return;
      mode = btn.dataset.mode;
      setEvidenceVisibilityMode(mode);
      updateLegendActiveState();
      renderDots();
    });
  }

  updateLegendActiveState();
  renderDots();
  root._tipRenderEvidenceOverlay = renderDots;
  if (typeof timeScale.subscribeVisibleTimeRangeChange === "function") {
    timeScale.subscribeVisibleTimeRangeChange(renderDots);
  }
}

// Mounts (or re-renders, if called again) a candlestick chart with
// evidence markers into the DOM node with id `containerId`. `ohlc` is the
// exact shape returned by /api/ticker/{t}/ohlc (live) or
// data/ohlc/{t}.json (static demo) -- see backend/app/core/candles.py.
// `opts.showLoadButton`: see renderChartUnavailable above.
function mountCandlestickChart(containerId, ohlc, opts = {}) {
  const root = document.getElementById(containerId);
  if (!root) return;

  if (!ohlc || ohlc.unavailable || !ohlc.candles || !ohlc.candles.length) {
    root.innerHTML = renderChartUnavailable(ohlc ? ohlc.reason : null, opts);
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
    priceScaleId: "right",
  });

  // 2026-10-15 (item #3, genuine 1D intraday): daily bars' `t` is a
  // plain "YYYY-MM-DD" date (UW's own docs: 1d/1w candles have no
  // start/end time); intraday bars' `t` is a full ISO datetime
  // (start_time). lightweight-charts needs a UTCTimestamp (seconds,
  // number) for intraday so same-day bars don't collapse onto one
  // x-position -- a plain date string works fine (and is simpler) for
  // daily bars. Detected once from the real data, never mixed.
  const intraday = ohlc.candles.length > 0 && String(ohlc.candles[0].t).includes("T");
  function toChartTime(ts) {
    return intraday ? Math.floor(new Date(ts).getTime() / 1000) : dateOnly(ts);
  }
  const bars = ohlc.candles.map((c) => ({
    time: toChartTime(c.t), open: c.o, high: c.h, low: c.l, close: c.c,
  }));

  // Phase B (2026-10-14): volume, ONLY when at least one real non-zero
  // value is actually present -- an all-zero/missing volume series
  // would render as a flat line at the bottom implying real (zero)
  // data, which is worse than just not showing it. Squeezed into the
  // bottom 20% of the chart via its own price scale so it never
  // crowds out the candles, which stay the primary content.
  const hasTrustworthyVolume = ohlc.candles.some((c) => typeof c.v === "number" && c.v > 0);
  // 2026-10-15 (item #2): accurate volume label. Daily bars' `v` is now
  // computed as regular-session-only shares (see candles.py's
  // select_regular_session_bars -- UW's own 'r' row volume is
  // cumulative pre+regular, so the premarket contribution is
  // subtracted out to match the regular-session-only OHLC prices
  // shown). Intraday (1D) bars' `v` is UW's real per-5-minute-bucket
  // share volume, already session-exact (no subtraction needed). Never
  // asserts a precision UW's own docs don't define -- see this
  // function's module notes for the full empirical derivation.
  const volLabelEl = document.getElementById("chartVolumeLabel");
  if (volLabelEl) {
    volLabelEl.textContent = hasTrustworthyVolume
      ? (intraday
          ? "Volume (shares, per 5-minute bar \u2014 real UW share volume, not dark-pool or options activity)"
          : "Volume (shares, regular session \u2014 real UW share volume, not dark-pool or options activity)")
      : "";
  }
  if (hasTrustworthyVolume) {
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      color: "rgba(96,165,250,0.5)",
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volumeSeries.setData(ohlc.candles.map((c) => ({
      time: toChartTime(c.t), value: c.v || 0, color: c.c >= c.o ? "rgba(34,211,168,0.5)" : "rgba(239,68,68,0.5)",
    })));
  }
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
  // Fix (2026-10-17, real captured-video defect: SPY markers unreadable/
  // overlapping near the chart's right edge): GROUP markers by (bar,
  // kind) into ONE group per bar+kind -- this part of the 2026-10-14 fix
  // was and remains correct (verified: distinct trading days stay
  // distinct groups, never collapsed; see nearestBarTime/dateOnly tests).
  // The ACTUAL root cause of the right-edge overlap is downstream of
  // this grouping: the old code then handed each group to lightweight-
  // charts' native series.setMarkers() as a marker carrying a permanent
  // inline TEXT label (e.g. "23 flow alerts (latest $187.20)"). That
  // library renders marker text unconditionally and with zero collision
  // detection between adjacent markers. Real UW evidence is heaviest on
  // the most RECENT trading days (see this file's own C1 module note
  // above: UW's flow-alert/dark-pool endpoints return only the most
  // recent events), so the right-hand side of the chart is exactly where
  // multiple consecutive daily bars are most likely to EACH carry a
  // label -- and a label like that is 120-180px wide while a 1M/3M/6M
  // view's bar spacing is often under 20px, so neighboring days' labels
  // physically overlap. This is a label-width-vs-bar-spacing density
  // problem, not a timestamp/timezone/bucketing bug (those were already
  // fixed and are covered by nearestBarTime's own tests) and not a
  // same-day stacking problem (also already fixed by this grouping).
  // Confirmed by reading lightweight-charts' marker renderer: it does
  // not reflow or hide overlapping marker text, and a marker anchored on
  // one of the last few bars can render its text past the plot area,
  // under/behind the right price scale.
  //
  // Fix: STOP asking the chart library to draw any permanent per-marker
  // text. Native setMarkers() is no longer used at all. Instead,
  // mountEvidenceMarkerOverlay() below draws small fixed-size, color-
  // coded dots (compact, same color convention already used elsewhere
  // in this app: orange = options flow, blue = dark pool) positioned via
  // the SAME chart.timeScale().timeToCoordinate() the library itself
  // uses, so dots always track real bar positions through scroll/zoom/
  // resize/range changes. Each dot still represents exactly one (bar,
  // kind) group -- same honest aggregation as before, same real count,
  // same real underlying events -- but ALL detail (count, aggregate
  // premium, real per-event timestamps) now lives in a hover/click
  // tooltip and in the unchanged, already-existing evidence table below,
  // never permanently drawn on the chart itself. See
  // mountEvidenceMarkerOverlay's own header comment for the rest.
  let excludedCount = 0;
  const groups = new Map(); // key: `${barTime}|${kind}` -> { barTime, kind, events: [...] }
  for (const m of ohlc.markers || []) {
    const barTime = nearestBarTime(toChartTime(m.time), candleDates);
    if (!barTime) {
      excludedCount += 1;
      continue;
    }
    const key = `${barTime}|${m.kind}`;
    if (!groups.has(key)) groups.set(key, { barTime, kind: m.kind, events: [] });
    groups.get(key).events.push(m);
  }
  const allEvents = []; // flat list for the detail table, newest-first within each group preserved
  for (const g of groups.values()) allEvents.push(...g.events);
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
    info.innerHTML = renderEvidencePanel(allEvents, excludedCount);
    root.after(info);
    wireEvidencePanel(allEvents);
  }
  mountActivityTimeline("activityTimelineContainer", allEvents);

  // Compact marker overlay + legend/toggle controls (2026-10-17 fix) --
  // see the long comment above this function's grouping loop for the
  // full root-cause writeup. No-ops safely when running under a
  // minimal test DOM stub (no real appendChild/timeToCoordinate).
  mountEvidenceMarkerOverlay(root, chart, groups);

  root._tipChart = chart;
  if (!root._tipResizeObserverAttached && typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (root._tipChart && root.clientWidth > 0) {
        root._tipChart.applyOptions({ width: root.clientWidth });
        if (typeof root._tipRenderEvidenceOverlay === "function") root._tipRenderEvidenceOverlay();
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
// Round 12 (ux-nav-watchlist-reliability): the backend's determine_transition
// (signal_engine.py, unchanged this round) can emit TWO separate reason
// strings for a zero-qualifying-gate case: no_qualifying_evidence_yet (the
// very first evaluation of a ticker) and no_gate_passed_remains_new (the
// L1 any_gate_passed guard's case -- a LATER re-evaluation of an
// already-NEW ticker that again found zero gates). Both mean exactly the
// same thing to a reader -- "nothing is actually qualifying" -- and prior
// rounds (P0/D2/P3/P5) only ever checked the first string, so the second
// one fell through to the generic STATE_GLOSS["NEW"] ("just started
// qualifying") / the backend's raw headline in all three visible
// locations below. This is the SINGLE shared predicate every one of those
// locations (primaryTickerHeadline, primaryStateGloss, historyEntryGloss)
// now branches on, so the "is this a zero-gate case" check is defined
// exactly once and can never drift between call sites again.
function isZeroGateTransitionReason(reason) {
  return reason === "no_qualifying_evidence_yet" || reason === "no_gate_passed_remains_new";
}

function historyEntryGloss(e) {
  if (e && isZeroGateTransitionReason(e.transition_rule)) {
    return "no qualifying evidence yet";
  }
  return STATE_GLOSS[e && e.state] || "";
}

function renderRawHistoryStep(e, i, entries) {
  const gloss = historyEntryGloss(e);
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
    const gloss = historyEntryGloss(e);
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
// Round 14 item 2 follow-on: when this ticker has genuinely never been
// evaluated, BOTH previousState and state are honestly null/undefined
// (see backend/app/main.py's _not_evaluated_ticker_analysis_response
// fix) -- without this guard the first branch below would render the
// literal, nonsensical string "Initial detection: null". Never invents
// a state; just states plainly that there is none yet.
function transitionLabel(previousState, state) {
  if (!state) return "No evaluation recorded yet.";
  if (!previousState) return `Initial detection: ${state}`;
  if (previousState === state) return `Status: Still ${state}`;
  return `Transition: ${previousState} \u2192 ${state}`;
}

// D2 (ux-nav-watchlist-reliability round 9): the backend's honest
// no_qualifying_evidence_yet reason string (from signal_engine's
// determine_transition, unchanged this round) was reaching the
// TRANSITION_RULE_GLOSS map and being correctly glossed, but that gloss
// was only ever read inside the bottom "Developer / Debug" <details>
// panel (renderRawDataSummary) -- the PRIMARY visible headline in the
// header card still showed the backend's generic tip_evidence.headline()
// text (e.g. "Unusual activity") alongside the state-gloss tag's "(just
// started qualifying)", which together read as if qualification had
// happened. This computes the actual primary-visible headline, genuinely
// branching on d.transition_rule (not guessing from d.state), and is
// used in place of the raw backend headline for this one specific case
// only -- every other transition_rule keeps the backend's own headline
// untouched, no lifecycle-state/confidence/CONFIRMED logic changed.
function primaryTickerHeadline(d) {
  if (isZeroGateTransitionReason(d.transition_rule)) {
    return "No qualifying signal detected yet.";
  }
  return d.headline;
}

// P3 fix (ux-nav-watchlist-reliability round 10): a SECOND, separate
// display location for the same zero-gate dishonesty the headline fix
// (primaryTickerHeadline above) already fixed two rounds ago. This
// badge/pill sits right next to the state tag in the header card
// (renderTickerDetail below) and used to be `STATE_GLOSS[d.state]`
// unconditionally -- keyed ONLY off the lifecycle state, never the
// transition reason. signal_engine.py's determine_transition can
// return state=NEW for TWO genuinely different reasons (see its
// `current_state is None` branch): any_gate_passed=True ->
// transition_rule="first_qualifying_gate" (a real qualifying signal
// just fired), or any_gate_passed=False ->
// transition_rule="no_qualifying_evidence_yet" (zero gates passed --
// nothing is actually qualifying). STATE_GLOSS["NEW"] is "just started
// qualifying", which is only true for the first case; for the second,
// zero-gate case it directly contradicts the already-fixed headline
// right above it ("No qualifying signal detected yet.") on the exact
// same page. Mirrors primaryTickerHeadline's own branch on
// d.transition_rule (never on state alone) so the two visible strings
// can never disagree again -- no signal_engine.py/lifecycle-state/
// threshold change, pure frontend display-layer fix, same as the
// headline fix it extends.
function primaryStateGloss(d) {
  if (isZeroGateTransitionReason(d.transition_rule)) {
    return "no qualifying evidence yet";
  }
  return STATE_GLOSS[d.state] || "";
}

// Round 13 item 3: a THIRD, previously-unfound display location for the
// same zero-gate dishonesty primaryTickerHeadline/primaryStateGloss
// already fixed two/three rounds ago -- this one is the results/
// activity-summary line directly below the primary headline in the
// Ticker Analysis header card (renderTickerDetail below), which used to
// render the backend's raw tip_evidence.current_read() narrative
// unconditionally (e.g. "Unusual activity; stock little changed." for
// ARTNA/MSEX with zero qualifying gates -- a backend-generated but
// genuinely misleading sentence for this specific case, same class of
// bug as the headline fix, just a different generator function and a
// different call site). Reuses the EXISTING shared
// isZeroGateTransitionReason predicate (no new/fourth check) -- when
// true, substitutes one honest, neutral sentence; every other
// transition_rule keeps the backend's own current_read untouched, same
// pattern as primaryTickerHeadline/primaryStateGloss. Pure frontend
// text-generation fix -- no signal_engine.py/lifecycle-state change.
function primaryCurrentRead(d) {
  if (isZeroGateTransitionReason(d.transition_rule)) {
    return "No qualifying signal detected -- this scan found zero qualifying gates for this ticker.";
  }
  return d.current_read;
}

// Round 14 item 2: a never-evaluated ticker (no cataloged evaluation at
// all -- backend/app/main.py's _not_evaluated_ticker_analysis_response,
// now fixed to report `state: null`/`not_evaluated: true` instead of
// faking `state: "NEW"`) must not render the lifecycle state badge at
// all -- "NEW (just started qualifying)" directly contradicted this
// same header card's own "Not yet evaluated" freshness text for the
// exact same ticker. Distinct from the zero-gate case (primaryStateGloss
// above): that is a ticker genuinely evaluated this scan with zero
// qualifying gates, which keeps its real state tag plus the honest
// "(no qualifying evidence yet)" gloss -- never evaluated at all is a
// THIRD, different state that gets its own neutral badge, reusing the
// SAME isNotEvaluated predicate the Market Radar grid's stateTagHtml
// already uses, never a fourth ad-hoc check.
function primaryStateBadgeHtml(d) {
  if (isNotEvaluated(d)) {
    return `<span class="tag state-unevaluated" title="This ticker has never been evaluated -- no real signal-detection scan has run for it yet.">Not evaluated</span>`;
  }
  const stateGloss = primaryStateGloss(d);
  return `<span class="tag state-${esc(d.state)}">${esc(d.state)}</span>${stateGloss ? ` <span class="muted state-gloss">(${esc(stateGloss)})</span>` : ""}`;
}

function renderTickerDetail(d) {
  const gatesHtml = renderGateGroups(d.gates);
  const ruleGloss = TRANSITION_RULE_GLOSS[d.transition_rule] || "internal rule identifier";
  const primaryHeadline = primaryTickerHeadline(d);
  const timelineHtml = renderLifecycleTimeline(d.history, d.state);
  // W2-b (2026-10-16): honest cache-vs-fresh disclosure, same freshness-
  // badge pattern already established for My Watchlist -- reuses
  // frontend/timefmt.js, never a raw ISO string. `served_from_cache`/
  // `eval_freshness`/`last_evaluated_at` only exist on a real
  // LiveUWAdapter response; Demo Mode responses omit them (always
  // fresh by construction), so this block degrades to nothing there.
  const hasFreshnessInfo = d.eval_freshness !== undefined && d.eval_freshness !== null;
  const freshnessHtml = hasFreshnessInfo
    ? `<div class="tags">
        <span class="tag freshness-${esc(d.eval_freshness)}">${esc(FRESHNESS_LABELS[d.eval_freshness] || d.eval_freshness)}</span>
        <span class="muted">${d.last_evaluated_at && typeof formatTimestampET === "function" ? `Last evaluated: ${esc(formatTimestampET(d.last_evaluated_at))}` : "Not yet evaluated"}</span>
        <button id="tickerRefreshBtn" type="button" class="btn-refresh" title="Re-runs signal detection (the gates/state above) for this ticker only. Does NOT refresh the price chart -- use the chart panel's own Load Chart Data / range buttons for that.">Refresh this ticker (signal only)</button>
      </div>`
    : "";
  return `
    <div class="card ticker-header-card">
      <div class="card-head"><span class="tk tk-lg">${esc(d.ticker)}</span>
        ${primaryStateBadgeHtml(d)}
      </div>
      ${freshnessHtml}
      <div class="tags">
        <span class="tag">confidence: ${esc(d.confidence)}</span>
        <span class="tag">${esc(d.signal_type)}</span>
        <span class="tag">qualifying days: ${d.qualifying_days_count}</span>
      </div>
      <div class="read primary-headline" id="tickerPrimaryHeadline">${esc(primaryHeadline)}</div>
      <div class="read">${esc(primaryCurrentRead(d))}</div>
      <div class="read">${esc(transitionLabel(d.previous_state, d.state))}</div>
      <div class="read">Confidence reason: ${esc(d.confidence_reason)}</div>
    </div>
    <div class="card chart-card chart-card-prominent">
      <div class="card-head"><strong>Price Chart &mdash; ${esc(d.ticker)}</strong>
        <span class="muted">evidence markers from the same flow_alerts/darkpool_trades the gates use</span></div>
      <!-- Phase B (2026-10-14): timeframe row wired by app.live.js's
           initChartRangeRow() (production-inspired timeframeRow/
           wireTimeframeRow pattern) -- each click is a REAL re-fetch
           (/api/ticker/{t}/ohlc?range=X), not a zoom of fixed data. -->
      <div id="chartRangeRow"></div>
      <div id="ohlcChartContainer" class="chart-container"><div class="empty">Loading chart...</div></div>
      <div id="chartVolumeLabel" class="muted" style="margin-top:4px;font-size:11px"></div>
      <div id="chartRangeNote" class="muted" style="margin-top:6px;font-size:11px"></div>
      <!-- C1 activity timeline (2026-10-15): flow-alert (orange) vs
           dark-pool (blue) counts by time-of-day -- see mountActivityTimeline's
           module comment for why this is single-session, not multi-day. -->
      <div class="activity-timeline-head muted" style="margin-top:10px;font-size:11px">
        Activity timeline <span style="color:#f59e0b">\u25a0 flow alerts</span> &middot; <span style="color:#60a5fa">\u25a0 dark-pool</span>
      </div>
      <div id="activityTimelineContainer"></div>
    </div>
    <div class="card evidence-primary-card">
      <div class="card-head"><strong>Evidence &mdash; what the chart above is based on</strong></div>
      <div class="gate-groups-summary">${renderGateGroupsSummary(d.gates)}</div>
      <!-- Phase B (2026-10-14): detailed per-gate breakdown moved behind
           a <details> (production's "more"/"how" pattern) so the chart
           above reads as the primary content -- nothing removed, the
           summary line above still always shows every category count. -->
      <details class="gate-details">
        <summary class="muted">Detailed gate-by-gate diagnostics</summary>
        <p class="muted gate-group-intro">A gate that did not pass is not automatically "conflicting evidence." Grouped below by what it actually represents, each with a plain-English note on what it observed. Confidence below describes how clearly gates fired this scan &mdash; it is never a prediction of trade outcome.</p>
        ${gatesHtml}
      </details>
    </div>
    <div id="investigateSection"></div>
    ${timelineHtml}
    ${renderRawDataSummary(d)}`;
}

// --- Readable evidence summary (2026-10-15, item #2) ------------------------
//
// Replaces this section's old PROMINENT behavior of dumping d.raw (the
// exact flow_alerts/darkpool_trades arrays the gates above consumed) as
// thousands of lines of unfiltered JSON. Computes real, concise numbers
// straight from those same two arrays -- never a new data source, never
// an invented value -- using the SAME evidence-summary-table / collapsed-
// <details> markup pattern already built for the paginated evidence
// panel above (renderEvidencePanel/wireEvidencePanel), so this is an
// extension of that existing pattern, not a parallel UI. The full raw
// JSON is kept, fully intact, just demoted into a clearly-labeled,
// collapsed-by-default "Developer / Debug" disclosure at the very bottom.
function _fa(a, key) { return Number(a[key] || 0); }

// Bug fix (#36, 2026-10-15 round 2): real UW flow-alert rows carry a
// `type` string field ("call"/"put"), NOT `is_call`/`is_put` booleans --
// those booleans only exist on this app's own demo/synthetic fixtures
// (and on anything already normalized by live_uw_adapter._normalize_flow_alert).
// Prefer the explicit booleans when present (keeps demo fixtures exactly
// as before), otherwise fall back to a case-insensitive, whitespace-
// tolerant read of `type` so this still works even if raw/unnormalized
// data ever reaches this renderer directly. Missing/malformed/unexpected
// values (no type field, "", a number, "Call ", a typo) safely resolve to
// neither side rather than crashing or guessing.
function _flowSide(a) {
  if (a.is_call === true) return "call";
  if (a.is_put === true) return "put";
  const t = String(a.type == null ? "" : a.type).trim().toLowerCase();
  if (t === "call") return "call";
  if (t === "put") return "put";
  return null;
}

function summarizeFlowAlerts(flowAlerts) {
  const alerts = flowAlerts || [];
  let callCount = 0, putCount = 0, callPrem = 0, putPrem = 0;
  let askPrem = 0, bidPrem = 0, sweepCount = 0, multilegCount = 0, totalPrem = 0;
  for (const a of alerts) {
    const prem = _fa(a, "total_premium");
    totalPrem += prem;
    const side = _flowSide(a);
    if (side === "call") { callCount += 1; callPrem += prem; }
    else if (side === "put") { putCount += 1; putPrem += prem; }
    askPrem += _fa(a, "total_ask_premium");
    bidPrem += _fa(a, "total_bid_premium");
    if (a.has_sweep) sweepCount += 1;
    if (a.has_multileg) multilegCount += 1;
  }
  const notable = alerts
    .slice()
    .sort((a, b) => _fa(b, "total_premium") - _fa(a, "total_premium"))
    .slice(0, 5);
  return { count: alerts.length, callCount, putCount, callPrem, putPrem, askPrem, bidPrem,
    sweepCount, multilegCount, totalPrem, notable };
}

function summarizeDarkpool(darkpoolTrades) {
  const trades = darkpoolTrades || [];
  let totalShares = 0, totalValue = 0;
  for (const t of trades) {
    totalShares += _fa(t, "size");
    totalValue += _fa(t, "premium");
  }
  const notable = trades.slice().sort((a, b) => _fa(b, "premium") - _fa(a, "premium")).slice(0, 5);
  return { count: trades.length, totalShares, totalValue, notable };
}

function renderRawDataSummary(d) {
  const raw = d.raw || {};
  const flowAlerts = raw.flow_alerts || [];
  const darkpoolTrades = raw.darkpool_trades || [];
  const of = summarizeFlowAlerts(flowAlerts);
  const dp = summarizeDarkpool(darkpoolTrades);
  const ruleGloss = TRANSITION_RULE_GLOSS[d.transition_rule] || "internal rule identifier";

  const askShare = (of.askPrem + of.bidPrem) > 0 ? of.askPrem / (of.askPrem + of.bidPrem) : null;
  const optionsRows = of.count === 0
    ? `<tr><td colspan="2" class="muted">No options flow alerts returned this scan.</td></tr>`
    : `
      <tr><td>Call vs put alerts</td><td>${of.callCount} call (${fmtMoney(of.callPrem) ?? "$0"}) &middot; ${of.putCount} put (${fmtMoney(of.putPrem) ?? "$0"})</td></tr>
      <tr><td>Total options premium</td><td>${fmtMoney(of.totalPrem) ?? "$0"}</td></tr>
      <tr><td>Ask-side vs bid-side premium</td><td>${fmtMoney(of.askPrem) ?? "$0"} ask &middot; ${fmtMoney(of.bidPrem) ?? "$0"} bid${askShare != null ? ` (${(askShare * 100).toFixed(0)}% ask)` : ""}</td></tr>
      <tr><td>Repeated hits / unusual flow</td><td>${of.sweepCount} sweep-flagged, ${of.multilegCount} multileg-flagged (of ${of.count} alert${of.count === 1 ? "" : "s"})</td></tr>`;

  const dpRows = dp.count === 0
    ? `<tr><td colspan="2" class="muted">No dark-pool trades returned this scan.</td></tr>`
    : `
      <tr><td>Dark-pool trade count</td><td>${dp.count}</td></tr>
      <tr><td>Dark-pool shares</td><td>${dp.totalShares.toLocaleString()}</td></tr>
      <tr><td>Dark-pool value</td><td>${fmtMoney(dp.totalValue) ?? "$0"}</td></tr>`;

  const notableRows = [
    ...of.notable.map((a) => `<tr><td>Option alert</td><td>${esc(_flowSide(a) || "n/a")}</td><td>${fmtMoney(_fa(a, "total_premium")) ?? "n/a"}</td><td>${esc(a.created_at || "n/a")}</td></tr>`),
    ...dp.notable.map((t) => `<tr><td>Dark-pool print</td><td>n/a</td><td>${fmtMoney(_fa(t, "premium")) ?? "n/a"}</td><td>${esc(t.executed_at || "n/a")}</td></tr>`),
  ].join("") || `<tr><td colspan="4" class="muted">No individual records this scan.</td></tr>`;

  const coverageNote = `This reflects a single most-recent snapshot from each UW endpoint this scan `
    + `(${of.count} flow alert${of.count === 1 ? "" : "s"}, ${dp.count} dark-pool trade${dp.count === 1 ? "" : "s"}) `
    + `-- not a multi-day historical pull. See "Signal Lifecycle" above for what real history this ticker has.`;

  return `
    <div class="card evidence-raw-summary-card">
      <div class="card-head"><strong>Evidence Detail &mdash; options flow &amp; dark pool</strong>
        <span class="muted">computed directly from this scan's adapter data, not invented</span></div>
      <table class="evidence-summary-table">
        <tbody>
          ${optionsRows}
          ${dpRows}
        </tbody>
      </table>
      <div class="muted" style="margin-top:8px">Evidence for vs against the current signal state: ${renderGateGroupsSummary(d.gates)}</div>
      <div class="muted" style="margin-top:6px;font-size:11px">${esc(coverageNote)}</div>
      <details class="evidence-detail-panel" style="margin-top:8px">
        <summary class="muted">Notable contracts / prints (top ${of.notable.length + dp.notable.length} by premium)</summary>
        <table class="evidence-summary-table">
          <thead><tr><th>Type</th><th>Side</th><th>Premium</th><th>Time</th></tr></thead>
          <tbody>${notableRows}</tbody>
        </table>
      </details>
      <details class="how" style="margin-top:14px">
        <summary>Developer / Debug &mdash; raw adapter JSON (what fed the gates above)</summary>
        <p class="muted">Transition rule id: <code>${esc(d.transition_rule)}</code> &mdash; ${esc(ruleGloss)}</p>
        <pre class="raw">${esc(JSON.stringify(d.raw, null, 2))}</pre>
      </details>
    </div>`;
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
// success/failure detail (tool_outcomes) was added 2026-10-15 (item 5) to
// backend/app/ai/schemas.py::InvestigationResult -- render it when present;
// fall back to the older honest-gap note only for an older/simulated
// response shape that doesn't carry tool_outcomes at all (e.g. a prior
// cached response from before this field existed -- see item 7).
function renderToolsUsed(toolsUsed, toolOutcomes) {
  const tools = toolsUsed || [];
  if (!tools.length) {
    return `<div class="tools-used"><span class="tools-used-label">Tools queried:</span> <span class="muted">none</span></div>`;
  }
  const outcomeByTool = {};
  (toolOutcomes || []).forEach((o) => { outcomeByTool[o.tool] = o; });
  const hasOutcomes = (toolOutcomes || []).length > 0;
  const steps = tools.map((t, i) => {
    const o = outcomeByTool[t];
    const statusHtml = o
      ? (o.ok
          ? `<span class="tool-step-status tool-step-ok">OK</span>`
          : `<span class="tool-step-status tool-step-fail">FAILED${o.error ? `: ${esc(o.error)}` : ""}</span>`)
      : "";
    return `<li class="tool-step"><span class="tool-step-n">${i + 1}</span>${esc(t)} ${statusHtml}</li>`;
  }).join("");
  const gapNote = hasOutcomes
    ? ""
    : `<p class="muted tools-used-gap">Per-tool success/failure detail is not available for this response
        (older response shape, before backend/app/ai/schemas.py::InvestigationResult.tool_outcomes existed) --
        that absence is surfaced here honestly, not hidden.</p>`;
  return `
    <div class="tools-used">
      <span class="tools-used-label">Tools queried (${tools.length}):</span>
      <ol class="tool-steps">${steps}</ol>
      ${gapNote}
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

// Evidence-limitation/truncation disclosure (item 4/5): server_truncated
// means the LLM's own response exceeded the stated size limits and had to
// be capped server-side (schemas.py) -- distinct from the sampling
// disclosure already embedded per-tool in the raw prompt (not surfaced in
// the response at all today, since it only affects what the model SAW, not
// what it said). Rendered only when true -- never shown as a false alarm.
function renderTruncationDisclosure(d) {
  if (!d.server_truncated) return "";
  return `<div class="ai-disabled-note" style="margin-top:8px">
    <strong>Note:</strong> the AI's response exceeded the stated size limits and was capped server-side
    (some facts/evidence entries and/or long strings were shortened or dropped) -- this is disclosed, not hidden.
  </div>`;
}

// Evidence-integrity warnings (items #1/#2, 2026-10-15): a non-empty list
// means validate_investigation_result() (backend/app/ai/schemas.py) could
// not corroborate a specific claim in the text against the actual tool
// data this investigation retrieved -- either a uniform buying/selling
// claim against genuinely mixed ask/bid-side flow, or a "several"/"heavy"-
// style quantified claim backed by too few observed_facts. Rendered as its
// own prominent, visually distinct warning block -- never folded silently
// into Limitations, since these are warnings ABOUT the response's own
// honesty, not a property of the underlying market data.
function renderEvidenceIntegrityWarnings(warnings) {
  if (!warnings || !warnings.length) return "";
  const items = warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  return `
    <div class="ai-section ai-integrity-warning">
      <h4>Evidence Integrity Warnings</h4>
      <p class="muted">The AI's own wording made a claim this app's validator could not corroborate against the
        actual tool data retrieved this run. Read the flagged claim(s) above with that caveat in mind.</p>
      <ul class="ai-list">${items}</ul>
    </div>`;
}

function renderInvestigateResult(d, ticker, modeLabel) {
  const simBanner = d.simulated
    ? `<div class="ai-simulated-banner ai-simulated-banner-strong">
         <span class="ai-simulated-badge">SIMULATED</span>
         ${esc(d.simulated_label || "Simulated agent walkthrough -- not live UW data, not a real AI call")}
       </div>`
    : `<div class="ai-live-banner">LIVE: this investigation made real calls using your configured AI provider and UW key.
         Bounded, read-only tool selection and synthesis. A real end-to-end AI-directed investigation over genuine
         MCP tool calls has been independently verified (a real NVDA run, mcp transport) -- see README for the
         verification writeup.
         <br><strong>Transport: ${esc(transportLabel(d.transport))}</strong></div>`;
  const headerHtml = ticker ? renderInvestigateHeader(ticker, modeLabel || "") : "";
  return `
    ${headerHtml}
    ${simBanner}
    ${renderTruncationDisclosure(d)}
    ${renderInvestigateProcessSteps()}
    ${renderToolsUsed(d.tools_used, d.tool_outcomes)}
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
    ${renderEvidenceIntegrityWarnings(d.evidence_integrity_warnings)}
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
  // Current STATE (what mode you're in right now) is deliberately a
  // separate, non-interactive line from the ACTION buttons below it --
  // the two used to be conflated (the active button's styling was the
  // only "current mode" signal), which reads ambiguously at a glance.
  const modeSection = `
    <div class="settings-card">
      <h3>Mode</h3>
      <p class="muted" style="margin:0 0 10px">You are currently in
        <strong class="${d.mode === "live" ? "settings-current-live" : "settings-current-demo"}">${d.mode === "live" ? "LIVE" : "DEMO"} mode</strong>.
      </p>
      <div class="settings-row">
        <button class="btn ${d.mode === "demo" ? "btn-active" : ""}" id="btnModeDemo" ${publicDemo ? "disabled" : ""}>Switch to Demo</button>
        <button class="btn ${d.mode === "live" ? "btn-active" : ""}" id="btnModeLive" ${liveDisabled ? "disabled" : ""} title="${esc(liveTitle)}">Switch to Live Mode</button>
      </div>
      ${!publicDemo && !uw.verified ? `<p class="muted">Switch to Live Mode is disabled until Test Connection succeeds for the current key.</p>` : ""}
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
          configured AI provider when live. A real end-to-end AI-directed investigation over genuine
          MCP tool calls has been independently verified (a real NVDA run) -- see the GitHub README's
          verification writeup.</p>
      </div>`
    : `<div class="settings-card">
        <h3>AI / MCP (optional)</h3>
        <p class="muted">Status: ${aiStatusLine}</p>
        <p class="muted">Bounded, read-only investigation: tool-selection and synthesis via your configured AI
          provider when live. A real end-to-end AI-directed investigation over genuine MCP tool calls has been
          independently verified (a real NVDA run, mcp transport) -- see README for the verification writeup.
          This confirms the <code>mcp</code> transport specifically; the <code>rest</code> fallback transport
          (see "Effective transport" below) uses plain REST calls and has not had a separate end-to-end
          verification pass.</p>
        <!-- Priority 5 fix: "an AI key is configured" and "which transport actually
             executes tool calls" are two separate facts -- shown as its own distinct
             status line here, not folded into aiStatusLine above. Sourced from
             ai.investigate_transport (settings.investigate_transport, an env-only
             config switch unrelated to any credential). -->
        <p class="muted">Effective transport: <strong>${esc(transportLabel(ai.investigate_transport))}</strong>
          <span class="muted">(configured via <code>INVESTIGATE_TRANSPORT</code> -- independent of whether an
          AI provider key is configured above)</span></p>
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

  root.innerHTML = `${banner}${modeSection}${uwSection}${aiSection}${renderUpdatesSection(d)}`;
  return { publicDemo };
}

// --- Market Radar watchlist editor (moved to Radar 2026-10-15) --------------
//
// MOVED (not rebuilt) from the Settings page into Market Radar's "My
// Watchlist" mode, per Chad's live-mode feedback: this is the exact same
// markup/behavior that used to live in renderSettingsView() above --
// same /api/settings/watchlist GET/PUT endpoints, same 20-symbol limit
// (watchlist.max_size, server-enforced), same initWatchlistEditor() wiring
// in app.live.js, same chip ids (#watchlistChips/#watchlistAddInput/
// #watchlistAddBtn/#watchlistStatus). Settings no longer renders a second
// copy of this editor -- there is exactly one watchlist editor in the
// app, and it lives where the watchlist is actually used (Market Radar).
// Not shown in the public demo build (its 11 synthetic scenarios are
// fixed, not a user-editable watchlist concept at all).
// Priority 2 fix (2026-10-15 round 3): this editor used to be a full,
// always-expanded .settings-card sitting directly above Featured Signal
// -- on a real 1920x1080/1080p viewport with a non-trivial ticker count
// it routinely pushed Featured Signal below the fold. Now a <details>
// element: COLLAPSED by default (no `open` attribute), with a compact
// one-line <summary> (count + a short preview of the actual tickers)
// that expands to the exact same full editor markup/ids/behavior below
// -- add/remove/Scan Now and the 20-symbol limit are unchanged, nothing
// removed, just not forced open by default.
//
// Also (Priority 2, Demo-vs-Live distinction): in the local edition's
// own Demo Mode (d.mode === "demo", NOT the separate public_demo static
// build), this editor is still shown and still saves real settings --
// but Market Radar's synthetic adapter scans its OWN fixed 11-scenario
// universe (adapter.get_universe(), see backend main.py radar()), never
// this watchlist. Demo Mode previously showed this editor with no
// indication that it does nothing to what Demo's Market Radar actually
// displays, which reads as if "my watchlist" and "the tickers shown in
// Market Radar right now" were the same thing. They are not -- this is
// the SAVED configuration that becomes active watchlist scanning only
// once you switch to Live Mode in Settings.
function renderWatchlistEditor(d, opts = {}) {
  const publicDemo = !!d.public_demo;
  if (publicDemo) return "";
  const watchlist = d.watchlist || { tickers: [], max_size: 20 };
  const count = watchlist.tickers.length;
  const preview = count ? watchlist.tickers.slice(0, 4).join(", ") + (count > 4 ? `, +${count - 4} more` : "") : "no tickers saved";
  const isDemo = d.mode === "demo";
  const demoNote = isDemo
    ? `<p class="muted watchlist-demo-note">You're in <strong>Demo Mode</strong>: Market Radar above is showing TIP's fixed
        synthetic ticker universe (11 built-in demo scenarios), <strong>not</strong> this watchlist. This editor saves
        your Live-Mode watchlist configuration now so it's ready the moment you switch to Live Mode in Settings --
        it has no effect on what Demo Mode's Market Radar displays.</p>`
    : "";
  // W1 FIX (real bug: add a ticker, immediately click its new chip's x
  // remove button -- nothing happens; a refresh "fixes" it): this
  // <details> was rebuilt from scratch via wlRoot.innerHTML on EVERY
  // add/remove save (see loadSettings() in app.live.js), which, with no
  // `open` attribute in this template, silently collapsed the editor
  // back closed every single time regardless of whether the user had
  // it expanded -- so a click on the just-rendered remove button that
  // landed a moment after that re-render hit a now-hidden/closed
  // <details>, not the control the user saw. The fix is for the CALLER
  // to read the current <details>'s real open/closed state BEFORE
  // replacing the DOM and pass it back in here as `opts.open`, so the
  // editor's expand/collapse state survives every save, exactly like a
  // real production-grade editor (never collapses itself out from under
  // an in-progress interaction). See initWatchlistEditor()/app.live.js's
  // render call site for where `opts.open` is captured and passed.
  const openAttr = opts.open ? " open" : "";
  return `
    <details class="settings-card watchlist-editor-compact" id="watchlistEditorCard"${openAttr}>
      <summary class="watchlist-editor-summary">
        <strong>My Watchlist</strong>
        <span class="muted">${count}/${watchlist.max_size} tickers${count ? " \u2014 " + esc(preview) : " (" + preview + ")"}${isDemo ? " \u00b7 saved for Live Mode" : ""}</span>
        <span class="btn-link watchlist-editor-toggle">edit</span>
      </summary>
      <div class="watchlist-editor-body">
        ${demoNote}
        <p class="muted">Each ticker in this watchlist costs ~3 real UW API calls per Market Radar scan
          (flow alerts, dark-pool trades, stock state) -- limit ${watchlist.max_size} symbols.
          This is watchlist-based scanning only; switch to the "Market Discovery" mode above for
          market-wide screener-based discovery instead.</p>
        <div class="watchlist-chips" id="watchlistChips">
          ${watchlist.tickers.map((t) => `
            <span class="watchlist-chip" data-ticker="${esc(t)}">
              ${esc(t)}<button class="watchlist-chip-remove" data-remove-ticker="${esc(t)}" title="Remove ${esc(t)}">&times;</button>
            </span>`).join("")}
          ${watchlist.tickers.length === 0 ? '<span class="muted">No tickers configured -- Live Mode\u2019s Market Radar would have nothing to scan.</span>' : ""}
        </div>
        <div class="settings-row" style="margin-top:8px">
          <input type="text" id="watchlistAddInput" placeholder="Add ticker (e.g. AMD)" autocomplete="off" maxlength="7" />
          <button class="btn" id="watchlistAddBtn" ${watchlist.tickers.length >= watchlist.max_size ? "disabled" : ""}>Add</button>
        </div>
        <div id="watchlistStatus" class="muted" style="margin-top:6px"></div>
      </div>
    </details>`;
}

// --- Application Updates (Settings) -----------------------------------------
//
// Check-and-inform ONLY: no automatic git pull / docker rebuild / restart
// is ever triggered from here (see README.md "Checking for updates" and
// backend/app/update_check.py's module docstring). `build` is the
// GET /api/settings/version payload (always fetched -- no network call,
// just reads a baked-in file). `checkResult` is the GET
// /api/settings/update-check payload, only present after the user has
// explicitly clicked "Check for Updates" (never auto-fetched, so this app
// never makes an unprompted outbound call to GitHub).
const UPDATE_STATUS_LABELS = {
  up_to_date: { label: "Up to date", cls: "state-STABLE" },
  update_available: { label: "Update available", cls: "state-BUILDING" },
  unable_to_check: { label: "Unable to check", cls: "state-FADING" },
  unknown_version: { label: "Unknown installed version", cls: "" },
};

function buildAgentUpdatePrompt(build) {
  const repoUrl = (build && build.repo_url) || "https://github.com/Ch0sen84/tip-hackathon-edition";
  return `You are updating an existing local installation of TIP Hackathon Edition
(${repoUrl}) to the latest commit on \`main\`. This is a MANUAL,
agent-assisted update -- never trigger any part of this automatically
from the running web app itself.

Ground rules:
- Preserve .env exactly as-is; do not overwrite or regenerate it.
- Preserve the "tip-data" Docker volume (SQLite settings/watchlist DB) --
  never run "docker compose down -v" as part of an update.
- Never print, log, or transmit any saved UW/AI API key.
- Confirm the current container is healthy BEFORE making changes:
  curl -s http://localhost:8000/api/health

Steps:
1. cd into the existing checkout (do not re-clone over it).
2. git fetch origin && git log HEAD..origin/main --oneline
   (show the user exactly what will change before doing anything).
3. Try a normal update first: git pull origin main.
   - If that succeeds cleanly (no conflicts, fast-forward or clean
     merge), skip straight to step 4.
   - If it FAILS with "refusing to merge unrelated histories",
     "Updating ... is not possible" (diverged), or any non-fast-forward/
     conflict error, this checkout's history has diverged from the
     remote (this can happen after a repository history rewrite) --
     do NOT force-push, force-merge, or discard anything yet. Instead:
     a. git fetch origin --prune
     b. git status --short  and  git log --oneline -5
        (show the user whether they have ANY real local-only commits
        or uncommitted edits worth keeping -- check this honestly,
        do not assume there are none).
     c. If there is genuinely nothing local-only to keep, STOP and show
        the user this exact plan before running anything further:
          "This checkout's git history has diverged from the remote
           (likely a repository history rewrite). The safe fix is:
             git fetch origin
             git reset --hard origin/main
           This discards any local-only commits/edits in THIS checkout
           (the .env file and the 'tip-data' Docker volume are NOT
           touched by this -- they live outside git). Proceed? (yes/no)"
        Only run "git fetch origin && git reset --hard origin/main"
        after the user explicitly confirms "yes" in this exact
        conversation -- never run it automatically, never assume
        consent from the update-check having been triggered.
     d. If there IS real local-only work worth keeping, STOP and tell
        the user to resolve/back it up manually (e.g. git branch
        backup-before-reset) before any reset is considered -- do not
        propose discarding it silently.
4. docker compose up --build -d (rebuilds the image; the existing
   "tip-data" named volume and .env are untouched by this -- true for
   both the clean-pull and the confirmed-reset path above).
5. curl -s http://localhost:8000/api/health again to confirm the
   updated container is healthy and settings/watchlist persisted
   (should look identical to before the update).

Report back exactly what changed (the commit range from step 2, and
whether a reset was needed and confirmed) and the before/after health
check output.`;
}

function renderUpdatesSection(d) {
  const build = d.build || {};
  const hasVersion = !!(build.commit || build.version);
  const installedLabel = build.version || build.commit || "unknown";
  const repoUrl = build.repo_url || "https://github.com/Ch0sen84/tip-hackathon-edition";
  return `
    <div class="settings-card" id="updatesCard">
      <h3>Application Updates</h3>
      <p class="muted">Installed: ${hasVersion ? `<code>${esc(installedLabel)}</code>${build.commit ? ` (commit <code>${esc(build.commit)}</code>)` : ""}${build.built_at ? `, built ${esc(build.built_at)}` : ""}` : `<em>unknown</em> -- this image was not built with BUILD_COMMIT baked in (see README "Checking for updates")`}</p>
      <div class="settings-row">
        <button class="btn" id="checkUpdatesBtn">Check for Updates</button>
        <a class="btn-link" href="${esc(repoUrl)}" target="_blank" rel="noopener">View repository</a>
      </div>
      <div id="updateCheckResult" class="muted" style="margin-top:8px"></div>
      <p class="muted" style="margin-top:10px">This only CHECKS and reports -- it never runs git/Docker commands or
        restarts anything by itself. Updating is a manual or AI-agent-assisted step you run yourself.</p>
      <div class="settings-row" style="margin-top:6px">
        <button class="btn" id="copyUpdatePromptBtn">Copy AI-agent update prompt</button>
      </div>
      <div id="copyUpdatePromptStatus" class="muted" style="margin-top:4px"></div>
    </div>`;
}

function renderUpdateCheckResult(result) {
  if (!result) return "";
  const meta = UPDATE_STATUS_LABELS[result.status] || { label: result.status, cls: "" };
  const commitLine = result.latest_commit
    ? `Latest on main: <a href="${esc(result.latest_commit_url || "#")}" target="_blank" rel="noopener"><code>${esc(result.latest_commit)}</code></a>`
    : "";
  const compareLine = result.compare_url
    ? `<a href="${esc(result.compare_url)}" target="_blank" rel="noopener">View on GitHub</a>`
    : "";
  return `
    <div>
      <span class="tag ${meta.cls}">${esc(meta.label)}</span>
      <span>${esc(result.message || "")}</span>
    </div>
    ${commitLine ? `<div style="margin-top:4px">${commitLine}</div>` : ""}
    ${compareLine ? `<div style="margin-top:4px">${compareLine}</div>` : ""}`;
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
