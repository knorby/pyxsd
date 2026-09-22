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


def test_cli_accepts_xsd_version(tmp_path):
    from pyxsd.cli import main

    schema = tmp_path / "s.xsd"
    schema.write_text(BASIC)
    xml = tmp_path / "i.xml"
    xml.write_text("<e>x</e>")
    main(["-i", str(xml), "-s", str(schema), "--xsd-version", "1.0", "-q"])
