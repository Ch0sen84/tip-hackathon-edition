// Regression tests for the Settings "Application Updates" section
// (2026-10-15) -- version display, the four update-check states, the
// copy-prompt button, and the moved-to-Radar watchlist editor. Also
// covers the fmtMoney/fmtNum formatting fixes from Chad's live-mode bug
// report (negative-sign placement, display-only rounding).
//
//   node frontend/test_updates_panel.mjs

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
  [fileURLToPath(new URL("./app.core.js", import.meta.url))],
  ["renderUpdatesSection", "renderUpdateCheckResult", "buildAgentUpdatePrompt",
   "renderWatchlistEditor", "fmtMoney", "fmtNum", "esc"],
);

console.log("\n=== fmtMoney: negative-sign placement (item #11) ===");
check("negative amount renders as -$X, never $-X", () => {
  assert.equal(lib.fmtMoney(-78_700_000), "-$78.7M");
  assert.ok(!lib.fmtMoney(-78_700_000).includes("$-"));
});
check("positive amount unaffected", () => {
  assert.equal(lib.fmtMoney(78_700_000), "$78.7M");
});
check("small negative amount", () => {
  assert.equal(lib.fmtMoney(-500), "-$500");
});
check("null stays null", () => {
  assert.equal(lib.fmtMoney(null), null);
});

console.log("\n=== fmtNum: display-only rounding (item #12) ===");
check("rounds to 2 decimals by default", () => {
  assert.equal(lib.fmtNum(20.6659), "20.67");
});
check("rounds to 0 decimals when asked", () => {
  assert.equal(lib.fmtNum(12345.6, 0), "12346");
});
check("null/undefined -> n/a, never a crash", () => {
  assert.equal(lib.fmtNum(null), "n/a");
  assert.equal(lib.fmtNum(undefined), "n/a");
});

console.log("\n=== renderUpdatesSection: installed version display ===");
check("shows installed commit/version when baked in", () => {
  const html = lib.renderUpdatesSection({ build: { commit: "abc1234", version: "v1.2.3", built_at: "2026-10-15", repo_url: "https://github.com/Ch0sen84/tip-hackathon-edition" } });
  assert.ok(html.includes("abc1234"));
  assert.ok(html.includes("v1.2.3"));
  assert.ok(html.includes("checkUpdatesBtn"));
  assert.ok(html.includes("copyUpdatePromptBtn"));
});
check("honestly shows unknown when nothing was baked in", () => {
  const html = lib.renderUpdatesSection({ build: { commit: null, version: null, built_at: null } });
  assert.ok(html.toLowerCase().includes("unknown"));
});
check("never auto-triggers anything -- no fetch call embedded, only buttons", () => {
  const html = lib.renderUpdatesSection({ build: {} });
  assert.ok(!html.includes("fetch("));
});

console.log("\n=== renderUpdateCheckResult: four distinct states ===");
check("up_to_date renders distinctly", () => {
  const html = lib.renderUpdateCheckResult({ status: "up_to_date", message: "Installed build matches the latest commit on main." });
  assert.ok(html.includes("Up to date"));
});
check("update_available renders distinctly, with a commit link", () => {
  const html = lib.renderUpdateCheckResult({
    status: "update_available", message: "1 new commit(s) available on main.",
    latest_commit: "deadbeef", latest_commit_url: "https://github.com/Ch0sen84/tip-hackathon-edition/commit/deadbeef",
  });
  assert.ok(html.includes("Update available"));
  assert.ok(html.includes("deadbeef"));
});
check("unable_to_check renders distinctly (network failure)", () => {
  const html = lib.renderUpdateCheckResult({ status: "unable_to_check", message: "Could not reach GitHub: timeout" });
  assert.ok(html.includes("Unable to check"));
});
check("unknown_version renders distinctly, never claims up to date or available", () => {
  const html = lib.renderUpdateCheckResult({ status: "unknown_version", message: "No resolvable installed commit was baked into this build." });
  assert.ok(html.includes("Unknown installed version"));
  assert.ok(!html.includes("Up to date"));
  assert.ok(!html.includes("Update available"));
});
check("all four states are visually distinct from each other", () => {
  const statuses = ["up_to_date", "update_available", "unable_to_check", "unknown_version"];
  const labels = statuses.map((s) => lib.renderUpdateCheckResult({ status: s, message: "x" }));
  assert.equal(new Set(labels).size, 4);
});

console.log("\n=== buildAgentUpdatePrompt: preserves data, never auto-triggers ===");
check("prompt explicitly preserves .env/volumes/data/credentials", () => {
  const prompt = lib.buildAgentUpdatePrompt({ repo_url: "https://github.com/Ch0sen84/tip-hackathon-edition" });
  assert.ok(prompt.includes(".env"));
  assert.ok(prompt.toLowerCase().includes("tip-data"));
  assert.ok(prompt.toLowerCase().includes("never print, log, or transmit any saved"));
  assert.ok(prompt.includes("MANUAL"));
});
check("prompt never includes a literal API key or secret-shaped string", () => {
  const prompt = lib.buildAgentUpdatePrompt({});
  assert.ok(!/sk-[a-zA-Z0-9]{10,}/.test(prompt));
});

console.log("\n=== renderWatchlistEditor: moved from Settings, same controls ===");
check("renders the chip editor with real tickers and the 20-symbol cap", () => {
  const html = lib.renderWatchlistEditor({ public_demo: false, watchlist: { tickers: ["NVDA", "TSLA"], max_size: 20 } });
  assert.ok(html.includes("NVDA"));
  assert.ok(html.includes("TSLA"));
  assert.ok(html.includes("watchlistAddBtn"));
  assert.ok(html.includes("watchlistChips"));
});
check("public demo build renders nothing (no editable watchlist concept there)", () => {
  const html = lib.renderWatchlistEditor({ public_demo: true, watchlist: { tickers: [], max_size: 20 } });
  assert.equal(html, "");
});
check("Add button disabled once at the max_size cap", () => {
  const tickers = Array.from({ length: 20 }, (_, i) => `T${i}`);
  const html = lib.renderWatchlistEditor({ public_demo: false, watchlist: { tickers, max_size: 20 } });
  assert.ok(html.includes('id="watchlistAddBtn" disabled'));
});

console.log(`\n${passed} check(s) passed.${failed ? ` ${failed} FAILED.` : ""}`);
process.exit(failed ? 1 : 0);
