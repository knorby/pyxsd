"""Phase 5 release gate: executable conversion parity and hostile inputs.

Every public RELAX NG conversion entry is exercised against its real adapter:
the shared XSD-compatible option contract, the absence of any XSD conversion
path, export/import round trips for the semantics each format promises, tagged
scalar claims, freshness and mutation gates, scoped datatype contexts, JSON
Pointer diagnostics, Draft 2020-12 agreement, and explicit representation-limit
disclosure.

Positive assertions validate actual exports against the generated schema.
Negative assertions pin the shapes the schema rejects.  Claim fixtures are
forged from real exports, so a test cannot pass by describing an invented wire
shape.
"""

from __future__ import annotations

import copy
import inspect
import io
import json
import warnings
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import pyxsd
import pyxsd.data_conversion.export as xsd_data_export
import pyxsd.data_conversion.import_data as xsd_data_import
import pyxsd.data_conversion.json_schema as xsd_json_schema
import pyxsd.dict_export as xsd_dict_export
import pyxsd.remote as remote
import pyxsd.resource_policy as resource_policy
from pyxsd.data_conversion.contracts import FormatName
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError, PyXSDWarning, ValidationError
from pyxsd.relaxng import (
    AlternativeValue,
    RelaxNGDocument,
    RelaxNGSchema,
    StaleInterpretationError,
)
from pyxsd.relaxng.conversion import RelaxNGUnsupportedAdapterError, export_relaxng
from pyxsd.relaxng.json_profile import data_to_relaxng_tree
from pyxsd.relaxng.json_values import (
    ScalarTagClaim,
    ScalarTagLocation,
    parse_scalar_tag,
    verify_tag_claims,
)
from pyxsd.xsd_data_types import QName

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"
XML_NS = "http://www.w3.org/XML/1998/namespace"
XMLNS_NS = "http://www.w3.org/2000/xmlns/"

FORMATS: tuple[FormatName, ...] = ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml")
NON_NATIVE: tuple[FormatName, ...] = ("pyxsd-json", "parker", "badgerfish", "jsonml")
GROUPED: tuple[FormatName, ...] = ("pyxsd", "parker", "badgerfish")
ORDERED: tuple[FormatName, ...] = ("pyxsd-json", "jsonml")
TAGGED: tuple[FormatName, ...] = ("pyxsd", "pyxsd-json")

INTEGER = '<data type="integer"/>'
STRING = '<data type="string"/>'
EMPTY = "<empty/>"
A = '<element name="a"><data type="integer"/></element>'
B = '<element name="b"><data type="string"/></element>'
ATTR = '<optional><attribute name="z"><data type="string"/></attribute></optional>'
ALT = "<choice><data type='integer'/><data type='string'/></choice>"
MIXED_I = (
    '<mixed><zeroOrMore><element name="i"><data type="string"/></element></zeroOrMore></mixed>'
)

XSD_SCHEMA = """\
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="root">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="item" type="xs:int"/>
      </xs:sequence>
      <xs:attribute name="a" type="xs:int"/>
    </xs:complexType>
  </xs:element>
</xs:schema>"""


# ---------------------------------------------------------------------------
# Fixtures and assertion helpers
# ---------------------------------------------------------------------------


def schema_for(content: str, *, name: str = 'name="r"') -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<element xmlns="{NS}" datatypeLibrary="{XSD}" {name}>{content}</element>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def grammar_for(body: str) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(f'<grammar xmlns="{NS}" datatypeLibrary="{XSD}">{body}</grammar>')
    )
    assert not schema.report.has_errors, schema.report.issues
    return schema


def parse(
    content: str,
    xml: str = "<r/>",
    *,
    typed: bool = True,
    name: str = 'name="r"',
) -> RelaxNGDocument:
    document = schema_for(content, name=name).parse(io.StringIO(xml), typed=typed)
    assert isinstance(document, RelaxNGDocument)
    return document


def valid(
    content: str,
    xml: str = "<r/>",
    *,
    typed: bool = True,
    name: str = 'name="r"',
) -> RelaxNGDocument:
    document = parse(content, xml, typed=typed, name=name)
    assert not document.report.has_errors, document.report.issues
    return document


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def conversion_error(code: str, pointer: str, action: Callable[[], Any]) -> PyXSDError:
    with pytest.raises(PyXSDError) as caught:
        action()
    message = str(caught.value)
    assert message.startswith(f"{code} at {pointer!r}: "), message
    return caught.value


def warning_prefixes(record: list[Any]) -> list[str]:
    return [
        str(item.message).split(" at ", 1)[0]
        for item in record
        if isinstance(item.message, PyXSDWarning)
    ]


def validates(schema: RelaxNGSchema, data: object, *, format: FormatName, **options: Any) -> None:
    generated = schema.json_schema(format=format, **options)
    Draft202012Validator.check_schema(generated)
    errors = list(Draft202012Validator(generated).iter_errors(data))
    assert errors == [], (format, errors[:3])


def rejects(schema: RelaxNGSchema, data: object, *, format: FormatName, **options: Any) -> None:
    generated = schema.json_schema(format=format, **options)
    assert list(Draft202012Validator(generated).iter_errors(data)), (format, data)


def round_trip(
    document: RelaxNGDocument,
    format: FormatName,
    *,
    fidelity: str = "strict",
    **options: Any,
) -> tuple[Any, RelaxNGDocument]:
    data = document.to_data(format=format, fidelity=fidelity, **options)  # type: ignore[arg-type]
    jsonable(data)
    imported = document.schema.from_data(
        jsonable(data),
        format=format,
        fidelity=fidelity,  # type: ignore[arg-type]
    )
    assert not imported.report.has_errors, (format, imported.report.issues)
    assert imported.to_data(format=format, fidelity=fidelity, **options) == data  # type: ignore[arg-type]
    validates(
        document.schema,
        data,
        format=format,
        fidelity=fidelity,
        **options,
    )
    return data, imported


def data_attributes(node: Any) -> dict[str, str]:
    return {
        name: value
        for name, value in node.attributes.items()
        if name != "xmlns" and not name.startswith(f"{{{XMLNS_NS}}}")
    }


def lexical_tree(node: Any) -> dict[str, Any]:
    return {
        "name": node._name_,
        "attributes": data_attributes(node),
        "text": node.text_value(),
        "children": [
            {"node": lexical_tree(child), "tail": child.tail_value()} for child in node.children
        ],
    }


def grouped_values(node: Any) -> dict[str, Any]:
    """Represented names, values, and repeated order; not text positions."""
    return {
        "name": node._name_,
        "attributes": data_attributes(node),
        "text": node.text_value(),
        "children": [(child._name_, grouped_values(child)) for child in node.children],
    }


def xsd_document() -> Any:
    schema = pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA))
    assert not schema.report.has_errors
    return schema.parse(io.StringIO('<root a="1"><item>2</item></root>'))


def matched_relaxng_document() -> RelaxNGDocument:
    return valid(
        '<attribute name="a"><data type="integer"/></attribute>'
        '<element name="item"><data type="integer"/></element>',
        '<root a="1"><item>2</item></root>',
        name='name="root"',
    )


def tag_claim(document: RelaxNGDocument, path: str = "/r") -> ScalarTagClaim:
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    value = data
    for segment in path.strip("/").split("/"):
        assert segment
        value = value[segment]  # type: ignore[index]
    return parse_scalar_tag(value, path)


# ---------------------------------------------------------------------------
# 1. Shared APIs and option contract
# ---------------------------------------------------------------------------


def test_every_public_entry_accepts_all_five_formats() -> None:
    document = valid(INTEGER, "<r>12</r>")
    expected = {
        "pyxsd": {"$": 12},
        "pyxsd-json": {"r": 12},
        "parker": 12,
        "badgerfish": {"r": 12},
        "jsonml": ["r", 12],
    }

    assert document.to_dict() == {"$": 12}
    for format in FORMATS:
        data = document.to_data(format=format)
        assert data == expected[format]
        assert json.loads(document.to_json(format=format, indent=2)) == expected[format]
        assert export_relaxng(document, format=format) == expected[format]

        imported = document.schema.from_data(data, format=format)
        assert not imported.report.has_errors
        from_text = document.schema.from_json(json.dumps(data), format=format)
        assert not from_text.report.has_errors
        assert from_text.to_data(format=format) == data

        generated = document.schema.json_schema(format=format)
        Draft202012Validator.check_schema(generated)
        assert generated["x-pyxsd-profile"] == "relaxng"
        assert generated["x-pyxsd-format"] == format


@pytest.mark.parametrize("format", NON_NATIVE)
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only data-export option"),
        ({"always_list": True}, "always_list is a native-only data-export option"),
    ],
)
def test_nonnative_export_options_raise_exactly(
    format: FormatName, options: dict[str, bool], message: str
) -> None:
    document = valid(INTEGER, "<r>12</r>")

    for action in (
        lambda: document.to_data(format=format, **options),
        lambda: document.to_json(format=format, **options),
        lambda: export_relaxng(document, format=format, **options),
    ):
        with pytest.raises(ValueError) as caught:
            action()
        assert str(caught.value) == message


@pytest.mark.parametrize("format", NON_NATIVE)
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only JSON Schema option"),
        ({"always_list": True}, "always_list is a native-only JSON Schema option"),
    ],
)
def test_nonnative_schema_options_raise_exactly(
    format: FormatName, options: dict[str, bool], message: str
) -> None:
    schema = schema_for(INTEGER)
    with pytest.raises(ValueError) as caught:
        schema.json_schema(format=format, **options)
    assert str(caught.value) == message


@pytest.mark.parametrize("format", FORMATS)
def test_invalid_format_and_fidelity_fail_before_any_adapter(format: FormatName) -> None:
    document = valid(INTEGER, "<r>12</r>")
    schema = document.schema

    for action in (
        lambda: document.to_data(format="nope"),  # type: ignore[arg-type]
        lambda: document.to_json(format="nope"),  # type: ignore[arg-type]
        lambda: export_relaxng(document, format="nope"),  # type: ignore[arg-type]
        lambda: document.to_data(format=format, fidelity="lossy"),  # type: ignore[arg-type]
        lambda: export_relaxng(document, format=format, fidelity="lossy"),  # type: ignore[arg-type]
        lambda: schema.from_data({"r": 1}, format="nope"),  # type: ignore[arg-type]
        lambda: schema.from_json('{"r": 1}', format="nope"),  # type: ignore[arg-type]
        lambda: schema.from_data({"r": 1}, format=format, fidelity="lossy"),  # type: ignore[arg-type]
        lambda: schema.from_json('{"r": 1}', format=format, fidelity="lossy"),  # type: ignore[arg-type]
        lambda: schema.json_schema(format="nope"),  # type: ignore[arg-type]
        lambda: schema.json_schema(format=format, fidelity="lossy"),  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError, match=r"unknown (data format|fidelity mode)"):
            action()


def test_imports_reject_native_only_options() -> None:
    schema = schema_for(INTEGER)

    for method, source in (
        ("from_data", {"r": 1}),
        ("from_json", '{"r": 1}'),
    ):
        with pytest.raises(TypeError):
            getattr(schema, method)(source, typed=False)  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            getattr(schema, method)(source, always_list=True)  # type: ignore[call-arg]


def test_matched_xsd_and_relaxng_fixtures_share_shared_format_output() -> None:
    xsd = xsd_document()
    relaxed = matched_relaxng_document()

    for format in ("pyxsd", "pyxsd-json", "badgerfish", "jsonml"):
        assert xsd.to_data(format=format, fidelity="strict") == relaxed.to_data(
            format=format, fidelity="strict"
        ), format

    for document in (xsd, relaxed):
        with pytest.raises(PyXSDError) as caught:
            document.to_data(format="parker", fidelity="strict")
        assert "attributes-lost" in str(caught.value)


def test_xsd_rejects_relaxng_tags_and_profile_metadata() -> None:
    relaxed = valid(ALT, "<r>12</r>")
    tagged = relaxed.to_data(format="pyxsd-json", fidelity="strict")
    assert "#value" in json.dumps(tagged)

    xsd = xsd_document()
    xsd_schema = xsd.schema.json_schema(format="pyxsd-json")
    assert xsd_schema.get("x-pyxsd-profile") != "relaxng"
    assert list(Draft202012Validator(xsd_schema).iter_errors(tagged))

    with pytest.raises(PyXSDError):
        xsd.schema.from_data(tagged, format="pyxsd-json")
    with pytest.raises(PyXSDError):
        xsd.schema.from_json(json.dumps(tagged), format="pyxsd-json")
    with pytest.raises(PyXSDError):
        xsd.schema.from_data({"a": 1, "#value": tagged["r"]}, format="pyxsd")

    # The XSD path itself remains exactly as before.
    assert xsd.to_dict() == {"@a": 1, "item": 2}
    assert xsd.to_data() == {"@a": 1, "item": 2}
    assert json.loads(xsd.to_json()) == {"@a": 1, "item": 2}
    xsd_data = xsd.to_data(format="pyxsd-json", fidelity="strict")
    validates(xsd.schema, xsd_data, format="pyxsd-json", fidelity="strict")
    assert xsd_data == {"root": [{"@a": 1}, {"item": 2}]}


def test_xsd_public_signatures_and_defaults_are_unchanged() -> None:
    xsd = xsd_document()
    relaxed = valid(INTEGER, "<r>12</r>")

    for method in ("to_dict", "to_data", "to_json"):
        xsd_signature = inspect.signature(getattr(Document, method))
        relaxed_signature = inspect.signature(getattr(RelaxNGDocument, method))
        assert [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in relaxed_signature.parameters.values()
        ] == [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in xsd_signature.parameters.values()
        ]

    assert relaxed.to_data() == {"$": 12}
    assert xsd.to_data() == {"@a": 1, "item": 2}
    assert xsd.to_dict() == {"@a": 1, "item": 2}


# ---------------------------------------------------------------------------
# 2. XSD isolation
# ---------------------------------------------------------------------------


def test_relaxng_never_reaches_an_xsd_conversion_or_schema_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached: list[str] = []

    def forbidden(name: str) -> Callable[..., Any]:
        def fail(*args: object, **kwargs: object) -> Any:
            reached.append(name)
            raise AssertionError(f"an XSD path was reached: {name}")

        return fail

    monkeypatch.setattr(xsd_data_export, "bound_to_data", forbidden("bound_to_data"))
    monkeypatch.setattr(xsd_dict_export, "bound_to_dict", forbidden("bound_to_dict"))
    monkeypatch.setattr(xsd_data_import, "data_to_element", forbidden("data_to_element"))
    monkeypatch.setattr(xsd_json_schema, "build_json_schema", forbidden("build_json_schema"))

    document = valid(INTEGER, "<r>12</r>")
    assert document.to_dict() == {"$": 12}
    for format in FORMATS:
        data = document.to_data(format=format)
        assert document.to_json(format=format)
        assert export_relaxng(document, format=format) == data
        assert document.schema.from_data(data, format=format).to_data(format=format) == data
        assert (
            document.schema.from_json(json.dumps(data), format=format).to_data(format=format)
            == data
        )
        Draft202012Validator.check_schema(document.schema.json_schema(format=format))

    assert reached == []


@pytest.mark.parametrize("format", FORMATS)
def test_public_paths_never_raise_the_unsupported_adapter_error(format: FormatName) -> None:
    document = valid(INTEGER, "<r>12</r>")
    try:
        assert (
            document.to_data(format=format)
            == {
                "pyxsd": {"$": 12},
                "pyxsd-json": {"r": 12},
                "parker": 12,
                "badgerfish": {"r": 12},
                "jsonml": ["r", 12],
            }[format]
        )
        assert document.to_json(format=format)
        assert export_relaxng(document, format=format)
        data = document.to_data(format=format)
        document.schema.from_data(data, format=format)
        document.schema.from_json(json.dumps(data), format=format)
        document.schema.json_schema(format=format)
    except RelaxNGUnsupportedAdapterError as error:  # pragma: no cover - explicit gate
        pytest.fail(f"a public format reached the unsupported adapter error: {error}")


# ---------------------------------------------------------------------------
# 3. Round trips
# ---------------------------------------------------------------------------


STABLE_CASES = [
    ("integer", INTEGER, "<r>12</r>"),
    ("string", STRING, "<r>x</r>"),
    ("empty", EMPTY, "<r/>"),
    ("optional-attribute-absent", ATTR + STRING, "<r>x</r>"),
    ("record", f"<interleave>{A}{B}</interleave>", "<r><b>B</b><a>1</a></r>"),
    ("ordered-sequence", f"{A}{B}", "<r><a>1</a><b>B</b></r>"),
    ("repeated", f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>1</a><a>2</a></r>"),
    ("repeated-empty", f"<zeroOrMore>{A}</zeroOrMore>", "<r/>"),
    ("required-one", f"<oneOrMore>{A}</oneOrMore>", "<r><a>1</a></r>"),
]


@pytest.mark.parametrize(("content", "xml"), [(case[1], case[2]) for case in STABLE_CASES])
@pytest.mark.parametrize("format", FORMATS)
def test_stable_values_round_trip_for_every_format(
    content: str, xml: str, format: FormatName
) -> None:
    document = valid(content, xml)
    data, imported = round_trip(document, format)
    assert data is not None

    if format in ORDERED:
        assert sorted(child._name_ for child in imported.root.children) == sorted(
            child._name_ for child in document.root.children
        )
    else:
        assert grouped_values(imported.root)["name"] == grouped_values(document.root)["name"]
        assert sorted(child._name_ for child in imported.root.children) == sorted(
            child._name_ for child in document.root.children
        )


@pytest.mark.parametrize("format", ("pyxsd", "pyxsd-json", "badgerfish", "jsonml"))
def test_absent_and_present_attributes_keep_their_representation(format: FormatName) -> None:
    present = valid(ATTR + STRING, '<r z="v">x</r>')
    absent = valid(ATTR + STRING, "<r>x</r>")

    present_data, present_imported = round_trip(present, format)
    absent_data, absent_imported = round_trip(absent, format)
    assert present_data != absent_data
    assert data_attributes(present_imported.root) == {"z": "v"}
    assert data_attributes(absent_imported.root) == {}
    assert present_imported.root.text_value() == absent_imported.root.text_value() == "x"


@pytest.mark.parametrize("format", FORMATS)
def test_empty_text_and_absent_text_remain_distinct_where_the_format_can(
    format: FormatName,
) -> None:
    empty_scalar = valid(STRING, "<r></r>")
    empty_route = valid(EMPTY, "<r/>")

    scalar_data, scalar_imported = round_trip(empty_scalar, format)
    route_data, route_imported = round_trip(empty_route, format)
    assert scalar_imported.root.text_value() == ""
    assert route_imported.root.text_value() in (None, "")

    if format == "jsonml":
        assert scalar_data == ["r", ""]
        assert route_data == ["r"]
    if format == "pyxsd":
        assert scalar_data == {"$": ""}
        assert route_data == {"$": None}
    if format == "badgerfish":
        assert scalar_data != route_data


def test_unique_attribute_exports_an_ordinary_scalar_in_native_and_profile() -> None:
    document = valid(
        f'<attribute name="z">{INTEGER}</attribute>{STRING}',
        '<r z="3">x</r>',
    )

    assert document.root.attribute_value("z") == 3
    assert not isinstance(document.root.attribute_value("z"), AlternativeValue)

    raw = document.to_dict()
    assert raw == {"@z": 3, "$": "x"}
    assert isinstance(raw["@z"], int)
    assert not isinstance(raw["@z"], AlternativeValue)

    native = document.to_data(format="pyxsd")
    profile = document.to_data(format="pyxsd-json")
    assert native == {"@z": 3, "$": "x"}
    assert profile == {"r": {"@z": 3, "#content": "x"}}
    assert "#value" not in json.dumps(native)
    assert "#value" not in json.dumps(profile)
    assert json.loads(document.to_json(format="pyxsd-json")) == profile

    for format, data in (("pyxsd", native), ("pyxsd-json", profile)):
        validates(document.schema, data, format=format, fidelity="strict")


def test_ordered_formats_preserve_mixed_text_tail_and_child_order() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b<i>y</i>c</r>")

    for format in ORDERED:
        _data, imported = round_trip(document, format)
        assert lexical_tree(imported.root) == lexical_tree(document.root)


def test_profile_round_trip_keeps_expanded_names_and_attributes() -> None:
    content = (
        '<element name="child" ns="urn:a"><data type="string"/></element>'
        '<attribute name="lang" ns="http://www.w3.org/XML/1998/namespace">'
        f"{STRING}</attribute>" + A
    )
    document = valid(content, '<r xmlns:a="urn:a" xml:lang="en"><a:child>v</a:child><a>1</a></r>')
    _data, imported = round_trip(document, "pyxsd-json", fidelity="strict")

    assert imported.root.children[0]._name_ == "{urn:a}child"
    assert data_attributes(imported.root) == {f"{{{XML_NS}}}lang": "en"}
    assert imported.root.children[0].text_value() == "v"


def test_repeated_interleaved_and_ordered_children_round_trip() -> None:
    content = (
        "<interleave>"
        f"<zeroOrMore>{A}</zeroOrMore>"
        '<zeroOrMore><element name="b"><data type="string"/></element></zeroOrMore>'
        "</interleave>"
    )
    document = valid(content, "<r><b>x</b><a>1</a><b>y</b><a>2</a></r>")

    for format in ("pyxsd-json", "jsonml"):
        data, imported = round_trip(document, format, fidelity="strict")
        assert lexical_tree(imported.root) == lexical_tree(document.root)
        assert data

    data, native = round_trip(document, "pyxsd", fidelity="strict")
    assert sorted(child._name_ for child in native.root.children) == sorted(
        child._name_ for child in document.root.children
    )
    assert sorted(child.text_value() for child in native.root.children) == sorted(
        child.text_value() for child in document.root.children
    )
    assert data

    for format in ("parker", "badgerfish"):
        with pytest.raises(PyXSDError, match=r"^order-ambiguous at"):
            document.to_data(format=format, fidelity="strict")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            data = document.to_data(format=format, fidelity="best-effort")
        assert "order-ambiguous" in warning_prefixes(caught), format
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", PyXSDWarning)
            imported = document.schema.from_data(
                jsonable(data), format=format, fidelity="best-effort"
            )
        assert not imported.report.has_errors


def test_recursive_route_round_trips_in_every_format() -> None:
    leaf = '<element name="leaf"><data type="string"/></element>'
    schema = grammar_for(
        '<start><ref name="node"/></start>'
        '<define name="node"><element name="n">'
        '<optional><attribute name="k"><data type="integer"/></attribute></optional>'
        '<zeroOrMore><choice><ref name="node"/>' + leaf + "</choice></zeroOrMore>"
        "</element></define>"
    )
    document = schema.parse(io.StringIO('<n k="1"><leaf>a</leaf><n k="2"><leaf>b</leaf></n></n>'))
    assert document.is_valid

    for format in ("pyxsd-json", "jsonml"):
        data = document.to_data(format=format, fidelity="strict")
        imported = schema.from_data(jsonable(data), format=format, fidelity="strict")
        assert imported.is_valid
        assert imported.to_data(format=format, fidelity="strict") == data


def test_generic_wildcard_descendants_round_trip() -> None:
    document = valid(
        "<zeroOrMore><element><anyName/>"
        '<optional><attribute name="n"><text/></attribute></optional>'
        f"{STRING}</element></zeroOrMore>",
        '<r><x n="1">v</x><y>w</y></r>',
    )

    data, imported = round_trip(document, "pyxsd-json", fidelity="strict")
    assert data
    assert [child._name_ for child in imported.root.children] == ["x", "y"]
    assert imported.root.children[0].text_value() == "v"

    for format in ("pyxsd", "parker", "badgerfish"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            generic = document.to_data(format=format, fidelity="best-effort")
        imported_generic = document.schema.from_data(
            jsonable(generic), format=format, fidelity="best-effort"
        )
        assert not imported_generic.report.has_errors
        assert "order-ambiguous" in warning_prefixes(caught), format


def test_named_and_wildcard_fields_coexist_and_round_trip() -> None:
    content = (
        '<element name="a"><data type="integer"/></element>'
        "<zeroOrMore><element><anyName><except><name>a</name></except></anyName>"
        f"{STRING}</element></zeroOrMore>"
    )
    document = valid(content, "<r><a>1</a><x>v</x><y>w</y></r>")

    data, imported = round_trip(document, "pyxsd-json", fidelity="strict")
    assert data
    assert [child._name_ for child in imported.root.children] == ["a", "x", "y"]

    for format in ("pyxsd", "parker", "badgerfish"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            grouped = document.to_data(format=format, fidelity="best-effort")
        imported_grouped = document.schema.from_data(
            jsonable(grouped), format=format, fidelity="best-effort"
        )
        assert not imported_grouped.report.has_errors
        assert sorted(child._name_ for child in imported_grouped.root.children) == [
            "a",
            "x",
            "y",
        ]
        assert "order-ambiguous" in warning_prefixes(caught)


def test_root_selectors_narrow_the_start_name_class() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}"><start><choice>'
            '<element name="a" ns="urn:a"><data type="string"/></element>'
            '<element name="b"><data type="string"/></element>'
            "</choice></start></grammar>"
        )
    )
    assert not schema.report.has_errors

    profile = schema.from_data({"Q{urn:a}a": "x"}, format="pyxsd-json", element="a")
    assert profile.root._name_ == "{urn:a}a"
    native = schema.from_data("1", format="parker", element="{urn:a}a")
    assert native.root._name_ == "{urn:a}a"
    jsonml = schema.from_data(["b", "y"], format="jsonml", element="b")
    assert jsonml.root._name_ == "b"

    with pytest.raises(PyXSDError, match="root"):
        schema.from_data({"Q{urn:a}a": "x"}, format="pyxsd-json", element="b")
    with pytest.raises(PyXSDError, match="root"):
        schema.from_data({"Q{urn:a}a": "x"}, format="pyxsd-json", element="missing")


def test_qname_values_and_attributes_keep_scoped_context() -> None:
    content = (
        '<optional><attribute name="q"><choice><data type="QName"/><data type="string"/>'
        "</choice></attribute></optional>"
        "<choice><data type='QName'/><data type='string'/></choice>"
    )
    document = valid(content, '<r xmlns:p="urn:a" q="p:a">p:x</r>')

    for format in ("pyxsd-json", "pyxsd", "badgerfish", "jsonml"):
        data, imported = round_trip(document, format, fidelity="strict")
        value = imported.root.scalar_value()
        assert isinstance(value, AlternativeValue)
        assert value.lexical == "p:x"
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name() == "{urn:a}x"
            for candidate in value.candidates
        )
        attribute = imported.root.attribute_value("q")
        assert isinstance(attribute, AlternativeValue)
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name() == "{urn:a}a"
            for candidate in attribute.candidates
        )
        assert data


def test_list_and_heterogeneous_list_round_trip() -> None:
    homogeneous = valid("<list><oneOrMore><data type='integer'/></oneOrMore></list>", "<r>1 2</r>")
    heterogeneous = valid(
        "<list><oneOrMore><choice><data type='integer'/><data type='string'/></choice>"
        "</oneOrMore></list>",
        "<r>1 word 2</r>",
    )

    for document in (homogeneous, heterogeneous):
        for format in FORMATS:
            data, imported = round_trip(document, format)
            assert imported.root.text_value() == document.root.text_value()
            assert data


def test_ambiguous_atomic_and_list_scalars_round_trip_with_complete_candidates() -> None:
    content = (
        "<choice><data type='integer'/><list><oneOrMore><data type='integer'/>"
        "</oneOrMore></list></choice>"
    )
    document = valid(content, "<r>1 2</r>")
    original = document.root.scalar_value()
    assert isinstance(original, AlternativeValue)
    assert len(original.candidates) == 1

    for format in FORMATS:
        data, imported = round_trip(document, format)
        rebuilt = imported.root.scalar_value()
        assert rebuilt.lexical == original.lexical
        assert tuple(candidate.candidate_id for candidate in rebuilt.candidates) == tuple(
            candidate.candidate_id for candidate in original.candidates
        )
        assert data


def test_nondecodable_candidates_are_tagged_or_kept_lexical_without_inventing_values() -> None:
    content = "<choice><text/><data type='integer'/></choice>"
    document = valid(content, "<r>12</r>")
    value = document.root.scalar_value()
    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 2
    assert {candidate.decodable for candidate in value.candidates} == {False, True}

    for format in ("pyxsd", "pyxsd-json"):
        data = document.to_data(format=format, fidelity="strict")
        body = data["$"] if format == "pyxsd" else data["r"]
        assert "#value" in body
        imported = document.schema.from_data(jsonable(data), format=format)
        assert not imported.report.has_errors
        rebuilt = imported.root.scalar_value()
        assert tuple(candidate.candidate_id for candidate in rebuilt.candidates) == tuple(
            candidate.candidate_id for candidate in value.candidates
        )

    for format in ("parker", "badgerfish", "jsonml"):
        data, imported = round_trip(document, format)
        assert "12" in json.dumps(data)
        assert not imported.report.has_errors


def test_strict_losses_raise_and_best_effort_warns_with_only_represented_information() -> None:
    mixed = valid(MIXED_I, "<r>a<i>x</i>b</r>")
    attributes = valid(ATTR + STRING, '<r z="v">x</r>')
    interleaved = valid(
        "<interleave><zeroOrMore><element name='a'><data type='integer'/></element>"
        "</zeroOrMore><zeroOrMore><element name='b'><data type='string'/></element>"
        "</zeroOrMore></interleave>",
        "<r><b>x</b><a>1</a><b>y</b></r>",
    )
    qname = valid('<data type="QName"/>', '<r xmlns:p="urn:p">p:v</r>')

    loss_cases = [
        (mixed, "pyxsd", "mixed-content"),
        (mixed, "parker", "mixed-content"),
        (mixed, "badgerfish", "mixed-content"),
        (attributes, "parker", "attributes-lost"),
        (interleaved, "parker", "order-ambiguous"),
        (interleaved, "badgerfish", "order-ambiguous"),
        (qname, "pyxsd", "namespace-lost"),
        (qname, "parker", "namespace-lost"),
    ]
    for document, format, code in loss_cases:
        with pytest.raises(PyXSDError) as strict:
            document.to_data(format=format, fidelity="strict")
        assert str(strict.value).startswith(f"{code} at "), str(strict.value)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fallback = document.to_data(format=format, fidelity="best-effort")
        prefixes = warning_prefixes(caught)
        assert code in prefixes, (format, prefixes)
        assert jsonable(fallback) == fallback
        if code == "namespace-lost":
            # The grouped formats cannot carry a QName binding; the fallback
            # preserves the lexical characters and still requires the caller's
            # scoped namespace frame to reconstruct the expanded value.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", PyXSDWarning)
                imported = document.schema.from_data(
                    jsonable(fallback),
                    format=format,
                    fidelity="best-effort",
                    namespaces={"p": "urn:p"},
                )
            assert imported.root.scalar_value() == QName("p:v")
            continue
        imported = document.schema.from_data(
            jsonable(fallback), format=format, fidelity="best-effort"
        )
        assert not imported.report.has_errors


def test_native_strict_and_best_effort_cover_mixed_order_and_qname_losses() -> None:
    mixed = valid(MIXED_I, "<r>before<i>x</i>after</r>")
    with pytest.raises(PyXSDError, match=r"^mixed-content at"):
        mixed.to_data(format="pyxsd", fidelity="strict")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert mixed.to_data(format="pyxsd", fidelity="best-effort") == {
            "i": "x",
            "$": "beforeafter",
        }
    assert warning_prefixes(caught) == ["mixed-content"]

    generic = valid(
        "<zeroOrMore><element><anyName/><data type='string'/></element></zeroOrMore>",
        "<r><x>v</x><y>w</y></r>",
    )
    with pytest.raises(PyXSDError, match=r"^order-ambiguous at"):
        generic.to_data(format="pyxsd", fidelity="strict")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        generic.to_data(format="pyxsd", fidelity="best-effort")
    assert warning_prefixes(caught) == ["order-ambiguous"]

    qname = valid('<data type="QName"/>', '<r xmlns:p="urn:p">p:v</r>')
    with pytest.raises(PyXSDError, match=r"^namespace-lost at"):
        qname.to_data(format="pyxsd", fidelity="strict")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert qname.to_data(format="pyxsd", fidelity="best-effort") == {"$": "p:v"}
    assert warning_prefixes(caught) == ["namespace-lost"]


# ---------------------------------------------------------------------------
# 4. Native options and modes
# ---------------------------------------------------------------------------


def test_native_typed_raw_and_json_safe_contracts_are_distinct() -> None:
    document = valid(ALT, "<r>12</r>")

    raw = document.to_dict()
    assert isinstance(raw["$"], AlternativeValue)
    safe = document.to_data(format="pyxsd")
    assert safe["$"] == {"#value": safe["$"]["#value"]}
    assert safe["$"]["#value"]["lexical"] == "12"
    lexical = document.to_data(format="pyxsd", typed=False)
    assert lexical == {"$": "12"}
    assert "12" in document.to_json(format="pyxsd")
    assert "12" in document.to_json(format="pyxsd", typed=False)


def test_native_always_list_and_untyped_modes_round_trip() -> None:
    document = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    expected = {
        ("typed",): {"a": 1, "b": "x"},
        ("untyped",): {"a": "1", "b": "x"},
        ("always",): {"a": [1], "b": ["x"]},
        ("untyped-always",): {"a": ["1"], "b": ["x"]},
    }
    options = {
        ("typed",): {},
        ("untyped",): {"typed": False},
        ("always",): {"always_list": True},
        ("untyped-always",): {"typed": False, "always_list": True},
    }
    for key, data in expected.items():
        selected = options[key]
        assert document.to_data(format="pyxsd", **selected) == data
        imported = document.schema.from_data(jsonable(data), format="pyxsd")
        assert not imported.report.has_errors
        assert imported.to_data(format="pyxsd", **selected) == data
        assert document.to_dict(**selected) == data


def test_native_raw_values_keep_python_wrappers_while_json_uses_tags() -> None:
    document = valid(ALT, "<r>12</r>")
    raw = document.to_dict()
    assert isinstance(raw["$"], AlternativeValue)
    assert json.loads(document.to_json(format="pyxsd"))["$"]["#value"]["candidates"] == sorted(
        candidate.candidate_id for candidate in raw["$"].candidates
    )


# ---------------------------------------------------------------------------
# 5. Hostile and stale tags
# ---------------------------------------------------------------------------


def test_malformed_tag_shapes_versions_and_contexts_are_rejected() -> None:
    document = valid(ALT, "<r>12</r>")
    good = document.to_data(format="pyxsd-json", fidelity="strict")
    body = good["r"]["#value"]

    def mutate(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        data = copy.deepcopy(good)
        change(data["r"]["#value"])
        return data

    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("scalar-tag-invalid", "/r", {"r": {"#value": 1}}),
        ("scalar-tag-invalid", "/r", {"r": {"#value": body, "extra": 1}}),
        (
            "scalar-tag-invalid",
            "/r",
            mutate(lambda value: value.pop("lexical")),
        ),
        (
            "scalar-tag-invalid",
            "/r",
            mutate(lambda value: value.__setitem__("extra", 1)),
        ),
        ("scalar-tag-version", "/r", mutate(lambda value: value.__setitem__("version", 2))),
        ("scalar-tag-version", "/r", mutate(lambda value: value.__setitem__("version", "1"))),
        ("scalar-tag-version", "/r", mutate(lambda value: value.__setitem__("version", True))),
        ("scalar-tag-grammar", "/r", mutate(lambda value: value.__setitem__("grammar", ""))),
        ("scalar-tag-lexical", "/r", mutate(lambda value: value.__setitem__("lexical", 1))),
        ("scalar-tag-candidates", "/r", mutate(lambda value: value.__setitem__("candidates", []))),
        (
            "scalar-tag-candidates",
            "/r",
            mutate(
                lambda value: value.__setitem__(
                    "candidates", [value["candidates"][0], value["candidates"][0]]
                )
            ),
        ),
        (
            "scalar-tag-candidates",
            "/r",
            mutate(
                lambda value: value.__setitem__("candidates", list(reversed(value["candidates"])))
            ),
        ),
        (
            "scalar-tag-context",
            "/r",
            mutate(lambda value: value["context"].__setitem__("extra", "x")),
        ),
        (
            "scalar-tag-context",
            "/r",
            mutate(lambda value: value.__setitem__("context", {"namespaces": {}})),
        ),
        (
            "scalar-tag-context",
            "/r",
            mutate(lambda value: value["context"]["namespaces"].__setitem__("xml", "urn:x")),
        ),
    ]
    for code, pointer, data in cases:
        conversion_error(
            code,
            pointer,
            lambda data=data: document.schema.from_data(data, format="pyxsd-json"),
        )


def test_forged_grammar_candidates_and_complete_sets_never_filter_branches() -> None:
    document = valid(ALT, "<r>12</r>")
    good = document.to_data(format="pyxsd-json", fidelity="strict")

    for field, value, code in (
        ("grammar", "urn:forged", "scalar-tag-grammar-mismatch"),
        ("candidates", ["forged-id"], "scalar-tag-candidates-mismatch"),
        (
            "candidates",
            sorted(good["r"]["#value"]["candidates"] + ["forged-id"]),
            "scalar-tag-candidates-mismatch",
        ),
        (
            "candidates",
            good["r"]["#value"]["candidates"][:1],
            "scalar-tag-candidates-mismatch",
        ),
    ):
        data = copy.deepcopy(good)
        data["r"]["#value"][field] = value
        conversion_error(
            code,
            "/r",
            lambda data=data: document.schema.from_data(data, format="pyxsd-json"),
        )


def test_lexical_and_context_claims_are_verified_against_complete_evidence() -> None:
    document = valid(ALT, "<r>12</r>")
    claim = replace(tag_claim(document), location=ScalarTagLocation((), content_index=0))
    other_lexical = valid(ALT, "<r>13</r>")
    conversion_error(
        "scalar-tag-lexical-mismatch",
        "/r",
        lambda: verify_tag_claims([claim], other_lexical),
    )

    content = "<choice><data type='QName'/><data type='string'/></choice>"
    namespaced = valid(content, '<r xmlns:p="urn:a">p:x</r>')
    namespaced_claim = replace(
        tag_claim(namespaced), location=ScalarTagLocation((), content_index=0)
    )
    other_context = valid(content, '<r xmlns:p="urn:b">p:x</r>')
    conversion_error(
        "scalar-tag-context-mismatch",
        "/r",
        lambda: verify_tag_claims([namespaced_claim], other_context),
    )


def test_duplicate_and_unbound_claim_locations_fail_explicitly() -> None:
    content = (
        "<zeroOrMore><element name='a'><choice><data type='integer'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    schema = schema_for(content)
    one = schema.parse(io.StringIO("<r><a>12</a></r>"), typed=True)
    data = one.to_data(format="pyxsd-json", fidelity="strict")
    claim = parse_scalar_tag(data["r"][0], "/r/0")
    two = schema.parse(io.StringIO("<r><a>12</a><a>12</a></r>"), typed=True)
    none = schema.parse(io.StringIO("<r><a>13</a></r>"), typed=True)

    conversion_error(
        "scalar-tag-occurrence-ambiguous",
        "/r/0",
        lambda: verify_tag_claims([claim], two),
    )
    conversion_error(
        "scalar-tag-occurrence-mismatch",
        "/r/0",
        lambda: verify_tag_claims([claim], none),
    )

    tree = data_to_relaxng_tree(schema, data)
    location = tree.claims[0].location
    assert isinstance(location, ScalarTagLocation)
    duplicate = replace(tree.claims[0], location=location)
    conversion_error(
        "scalar-tag-location-duplicate",
        "/r/0",
        lambda: verify_tag_claims([replace(tree.claims[0], location=location), duplicate], one),
    )


def test_tags_are_rejected_in_non_scalar_positions() -> None:
    content = '<element name="c"><element name="x"><data type="integer"/></element></element>'
    document = valid(content, "<r><c><x>1</x></c></r>")
    tag = {
        "#value": {
            "version": 1,
            "grammar": "g",
            "lexical": "1",
            "context": {"base_uri": None, "namespaces": {}},
            "candidates": ["c"],
        }
    }

    cases = [
        ("pyxsd", {"c": {"#value": tag}}),
        ("pyxsd-json", {"r": [{"c": [{"#value": tag}]}]}),
        ("parker", {"c": {"#value": tag}}),
        ("badgerfish", {"r": {"c": {"#value": tag}}}),
        ("jsonml", ["r", ["c", ["#value", tag]]]),
    ]
    for format, data in cases:
        with pytest.raises(PyXSDError):
            document.schema.from_data(data, format=format)  # type: ignore[arg-type]

    for format, data in (
        ("pyxsd", {"#value": tag}),
        ("pyxsd-json", {"#value": tag}),
        ("badgerfish", {"#value": tag}),
        ("jsonml", ["#value", tag]),
    ):
        with pytest.raises(PyXSDError):
            document.schema.from_data(data, format=format)  # type: ignore[arg-type]


def test_tags_on_invalid_documents_leave_authority_to_the_report() -> None:
    schema = schema_for("<choice><data type='integer'/><data type='decimal'/></choice>")
    document = schema.parse(io.StringIO("<r>12</r>"), typed=True)
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    data["r"]["#value"]["lexical"] = "not-a-number"
    data["r"]["#value"]["candidates"] = ["forged"]

    imported = schema.from_data(data, format="pyxsd-json")
    assert imported.report.has_errors
    assert imported.binding_evidence is None

    with pytest.raises(ValidationError):
        imported.to_data(format="pyxsd-json")
    assert imported.to_data(format="jsonml") == ["r", "not-a-number"]


def test_stale_and_mutated_typed_documents_fail_while_lexical_paths_show_current_tree() -> None:
    document = valid(INTEGER, "<r>12</r>")
    document.root.set_text("13")

    with pytest.raises(StaleInterpretationError):
        document.to_data(format="pyxsd-json")
    with pytest.raises(StaleInterpretationError):
        document.to_data(format="pyxsd")
    assert document.to_data(format="pyxsd", typed=False) == {"$": "13"}
    assert document.to_data(format="jsonml") == ["r", "13"]
    assert document.report.to_json()


def test_claims_cannot_authorize_a_resource_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a conversion tag context attempted resource access")

    monkeypatch.setattr(resource_policy, "read_resource", forbidden)
    monkeypatch.setattr(remote.HttpClientTransport, "fetch", forbidden)

    document = valid(ALT, "<r>12</r>")
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    data["r"]["#value"]["context"]["base_uri"] = "file:///definitely/not/read"

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    assert imported.root.base_uri == "file:///definitely/not/read"
    assert imported.to_data(format="pyxsd-json", fidelity="strict") == data


# ---------------------------------------------------------------------------
# 6. Mutation, invalid input, revalidation, and audit immutability
# ---------------------------------------------------------------------------


def test_typed_exports_fail_after_mutation_and_invalid_parse() -> None:
    dirty = valid(INTEGER, "<r>12</r>")
    dirty.root.set_text("13")
    invalid = parse(INTEGER, "<r>bad</r>")

    for document, error in ((dirty, StaleInterpretationError), (invalid, ValidationError)):
        for format in ("pyxsd", "pyxsd-json", "parker", "badgerfish"):
            with pytest.raises(error):
                document.to_data(format=format)
        assert document.to_data(format="pyxsd", typed=False)
        assert document.to_data(format="jsonml")
        assert document.report.to_json()
        assert document.to_string()


def test_revalidation_restores_typed_conversion() -> None:
    dirty = valid(INTEGER, "<r>12</r>")
    dirty.root.set_text("13")
    assert dirty.interpretations_current is False
    with pytest.raises(StaleInterpretationError):
        dirty.to_data()

    revalidated = dirty.revalidate()
    assert revalidated.interpretations_current is True
    assert revalidated.root.text_value() == "13"
    assert revalidated.to_data(format="pyxsd-json") == {"r": 13}
    assert revalidated.to_data(format="jsonml") == ["r", 13]

    invalid = dirty.revalidate()
    invalid.root.set_text("not-a-number")
    again = invalid.revalidate()
    assert again.report.has_errors
    with pytest.raises(ValidationError):
        again.to_data()


def test_exports_and_schema_generation_do_not_mutate_the_document() -> None:
    document = valid(ALT + ATTR, '<r z="v">12</r>')
    before_string = document.to_string()
    before_revision = document.root.revision.revision_id
    before_dirty = document.root.revision.dirty
    before_report = document.report.to_json()

    for format in FORMATS:
        for fidelity in ("strict", "best-effort"):
            with suppress(PyXSDError):
                document.to_data(format=format, fidelity=fidelity)
        with suppress(PyXSDError):
            document.schema.json_schema(format=format)
        with suppress(ValueError):
            document.to_data(format=format, fidelity="best-effort", typed=False)

    assert document.to_string() == before_string
    assert document.root.revision.revision_id == before_revision
    assert document.root.revision.dirty == before_dirty
    assert document.report.to_json() == before_report


# ---------------------------------------------------------------------------
# 7. Scoped contexts
# ---------------------------------------------------------------------------


def test_sibling_tags_keep_their_own_namespace_and_base_context() -> None:
    content = (
        "<zeroOrMore><element name='q'><choice><data type='QName'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    document = valid(content, '<r xmlns:p="urn:a"><q>p:x</q><q xmlns:p="urn:b">p:y</q></r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")

    first = data["r"][0]["#value"]
    second = data["r"][1]["#value"]
    assert first["context"]["namespaces"] == {"p": "urn:a"}
    assert second["context"]["namespaces"] == {"p": "urn:b"}

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    for child, uri in zip(imported.root.children, ("urn:a", "urn:b"), strict=True):
        value = child.scalar_value()
        assert value.context.namespaces["p"] == uri
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name()
            == f"{{{uri}}}" + ("x" if uri == "urn:a" else "y")
            for candidate in value.candidates
        )


def test_sibling_tags_keep_separate_base_uri_contexts() -> None:
    base = f'<optional><attribute name="base" ns="{XML_NS}"><text/></attribute></optional>'
    content = (
        base
        + "<zeroOrMore><element name='q'>"
        + base
        + "<choice><data type='QName'/><data type='string'/></choice>"
        + "</element></zeroOrMore>"
    )
    document = valid(
        content,
        '<r xmlns:p="urn:a" xml:base="http://example.com/base/">'
        '<q xml:base="a/">p:x</q>'
        '<q xmlns:p="urn:b" xml:base="b/">p:y</q></r>',
    )
    data = document.to_data(format="pyxsd-json", fidelity="strict")

    first, second = data["r"]["q"]
    assert first["#content"]["#value"]["context"] == {
        "base_uri": "http://example.com/base/a/",
        "namespaces": {"p": "urn:a"},
    }
    assert second["#content"]["#value"]["context"] == {
        "base_uri": "http://example.com/base/b/",
        "namespaces": {"p": "urn:b"},
    }

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    for child, base_uri, uri, local in zip(
        imported.root.children,
        ("http://example.com/base/a/", "http://example.com/base/b/"),
        ("urn:a", "urn:b"),
        ("x", "y"),
        strict=True,
    ):
        assert child.base_uri == base_uri
        value = child.scalar_value()
        assert value.context.base_uri == base_uri
        assert value.context.namespaces["p"] == uri
        assert any(
            isinstance(candidate.decoded, QName)
            and candidate.decoded.to_expanded_name() == f"{{{uri}}}{local}"
            for candidate in value.candidates
        )


def test_a_sibling_tags_context_cannot_leak() -> None:
    content = (
        "<zeroOrMore><element name='q'><choice><data type='QName'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    document = valid(content, '<r xmlns:p="urn:a"><q>p:x</q><q>x</q></r>')
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    data["r"][1]["#value"]["context"] = {"base_uri": None, "namespaces": {}}

    imported = document.schema.from_data(data, format="pyxsd-json")
    assert not imported.report.has_errors
    first, second = imported.root.children
    assert first.scalar_value().context.namespaces == {"p": "urn:a"}
    assert second.scalar_value().context.namespaces == {}
    assert second.base_uri is None
    assert any(
        isinstance(candidate.decoded, QName) and candidate.decoded.to_expanded_name() == "x"
        for candidate in second.scalar_value().candidates
    )


def test_a_tag_context_scope_mismatch_is_rejected_per_occurrence() -> None:
    content = (
        "<zeroOrMore><element name='q'><choice><data type='QName'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    document = valid(content, '<r xmlns:p="urn:a"><q>p:x</q><q xmlns:p="urn:b">p:y</q></r>')
    claim = parse_scalar_tag(
        document.to_data(format="pyxsd-json", fidelity="strict")["r"][1], "/r/1"
    )
    bound = replace(claim, location=ScalarTagLocation((1,), content_index=0))
    other = valid(content, '<r xmlns:p="urn:c"><q>p:x</q><q xmlns:p="urn:d">p:y</q></r>')
    conversion_error(
        "scalar-tag-context-mismatch",
        "/r/1",
        lambda: verify_tag_claims([bound], other),
    )


# ---------------------------------------------------------------------------
# 8. Error pointers
# ---------------------------------------------------------------------------


def test_shape_errors_carry_codes_and_json_pointers() -> None:
    schema = schema_for(f"{A}{B}")
    cases = [
        ("pyxsd", [1], "container-invalid", ""),
        ("pyxsd", {"a": 1}, "order-ambiguous", ""),
        ("pyxsd-json", [1], "container-invalid", ""),
        ("pyxsd-json", {"r": {"b": "x"}}, "container-invalid", "/r"),
        ("parker", None, "container-invalid", ""),
        ("badgerfish", [1], "container-invalid", ""),
        ("badgerfish", {"r": None}, "container-invalid", "/r"),
        ("jsonml", {}, "container-invalid", ""),
        ("jsonml", ["r", None], "container-invalid", "/1"),
    ]
    for format, data, code, pointer in cases:
        conversion_error(
            code,
            pointer,
            lambda data=data, format=format: schema.from_data(data, format=format),  # type: ignore[arg-type]
        )


def test_tag_mismatch_errors_point_at_the_json_input_not_constructed_xml() -> None:
    content = (
        "<zeroOrMore><element name='a'><choice><data type='integer'/>"
        "<data type='string'/></choice></element></zeroOrMore>"
    )
    document = valid(content, "<r><a>12</a></r>")
    data = document.to_data(format="pyxsd-json", fidelity="strict")
    data["r"][0]["#value"]["grammar"] = "urn:forged"

    conversion_error(
        "scalar-tag-grammar-mismatch",
        "/r/0",
        lambda: document.schema.from_data(data, format="pyxsd-json"),
    )

    profile = valid(ALT, "<r>12</r>")
    tag = profile.to_data(format="pyxsd-json", fidelity="strict")["r"]
    tag["#value"]["version"] = 7
    conversion_error(
        "scalar-tag-version",
        "/r",
        lambda: profile.schema.from_data({"r": tag}, format="pyxsd-json"),
    )


def test_json_text_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    schema = schema_for(INTEGER)
    for source in ('{"r":1,"r":2}', '{"r":NaN}', '{"r":Infinity}', '{"r":-Infinity}'):
        with pytest.raises(PyXSDError) as caught:
            schema.from_json(source, format="pyxsd-json")
        assert str(caught.value).startswith(("json-duplicate-key at ''", "json-invalid at ''"))


def test_native_lexical_escape_uses_the_current_unknown_root_and_warns() -> None:
    document = valid(f"{A}", "<r><a>1</a></r>")
    document.root.set_name("unknown")

    with pytest.raises(PyXSDError, match=r"^order-ambiguous at ''"):
        document.to_data(format="pyxsd", typed=False, fidelity="strict")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert document.to_data(format="pyxsd", typed=False, fidelity="best-effort") == {"a": "1"}
    assert warning_prefixes(caught) == ["order-ambiguous"]
    assert "no compiled child order" in str(caught[0].message)
    assert document.to_data(format="jsonml") == ["unknown", ["a", "1"]]


def test_root_and_null_errors_point_at_the_json_input() -> None:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<grammar xmlns="{NS}" datatypeLibrary="{XSD}"><start><choice>'
            f'<element name="r">{INTEGER}</element><element name="s">{INTEGER}</element>'
            "</choice></start></grammar>"
        )
    )
    assert not schema.report.has_errors

    conversion_error(
        "root-mismatch",
        "",
        lambda: schema.from_data({"r": 1}, format="pyxsd-json", element="s"),
    )
    conversion_error(
        "root-unresolved",
        "",
        lambda: schema.from_data({"r": 1}, format="pyxsd-json", element="nope"),
    )
    conversion_error(
        "scalar-type-invalid",
        "/r",
        lambda: schema.from_data({"r": None}, format="pyxsd-json"),
    )
    conversion_error(
        "scalar-type-invalid",
        "",
        lambda: schema_for(INTEGER).from_data(None, format="parker"),
    )


# ---------------------------------------------------------------------------
# 9. JSON Schema agreement
# ---------------------------------------------------------------------------


SCHEMA_CASES = [
    ("scalar", INTEGER, "<r>12</r>"),
    ("empty", EMPTY, "<r/>"),
    ("optional-attributes", ATTR + STRING, '<r z="v">x</r>'),
    ("record", f"<interleave>{A}{B}</interleave>", "<r><b>B</b><a>1</a></r>"),
    ("repeat", f"<zeroOrMore>{A}</zeroOrMore>", "<r><a>1</a><a>2</a></r>"),
    ("mixed", MIXED_I, "<r>a<i>x</i>b</r>"),
    ("alternative", ALT, "<r>12</r>"),
    ("nondecodable", "<choice><text/><data type='integer'/></choice>", "<r>12</r>"),
    ("list", "<list><oneOrMore><data type='integer'/></oneOrMore></list>", "<r>1 2</r>"),
    (
        "wildcard",
        "<zeroOrMore><element><anyName/><data type='string'/></element></zeroOrMore>",
        "<r><x>v</x><y>w</y></r>",
    ),
]

#: Documented strict-only representation losses in this matrix.  Anything else
#: is an unexpected failure; every best-effort export must succeed.
EXPECTED_STRICT_LOSSES: dict[tuple[str, FormatName], frozenset[str]] = {
    ("optional-attributes", "parker"): frozenset({"attributes-lost"}),
    **{
        ("mixed", format): frozenset({"mixed-content"})
        for format in ("pyxsd", "parker", "badgerfish")
    },
    **{
        ("wildcard", format): frozenset({"order-ambiguous"})
        for format in ("pyxsd", "parker", "badgerfish")
    },
}


@pytest.mark.parametrize(
    ("case", "content", "xml"), [(name, content, xml) for name, content, xml in SCHEMA_CASES]
)
@pytest.mark.parametrize("format", FORMATS)
def test_every_actual_export_validates_under_every_fidelity_and_option(
    case: str, content: str, xml: str, format: FormatName
) -> None:
    document = valid(content, xml)
    option_sets: list[dict[str, Any]] = [{}]
    if format == "pyxsd":
        option_sets += [
            {"typed": False},
            {"always_list": True},
            {"typed": False, "always_list": True},
        ]

    expected_loss = EXPECTED_STRICT_LOSSES.get((case, format), frozenset())
    successes = 0
    for fidelity in ("strict", "best-effort"):
        for options in option_sets:
            if fidelity == "strict" and expected_loss:
                with pytest.raises(PyXSDError) as caught:
                    document.to_data(format=format, fidelity=fidelity, **options)  # type: ignore[arg-type]
                code = str(caught.value).split(" at ", 1)[0]
                assert code in expected_loss, (case, format, options, caught.value)
                continue

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", PyXSDWarning)
                    data = document.to_data(  # type: ignore[arg-type]
                        format=format, fidelity=fidelity, **options
                    )
            except PyXSDError as error:
                pytest.fail(
                    f"unexpected {fidelity} export failure for {case}/{format}/{options}: {error}"
                )
            validates(
                document.schema,
                data,
                format=format,
                fidelity=fidelity,
                **options,
            )
            successes += 1

    expected_successes = len(option_sets) * (1 if expected_loss else 2)
    assert successes == expected_successes, (case, format, successes)


def test_negative_shapes_fail_for_every_format() -> None:
    schema = schema_for(f"<interleave>{A}{B}</interleave>")
    document = valid(f"<interleave>{A}{B}</interleave>", "<r><a>1</a><b>x</b></r>")
    assert not document.report.has_errors

    negatives: dict[FormatName, dict[str, object]] = {
        "pyxsd": {
            "wrong-root": {"nope": 1},
            "container": [],
            "missing": {"a": 1},
            "cardinality": {"a": [1, 1], "b": "x"},
            "bool": {"a": True, "b": "x"},
            "null": {"a": None, "b": "x"},
            "xmlns": {"@xmlns": "urn:x", "a": 1, "b": "x"},
        },
        "pyxsd-json": {
            "wrong-root": {"nope": {"a": 1, "b": "x"}},
            "container": [],
            "missing": {"r": {"a": 1}},
            "cardinality": {"r": {"a": [1, 1], "b": "x"}},
            "bool": {"r": {"a": True, "b": "x"}},
            "null": {"r": {"a": None, "b": "x"}},
            "xmlns": {"r": {"@xmlns": "urn:x", "a": 1, "b": "x"}},
        },
        "parker": {
            "container": [],
            "missing": {"a": 1},
            "cardinality": {"a": [1, 1], "b": "x"},
            "bool": {"a": True, "b": "x"},
            "null": {"a": None, "b": "x"},
        },
        "badgerfish": {
            "wrong-root": {"nope": {}},
            "container": [],
            "missing": {"r": {"a": 1}},
            "cardinality": {"r": {"a": [1, 1], "b": "x"}},
            "bool": {"r": {"a": True, "b": "x"}},
            "null": {"r": {"a": None, "b": "x"}},
            "xmlns": {"r": {"@xmlns": {"p": "urn:x"}, "a": 1, "b": "x"}},
        },
        "jsonml": {
            "wrong-root": ["nope", ["a", 1], ["b", "x"]],
            "container": {},
            "missing": ["r", ["a", 1]],
            "cardinality": ["r", ["a", 1], ["a", 1], ["b", "x"]],
            "bool": ["r", ["a", True], ["b", "x"]],
            "null": ["r", ["a", None], ["b", "x"]],
            "xmlns": ["r", {"xmlns:p": "urn:x"}, ["a", 1], ["b", "x"]],
        },
    }

    for format, cases in negatives.items():
        for data in cases.values():
            rejects(schema, data, format=format)


def test_tag_positions_and_jsonml_attribute_placement_fail() -> None:
    record = schema_for(f"<interleave>{A}{B}</interleave>")
    rejects(record, ["r", ["a", 1], {"b": "x"}], format="jsonml")

    content = '<element name="c"><element name="x"><data type="integer"/></element></element>'
    structured = schema_for(content)
    tag = {
        "#value": {
            "version": 1,
            "grammar": "g",
            "lexical": "1",
            "context": {"base_uri": None, "namespaces": {}},
            "candidates": ["c"],
        }
    }
    rejects(structured, {"c": {"#value": tag}}, format="pyxsd")
    rejects(structured, {"r": [{"c": [{"#value": tag}]}]}, format="pyxsd-json")
    rejects(structured, {"r": {"c": {"#value": tag}}}, format="badgerfish")
    rejects(structured, ["r", ["c", ["#value", tag]]], format="jsonml")


def test_sequence_particle_order_is_a_disclosed_limitation() -> None:
    sequence = schema_for(f"{A}{B}")
    for format in ("pyxsd-json", "jsonml"):
        reasons = sequence.json_schema(format=format)["x-pyxsd-validation"]["unvalidated"]
        assert "relaxng-particle-order" in reasons, format


def test_schema_envelope_metadata_and_disclosures_are_complete() -> None:
    document = valid(ALT, "<r>12</r>")
    for format in FORMATS:
        generated = document.schema.json_schema(format=format)
        assert generated["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert generated["x-pyxsd-format"] == format
        assert generated["x-pyxsd-profile"] == "relaxng"
        assert generated["x-pyxsd-profile-version"] == 1
        validation = generated["x-pyxsd-validation"]
        assert validation["level"] == "structural"
        reasons = validation["unvalidated"]
        assert reasons == sorted(reasons)
        assert {
            "relaxng-datatype-context",
            "relaxng-grammar-validity",
            "relaxng-name-class",
            "relaxng-scalar-candidates",
        } <= set(reasons)


def test_schema_does_not_freeze_grammar_or_candidate_identifiers() -> None:
    document = valid(ALT, "<r>12</r>")
    value = document.root.scalar_value()
    assert isinstance(value, AlternativeValue)
    identifiers = [
        value.grammar_fingerprint,
        *(candidate.candidate_id for candidate in value.candidates),
    ]

    for format in FORMATS:
        serialized = json.dumps(document.schema.json_schema(format=format))
        for identifier in identifiers:
            assert identifier not in serialized, (format, identifier)


def test_route_less_jsonml_wildcard_minimum_is_enforced_or_disclosed() -> None:
    schema = schema_for("<oneOrMore><element><anyName/><data type='string'/></element></oneOrMore>")
    route_id = schema.projection.roots[0]
    route = schema.projection.routes[route_id]
    wildcard = route.wildcards[0]
    plan = replace(
        schema.projection,
        routes={
            **schema.projection.routes,
            route_id: replace(route, wildcards=(replace(wildcard, child_route_id=None),)),
        },
    )
    from pyxsd.relaxng.json_schema import build_relaxng_json_schema

    generated = build_relaxng_json_schema(type("Plan", (), {"projection": plan})(), format="jsonml")
    Draft202012Validator.check_schema(generated)
    exported = valid(
        "<oneOrMore><element><anyName/><data type='string'/></element></oneOrMore>",
        "<r><x>v</x></r>",
    ).to_data(format="jsonml")
    assert list(Draft202012Validator(generated).iter_errors(exported)) == []
    assert list(Draft202012Validator(generated).iter_errors(["r"]))
    assert "relaxng-wildcard-cardinality" not in generated["x-pyxsd-validation"]["unvalidated"]

    declared = schema_for(
        '<element name="b"><data type="integer"/></element>'
        "<oneOrMore><element><anyName><except><name>b</name></except></anyName>"
        f"{STRING}</element></oneOrMore>"
    )
    declared_route_id = declared.projection.roots[0]
    declared_route = declared.projection.routes[declared_route_id]
    declared_plan = replace(
        declared.projection,
        routes={
            **declared.projection.routes,
            declared_route_id: replace(
                declared_route,
                wildcards=(replace(declared_route.wildcards[0], child_route_id=None),),
            ),
        },
    )
    disclosed = build_relaxng_json_schema(
        type("Plan", (), {"projection": declared_plan})(), format="jsonml"
    )
    reasons = disclosed["x-pyxsd-validation"]["unvalidated"]
    assert "relaxng-wildcard-cardinality" in reasons
    assert (
        list(
            Draft202012Validator(disclosed).iter_errors(
                valid(
                    '<element name="b"><data type="integer"/></element>'
                    "<oneOrMore><element><anyName><except><name>b</name></except></anyName>"
                    f"{STRING}</element></oneOrMore>",
                    "<r><b>1</b><x>v</x></r>",
                ).to_data(format="jsonml")
            )
        )
        == []
    )


def test_known_representation_limits_are_disclosed_in_schema_reasons() -> None:
    parker = valid(ATTR + STRING, '<r z="v">x</r>')
    reasons = parker.schema.json_schema(format="parker")["x-pyxsd-validation"]["unvalidated"]
    assert "relaxng-parker-attribute-loss" in reasons

    grouped = valid(f"{A}{B}", "<r><a>1</a><b>x</b></r>")
    assert (
        "relaxng-particle-order"
        in grouped.schema.json_schema(format="parker")["x-pyxsd-validation"]["unvalidated"]
    )

    lexical = valid(
        '<element name="child" ns="urn:a"><data type="string"/></element>',
        '<r xmlns:a="urn:a"><a:child>v</a:child></r>',
    )
    assert (
        "relaxng-local-name-mapping"
        in lexical.schema.json_schema(format="parker")["x-pyxsd-validation"]["unvalidated"]
    )
    assert (
        "relaxng-lexical-name-binding"
        in lexical.schema.json_schema(format="jsonml")["x-pyxsd-validation"]["unvalidated"]
    )


# ---------------------------------------------------------------------------
# 10. Representation limits in failures
# ---------------------------------------------------------------------------


def test_every_conversion_failure_names_a_concrete_limit() -> None:
    document = valid(MIXED_I, "<r>a<i>x</i>b</r>")
    messages: list[str] = []

    for format in ("pyxsd", "parker", "badgerfish"):
        with pytest.raises(PyXSDError) as caught:
            document.to_data(format=format, fidelity="strict")
        messages.append(str(caught.value))

    attributes = valid(ATTR + STRING, '<r z="v">x</r>')
    with pytest.raises(PyXSDError) as caught:
        attributes.to_data(format="parker", fidelity="strict")
    messages.append(str(caught.value))

    interleaved = valid(
        "<interleave><zeroOrMore><element name='a'><data type='integer'/></element>"
        "</zeroOrMore><zeroOrMore><element name='b'><data type='string'/></element>"
        "</zeroOrMore></interleave>",
        "<r><b>x</b><a>1</a><b>y</b></r>",
    )
    with pytest.raises(PyXSDError) as caught:
        interleaved.to_data(format="parker", fidelity="strict")
    messages.append(str(caught.value))

    for message in messages:
        lowered = message.lower()
        assert "not implemented" not in lowered
        assert "nonunique" not in lowered and "non-unique" not in lowered
        assert "adapter" not in lowered
        assert "not supported" not in lowered
        assert " at " in message and ":" in message

    assert any("mixed" in message for message in messages)
    assert any("attributes" in message for message in messages)
    assert any("order" in message for message in messages)


def test_expected_limitations_are_named_in_errors_or_warnings() -> None:
    generic = valid(
        "<zeroOrMore><element><anyName/><data type='string'/></element></zeroOrMore>",
        "<r><x>v</x><y>w</y></r>",
    )
    with pytest.raises(PyXSDError) as strict:
        generic.to_data(format="pyxsd", fidelity="strict")
    assert "one accepted compiled child order" in str(strict.value)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        generic.to_data(format="pyxsd", fidelity="best-effort")
    assert warning_prefixes(caught) == ["order-ambiguous"]
    assert "at ''" in str(caught[0].message)

    scalar = valid(INTEGER, "<r>12</r>")
    generated = scalar.schema.json_schema(format="jsonml")
    reasons = generated["x-pyxsd-validation"]["unvalidated"]
    assert "relaxng-grammar-validity" in reasons
    assert "relaxng-name-class" in reasons
