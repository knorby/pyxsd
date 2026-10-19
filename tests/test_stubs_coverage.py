"""Edge paths of the stub renderer left uncovered by the feature modules.

The feature modules pin the renderer's ordinary behaviour. This module
drives the branches they never reach: aliased member accessors, names
that cannot be Python identifiers, overlay members whose signature
cannot be read, the never-raises guards around descriptor accessors and
particle lookups, and the render-time omissions (a schema with no root
elements, no classes at all, or a facet-only simple type folded onto its
user). Each test asserts the emitted text or the resolved member/alias
model, never merely that rendering ran.

Four target lines are not exercised because no input reaches them; the
task report records the proof (``stubs.py`` 436->465, 687, 1330->1332,
1517->1528).
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from types import SimpleNamespace

from pyxsd import xsd_data_types
from pyxsd.integrations._shape import particle_elements
from pyxsd.namespaces import XSD_NS
from pyxsd.schema import Schema
from pyxsd.stubs import (
    _annotation_base,
    _call,
    _called_name,
    _collect_particle_flags,
    _compact_extension_line,
    _compact_spellings,
    _declared_occurs,
    _docstring_lines,
    _effective_occurs,
    _extension_line,
    _facet_items,
    _fold_occurs,
    _inventory,
    _is_global_element,
    _is_simple_type,
    _leaf_particle,
    _Member,
    _member_lines,
    _members,
    _multiplied,
    _preferred_leaf,
    _resolved_base,
    _rewrite_typing_spellings,
    _root_alias,
    _RootAlias,
    _source_label,
    _spell_text,
    _Spelling,
    _spelling_pattern,
    _split_signature,
    _StubClass,
    _substitution_comment,
    _twin_leaves,
    _union_member_names,
    _validated_signature,
    emit_stub,
)


def compile_schema(xsd: str) -> Schema:
    return Schema.compile(io.StringIO(xsd))


def entry_names(schema: Schema) -> tuple[list, dict[int, str]]:
    """Returns a schema's inventory and the ``{id(cls): stub name}`` map."""
    inventory = _inventory(schema)
    return inventory, {id(entry.cls): entry.name for entry in inventory}


# ---------------------------------------------------------------------------
# Member model: aliased names, unusable names, degraded attribute types
# ---------------------------------------------------------------------------

DESCRIPTOR_SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="cfg"><xs:sequence>
    <xs:element name="one" type="xs:int"/></xs:sequence>
  <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:element name="root" type="cfg"/>
</xs:schema>"""

ELEMENT_ATTRIBUTE_COLLISION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="itemType"><xs:sequence>
    <xs:element name="code" type="xs:int"/></xs:sequence>
  <xs:attribute name="code" type="xs:string"/>
  </xs:complexType>
  <xs:element name="items" type="itemType"/>
</xs:schema>"""

NON_IDENTIFIER_ELEMENT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="ship-to" type="xs:string"/></xs:sequence>
  </xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""

NON_IDENTIFIER_ATTRIBUTE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="rec">
    <xs:attribute name="ship-to" type="xs:string"/>
  </xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""

UNION_ATTRIBUTE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="u"><xs:union memberTypes="xs:int xs:string"/></xs:simpleType>
  <xs:complexType name="rec">
    <xs:attribute name="a" type="u"/>
  </xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""


def test_aliased_element_member_records_its_xml_name():
    # An element sharing its name with an attribute keeps the XML name
    # but is bound under ``code_element``; the annotation alone would
    # hide which XML declaration the accessor belongs to.
    schema = compile_schema(ELEMENT_ATTRIBUTE_COLLISION)
    inventory, names = entry_names(schema)
    entry = next(e for e in inventory if e.name == "itemType")
    members = {m.name: m for m in _members(entry.cls, names, compact=False)}
    assert members["code_element"].annotation == "Int"
    assert members["code_element"].comment == "xml: 'code'"
    # The attribute keeps the natural accessor, so it carries no note.
    assert members["code"].comment is None


def test_non_identifier_attribute_member_is_comment_only():
    schema = compile_schema(NON_IDENTIFIER_ATTRIBUTE)
    inventory, names = entry_names(schema)
    entry = next(e for e in inventory if e.name == "rec")
    members = _members(entry.cls, names, compact=False)
    assert [m.name for m in members] == [""]
    assert members[0].kind == "attribute"
    assert members[0].comment == "attribute 'ship-to' is not dot-accessible (bound as 'ship-to')"


def test_union_typed_attribute_degrades_with_a_comment():
    schema = compile_schema(UNION_ATTRIBUTE)
    inventory, names = entry_names(schema)
    entry = next(e for e in inventory if e.name == "rec")
    members = {m.name: m for m in _members(entry.cls, names, compact=False)}
    assert members["a"].annotation == "Any | None"
    assert members["a"].comment == "union of: Int | String; annotated Any"


def test_member_bookkeeping_skips_non_descriptor_bindings():
    # The element/attribute name lists are rebuilt by ``__init_subclass__``
    # from the descriptors in the class body; a class whose bookkeeping
    # was patched to name a key bound to a plain value must skip that key
    # rather than hand a non-descriptor to the member resolver.
    schema = compile_schema(DESCRIPTOR_SCHEMA)
    inventory, names = entry_names(schema)
    cfg = next(e for e in inventory if e.name == "cfg").cls

    class Patched(cfg):
        pass

    Patched._elementNames_ = ["one", "ghost_element"]
    Patched._attributeNames_ = ["code", "ghost_attribute"]
    Patched.one = cfg.__dict__["one"]
    Patched.code = cfg.__dict__["code"]
    Patched.ghost_element = "shadowed"
    Patched.ghost_attribute = "shadowed"
    rendered = {m.name for m in _members(Patched, names, compact=False)}
    assert {"one", "code"} <= rendered
    assert "ghost_element" not in rendered and "ghost_attribute" not in rendered


def test_aliased_attribute_member_records_its_xml_name():
    schema = compile_schema(DESCRIPTOR_SCHEMA)
    inventory, names = entry_names(schema)
    cfg = next(e for e in inventory if e.name == "cfg").cls

    class Patched(cfg):
        pass

    Patched._attributeNames_ = ["code_element"]
    Patched.code_element = cfg.__dict__["code"]
    members = {m.name: m for m in _members(Patched, names, compact=False)}
    assert members["code_element"].annotation == "String | None"
    assert members["code_element"].comment == "xml: 'code'"


# ---------------------------------------------------------------------------
# Overlay extension members: unreadable and unannotated signatures
# ---------------------------------------------------------------------------


class _UnreadableSignature:
    """A callable whose ``__signature__`` read raises ``ValueError``."""

    @property
    def __signature__(self):
        raise ValueError("no signature")

    def __call__(self):
        raise AssertionError("the renderer must never call the getter")


def test_extension_member_degrades_when_the_signature_is_unavailable():
    schema = compile_schema(DESCRIPTOR_SCHEMA)
    inventory, names = entry_names(schema)
    cfg = next(e for e in inventory if e.name == "cfg").cls

    class Overlay(cfg):
        name = "cfg"
        write_only = property(None, lambda self, value: None)
        broken = property(_UnreadableSignature())

    members = {m.name: m for m in _members(Overlay, names, compact=False)}
    # ``write_only`` has no getter at all; ``broken``'s getter has no
    # readable signature. Both become the variadic Any shape.
    assert members["write_only"].is_property is True
    assert members["write_only"].annotation == "(self, *args, **kwargs) -> Any"
    assert members["write_only"].comment == "signature unavailable; annotated Any"
    assert members["broken"].annotation == "(self, *args, **kwargs) -> Any"
    assert members["broken"].comment == "signature unavailable; annotated Any"


def test_unannotated_overlay_return_degrades_to_any():
    schema = compile_schema(DESCRIPTOR_SCHEMA)
    inventory, names = entry_names(schema)
    cfg = next(e for e in inventory if e.name == "cfg").cls

    class Overlay(cfg):
        name = "cfg"

        def plain(self):
            return 1

        @property
        def bare(self):
            return 2

    members = {m.name: m for m in _members(Overlay, names, compact=False)}
    assert members["plain"].annotation == "(self) -> Any"
    assert members["plain"].comment is None
    # A property's unannotated getter gets the note the plain method
    # does not; both still degrade to Any.
    assert members["bare"].annotation == "(self) -> Any"
    assert members["bare"].comment == "getter return unannotated; annotated Any"


# ---------------------------------------------------------------------------
# Signature validation: the annotation shapes the walker must classify
# ---------------------------------------------------------------------------


def test_validated_signature_accepts_a_missing_return():
    assert _validated_signature("(self)", frozenset()) == ("(self)", None)


def test_quoted_annotation_is_an_unresolvable_forward_reference():
    text, problem = _validated_signature("(self) -> 'Item'", frozenset())
    assert text == "(self) -> 'Item'"
    assert problem is not None and "'Item'" in problem


def test_subscripted_unresolvable_base_reports_the_base():
    text, problem = _validated_signature("(self) -> Bare[int]", frozenset())
    assert text == "(self) -> Bare[int]"
    assert problem is not None and "Bare" in problem and "Bare[int]" not in problem


def test_non_string_constant_annotation_is_not_a_forward_reference():
    # Only a quoted string is a forward reference; a numeric literal is
    # a value the walk treats as resolvable.
    assert _validated_signature("(self) -> 5", frozenset()) == ("(self) -> 5", None)


def test_literal_arguments_are_values_not_annotations():
    assert _validated_signature("(self) -> Literal['fast']", frozenset()) == (
        "(self) -> Literal['fast']",
        None,
    )


def test_unresolvable_part_of_a_union_reports_that_part():
    text, problem = _validated_signature("(self) -> int | Bare", frozenset())
    assert text == "(self) -> int | Bare"
    assert problem is not None and "'Bare'" in problem


def test_union_tuple_and_unmodelled_annotations_are_walked():
    assert _validated_signature("(self) -> int | str", frozenset()) == (
        "(self) -> int | str",
        None,
    )
    assert _validated_signature("(self) -> tuple[int, str]", frozenset()) == (
        "(self) -> tuple[int, str]",
        None,
    )
    assert _validated_signature("(self) -> int + str", frozenset())[1] == (
        "unresolvable identifier 'int + str' in signature; annotated Any"
    )


def test_multi_attribute_annotation_is_reported_by_source():
    text, problem = _validated_signature("(self) -> pandas.core.Series", frozenset())
    assert text == "(self) -> pandas.core.Series"
    assert problem == ("unresolvable identifier 'pandas.core.Series' in signature; annotated Any")


def test_rewrite_rejects_a_span_that_is_not_the_spelling():
    assert _rewrite_typing_spellings("(self) -> List[str]", [(7, 10, "List")]) is None


# ---------------------------------------------------------------------------
# Particle helpers: occurrence folding and leaf selection
# ---------------------------------------------------------------------------


OCCURS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="order"><xs:sequence>
    <xs:element name="one" type="xs:int"/>
    <xs:element name="opt" type="xs:int" minOccurs="0"/>
    <xs:element name="many" type="xs:int" maxOccurs="unbounded"/>
  </xs:sequence></xs:complexType>
  <xs:element name="root" type="order"/>
</xs:schema>"""


def test_effective_occurs_without_a_model_uses_the_declared_values():
    order = compile_schema(OCCURS).classes["order"]
    assert _effective_occurs(None, order.__dict__["opt"]) == (0, 1)


def test_effective_occurs_with_a_foreign_descriptor_uses_the_declared_values():
    model = compile_schema(OCCURS).classes["order"].__dict__["_contentModel_"]
    foreign = compile_schema(OCCURS).classes["order"]
    assert _effective_occurs(model, foreign.__dict__["opt"]) == (0, 1)


def test_fold_occurs_returns_unfolded_when_the_edge_is_absent():
    model = compile_schema(OCCURS).classes["order"].__dict__["_contentModel_"]
    foreign = compile_schema(OCCURS).classes["order"].__dict__["_contentModel_"]
    leaf = next(p for p in particle_elements(foreign) if p.descriptor is not None)
    assert _fold_occurs(model, leaf, 1, 1) == (1, 1)


def test_leaf_particle_without_a_referred_element_is_none():
    model = compile_schema(OCCURS).classes["order"].__dict__["_contentModel_"]

    class Ref:
        isElementRef = True
        referredElement = None

    assert _leaf_particle(model, Ref()) is None


def test_twin_leaves_without_a_source_node_is_empty():
    class NoSource:
        xsdElement = None

    assert _twin_leaves([], NoSource()) == []


class _Particle:
    synthetic = False
    children = ()
    min_occurs = 1
    max_occurs = 1


class _Leaf:
    def __init__(self, minimum: int, maximum: int) -> None:
        self.min_occurs = minimum
        self.max_occurs = maximum


class _OccursDescriptor:
    def __init__(self, minimum: int, maximum: int) -> None:
        self._minimum = minimum
        self._maximum = maximum

    def getMinOccurs(self) -> int:
        return self._minimum

    def getMaxOccurs(self) -> int:
        return self._maximum


def test_preferred_leaf_without_candidates_is_none():
    assert _preferred_leaf(_Particle(), [], SimpleNamespace()) is None


def test_preferred_leaf_matches_by_occurrence_then_falls_back():
    model = _Particle()
    first, match, other = _Leaf(1, 1), _Leaf(5, 5), _Leaf(2, 2)
    # No copy-signature match (the leaves are not in the model), so the
    # exact occurrence picks the leaf; otherwise the first candidate wins.
    assert _preferred_leaf(model, [first, match], _OccursDescriptor(5, 5)) is match
    assert _preferred_leaf(model, [first, other], _OccursDescriptor(5, 5)) is first


def test_multiplied_caps_at_the_unbounded_sentinel():
    assert _multiplied(1, 60000, 1, 2) == (1, None)


def test_declared_occurs_tolerates_raising_accessors():
    class RaiseMin:
        def getMinOccurs(self):
            raise RuntimeError("boom")

        def getMaxOccurs(self):
            return 3

    class RaiseMax:
        def getMinOccurs(self):
            return 2

        def getMaxOccurs(self):
            raise RuntimeError("boom")

    assert _declared_occurs(RaiseMin()) == (1, 3)
    assert _declared_occurs(RaiseMax()) == (2, 1)


# ---------------------------------------------------------------------------
# Annotation resolution and the never-raises guards
# ---------------------------------------------------------------------------


def test_annotation_base_degrades_when_get_type_raises():
    class RaiseType:
        def getType(self):
            raise RuntimeError("boom")

    assert _annotation_base(RaiseType(), "x", {}) == (
        "Any",
        "type of 'x' could not be resolved; annotated Any",
    )


def test_annotation_base_degrades_a_non_class_type():
    class NonType:
        def getType(self):
            return "Some"

    assert _annotation_base(NonType(), "x", {}) == (
        "Any",
        "type 'Some' is not stubbed; annotated Any",
    )


def test_resolved_base_names_an_unstubbed_class():
    class Stray:
        pass

    assert _resolved_base(Stray, {}, frozenset()) == (
        "Any",
        "type 'Stray' is not stubbed; annotated Any",
    )


def test_union_member_names_falls_back_to_repr_then_name():
    class Stray:
        pass

    assert _union_member_names([42, Stray], {}) == "42 | Stray"


def test_substitution_comment_survives_a_raising_schema_read():
    class Sub:
        isElementRef = True

        def getSchema(self):
            raise RuntimeError("boom")

    assert _substitution_comment(Sub()) is None


def test_substitution_comment_needs_a_string_head():
    class Head:
        isElementRef = True
        name = None

        def getSchema(self):
            return SimpleNamespace(substitutionGroups={"x": []})

    assert _substitution_comment(Head()) is None


def test_substitution_comment_returns_none_when_the_head_is_not_in_the_table():
    class Head:
        isElementRef = True
        name = "marker"
        expandedName = "other"

        def getSchema(self):
            return SimpleNamespace(substitutionGroups={"different": []})

    assert _substitution_comment(Head()) is None


def test_substitution_comment_without_named_members_keeps_the_head():
    class Head:
        isElementRef = True
        name = "marker"

        def getSchema(self):
            return SimpleNamespace(substitutionGroups={"marker": [object()]})

    assert _substitution_comment(Head()) == "substitution group: marker"


def test_call_returns_none_when_the_accessor_raises():
    class Raises:
        def getDefault(self):
            raise RuntimeError("boom")

    assert _call(Raises(), "getDefault") is None


def test_collect_particle_flags_tolerates_none():
    flags: set[str] = set()
    _collect_particle_flags(None, flags)
    assert flags == set()


def test_is_simple_type_false_for_a_non_class():
    assert _is_simple_type("not a class") is False


# ---------------------------------------------------------------------------
# Class-block helpers: docstrings and facets
# ---------------------------------------------------------------------------


def test_docstring_lines_without_an_er_is_empty():
    assert _docstring_lines(_StubClass(cls=object, name="X", er=None)) == []


def test_docstring_lines_render_multiline_text():
    er = SimpleNamespace()
    er.__dict__["__doc__"] = "first\nsecond"
    assert _docstring_lines(_StubClass(cls=object, name="X", er=er)) == [
        '    """first',
        "    second",
        '    """',
    ]


def test_facet_items_without_a_declaration_element_is_empty():
    assert _facet_items(_StubClass(cls=object, name="s", er=SimpleNamespace())) == []


def test_facet_items_skips_children_without_a_value():
    element = ET.fromstring(
        f'<xs:simpleType xmlns:xs="{XSD_NS}">'
        '<xs:restriction><xs:annotation/><xs:pattern value="a"/></xs:restriction>'
        "</xs:simpleType>"
    )
    entry = _StubClass(cls=object, name="s", er=SimpleNamespace(xsdElement=element))
    assert _facet_items(entry) == ["pattern: a"]


def test_facet_items_survives_a_raising_scan():
    class Boom:
        def findall(self, pattern):
            raise RuntimeError("boom")

    entry = _StubClass(cls=object, name="s", er=SimpleNamespace(xsdElement=Boom()))
    assert _facet_items(entry) == []


def test_member_lines_skip_a_comment_less_nameless_member():
    assert _member_lines([_Member(name="", annotation="", kind="element", comment=None)]) == []


def test_extension_line_degrades_an_unsplittable_signature():
    assert _extension_line(_Member(name="f", annotation="", kind="extension")) == (
        "    def f(self) -> Any: ...  # signature could not be split; annotated Any"
    )
    # A member that already carries a reason keeps that reason.
    assert (
        _extension_line(_Member(name="f", annotation="", kind="extension", comment="why"))
        == "    def f(self) -> Any: ...  # why"
    )


# ---------------------------------------------------------------------------
# Signature splitting
# ---------------------------------------------------------------------------


def test_split_signature_rejects_unsplittable_shapes():
    assert _split_signature("nope") == (None, None)
    assert _split_signature("(x) notarrow") == (None, None)
    assert _split_signature("(unclosed") == (None, None)


def test_split_signature_keeps_nested_parentheses():
    # The closing paren of an inner parenthesised annotation must not end
    # the parameter list.
    assert _split_signature("(x: (int)) -> str") == ("(x: (int))", "str")
    assert _split_signature("(a: Callable[[int], str]) -> str") == (
        "(a: Callable[[int], str])",
        "str",
    )


# ---------------------------------------------------------------------------
# Provenance and root aliases
# ---------------------------------------------------------------------------


def test_source_label_reads_a_name_and_survives_a_raising_name():
    assert _source_label(SimpleNamespace(name="schema.xsd")) == "schema.xsd"

    class Boom:
        @property
        def name(self):
            raise RuntimeError("boom")

    assert _source_label(Boom()) == "<stream>"


def test_is_global_element_survives_a_raising_check():
    class Element:
        def isGlobalDeclaration(self):
            raise RuntimeError("boom")

    assert _is_global_element(Element()) is False


def test_root_alias_falls_back_to_the_called_name():
    class ER:
        name = ""

        def getName(self):
            return "ok"

        def getType(self):
            return xsd_data_types.Int

    assert _root_alias(ER(), {}, set()) == _RootAlias(name="ok", type_name="Int")


def test_root_alias_rejects_a_non_identifier_name():
    class ER:
        name = "ship-to"

    alias = _root_alias(ER(), {}, set())
    assert alias.type_name is None
    assert alias.comment == "root element 'ship-to' is not a Python identifier"


def test_root_alias_marks_a_repeat():
    schema = compile_schema(
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
             <xs:element name="root" type="xs:int"/>
           </xs:schema>"""
    )
    er = next(
        r for reps in schema.components.values() for r in reps if type(r).__name__ == "Element"
    )
    used: set[str] = set()
    first = _root_alias(er, {}, used)
    second = _root_alias(er, {}, used)
    assert first.type_name == "Int"
    assert second.type_name is None and "repeats an earlier alias" in second.comment


def test_called_name_returns_none_when_get_name_raises_or_is_not_text():
    class Raises:
        def getName(self):
            raise RuntimeError("boom")

    class Named:
        def getName(self):
            return "ok"

    assert _called_name(Raises()) is None
    assert _called_name(Named()) == "ok"


# ---------------------------------------------------------------------------
# Compact spelling machinery
# ---------------------------------------------------------------------------


def test_compact_spellings_skips_a_non_foldable_entry():
    spellings = _compact_spellings([], [_StubClass(cls=object, name="Stray")])
    assert "Stray" not in spellings


def test_spelling_pattern_without_spellings_is_none():
    assert _spelling_pattern({}) is None


def test_spell_text_without_a_pattern_returns_the_text():
    assert _spell_text("x: Int", None, {}, {}, {}) == ("x: Int", [])


def test_spell_text_inlines_each_facet_item_once():
    pattern = _spelling_pattern({"SKU": _Spelling("str", "String")})
    used: dict[str, list[str]] = {}
    text, inlined = _spell_text(
        "SKU | SKU",
        pattern,
        {"SKU": _Spelling("str", "String")},
        {"SKU": ["pattern: a"]},
        used,
    )
    assert text == "str | str"
    assert inlined == ["pattern: a"]
    assert used == {"str": ["String"]}


def test_compact_extension_line_degrades_an_unsplittable_signature():
    line = _compact_extension_line(_Member(name="f", annotation="", kind="extension"), "", "")
    assert line == "  def f(self) -> Any: ..."


# ---------------------------------------------------------------------------
# Render-time omissions and folded facets
# ---------------------------------------------------------------------------

NO_ROOT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="a"><xs:sequence>
    <xs:element name="x" type="xs:int"/></xs:sequence></xs:complexType>
</xs:schema>"""

EMPTY_SCHEMA = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"

SIMPLE_CHAIN = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="A"><xs:restriction base="xs:string">
    <xs:pattern value="a+"/></xs:restriction></xs:simpleType>
  <xs:simpleType name="B"><xs:restriction base="A">
    <xs:minLength value="2"/></xs:restriction></xs:simpleType>
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="v" type="B"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""

RESTRICTED_UNION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="u"><xs:union memberTypes="xs:int xs:string"/></xs:simpleType>
  <xs:simpleType name="ru"><xs:restriction base="u">
    <xs:minLength value="1"/></xs:restriction></xs:simpleType>
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="v" type="ru"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""


def test_full_stub_without_roots_omits_the_root_section():
    text = emit_stub(compile_schema(NO_ROOT))
    assert "class a(SchemaBase):" in text
    assert "# Root elements:" not in text


def test_compact_stub_without_roots_omits_the_roots_line():
    text = emit_stub(compile_schema(NO_ROOT), compact=True)
    assert "class a:" in text
    assert "# roots:" not in text


def test_empty_schema_renders_header_and_imports_only():
    text = emit_stub(compile_schema(EMPTY_SCHEMA))
    assert text.startswith("# Generated by pyxsd")
    assert "\nclass " not in text


def test_compact_inlines_facets_from_a_folded_simple_type_ancestor():
    text = emit_stub(compile_schema(SIMPLE_CHAIN), compact=True)
    # Both restriction levels are facet-only, so they are dropped and the
    # derived type inlines its own facet and its skipped ancestor's.
    assert "class A" not in text and "class B" not in text
    assert "v: str  # minLength: 2; pattern: a+" in text


def test_compact_renders_a_restricted_union_with_its_facets():
    # An ``xs:union`` restriction has no builtin ancestor to fold into, so
    # it keeps its own line; the facet-only header must still render.
    text = emit_stub(compile_schema(RESTRICTED_UNION), compact=True)
    assert "class ru(u):  # minLength: 1" in text
    assert "v: ru" in text


def test_compact_renders_a_comment_only_member():
    text = emit_stub(compile_schema(NON_IDENTIFIER_ELEMENT), compact=True)
    assert "  # element 'ship-to' is not dot-accessible (bound as 'ship-to')" in text
