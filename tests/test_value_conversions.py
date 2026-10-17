import datetime

import pytest
from elementpath import datatypes as ep

from pyxsd.xsd_data_types import (
    Base64Binary,
    Date,
    DateTime,
    DayTimeDuration,
    Duration,
    GDay,
    GMonth,
    GMonthDay,
    GYear,
    GYearMonth,
    HexBinary,
    QName,
    Time,
    YearMonthDuration,
    qname_context,
)


class TestDurationToTimedelta:
    def test_days_and_time(self):
        assert Duration("P1DT2H30M").to_timedelta() == datetime.timedelta(
            days=1, hours=2, minutes=30
        )

    def test_negative(self):
        assert Duration("-P1D").to_timedelta() == datetime.timedelta(days=-1)

    def test_fractional_seconds(self):
        assert Duration("PT0.5S").to_timedelta() == datetime.timedelta(milliseconds=500)

    def test_years_raise(self):
        with pytest.raises(ValueError, match="P1Y3D"):
            Duration("P1Y3D").to_timedelta()

    def test_months_raise(self):
        with pytest.raises(ValueError, match="P1M"):
            Duration("P1M").to_timedelta()

    def test_day_time_duration(self):
        assert DayTimeDuration("P2D").to_timedelta() == datetime.timedelta(days=2)

    def test_strict_rejects_sub_microsecond_fraction(self):
        with pytest.raises(ValueError, match="sub-microsecond"):
            Duration("PT0.0000001S").to_timedelta(strict=True)

    def test_default_truncates_sub_microsecond_fraction(self):
        assert Duration("PT0.0000001S").to_timedelta() == datetime.timedelta(0)


class TestYearMonthDuration:
    def test_to_months(self):
        assert YearMonthDuration("P1Y2M").to_months() == 14

    def test_to_months_negative(self):
        assert YearMonthDuration("-P3M").to_months() == -3

    def test_to_timedelta_raises(self):
        with pytest.raises(ValueError):
            YearMonthDuration("P1Y").to_timedelta()


class TestDurationToXsdValue:
    def test_full_duration_preserves_months(self):
        value = Duration("P1Y2M3DT4H5M6.5S").to_xsd_value()
        assert isinstance(value, ep.Duration)
        assert (value.months, value.seconds) == (14, 273906.5)

    def test_year_month_duration(self):
        value = YearMonthDuration("P1Y2M").to_xsd_value()
        assert isinstance(value, ep.YearMonthDuration)
        assert str(value) == "P1Y2M"

    def test_day_time_duration(self):
        value = DayTimeDuration("P2DT3H").to_xsd_value()
        assert isinstance(value, ep.DayTimeDuration)
        assert str(value) == "P2DT3H"


class TestHexBinaryToBytes:
    def test_bytes(self):
        assert HexBinary("FF00ab").to_bytes() == b"\xff\x00\xab"


class TestBase64BinaryToBytes:
    def test_bytes(self):
        assert Base64Binary("QQ==").to_bytes() == b"A"

    def test_whitespace_is_ignored(self):
        assert Base64Binary("Q Q = =").to_bytes() == b"A"


class TestQNameToExpandedName:
    def test_resolved(self):
        with qname_context({"p": "urn:example"}):
            assert QName("p:local").to_expanded_name() == "{urn:example}local"

    def test_resolved_without_namespace(self):
        with qname_context({}):
            assert QName("local").to_expanded_name() == "local"

    def test_unresolved_defaults_to_lexical(self):
        assert QName("p:local").to_expanded_name() == "p:local"

    def test_unresolved_strict_raises(self):
        with pytest.raises(ValueError, match="p:local"):
            QName("p:local").to_expanded_name(strict=True)


class TestGTypesToGregorian:
    def test_gyear(self):
        assert GYear("2006").to_gregorian() == ep.GregorianYear(2006)

    def test_gyearmonth(self):
        assert GYearMonth("2006-08").to_gregorian() == ep.GregorianYearMonth(2006, 8)

    def test_gmonth(self):
        assert GMonth("--08").to_gregorian() == ep.GregorianMonth(8)

    def test_gmonthday(self):
        assert GMonthDay("--08-30").to_gregorian() == ep.GregorianMonthDay(8, 30)

    def test_gday(self):
        assert GDay("---30").to_gregorian() == ep.GregorianDay(30)

    def test_timezone_is_preserved(self):
        value = GMonthDay("--08-30+05:30").to_gregorian()
        assert value.tzinfo.utcoffset(None) == datetime.timedelta(hours=5, minutes=30)

    def test_year_zero_maps_to_elementpath_numbering(self):
        # XSD 1.1 lexical year 0000 is 1 BCE; elementpath numbers it -1.
        assert GYear("0000").to_gregorian() == ep.GregorianYear(-1)

    def test_extended_year(self):
        assert GYear("12006").to_gregorian() == ep.GregorianYear(12006)


class TestTemporalToGregorian:
    def test_date_extended_year(self):
        assert Date("12006-08-30").to_gregorian() == ep.Date(12006, 8, 30)

    def test_date_year_zero(self):
        # Same no-year-zero numbering as the g* types.
        assert Date("0000-01-01").to_gregorian() == ep.Date(-1, 1, 1)

    def test_datetime_bce(self):
        assert DateTime("-0005-03-01T00:00:00").to_gregorian() == ep.DateTime(-6, 3, 1, 0, 0, 0)

    def test_time(self):
        assert Time("14:30:00").to_gregorian() == ep.Time.fromstring("14:30:00")
