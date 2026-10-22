# Unusual Whales pagination: live verification record

| | |
|---|---|
| **Date** | 2026-10-20, 04:4x–04:5x UTC (market closed; latest complete session 2026-10-19) |
| **Budget** | at most 10 live requests. **Used: 10** |
| **Credential** | the app's own UI-saved key, read via `SettingsStore` from a read-only copy of an existing install's `settings.json`. Never printed or stored |
| **Client** | the repository's `UWRestClient`: `max_retries=0` (1 logical call = 1 HTTP request), strictly sequential, concurrency 1 |
| **Recorded** | sanitized aggregates only. No response bodies, headers, keys, or row-level market data |
| **Docs consulted** | `PublicApi.DarkpoolController.darkpool_ticker`, `PublicApi.OptionTradeController.flow_alerts` |

## Request ledger (ticker SPY)

| # | Endpoint | Sanitized params | HTTP | Rows | Time range (UTC) | Observation |
|---|---|---|---|---|---|---|
| 1 | `/api/darkpool/SPY` | date=2026-10-19, limit=500 | 200 | 500 | 19:59:10 → 23:53:35 | newest-first; all on NY date 10-19; **432 distinct `tracking_id` in 500 rows** |
| 2 | `/api/darkpool/SPY` | date=2026-10-19, limit=500, older_than=19:59:10Z | 200 | 500 | 19:56:10 → 19:59:09 | **older_than honoured, EXCLUSIVE** (0 rows at/after cursor); continues seamlessly |
| 3 | `/api/darkpool/SPY` | date=2026-10-19, newer_than=13:30:00Z, older_than=13:31:00Z | 200 | 145 | 13:30:00 → 13:30:59 | **newer_than INCLUSIVE**; all rows in bounds; **short page (145 < 500) = window exhausted** |
| 4 | `/api/darkpool/SPY` | date=2026-10-19, older_than=08:00:00Z, limit=10 | 200 | 0 | n/a | before any trading that day: **HTTP 200 with an empty list** (not an error) |
| 5 | `/api/darkpool/SPY` | date=2026-09-08, limit=5 | 200 | 5 | 2026-09-08 23:51 → 23:57 | **~30 sessions back is within Basic entitlement** |
| 6 | `/api/option-trades/flow-alerts` | ticker_symbol=SPY, limit=200 | 200 | 200 | 19:55:06.46 → 20:14:47.86 | newest-first; microsecond timestamps; ids unique; all SPY |
| 7 | `/api/option-trades/flow-alerts` | + older_than=19:55:06.462557Z | 200 | 200 | 19:50:07.94 → 19:55:06.46 | **older_than INCLUSIVE**: the 3 rows at the exact cursor were returned again |
| 8 | `/api/option-trades/flow-alerts` | newer_than=2026-10-19T13:30Z, older_than=13:45Z | 200 | 200 | 13:38:39 → 13:44:54 | full page inside bounds: window **not** exhausted (SPY ≈ 30–40 alerts/min) |
| 9 | `/api/option-trades/flow-alerts` | newer_than=2026-09-08T13:30Z, older_than=14:30Z, limit=5 | 200 | 5 | 2026-09-08 14:25 → 14:29 | **~30 sessions back within Basic entitlement** |
| 10 | `/api/option-trades/flow-alerts` | newer_than=2026-10-16T20:10Z, older_than=2026-10-19T13:30Z | 200 | 47 | Fri 20:10:08 → Mon 12:22:47 | **short page = window exhausted**; crosses the weekend; alerts exist **before the 09:30 open** |

Latency was 0.06–0.17 s per request. No 429, no 5xx, no retries. Field sets were identical to the recorded
captures and to the adapters' expectations; every field the collector reads was present.

## Verified capabilities (VERIFIED = observed in the responses above)

| Capability | Dark pool | Flow alerts |
|---|---|---|
| Date filtering | VERIFIED (`date`, rows only on that NY date) | not offered (documented); time windows instead |
| `older_than` pagination | VERIFIED, **exclusive**, second resolution | VERIFIED, **inclusive**, microsecond resolution |
| `newer_than` lower bound | VERIFIED, inclusive | VERIFIED (all rows ≥ bound) |
| Ordering | VERIFIED newest-first | VERIFIED newest-first (undocumented) |
| Stable identifiers | `tracking_id` **not unique**; `(tracking_id, executed_at, price, size, volume)` unique on every page and stable across re-fetches (captured DB: 4,624 re-fetched prints, 0 changes) | `id` unique |
| Historical sessions | VERIFIED ≥ ~30 sessions on Basic | VERIFIED ≥ ~30 sessions on Basic |
| End of history | VERIFIED: empty 200 | VERIFIED: short page |
| Overlap on repeated pagination | none with an exclusive cursor; the collector re-reads the boundary second deliberately | boundary rows repeat (inclusive), deduplicated by `id` |
| Beyond entitlement | documented 403 `historic_data_access_missing` (not exercised: not reached within ~30 sessions) | same (documented) |

## Consequences for the collector (implemented)
- **Bounded windows.** Every collection request sends both `newer_than` and `older_than`, so a short page
  proves its window exhausted. A record at the open is no longer required, so quiet or thin sessions can be
  complete. The flow-alerts 7-day default never applies.
- **Per-endpoint cursor steps.** Dark pool uses the oldest row + 1 s (exclusive, second resolution; the
  boundary second is re-read and deduplicated). **Observed 2026-10-22 (real NVDA walks):** on some pages the
  dark-pool endpoint also returns rows stamped exactly AT the exclusive `older_than` second (whole seconds,
  reproducible on retry, including a first page bounded at close + 1 s). The collector drops those rows (they
  are outside the window by their own timestamp, and that second is the previous page's) instead of rejecting
  the page; before this, 3 of ~26 NVDA sessions were left `unknown: bounds_not_honored`. Flow alerts use the oldest row (inclusive; boundary rows
  deduplicated by `id`).
- **Dark-pool identity** is the composite key above. 05c47ca's value fingerprint
  (ticker, executed_at, price, size, premium) silently dropped **437** distinct prints in the captured DB, and
  this branch's earlier `tracking_id` key would have dropped more. Both are corrected.
- **Coverage statuses and reasons** are listed in `backend/app/session_collection.py` and
  [SIGNAL_INTEGRITY_RULES.md](SIGNAL_INTEGRITY_RULES.md).

## Not verified (honestly)
- **Rate limits / daily quotas on Basic.** The repo's notes say there is no daily or minute cap and a ceiling of
  3 concurrent requests. Only 10 sequential requests were made, so this is **INFERRED**, not verified.
- **The exact entitlement cutoff date.** Not probed (it would need extra requests). The collector maps the
  documented 403 to `unavailable / provider_history_cutoff` and stops backfilling.
- **Flow-alert default filters.** The documented `all_opening=true` default had no visible effect: every
  returned row had `all_opening_trades=false`. Whatever filter applies is identical across sessions, so the
  baseline remains like-for-like.

## Path to 20 eligible sessions on Basic (INFERRED from verified densities)

Densities measured on SPY:

| Dataset | Rate | Rows per session | Pages per session |
|---|---|---|---|
| Dark pool | ~100–170 prints/min | ~40k–65k | **~80–130** (at 500/page) |
| Flow alerts | ~30–40 alerts/min | ~12k–16k | **~60–80** (at 200/page) |

So SPY needs about 140–210 requests per session, or **~3,000–4,200 requests for 20 sessions**. Less liquid
tickers need a handful of pages per session.

Default schedule:
- **Scheduler:** `enabled` mode only, regular market hours only, 15-minute cycles (26 per day). Each cycle gives
  60 pages to each of 2 rotating watchlist tickers.
- **For a 4-ticker watchlist:** about 780 pages per ticker per trading day. That is **~4–6 trading days for
  SPY-like names, about one day for mid-caps**.
- **The explicit backfill route** (`POST /api/ticker/{t}/baseline/backfill`, ≤ 200 pages per call) can
  accelerate this at any time.
- **Requirement:** the ~30 verified sessions of history must cover the 20 prior sessions needed.
- **Feasibility on Basic** depends on the unverified "no daily cap" note above.
