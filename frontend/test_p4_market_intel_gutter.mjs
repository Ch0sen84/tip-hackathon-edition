// P4 regression test (ux-nav-watchlist-reliability round 10): Market
// Intelligence desktop gutter fix. Real-browser CDP measurement (not
// reproducible in this Node-only harness, which has no real layout
// engine -- see the task's final report for the actual before/after
// pixel numbers captured via a live Chrome CDP session at 1920x1080 and
// 2560x1440) is the authoritative verification for this priority; this
// file is a fast, repeatable SOURCE-level regression guard so a future
// change can't silently delete or narrow the fix without a test
// failing, and confirms the fix is properly SCOPED (Market Intelligence
// only, no other view touched, no row/card markup change).
//
//   node frontend/test_p4_market_intel_gutter.mjs

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const css = fs.readFileSync(fileURLToPath(new URL("./styles.css", import.meta.url)), "utf8");
const coreSrc = fs.readFileSync(fileURLToPath(new URL("./app.core.js", import.meta.url)), "utf8");

let passed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); process.exitCode = 1; }
}

check("a .main override scoped to #view-marketwide.active exists (the real gutter fix)", () => {
  const m = css.match(/\.main:has\(#view-marketwide\.active\)\s*\{([^}]*)\}/);
  assert.ok(m, "expected a .main:has(#view-marketwide.active) rule in styles.css");
  const decl = m[1];
  const maxWidthMatch = decl.match(/max-width:\s*min\((\d+)px/);
  assert.ok(maxWidthMatch, `expected a max-width: min(Npx, ...) declaration, got: ${decl}`);
  const widenedTo = Number(maxWidthMatch[1]);
  assert.ok(widenedTo > 1880, `widened cap (${widenedTo}px) must genuinely exceed the old 1880px cap this round found insufficient`);
});

check("the base .main rule (every OTHER view) keeps its existing 1880px cap completely unchanged", () => {
  const m = css.match(/\.main\s*\{([^}]*)\}/);
  assert.ok(m, "expected the base .main rule to still exist");
  assert.ok(/max-width:\s*min\(1880px/.test(m[1]), `base .main rule's cap must stay exactly 1880px, got: ${m[1]}`);
});

check("the widened cap is scoped ONLY to Market Intelligence -- no other #view-* id is widened the same way", () => {
  const hasRules = [...css.matchAll(/\.main:has\(([^)]*)\)/g)].map((m) => m[1]);
  assert.deepEqual(hasRules, ["#view-marketwide.active"], `expected exactly one scoped override, found: ${JSON.stringify(hasRules)}`);
});

check("Market Intelligence rows keep their existing left-alignment rules unchanged (no card/row markup change)", () => {
  assert.ok(/\.market-wide-row-main\s*\{[^}]*justify-content:\s*flex-start/.test(css));
  assert.ok(/\.market-wide-row-main\s*\{[^}]*text-align:\s*left/.test(css));
  assert.ok(/\.market-wide-row-main\s*>\s*\*\s*\{[^}]*text-align:\s*left/.test(css));
});

check("renderMarketWideCandidateRow's markup (classes/structure) is untouched by this round's layout fix", () => {
  assert.ok(coreSrc.includes('class="candidate-row market-wide-row"'));
  assert.ok(coreSrc.includes('class="market-wide-row-main"'));
});

console.log(`\n${passed} check(s) passed.`);
