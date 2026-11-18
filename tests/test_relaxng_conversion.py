"""Public RELAX NG conversion dispatch and freshness gates.

Task 1 installs no concrete format adapters. These tests replace the internal
registry with recording fakes to prove that every public format reaches a
RELAX NG-owned adapter with the XSD option contract, that typed exports require
valid current evidence before adapter invocation, and that the lexical escape
paths observe the actual dirty or invalid tree. Production entries must raise
an explicit RELAX NG unsupported-adapter error rather than falling through to
an XSD converter.
"""

from __future__ import annotations

import inspect
import io
import json
from typing import Any

import pytest

import pyxsd
import pyxsd.data_conversion.export as xsd_data_export
import pyxsd.data_conversion.import_data as xsd_data_import
import pyxsd.dict_export as xsd_dict_export
from pyxsd.data_conversion.contracts import FormatName
from pyxsd.document import Document
from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.relaxng import RelaxNGDocument, RelaxNGSchema, StaleInterpretationError, conversion
from pyxsd.relaxng.conversion import (
    ExportMode,
    RelaxNGUnsupportedAdapterError,
    export_relaxng,
)

NS = "http://relaxng.org/ns/structure/1.0"
XSD_DATATYPES = "http://www.w3.org/2001/XMLSchema-datatypes"

FORMATS: tuple[FormatName, ...] = (
    "pyxsd",
    "pyxsd-json",
    "parker",
    "badgerfish",
    "jsonml",
)

# Formats whose typed export has to materialize schema-directed values.
TYPED_FORMATS: tuple[FormatName, ...] = (
    "pyxsd",
    "pyxsd-json",
    "parker",
    "badgerfish",
)

INTEGER_SCHEMA = f"""<grammar xmlns="{NS}">
  <start>
    <element name="doc">
      <data type="integer" datatypeLibrary="{XSD_DATATYPES}"/>
    </element>
  </start>
</grammar>"""

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


class _FakeAdapter:
    """Recording adapter whose payloads prove dispatch, mode, and options."""

    def __init__(
        self,
        format: FormatName,
        *,
        requires_current_typed_evidence: bool | None = None,
    ) -> None:
        self.format = format
        self.requires_current_typed_evidence = (
            format != "jsonml"
            if requires_current_typed_evidence is None
            else requires_current_typed_evidence
        )
        self.raw_payload: Any = {"format": format, "mode": "raw"}
        self.json_safe_payload: Any = {"format": format, "mode": "json-safe"}
        self.calls: list[dict[str, object]] = []
        self.seen: list[tuple[RelaxNGDocument, Any, Any]] = []

    def export(
        self,
        document: RelaxNGDocument,
        *,
        fidelity: str,
        typed: bool,
        always_list: bool,
        mode: ExportMode,
    ) -> Any:
        self.calls.append(
            {
                "fidelity": fidelity,
                "typed": typed,
                "always_list": always_list,
                "mode": mode,
            }
        )
        self.seen.append((document, document.root, document.root.revision))
        payload = self.raw_payload if mode == "raw" else self.json_safe_payload
        return payload(document) if callable(payload) else payload


def _schema() -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(io.StringIO(INTEGER_SCHEMA))
    assert not schema.report.has_errors, schema.report
    return schema


def _typed_document(xml: str = "<doc>12</doc>") -> RelaxNGDocument:
    document = _schema().parse(io.StringIO(xml), typed=True)
    assert isinstance(document, RelaxNGDocument)
    return document


def _invalid_typed_document() -> RelaxNGDocument:
    document = _typed_document("<doc>oops</doc>")
    assert document.report.has_errors
    return document


@pytest.fixture
def adapters(monkeypatch: pytest.MonkeyPatch) -> dict[FormatName, _FakeAdapter]:
    """Install one recording fake per format for the current test only."""
    installed: dict[FormatName, _FakeAdapter] = {}
    for format in FORMATS:
        adapter = _FakeAdapter(format)
        monkeypatch.setitem(conversion._ADAPTERS, format, adapter)
        installed[format] = adapter
    return installed


def test_production_registry_covers_every_format_with_relaxng_adapters() -> None:
    expected_evidence = {
        "pyxsd": True,
        "pyxsd-json": True,
        "parker": True,
        "badgerfish": True,
        "jsonml": False,
    }

    assert set(conversion._ADAPTERS) == set(FORMATS)
    for format, adapter in conversion._ADAPTERS.items():
        assert adapter.format == format
        assert hasattr(adapter, "export")
        assert adapter.requires_current_typed_evidence is expected_evidence[format]


def test_invalid_and_dirty_jsonml_is_lexical_while_gated_formats_fail() -> None:
    invalid = _invalid_typed_document()
    with pytest.raises(ValidationError):
        export_relaxng(invalid, format="badgerfish")
    with pytest.raises(RelaxNGUnsupportedAdapterError):
        export_relaxng(invalid, format="jsonml")

    dirty = _typed_document()
    dirty.root.set_text("13")
    with pytest.raises(StaleInterpretationError):
        export_relaxng(dirty, format="parker")
    with pytest.raises(RelaxNGUnsupportedAdapterError):
        export_relaxng(dirty, format="jsonml")


@pytest.mark.parametrize("method", ["to_dict", "to_data", "to_json"])
def test_public_signatures_match_xsd_defaults(method: str) -> None:
    xsd_signature = inspect.signature(getattr(Document, method))
    relaxed_signature = inspect.signature(getattr(RelaxNGDocument, method))

    assert [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in relaxed_signature.parameters.values()
    ] == [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in xsd_signature.parameters.values()
    ]
    assert relaxed_signature.return_annotation == xsd_signature.return_annotation


def test_every_format_dispatches_to_its_relaxng_adapter(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()

    for format in FORMATS:
        adapter = adapters[format]

        assert document.to_data(format=format) == {"format": format, "mode": "json-safe"}
        assert adapter.calls == [
            {
                "fidelity": "best-effort",
                "typed": True,
                "always_list": False,
                "mode": "json-safe",
            }
        ]

        adapter.calls.clear()
        assert export_relaxng(
            document,
            format=format,
            fidelity="strict",
            typed=True,
            always_list=False,
        ) == {"format": format, "mode": "json-safe"}
        assert adapter.calls == [
            {
                "fidelity": "strict",
                "typed": True,
                "always_list": False,
                "mode": "json-safe",
            }
        ]


def test_to_json_routes_each_adapter_and_encodes_its_value(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()

    for format in FORMATS:
        encoded = document.to_json(format=format, indent=2)

        assert encoded == json.dumps({"format": format, "mode": "json-safe"}, indent=2)
        assert adapters[format].calls == [
            {
                "fidelity": "best-effort",
                "typed": True,
                "always_list": False,
                "mode": "json-safe",
            }
        ]


def test_to_dict_routes_native_and_preserves_raw_python_values(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    sentinel = object()
    adapters["pyxsd"].raw_payload = {"value": sentinel, "mode": "raw"}
    adapters["pyxsd"].json_safe_payload = {"value": "json-safe", "mode": "json-safe"}
    document = _typed_document()

    exported = document.to_dict()

    assert isinstance(exported, dict)
    assert exported["value"] is sentinel
    assert adapters["pyxsd"].calls == [
        {
            "fidelity": "best-effort",
            "typed": True,
            "always_list": False,
            "mode": "raw",
        }
    ]
    assert document.to_data(format="pyxsd")["value"] == "json-safe"
    assert adapters["pyxsd"].calls[-1]["mode"] == "json-safe"

    adapters["pyxsd"].calls.clear()
    assert document.to_dict(typed=False, always_list=True)["value"] is sentinel
    assert adapters["pyxsd"].calls == [
        {
            "fidelity": "best-effort",
            "typed": False,
            "always_list": True,
            "mode": "raw",
        }
    ]


def test_native_lexical_options_pass_through_the_adapter(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()

    assert document.to_data(format="pyxsd", typed=False, always_list=True) == {
        "format": "pyxsd",
        "mode": "json-safe",
    }
    assert adapters["pyxsd"].calls == [
        {
            "fidelity": "best-effort",
            "typed": False,
            "always_list": True,
            "mode": "json-safe",
        }
    ]


@pytest.mark.parametrize("format", ["pyxsd-json", "parker", "badgerfish", "jsonml"])
@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only data-export option"),
        ({"always_list": True}, "always_list is a native-only data-export option"),
    ],
)
def test_nonnative_nondefault_options_raise_exactly_before_dispatch(
    adapters: dict[FormatName, _FakeAdapter],
    format: FormatName,
    options: dict[str, bool],
    message: str,
) -> None:
    document = _typed_document()
    adapter = adapters[format]

    for call in (
        lambda: document.to_data(format=format, **options),
        lambda: document.to_json(format=format, **options),
        lambda: export_relaxng(document, format=format, **options),
    ):
        with pytest.raises(ValueError) as raised:
            call()
        assert str(raised.value) == message

    assert adapter.calls == []


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"typed": False}, "typed is a native-only data-export option"),
        ({"always_list": True}, "always_list is a native-only data-export option"),
    ],
)
def test_native_only_option_errors_match_xsd_exactly(
    adapters: dict[FormatName, _FakeAdapter],
    options: dict[str, bool],
    message: str,
) -> None:
    relaxed = _typed_document()
    xsd = pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA)).parse(
        io.StringIO('<root a="1"><item>2</item></root>')
    )

    with pytest.raises(ValueError) as relaxed_error:
        relaxed.to_data(format="jsonml", **options)
    with pytest.raises(ValueError) as xsd_error:
        xsd.to_data(format="jsonml", **options)

    assert type(relaxed_error.value) is type(xsd_error.value)
    assert str(relaxed_error.value) == str(xsd_error.value) == message
    assert adapters["jsonml"].calls == []


def test_invalid_format_and_fidelity_are_rejected_before_dispatch(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()

    with pytest.raises(ValueError, match="unknown data format"):
        document.to_data(format="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown data format"):
        export_relaxng(document, format="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown fidelity mode"):
        document.to_data(fidelity="lossy")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown fidelity mode"):
        export_relaxng(document, fidelity="lossy")  # type: ignore[arg-type]

    assert all(not adapter.calls for adapter in adapters.values())


def test_invalid_typed_documents_fail_before_any_typed_adapter(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _invalid_typed_document()

    assert document.typed is True
    assert document.binding_evidence is None

    for format in TYPED_FORMATS:
        with pytest.raises(ValidationError) as raised:
            export_relaxng(document, format=format)
        assert raised.value.report is document.report
        with pytest.raises(ValidationError):
            document.to_data(format=format)
        assert adapters[format].calls == []


def test_dirty_and_transformed_typed_documents_fail_before_dispatch(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    dirty = _typed_document()
    dirty.root.set_text("13")
    assert dirty.interpretations_current is False

    stale = _typed_document().transform(lambda root: root)
    assert isinstance(stale, RelaxNGDocument)
    assert stale.interpretations_current is False

    for document in (dirty, stale):
        for format in TYPED_FORMATS:
            with pytest.raises(StaleInterpretationError):
                export_relaxng(document, format=format)
            assert adapters[format].calls == []


def test_validation_only_document_has_no_evidence_for_typed_export(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _schema().parse(io.StringIO("<doc>12</doc>"), typed=False)

    assert isinstance(document, RelaxNGDocument)
    assert document.typed is False
    assert document.binding_evidence is None

    with pytest.raises(PyXSDError, match="requires a typed document"):
        document.to_data()
    assert adapters["pyxsd"].calls == []


def test_native_typed_false_exports_the_current_dirty_tree(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()
    adapter = adapters["pyxsd"]
    root = document.root
    revision = root.revision
    adapter.json_safe_payload = lambda current: {"text": current.root.text_value()}
    root.set_text("13")

    assert document.to_data(format="pyxsd", typed=False) == {"text": "13"}
    assert adapter.calls == [
        {
            "fidelity": "best-effort",
            "typed": False,
            "always_list": False,
            "mode": "json-safe",
        }
    ]
    assert adapter.seen == [(document, root, revision)]
    assert document.root is root
    assert document.root.revision is revision


def test_native_typed_false_and_jsonml_export_invalid_current_tree(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _invalid_typed_document()
    root = document.root
    revision = root.revision
    adapters["pyxsd"].json_safe_payload = lambda current: {"text": current.root.text_value()}
    adapters["jsonml"].json_safe_payload = lambda current: {"text": current.root.text_value()}

    assert document.to_data(format="pyxsd", typed=False) == {"text": "oops"}
    assert adapters["pyxsd"].seen == [(document, root, revision)]

    assert document.to_data(format="jsonml") == {"text": "oops"}
    assert adapters["jsonml"].calls == [
        {
            "fidelity": "best-effort",
            "typed": True,
            "always_list": False,
            "mode": "json-safe",
        }
    ]
    assert adapters["jsonml"].seen == [(document, root, revision)]
    assert document.root is root
    assert document.root.revision is revision


def test_jsonml_stays_lexical_after_mutation(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    document = _typed_document()
    adapter = adapters["jsonml"]
    root = document.root
    revision = root.revision
    adapter.json_safe_payload = lambda current: {"text": current.root.text_value()}
    root.set_text("99")

    assert document.to_data(format="jsonml") == {"text": "99"}
    assert adapter.calls == [
        {
            "fidelity": "best-effort",
            "typed": True,
            "always_list": False,
            "mode": "json-safe",
        }
    ]
    assert adapter.seen == [(document, root, revision)]
    assert document.root is root
    assert document.root.revision is revision


def test_json_rejects_nonfinite_adapter_output_but_to_dict_stays_raw(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    nonfinite = {
        "positive": float("inf"),
        "negative": float("-inf"),
        "missing": float("nan"),
    }
    adapters["pyxsd"].raw_payload = nonfinite
    adapters["pyxsd"].json_safe_payload = nonfinite
    document = _typed_document()

    assert isinstance(document.to_dict(), dict)
    raw = document.to_dict()
    assert raw["positive"] == float("inf")
    assert document.to_data()["positive"] == float("inf")
    with pytest.raises(ValueError):
        document.to_json()
    assert [call["mode"] for call in adapters["pyxsd"].calls] == [
        "raw",
        "raw",
        "json-safe",
        "json-safe",
    ]


@pytest.mark.parametrize("format", FORMATS)
def test_unimplemented_production_adapter_raises_explicitly_without_xsd_path(
    monkeypatch: pytest.MonkeyPatch,
    format: FormatName,
) -> None:
    document = _typed_document()
    xsd_calls: list[str] = []

    def fail(name: str) -> Any:
        def reached(*args: object, **kwargs: object) -> object:
            xsd_calls.append(name)
            raise AssertionError(f"an XSD conversion path was reached: {name}")

        return reached

    monkeypatch.setattr(xsd_data_export, "bound_to_data", fail("bound_to_data"))
    monkeypatch.setattr(xsd_dict_export, "bound_to_dict", fail("bound_to_dict"))
    monkeypatch.setattr(xsd_data_import, "data_to_element", fail("data_to_element"))

    if format == "pyxsd-json":
        assert export_relaxng(document, format=format) == {"doc": 12}
    else:
        with pytest.raises(RelaxNGUnsupportedAdapterError) as raised:
            export_relaxng(document, format=format)

        assert raised.value.format == format
        assert "not implemented" in str(raised.value)
    assert xsd_calls == []


def test_native_to_dict_uses_the_relaxng_adapter_not_the_xsd_pathway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _typed_document()

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("an XSD conversion path was reached")

    monkeypatch.setattr(xsd_dict_export, "bound_to_dict", fail)
    monkeypatch.setattr(xsd_data_export, "bound_to_data", fail)

    with pytest.raises(RelaxNGUnsupportedAdapterError):
        document.to_dict()


def test_registry_injection_does_not_leak_between_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = conversion._ADAPTERS["pyxsd"]

    with monkeypatch.context() as patch:
        fake = _FakeAdapter("pyxsd")
        patch.setitem(conversion._ADAPTERS, "pyxsd", fake)
        assert conversion._ADAPTERS["pyxsd"] is fake

    assert conversion._ADAPTERS["pyxsd"] is original


def test_xsd_compile_and_export_behavior_is_unchanged(
    adapters: dict[FormatName, _FakeAdapter],
) -> None:
    schema = pyxsd.Schema.compile(io.StringIO(XSD_SCHEMA))
    document = schema.parse(io.StringIO('<root a="1"><item>2</item></root>'))

    assert document.to_dict() == {"@a": 1, "item": 2}
    assert document.to_data() == {"@a": 1, "item": 2}
    assert json.loads(document.to_json()) == {"@a": 1, "item": 2}
    assert all(not adapter.calls for adapter in adapters.values())
