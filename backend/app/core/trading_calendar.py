"""Phase B: minimal trading-session-boundary helper.

No equivalent existed anywhere in this repo (checked candles.py,
scan_service.py, main.py -- none of them compute a session START
TIME; candles.py's `select_regular_session_bars` only distinguishes
pre/regular/post rows AFTER UW has already tagged them `market_time`,
it never independently derives when a session begins/ends). This module
is the minimal thing dark_pool_pagination.py needs: "what UTC instant is
09:30 America/New_York on this calendar date", used ONLY to decide
whether backward pagination has genuinely reached the start of a trading
day -- never to decide anything about OHLC/signal data.

KNOWN LIMITATION (flagged, not silently papered over): this models
regular NYSE hours (09:30-16:00 America/New_York, Mon-Fri) but does NOT
maintain a real market-holiday calendar (Thanksgiving, early closes,
etc.) -- there is no such calendar anywhere else in this repo to port.
A holiday is treated as a normal weekday here, which means pagination
against a real market holiday will walk back to a 09:30 ET boundary that
the market never actually opened for that day, and classify coverage as
'complete' once it reaches that (non-existent) session start -- rather
than recognizing the whole day as a non-trading day. This is listed as
an explicit unresolved issue in the Phase B FINAL REPORT; a real holiday
calendar (e.g. `pandas_market_calendars` or a maintained static table) is
the natural follow-up, intentionally out of scope for this phase's
bounded live-validation budget.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
REGULAR_SESSION_START = time(9, 30)
REGULAR_SESSION_END = time(16, 0)


def is_weekday(trading_date: date_cls) -> bool:
    """Monday-Friday only. Does NOT exclude market holidays -- see
    module docstring."""
    return trading_date.weekday() < 5


def session_start_utc(trading_date: date_cls) -> Optional[datetime]:
    """The real UTC instant of 09:30 America/New_York on `trading_date`,
    correctly DST-aware (zoneinfo handles the EST/EDT transition itself
    -- never a fixed UTC offset). Returns None for a weekend date (not a
    real trading day at all); see module docstring for the holiday
    caveat this does NOT handle."""
    if not is_weekday(trading_date):
        return None
    local = datetime.combine(trading_date, REGULAR_SESSION_START, tzinfo=NY_TZ)
    return local.astimezone(timezone.utc)


def session_end_utc(trading_date: date_cls) -> Optional[datetime]:
    if not is_weekday(trading_date):
        return None
    local = datetime.combine(trading_date, REGULAR_SESSION_END, tzinfo=NY_TZ)
    return local.astimezone(timezone.utc)
