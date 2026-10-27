"""Skill guidance examples stay runnable without an MCP server installed."""

import re
import subprocess
import sys
from pathlib import Path

from pyxsd import Schema
from pyxsd.binding import ParseModes

MIXED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="note">
  <xs:complexType mixed="true"><xs:sequence>
    <xs:element name="body" type="xs:string"/>
  </xs:sequence></xs:complexType>
</xs:element></xs:schema>"""

CATALOG = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
  xmlns:cat="http://catalog.example" targetNamespace="http://catalog.example">
  <xs:element name="catalog"><xs:complexType><xs:sequence>
    <xs:element name="item" maxOccurs="unbounded"><xs:complexType><xs:sequence>
      <xs:element name="sku" type="cat:Sku"/>
      <xs:element name="price" type="xs:decimal"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType></xs:element>
  <xs:simpleType name="Sku"><xs:restriction base="xs:string">
    <xs:pattern value="[A-Z]{3}-[0-9]{4}"/>
  </xs:restriction></xs:simpleType>
</xs:schema>"""

ITEMS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item"><xs:sequence>
    <xs:element name="value" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:element name="root"><xs:complexType><xs:sequence>
    <xs:element name="item" type="Item" minOccurs="0" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


def write(root: Path, name: str, text: str) -> str:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_compile_once_parse_many_and_stub_depths(tmp_path):
    schema = Schema.compile(write(tmp_path, "catalog.xsd", CATALOG))
    good = write(
        tmp_path,
        "good.xml",
        "<cat:catalog xmlns:cat='http://catalog.example'>"
        "<item><sku>ABC-1234</sku><price>1.2300</price></item></cat:catalog>",
    )
    first = schema.parse(good)
    second = schema.parse(good)
    assert first.report.error_count == 0 == second.report.error_count
    compact = schema.stub(compact=True)
    full = schema.stub(compact=False)
    assert len(full) > len(compact)
    assert "Catalog" in full
    assert "Sku" in full


def test_precise_repeated_node_repair_and_fresh_revalidation(tmp_path):
    schema = Schema.compile(write(tmp_path, "items.xsd", ITEMS), mode=ParseModes.LAX)
    document = write(
        tmp_path,
        "broken.xml",
        "<root><item><value>1</value></item>"
        "<item><value>bad</value></item>"
        "<item><value>3</value></item></root>",
    )
    parsed = schema.parse(document)
    assert parsed.report.error_count == 1
    occurrences = parsed.findall("./item")
    assert len(occurrences) == 3
    assert occurrences[1] is not parsed.root._children_[0]
    occurrences[1].value = 2
    revised = tmp_path / "revised.xml"
    revised.write_text(parsed.to_string(), encoding="utf-8")
    again = schema.parse(str(revised))
    assert again.report.error_count == 0
    assert [item.value for item in again.findall("./item")] == [1, 2, 3]


def test_minimal_semantic_change_preserves_other_nodes(tmp_path):
    schema = Schema.compile(write(tmp_path, "items.xsd", ITEMS), mode=ParseModes.LAX)
    document = write(
        tmp_path,
        "minimal.xml",
        "<root><item><value>1</value></item><item><value>2</value></item></root>",
    )
    parsed = schema.parse(document)
    before = parsed.to_string()
    parsed.findall("./item")[0].value = 9
    after = parsed.to_string()
    assert "<value>1</value>" in before
    assert "<value>9</value>" in after
    assert "<value>2</value>" in after
    assert after.count("<item>") == before.count("<item>")
    explicit = tmp_path / "published.xml"
    explicit.write_text(parsed.to_string(), encoding="utf-8")
    assert explicit.is_file()
    assert not (tmp_path / "unrequested.xml").exists()


def test_facet_evidence_and_mixed_content_fidelity(tmp_path):
    schema = Schema.compile(write(tmp_path, "catalog.xsd", CATALOG))
    invalid = write(
        tmp_path,
        "facet.xml",
        "<cat:catalog xmlns:cat='http://catalog.example'>"
        "<item><sku>nope</sku><price>1.00</price></item></cat:catalog>",
    )
    failed = schema.parse(invalid)
    assert failed.report.error_count == 1
    issue = failed.report.issues[0]
    assert issue.code == "value"
    assert issue.constraint.kind == "pattern"
    assert issue.constraint.actual == "nope"
    assert "[A-Z]{3}-[0-9]{4}" in issue.constraint.expected[0]

    mixed = Schema.compile(write(tmp_path, "mixed.xsd", MIXED))
    note = write(tmp_path, "note.xml", "<note>around <body>text</body> tail</note>")
    parsed = mixed.parse(note)
    assert parsed.report.error_count == 0
    rendered = parsed.to_string()
    assert "around" in rendered
    assert "tail" in rendered
    assert "text" in rendered


def test_core_workflow_runs_without_optional_dependencies():
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import pyxsd, pyxsd.skills\nprint(pyxsd.__version__)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    assert re.fullmatch(r"\d+\.\d+\.\d+\S*", probe.stdout.strip())
    assert "pyxsd.mcp" not in probe.stdout
