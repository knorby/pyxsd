"""Coverage-rounding tests for the core model, identity and document paths.

The schema-driven tests pin user-visible contracts (the error codes on the
report, the bound value, the exported document); the unit tests exercise
pure helpers with small ``Particle``/duck-typed stand-ins, mirroring
``tests/test_misc_coverage.py`` and ``tests/test_identity_internals.py``.
"""

import xml.etree.ElementTree as ET
from io import StringIO
from types import SimpleNamespace

import pytest

import pyxsd.schema  # noqa: F401  (first import loads the ER stack in its safe order)
from pyxsd import derivation
from pyxsd import xsd_data_types as dt
from pyxsd.binding import ParseModes
from pyxsd.content_model import (
    Particle,
    _contributes_no_elements,
    _RunEnds,
    all_extension_occurrence,
)
from pyxsd.exceptions import PyXSDError
from pyxsd.extensions import Extensions
from pyxsd.identity import _tokenContributions, _wildcardResolvedType
from pyxsd.schema import Schema, load_overlay_classes, parse
from pyxsd.schema_base import SchemaBase
from pyxsd.wildcards import WildcardSpec

XSD = "http://www.w3.org/2001/XMLSchema"
HEAD = f"<xs:schema xmlns:xs='{XSD}'>"
TAIL = "</xs:schema>"

NOTE_SCHEMA = (
    HEAD + "<xs:element name='note' type='Note'/>"
    "<xs:complexType name='Note'><xs:sequence>"
    "<xs:element name='to' type='xs:string'/>"
    "</xs:sequence></xs:complexType>" + TAIL
)


def full_parse(body, xml, tmp_path):
    """Run the complete pipeline (schema build plus instance binding)."""
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"
    schema_path.write_text(HEAD + body + TAIL, encoding="utf-8")
    instance_path.write_text(xml, encoding="utf-8")
    return Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))


def _type_ers(schema):
    """Every type ER of *schema*, keyed by local name."""
    ers = {}
    for entries in schema.components.values():
        for er in entries:
            if type(er).__name__ in ("SimpleType", "ComplexType"):
                ers[er.name] = er
    return ers


# ---------------------------------------------------------------------------
# content-model compiler helpers
# ---------------------------------------------------------------------------


def test_contributes_no_elements_classifies_particles():
    """Leaf particles carry elements; a group only does so through its children."""
    assert _contributes_no_elements(Particle("element", 1, 1, [], "a")) is False
    assert _contributes_no_elements(Particle("any", 0, None)) is False
    assert _contributes_no_elements(Particle("sequence", 1, 1, [])) is True
    assert (
        _contributes_no_elements(Particle("choice", 1, 1, [Particle("element", 1, 1, [], "a")]))
        is False
    )
    assert _contributes_no_elements(Particle("choice", 1, 1, [])) is True
    assert (
        _contributes_no_elements(Particle("all", 1, 1, [Particle("element", 1, 1, [], "a")]))
        is False
    )


def test_all_extension_occurrence_folds_a_group_reference_occurrence():
    inner = Particle("all", 3, 1, [Particle("element", 1, 1, [], "a")])
    wrapper = Particle("sequence", 2, 1, [inner], synthetic=True)
    assert all_extension_occurrence(wrapper) == (inner, 6)


def test_run_ends_compares_as_a_set_and_renders_its_interval():
    run = _RunEnds(0, 3)
    assert run == _RunEnds(0, 3)
    assert run != _RunEnds(0, 4)
    assert run == frozenset({0, 1, 2})
    assert run != {0, 1}
    assert repr(run) == "_RunEnds(0, 3)"


def test_run_ends_yields_not_implemented_for_foreign_operands():
    run = _RunEnds(0, 2)
    assert run.__and__(object()) is NotImplemented
    assert run.__eq__(object()) is NotImplemented


# ---------------------------------------------------------------------------
# derivation checks
# ---------------------------------------------------------------------------


class _MarkerlessType(SchemaBase):
    """A generated class whose chain reaches the ur-type without a marker."""


def test_blocked_step_ignores_the_implicit_ur_type_step():
    assert derivation._blocked_step(_MarkerlessType, dt.Integer, frozenset({"extension"})) is False


def test_any_atomic_type_rejects_the_ur_type_family():
    assert derivation.is_valid_xsi_type(dt.AnyType, dt.AnyAtomicType) == "not-derived"
    assert derivation.is_valid_xsi_type(dt.AnySimpleType, dt.AnyAtomicType) == "not-derived"
    assert derivation.is_valid_xsi_type(dt.AnyAtomicType, dt.AnyAtomicType) == "not-derived"


def test_any_atomic_type_rejects_union_and_list_overrides():
    class MemberUnion(dt.XsdDataType):
        _unionMembers = (dt.Integer,)

    class IntList(dt.XsdList):
        itemType = dt.Integer

    assert derivation.is_valid_xsi_type(MemberUnion, dt.AnyAtomicType) == "not-derived"
    assert derivation.is_valid_xsi_type(IntList, dt.AnyAtomicType) == "not-derived"


# ---------------------------------------------------------------------------
# extension path resolution
# ---------------------------------------------------------------------------

EXT_SCHEMA = (
    HEAD + "<xs:complexType name='Base'><xs:sequence>"
    "<xs:element name='a' type='xs:string'/>"
    "</xs:sequence></xs:complexType>"
    "<xs:complexType name='Derived'><xs:complexContent>"
    "<xs:extension base='Base'><xs:sequence>"
    "<xs:element name='b' type='xs:string'/></xs:sequence>"
    "</xs:extension></xs:complexContent></xs:complexType>"
    "<xs:element name='G' type='Base'/>"
    "<xs:element name='Doc'>"
    "<xs:annotation><xs:documentation>annotated</xs:documentation></xs:annotation>"
    "<xs:complexType><xs:sequence>"
    "<xs:element name='child' type='Base'/>"
    "<xs:element name='bare'/>"
    "</xs:sequence></xs:complexType>"
    "</xs:element>"
    "<xs:element name='RefDoc'><xs:complexType><xs:sequence>"
    "<xs:element ref='G'/>"
    "</xs:sequence></xs:complexType></xs:element>" + TAIL
)


def test_extension_paths_reach_inline_annotations_and_bare_type_names():
    class ClarkBaseMixin:
        pass

    class ChildMixin:
        pass

    class RefMixin:
        pass

    ext = Extensions()
    ext.for_kind("complexType", derived_from="{urn:irrelevant}Base", mixin=ClarkBaseMixin)
    ext.for_path("/Doc/child", mixin=ChildMixin)
    ext.for_path("/RefDoc/G", mixin=RefMixin)
    schema = Schema.compile(StringIO(EXT_SCHEMA), extensions=ext)

    # ``Derived`` extends an unprefixed ``Base``: the Clark spelling of the
    # selector still matches by local part.
    derived_cls = schema.classes["Derived"]
    assert issubclass(derived_cls, ClarkBaseMixin)
    # Both paths resolve onto the global ``Base`` type.
    base_cls = schema.classes["Base"]
    assert issubclass(base_cls, ChildMixin)
    assert issubclass(base_cls, RefMixin)


def test_extension_path_naming_no_segments_is_rejected():
    class Mixin:
        pass

    ext = Extensions()
    ext.for_path("/", mixin=Mixin)
    with pytest.raises(PyXSDError, match="names no element declarations"):
        Schema.compile(StringIO(EXT_SCHEMA), extensions=ext)


def test_extension_path_with_a_duplicate_name_is_rejected():
    class Mixin:
        pass

    ambiguous = (
        HEAD + "<xs:element name='Root'><xs:complexType><xs:choice>"
        "<xs:element name='shared'>"
        "<xs:complexType><xs:attribute name='k' type='xs:string'/></xs:complexType></xs:element>"
        "<xs:element name='shared'>"
        "<xs:complexType><xs:attribute name='k' type='xs:string'/></xs:complexType></xs:element>"
        "</xs:choice></xs:complexType></xs:element>" + TAIL
    )
    ext = Extensions()
    ext.for_path("/Root/shared", mixin=Mixin)
    with pytest.raises(PyXSDError, match="is ambiguous"):
        Schema.compile(StringIO(ambiguous), extensions=ext)


def test_extension_path_through_a_builtin_typed_element_dangles():
    class Mixin:
        pass

    builtin_mid = (
        HEAD + "<xs:element name='doc'><xs:complexType><xs:sequence>"
        "<xs:element name='mid' type='xs:string'/>"
        "</xs:sequence></xs:complexType></xs:element>" + TAIL
    )
    ext = Extensions()
    ext.for_path("/doc/mid/x", mixin=Mixin)
    with pytest.raises(PyXSDError, match="has no element declaration named 'x'"):
        Schema.compile(StringIO(builtin_mid), extensions=ext)


def test_extension_path_to_a_typeless_element_is_rejected():
    class Mixin:
        pass

    ext = Extensions()
    ext.for_path("/Doc/bare", mixin=Mixin)
    with pytest.raises(PyXSDError, match="of no schema-declared type"):
        Schema.compile(StringIO(EXT_SCHEMA), extensions=ext)


def test_extension_path_through_a_clark_name_reference():
    namespaced = (
        f"<xs:schema xmlns:xs='{XSD}' xmlns:m='http://example.com/m' "
        "targetNamespace='http://example.com/m' elementFormDefault='qualified'>"
        "<xs:complexType name='Leaf'><xs:sequence>"
        "<xs:element name='q' type='xs:string'/>"
        "</xs:sequence></xs:complexType>"
        "<xs:element name='Root'><xs:complexType><xs:sequence>"
        "<xs:element name='leaf' type='m:Leaf'/>"
        "</xs:sequence></xs:complexType></xs:element></xs:schema>"
    )

    class LeafMixin:
        pass

    ext = Extensions()
    ext.for_path("/Root/leaf", mixin=LeafMixin)
    schema = Schema.compile(StringIO(namespaced), extensions=ext, mode=ParseModes.NAMESPACED)
    assert issubclass(schema.classes["Leaf"], LeafMixin)


def test_declaration_type_er_needs_a_component_table():
    element = SimpleNamespace(
        isElementRef=False,
        processedChildren=(),
        resolvedTypeName=lambda: "T",
        getSchema=lambda: SimpleNamespace(components=None),
    )
    assert Extensions._declaration_type_er(element) is None


def test_mixin_hook_returns_an_unsupported_finding():
    ext = Extensions()

    @ext.for_type("Note")
    class NoteMixin:
        @staticmethod
        def __pyxsd_validate__(instance):
            return [42]

    schema = Schema.compile(StringIO(NOTE_SCHEMA), extensions=ext, mode=ParseModes.NAMESPACED)
    doc = schema.parse(StringIO("<note><to>you</to></note>"))
    errors = [issue for issue in doc.report.errors if issue.code == "extension-error"]
    assert len(errors) == 1
    assert "unsupported finding of type int" in errors[0].message


# ---------------------------------------------------------------------------
# identity-constraint internals
# ---------------------------------------------------------------------------


def test_wildcard_resolved_type_falls_back_to_the_collected_specs():
    spec = WildcardSpec(namespace="##any", process_contents="lax")
    node_type = type(
        "WildcardNode",
        (SchemaBase,),
        {
            "hasWildcardAttributes_": True,
            "wildcardAttributeSpecs_": [spec],
            "_parseMode_": ParseModes.NAMESPACED,
            "schema": SimpleNamespace(_host=SimpleNamespace(components=None)),
        },
    )
    assert _wildcardResolvedType(node_type(), "attr") is None


def test_root_id_value_bound_by_its_own_attribute_is_not_reported(tmp_path):
    doc = full_parse(
        "<xs:element name='r'><xs:complexType><xs:simpleContent>"
        "<xs:extension base='xs:ID'>"
        "<xs:attribute name='x' type='xs:ID'/>"
        "</xs:extension></xs:simpleContent></xs:complexType></xs:element>",
        "<r x='abc'>abc</r>",
        tmp_path,
    )
    assert not any(issue.code == "idref-unresolved" for issue in doc.report.errors)
    assert not any(issue.code == "id-duplicate" for issue in doc.report.errors)
    assert doc.root.x == "abc"


def test_idref_values_are_recorded_and_unresolved_reads_reported(tmp_path):
    """An IDREF-typed value contributes a reference (never a binding)."""
    doc = full_parse(
        "<xs:element name='r'><xs:complexType><xs:sequence>"
        "<xs:element name='ref' type='xs:IDREF'/>"
        "</xs:sequence></xs:complexType></xs:element>",
        "<r><ref>missing</ref></r>",
        tmp_path,
    )
    unresolved = [issue for issue in doc.report.errors if issue.code == "idref-unresolved"]
    assert len(unresolved) == 1
    assert "IDREF 'missing'" in unresolved[0].message
    assert doc.root.ref == "missing"


class _ExplodingItemTypeMeta(type):
    """A metaclass whose ``itemType`` lookup fails on every read."""

    @property
    def itemType(cls):
        raise TypeError("itemType is not available")


class _ExplodingItemList(dt.XsdList, metaclass=_ExplodingItemTypeMeta):
    """A list type whose item declaration cannot be read."""


def test_token_contributions_survives_a_broken_list_item_type():
    assert _tokenContributions(_ExplodingItemList, "a") is None


# ---------------------------------------------------------------------------
# Schema facade and overlay loading
# ---------------------------------------------------------------------------


def test_parse_rebuilds_a_missing_context_and_tolerates_a_missing_host():
    schema = Schema.compile(StringIO(NOTE_SCHEMA), mode=ParseModes.NAMESPACED)
    schema._schema_context = None
    schema._host = None
    doc = schema.parse(StringIO("<note><to>you</to></note>"))
    assert doc.root is not None
    assert doc.is_valid
    assert schema._schema_context is not None


def test_module_parse_accepts_a_parsed_element_tree():
    doc = parse(ET.fromstring("<note><to>you</to></note>"), xsd=StringIO(NOTE_SCHEMA))
    assert doc.root is not None
    assert doc.is_valid


def test_overlay_classes_resolve_a_module_name_in_the_fallback_dir(tmp_path):
    schema = Schema.compile(StringIO(NOTE_SCHEMA))
    module_path = tmp_path / "task8_overlay.py"
    module_path.write_text(
        "from pyxsd.schema_base import SchemaBase\n\n\n"
        "class Thing(SchemaBase):\n"
        "    name = 'Thing'\n",
        encoding="utf-8",
    )
    load_overlay_classes(schema, "task8_overlay", fallback_dir=tmp_path)
    assert schema.classes["Thing"].schema is schema


def test_overlay_file_without_a_loader_is_rejected(tmp_path):
    schema = Schema.compile(StringIO(NOTE_SCHEMA))
    bad = tmp_path / "task8_overlay.xyz"
    bad.write_text("value = 1", encoding="utf-8")
    with pytest.raises(ImportError, match="could not be loaded"):
        load_overlay_classes(schema, str(bad))


def test_schema_file_that_is_not_well_formed_is_rejected(tmp_path):
    bad = tmp_path / "broken.xsd"
    bad.write_text("<xs:schema", encoding="utf-8")
    with pytest.raises(PyXSDError, match="not well-formed XML"):
        Schema.compile(str(bad))


# ---------------------------------------------------------------------------
# Document API fallbacks
# ---------------------------------------------------------------------------


def test_document_without_a_root_refuses_to_export_or_query():
    doc = Schema.compile(StringIO(NOTE_SCHEMA)).parse(StringIO("<memo/>"))
    assert doc.root is None
    with pytest.raises(PyXSDError, match="no root to write"):
        doc.to_string()
    with pytest.raises(PyXSDError, match="no root to query"):
        doc.find("note")


def test_find_returns_none_for_a_missing_path():
    doc = Schema.compile(StringIO(NOTE_SCHEMA)).parse(StringIO("<note><to>you</to></note>"))
    assert doc.find("missing") is None
    assert doc.find("to")._name_ == "to"
