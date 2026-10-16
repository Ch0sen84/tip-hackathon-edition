"""Phase C: NYSE trading-calendar correctness tests. All dates here are
real, documented NYSE holidays/early-closes/DST-transition dates -- no
network access, pure date arithmetic."""
from __future__ import annotations

import datetime as dt

from app.core import trading_calendar as tc


class TestRealDocumentedHolidays:
    def test_new_years_day_2026(self):
        assert not tc.is_trading_day(dt.date(2026, 1, 1))

    def test_mlk_day_2026_is_third_monday_of_january(self):
        # 2026: Jan 19 is the 3rd Monday of January.
        d = dt.date(2026, 1, 19)
        assert d.weekday() == 0
        assert not tc.is_trading_day(d)

    def test_good_friday_2026(self):
        # Easter Sunday 2026 is April 5 -> Good Friday is April 3.
        assert not tc.is_trading_day(dt.date(2026, 4, 3))
        # The Thursday and the following Monday around it ARE trading days.
        assert tc.is_trading_day(dt.date(2026, 4, 2))
        assert tc.is_trading_day(dt.date(2026, 4, 6))

    def test_juneteenth_observed_from_2022_onward_not_before(self):
        assert not tc.is_trading_day(dt.date(2022, 6, 20))  # June 19 2022 was a Sunday -> observed Monday 6/20
        # Before NYSE added Juneteenth (2021), June 19 (a Saturday that
        # year) must NOT be treated as a market holiday at all.
        holidays_2021 = tc.nyse_holidays_for_year(2021)
        assert "Juneteenth National Independence Day" not in holidays_2021.values()

    def test_thanksgiving_2026_is_fourth_thursday_of_november(self):
        d = dt.date(2026, 11, 26)
        assert d.weekday() == 3
        assert not tc.is_trading_day(d)

    def test_christmas_day_2026(self):
        assert not tc.is_trading_day(dt.date(2026, 12, 25))


class TestObservedWeekendRule:
    def test_holiday_falling_on_saturday_observed_preceding_friday(self):
        # Independence Day 2026 (July 4) is a Saturday -> observed Friday
        # July 3, which must be the full closure (not an early close).
        assert dt.date(2026, 7, 4).weekday() == 5
        assert not tc.is_trading_day(dt.date(2026, 7, 3))
        info = tc.session_info(dt.date(2026, 7, 3))
        assert info.holiday_name == "Independence Day"
        assert not info.is_early_close
        # The actual Saturday itself is irrelevant (market's never open
        # on a Saturday anyway) but Monday July 6 must be a normal day.
        assert tc.is_trading_day(dt.date(2026, 7, 6))

    def test_holiday_falling_on_sunday_observed_following_monday(self):
        # Independence Day 2021 (July 4) was a Sunday -> observed Monday July 5.
        assert dt.date(2021, 7, 4).weekday() == 6
        assert not tc.is_trading_day(dt.date(2021, 7, 5))
        # The preceding Friday (July 2) is a completely normal trading day.
        assert tc.is_trading_day(dt.date(2021, 7, 2))


class TestEarlyCloses:
    def test_day_after_thanksgiving_is_one_pm_close(self):
        d = dt.date(2026, 11, 27)
        assert tc.is_trading_day(d)
        info = tc.session_info(d)
        assert info.is_early_close is True
        assert info.session_close_et.time() == dt.time(13, 0)

    def test_christmas_eve_early_close_when_a_trading_day(self):
        d = dt.date(2026, 12, 24)
        info = tc.session_info(d)
        assert info.is_trading_day is True
        assert info.is_early_close is True

    def test_christmas_eve_not_early_close_when_it_is_the_observed_holiday_itself(self):
        # 2022: Christmas Day (Dec 25) is a Sunday -> observed Monday Dec
        # 26. Dec 24 (Saturday) is irrelevant; use a year where Dec 25 is
        # a Saturday instead, which moves the observed holiday itself
        # onto Dec 24 -- that day must be a full closure, not an early
        # close. 2027: Dec 25 is a Saturday.
        d = dt.date(2027, 12, 24)
        assert dt.date(2027, 12, 25).weekday() == 5
        info = tc.session_info(d)
        assert info.is_trading_day is False
        assert info.is_early_close is False
        assert info.holiday_name == "Christmas Day"

    def test_july_3rd_early_close_when_july_4th_is_a_midweek_weekday(self):
        # 2024: July 4 is a Thursday -> July 3rd (Wednesday) is an early close.
        assert dt.date(2024, 7, 4).weekday() == 3
        info = tc.session_info(dt.date(2024, 7, 3))
        assert info.is_trading_day is True
        assert info.is_early_close is True

    def test_regular_session_boundaries_on_an_ordinary_day(self):
        info = tc.session_info(dt.date(2026, 3, 12))  # an ordinary Thursday
        assert info.is_trading_day is True
        assert info.is_early_close is False
        assert info.session_open_et.time() == dt.time(9, 30)
        assert info.session_close_et.time() == dt.time(16, 0)


class TestDstBoundaryWeek:
    """2024 spring-forward: clocks jump 2am -> 3am EST->EDT on Sunday
    March 10. session_start_utc must reflect the REAL ET-local 9:30 open
    on both sides of that transition -- a fixed UTC offset would get one
    side wrong."""

    def test_friday_before_spring_forward_is_still_est(self):
        utc_open = tc.session_start_utc(dt.date(2024, 3, 8))
        # EST = UTC-5, so 9:30 ET == 14:30 UTC.
        assert utc_open.hour == 14 and utc_open.minute == 30

    def test_monday_after_spring_forward_is_now_edt(self):
        utc_open = tc.session_start_utc(dt.date(2024, 3, 11))
        # EDT = UTC-4, so 9:30 ET == 13:30 UTC -- one hour earlier in UTC
        # terms than the Friday before, purely from the DST shift, with
        # no change to this module's code or a hardcoded offset table.
        assert utc_open.hour == 13 and utc_open.minute == 30

    def test_fall_back_week_2024(self):
        # 2024 fall-back: Sunday Nov 3. Friday Nov 1 is EDT (UTC-4),
        # Monday Nov 4 is back to EST (UTC-5).
        friday_open = tc.session_start_utc(dt.date(2024, 11, 1))
        monday_open = tc.session_start_utc(dt.date(2024, 11, 4))
        assert friday_open.hour == 13 and friday_open.minute == 30
        assert monday_open.hour == 14 and monday_open.minute == 30


class TestBackwardCompatibleWeekendOnlyHelpers:
    def test_is_weekday_does_not_know_about_holidays(self):
        # is_weekday is the narrower, Phase-B-compatible check -- a
        # holiday that falls on a weekday is still "a weekday" by this
        # function's definition; only is_trading_day/session_info know
        # about holidays. This pins down that distinction so a future
        # change doesn't accidentally conflate the two.
        assert tc.is_weekday(dt.date(2026, 12, 25)) is True
        assert tc.is_trading_day(dt.date(2026, 12, 25)) is False

    def test_session_start_utc_returns_none_on_a_holiday(self):
        # This is the exact Phase B limitation flagged in the old
        # module's docstring ("a holiday is treated as a normal weekday
        # here") -- now closed.
        assert tc.session_start_utc(dt.date(2026, 12, 25)) is None

    def test_session_start_utc_returns_none_on_a_weekend(self):
        assert tc.session_start_utc(dt.date(2026, 10, 17)) is None  # a Saturday

    def test_session_end_utc_reflects_early_close(self):
        early_close_day = dt.date(2026, 11, 27)
        early_close_end = tc.session_end_utc(early_close_day)
        assert early_close_end is not None
        assert early_close_end.astimezone(tc.NY_TZ).time() == dt.time(13, 0)
        # Same ET calendar day's REGULAR close would have been 4pm ET --
        # 3 hours later than the actual 1pm early close.
        regular_close_same_day = dt.datetime.combine(early_close_day, dt.time(16, 0), tzinfo=tc.NY_TZ)
        assert early_close_end < regular_close_same_day.astimezone(dt.timezone.utc)
