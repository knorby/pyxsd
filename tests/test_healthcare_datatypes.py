"""Neutral CDA values, including source precision and explicit absence."""

from decimal import Decimal
from io import StringIO

import pytest


@pytest.mark.parametrize(
    "raw,lexical,precision,zone",
    [
        (None, None, None, None),
        ("2005", "2005", "year", None),
        ("200505", "2005-05", "month", None),
        ("20050501", "2005-05-01", "day", None),
        ("201409171904-0500", "2014-09-17T19:04:00-05:00", "minute", "-05:00"),
        ("20140917190403.25+0000", "2014-09-17T19:04:03.25+00:00", "fraction", "+00:00"),
        ("2014091719", "2014-09-17T19:00:00", "hour", None),
    ],
)
def test_ts_precision_and_missing_timezone(raw, lexical, precision, zone):
    from examples.healthcare.common.cda_datatypes import datetime_value

    value = datetime_value(raw)
    assert (value.lexical, value.precision, value.timezone) == (lexical, precision, zone)


def test_invalid_timestamp_is_not_guessed():
    from examples.healthcare.common.cda_datatypes import datetime_value

    for raw in ("bad", "20051301", "20050230", "201401012500", "201401010000+1500"):
        with pytest.raises(ValueError):
            datetime_value(raw)


def test_mixins_bind_derived_types_and_exact_values():
    from examples.healthcare.common.cda_datatypes import cda_extensions

    from pyxsd import Schema

    # Small always-on schema exercises generated-class inheritance without downloads.
    schema = Schema.compile(
        StringIO("""<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:hl7-org:v3" xmlns:c="urn:hl7-org:v3">
    <xs:complexType name="II"><xs:attribute name="root"/><xs:attribute name="extension"/><xs:attribute name="nullFlavor"/><xs:attribute name="assigningAuthorityName"/></xs:complexType>
    <xs:complexType name="TS"><xs:attribute name="value"/><xs:attribute name="nullFlavor"/></xs:complexType>
    <xs:complexType name="CD"><xs:sequence><xs:element name="translation" type="c:CD" minOccurs="0" maxOccurs="unbounded"/></xs:sequence><xs:attribute name="code"/><xs:attribute name="codeSystem"/><xs:attribute name="displayName"/><xs:attribute name="nullFlavor"/></xs:complexType>
    <xs:complexType name="CE"><xs:complexContent><xs:extension base="c:CD"/></xs:complexContent></xs:complexType>
    <xs:complexType name="PQ"><xs:attribute name="value" type="xs:decimal"/><xs:attribute name="unit"/><xs:attribute name="nullFlavor"/></xs:complexType>
    </xs:schema>"""),
        extensions=cda_extensions(),
    )
    ii = schema.classes["II"]()
    ii.root, ii.extension = "2.16.840.1.113883.4.1", "111-00-2330"
    assert ii.as_identifier().system == "http://hl7.org/fhir/sid/us-ssn"
    assert ii.as_identifier().value == "111-00-2330"
    ii = schema.classes["II"]()
    ii.root = "1.2.3"
    assert ii.as_identifier().system == "urn:oid:1.2.3"
    assert ii.as_identifier().value == "1.2.3"
    pq = schema.classes["PQ"]()
    pq.value, pq.unit = Decimal("0.100000000000000001"), "mm[Hg]"
    assert pq.as_quantity().value == Decimal("0.100000000000000001")
    assert pq.as_quantity().unit == "mm[Hg]"
    ce = schema.classes["CE"]()
    ce.codeSystem, ce.code, ce.displayName = "2.16.840.1.113883.6.96", "35064005", "Dark stools"
    assert ce.as_coding().system == "http://snomed.info/sct"
    assert ce.as_coding().display == "Dark stools"
    ce.codeSystem = "1.2.3"
    assert not ce.as_coding().known_system
    ce.nullFlavor = "UNK"
    assert ce.as_coding().code is None
    assert ce.as_coding().null_flavor == "UNK"
    pq.nullFlavor = "NI"
    assert pq.as_quantity().value is None
    ii.nullFlavor = "NI"
    assert ii.as_identifier().value is None
    ts = schema.classes["TS"]()
    ts.nullFlavor = "UNK"
    assert ts.as_datetime().null_flavor == "UNK"


@pytest.mark.parametrize(
    "root, expected",
    [
        ("c6f88321-67ad-11db-bd13-0800200c9a66", "urn:uuid:c6f88321-67ad-11db-bd13-0800200c9a66"),
        ("local-identifier-root", None),
        ("abcdefabcdefabcdefabcdefabcdefab", None),
        (None, None),
    ],
)
def test_identifier_roots_are_not_fabricated_oid_uris(root, expected):
    from examples.healthcare.common.cda_datatypes import system_uri

    assert system_uri(root) == expected
