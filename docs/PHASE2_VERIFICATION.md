# Phase 2 frontend redesign — verification record

Date: 2026-10-12

## What changed

- `frontend/app.js` and `static-demo/app.js` (two hand-maintained copies)
  replaced by:
  - `frontend/app.core.js` — all rendering/view logic, no `fetch()`.
  - `frontend/app.live.js` — real backend adapter (fetch/post/delete,
    button handlers, bootstrap).
  - `static-demo/app.static.js` — pre-baked-JSON adapter, no mutating calls.
  - `tools/build_static_demo.sh` — copies `app.core.js` + `styles.css`
    from `frontend/` into `static-demo/` verbatim (not a bundler).
- Market Radar: activity-summary strip, Featured Signal panel (real
  `highest[0]`), contextual Signal Breakdown sidebar, per-card/row
  "Investigate" affordance, single "price data unavailable in this
  scenario" line instead of the old "n/a n/a" double fallback, `.main`
  widened via `clamp()`.
- Shared "current ticker" state (`#ticker=TICKER` hash + module var) so
  clicking Investigate on Radar lands on Ticker Analysis pre-loaded and
  pre-selected — verified end to end (see below).
- Real 4-way gate classification (`classifyGate` in `app.core.js`):
  SUPPORTING / CONFLICTING / UNMET_CRITERIA / DATA_LIMITATION, replacing
  the old flat PASS/no list, plus a Limitations note per gate sourced
  from `signal_engine.py`'s own docstrings/metrics (synthetic percentile
  model, bootstrap/short-history caveats, catalyst/data-quality meaning).
- Investigate-with-AI: real `tools_used` rendered as a numbered step
  sequence; explicit note that per-tool success/failure detail is not
  part of `InvestigationResult` (a real schema gap, not fabricated);
  `simulated`/`simulated_label` banner strengthened visually; new copy
  states the LLM-directed workflow is "not yet verified end-to-end with
  a real provider" per README's documented status — no claim of full
  verification was added anywhere.
- Backend: **unchanged**. Everything above was derivable client-side
  from fields the API already returns.

## Verification performed (real, not described)

- `cd backend && python -m pytest -q` → **108 passed, 1 warning** (before
  and after frontend changes — backend untouched).
- `static-demo/check_static_demo.sh` → **ALL CHECKS PASSED** (before and
  after).
- N5 test deployment (`ssh root@10.10.10.2`, `/mnt/user/appdata/tip-hackathon-test`,
  `docker compose -p tip-hackathon-test`): synced new `frontend/`, rebuilt
  the image, restarted the container on port 8842. Confirmed via real
  HTTP calls: `/api/health`, `/api/radar`, `/api/ticker/FLUX`,
  `POST /api/investigate/FLUX`, `/api/settings`, and that `/`,
  `/app.core.js`, `/app.live.js` serve 200.
- Browser-driven (real headless Chromium via the Hermes browser tool,
  not described-only) verification against both the static demo
  (`python -m http.server` locally) and the live N5 deployment
  (`http://10.10.10.2:8842/`):
  - Market Radar renders Featured Signal / activity strip / Signal
    Breakdown / Other Noteworthy table with the new markup, no console
    errors observed in rendered output.
  - Clicking an "Investigate" affordance on a table row (ZYNC) navigated
    to Ticker Analysis with the `<select>` pre-set to ZYNC, data already
    loaded, and `location.hash` set to `#ticker=ZYNC` — confirmed by
    reading the live DOM, not assumed.
  - Ticker Analysis evidence classification confirmed per ticker (gate
    group counts differ per scenario, all 5 synthetic tickers render
    without error):
    - FLUX: Supporting(5) / Unmet criteria(1) / Data limitation(2)
    - QRVX: Supporting(3) / Unmet criteria(2) / Data limitation(3)
    - ZYNC, NADA, DRFT: Supporting(2) / Unmet criteria(3) / Data limitation(3)
  - Investigate-with-AI: simulated badge present, 4-step tools-used
    sequence rendered, the "not part of the schema" gap note rendered.
  - Settings view rendered correctly in both live (N5, env-sourced UW
    key, `configured: true`) and public-demo (static) shapes.
  - Responsive check at 1920 / 1440 / 1024 / 390px — screenshots below.

## Screenshots (`.verification_screenshots/`, real headless-Chromium captures)

1. `01_market_radar_1440.png` — static demo, Market Radar, 1440px
2. `02_ticker_analysis_gates_1440.png` — static demo, Ticker Analysis evidence groups, FLUX
3. `03_investigate_ai_1440.png` — static demo, Investigate with AI, simulated banner + tools-used steps
4. `04_market_radar_1920.png` — static demo, Market Radar, 1920px
5. `05_market_radar_1024.png` — static demo, Market Radar, 1024px
6. `06_market_radar_390_mobile.png` — static demo, Market Radar, 390px mobile viewport
7. `07_n5_docker_live_radar.png` — **live N5 Docker deployment** (10.10.10.2:8842), Market Radar, demo mode

## Known scope/design tradeoffs to flag before hackathon submission

- **CONFLICTING gate classification is implemented but not exercised by
  any of the 5 synthetic fixtures.** `price_confirmation`'s
  `metrics.confirmed === false` (price moved against the expected
  direction) never occurs in `fixtures/scenario_*.json` as currently
  written, so the CONFLICTING group's styling/copy has not been visually
  verified against real rendered data — only read-through-tested against
  the classification function's logic. Recommend adding one fixture
  scenario where `direction_heuristic` is set but the realized price move
  contradicts it, to exercise this path before a reviewer asks for it.
- The Featured Signal / Signal Breakdown pattern surfaces only
  `highest[0]`; when `highest` has 0 entries (Market Radar with nothing
  qualifying for Highest Attention) the Featured Signal shows an explicit
  "no featured signal" empty state rather than silently rendering nothing
  — intentional, but worth a reviewer's eye on a day with zero qualifying
  tickers.
- No new backend fields were needed or added; static-demo JSON captures
  were therefore **not regenerated** (section 6 of the spec only applies
  if the API shape changed, and it did not).
- No disposable nginx:alpine container was needed for static-demo testing
  since the local `python3 -m http.server` + the Hermes browser tool's
  shared network namespace reached it directly; nothing was deployed or
  left running on the N5 box as a result of this check (only the
  pre-existing `tip-hackathon-test` container was touched, as instructed).
