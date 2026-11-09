"""Characterize the canonical stylesheet schemas as inspection input.

These probes are advisory.  They record what the current pyxsd XSD
compiler can and cannot do with the packaged W3C resources; a finding
here never gates normative XSLT compilation.
"""

from __future__ import annotations

import hashlib
import io
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import pytest

from pyxsd.namespaces import parse_with_namespaces
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xslt import inspection as inspection_module
from pyxsd.xslt.inspection import inspect_stylesheet

FIXTURES = Path(__file__).parents[1] / "fixtures" / "inspection"
RESOURCES = Path(inspection_module.__file__).parent / "resources"
XS = "http://www.w3.org/2001/XMLSchema"

RESOURCE_FILES = (
    "schema-for-xslt20.xsd",
    "schema-for-xslt30.xsd",
    "XMLSchema10.xsd",
    "XMLSchema11.xsd",
    "xml.xsd",
)

#: Original W3C downloads and their SHA-256 digests before the local
#: identifier rewrite recorded in ``resources/NOTICE.md``.
UPSTREAM = {
    "schema-for-xslt20.xsd": (
        "https://www.w3.org/2007/schema-for-xslt20.xsd",
        "2614b194decb2ca130d47b831b23adf197af52c458808951bdc13c1783846db4",
    ),
    "schema-for-xslt30.xsd": (
        "https://www.w3.org/TR/xslt-30/schema-for-xslt30.xsd",
        "1c4c087b7b913f693f43aa9e24b0a06b6db60edee861ca930b1f188b21fec2fc",
    ),
    "XMLSchema10.xsd": (
        "https://www.w3.org/2001/XMLSchema.xsd",
        "e767a159c179bd6e4dc2bac2a63a62f50cbd027ec19d6b5cc9eb1c2f33e0cb2e",
    ),
    "XMLSchema11.xsd": (
        "https://www.w3.org/TR/xmlschema11-1/XMLSchema.xsd",
        "047e652e9863065d297651c103e366daa24fb2c0933cebcf1982272cb80600f5",
    ),
    "xml.xsd": (
        "https://www.w3.org/2001/xml.xsd",
        "61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c",
    ),
}

EXPECTED_XSLT20 = [
    ("unknown-elementRef", "pyxsd-profile-gap"),
    ("attribute-restriction", "pyxsd-validation-defect"),
    ("wildcard-invalid", "pyxsd-validation-defect"),
    ("wildcard-invalid", "pyxsd-validation-defect"),
]

EXPECTED_XSLT30 = [
    ("unknown-elementRef", "pyxsd-profile-gap"),
    ("attribute-restriction", "pyxsd-validation-defect"),
    ("attribute-restriction", "pyxsd-validation-defect"),
    ("wildcard-invalid", "pyxsd-validation-defect"),
    ("wildcard-invalid", "pyxsd-validation-defect"),
    ("wildcard-invalid", "pyxsd-validation-defect"),
]


class OfflinePolicy:
    """Reject any remote read and record local reads for the closure probe."""

    def __init__(self) -> None:
        self.reads: list[str] = []
        self.network_requests = 0

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        if _remote(source) or _remote(base):
            self.network_requests += 1
            raise ResourcePolicyError("network access is not allowed")
        path = Path(source)
        if not path.is_absolute() and base is not None:
            path = Path(base).parent / path
        path = path.resolve()
        self.reads.append(str(path))
        return ResourceData(str(path), path.read_bytes())

    def parse(self, data: ResourceData, context: Any) -> ET.Element:
        return parse_with_namespaces(io.BytesIO(data.content), context)


def _remote(source: str | None) -> bool:
    return source is not None and urlparse(source).scheme in {"http", "https"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _schema_locations(path: Path) -> list[str]:
    root = ET.parse(path).getroot()
    locations: list[str] = []
    for child in root:
        location = child.get("schemaLocation")
        if child.tag in {f"{{{XS}}}import", f"{{{XS}}}include"} and location is not None:
            locations.append(location)
    return locations


def _probe_observation(language: str) -> dict[str, Any]:
    schema = inspection_module._compile_stylesheet_schema(language)
    result = inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language)
    return {
        "classes": len(schema.classes),
        "error_count": schema.report.error_count,
        "codes": Counter(issue.code for issue in schema.report.issues),
        "capability": result.capability,
        "findings": [(item.code, item.classification) for item in result.diagnostics],
        "report_codes": [issue.code for issue in result.report.issues],
    }


def test_packaged_resources_are_local_and_complete():
    for name in RESOURCE_FILES:
        assert (RESOURCES / name).is_file(), name

    for name in RESOURCE_FILES:
        path = RESOURCES / name
        for location in _schema_locations(path):
            assert not urlparse(location).scheme, (name, location)
            assert (path.parent / location).is_file(), (name, location)

    xslt30 = set(_schema_locations(RESOURCES / "schema-for-xslt30.xsd"))
    xslt20 = set(_schema_locations(RESOURCES / "schema-for-xslt20.xsd"))
    assert "XMLSchema11.xsd" in xslt30
    assert "XMLSchema10.xsd" in xslt20
    assert "xml.xsd" in xslt30
    assert "xml.xsd" in xslt20
    assert "xml.xsd" in _schema_locations(RESOURCES / "XMLSchema11.xsd")
    assert "xml.xsd" in _schema_locations(RESOURCES / "XMLSchema10.xsd")


def test_notice_records_provenance_hashes_and_license():
    notice = (RESOURCES / "NOTICE.md").read_text(encoding="utf-8")
    license_text = (RESOURCES / "LICENSE-W3C.txt").read_text(encoding="utf-8")
    for name in RESOURCE_FILES:
        assert name in notice
        assert _sha256(RESOURCES / name) in notice
        url, original_hash = UPSTREAM[name]
        assert url in notice
        assert original_hash in notice
    assert "W3C Software Notice and License" in notice
    assert "LICENSE-W3C.txt" in notice
    assert "W3C SOFTWARE NOTICE AND LICENSE" in license_text
    assert "1998" in license_text
    assert "Copyright" in license_text


def test_probe_xslt20_schema_and_findings():
    observation = _probe_observation("2.0")
    assert observation["classes"] == 177
    assert observation["error_count"] == 4
    assert observation["codes"] == Counter(
        {
            "unknown-elementRef": 1,
            "attribute-restriction": 1,
            "wildcard-invalid": 2,
        }
    )
    assert observation["capability"] == "unsupported"
    assert observation["findings"] == EXPECTED_XSLT20
    assert observation["report_codes"] == [item[0] for item in EXPECTED_XSLT20]
    assert not any(code.upper().startswith("XTSE") for code in observation["report_codes"])


def test_probe_xslt30_schema_and_findings():
    observation = _probe_observation("3.0")
    assert observation["classes"] == 267
    assert observation["error_count"] == 6
    assert observation["codes"] == Counter(
        {
            "unknown-elementRef": 1,
            "attribute-restriction": 2,
            "wildcard-invalid": 3,
        }
    )
    assert observation["capability"] == "unsupported"
    assert observation["findings"] == EXPECTED_XSLT30
    assert observation["report_codes"] == [item[0] for item in EXPECTED_XSLT30]
    assert not any(code.upper().startswith("XTSE") for code in observation["report_codes"])


def test_xslt30_support_is_not_inferred_from_xslt20():
    xslt20 = _probe_observation("2.0")
    xslt30 = _probe_observation("3.0")
    assert xslt30["error_count"] > xslt20["error_count"]
    assert Counter(xslt30["report_codes"]) - Counter(xslt20["report_codes"]) == Counter(
        {"attribute-restriction": 1, "wildcard-invalid": 1}
    )
    xslt20_messages = {
        diagnostic.message
        for diagnostic in inspect_stylesheet(
            FIXTURES / "literal-result.xsl", language="2.0"
        ).diagnostics
    }
    xslt30_messages = {
        diagnostic.message
        for diagnostic in inspect_stylesheet(
            FIXTURES / "literal-result.xsl", language="3.0"
        ).diagnostics
    }
    extra = xslt30_messages - xslt20_messages
    assert len(extra) == 2
    assert any("_version" in message for message in extra)
    assert any("sequence-constructor-or-select" in message for message in extra)


@pytest.mark.parametrize("language", ["2.0", "3.0"])
def test_closure_compiles_offline(language: str):
    policy = OfflinePolicy()
    schema = inspection_module._compile_stylesheet_schema(language, resource_policy=policy)
    assert schema.report.error_count in {4, 6}
    result = inspect_stylesheet(
        FIXTURES / "literal-result.xsl", language=language, resource_policy=policy
    )
    assert result.capability == "unsupported"
    assert policy.network_requests == 0
    assert policy.reads
    assert any(f"schema-for-xslt{language.replace('.', '')}.xsd" in path for path in policy.reads)


def test_pyxsd_findings_are_classified_not_hidden():
    for language, expected in (("2.0", EXPECTED_XSLT20), ("3.0", EXPECTED_XSLT30)):
        result = inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language)
        assert result.capability == "unsupported"
        assert result.report.has_errors
        assert len(result.diagnostics) == len(expected)
        assert all(diagnostic.rationale for diagnostic in result.diagnostics)
        assert all(
            diagnostic.classification != "unclassified-schema-finding"
            for diagnostic in result.diagnostics
        )
        assert (
            inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language).capability
            == result.capability
        )


def test_independent_xsd_oracle_accepts_the_packaged_schemas():
    """The findings above are pyxsd's, not the official schemas'."""
    xmlschema = pytest.importorskip("xmlschema")
    assert xmlschema.XMLSchema10(str(RESOURCES / "schema-for-xslt20.xsd")).all_errors == []
    assert xmlschema.XMLSchema11(str(RESOURCES / "schema-for-xslt30.xsd")).all_errors == []


def test_inspection_needs_no_network_even_without_a_policy(monkeypatch: pytest.MonkeyPatch):
    import socket

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("inspection attempted network access")

    monkeypatch.setattr(socket.socket, "connect", fail)
    for language in ("2.0", "3.0"):
        result = inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language)
        assert result.capability == "unsupported"


def test_schema_probe_never_reports_normative_xslt_errors():
    for language in ("2.0", "3.0"):
        result = inspect_stylesheet(FIXTURES / "literal-result.xsl", language=language)
        for issue in result.report.issues:
            assert not issue.code.upper().startswith("XTSE")
        for diagnostic in result.diagnostics:
            assert not diagnostic.code.upper().startswith("XTSE")
