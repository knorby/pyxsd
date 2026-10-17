import datetime

import pytest

from pyxsd.xsd_data_types import Date, DateTime, Time


class TestDateToDate:
    def test_plain_date(self):
        assert Date("2006-08-30").to_date() == datetime.date(2006, 8, 30)

    def test_timezone_is_discarded(self):
        assert Date("2006-08-30+05:30").to_date() == datetime.date(2006, 8, 30)

    def test_year_zero_raises(self):
        with pytest.raises(ValueError, match="0000"):
            Date("0000-01-01").to_date()

    def test_extended_year_raises(self):
        with pytest.raises(ValueError):
            Date("12006-08-30").to_date()


class TestDateTimeToDatetime:
    def test_naive(self):
        assert DateTime("2006-08-30T14:30:00").to_datetime() == datetime.datetime(
            2006, 8, 30, 14, 30
        )

    def test_zoned(self):
        value = DateTime("2006-08-30T14:30:00+05:30").to_datetime()
        assert value.utcoffset() == datetime.timedelta(hours=5, minutes=30)

    def test_zulu(self):
        assert DateTime("2006-08-30T14:30:00Z").to_datetime().utcoffset() == datetime.timedelta(0)

    def test_fractional_seconds(self):
        assert DateTime("2006-08-30T14:30:00.5").to_datetime().microsecond == 500000

    def test_end_of_day_rolls_to_next_day(self):
        assert DateTime("2006-08-30T24:00:00").to_datetime() == datetime.datetime(2006, 8, 31)

    def test_end_of_day_rollover_past_the_range_raises_value_error(self):
        # The lexical form is in range, but the rollover from the last
        # representable day has no datetime to land on.
        with pytest.raises(ValueError, match="9999-12-31T24:00:00"):
            DateTime("9999-12-31T24:00:00").to_datetime()


class TestTimeToTime:
    def test_naive(self):
        assert Time("14:30:00").to_time() == datetime.time(14, 30)

    def test_zoned(self):
        assert Time("14:30:00-02:00").to_time().utcoffset() == -datetime.timedelta(hours=2)

    def test_end_of_day_raises(self):
        with pytest.raises(ValueError, match="24:00:00"):
            Time("24:00:00").to_time()


class TestDateToDateStrict:
    def test_default_drops_timezone(self):
        assert Date("2006-08-30+05:30").to_date() == datetime.date(2006, 8, 30)

    def test_strict_rejects_timezone(self):
        with pytest.raises(ValueError, match="2006-08-30"):
            Date("2006-08-30+05:30").to_date(strict=True)

    def test_strict_accepts_naive(self):
        assert Date("2006-08-30").to_date(strict=True) == datetime.date(2006, 8, 30)


class TestStrictFractions:
    def test_default_truncates_sub_microseconds(self):
        assert DateTime("2006-08-30T14:30:00.1234567").to_datetime().microsecond == 123456

    def test_strict_rejects_sub_microsecond_fraction(self):
        with pytest.raises(ValueError, match="sub-microsecond"):
            DateTime("2006-08-30T14:30:00.1234567").to_datetime(strict=True)

    def test_strict_allows_zero_padded_fraction(self):
        value = DateTime("2006-08-30T14:30:00.5000000").to_datetime(strict=True)
        assert value.microsecond == 500000

    def test_strict_time_rejects_sub_microsecond_fraction(self):
        with pytest.raises(ValueError, match="sub-microsecond"):
            Time("14:30:00.1234567").to_time(strict=True)
