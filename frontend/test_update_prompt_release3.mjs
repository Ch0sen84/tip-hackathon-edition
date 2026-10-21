// Release 3 (v7 review finding 10): the copied "upgrade guidance" from Settings -> Application Updates must
// never contain a direct upgrade procedure (git pull/fetch/reset, docker compose up --build, restarts) and
// must point only to docs/RELEASE3_RECOVERY_RUNBOOK.md, requiring a verified U4 and approvals A1-A4,
// without suggesting that copying it authorizes anything.
//
//   node frontend/test_update_prompt_release3.mjs
//   APP_CORE=<path to another app.core.js> node frontend/test_update_prompt_release3.mjs   (reproduction)

import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const targets = process.env.APP_CORE
  ? [process.env.APP_CORE]
  : [fileURLToPath(new URL("./app.core.js", import.meta.url)),
     fileURLToPath(new URL("../static-demo/app.core.js", import.meta.url))];

let passed = 0, failed = 0;
function check(name, fn) {
  try { fn(); console.log(`PASS: ${name}`); passed++; }
  catch (e) { console.log(`FAIL: ${name} -- ${e.message}`); failed++; }
}

const UNSAFE = [
  [/\bgit\s+(pull|fetch|reset|checkout|clone|merge|rebase|switch)\b/i, "a git command that changes the checkout"],
  [/\bdocker\b/i, "a docker command"],
  [/\bcompose\b/i, "a compose command"],
  [/--build\b/, "--build"],
  [/\bup\s+-d\b/, "up -d"],
  [/\bdown\s+-v\b/, "down -v"],
  [/reset\s+--hard/i, "reset --hard"],
  [/\bprune\b/i, "prune"],
  [/\brestart\s+the\s+(app|container)/i, "a restart instruction"],
];

for (const path of targets) {
  const src = fs.readFileSync(path, "utf8");
  // eslint-disable-next-line no-new-func
  const lib = new Function(`${src}\nreturn {buildAgentUpdatePrompt, renderUpdatesSection};`)();
  const label = path.replace(/\\/g, "/").split("/").slice(-2).join("/");
  const builds = [{}, { repo_url: "https://github.com/Ch0sen84/tip-hackathon-edition", commit: "147e3154b001cd6e73dde16e612978864ff6f500" }];
  console.log(`\n=== ${label} ===`);
  for (const b of builds) {
    const prompt = lib.buildAgentUpdatePrompt(b);
    for (const [rx, what] of UNSAFE) {
      check(`[${label}] copied guidance contains no ${what}`, () => assert.ok(!rx.test(prompt), `found: ${prompt.match(rx)}`));
    }
  }
  const prompt = lib.buildAgentUpdatePrompt({ commit: "147e3154b001cd6e73dde16e612978864ff6f500" });
  check(`[${label}] points to the authoritative runbook`, () => assert.ok(prompt.includes("docs/RELEASE3_RECOVERY_RUNBOOK.md")));
  check(`[${label}] requires a verified, test-restored U4`, () => assert.ok(/verified, test-restored U4/.test(prompt) && /No verified U4 means no upgrade/.test(prompt)));
  check(`[${label}] requires approvals A1-A4`, () => ["A1", "A2", "A3", "A4"].forEach((g) => assert.ok(prompt.includes(g), g)));
  check(`[${label}] states that copying it is not authorization`, () => assert.ok(/INFORMATION, NOT AUTHORIZATION/.test(prompt) && /does not\s+approve/.test(prompt)));
  check(`[${label}] tells an agent to stop and ask the release owner`, () => assert.ok(/STOP\. Ask the release owner/.test(prompt)));
  check(`[${label}] still protects .env, the tip-data volume and saved secrets`, () => {
    assert.ok(prompt.includes(".env") && prompt.includes("tip-data"));
    assert.ok(prompt.toLowerCase().includes("never print, log, or transmit any saved"));
  });
  check(`[${label}] never embeds a secret-shaped string`, () => assert.ok(!/sk-[a-zA-Z0-9]{10,}/.test(prompt)));
  const card = lib.renderUpdatesSection({ build: { commit: "147e315" } });
  check(`[${label}] Updates card points to the runbook and does not offer an "update prompt"`, () => {
    assert.ok(card.includes("docs/RELEASE3_RECOVERY_RUNBOOK.md"));
    assert.ok(card.includes("not authorization"));
    assert.ok(!/AI-agent update prompt/.test(card));
    assert.ok(card.includes('id="copyUpdatePromptBtn"'));   // same control, same wiring in app.live.js
  });
}

console.log(`\n${passed} check(s) passed.${failed ? ` ${failed} FAILED.` : ""}`);
process.exit(failed ? 1 : 0);
