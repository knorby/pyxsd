"""Sequential, explicit-path parsing contracts."""

from dataclasses import FrozenInstanceError
from io import StringIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pyxsd import Schema, ValidationError
from pyxsd.batch import BatchParseError, DocumentSource, InputFailure, ParseOutcome
from pyxsd.binding import ParseModes
from pyxsd.exceptions import PyXSDError


@pytest.mark.parametrize("identifier", ["", None, 1])
def test_source_requires_nonempty_string_id(identifier, tmp_path):
    with pytest.raises((TypeError, ValueError), match="id"):
        DocumentSource(identifier, tmp_path / "missing.xml")


@pytest.mark.parametrize("path", [None, 1, b"input.xml", object()])
def test_source_rejects_non_text_paths(path):
    with pytest.raises(TypeError, match="path"):
        DocumentSource("input", path)


def test_source_anchors_path_at_construction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = DocumentSource("input", "missing.xml")
    monkeypatch.chdir(tmp_path.parent)
    assert source.path == tmp_path / "missing.xml"
    assert isinstance(source.path, Path)
    with pytest.raises(FrozenInstanceError):
        source.id = "changed"


def test_outcome_snapshots_issues_and_failure_has_no_exception(tmp_path):
    issues = []
    failure = InputFailure("io", "missing file")
    outcome = ParseOutcome(
        DocumentSource("input", tmp_path / "missing"), "input_error", None, issues, failure
    )
    issues.append("later")
    assert outcome.issues == ()
    assert vars(failure) == {"kind": "io", "message": "missing file"}
    error = BatchParseError(outcome)
    assert isinstance(error, PyXSDError)
    assert error.outcome is outcome
    assert "input" in str(error)


SIMPLE_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="record"><xs:complexType><xs:sequence>
    <xs:element name="count" type="xs:int"/>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""


@pytest.fixture
def schema():
    return Schema.compile(StringIO(SIMPLE_SCHEMA))


def sources_for(tmp_path, *texts):
    sources = []
    for index, text in enumerate(texts):
        path = tmp_path / f"{index}.xml"
        path.write_text(text)
        sources.append(DocumentSource(str(index), path))
    return sources


@pytest.fixture
def mixed_sources(tmp_path):
    return sources_for(
        tmp_path,
        "<record><count>1</count></record>",
        "<record/>",
        "<record>",
        "<record><count>4</count></record>",
    )


def test_report_isolates_results_and_reuses_classes(schema, mixed_sources):
    outcomes = list(schema.iter_parse(mixed_sources, errors="report"))
    assert [outcome.status for outcome in outcomes] == ["valid", "invalid", "input_error", "valid"]
    a, b, c, d = outcomes
    assert a.issues == d.issues == ()
    assert b.issues and all(issue.phase == "instance" for issue in b.issues)
    assert c.document is None and c.error.kind == "malformed_xml"
    assert type(a.document.root) is type(d.document.root)
    assert a.document.to_dict() == {"count": 1}
    assert a.document.revalidate().is_valid


def test_raise_stops_without_pulling_later_sources(schema, mixed_sources):
    pulled = []

    def inputs():
        for source in mixed_sources:
            pulled.append(source.id)
            yield source

    results = schema.iter_parse(inputs())
    assert next(results).status == "valid"
    with pytest.raises(BatchParseError) as caught:
        next(results)
    assert caught.value.outcome.source.id == "1"
    assert isinstance(caught.value.__cause__, ValidationError)
    assert pulled == ["0", "1"]


@pytest.mark.parametrize(
    "text,kind,cause", [(None, "io", OSError), ("<broken>", "malformed_xml", ET.ParseError)]
)
@pytest.mark.parametrize("policy", ["report", "raise"])
def test_expected_input_failures(schema, tmp_path, text, kind, cause, policy):
    path = tmp_path / "input.xml"
    if text is not None:
        path.write_text(text)
    iterator = schema.iter_parse([DocumentSource("input", path)], errors=policy)
    if policy == "raise":
        with pytest.raises(BatchParseError) as caught:
            next(iterator)
        assert isinstance(caught.value.__cause__, PyXSDError)
        assert isinstance(caught.value.__cause__.__cause__, cause)
        outcome = caught.value.outcome
    else:
        outcome = next(iterator)
    assert outcome.status == "input_error"
    assert outcome.error.kind == kind
    assert outcome.error.message
    assert outcome.document is None and outcome.issues == ()


@pytest.mark.parametrize("empty", [False, True])
def test_invalid_schema_checked_before_input_pull(tmp_path, empty):
    broken = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
    <xs:element name="a"><xs:complexType><xs:complexContent>
    <xs:extension base="Missing"/>
    </xs:complexContent></xs:complexType></xs:element></xs:schema>"""
    schema = Schema.compile(StringIO(broken))
    pulled = []

    def inputs():
        pulled.append(True)
        if not empty:
            yield DocumentSource("input", tmp_path / "missing")

    with pytest.raises(ValidationError):
        list(schema.iter_parse(inputs(), errors="report"))
    assert pulled == []


def test_duplicate_ids_stop_before_open_but_same_path_allowed(schema, mixed_sources):
    source = mixed_sources[0]
    results = schema.iter_parse([source, DocumentSource(source.id, "missing")], errors="report")
    assert next(results).status == "valid"
    with pytest.raises(ValueError, match="duplicate"):
        next(results)
    assert [
        outcome.status
        for outcome in schema.iter_parse([source, DocumentSource("again", source.path)])
    ] == ["valid", "valid"]


def test_invalid_policy_and_source_types_rejected(schema):
    with pytest.raises(ValueError, match="errors"):
        list(schema.iter_parse([], errors="ignore"))
    with pytest.raises(TypeError, match="DocumentSource"):
        list(schema.iter_parse(["input.xml"], errors="report"))


@pytest.mark.parametrize("policy", ["report", "raise"])
def test_iterator_exceptions_propagate(schema, policy):
    def inputs():
        raise RuntimeError("iterator defect")
        yield

    with pytest.raises(RuntimeError, match="iterator defect"):
        list(schema.iter_parse(inputs(), errors=policy))


@pytest.mark.parametrize("policy", ["report", "raise"])
@pytest.mark.parametrize(
    "defect",
    [
        RuntimeError("binder defect"),
        TypeError("binder defect"),
        PyXSDError("binder defect"),
        KeyboardInterrupt(),
    ],
)
def test_internal_binding_defects_propagate(schema, mixed_sources, monkeypatch, policy, defect):
    def broken(*args):
        raise defect

    monkeypatch.setattr("pyxsd.schema.bind_instance", broken)
    with pytest.raises(type(defect)):
        list(schema.iter_parse(mixed_sources, errors=policy))


IDENTITY_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
<xs:element name="catalog"><xs:complexType><xs:sequence>
<xs:element name="item" minOccurs="0" maxOccurs="unbounded"><xs:complexType>
<xs:attribute name="id" type="xs:string" use="required"/>
<xs:attribute name="name" type="xs:QName"/>
</xs:complexType></xs:element>
<xs:element name="link" minOccurs="0" maxOccurs="unbounded"><xs:complexType>
<xs:attribute name="ref" type="xs:string" use="required"/>
</xs:complexType></xs:element>
</xs:sequence></xs:complexType>
<xs:key name="itemKey"><xs:selector xpath="item"/><xs:field xpath="@id"/></xs:key>
<xs:keyref name="linkRef" refer="itemKey"><xs:selector xpath="link"/><xs:field xpath="@ref"/></xs:keyref>
</xs:element></xs:schema>"""


def test_keys_and_qname_evidence_remain_per_document(tmp_path):
    schema = Schema.compile(StringIO(IDENTITY_SCHEMA), mode=ParseModes.NAMESPACED)
    inputs = sources_for(
        tmp_path,
        '<catalog xmlns:p="urn:first"><item id="same" name="p:Thing"/></catalog>',
        '<catalog xmlns:p="urn:later"><item id="same" name="p:Thing"/></catalog>',
        '<catalog><link ref="same"/></catalog>',
    )
    a, d, unresolved = schema.iter_parse(inputs, errors="report")
    assert a.status == d.status == "valid"
    assert unresolved.status == "invalid"
    assert "identity-keyref" in [issue.code for issue in unresolved.issues]
    from pyxsd.xsd_data_types import xsd_value_key

    assert a.document.to_dict()["item"]["@name"] == "p:Thing"
    assert xsd_value_key(getattr(a.document.find("item"), "name|2")) == (
        "QName",
        ("urn:first", "Thing"),
    )
    assert xsd_value_key(getattr(d.document.find("item"), "name|2")) == (
        "QName",
        ("urn:later", "Thing"),
    )
    assert "p:Thing" in a.document.to_string()
