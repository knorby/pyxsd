"""Tests for the conformance runner itself (not the corpus).

These guard against the runner passing for the wrong reason: before
phase separation it read one merged report, so an instance-invalid case
could pass even when the schema itself was broken.
"""

from conformance_runner import run_case

XS = "http://www.w3.org/2001/XMLSchema"

# A schema that compiles but reports a schema-phase error
# (``unknown-substitution-head``).
BROKEN_SCHEMA = f"""<xs:schema xmlns:xs="{XS}">
  <xs:element name="m" type="xs:string" substitutionGroup="nope"/>
  <xs:element name="r"><xs:complexType><xs:sequence>
    <xs:element name="a" type="xs:string"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""

GOOD_SCHEMA = f"""<xs:schema xmlns:xs="{XS}">
  <xs:element name="r" type="xs:string"/>
</xs:schema>"""


def test_schema_only_valid_case_passes(tmp_path):
    case = {"schema": GOOD_SCHEMA, "schema_valid": True}
    passed, detail = run_case(case, tmp_path)
    assert passed, detail


def test_schema_invalid_case_checks_schema_phase(tmp_path):
    case = {
        "schema": BROKEN_SCHEMA,
        "schema_valid": False,
        "expected_codes": ["unknown-substitution-head"],
    }
    passed, detail = run_case(case, tmp_path)
    assert passed, detail


def test_schema_error_never_masked_by_instance_error(tmp_path):
    """The R15 regression: schema is broken and the instance is invalid.

    The old runner summed the two reports, saw the expected instance
    code, and passed. The schema error must fail the case on its own.
    """
    case = {
        "schema": BROKEN_SCHEMA,
        "instance": "<r/>",
        "schema_valid": True,
        "instance_valid": False,
        "expected_codes": ["order"],
    }
    passed, detail = run_case(case, tmp_path)
    assert not passed
    assert "schema" in detail.lower()


def test_valid_instance_with_schema_error_fails(tmp_path):
    case = {
        "schema": BROKEN_SCHEMA,
        "instance": "<r><a>x</a></r>",
        "schema_valid": True,
    }
    passed, detail = run_case(case, tmp_path)
    assert not passed
    assert "schema" in detail.lower()
