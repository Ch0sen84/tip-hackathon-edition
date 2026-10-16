// UX/Nav round (2026-10-16) frontend regression tests: nav label rename
// + marketwide URL persistence, timestamp formatting (incl. a DST-
// boundary case), the market-wide discovered/evaluated/awaiting
// summary (scoped to the active snapshot only), and the Run Once UI
// (success copy, disabled-while-running, top-N display).
//
//   node frontend/test_ux_nav_round.mjs [backend_base_url]

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

function loadReal(paths, names) {
  const src = paths.map((p) => fs.readFileSync(p, "utf8")).join("\n;\n");
  const body = `${src}\nreturn {${names.join(",")}};`;
  // eslint-disable-next-line no-new-func
  return new Function(body)();
}

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); failed++; }
}

const lib = loadReal(
  [
    fileURLToPath(new URL("./timefmt.js", import.meta.url)),
    fileURLToPath(new URL("./app.core.js", import.meta.url)),
  ],
  ["formatTimestampET", "formatRelative", "formatTimestampFull",
   "renderSchedulerControls", "renderMarketWideSummary", "renderMarketWideCandidateRow"],
);

console.log("\n=== Nav label rename (item 3) -- index.html markup ===");
const indexHtml = fs.readFileSync(fileURLToPath(new URL("./index.html", import.meta.url)), "utf8");
check("'My Watchlist' is the nav label for data-view=radar", () => {
  assert.match(indexHtml, /data-view="radar"[\s\S]*?<\/span>My Watchlist<\/button>/);
});
check("'Market Intelligence' is the nav label for data-view=marketwide", () => {
  assert.match(indexHtml, /data-view="marketwide"[\s\S]*?<\/span>Market Intelligence<\/button>/);
});
check("route ids (data-view values) are UNCHANGED -- no deep link/bookmark breaks", () => {
  assert.match(indexHtml, /data-view="radar"/);
  assert.match(indexHtml, /data-view="marketwide"/);
  assert.match(indexHtml, /data-view="ticker"/);
});

console.log("\n=== URL-backed nav fix: 'marketwide' now a supported NAV_VIEWS route (item 7) ===");
const appCoreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");
check("NAV_VIEWS includes marketwide", () => {
  assert.match(appCoreSrc, /NAV_VIEWS = \[[^\]]*"marketwide"[^\]]*\]/);
});
check("applyNavState has a dedicated marketwide restore branch calling handlers.onMarketWideRestore", () => {
  assert.match(appCoreSrc, /state\.view === "marketwide"/);
  assert.match(appCoreSrc, /onMarketWideRestore/);
});

console.log("\n=== timefmt.js: absolute ET formatting ===");
check("formats a real ISO-with-microseconds timestamp as 'Mon D, H:MM AM/PM ET'", () => {
  const out = lib.formatTimestampET("2026-10-16T14:53:37.980307+00:00");
  assert.match(out, /^Oct 16, \d{1,2}:\d{2} (AM|PM) ET$/);
});
check("null/undefined/empty -> 'Not evaluated', never a crash or 'Invalid Date'", () => {
  assert.equal(lib.formatTimestampET(null), "Not evaluated");
  assert.equal(lib.formatTimestampET(undefined), "Not evaluated");
  assert.equal(lib.formatTimestampET(""), "Not evaluated");
});
check("malformed input -> 'Not evaluated', never throws", () => {
  assert.equal(lib.formatTimestampET("not-a-timestamp-at-all"), "Not evaluated");
});
check("DST BOUNDARY: a UTC timestamp just after the 2026 US fall-back (Nov 1, 2026 06:00 UTC) renders in EST (UTC-5), not EDT", () => {
  // 2026-11-01 is the US fall-back Sunday; 06:00 UTC that morning is
  // 01:00 EST (just after the fallback, Intl handles this correctly
  // without any manual offset table in this codebase).
  const out = lib.formatTimestampET("2026-11-01T06:30:00+00:00");
  assert.match(out, /^Nov 1, 1:30 AM ET$/);
});
check("DST BOUNDARY: a UTC timestamp just before the 2026 US spring-forward (Mar 8, 2026 06:30 UTC) renders in EST (UTC-5)", () => {
  const out = lib.formatTimestampET("2026-03-08T06:30:00+00:00");
  assert.match(out, /^Mar 8, 1:30 AM ET$/);
});
check("DST BOUNDARY: a UTC timestamp just after the 2026 US spring-forward (Mar 8, 2026 07:30 UTC) renders in EDT (UTC-4)", () => {
  const out = lib.formatTimestampET("2026-03-08T07:30:00+00:00");
  assert.match(out, /^Mar 8, 3:30 AM ET$/);
});

console.log("\n=== timefmt.js: relative recency, no nonsensical output ===");
check("<60s ago -> 'Just updated'", () => {
  const now = new Date("2026-10-16T12:00:30Z").getTime();
  assert.equal(lib.formatRelative("2026-10-16T12:00:00Z", now), "Just updated");
});
check("10h35m ago -> '10h 35m ago'", () => {
  const now = new Date("2026-10-16T22:35:00Z").getTime();
  assert.equal(lib.formatRelative("2026-10-16T12:00:00Z", now), "10h 35m ago");
});
check("a timestamp YEARS old -> a sane 'Nd ago'/'Ny ago' form, never an absurd minute count", () => {
  const now = new Date("2026-10-16T12:00:00Z").getTime();
  const out = lib.formatRelative("2020-01-01T00:00:00Z", now);
  assert.match(out, /^\d+y ago$/);
  assert.ok(!/\d{5,}m ago/.test(out));
});
check("a timestamp slightly in the future (small clock skew) never renders a negative duration", () => {
  const now = new Date("2026-10-16T12:00:00Z").getTime();
  const out = lib.formatRelative("2026-10-16T12:00:02Z", now); // 2s future, within grace
  assert.ok(!out.includes("-"));
});
check("a timestamp meaningfully in the future is reported honestly, not as a negative/nonsensical 'ago'", () => {
  const now = new Date("2026-10-16T12:00:00Z").getTime();
  const out = lib.formatRelative("2026-10-16T13:00:00Z", now); // 1h future
  assert.ok(!/-\d+/.test(out), `must never contain a negative number: ${out}`);
  assert.ok(!/ago/.test(out), `a future timestamp must never be labeled "ago": ${out}`);
});
check("null -> 'Not evaluated'", () => {
  assert.equal(lib.formatRelative(null), "Not evaluated");
});

console.log("\n=== Market-wide summary: scoped to the ACTIVE snapshot only (item 4) ===");
check("discovered/evaluated/awaiting counts computed from the one active-snapshot response, scope visible as 'this scan'", () => {
  const data = {
    candidate_count_in_snapshot: 12,
    snapshot_at: "2026-10-16T10:00:00+00:00",
    candidates: [
      { ticker: "A", freshness: "current" },
      { ticker: "B", freshness: "stale" },
      { ticker: "C", freshness: "unevaluated" },
    ],
  };
  const html = lib.renderMarketWideSummary(data, { mode: "dry_run" });
  assert.match(html, />12<\/strong> discovered/);
  assert.match(html, />2<\/strong> evaluated/);
  assert.match(html, />1<\/strong> awaiting evaluation/);
  assert.match(html, /this scan/);
});
check("Disabled scheduler -> honest disclosure, never implies an ongoing scan", () => {
  const html = lib.renderMarketWideSummary({ candidates: [], candidate_count_in_snapshot: 0, snapshot_at: null }, { mode: "disabled" });
  assert.match(html, /currently Disabled/);
  assert.ok(!/keeps discovering/.test(html));
});

console.log("\n=== Scheduler Run Once UI (item 5) ===");
check("each mode's label is understandable without reading docs (no raw backend identifier shown as the only explanation)", () => {
  const html = lib.renderSchedulerControls({ mode: "dry_run", top_n: 5, freshness_window_s: 1200 });
  assert.match(html, /automatically discovers candidates across the broader market on a schedule/);
});
check("Run Once button shows the REAL configured top-N for Enabled mode, never a guess", () => {
  const html = lib.renderSchedulerControls({ mode: "enabled", top_n: 7, freshness_window_s: 1200 });
  assert.match(html, /top 7 ranked selections/);
  assert.match(html, /Top-N configured: 7/);
});
check("Run Once is disabled while a cycle is already in progress (surfaces the backend lease, never lets a duplicate click fire)", () => {
  const html = lib.renderSchedulerControls({ mode: "enabled", top_n: 5, freshness_window_s: 1200, cycle_in_progress: true });
  assert.match(html, /id="schedulerRunOnceBtn" disabled/);
  assert.match(html, /Run in progress/);
});
check("Run Once is disabled (with an honest reason) when the scheduler is Disabled", () => {
  const html = lib.renderSchedulerControls({ mode: "disabled", top_n: 5, freshness_window_s: 1200 });
  assert.match(html, /id="schedulerRunOnceBtn" disabled/);
  assert.match(html, /has no action to perform/);
});
check("Run Once is enabled and ready in dry_run/enabled modes with no cycle in progress", () => {
  const html = lib.renderSchedulerControls({ mode: "dry_run", top_n: 5, freshness_window_s: 1200, cycle_in_progress: false });
  assert.ok(!/id="schedulerRunOnceBtn" disabled/.test(html));
  assert.match(html, /Run Once now/);
});
check("copy explicitly distinguishes this scheduler (broader market) from the separate watchlist Refresh Data action", () => {
  const html = lib.renderSchedulerControls({ mode: "enabled", top_n: 5, freshness_window_s: 1200 });
  assert.match(html, /broader market discovery/);
  assert.match(html, /Refresh Data/);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
