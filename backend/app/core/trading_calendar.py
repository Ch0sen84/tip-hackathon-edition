"""Phase C: a genuine NYSE trading calendar, replacing Phase B's DST-aware-
but-holiday-blind session helper.

LIBRARY CHECK (done before hand-rolling anything, per Chad's instruction to
prefer a maintained dependency over a hand-rolled table when one is already
present): `backend/requirements.txt` and `mcp_isolated/requirements.txt`
were checked -- neither this repo nor any other part of it already depends
on `pandas_market_calendars`, `pandas`, or any other maintained market-
calendar library. Adding one of those just for this module would be a new,
fairly heavy dependency (pandas_market_calendars pulls in pandas) for what
is, in the end, a well-documented and genuinely static set of federal/NYSE
holiday rules that do not change year to year. Per the "don't add a new
heavy dependency just for this" instruction, this module hand-rolls the
holiday table instead -- every rule below cites the real NYSE convention it
implements, and the whole thing is <250 lines with no new imports beyond
the stdlib `datetime`/`zoneinfo` already used by the Phase B module this
replaces.

WHAT THIS MODULE COVERS (see `nyse_holidays_for_year` / `nyse_early_closes_for_year`):
  * Full annual NYSE holiday list: New Year's Day, Martin Luther King Jr.
    Day, Washington's Birthday (Presidents' Day), Good Friday, Memorial
    Day, Juneteenth National Independence Day (observed by NYSE starting
    2022), Independence Day, Labor Day, Thanksgiving Day, Christmas Day.
  * NYSE's weekend "observed" rule: a holiday that falls on Saturday is
    observed the preceding Friday; one that falls on Sunday is observed
    the following Monday (applied uniformly below -- this is the actual
    NYSE convention, not a holiday-specific special case).
  * Early closes (1:00pm ET regular-session close instead of 4:00pm):
    the day after Thanksgiving (always a Friday), Christmas Eve (Dec 24)
    when it is itself a trading day, and July 3rd in the specific years
    where NYSE's documented practice applies (July 4th observed on a
    weekday Tue-Fri, making July 3rd the preceding trading day) -- see
    `nyse_early_closes_for_year`'s docstring for the exact rule and its
    known limitation (NYSE occasionally announces one-off extra early
    closes, e.g. around a papal visit or a 9/11 anniversary, that no
    static formula can predict; none of those are modeled here).

Easter Sunday (needed for Good Friday) is computed with the standard
anonymous Gregorian-calendar algorithm (Meeus/Jones/Butcher) -- a closed-
form arithmetic formula, not a lookup table, so it is exact for any year
without needing its own data file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_cls, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
REGULAR_SESSION_START = time(9, 30)
REGULAR_SESSION_END = time(16, 0)
EARLY_SESSION_END = time(13, 0)

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)


# --- date-arithmetic helpers (no stdlib/library shortcut exists for these) --

def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date_cls:
    """The n-th occurrence (1-indexed) of `weekday` in `month`/`year`."""
    first = date_cls(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date_cls:
    if month == 12:
        last_day = date_cls(year, 12, 31)
    else:
        last_day = date_cls(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date_cls) -> date_cls:
    """NYSE's weekend-observed rule: Saturday -> preceding Friday,
    Sunday -> following Monday. Applied to every holiday below -- this is
    the real exchange convention, not a per-holiday special case."""
    if d.weekday() == SATURDAY:
        return d - timedelta(days=1)
    if d.weekday() == SUNDAY:
        return d + timedelta(days=1)
    return d


def _easter_sunday(year: int) -> date_cls:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) -- exact closed-
    form Easter Sunday for any Gregorian-calendar year, needed to derive
    Good Friday (Easter Sunday minus 2 days)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date_cls(year, month, day)


# --- the actual holiday/early-close rule tables ------------------------------

@lru_cache(maxsize=None)
def nyse_holidays_for_year(year: int) -> dict:
    """Returns {date: holiday_name} for every NYSE full-market-closure
    holiday observed in `year`. Juneteenth is only included from 2022
    onward (the year NYSE actually added it to the holiday calendar --
    including it for earlier years would be historically wrong)."""
    holidays = {
        _observed(date_cls(year, 1, 1)): "New Year's Day",
        _nth_weekday_of_month(year, 1, MONDAY, 3): "Martin Luther King Jr. Day",
        _nth_weekday_of_month(year, 2, MONDAY, 3): "Washington's Birthday",
        _easter_sunday(year) - timedelta(days=2): "Good Friday",
        _last_weekday_of_month(year, 5, MONDAY): "Memorial Day",
        _observed(date_cls(year, 7, 4)): "Independence Day",
        _nth_weekday_of_month(year, 9, MONDAY, 1): "Labor Day",
        _nth_weekday_of_month(year, 11, THURSDAY, 4): "Thanksgiving Day",
        _observed(date_cls(year, 12, 25)): "Christmas Day",
    }
    if year >= 2022:
        holidays[_observed(date_cls(year, 6, 19))] = "Juneteenth National Independence Day"
    return holidays


@lru_cache(maxsize=None)
def nyse_early_closes_for_year(year: int) -> dict:
    """Returns {date: "1:00pm ET"} for every NYSE 1:00pm-ET early-close
    day in `year` this module can derive formulaically:

      * Day after Thanksgiving -- always the Friday immediately following
        Thanksgiving Day (4th Thursday of November); NYSE has closed early
        on this day every year for decades.
      * Christmas Eve (Dec 24) -- early close ONLY when Dec 24 is itself a
        real trading day (not a weekend, and not the observed Christmas
        holiday itself, which happens when Christmas Day falls on a
        Saturday and gets observed on the preceding Friday Dec 24).
      * July 3rd -- NYSE's documented practice is an early close on July
        3rd specifically when July 4th (actual calendar date, not the
        observed date) falls Tuesday-Friday, making July 3rd the trading
        day immediately preceding the Independence Day holiday. When
        July 4th falls on Saturday, the observed holiday moves to Friday
        July 3rd itself (a full closure, not an early close -- excluded
        here). When July 4th falls on Sunday, the observed holiday moves
        to Monday July 5th and July 3rd (Friday) is a normal full session
        per NYSE's own historical calendar -- also excluded here.

    KNOWN LIMITATION (flagged, not silently papered over): NYSE
    occasionally announces additional one-off early closes (e.g. around a
    national day of mourning) that follow no predictable formula. None of
    those are -- or can be -- modeled by a static function like this one;
    treat this table as "the regular, calendar-driven early closes" only.
    """
    holidays = nyse_holidays_for_year(year)
    early_closes = {}

    thanksgiving = _nth_weekday_of_month(year, 11, THURSDAY, 4)
    early_closes[thanksgiving + timedelta(days=1)] = "1:00pm ET (day after Thanksgiving)"

    christmas_eve = date_cls(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
        early_closes[christmas_eve] = "1:00pm ET (Christmas Eve)"

    july_4_actual = date_cls(year, 7, 4)
    if july_4_actual.weekday() in (TUESDAY, WEDNESDAY, THURSDAY, FRIDAY):
        july_3 = date_cls(year, 7, 3)
        if july_3.weekday() < 5 and july_3 not in holidays:
            early_closes[july_3] = "1:00pm ET (July 3rd)"

    return early_closes


@dataclass(frozen=True)
class SessionInfo:
    trading_date: date_cls
    is_trading_day: bool
    session_open_et: Optional[datetime]
    session_close_et: Optional[datetime]
    is_early_close: bool
    holiday_name: Optional[str] = None


# Unscheduled full-day NYSE closures (national days of mourning, weather). No formula predicts these;
# they are listed explicitly, and TIP_EXTRA_MARKET_CLOSURES (comma-separated ISO dates) adds future ones.
SPECIAL_CLOSURES = {
    date_cls(2004, 6, 11): "National Day of Mourning (President Reagan)",
    date_cls(2007, 1, 2): "National Day of Mourning (President Ford)",
    date_cls(2012, 10, 29): "Hurricane Sandy",
    date_cls(2012, 10, 30): "Hurricane Sandy",
    date_cls(2018, 12, 5): "National Day of Mourning (President G.H.W. Bush)",
    date_cls(2025, 1, 9): "National Day of Mourning (President Carter)",
}


def _extra_closures() -> dict:
    import os
    out = {}
    for part in (os.environ.get("TIP_EXTRA_MARKET_CLOSURES") or "").split(","):
        part = part.strip()
        if part:
            try:
                out[date_cls.fromisoformat(part)] = "operator-configured market closure"
            except ValueError:
                pass
    return out


def session_info(trading_date: date_cls) -> SessionInfo:
    """The single source of truth this module exposes, per Phase C's
    spec: given a calendar date, is it a real NYSE trading day, and if so
    what are its real session-open/close timestamps (ET-aware
    `datetime`s, DST handled correctly by `zoneinfo` -- never a hardcoded
    UTC offset) and is it an early-close day.

    A weekend OR a holiday (full-market-closure day, including a holiday's
    *observed* date) is NOT a trading day -- both cases are modeled, where
    Phase B's module only ever modeled the weekend case."""
    holidays = nyse_holidays_for_year(trading_date.year)
    if trading_date.weekday() >= 5:
        return SessionInfo(trading_date, False, None, None, False, None)
    if trading_date in holidays:
        return SessionInfo(trading_date, False, None, None, False, holidays[trading_date])
    special = SPECIAL_CLOSURES.get(trading_date) or _extra_closures().get(trading_date)
    if special:
        return SessionInfo(trading_date, False, None, None, False, special)

    early_closes = nyse_early_closes_for_year(trading_date.year)
    is_early = trading_date in early_closes
    close_time = EARLY_SESSION_END if is_early else REGULAR_SESSION_END

    open_dt = datetime.combine(trading_date, REGULAR_SESSION_START, tzinfo=NY_TZ)
    close_dt = datetime.combine(trading_date, close_time, tzinfo=NY_TZ)
    return SessionInfo(trading_date, True, open_dt, close_dt, is_early, None)


def is_trading_day(trading_date: date_cls) -> bool:
    return session_info(trading_date).is_trading_day


def is_early_close(trading_date: date_cls) -> bool:
    return session_info(trading_date).is_early_close


# --- backward-compatible helpers (Phase B's original interface) -------------
# dark_pool_pagination.py (and anything else written against Phase B) calls
# these two by name. They now correctly return None for a market holiday
# too, not just a weekend -- closing the exact limitation Phase B's FINAL
# REPORT flagged ("a holiday is treated as a normal weekday here").

def is_weekday(trading_date: date_cls) -> bool:
    """Monday-Friday only -- kept for backward compatibility with any
    caller that genuinely only wants the weekend check (not the fuller
    holiday-aware `is_trading_day` above)."""
    return trading_date.weekday() < 5


def session_start_utc(trading_date: date_cls) -> Optional[datetime]:
    """The real UTC instant of this trading day's session open, correctly
    DST-aware. Returns None for a weekend OR a market holiday (the Phase B
    limitation this module closes)."""
    info = session_info(trading_date)
    if not info.is_trading_day:
        return None
    return info.session_open_et.astimezone(timezone.utc)


def session_end_utc(trading_date: date_cls) -> Optional[datetime]:
    """The real UTC instant of this trading day's session close -- 1:00pm
    ET on an early-close day, 4:00pm ET otherwise. Returns None for a
    weekend or holiday."""
    info = session_info(trading_date)
    if not info.is_trading_day:
        return None
    return info.session_close_et.astimezone(timezone.utc)


# --- session attribution (signal-integrity remediation, audit B6) ----------
#
# Lifecycle history counts distinct EXCHANGE TRADING SESSIONS, never the
# server's calendar date. A scan's evidence belongs to the most recent NYSE
# session that had OPENED by the time of the scan: a Saturday scan, a
# holiday scan, or a pre-open Monday scan all re-read the previous session's
# data, so they are attributed to that previous session (and therefore can
# never create a "new" qualifying session out of stale data).

def previous_trading_day(d: date_cls) -> date_cls:
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date_cls) -> date_cls:
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def evidence_session_date(as_of_utc: datetime) -> date_cls:
    if as_of_utc.tzinfo is None:
        as_of_utc = as_of_utc.replace(tzinfo=timezone.utc)
    ny = as_of_utc.astimezone(NY_TZ)
    info = session_info(ny.date())
    if info.is_trading_day and ny >= info.session_open_et:
        return ny.date()
    return previous_trading_day(ny.date())


def trading_sessions_between(earlier: date_cls, later: date_cls) -> int:
    """Number of trading sessions strictly after `earlier` up to and
    including `later` (0 when later <= earlier)."""
    if later <= earlier:
        return 0
    n, d = 0, earlier
    while d < later:
        d = next_trading_day(d)
        if d <= later:
            n += 1
    return n
