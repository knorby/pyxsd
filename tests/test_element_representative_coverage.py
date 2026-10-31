"""Coverage tests for the element-representative base layer.

Two families: base-class helpers exercised with small stand-ins (the
detached/detached-schema fallbacks that a real compile never reaches),
and schema-driven checks that pin the observable behavior of occurrence
reading, group-reference resolution and namespace resolution.
"""

import decimal
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import pyxsd.schema  # noqa: F401  (first import loads the ER stack in its safe order)
from pyxsd.binding import ParseModes
from pyxsd.element_representatives import element_representative as er_module
from pyxsd.element_representatives.element import Element
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
    _RegistryProxy,
)
from pyxsd.schema import Schema

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


def schema_tree(body):
    """Build an element-representative tree without a parser attached."""
    root = ET.fromstring(XSD_HEAD + body + XSD_TAIL)
    return ElementRepresentative.factory(root, None)


def find_all(er, class_name, found=None):
    """Every processed descendant (and ``er`` itself) of ``class_name``."""
    if found is None:
        found = []
    if type(er).__name__ == class_name:
        found.append(er)
    for child in getattr(er, "processedChildren", None) or []:
        if child is not None:
            find_all(child, class_name, found)
    return found


def compile_schema(body, tmp_path, mode=ParseModes.NAMESPACED):
    path = tmp_path / "schema.xsd"
    path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
    return Schema.compile(str(path), mode=mode)


def entries_of(compiled, class_name):
    return [
        entry
        for entries in compiled.components.values()
        for entry in entries
        if type(entry).__name__ == class_name
    ]


# ---------------------------------------------------------------------------
# The module-level registry proxy
# ---------------------------------------------------------------------------


def test_registry_proxy_falls_back_to_an_empty_table(monkeypatch):
    """Outside any parser the proxy reads and writes an empty fallback table."""
    monkeypatch.setattr(er_module, "context_or_ambient", lambda: SimpleNamespace(components=None))
    monkeypatch.setattr(er_module, "last_components", lambda: None)
    proxy = _RegistryProxy()
    proxy["k"] = ["v"]
    assert proxy["k"] == ["v"]
    assert "k" in proxy
    assert list(iter(proxy)) == ["k"]
    assert len(proxy) == 1
    assert proxy.get("k") == ["v"]
    assert proxy.get("missing", "fallback") == "fallback"
    assert proxy.setdefault("k", []) == ["v"]
    assert proxy.setdefault("new", ["n"]) == ["n"]
    assert list(proxy.keys()) == ["k", "new"]
    assert list(proxy.values()) == [["v"], ["n"]]
    assert list(proxy.items()) == [("k", ["v"]), ("new", ["n"])]
    assert proxy.getFromName("k", warn=False) == "v"
    proxy.clear()
    assert len(proxy) == 0


# ---------------------------------------------------------------------------
# Strict-mode expanded-name type lookup
# ---------------------------------------------------------------------------


def test_strict_expanded_name_lookup_falls_back_to_builtins():
    """An unnamespaced bookkeeping name has no clause; a bare built-in resolves."""
    table = ComponentTable()
    host = SimpleNamespace(components=table)
    assert ElementRepresentative._typeFromExpandedName("host|SimpleType", table, host) is None
    primitive = ElementRepresentative._typeFromExpandedName("string", table, host)
    assert primitive is not None
    assert issubclass(primitive, str)


# ---------------------------------------------------------------------------
# Base-class helpers on detached / stand-in representatives
# ---------------------------------------------------------------------------


def test_add_super_class_name_ignores_none_and_dedupes():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence><xs:element name='a'/></xs:sequence>"
        "</xs:complexType>"
    )
    element = find_all(tree, "Element")[0]
    containing = element.getContainingType()
    assert containing is not None
    assert element.addSuperClassName(None) is None
    assert element.addSuperClassName("Base") is None
    assert element.addSuperClassName("Base") is None
    assert element.addSuperClassName("Other") is None
    assert containing.superClassNames == ["Base", "Other"]


def test_processor_version_reads_the_attached_host_first():
    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    assert element._processorVersion() is None
    element.host = SimpleNamespace(processor_version=decimal.Decimal("1.0"))
    assert element._processorVersion() == decimal.Decimal("1.0")
    assert element._isXsd10() is True


def test_wildcard_declaration_check_without_a_registered_spec():
    """A non-wildcard ER has no spec, so the consistency check is skipped."""
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    element = find_all(tree, "Element")[0]
    assert not hasattr(element, "wildcardSpec")
    element._checkWildcardDeclaration(is_attribute=False)
    assert not hasattr(element, "wildcardSpec")


def test_refine_wildcard_spec_without_a_containing_type():
    """A detached wildcard refines its spec but has no type to notify."""
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence>"
        "<xs:any processContents='lax'/></xs:sequence></xs:complexType>"
    )
    any_er = find_all(tree, "Any")[0]
    any_er.parent = None
    assert any_er.getContainingType() is None
    assert any_er._wildcardQNameResolver() is None
    old = any_er.wildcardSpec
    any_er.tagAttributes["notQName"] = "foo"
    any_er.refineWildcardSpec()
    assert any_er.wildcardSpec is not old


def test_wildcard_qname_resolver_without_a_namespace_context():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence>"
        "<xs:any processContents='lax'/></xs:sequence></xs:complexType>"
    )
    any_er = find_all(tree, "Any")[0]
    assert any_er._wildcardQNameResolver() is None


def test_invalid_token_list_accepts_empty_and_rejects_hash_all_with_company():
    assert ElementRepresentative._invalidTokenList("", {"extension", "restriction"}) is False
    assert ElementRepresentative._invalidTokenList("#all extension", {"extension"}) is True
    assert ElementRepresentative._invalidTokenList("extension restriction", {"extension"}) is True


def test_silent_occurs_ignores_a_garbage_value():
    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    element.maxOccurs = "not-a-number"
    assert element._silentOccurs("maxOccurs") == 1


def test_compositor_emptiability_honors_an_optional_occurrence():
    tree = schema_tree(
        "<xs:complexType name='t'><xs:sequence minOccurs='0'>"
        "<xs:element name='a'/></xs:sequence></xs:complexType>"
    )
    sequence = find_all(tree, "Sequence")[0]
    assert sequence.emptiable is True


def test_resolve_group_ref_guards_and_resolver_fallthrough(tmp_path):
    tree = schema_tree(
        "<xs:group name='g'><xs:sequence><xs:element name='a'/></xs:sequence></xs:group>"
    )
    group = find_all(tree, "Group")[0]
    assert group.resolveGroupRef(SimpleNamespace(ref=None)) is None
    assert group.resolveGroupRef(SimpleNamespace(ref="g", resolveReference=None)) is group
    assert group.resolveGroupRef(
        SimpleNamespace(ref="g", resolveReference=lambda *a, **k: None)
    ) is (group)


def test_resolve_group_ref_without_a_schema():
    stub = SimpleNamespace(ref="g", resolveReference=None)
    assert ElementRepresentative.resolveGroupRef(SimpleNamespace(getSchema=lambda: None), stub) is (
        None
    )


def test_get_schema_block_default_fallbacks():
    detached = Element(
        ET.fromstring("<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"), None
    )
    assert detached.getSchemaBlockDefault() is None
    assert ElementRepresentative.getSchemaBlockDefault(SimpleNamespace(getSchema=lambda: None)) is (
        None
    )
    no_element = SimpleNamespace(getSchema=lambda: SimpleNamespace())
    assert ElementRepresentative.getSchemaBlockDefault(no_element) is None


def test_get_namespace_without_a_schema_getter():
    stub = SimpleNamespace(getSchema=lambda: SimpleNamespace(targetNamespace="urn:t"))
    assert ElementRepresentative.getNamespace(stub) == "urn:t"


def test_effective_final_without_a_schema():
    assert ElementRepresentative.effectiveFinal(SimpleNamespace(final=None)) is None
    assert ElementRepresentative.effectiveFinal(
        SimpleNamespace(final=None, getSchema=lambda: None)
    ) is (None)


def test_expanded_name_and_instance_name_of_a_nameless_declaration():
    element = Element(
        ET.fromstring("<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"), None
    )
    assert element.name is None
    assert element.expandedName is None
    assert element.instanceName() is None


def test_local_declaration_qualified_paths():
    explicit_target = SimpleNamespace(
        xsdElement=ET.fromstring(
            "<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema' targetNamespace='urn:t'/>"
        )
    )
    assert (
        ElementRepresentative._localDeclarationIsQualified(
            explicit_target, SimpleNamespace(), False
        )
        is True
    )
    bare = SimpleNamespace(
        xsdElement=ET.fromstring("<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema'/>")
    )
    assert ElementRepresentative._localDeclarationIsQualified(bare, None, False) is False


def test_effective_parser_without_a_getter():
    assert ElementRepresentative._effectiveParser(SimpleNamespace()) is None


def test_effective_schema_without_a_getter():
    assert ElementRepresentative._effectiveSchema(SimpleNamespace()) is None


def test_authored_qname_guards():
    tree = schema_tree("<xs:element name='a' ref='b'/>")
    element = find_all(tree, "Element")[0]
    assert element._authoredQName("") is None
    assert element._authoredQName("x") is None
    raising_context = SimpleNamespace(namespaceContext=object())
    no_element = SimpleNamespace(
        _effectiveSchema=lambda: raising_context,
        ref="x",
    )
    assert ElementRepresentative._authoredQName(no_element) is None


def test_authored_namespace_without_an_element():
    stub = SimpleNamespace(getNamespace=lambda: "urn:z")
    assert ElementRepresentative.authoredNamespace(stub) == "urn:z"


def test_authored_namespace_known_without_an_element():
    assert ElementRepresentative.authoredNamespaceKnown(SimpleNamespace()) is False


def test_reference_namespace_guards():
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    element = find_all(tree, "Element")[0]
    assert element.referenceNamespace("") is None
    assert element.referenceNamespace("xs:string") is None


def test_reference_namespace_unbound_prefix_is_none(tmp_path):
    compiled = compile_schema("<xs:element name='r' type='bad:Thing'/>", tmp_path)
    element = entries_of(compiled, "Element")[0]
    assert element.referenceNamespace("bad:Thing") is None


def test_same_name_type_declarations_without_a_schema():
    detached = Element(
        ET.fromstring("<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"), None
    )
    assert detached.sameNameTypeDeclarations("t") == []


def test_same_name_type_declarations_skips_non_string_names():
    tree = schema_tree("<xs:simpleType name='s'><xs:restriction base='xs:string'/></xs:simpleType>")
    declaration = find_all(tree, "SimpleType")[0]
    declaration.name = None
    assert tree.sameNameTypeDeclarations("s") == []


def test_resolve_schema_qname_without_a_namespace_context():
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    element = find_all(tree, "Element")[0]
    parser = SimpleNamespace(mode=ParseModes.NAMESPACED)
    assert element.resolveSchemaQName("bad:Thing", parser=parser) == "bad:Thing"


def test_resolve_schema_qname_passes_a_non_string_value_through():
    class _Context:
        def resolve(self, element, value):
            return value

    stub = SimpleNamespace(
        getSchema=lambda: SimpleNamespace(namespaceContext=_Context()),
        xsdElement=ET.fromstring("<xs:element xmlns:xs='http://www.w3.org/2001/XMLSchema'/>"),
        name="r",
        _effectiveParser=lambda parser=None: SimpleNamespace(mode=ParseModes.NAMESPACED),
    )
    assert ElementRepresentative.resolveSchemaQName(stub, 123) == 123


def test_resolve_schema_qname_reports_an_unbound_prefix_without_a_parser(tmp_path):
    compiled = compile_schema("<xs:element name='r' type='bad:Thing'/>", tmp_path)
    element = entries_of(compiled, "Element")[0]
    schema_er = element.getSchema()
    element.host = None
    schema_er.host = None
    assert element.resolveSchemaQName("bad:Thing") == "bad:Thing"


def test_resolve_reference_none_value():
    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    assert element.resolveReference(None, []) is None


def test_legacy_clark_reference_uses_recorded_authored_namespace():
    reference = find_all(schema_tree("<xs:element name='r'/>"), "Element")[0]
    parser = SimpleNamespace(mode=SimpleNamespace(namespaces="legacy"))

    def candidate(authored, known=True):
        return SimpleNamespace(
            name="g",
            getNamespace=lambda: "urn:main",
            authoredNamespaceKnown=lambda: known,
            authoredNamespace=lambda: authored,
        )

    wrong = candidate("urn:other")
    right = candidate("urn:imp")
    assert reference.resolveReference("{urn:imp}g", [wrong, right], parser=parser) is right
    # The reported namespace matching the reference is not enough: a
    # candidate whose recorded authored namespace differs is skipped,
    # so a "reported match OR authored match" rule would fail here.
    reported_match = SimpleNamespace(
        name="g",
        getNamespace=lambda: "urn:imp",
        authoredNamespaceKnown=lambda: True,
        authoredNamespace=lambda: "urn:other",
    )
    assert reference.resolveReference("{urn:imp}g", [reported_match], parser=parser) is None
    unknown = candidate("urn:other", known=False)
    assert reference.resolveReference("{urn:imp}g", [unknown], parser=parser) is unknown
    unrecorded = SimpleNamespace(name="g", getNamespace=lambda: "urn:main")
    assert reference.resolveReference("{urn:imp}g", [unrecorded], parser=parser) is unrecorded


def test_simple_variety_paths():
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    element = find_all(tree, "Element")[0]
    assert element.simpleVariety() is None

    illegal_child = schema_tree("<xs:simpleType name='s'><xs:element name='nope'/></xs:simpleType>")
    simple = find_all(illegal_child, "SimpleType")[0]
    assert simple.simpleVariety() == "atomic"


def test_simple_variety_of_inline_restriction_chain():
    inline = schema_tree(
        "<xs:simpleType name='s'><xs:restriction>"
        "<xs:simpleType><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:restriction></xs:simpleType>"
    )
    simple = find_all(inline, "SimpleType")[0]
    assert simple.simpleVariety() == "atomic"

    baseless = schema_tree("<xs:simpleType name='s'><xs:restriction/></xs:simpleType>")
    assert find_all(baseless, "SimpleType")[0].simpleVariety() is None


def test_union_transitive_membership_detects_an_inline_list():
    tree = schema_tree(
        "<xs:simpleType name='s'><xs:union>"
        "<xs:simpleType><xs:list itemType='xs:string'/></xs:simpleType>"
        "</xs:union></xs:simpleType>"
    )
    union = find_all(tree, "Union")[0]
    assert union.unionTransitiveMembershipHasNoList(union) is False


def test_global_type_candidates_without_a_component_table():
    schema = SimpleNamespace(
        components=None,
        simpleTypes={},
        complexTypes={},
    )
    stub = SimpleNamespace(getSchema=lambda: schema)
    assert ElementRepresentative._globalTypeCandidates(stub) == []


def test_global_component_candidates_modes():
    legacy = {"a": "legacy-a"}
    schema = SimpleNamespace(host=None, components=None)
    stub = SimpleNamespace(getSchema=lambda: schema)
    assert ElementRepresentative._globalComponentCandidates(stub, "attributeGroup", legacy) == [
        "legacy-a"
    ]

    strict = SimpleNamespace(mode=ParseModes.NAMESPACED)
    assert ElementRepresentative._globalComponentCandidates(
        stub, "attributeGroup", legacy, parser=strict
    ) == ["legacy-a"]


def test_resolved_type_name_without_a_type():
    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    assert element.resolvedTypeName() is None


def test_set_active_form_defaults_roundtrip():
    previous = er_module.get_active_form_defaults()
    try:
        er_module.set_active_form_defaults({})
        assert er_module.get_active_form_defaults() == {}
    finally:
        er_module.set_active_form_defaults(previous)


def test_factory_reports_an_unknown_tag(caplog):
    unknown = ET.fromstring("<bogus xmlns:xs='http://www.w3.org/2001/XMLSchema'/>")
    with caplog.at_level("WARNING", logger=er_module.__name__):
        assert ElementRepresentative.factory(unknown, None) is None
    assert "is not defined" in caplog.text


def test_component_kind_and_lookup_from_a_component_table():
    from pyxsd.element_representatives.element_representative import componentKind

    tree = schema_tree("<xs:element name='a'/>")
    element = find_all(tree, "Element")[0]
    assert componentKind(element) == "element"
    table = ComponentTable()
    table.setdefault("a", []).append(element)
    assert table.getFromName("a", warn=False) is element


def test_registry_proxy_forwards_get_from_name(monkeypatch):
    table = ComponentTable()
    sentinel = object()
    table.setdefault("thing", []).append(sentinel)
    monkeypatch.setattr(er_module, "context_or_ambient", lambda: SimpleNamespace(components=table))
    monkeypatch.setattr(er_module, "last_components", lambda: None)
    proxy = _RegistryProxy()
    assert proxy.getFromName("thing", warn=False) is sentinel


@pytest.mark.parametrize("value", ["true", "1"])
def test_invalid_boolean_accepts_truthy_spellings(value):
    assert ElementRepresentative._invalidBoolean(value) is False
    assert ElementRepresentative._invalidBoolean("TRUE") is True


def test_invalid_ncname_detects_a_colon():
    assert ElementRepresentative._invalidNCName("a:b") is True
    assert ElementRepresentative._invalidNCName("name") is False
