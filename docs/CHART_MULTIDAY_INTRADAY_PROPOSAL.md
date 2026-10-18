# Multi-day intraday chart ranges (5D / 1W / custom range) -- design proposal

**Status:** Documentation only. Not implemented this round (explicitly
deferred by Chad per the Step 4 scoping decision, 2026-10-17). No
application code in this document's companion commit touches ingestion,
caching, or the frontend range selector beyond the existing 1D/1M/3M/6M/1Y.

## Why this doesn't already work

The current 1D range (`backend/app/core/candles.py`, `CHART_RANGES` in
`frontend/app.core.js`) fetches UW's intraday OHLC endpoint for exactly
**one trading day** and caches that single day's bars. There is no
multi-day intraday cache: asking for "5 days of 5-minute bars" today
would mean either (a) five separate intraday fetches stitched together
client-side with no existing code to do that stitching, or (b) hoping
UW's own `window`/range parameter accepts a multi-day span for intraday
granularity -- which has **not been verified live** against the real UW
API from this codebase. Promising 5D/1W/custom-range today would be
promising something nobody has confirmed UW actually serves.

## What must be verified against the real UW API before promising this

1. **Does UW's OHLC endpoint's `window`/range parameter accept multi-day
   spans at intraday (5-minute) granularity**, or does it silently
   downgrade to daily bars once the window exceeds one session (as some
   vendors do)? This is the single highest-priority unknown -- it
   determines whether this feature is "add a parameter" or "build
   client-side day-stitching."
2. If multi-day intraday IS supported server-side: what is the real
   per-request payload size/row count for, say, 5 trading days of
   5-minute bars (5 days x ~78 bars/day x however many fields) -- does
   it fit comfortably in one response, or does UW paginate/truncate it?
3. If multi-day intraday is NOT supported in one call: can the existing
   darkpool-trades-style `date` parameter pattern (one call per calendar
   day, already used elsewhere in this codebase per the Activity
   Timeline's own data-coverage finding) be reused for OHLC, and what
   is the real rate-limit/latency cost of N sequential daily calls for
   N=5 (5D) vs N=7-10 (1W, accounting for weekends/holidays)?
4. Do intraday bars' timestamps/session boundaries behave consistently
   across a multi-day fetch (pre/post-market inclusion, DST transitions
   if the range straddles one) -- i.e., does `select_regular_session_bars`
   (candles.py) need to run per-day or can it run once over the
   concatenated multi-day series?

None of this can be answered from documentation alone with full
confidence; it requires at least one live, authenticated call against
each candidate parameter shape, reviewed before any UI promises 5D/1W.

## Backend work this would actually require (once verified)

- **New cache shape.** The current OHLC cache is keyed per
  `(ticker, range)` with `range` implying a fixed bar granularity (daily
  for 1M+, intraday-single-day for 1D). A multi-day intraday range needs
  a cache entry that is explicitly `(ticker, start_date, end_date,
  intraday=True)`-shaped, distinct from the existing single-day 1D
  entry, so a 5D request doesn't collide with or invalidate the 1D
  cache (and vice versa).
- **Ingestion**: either (a) a single multi-day intraday UW call if
  verified to work, normalized through the existing
  `select_regular_session_bars` path, or (b) a day-by-day loop (mirrors
  the darkpool `date`-param pattern already in this codebase) with
  explicit per-day error handling -- a failed/missing day in the middle
  of a 5-day window must be disclosed (honest partial-coverage note,
  same convention `renderEvidencePanel`'s excluded-marker disclosure
  already uses), never silently dropped or backfilled with a
  flat/estimated bar.
- **Evidence markers across days**: `nearestBarTime`'s "marker predates
  the series -> falls back to the first candle" behavior (currently
  intentional for a SINGLE day/range) needs re-examination for a
  multi-day intraday series -- a marker landing in an overnight gap
  between two trading days' sessions should map to the nearest REAL
  session's bar, not silently clamp across a multi-day boundary in a
  way that misattributes it to the wrong day.
- **This round's evidence histogram** (`mountEvidenceHistogram`,
  `frontend/app.core.js`) needs no structural change for this: it
  already buckets per real candle-bar time, reused verbatim from
  `groups` -- a multi-day intraday series' bars are still just "real bar
  times," so the histogram would automatically get finer-grained
  multi-day buckets for free once the underlying OHLC data supports it.

## Frontend scope (once backend support lands)

- Add `5D`/`1W` (and later a custom date-range picker) to
  `CHART_RANGES` in `frontend/app.core.js` and the corresponding
  `/api/ticker/{t}/ohlc?range=` handling in `backend/app/main.py`.
- **Recommendation: treat 5D and 1W as the SAME selector.** US equities
  trade 5 days/week, so "5 trading days" and "1 calendar week" are the
  same real window in the overwhelming majority of cases (the only
  divergence is a holiday inside the window, which shortens "1 week" to
  fewer than 5 trading days anyway -- there is no case where they
  meaningfully differ for this app's purposes). Offering both as
  separate buttons would imply a distinction that doesn't exist in the
  real underlying data and would double the surface area for the
  verification/testing work above with no real user benefit. A single
  "5D" button (or "1W" -- either label, same real window) covers it.
- A true custom date-range picker is a materially larger scope increase
  (arbitrary start/end, not just a fixed preset) and should be scoped
  as its own follow-up after 5D/1W ships and is verified against real
  UW data in production use, not bundled into the same round.

## Summary

Nothing here is implemented. The concrete next step, before any backend
work, is a live (real, authenticated, logged) call against UW's OHLC
endpoint with a multi-day `window` value to settle the single open
question in section "What must be verified" above -- that result
determines whether this is a small parameter change or a real
day-stitching ingestion project.
