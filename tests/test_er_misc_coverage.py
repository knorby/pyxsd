"""Coverage tests for the smaller element-representative error paths.

Each test pins the user-visible contract of one missed branch: the
``final``/``atomic-required`` facet checks of ``list`` and ``union``,
the misplacement records of ``all``/``sequence``/``attributeGroup``, the
attribute-group index fallback, the notation attribute and restriction
container rules, the schema element cache, and the identity-constraint
namespace/Clark-name helpers.
"""

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import pyxsd.schema  # noqa: F401  (loads the ER stack in its safe order)
from pyxsd.binding import ParseModes
from pyxsd.element_representatives.attribute import Attribute
from pyxsd.element_representatives.attribute_group import _attribute_group_index
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.element_representatives.identity import IdentityConstraint
from pyxsd.element_representatives.list import List
from pyxsd.element_representatives.union import Union
from pyxsd.schema import Schema

XSD_HEAD = "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
XSD_TAIL = "</xs:schema>"


@pytest.fixture
def schema_report(tmp_path):
    """Compile a schema fragment (default charset) and return its report."""
    schema_path = tmp_path / "schema.xsd"

    def _parse(body, *, version="1.1"):
        schema_path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
        return Schema.compile(str(schema_path), mode=ParseModes.STRICT, xsd_version=version).report

    return _parse


def full_parse(body, xml, tmp_path):
    schema_path = tmp_path / "schema.xsd"
    instance_path = tmp_path / "instance.xml"
    schema_path.write_text(XSD_HEAD + body + XSD_TAIL, encoding="utf-8")
    instance_path.write_text(xml, encoding="utf-8")
    return Schema.compile(str(schema_path), mode=ParseModes.STRICT).parse(str(instance_path))


def schema_tree(body):
    """Build an element-representative tree without a parser attached."""
    root = ET.fromstring(XSD_HEAD + body + XSD_TAIL)
    return ElementRepresentative.factory(root, None)


def find_all(er, class_name, found=None):
    """Every processed descendant (and *er* itself) of *class_name*."""
    if found is None:
        found = []
    if type(er).__name__ == class_name:
        found.append(er)
    for child in getattr(er, "processedChildren", None) or []:
        if child is not None:
            find_all(child, class_name, found)
    return found


def _compiled_ers(schema, class_name):
    found = []
    for entries in schema.components.values():
        for entry in entries:
            found.extend(find_all(entry, class_name))
    return found


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_inline_item_type_final_for_list_is_reported(schema_report):
    report = schema_report(
        "<xs:simpleType name='L'><xs:list>"
        "<xs:simpleType final='list'><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:list></xs:simpleType>"
    )
    final = [issue for issue in report.errors if issue.code == "final"]
    assert len(final) == 1
    assert "is final for list derivation" in final[0].message


def test_list_builtin_item_type_needs_no_final_check(schema_report):
    """A built-in item type has no representative, so the final check is skipped."""
    report = schema_report(
        "<xs:simpleType name='L'><xs:list itemType='xs:string'/></xs:simpleType>"
    )
    assert report.errors == []

    # A non-atomic item type is still reported, showing the item check runs.
    rejected = schema_report(
        "<xs:complexType name='CT'><xs:sequence><xs:element name='a' type='xs:string'/>"
        "</xs:sequence></xs:complexType>"
        "<xs:simpleType name='L'><xs:list itemType='CT'/></xs:simpleType>"
    )
    atomic = [issue for issue in rejected.errors if issue.code == "atomic-required"]
    assert len(atomic) == 1
    assert "not an atomic simple type" in atomic[0].message


def test_list_unresolved_item_type_is_left_to_the_unknown_type_check(schema_report):
    report = schema_report(
        "<xs:simpleType name='L'><xs:list itemType='NoSuchType'/></xs:simpleType>"
    )
    assert any(issue.code == "unknown-type" for issue in report.errors)
    assert not any(issue.code == "atomic-required" for issue in report.errors)


def test_list_final_check_treats_a_missing_getter_as_unblocked():
    """A type representative without ``effectiveFinal`` is never final-blocked."""
    assert List._finalBlocks(SimpleNamespace(), "list") is False


# ---------------------------------------------------------------------------
# union
# ---------------------------------------------------------------------------


def test_union_inline_member_final_for_union_is_reported(schema_report):
    report = schema_report(
        "<xs:simpleType name='U'><xs:union>"
        "<xs:simpleType final='union'><xs:restriction base='xs:string'/></xs:simpleType>"
        "</xs:union></xs:simpleType>"
    )
    final = [issue for issue in report.errors if issue.code == "final"]
    assert len(final) == 1
    assert "is final for union derivation" in final[0].message


def test_union_builtin_member_type_needs_no_final_check(schema_report):
    report = schema_report(
        "<xs:simpleType name='U'><xs:union memberTypes='xs:string'/></xs:simpleType>"
    )
    assert report.errors == []

    # A complex member type is still reported, showing the member check runs.
    rejected = schema_report(
        "<xs:complexType name='CT'><xs:sequence><xs:element name='a' type='xs:string'/>"
        "</xs:sequence></xs:complexType>"
        "<xs:simpleType name='U'><xs:union memberTypes='CT'/></xs:simpleType>"
    )
    atomic = [issue for issue in rejected.errors if issue.code == "atomic-required"]
    assert len(atomic) == 1
    assert "is not a simple type" in atomic[0].message


def test_union_final_check_treats_a_missing_getter_as_unblocked():
    """A type representative without ``effectiveFinal`` is never final-blocked."""
    assert Union._finalBlocks(SimpleNamespace(), "union") is False


def test_union_inline_complex_member_is_reported(schema_report):
    report = schema_report(
        "<xs:complexType name='CT'><xs:sequence><xs:element name='a' type='xs:string'/>"
        "</xs:sequence></xs:complexType>"
        "<xs:simpleType name='U'><xs:union>"
        "<xs:simpleType><xs:restriction base='CT'/></xs:simpleType>"
        "</xs:union></xs:simpleType>"
    )
    atomic = [issue for issue in report.errors if issue.code == "atomic-required"]
    assert len(atomic) == 1
    assert "is not a simple type" in atomic[0].message


# ---------------------------------------------------------------------------
# all / sequence
# ---------------------------------------------------------------------------


def test_all_inside_a_simple_type_is_misplaced(schema_report):
    report = schema_report(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:all><xs:element name='e'/></xs:all>"
        "</xs:restriction></xs:simpleType>"
    )
    misplaced = [issue for issue in report.errors if issue.code == "misplaced-declaration"]
    assert len(misplaced) == 1
    assert "all cannot appear inside SimpleType" in misplaced[0].message


def test_sequence_inside_a_simple_type_is_misplaced(schema_report):
    report = schema_report(
        "<xs:simpleType name='s'><xs:restriction base='xs:string'>"
        "<xs:sequence><xs:element name='e'/></xs:sequence>"
        "</xs:restriction></xs:simpleType>"
    )
    misplaced = [issue for issue in report.errors if issue.code == "misplaced-declaration"]
    assert len(misplaced) == 1
    assert "sequence cannot appear inside SimpleType" in misplaced[0].message


def test_all_particle_occurrence_cap_skips_group_references(schema_report):
    """XSD 1.0 caps an ``all``'s element particles; group references are skipped.

    A group reference nested in an ``all`` is appended to ``All.elements``
    with ``isRefSite`` set, so the 1.0 occurrence cap skips it; an element
    particle above ``maxOccurs=1`` is still reported. (pyxsd emits no
    diagnostic for a group reference inside a 1.0 ``all`` at all — a
    pre-existing gap, noted in the task report.)
    """
    group_ref = schema_report(
        "<xs:group name='g'><xs:all><xs:element name='a'/></xs:all></xs:group>"
        "<xs:complexType name='t'><xs:all><xs:group ref='g'/></xs:all></xs:complexType>",
        version="1.0",
    )
    assert group_ref.errors == []

    element_particle = schema_report(
        "<xs:complexType name='t'><xs:all>"
        "<xs:element name='e' type='xs:string' maxOccurs='2'/></xs:all></xs:complexType>",
        version="1.0",
    )
    capped = [
        issue for issue in element_particle.errors if "XSD 1.0 allows only 0 or 1" in issue.message
    ]
    assert len(capped) == 1
    assert capped[0].code == "all-rule"


def test_all_emptiability_follows_its_children():
    optional = find_all(
        schema_tree(
            "<xs:complexType name='t'><xs:all>"
            "<xs:element name='a' minOccurs='0'/></xs:all></xs:complexType>"
        ),
        "All",
    )[0]
    assert optional.emptiable is True

    required = find_all(
        schema_tree(
            "<xs:complexType name='t'><xs:all><xs:element name='a'/></xs:all></xs:complexType>"
        ),
        "All",
    )[0]
    assert required.emptiable is False


# ---------------------------------------------------------------------------
# attributeGroup
# ---------------------------------------------------------------------------


def test_attribute_group_reference_with_a_name_is_reported(schema_report):
    report = schema_report(
        "<xs:attributeGroup name='g'/>"
        "<xs:element name='r'><xs:complexType>"
        "<xs:attributeGroup ref='g' name='n'/></xs:complexType></xs:element>"
    )
    named = [issue for issue in report.errors if "must not carry a name attribute" in issue.message]
    assert len(named) == 1
    assert named[0].code == "declaration-attribute"


def test_attribute_group_index_falls_back_without_a_component_table():
    """Without a component table the historical ``attributeGroups`` dict is used."""
    entry = SimpleNamespace(name="g")
    schema = SimpleNamespace(attributeGroups={"g": entry}, components=None)

    index = _attribute_group_index(schema)

    assert index["candidates"] == [entry]
    assert index["by_name"] == {"g": [entry]}
    assert schema._attribute_group_index is index


# ---------------------------------------------------------------------------
# notation / restriction / schema
# ---------------------------------------------------------------------------


def test_notation_ignores_namespaced_attributes(schema_report):
    """A foreign-namespaced attribute is skipped; an unqualified one is not."""
    report = schema_report(
        "<xs:notation name='n' public='p' xmlns:foo='urn:foo' foo:bar='baz' bogus='x'/>"
    )
    messages = [issue.message for issue in report.errors]
    assert any("'bogus'" in message for message in messages)
    assert not any("bar" in message for message in messages)


def test_get_elements_is_cached():
    tree = schema_tree("<xs:element name='a' type='xs:string'/>")
    first = tree.getElements()
    assert first == tree.getElements()
    assert first is tree.getElements()


# ---------------------------------------------------------------------------
# attribute descriptor write-through
# ---------------------------------------------------------------------------


def test_descriptor_set_without_a_raw_container_skips_write_through(tmp_path):
    body = (
        "<xs:element name='r'><xs:complexType>"
        "<xs:attribute name='a' type='xs:string'/></xs:complexType></xs:element>"
    )
    document = full_parse(body, "<r/>", tmp_path)
    descriptor = type(document.root).a
    holder = document.root
    holder.__dict__.pop("_attribs_", None)

    value = descriptor.getType()("5")
    Attribute.__set__(descriptor, holder, value)

    # The converted value is stored on the instance, and because there is
    # no raw ``_attribs_`` container the lexical write-through is skipped.
    assert holder.a == "5"
    assert "_attribs_" not in holder.__dict__


# ---------------------------------------------------------------------------
# identity-constraint declaration sites
# ---------------------------------------------------------------------------

KEY_ELEMENT = (
    "<xs:key xmlns:xs='http://www.w3.org/2001/XMLSchema' name='k'>"
    "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key>"
)


def test_detached_constraint_namespace_helpers_are_empty():
    constraint = IdentityConstraint(ET.fromstring(KEY_ELEMENT), None)

    assert constraint._declarationNamespaces() == {}
    assert constraint._schemaTargetNamespace() is None
    assert constraint._rawXpathDefaultNamespace() is None


def test_constraint_without_a_namespace_context_has_no_bindings():
    key = find_all(
        schema_tree(
            "<xs:element name='e'><xs:key name='k'>"
            "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key></xs:element>"
        ),
        "Key",
    )[0]

    assert key._declarationNamespaces() == {}


def test_xpath_default_namespace_defaultnamespace_uses_the_in_scope_default(tmp_path):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"
        " targetNamespace='urn:x' xmlns='urn:x' elementFormDefault='qualified'>"
        "<xs:element name='r'><xs:key name='k'>"
        "<xs:selector xpath='a' xpathDefaultNamespace='##defaultNamespace'/>"
        "<xs:field xpath='@b'/></xs:key></xs:element></xs:schema>",
        encoding="utf-8",
    )
    schema = Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED)
    selector = _compiled_ers(schema, "Selector")[0]

    assert selector._xpathDefaultNamespace() == "urn:x"


def test_parsed_selector_path_returns_the_selectors_parsed_path(tmp_path):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(
        XSD_HEAD + "<xs:element name='e'><xs:key name='k'>"
        "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key></xs:element>" + XSD_TAIL,
        encoding="utf-8",
    )
    schema = Schema.compile(str(schema_path), mode=ParseModes.STRICT)
    key = _compiled_ers(schema, "Key")[0]

    assert key.parsedSelectorPath is not None


def test_parsed_selector_path_is_none_without_a_selector():
    unique = find_all(
        schema_tree(
            "<xs:element name='e'><xs:unique name='u'>"
            "<xs:field xpath='@b'/></xs:unique></xs:element>"
        ),
        "Unique",
    )[0]

    assert unique.parsedSelectorPath is None
    assert len(unique.parsedFieldPaths) == 1


def test_constraint_name_with_a_bookkeeping_pipe_is_accepted(schema_report):
    report = schema_report(
        "<xs:element name='e'><xs:key name='a|b'>"
        "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key></xs:element>"
    )
    assert not any("valid NCName" in issue.message for issue in report.errors)


def test_unnamed_constraint_has_no_clark_name():
    key = find_all(
        schema_tree(
            "<xs:element name='e'><xs:key name='k'>"
            "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key></xs:element>"
        ),
        "Key",
    )[0]
    key.constraintName = ""

    assert key._constraintClarkName() is None


def test_empty_ref_has_no_clark_name():
    unique = find_all(
        schema_tree(
            "<xs:element name='e'><xs:unique ref=''><xs:field xpath='@b'/></xs:unique></xs:element>"
        ),
        "Unique",
    )[0]

    assert unique.isConstraintRef is True
    assert unique._refClarkName() is None


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("##targetNamespace", "{urn:x}k"),
        ("##local", "k"),
        ("urn:z", "{urn:z}k"),
    ],
)
def test_refer_qname_resolution_by_xpath_default_namespace(tmp_path, keyword, expected):
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(
        "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'"
        " targetNamespace='urn:x' xmlns:t='urn:x'>"
        "<xs:element name='r'><xs:key name='k'>"
        "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:key>"
        f"<xs:keyref name='kr' refer='k' xpathDefaultNamespace='{keyword}'>"
        "<xs:selector xpath='a'/><xs:field xpath='@b'/></xs:keyref></xs:element></xs:schema>",
        encoding="utf-8",
    )
    schema = Schema.compile(str(schema_path), mode=ParseModes.STRICT)
    keyref = _compiled_ers(schema, "Keyref")[0]

    assert keyref._referClarkName() == expected


def test_selector_attribute_step_is_reported(schema_report):
    report = schema_report(
        "<xs:element name='r'><xs:key name='k'>"
        "<xs:selector xpath='@a'/><xs:field xpath='@b'/></xs:key></xs:element>"
    )
    invalid = [issue for issue in report.errors if issue.code == "xpath-invalid"]
    assert len(invalid) == 1
    assert "must not select attribute nodes" in invalid[0].message


def test_namespaced_attributes_are_skipped_by_the_grammar_checks():
    tree = schema_tree(
        "<xs:element name='e'><xs:key name='k' xmlns:foo='urn:foo' foo:bar='x'>"
        "<xs:selector xpath='a' xmlns:foo='urn:foo' foo:bar='x'/>"
        "<xs:field xpath='@b'/></xs:key></xs:element>"
    )
    key = find_all(tree, "Key")[0]
    selector = find_all(tree, "Selector")[0]

    assert list(key._unqualifiedAttributes()) == ["name"]
    assert list(selector._unqualifiedAttributes()) == ["xpath"]
