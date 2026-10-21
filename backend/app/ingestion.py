"""Phase B: shared historical-catalog ingestion layer.

Both the EXISTING on-demand Radar / Ticker-Analysis / Discovery routes in
main.py and any FUTURE scheduled background worker (Phase D -- not built
here) call through this ONE module to persist whatever the adapter
already fetched into the Phase-A/B catalog tables (app/migrations.py).

HARD DESIGN RULE (do not violate): this module is a WRITE-ONLY shadow of
whatever the existing live-fetch path already returned to the caller. It
never decides what a Radar/Ticker-Analysis/Discovery response body
contains, never substitutes a cataloged/cached row for a fresh adapter
call, and a failure here must never surface as a failure of the calling
route -- every public function in this module is safe to wrap in a bare
`try/except Exception: pass` by its caller (main.py does exactly that).
backend/app/core/signal_engine.py is never imported or touched here.

Dedup: every raw table has `UNIQUE(source_id)` (see migrations.py). When
the real UW row carries a genuine unique id, that is `source_id`
directly. When it doesn't (confirmed live for dark-pool rows during this
phase's bounded validation -- see FINAL REPORT), a deterministic
fingerprint (sha256 of the fields that together uniquely identify one
real event) stands in, so the exact same upstream event re-ingested
twice (e.g. two overlapping paginated requests) is still suppressed by
the same UNIQUE constraint rather than double-inserted.

Corrections: `catalog_*` functions NEVER UPDATE an existing raw row.
When a caller determines a previously-cataloged source_id's data has
changed upstream (see `reconcile_correction`), the new value is inserted
as a BRAND NEW row (new id, new source_id suffix) and a
`source_corrections` link records old-id -> new-id. The original row is
never touched.

Storage guard: every write here goes through app/storage_guard.py's
EXISTING circuit breaker (no second storage-guard mechanism). Callers
pass `kind="on_demand"` (today's only real caller) or `kind="background"`
(for the Phase D scheduler once it exists) -- see `IngestionWriteGuard`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .core.trading_calendar import session_end_utc
from .migrations import run_migrations
from .storage_guard import BACKGROUND, ONDEMAND, StorageGuard, default_mount_point

ON_DEMAND = ONDEMAND  # re-exported under the name this module's own docs/tests use

# Stamped on every signal_evaluations row this build writes. Rows written by the
# pre-remediation engine carry "v1": their gates had no explicit status, failed
# collections read as zero activity and synthetic gates carried weight. Those rows
# stay in the table as an audit trail, but they are never served as a ticker's
# *current* read (get_latest_signal_evaluation) or used to annotate new lifecycle
# history (list_signal_evaluation_rules). Found by the Phase 3 Docker upgrade
# rehearsal: after upgrading, Live showed 05c47ca's BUILDING/INVALIDATED verdicts.
ENGINE_RULE_VERSION = "v2-signal-integrity"
BACKGROUND_KIND = BACKGROUND


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_dt(ts: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO8601 parse to a tz-aware UTC datetime. Returns None on
    anything unparsable so callers can treat it as "unknown" rather than
    crash -- same tolerant-parse convention as `should_refetch` below."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_date_for_bar(event_time) -> Optional[date]:
    """A price_bars `event_time` is either a plain 'YYYY-MM-DD' (daily
    bar, UW's own date for that session) or a full ISO instant (intraday
    bar's start_time) -- either way, the first 10 characters are always
    the real trading date. Returns None when unparsable (caller treats
    finality as unknown, never guesses)."""
    try:
        return datetime.fromisoformat(str(event_time)[:10]).date()
    except ValueError:
        return None


# R8-2 INVESTIGATION FINDING (chart-data-integrity-fixes round 9,
# 2026-10-18), stated plainly per this round's requirement to report
# honestly either way: UW's real `/api/stock/{ticker}/ohlc/{size}`
# response DOES carry a per-row `market_time` field (confirmed live,
# see `backend/app/core/candles.py`'s own module docstring, gotcha #3 --
# values "pr"/"r"/"po" for pre-market/regular/post-market). This
# codebase already uses it (`select_regular_session_bars`) to pick the
# correct SESSION-TYPE row out of up to 3 UW returns per calendar date.
#
# HOWEVER: `market_time` is a session-type CLASSIFIER, not a
# completeness/finality signal. UW can and does return an `"r"`
# (regular-session) row for a date whose regular session is still
# actively in progress (intraday), with OHLC/volume that keep changing
# on every subsequent poll until the session genuinely ends -- there is
# no separate flag anywhere in the real response (confirmed against the
# documented OpenAPI schema and this adapter's own live-verified field
# list) that says "this particular row's data has finished
# consolidating." So the honest answer is: NO, UW does not expose a
# reliable SOURCE-based completeness signal for daily OHLC bars. The
# only thing this codebase can observe is its OWN fetch wall-clock time
# relative to the real NYSE session close (which `market_time` plays no
# role in beyond confirming "r" is the regular-session row).
#
# POLICY IMPLEMENTED (since no source signal exists): fetch-time-based
# finality, as before, PLUS a conservative grace period after the real
# session close -- never an instantaneous "fetched at/after close ->
# final" edge. NYSE/SIP/FINRA allow late trade reporting and exchange
# corrections for a window after the closing cross; a bar fetched in
# the first few minutes after 4:00pm (or 1:00pm on an early-close day --
# trading_calendar.session_end_utc already returns the CORRECT close
# for either case, so this grace period applies uniformly to both) is
# at real, documented risk of still missing late prints. This grace
# period is a HEURISTIC, documented as such, not a guarantee -- see
# DAILY_BAR_FINALITY_GRACE_SECONDS below for the exact value and
# rationale, and `catalog_price_bars`'s own updated docstring for how a
# genuine correction arriving AFTER a bar has already been marked final
# is still honored (never a new permanent-lock trap).
DAILY_BAR_FINALITY_GRACE_SECONDS = 20 * 60  # 20 minutes past the real session close


def _bar_is_final(
    event_time, retrieved_at: Optional[str],
    grace_period_s: float = DAILY_BAR_FINALITY_GRACE_SECONDS,
) -> bool:
    """chart-data-integrity-fixes (2026-10-18, Issue 2; widened round 9,
    R8-2): True only when this bar's real NYSE trading session had
    GENUINELY closed (per trading_calendar.session_end_utc -- ET/DST-
    aware and early-close-aware, the same real calendar this codebase
    already trusts server-side) AT LEAST `grace_period_s` seconds before
    the moment this fetch happened (`retrieved_at`). A bar fetched
    mid-session, or within the grace window right after close, is not
    final; a bar fetched comfortably after that real session's close is
    treated as final.

    This is a PROXY, not a guarantee UW's own upstream data is done
    revising at that instant -- see the module note above this function
    (R8-2's honest finding: UW's OHLC response has no real completeness
    flag, only a session-TYPE classifier) and `catalog_price_bars`'s
    docstring for how a correction landing even after this grace period
    is still accepted, never permanently blocked. Always returns False
    (never-final / unknown) rather than guessing True on any parse
    failure, and returns False for any date with no real trading session
    (weekend/holiday -- `session_end_utc` returns None, there is no
    close to have genuinely passed) -- a False result is always SAFE
    here: it only means "a later fetch is still allowed to replace this
    bar," never "this bar is wrong."
    """
    trading_date = _event_date_for_bar(event_time)
    if trading_date is None:
        return False
    fetched_at = _parse_iso_dt(retrieved_at)
    if fetched_at is None:
        return False
    close_utc = session_end_utc(trading_date)
    if close_utc is None:  # weekend/holiday -- not a real session, can't assess finality this way
        return False
    return (fetched_at - close_utc).total_seconds() >= grace_period_s


def _bars_content_equal(a: dict, b: dict) -> bool:
    """O/H/L/C/V equality check (used only to decide whether a repeated
    fetch for an already-cataloged date is a genuine no-op -- see
    catalog_price_bars). A tiny float tolerance avoids a spurious
    "changed" classification from harmless float round-tripping; this is
    NOT where real price corrections are detected (those differ by far
    more than this epsilon)."""
    for key in ("o", "h", "l", "c"):
        av, bv = a.get(key), b.get(key)
        if av is None or bv is None:
            if av != bv:
                return False
            continue
        if abs(float(av) - float(bv)) > 1e-9:
            return False
    return a.get("v") == b.get("v")


# R8-4 (chart-data-integrity-fixes round 9, 2026-10-18): bounded growth
# for daily-bar revisions. `price_bars` is append-only by this
# codebase's own established design rule (see this module's docstring
# and catalog_price_bars's own Issue-2-fix comment: "every value this
# app ever legitimately saw for a date remains in the table forever,
# exactly like every other catalog_* table's append-only contract") --
# but that rule was written assuming infrequent corrections, not a
# genuinely LIVE, still-in-progress session being polled repeatedly
# while non-final (its OHLC/volume can legitimately change on every
# poll until the real close). Without a bound, a long-lived process
# polling one ticker's daily chart throughout a trading day could
# accumulate an unbounded number of real, distinct revision rows for
# the SAME (ticker, timeframe, event_time) before it ever goes final.
#
# Honest scoping of this bound (deliberately narrow, not a reversal of
# the append-only rule): it ONLY ever prunes SUPERSEDED revisions for a
# date that is STILL NON-FINAL right now -- the current pointer row is
# NEVER a candidate for deletion (the join below always excludes it),
# and the moment a date's pointer becomes final, no further inserts
# happen for it at all (barring the rare, already strictly-newer-and-
# itself-final late-correction path), so a final date's prior revision
# count is simply frozen at whatever it was, never pruned further. This
# keeps the most recent PRICE_BAR_REVISION_KEEP superseded revisions
# (default 20 -- generous headroom for real intraday-polling churn
# while a session is live) for audit, and discards only the OLDEST
# excess beyond that. A legacy/rarely-refetched date (the overwhelming
# majority of rows) never approaches this bound and is never touched.
PRICE_BAR_REVISION_KEEP = 20


def _prune_superseded_price_bar_revisions(
    conn: sqlite3.Connection, ticker: str, timeframe: str, event_time,
    keep: int = PRICE_BAR_REVISION_KEEP,
) -> None:
    """Deletes the OLDEST superseded (non-current) `price_bars` rows for
    this exact (ticker, timeframe, event_time) beyond the most recent
    `keep`, if the current pointer for this date is NOT final (see the
    module note above for why this is scoped that way and why it does
    not regress this codebase's append-only design rule in any case
    that actually matters in practice). Only ever called for daily
    (`timeframe == "1d"`) bars, immediately after catalog_price_bars's
    own pointer upsert for this date -- never touches any OTHER date's
    rows, and never deletes the row price_bar_current currently points
    at, confirmed by the `pb.id != pc.price_bar_id` exclusion below."""
    pointer = conn.execute(
        "SELECT price_bar_id, is_final FROM price_bar_current "
        "WHERE ticker = ? AND timeframe = ? AND event_time = ?",
        (ticker, timeframe, event_time),
    ).fetchone()
    if pointer is None:
        return
    current_bar_id, is_final = pointer
    if is_final:
        return  # confirmed final -- no further revisions will arrive through this path; nothing to bound
    superseded_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM price_bars "
            "WHERE ticker = ? AND timeframe = ? AND event_time = ? AND id != ? "
            "ORDER BY id DESC",
            (ticker, timeframe, event_time, current_bar_id),
        ).fetchall()
    ]
    stale_ids = superseded_ids[keep:]  # keep the most recent `keep`, prune anything older
    if stale_ids:
        conn.executemany("DELETE FROM price_bars WHERE id = ?", [(i,) for i in stale_ids])


def _num(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return v


def _is_canceled(trade: dict) -> bool:
    return trade.get("canceled") in (True, "true", "True", 1)


def darkpool_print_key(ticker: str, trade: dict) -> str:
    """Verified-unique, re-fetch-stable identity of one UW dark-pool print."""
    return "dp:" + fingerprint(
        str(ticker).upper(), trade.get("tracking_id"), trade.get("executed_at"),
        _num(trade.get("price")), _num(trade.get("size")), _num(trade.get("volume")),
    )


def fingerprint(*parts: object) -> str:
    """Deterministic dedup key for an upstream record that carries no
    genuine unique id of its own. Order-sensitive and type-stable (every
    part is str()'d first) so the SAME logical event always produces the
    SAME fingerprint across repeated/overlapping fetches -- that's what
    lets the UNIQUE(source_id) constraint do real dedup work for it."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return "fp_" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:40]


class StorageGuardPausedError(RuntimeError):
    """Mirrors app.storage.StorageGuardPausedError but for ingestion
    writes specifically -- raised instead of silently writing when the
    shared storage-guard circuit breaker reports the configured
    `kind` threshold is breached. Callers that want ingestion to be
    best-effort (main.py's on-demand wiring) catch this; a future
    background worker (Phase D) can instead use it to skip a cycle."""

    def __init__(self, result):
        super().__init__(result.reason)
        self.result = result


class IngestionCatalog:
    """Thin SQLite wrapper over the Phase A/B catalog tables, deliberately
    modeled on app/storage.py's QualifyingDaysStore (same ':memory:'
    persistent-connection special case, same file-path open/close-per-call
    pattern, same storage-guard wiring) so it behaves identically under
    test and in the real Docker deployment -- it is NOT a second database;
    `db_path` is the SAME file QualifyingDaysStore already uses, so the
    catalog tables created by app/migrations.py live right alongside
    ticker_scan_log/ticker_current_state in one file, inside the one
    `tip-data` Docker volume.
    """

    def __init__(self, db_path: str = "./data/tip_hackathon.db"):
        self.db_path = db_path
        self._persistent_conn = (
            sqlite3.connect(db_path, check_same_thread=False) if db_path == ":memory:" else None
        )
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._storage_guard: Optional[StorageGuard] = (
            None if db_path == ":memory:" else StorageGuard(default_mount_point(db_path))
        )
        # Runs the SAME versioned migration runner QualifyingDaysStore
        # uses (app/migrations.py) -- idempotent, safe to call from both
        # stores against the same underlying file; whichever constructs
        # first actually creates the schema, the other's call is a
        # no-op. Never a second/competing schema definition.
        with self._conn() as conn:
            run_migrations(conn, self.db_path)

    @contextmanager
    def _conn(self):
        if self._persistent_conn is not None:
            yield self._persistent_conn
            return
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            yield conn
        finally:
            conn.close()

    def _check_guard(self, kind: str) -> None:
        if self._storage_guard is None:
            return
        result = self._storage_guard.check_before_write(kind)
        if not result.allowed:
            raise StorageGuardPausedError(result)

    # --- raw-observation catalog writes --------------------------------

    def catalog_options_flow_observations(
        self, ticker: str, flow_alerts: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Persists each already-normalized flow-alert dict (the EXACT
        shape live_uw_adapter._normalize_flow_alert already produces --
        ask_side/bid_side premium under their real UW names are preserved
        verbatim in raw_json; no new field semantics invented here).
        Returns the number of NEW rows actually inserted (dupes silently
        suppressed by UNIQUE(source_id), not counted as an error)."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for alert in flow_alerts or []:
                source_id = (
                    alert.get("id") or alert.get("alert_id")
                    or fingerprint(
                        ticker, alert.get("created_at"), alert.get("total_premium"),
                        alert.get("total_ask_premium"), alert.get("total_bid_premium"),
                        alert.get("type"),
                    )
                )
                # Evidence time is the provider's own timestamp. A row without a
                # parseable one is not evidence: never stamp it with the retrieval
                # time (it would be counted inside whatever session that falls in).
                event_time = alert.get("created_at")
                if _parse_iso_dt(event_time) is None:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO options_flow_observations "
                    "(source_id, ticker, event_time, retrieved_at, raw_json) VALUES (?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, json.dumps(alert, default=str)),
                )
                if cur.rowcount:
                    inserted += 1
            conn.commit()
        return inserted

    def catalog_darkpool_observations(
        self, ticker: str, darkpool_trades: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Confirmed during this phase's bounded live validation: real UW
        dark-pool rows do NOT carry a field that is reliably a genuine
        unique id across repeated/paginated fetches (see FINAL REPORT) --
        a deterministic fingerprint over (ticker, executed_at, price,
        size, premium) is used as source_id whenever no id-shaped field
        is present, which is the common case. When UW DOES send an
        explicit id-like field (`id`/`trade_id`), it is preferred."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for trade in darkpool_trades or []:
                # Print identity (VERIFIED against live UW + the captured DB):
                # UW `tracking_id` is NOT unique -- distinct prints share it
                # (500-row live page: 432 distinct tracking_ids). The tuple
                # (tracking_id, executed_at, price, size, volume) is unique on
                # every page and stable across re-fetches; `volume` (day
                # volume at the print) is what separates same-second,
                # same-size prints that 05c47ca's value fingerprint collapsed
                # (437 real prints lost in the captured DB).
                source_id = trade.get("id") or trade.get("trade_id") or darkpool_print_key(ticker, trade)
                event_time = trade.get("executed_at")
                if _parse_iso_dt(event_time) is None:   # not evidence: never use the retrieval time
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO darkpool_observations "
                    "(source_id, ticker, event_time, retrieved_at, raw_json) VALUES (?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, json.dumps(trade, default=str)),
                )
                if cur.rowcount:
                    inserted += 1
                elif _is_canceled(trade):
                    # C-F3: a print stored earlier and now reported CANCELED. Raw rows are append-only:
                    # record the cancellation as its own row (idempotent key); _iter_window then excludes
                    # every version of that print, and the window signature changes (cache rebuild).
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO darkpool_observations "
                        "(source_id, ticker, event_time, retrieved_at, raw_json) VALUES (?, ?, ?, ?, ?)",
                        (f"{source_id}#canceled", ticker, event_time, retrieved_at, json.dumps(trade, default=str)))
                    if cur.rowcount:
                        inserted += 1
            conn.commit()
        return inserted

    def catalog_price_bars(
        self, ticker: str, timeframe: str, candles: Iterable[dict], retrieved_at: Optional[str] = None,
        kind: str = ON_DEMAND,
    ) -> int:
        """Persists the OUTPUT of candles.build_candles() as-is (post
        session-classification/delta-correction) -- never re-derives or
        second-guesses that logic, just stores its result. Candles carry
        no upstream id at all, so source_id is always a fingerprint over
        (ticker, timeframe, t[, retrieved_at] -- see below); dupes are
        suppressed (not counted as a real change).

        ISSUE 2 FIX (chart-data-integrity-fixes, 2026-10-18): for DAILY
        (`timeframe == "1d"`) bars specifically, the OLD source_id was a
        fingerprint of (ticker, timeframe, t) ONLY -- never the bar's own
        OHLCV content. A bare INSERT OR IGNORE on that key meant a LATER
        re-fetch for an already-cataloged date, however different (e.g.
        the real post-close final bar replacing one cataloged
        mid-session), produced the exact same source_id and was silently
        discarded by UNIQUE(source_id) every time -- permanently locking
        in whichever value was cataloged FIRST. Confirmed as the real
        cause of a genuine cross-installation SPY Oct-16 discrepancy
        ($771.36 mid-session vs. $763.25 the real final close).

        Fix (daily bars only): resolve the CURRENT authoritative row for
        (ticker, timeframe, event_time) via the `price_bar_current`
        pointer table (added by this round's migration; see its own
        comment for why a new table, not an ALTER on price_bars). `price_bars`
        itself STAYS append-only -- a genuine replacement is always a
        BRAND NEW row (new source_id, now content-sensitive via
        `retrieved_at`), never an UPDATE of an existing one, so every
        value this app ever legitimately saw for a date remains in the
        table forever, exactly like every other catalog_* table's
        append-only contract. Only the POINTER (which row is "current"
        for this date) is updated in place -- the same bookkeeping
        category as `scheduler_state`/`ingestion_jobs` above, not a raw
        observation row.

        Directional safety (never regress a good bar to a worse one):
          - No existing pointer for this date -> always insert + point
            (first real value seen for that date).
          - Existing pointer not final, incoming `retrieved_at` OLDER
            than the pointer's own -> rejected (never replace with
            staler data).
          - Existing pointer not final, incoming content AND finality
            are both unchanged from the current pointer -> a harmless
            repeated fetch, treated as a no-op (not counted as a real
            change) -- preserves the pre-existing dedup-on-repeat-fetch
            guarantee this function has always had.
          - Existing pointer's bar already confirmed FINAL (see
            `_bar_is_final`) -> R8-2 WIDENED RULE (round 9, 2026-10-18):
            no longer an unconditional permanent lock. A genuine late
            correction (R8-2's own requirement -- "a correction arriving
            after the grace period should still be accepted if it's
            more complete than what's stored... don't make the
            grace-period itself a new way to permanently lock in a bad
            bar") is accepted when BOTH: (a) the incoming fetch's
            `retrieved_at` is strictly NEWER than the stored pointer's
            own `retrieved_at` (never replace with data fetched earlier
            or at the same instant -- that direction is still always
            rejected, preserving the non-regression guarantee), AND
            (b) the incoming fetch ALSO computes as final under
            `_bar_is_final` (since `retrieved_at` only increases and the
            trading date/close time are fixed, a strictly-later fetch
            for an already-past, already-final session always re-
            computes final too -- this is never a path for a mid-
            session/less-complete snapshot to overwrite a final one).
            Content-unchanged corrections are still a no-op (see above).
            This is the ONLY way a confirmed-final bar's pointer can
            ever move; it can never move to anything less trustworthy
            than what it already points at.
          - Otherwise (content changed, or finality newly achieved) ->
            a real replacement: insert the new row, repoint, count it.

        Intraday (`timeframe != "1d"`) bars are UNCHANGED (still plain
        INSERT OR IGNORE, no pointer table involved) -- deliberately out
        of this fix's scope; see this round's FINAL REPORT for why (each
        intraday bucket closes every few minutes, a materially smaller
        and different staleness window than a full trading day, and not
        what the real reported discrepancy showed)."""
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        inserted = 0
        with self._conn() as conn:
            for bar in candles or []:
                event_time = bar.get("t") or retrieved_at
                if timeframe != "1d":
                    source_id = fingerprint(ticker, timeframe, bar.get("t"))
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO price_bars "
                        "(source_id, ticker, event_time, retrieved_at, timeframe, open, high, low, close, volume) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(source_id), ticker, event_time, retrieved_at, timeframe,
                         bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v")),
                    )
                    if cur.rowcount:
                        inserted += 1
                    continue

                new_is_final = 1 if _bar_is_final(event_time, retrieved_at) else 0
                existing = conn.execute(
                    "SELECT pc.price_bar_id, pc.is_final, pc.retrieved_at, "
                    "pb.open, pb.high, pb.low, pb.close, pb.volume "
                    "FROM price_bar_current pc JOIN price_bars pb ON pb.id = pc.price_bar_id "
                    "WHERE pc.ticker = ? AND pc.timeframe = ? AND pc.event_time = ?",
                    (ticker, timeframe, event_time),
                ).fetchone()

                if existing is not None:
                    old_bar_id, old_is_final, old_retrieved_at, oo, oh, ol, oc, ov = existing
                    old_dt, new_dt = _parse_iso_dt(old_retrieved_at), _parse_iso_dt(retrieved_at)
                    if old_dt is not None and new_dt is not None and new_dt < old_dt:
                        continue  # staler than what's already cached -- rejected (true regardless of finality)
                    if old_is_final:
                        # R8-2 widened rule (round 9, 2026-10-18): a confirmed-final
                        # bar is no longer an unconditional lock -- a genuine late
                        # correction is accepted only when the incoming fetch is
                        # BOTH strictly newer (checked above) AND itself final (a
                        # strictly-later fetch for an already-past session always
                        # re-computes final, so this never admits a less-complete
                        # snapshot). Same-instant retrieved_at (new_dt == old_dt,
                        # not older) falls through here too and is correctly
                        # rejected by requiring new_is_final AND a real content
                        # change below -- see this function's updated docstring.
                        if not new_is_final:
                            continue  # would-be correction isn't itself final -- never trust it over a confirmed-final bar
                        if old_dt is not None and new_dt is not None and new_dt == old_dt:
                            continue  # same fetch instant, no genuine new information -- reject
                    unchanged = (
                        bool(new_is_final) == bool(old_is_final)
                        and _bars_content_equal(
                            {"o": oo, "h": oh, "l": ol, "c": oc, "v": ov},
                            {"o": bar.get("o"), "h": bar.get("h"), "l": bar.get("l"),
                             "c": bar.get("c"), "v": bar.get("v")},
                        )
                    )
                    if unchanged:
                        continue  # harmless repeat fetch of the same real value -- not a change

                source_id = fingerprint(ticker, timeframe, event_time, retrieved_at)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO price_bars "
                    "(source_id, ticker, event_time, retrieved_at, timeframe, open, high, low, close, volume) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(source_id), ticker, event_time, retrieved_at, timeframe,
                     bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v")),
                )
                new_bar_id = conn.execute(
                    "SELECT id FROM price_bars WHERE source_id = ?", (str(source_id),)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO price_bar_current (ticker, timeframe, event_time, price_bar_id, is_final, retrieved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ticker, timeframe, event_time) DO UPDATE SET "
                    "price_bar_id = excluded.price_bar_id, is_final = excluded.is_final, "
                    "retrieved_at = excluded.retrieved_at",
                    (ticker, timeframe, event_time, new_bar_id, new_is_final, retrieved_at),
                )
                _prune_superseded_price_bar_revisions(conn, ticker, timeframe, event_time)
                inserted += 1
            conn.commit()
        return inserted

    # --- S1-b fix (2026-10-16): read-only cache accessors for the OHLC
    # route's GET path -- mirror catalog_price_bars/catalog_options_flow_
    # observations/catalog_darkpool_observations exactly (same tables,
    # same shapes), so GET /api/ticker/{ticker}/ohlc can be made
    # structurally read-only without inventing a second storage layer.
    # These never write anything.

    def get_cached_price_bars(self, ticker: str, timeframe: str, limit: Optional[int] = None) -> list[dict]:
        """Real cataloged candles for (ticker, timeframe), chronological
        (ASC). When more than `limit` rows exist, returns the most
        RECENT `limit` (mirrors how a live fetch's own `limit` behaves --
        "give me the most recent N bars"), never an arbitrary slice.

        ISSUE 2 FIX (chart-data-integrity-fixes, 2026-10-18): daily bars
        can now have MULTIPLE append-only rows for the same event_time
        (see catalog_price_bars) -- a plain SELECT over price_bars alone
        would return every historical value ever cataloged for a date,
        double-(or more-)counting it. The LEFT JOIN against
        `price_bar_current` resolves each (ticker, timeframe, event_time)
        to ONLY its current authoritative row when a pointer exists;
        intraday rows never have a pointer row at all (catalog_price_bars
        never writes one for `timeframe != "1d"`), so `cur.price_bar_id
        IS NULL` keeps every intraday row exactly as before -- this
        fix changes nothing about intraday read behavior."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT pb.event_time, pb.open, pb.high, pb.low, pb.close, pb.volume "
                "FROM price_bars pb "
                "LEFT JOIN price_bar_current cur "
                "  ON cur.ticker = pb.ticker AND cur.timeframe = pb.timeframe AND cur.event_time = pb.event_time "
                "WHERE pb.ticker = ? AND pb.timeframe = ? "
                "  AND (cur.price_bar_id IS NULL OR cur.price_bar_id = pb.id) "
                "ORDER BY pb.event_time ASC",
                (ticker, timeframe),
            ).fetchall()
        bars = [
            {"t": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in rows
        ]
        if limit is not None and len(bars) > limit:
            bars = bars[-limit:]
        return bars

    def get_latest_daily_bar_final_status(self, ticker: str, timeframe: str) -> Optional[bool]:
        """R8-3 (chart-data-integrity-fixes round 9, 2026-10-18): the
        is_final flag for the most-recent CATALOGED daily bar's current
        pointer row -- lets a cache-only read honestly tell the frontend
        whether its trailing bar might still need a refresh, without
        the frontend ever having to guess from wall-clock time itself
        (the backend already owns the real finality determination, see
        `_bar_is_final`). Reads `price_bar_current` directly (the single
        pointer table that always resolves to the CURRENT authoritative
        bar per date -- see R8-4's `price_bar_current_latest` VIEW in
        migrations.py for the equivalent, documented SQL-level path any
        future direct-SQL caller should use instead of touching
        `price_bars` raw) rather than the raw append-only `price_bars`
        table, so a stale/superseded revision can never be mistaken for
        the authoritative one.

        Returns None when there is no cataloged daily pointer at all
        (nothing to assess) or when `timeframe != '1d'` (intraday bars
        never get a pointer row -- see catalog_price_bars -- so
        finality is not a meaningful question for them; the frontend
        must never show a trailing-bar refresh affordance for 1D)."""
        if timeframe != "1d":
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT is_final FROM price_bar_current_latest "
                "WHERE ticker = ? AND timeframe = ? ORDER BY event_time DESC LIMIT 1",
                (ticker, timeframe),
            ).fetchone()
        if row is None:
            return None
        return bool(row[0])

    def get_cached_flow_observations(self, ticker: str) -> list[dict]:
        """Every real cataloged options-flow-alert raw_json for `ticker`,
        deserialized back to the exact normalized dict shape
        catalog_options_flow_observations was given -- the same shape
        `build_markers` already consumes from a live fetch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM options_flow_observations WHERE ticker = ? ORDER BY event_time ASC",
                (ticker,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def get_cached_darkpool_observations(self, ticker: str) -> list[dict]:
        """Every real cataloged dark-pool-trade raw_json for `ticker`,
        same shape contract as get_cached_flow_observations above."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM darkpool_observations WHERE ticker = ? ORDER BY event_time ASC",
                (ticker,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def get_price_bars_cached_at(self, ticker: str, timeframe: str) -> Optional[str]:
        """Most recent REAL retrieved_at among this ticker/timeframe's
        cataloged price bars -- lets a cache-only response honestly
        disclose how stale its chart data is, instead of ever implying
        the read itself refreshed anything (see should_refetch's
        docstring for the sibling freshness-decision use of this same
        column; this is a plain disclosure read, not a refetch decision)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(retrieved_at) FROM price_bars WHERE ticker = ? AND timeframe = ?",
                (ticker, timeframe),
            ).fetchone()
        return row[0] if row and row[0] else None

    def catalog_discovery_snapshot(
        self, filters: dict, candidates: Iterable[dict], snapshot_at: str,
        kind: str = ON_DEMAND,
    ) -> Optional[int]:
        """One discovery_snapshots row (keyed by its own fingerprint over
        snapshot_at+filters, since a screener snapshot has no upstream id
        either) plus one discovery_candidates row per candidate. Returns
        the snapshot row id (new or the pre-existing one on a dedup hit),
        or None if the guard paused the write."""
        self._check_guard(kind)
        candidates = list(candidates or [])
        source_id = fingerprint("discovery_snapshot", snapshot_at, json.dumps(filters or {}, sort_keys=True, default=str))
        retrieved_at = utcnow_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO discovery_snapshots "
                "(source_id, event_time, retrieved_at, filters_json, candidate_count) VALUES (?, ?, ?, ?, ?)",
                (source_id, snapshot_at, retrieved_at, json.dumps(filters or {}, default=str), len(candidates)),
            )
            row = conn.execute(
                "SELECT id FROM discovery_snapshots WHERE source_id = ?", (source_id,)
            ).fetchone()
            snapshot_id = row[0] if row else None
            if snapshot_id is not None:
                for c in candidates:
                    ticker = c.get("ticker") or c.get("symbol") or ""
                    cand_source_id = fingerprint("discovery_candidate", source_id, ticker)
                    conn.execute(
                        "INSERT OR IGNORE INTO discovery_candidates "
                        "(source_id, snapshot_id, ticker, event_time, retrieved_at, raw_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (cand_source_id, snapshot_id, ticker, snapshot_at, retrieved_at,
                         json.dumps(c, default=str)),
                    )
            conn.commit()
        return snapshot_id

    def catalog_signal_evaluation(
        self, ticker: str, scan: dict, engine_rule_version: str = ENGINE_RULE_VERSION,
        retrieved_at: Optional[str] = None, kind: str = ON_DEMAND,
        evidence_refs: Optional[list] = None,
    ) -> Optional[int]:
        """Persists ONE scan_service.evaluate_ticker_scan() result (the
        exact dict main.py already builds -- never re-derived) as a
        signal_evaluations row, a signal_transitions row when the state
        genuinely changed, and links to whatever raw observations were
        cited as evidence via signal_evaluation_evidence.
        `evidence_refs`: list of (observation_table, observation_id)
        tuples -- e.g. the ids catalog_options_flow_observations/
        catalog_darkpool_observations just inserted/matched for this same
        scan, so the evaluation is traceably linked to its real inputs.
        """
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        event_time = retrieved_at
        source_id = fingerprint("signal_evaluation", ticker, event_time, scan.get("state"), scan.get("confidence"))
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO signal_evaluations "
                "(source_id, ticker, event_time, retrieved_at, state, confidence, engine_rule_version, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                # NO_STATE: an evaluation attempt whose collection failed for a
                # ticker with no lifecycle record yet (state stays None in raw_json).
                (source_id, ticker, event_time, retrieved_at, scan.get("state") or "NO_STATE",
                 scan.get("confidence"), engine_rule_version, json.dumps(scan, default=str)),
            )
            row = conn.execute(
                "SELECT id FROM signal_evaluations WHERE source_id = ?", (source_id,)
            ).fetchone()
            evaluation_id = row[0] if row else None

            previous_state = scan.get("previous_state")
            new_state = scan.get("state")
            if evaluation_id is not None and previous_state != new_state:
                transition_source_id = fingerprint(
                    "signal_transition", ticker, event_time, previous_state, new_state
                )
                conn.execute(
                    "INSERT OR IGNORE INTO signal_transitions "
                    "(source_id, ticker, event_time, retrieved_at, from_state, to_state, transition_rule) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (transition_source_id, ticker, event_time, retrieved_at,
                     previous_state, new_state, scan.get("transition_rule")),
                )

            if evaluation_id is not None:
                for observation_table, observation_id in (evidence_refs or []):
                    if observation_id is None:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO signal_evaluation_evidence "
                        "(evaluation_id, observation_table, observation_id, created_at) VALUES (?, ?, ?, ?)",
                        (evaluation_id, observation_table, observation_id, retrieved_at),
                    )
            conn.commit()
        return evaluation_id

    def catalog_ai_investigation(
        self, ticker: str, result: dict, simulated: bool, transport: str,
        retrieved_at: Optional[str] = None, kind: str = ON_DEMAND,
    ) -> Optional[int]:
        self._check_guard(kind)
        retrieved_at = retrieved_at or utcnow_iso()
        source_id = fingerprint("ai_investigation", ticker, retrieved_at)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ai_investigations "
                "(source_id, ticker, event_time, retrieved_at, simulated, transport, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source_id, ticker, retrieved_at, retrieved_at, int(simulated), transport,
                 json.dumps(result, default=str)),
            )
            row = conn.execute(
                "SELECT id FROM ai_investigations WHERE source_id = ?", (source_id,)
            ).fetchone()
            conn.commit()
            return row[0] if row else None

    # --- source corrections (append-only, never UPDATE) -----------------

    def reconcile_correction(
        self, observation_table: str, new_observation_id: int, old_observation_id: int,
        reason: str = "", kind: str = ON_DEMAND,
    ) -> None:
        """Records that `new_observation_id` (a brand-new row this caller
        JUST inserted into `observation_table` via one of the catalog_*
        methods above) supersedes `old_observation_id` in that SAME
        table. Never touches either row's own columns -- append-only."""
        self._check_guard(kind)
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO source_corrections "
                "(observation_table, observation_id, supersedes_observation_id, correction_reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (observation_table, new_observation_id, old_observation_id, reason, utcnow_iso()),
            )
            conn.commit()

    # --- ingestion job checkpointing (resumable pagination jobs) --------

    def start_ingestion_job(self, job_type: str, detail: dict, kind: str = ON_DEMAND) -> str:
        """Creates one `ingestion_jobs` row with status='running' and
        returns its source_id (the job's own handle, used by
        update_ingestion_job / find_resumable_job below). `detail` is a
        free-form JSON blob the caller controls -- for dark-pool
        pagination this is where the checkpoint cursor lives (see
        DarkPoolPaginator)."""
        self._check_guard(kind)
        now = utcnow_iso()
        source_id = fingerprint("ingestion_job", job_type, now, json.dumps(detail, sort_keys=True, default=str))
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ingestion_jobs (source_id, job_type, event_time, retrieved_at, status, detail) "
                "VALUES (?, ?, ?, ?, 'running', ?)",
                (source_id, job_type, now, now, json.dumps(detail, default=str)),
            )
            conn.commit()
        return source_id

    def update_ingestion_job(self, source_id: str, status: str, detail: dict) -> None:
        """Checkpoint update for a resumable job. This IS an UPDATE (not
        an append-only observation row) -- `ingestion_jobs` is job
        bookkeeping/progress state, not a raw-observation catalog table,
        so overwriting its own checkpoint in place is correct and
        intentional; it never touches any catalog table's rows."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE ingestion_jobs SET status = ?, detail = ?, retrieved_at = ? WHERE source_id = ?",
                (status, json.dumps(detail, default=str), utcnow_iso(), source_id),
            )
            conn.commit()

    def get_ingestion_job(self, source_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT source_id, job_type, status, detail FROM ingestion_jobs WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if not row:
                return None
            return {"source_id": row[0], "job_type": row[1], "status": row[2],
                    "detail": json.loads(row[3]) if row[3] else {}}

    def find_resumable_job(self, job_type: str, match: dict) -> Optional[dict]:
        """Finds the most recent `status='running'` job of this type whose
        detail dict contains every key/value in `match` (e.g.
        {"ticker": "SPY"}) -- used to resume an interrupted pagination
        run instead of starting over from scratch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT source_id, job_type, status, detail FROM ingestion_jobs "
                "WHERE job_type = ? AND status = 'running' ORDER BY id DESC",
                (job_type,),
            ).fetchall()
        for row in rows:
            detail = json.loads(row[3]) if row[3] else {}
            if all(detail.get(k) == v for k, v in match.items()):
                return {"source_id": row[0], "job_type": row[1], "status": row[2], "detail": detail}
        return None

    # --- dark-pool coverage classification (three distinct states) ------

    def record_coverage_status(
        self, ticker: str, coverage_date: str, status: str, pages_walked: int,
        session_start_verified: bool, kind: str = ON_DEMAND,
    ) -> None:
        """Upserts the darkpool_coverage_status row for (ticker, date).
        This IS allowed to overwrite in place (unlike a raw-observation
        table): coverage status is this app's own derived bookkeeping
        about how much of a day we've paginated through, not an upstream
        source record, so there is nothing here that 'destroys history'
        by being updated -- re-running pagination for the same day is
        expected to refine (never silently downgrade undetected) this
        row. `status` must be one of 'complete'/'incomplete'/'unknown'."""
        if status not in ("complete", "incomplete", "unknown"):
            raise ValueError(f"invalid coverage status: {status!r}")
        self._check_guard(kind)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO darkpool_coverage_status "
                "(ticker, coverage_date, status, pages_walked, session_start_verified, retrieved_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, coverage_date) DO UPDATE SET "
                "status=excluded.status, pages_walked=excluded.pages_walked, "
                "session_start_verified=excluded.session_start_verified, retrieved_at=excluded.retrieved_at",
                (ticker, coverage_date, status, pages_walked, int(session_start_verified), utcnow_iso()),
            )
            conn.commit()

    def get_coverage_status(self, ticker: str, coverage_date: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, pages_walked, session_start_verified, retrieved_at "
                "FROM darkpool_coverage_status WHERE ticker = ? AND coverage_date = ?",
                (ticker, coverage_date),
            ).fetchone()
            if not row:
                return None
            return {"status": row[0], "pages_walked": row[1],
                    "session_start_verified": bool(row[2]), "retrieved_at": row[3]}

    # --- Phase C: read-only accessors for the baseline methodology module
    # (app/core/dark_pool_baseline.py). Both are pure reads of whatever
    # Phase B already cataloged -- neither triggers a fetch, a backfill,
    # or any live call of any kind.

    def list_coverage_status(self, ticker: str) -> list:
        """Every darkpool_coverage_status row cataloged for `ticker`,
        oldest first -- includes 'complete'/'incomplete'/'unknown' rows
        alike; filtering those down to the eligible ('complete'-only) set
        is deliberately NOT done here (see
        dark_pool_baseline.eligible_sessions, the one place that happens)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT coverage_date, status, pages_walked, session_start_verified, retrieved_at "
                "FROM darkpool_coverage_status WHERE ticker = ? ORDER BY coverage_date",
                (ticker,),
            ).fetchall()
        return [
            {
                "coverage_date": row[0], "status": row[1], "pages_walked": row[2],
                "session_start_verified": bool(row[3]), "retrieved_at": row[4],
            }
            for row in rows
        ]

    def list_darkpool_observations_by_date(self, ticker: str) -> dict:
        """Every cataloged darkpool_observations row for `ticker`, grouped
        by the calendar date (the first 10 chars of its ISO `event_time`)
        it falls on, each shaped as {"event_time": ..., "premium": float}
        -- the minimal shape dark_pool_baseline.cumulative_through_elapsed
        needs. `premium` is read out of the stored `raw_json` (the exact
        upstream UW field `catalog_darkpool_observations` stored verbatim,
        never re-derived)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_time, raw_json FROM darkpool_observations "
                "WHERE ticker = ? ORDER BY event_time",
                (ticker,),
            ).fetchall()
        grouped: dict = {}
        for event_time, raw_json in rows:
            date_key = (event_time or "")[:10]
            premium = 0.0
            if raw_json:
                try:
                    data = json.loads(raw_json)
                    premium = float(data.get("premium") or 0)
                except (json.JSONDecodeError, TypeError, ValueError):
                    premium = 0.0
            grouped.setdefault(date_key, []).append({"event_time": event_time, "premium": premium})
        return grouped

    # --- Phase D: read-only accessor for freshness/dedup (market-wide
    # Radar). ONE query, ORDER BY retrieved_at DESC -- this is the whole
    # dedup mechanism between watchlist-sourced and discovery-sourced
    # evaluations of the same ticker: both paths write into this SAME
    # table via catalog_signal_evaluation above, so "most recent wins
    # regardless of source" falls straight out of this one query with no
    # separate source-tracking column needed. See
    # core/scheduler_freshness.py for the classification built on top of
    # this.

    def list_signal_evaluation_rules(self, ticker: str) -> list[dict]:
        """Read historical display metadata from existing evaluation JSON.

        This is read-only: no schema or stored record is modified. The catalog
        already persists each scan's exact transition_rule in raw_json.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_time, state, raw_json FROM signal_evaluations "
                "WHERE ticker = ? AND engine_rule_version = ? ORDER BY retrieved_at ASC, id ASC",
                (ticker, ENGINE_RULE_VERSION),
            ).fetchall()
        result = []
        for event_time, state, raw_json in rows:
            try:
                raw = json.loads(raw_json) if raw_json else {}
            except (json.JSONDecodeError, TypeError):
                raw = {}
            result.append({
                "event_time": event_time,
                "state": state,
                "transition_rule": raw.get("transition_rule"),
            })
        return result

    # --- session coverage (signal-integrity remediation) -------------------

    # columns added by migration 11 (settlement + provenance); NULL on legacy rows
    COVERAGE_PROVENANCE = ("proof_started_utc", "certified_by", "certified_at_utc", "session_rows", "max_gap_s",
                           "gap_flag", "reverify_trigger", "rows_before_reverify", "island_from_utc",
                           "island_through_utc")
    _COVERAGE_COLS = ("session_date", "status", "covered_from_utc", "covered_through_utc", "pages_walked",
                      "rows_seen", "last_error", "retrieved_at") + COVERAGE_PROVENANCE

    def _coverage_row(self, dataset: str, ticker: str, row) -> dict:
        return {"dataset": dataset, "ticker": ticker, **dict(zip(self._COVERAGE_COLS, row))}

    def get_session_coverage(self, dataset: str, ticker: str, session_date: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {', '.join(self._COVERAGE_COLS)} FROM session_coverage "
                "WHERE dataset = ? AND ticker = ? AND session_date = ?", (dataset, ticker, session_date)).fetchone()
        return self._coverage_row(dataset, ticker, row) if row else None

    def upsert_session_coverage(self, dataset: str, ticker: str, session_date: str, status: str,
                                covered_from_utc: Optional[str], covered_through_utc: Optional[str],
                                pages_walked: int, rows_seen: int, last_error: Optional[str] = None,
                                kind: str = ON_DEMAND, provenance: Optional[dict] = None) -> None:
        """Writes the WHOLE coverage state of one session. `provenance` holds the
        migration-11 columns; any not given are written NULL, so a 'complete' row
        is only ever certified when the caller says so explicitly."""
        if status not in ("complete", "partial", "unknown", "unavailable"):
            raise ValueError(f"invalid session coverage status: {status!r}")
        prov = dict(provenance or {})
        unknown = set(prov) - set(self.COVERAGE_PROVENANCE)
        if unknown:
            raise ValueError(f"unknown coverage provenance fields: {sorted(unknown)}")
        if status != "complete":
            prov["certified_by"] = prov["certified_at_utc"] = None
        pv = [prov.get(k) for k in self.COVERAGE_PROVENANCE]
        self._check_guard(kind)
        cols = ", ".join(self.COVERAGE_PROVENANCE)
        sets = ", ".join(f"{k}=excluded.{k}" for k in self.COVERAGE_PROVENANCE)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_coverage (dataset, ticker, session_date, status, covered_from_utc, "
                f"covered_through_utc, pages_walked, rows_seen, last_error, retrieved_at, {cols}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {', '.join('?' * len(pv))}) "
                "ON CONFLICT(dataset, ticker, session_date) DO UPDATE SET status=excluded.status, "
                "covered_from_utc=excluded.covered_from_utc, covered_through_utc=excluded.covered_through_utc, "
                "pages_walked=session_coverage.pages_walked + excluded.pages_walked, "
                "rows_seen=excluded.rows_seen, last_error=excluded.last_error, retrieved_at=excluded.retrieved_at, "
                + sets,
                (dataset, ticker, session_date, status, covered_from_utc, covered_through_utc,
                 pages_walked, rows_seen, last_error, utcnow_iso(), *pv))
            conn.commit()

    def record_coverage_reconciliation(self, dataset: str, ticker: str, session_date: str, trigger: str,
                                       rows_before: Optional[int], rows_after: Optional[int], outcome: str,
                                       certified_by: Optional[str], detail: Optional[dict] = None,
                                       kind: str = ON_DEMAND) -> None:
        self._check_guard(kind)
        new_rows = (rows_after - rows_before) if rows_before is not None and rows_after is not None else None
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO coverage_reconciliations (created_at, dataset, ticker, session_date, trigger, "
                "rows_before, rows_after, new_rows, outcome, certified_by, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utcnow_iso(), dataset, ticker, session_date, trigger, rows_before, rows_after, new_rows, outcome,
                 certified_by, json.dumps(detail or {}, sort_keys=True, default=str)))
            conn.commit()

    def list_coverage_reconciliations(self, dataset: Optional[str] = None, ticker: Optional[str] = None,
                                      limit: int = 200) -> list:
        clauses, args = [], []
        for col, val in (("dataset", dataset), ("ticker", ticker)):
            if val:
                clauses.append(f"{col} = ?")
                args.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, created_at, dataset, ticker, session_date, trigger, rows_before, rows_after, new_rows, "
                f"outcome, certified_by, detail_json FROM coverage_reconciliations {where} ORDER BY id DESC LIMIT ?",
                (*args, max(1, min(int(limit), 1000)))).fetchall()
        keys = ("id", "created_at", "dataset", "ticker", "session_date", "trigger", "rows_before", "rows_after",
                "new_rows", "outcome", "certified_by")
        return [{**dict(zip(keys, r[:11])), "detail": json.loads(r[11] or "{}")} for r in rows]

    # --- collector diagnostics (migration 10; Phase 9) -------------------------
    DIAGNOSTICS_MAX_ROWS = 5000

    def record_collection_diagnostic(self, diag: dict, kind: str = ON_DEMAND) -> bool:
        """Stores one sanitized rejected-page diagnostic (session_collection
        builds it: bounds, timestamps and counts only). Bounded ring buffer
        (PE-P8): the table keeps the newest DIAGNOSTICS_MAX_ROWS rows; the oldest
        are dropped in the same transaction. Returns whether the row was stored."""
        self._check_guard(kind)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM collection_diagnostics WHERE id IN (SELECT id FROM collection_diagnostics "
                         "ORDER BY id DESC LIMIT -1 OFFSET ?)", (self.DIAGNOSTICS_MAX_ROWS - 1,))
            conn.execute(
                "INSERT INTO collection_diagnostics (created_at, dataset, ticker, session_date, reason, classification, "
                "action, walk_id, page_in_window, attempt, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utcnow_iso(), diag["dataset"], diag["ticker"], diag["session_date"], diag["reason"],
                 diag.get("classification"), diag.get("action"), diag.get("walk_id"), diag.get("page_in_window"),
                 diag.get("attempt"), json.dumps(diag, sort_keys=True, default=str)))
            conn.commit()
        return True

    def list_collection_diagnostics(self, dataset: Optional[str] = None, ticker: Optional[str] = None,
                                    session_date: Optional[str] = None, limit: int = 200) -> list:
        clauses, args = [], []
        for col, val in (("dataset", dataset), ("ticker", ticker), ("session_date", session_date)):
            if val:
                clauses.append(f"{col} = ?")
                args.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(f"SELECT id, created_at, detail_json FROM collection_diagnostics {where} "
                                "ORDER BY id DESC LIMIT ?", (*args, max(1, min(int(limit), 1000)))).fetchall()
        return [{"id": r[0], "created_at": r[1], **json.loads(r[2])} for r in rows]

    def list_session_coverage(self, dataset: str, ticker: str) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(self._COVERAGE_COLS)} FROM session_coverage WHERE dataset = ? AND ticker = ? "
                "ORDER BY session_date", (dataset, ticker)).fetchall()
        return [self._coverage_row(dataset, ticker, r) for r in rows]

    @staticmethod
    def _window_date_range(start_utc: datetime, end_utc: datetime) -> tuple:
        return ((start_utc - timedelta(days=1)).date().isoformat(),
                (end_utc + timedelta(days=1)).date().isoformat())

    def _iter_window(self, dataset: str, ticker: str, start_utc: datetime, end_utc: datetime,
                     on_duplicate=None):
        """Yields (ts, key, data) for the deduplicated, non-canceled observations
        with start <= event_time <= end, in the catalog's event_time order --
        THE single definition of which raw rows count (observations_between and
        the completed-session curves both use it). `on_duplicate(ts, key, data)`
        sees each suppressed duplicate."""
        table = {"darkpool": "darkpool_observations", "options_flow": "options_flow_observations"}[dataset]
        lo, hi = self._window_date_range(start_utc, end_utc)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT event_time, raw_json FROM {table} WHERE ticker = ? AND event_time >= ? AND event_time < ? "
                f"ORDER BY event_time", (ticker, lo, hi)).fetchall()
        seen = set()
        parsed = []
        for event_time, raw_json in rows:
            ts = _parse_iso_dt(str(event_time).replace("Z", "+00:00"))
            if ts is None or ts < start_utc or ts > end_utc:
                continue
            try:
                data = json.loads(raw_json) if raw_json else {}
            except (json.JSONDecodeError, TypeError):
                continue
            parsed.append((ts, data))
        # a print reported canceled in ANY stored version is excluded in every version (C-F3)
        canceled = ({darkpool_print_key(ticker, d) for _t, d in parsed if _is_canceled(d)}
                    if dataset == "darkpool" else set())
        for ts, data in parsed:
            if dataset == "darkpool":
                if _is_canceled(data):
                    continue
                key = darkpool_print_key(ticker, data)
                if key in canceled:
                    continue
            else:
                key = ("i", data.get("id")) if data.get("id") is not None else (
                    "f", data.get("created_at"), _num(data.get("total_premium")), data.get("type"))
            if key in seen:
                if on_duplicate is not None:
                    on_duplicate(ts, key, data)
                continue
            seen.add(key)
            yield ts, key, data

    def observations_between(self, dataset: str, ticker: str, start_utc: datetime, end_utc: datetime) -> list:
        """Deduplicated observation dicts for `ticker` with start <= event_time
        <= end. Dark-pool rows are deduplicated by the verified print key
        (darkpool_print_key -- also covers legacy rows stored under older
        source_id schemes) and CANCELED prints are excluded -- a canceled
        print is not market activity."""
        out = []
        for ts, _key, data in self._iter_window(dataset, ticker, start_utc, end_utc):
            data["_event_time_utc"] = ts.isoformat()
            out.append(data)
        return out

    # --- completed-session value curves (derived cache; raw rows stay authoritative)
    def observation_window_signature(self, dataset: str, ticker: str, start_utc: datetime,
                                     end_utc: datetime) -> tuple:
        """(row count, max id) of every raw row the window query could read.
        Observation tables are append-only, so any late, corrected or canceled
        print for that window changes this signature."""
        table = {"darkpool": "darkpool_observations", "options_flow": "options_flow_observations"}[dataset]
        lo, hi = self._window_date_range(start_utc, end_utc)
        with self._conn() as conn:
            row = conn.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {table} "
                               f"WHERE ticker = ? AND event_time >= ? AND event_time < ?", (ticker, lo, hi)).fetchone()
        return int(row[0]), int(row[1])

    def get_session_curve(self, dataset: str, ticker: str, session_date: str, value_key: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT curve_version, window_start_utc, window_end_utc, src_count, src_max_id, n, monotonic, "
                "ts_us, vals, sealed, archive_ref FROM session_value_curves WHERE dataset = ? AND ticker = ? "
                "AND session_date = ? AND value_key = ?", (dataset, ticker, session_date, value_key)).fetchone()
        if not row:
            return None
        return {"curve_version": row[0], "window_start_utc": row[1], "window_end_utc": row[2],
                "src_count": row[3], "src_max_id": row[4], "n": row[5], "monotonic": bool(row[6]),
                "ts_us": row[7], "vals": row[8], "sealed": bool(row[9]), "archive_ref": row[10]}

    def put_session_curve(self, dataset: str, ticker: str, session_date: str, value_key: str, curve: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_value_curves (dataset, ticker, session_date, value_key, curve_version, "
                "window_start_utc, window_end_utc, src_count, src_max_id, n, monotonic, ts_us, vals, built_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dataset, ticker, session_date, value_key) DO UPDATE SET "
                "curve_version=excluded.curve_version, window_start_utc=excluded.window_start_utc, "
                "window_end_utc=excluded.window_end_utc, src_count=excluded.src_count, "
                "src_max_id=excluded.src_max_id, n=excluded.n, monotonic=excluded.monotonic, "
                "ts_us=excluded.ts_us, vals=excluded.vals, built_at=excluded.built_at "
                "WHERE session_value_curves.sealed = 0",                # never overwrite a retained aggregate
                (dataset, ticker, session_date, value_key, curve["curve_version"], curve["window_start_utc"],
                 curve["window_end_utc"], curve["src_count"], curve["src_max_id"], curve["n"],
                 int(curve["monotonic"]), curve["ts_us"], curve["vals"], utcnow_iso()))
            conn.commit()

    def get_last_successful_collection_at(self, ticker: str) -> Optional[str]:
        """collected_at of the newest evaluation whose data collection was
        explicitly complete. Evaluations recorded before collection status
        existed carry no such flag and are NOT counted (their completeness
        is unknown -- the pre-remediation adapter turned failures into
        empty lists)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(json_extract(raw_json, '$.collected_at')) FROM signal_evaluations "
                "WHERE ticker = ? AND json_extract(raw_json, '$.collection_complete') = 1",
                (ticker,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_latest_signal_evaluation(self, ticker: str,
                                     engine_rule_version: Optional[str] = ENGINE_RULE_VERSION) -> Optional[dict]:
        """Latest evaluation written by `engine_rule_version` (default: this build's
        engine; None = any version, for audit reads only)."""
        version_sql, params = ("", (ticker,)) if engine_rule_version is None else (
            " AND engine_rule_version = ?", (ticker, engine_rule_version))
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, ticker, event_time, retrieved_at, state, confidence, "
                "engine_rule_version, raw_json FROM signal_evaluations "
                "WHERE ticker = ?" + version_sql + " ORDER BY retrieved_at DESC, id DESC LIMIT 1",
                params,
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "ticker": row[1], "event_time": row[2], "retrieved_at": row[3],
            "state": row[4], "confidence": row[5], "engine_rule_version": row[6],
            "raw_json": json.loads(row[7]) if row[7] else None,
        }

    def count_superseded_signal_evaluations(self, ticker: str) -> int:
        """Evaluations of `ticker` written by an earlier engine version (kept as history,
        never served as a current read)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM signal_evaluations WHERE ticker = ? AND engine_rule_version != ?",
                (ticker, ENGINE_RULE_VERSION),
            ).fetchone()
        return int(row[0]) if row else 0

    # --- freshness decision (used by backfill/pagination jobs ONLY --
    # never by the on-demand Radar/Ticker-Analysis/Discovery routes,
    # which must always reflect a fresh live fetch; see module docstring) --

    def most_recent_retrieved_at(
        self, table: str, ticker: str, params_fingerprint: Optional[str] = None,
    ) -> Optional[str]:
        """Most recent `retrieved_at` already cataloged for this
        ticker (optionally further scoped by a caller-computed
        `params_fingerprint`, e.g. a pagination job's window) -- or None
        if nothing is cataloged yet. Used only by `should_refetch` /
        backfill-style callers deciding whether to spend a NEW live
        request; the on-demand request/response path never consults
        this."""
        query = f"SELECT MAX(retrieved_at) FROM {table} WHERE ticker = ?"  # nosec - table is from a fixed internal allowlist, never user input
        if table not in _CATALOG_TABLES:
            raise ValueError(f"unknown catalog table: {table!r}")
        with self._conn() as conn:
            row = conn.execute(query, (ticker,)).fetchone()
            return row[0] if row and row[0] else None


_CATALOG_TABLES = {
    "options_flow_observations", "darkpool_observations", "price_bars",
    "discovery_snapshots", "discovery_candidates", "signal_evaluations",
    "signal_transitions", "ai_investigations",
}


def should_refetch(most_recent_retrieved_at: Optional[str], freshness_window_s: float) -> bool:
    """Pure decision helper for backfill/pagination-style callers (NOT the
    on-demand live routes, see module docstring): True when there is no
    cataloged observation yet, or the most recent one is older than
    `freshness_window_s`. Never raises on a malformed timestamp -- treats
    it as "no usable cache", i.e. refetch."""
    if not most_recent_retrieved_at:
        return True
    try:
        ts = datetime.fromisoformat(most_recent_retrieved_at)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= freshness_window_s
