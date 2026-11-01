"""XSD processor-version selection (``Schema.compile(xsd_version=...)``).

The processor version is the declared ``V`` that ``vc:*`` conditional
inclusion tests against (XSD 1.1 §4.2.2). It does not by itself restrict
the schema vocabulary; that is checked separately (see ``version_gates``).
"""

from io import StringIO

import pytest

import pyxsd

BASIC = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="e" type="xs:string"/>
</xs:schema>"""

VC_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
             xmlns:vc="http://www.w3.org/2007/XMLSchema-versioning">
  <xs:element name="e" type="xs:string" vc:minVersion="1.1"/>
</xs:schema>"""

#: Pattern forms the XSD 1.1 grammar liberalized and XSD 1.0 rejects.
VERSION_SPLIT_PATTERNS = (
    r"[^a-d-b-c]",
    r"[a-c-1-4x-z-7-9]*",
    r"[a-a-x-x]+",
    r"\p{IsaA0-a9}",
)


def _pattern_schema(pattern):
    return (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
        '<xs:element name="e"><xs:simpleType><xs:restriction base="xs:string">'
        f'<xs:pattern value="{pattern}"/>'
        "</xs:restriction></xs:simpleType></xs:element></xs:schema>"
    )


def _compile(text, **kwargs):
    return pyxsd.Schema.compile(StringIO(text), **kwargs)


def test_default_is_xsd_1_1():
    assert _compile(BASIC).xsd_version == "1.1"


def test_xsd_1_0_mode_compiles_and_reports_version():
    schema = _compile(BASIC, xsd_version="1.0")
    assert schema.xsd_version == "1.0"
    assert schema.parse(StringIO("<e>x</e>")).is_valid


def test_invalid_version_string_rejected():
    with pytest.raises(ValueError):
        _compile(BASIC, xsd_version="1.2")


def test_vc_min_version_respects_mode():
    # In 1.0 mode the processor version is 1.0, so a vc:minVersion="1.1"
    # declaration is excluded; under the default 1.1 processor it binds.
    s10 = _compile(VC_SCHEMA, xsd_version="1.0")
    assert s10.parse(StringIO("<e>x</e>")).root is None
    s11 = _compile(VC_SCHEMA)
    assert s11.parse(StringIO("<e>x</e>")).root is not None


@pytest.mark.parametrize("pattern", VERSION_SPLIT_PATTERNS)
def test_regex_grammar_follows_the_selected_version(pattern):
    # The production path threads the compile host's processor version, so
    # a XSD 1.0 compilation rejects what only the 1.1 grammar accepts.
    s11 = _compile(_pattern_schema(pattern))
    assert s11.report.errors == []
    s10 = _compile(_pattern_schema(pattern), xsd_version="1.0")
    assert [issue.code for issue in s10.report.errors] == ["facet"]


def test_ordinary_pattern_compiles_in_both_versions():
    text = _pattern_schema("[0-9]{3}")
    assert _compile(text).report.errors == []
    assert _compile(text, xsd_version="1.0").report.errors == []


def test_cli_accepts_xsd_version(tmp_path):
    from pyxsd.cli import main

    schema = tmp_path / "s.xsd"
    schema.write_text(BASIC)
    xml = tmp_path / "i.xml"
    xml.write_text("<e>x</e>")
    main(["-i", str(xml), "-s", str(schema), "--xsd-version", "1.0", "-q"])
