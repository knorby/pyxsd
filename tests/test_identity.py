"""Tests for identity constraints (Phase 9).

Covers ``xs:key`` (presence + uniqueness), ``xs:unique``
(uniqueness of present values), ``xs:keyref`` (values must match a
key), constraint placement rules and the supported XPath subset.
"""

import pytest

from conftest import run_parser
from pyxsd.element_representatives.element_representative import registry
from pyxsd.schema import Schema

_xs = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

_XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse(schema_text, instance_text, tmp_path):
    """Compiles an inline schema and binds an inline instance document."""
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path)).parse(str(instance_path))


def _catalog_schema(constraints, item_content="", item_attrs=""):
    """Builds a schema whose root catalog has ``items`` and ``orders``.

    ``constraints`` are placed at element-declaration level, where
    XSD 1.0 allows identity constraints.
    """
    item_inner = item_content or '<xs:element name="code" type="xs:token"/>'
    if item_attrs:
        item_attrs = f"    {item_attrs}\n"
    return (
        f"<xs:schema {_xs}>\n"
        '  <xs:element name="catalog">\n'
        "    <xs:complexType>\n"
        "      <xs:sequence>\n"
        '        <xs:element name="item" maxOccurs="unbounded">\n'
        "          <xs:complexType>\n"
        "            <xs:sequence>\n"
        f"              {item_inner}\n"
        "            </xs:sequence>\n"
        f"{item_attrs}"
        "          </xs:complexType>\n"
        "        </xs:element>\n"
        "      </xs:sequence>\n"
        "    </xs:complexType>\n"
        f"{constraints}"
        "  </xs:element>\n"
        "</xs:schema>\n"
    )


def _key(constraint_name="itemKey", selector="item", field="@id"):
    return (
        f'    <xs:key name="{constraint_name}">\n'
        f'      <xs:selector xpath="{selector}"/>\n'
        f'      <xs:field xpath="{field}"/>\n'
        "    </xs:key>\n"
    )


def _keyref(name, refer, selector, field):
    return (
        f'    <xs:keyref name="{name}" refer="{refer}">\n'
        f'      <xs:selector xpath="{selector}"/>\n'
        f'      <xs:field xpath="{field}"/>\n'
        "    </xs:keyref>\n"
    )


def _unique(name, selector, field):
    return (
        f'    <xs:unique name="{name}">\n'
        f'      <xs:selector xpath="{selector}"/>\n'
        f'      <xs:field xpath="{field}"/>\n'
        "    </xs:unique>\n"
    )


_ID_ATTR = '<xs:attribute name="id" type="xs:ID" use="required"/>\n'

_LINK_ELEMENT = (
    '        <xs:element name="link" minOccurs="0" maxOccurs="unbounded">\n'
    "          <xs:complexType>\n"
    '            <xs:attribute name="ref" type="xs:string" use="required"/>\n'
    "          </xs:complexType>\n"
    "        </xs:element>\n"
)

_LINK_SCHEMA_BODY = (
    "      <xs:sequence>\n"
    '        <xs:element name="item" maxOccurs="unbounded">\n'
    "          <xs:complexType>\n"
    '            <xs:attribute name="id" type="xs:ID" use="required"/>\n'
    "          </xs:complexType>\n"
    "        </xs:element>\n"
    f"{_LINK_ELEMENT}"
    "      </xs:sequence>\n"
)


def _link_schema(constraints):
    """A catalog schema with id-carrying items and ref-carrying links."""
    return (
        f"<xs:schema {_xs}>\n"
        '  <xs:element name="catalog">\n'
        "    <xs:complexType>\n"
        f"{_LINK_SCHEMA_BODY}"
        "    </xs:complexType>\n"
        f"{constraints}"
        "  </xs:element>\n"
        "</xs:schema>\n"
    )


class TestIdentityFixture:
    """The identity fixture: key + unique + keyref, all valid."""

    @pytest.fixture()
    def doc(self):
        return run_parser("identity")

    def test_valid_document_has_clean_report(self, doc):
        assert len(doc.report) == 0

    def test_constraints_recorded_on_root_element(self, doc):
        catalog_er = registry["catalog"][0]
        names = {constraint.constraintName for constraint in catalog_er.identities}
        assert names == {"itemKey", "itemCode", "orderRef"}


class TestKey:
    """xs:key: fields must be present and unique across selected nodes."""

    def test_duplicate_key_value_is_reported(self, tmp_path):
        schema = _catalog_schema(constraints=_key())
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>c-1</code></item>\n'
            '  <item id="a1"><code>c-2</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_missing_key_field_is_reported(self, tmp_path):
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="catalog">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="code" type="xs:token"/>\n'
            '              <xs:element name="ref" type="xs:string" minOccurs="0"/>\n'
            "            </xs:sequence>\n"
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key('refKey', field='ref')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>c-1</code><ref>r1</ref></item>\n'
            '  <item id="a2"><code>c-2</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes


class TestUnique:
    """xs:unique: fields unique when present; absence is allowed."""

    def test_duplicate_unique_value_is_reported(self, tmp_path):
        schema = _catalog_schema(constraints=_unique("codeUnique", "item", "code"))
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>same</code></item>\n'
            '  <item id="a2"><code>same</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-unique" in codes

    def test_absent_unique_field_is_allowed(self, tmp_path):
        schema = _catalog_schema(
            item_content='<xs:element name="code" type="xs:token" minOccurs="0"/>',
            item_attrs=_ID_ATTR,
            constraints=_unique("codeUnique", "item", "code"),
        )
        instance = (
            '<catalog>\n  <item id="a1"><code>same</code></item>\n  <item id="a2"/>\n</catalog>\n'
        )
        doc = _parse(schema, instance, tmp_path)
        assert not doc.report.has_errors


class TestKeyref:
    """xs:keyref: field values must match a collected key or unique."""

    def test_unmatched_keyref_is_reported(self, tmp_path):
        schema = _link_schema(constraints=_key() + _keyref("linkRef", "itemKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="no-such-id"/>\n</catalog>\n'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes

    def test_matching_keyref_passes(self, tmp_path):
        schema = _link_schema(constraints=_key() + _keyref("linkRef", "itemKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="a1"/>\n</catalog>\n'
        doc = _parse(schema, instance, tmp_path)
        assert not doc.report.has_errors

    def test_keyref_field_with_complex_content_is_reported(self, tmp_path):
        """idH006: a keyref field selecting a complex-content element is a
        violation, not an absent value."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element ref="kid" maxOccurs="unbounded"/>\n'
            '        <xs:element ref="uid" maxOccurs="unbounded"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath=".//kid"/>'
            '<xs:field xpath="@val"/></xs:key>\n'
            '    <xs:keyref name="kr" refer="k"><xs:selector xpath=".//uid"/>'
            '<xs:field xpath="pid"/></xs:keyref>\n'
            "  </xs:element>\n"
            '  <xs:element name="kid"><xs:complexType>'
            '<xs:attribute name="val" type="xs:string"/></xs:complexType></xs:element>\n'
            '  <xs:element name="uid"><xs:complexType><xs:sequence>'
            '<xs:element name="pid"><xs:complexType>'
            '<xs:attribute name="p" type="xs:string"/></xs:complexType></xs:element>'
            "</xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>\n"
        )
        instance = '<root><kid val="1"/><uid><pid p="1"/></uid></root>'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes

    def test_keyref_to_unknown_refer_is_reported(self, tmp_path):
        schema = _link_schema(constraints=_keyref("linkRef", "noSuchKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="a1"/>\n</catalog>\n'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes


class TestConstraintPlacement:
    """Constraints outside element declarations are ignored, and the
    illegal placement is reported as a child-grammar error."""

    def test_constraint_in_complex_type_is_dropped(self, tmp_path):
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:complexType name="misplacedType">\n'
            "    <xs:sequence>\n"
            '      <xs:element name="x" type="xs:integer"/>\n'
            "    </xs:sequence>\n"
            f"{_key('misplaced', selector='x', field='.')}"
            "  </xs:complexType>\n"
            '  <xs:element name="root" type="misplacedType"/>\n'
            "</xs:schema>\n"
        )
        doc = _parse(schema, "<root><x>1</x></root>", tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "declaration-child" in codes
        # The constraint is still dropped rather than applied.
        assert not any(code.startswith("identity-") for code in codes)


class TestXPathSubset:
    """The supported path subset and its documented limits."""

    def test_dot_prefix_selector(self, tmp_path):
        """``./item`` selects the same nodes as ``item``."""
        schema = _catalog_schema(constraints=_key(selector="./item"))
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>c-1</code></item>\n'
            '  <item id="a1"><code>c-2</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_wildcard_selector(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="*"))
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>c-1</code></item>\n'
            '  <item id="a1"><code>c-2</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes


class TestUnionFields:
    """Union field alternatives concatenate into one value space."""

    def test_field_union_alternatives(self, tmp_path):
        """``@x | @y`` is violated when both attributes are present and
        when the surviving alternatives repeat a value (idL union
        shapes)."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="a" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="x" type="xs:string"/>\n'
            '        <xs:attribute name="y" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:unique name="u"><xs:selector xpath="a"/>\n'
            '      <xs:field xpath="@x | @y"/></xs:unique>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = '<root><a x="1" y="9"/><a y="1"/></root>'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert any(code.startswith("identity-") for code in codes)

    def test_attribute_wildcard_union_collision(self, tmp_path):
        """``@*`` selects every attribute: two attributes on one node
        violate the field's at-most-one-node rule."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="a" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="x" type="xs:string"/>\n'
            '        <xs:attribute name="y" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:unique name="u"><xs:selector xpath="a"/>\n'
            '      <xs:field xpath="@*"/></xs:unique>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        doc = _parse(schema, '<root><a x="1" y="2"/></root>', tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert any(code.startswith("identity-") for code in codes)

    def test_union_selector_finds_all_alternatives(self, tmp_path):
        """A union selector covers every alternative's matches."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:choice '
            'maxOccurs="unbounded">\n'
            '    <xs:element name="a" type="xs:token"/>\n'
            '    <xs:element name="b" type="xs:token"/>\n'
            "  </xs:choice></xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath="a | b"/>\n'
            '      <xs:field xpath="."/></xs:key>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = "<root><a>dup</a><b>dup</b></root>"
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes


class TestQualifiedNames:
    """Element and attribute steps match by expanded (Clark) name."""

    def test_qualified_attribute_field_matches(self, tmp_path):
        """``@ns:id`` matches the namespace-qualified attribute."""
        ns = "http://example.com/q"
        schema = (
            f'<xs:schema {_xs} xmlns:q="{ns}" targetNamespace="{ns}" '
            'elementFormDefault="qualified" attributeFormDefault="qualified">\n'
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="item" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="id" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath="q:item"/>\n'
            '      <xs:field xpath="@q:id"/></xs:key>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = f'<q:root xmlns:q="{ns}"><q:item q:id="dup"/><q:item q:id="dup"/></q:root>'
        doc = _parse11(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_qualified_selector_does_not_match_other_namespace(self, tmp_path):
        """A selector naming another namespace selects nothing, so the
        key is vacuously satisfied."""
        ns = "http://example.com/q"
        other = "http://example.com/other"
        schema = (
            f'<xs:schema {_xs} xmlns:q="{ns}" xmlns:o="{other}" '
            f'targetNamespace="{ns}" elementFormDefault="qualified">\n'
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="item" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="id" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath="o:item"/>\n'
            '      <xs:field xpath="@q:id"/></xs:key>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = f'<q:root xmlns:q="{ns}"><q:item id="dup"/><q:item id="dup"/></q:root>'
        doc = _parse11(schema, instance, tmp_path)
        assert not doc.report.has_errors


class TestAttributeShadowing:
    """An attribute step reads the attribute, not a same-named
    child accessor."""

    def test_attribute_wins_over_child_accessor(self, tmp_path):
        """An undeclared (wildcard-admitted) attribute ``x`` and a
        declared child element ``x`` coexist; ``@x`` reads the
        attribute."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="item" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            "        <xs:sequence>\n"
            '          <xs:element name="x" type="xs:string"/>\n'
            "        </xs:sequence>\n"
            '        <xs:anyAttribute namespace="##any" processContents="lax"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:unique name="u"><xs:selector xpath="item"/>\n'
            '      <xs:field xpath="@x"/></xs:unique>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = '<root><item x="1"><x>a</x></item><item x="1"><x>b</x></item></root>'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-unique" in codes


class TestFieldCardinality:
    """Field node-set cardinality is counted before nil-discard."""

    def test_nilled_and_valued_union_alternatives_violate_cardinality(self, tmp_path):
        """A nilled element and an attribute selected by one union field
        are two nodes: more than one node violates the field rule even
        though the nilled element carries no value."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="item" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            "        <xs:sequence>\n"
            '          <xs:element name="v" type="xs:string" nillable="true"/>\n'
            "        </xs:sequence>\n"
            '        <xs:attribute name="x" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:unique name="u"><xs:selector xpath="item"/>\n'
            '      <xs:field xpath="@x | v"/></xs:unique>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = f'<root {_XSI}><item x="1"><v xsi:nil="true"/></item></root>'
        doc = _parse11(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert any(code.startswith("identity-") for code in codes)

    def test_field_on_complex_content_element_is_reported(self, tmp_path):
        """A field selecting an element with element children violates
        the constraint (idK012): the item is not skipped silently."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="item" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            "        <xs:sequence>\n"
            '          <xs:element name="pid">\n'
            "            <xs:complexType>\n"
            "              <xs:sequence>\n"
            '                <xs:element name="gid" type="xs:string"/>\n'
            "              </xs:sequence>\n"
            "            </xs:complexType>\n"
            "          </xs:element>\n"
            "        </xs:sequence>\n"
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:unique name="u"><xs:selector xpath="item"/>\n'
            '      <xs:field xpath="pid"/></xs:unique>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        doc = _parse(schema, "<root><item><pid><gid>g</gid></pid></item></root>", tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert any(code.startswith("identity-") for code in codes)


class TestKeyrefReferLegality:
    """A keyref's refer must name a key or unique with the same number
    of fields (idH011/13/14/035)."""

    def _schema(self, constraint_fragment):
        return (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="uid" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="val" type="xs:string"/>\n'
            '        <xs:attribute name="val2" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            f"{constraint_fragment}"
            "  </xs:element>\n"
            "</xs:schema>"
        )

    def test_refer_to_undeclared_key_is_a_schema_error(self, tmp_path):
        schema = self._schema(
            '    <xs:keyref name="kr" refer="noSuchKey">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
        )
        doc = _parse(schema, '<root><uid val="1"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes

    def test_refer_naming_a_keyref_is_an_error(self, tmp_path):
        schema = self._schema(
            '    <xs:keyref name="kr1" refer="kr2">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            '    <xs:keyref name="kr2" refer="kr1">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val2"/>\n'
            "    </xs:keyref>\n"
        )
        doc = _parse(schema, '<root><uid val="1" val2="2"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes

    def test_field_count_mismatch_is_a_schema_error(self, tmp_path):
        schema = self._schema(
            '    <xs:keyref name="kr" refer="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            '      <xs:field xpath="@val2"/>\n'
            "    </xs:keyref>\n"
            '    <xs:key name="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:key>\n"
        )
        doc = _parse(schema, '<root><uid val="1" val2="2"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes

    def test_matching_refer_and_field_count_is_legal(self, tmp_path):
        schema = self._schema(
            '    <xs:keyref name="kr" refer="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            '    <xs:key name="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:key>\n"
        )
        doc = _parse(schema, '<root><uid val="1" val2="2"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" not in codes

    def test_refer_to_key_in_another_namespace_is_a_schema_error(self, tmp_path):
        """A prefixed ``refer`` resolving to a foreign namespace finds
        no key even when a same-named key exists: keys are matched by
        Clark name, not by local name."""
        schema = (
            f"<xs:schema {_xs} xmlns:o='urn:other'>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="uid" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="val" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:key name="foreignKey"><xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/></xs:key>\n'
            '    <xs:keyref name="kr" refer="o:foreignKey">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            "  </xs:element>\n"
            "</xs:schema>"
        )
        doc = _parse(schema, '<root><uid val="1"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes

    def test_refer_with_undeclared_prefix_is_a_schema_error(self, tmp_path):
        schema = self._schema(
            '    <xs:keyref name="kr" refer="nope:k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            '    <xs:key name="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:key>\n"
        )
        doc = _parse(schema, '<root><uid val="1"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes

    def test_refer_to_key_in_target_namespace_resolves(self, tmp_path):
        """A prefixed ``refer`` naming a key in the keyref's own target
        namespace resolves by Clark name."""
        target = 'xmlns:t="urn:t" targetNamespace="urn:t"'
        schema = (
            f"<xs:schema {_xs} {target}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="uid" maxOccurs="unbounded">\n'
            "      <xs:complexType>\n"
            '        <xs:attribute name="val" type="xs:string"/>\n'
            "      </xs:complexType>\n"
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/></xs:key>\n'
            '    <xs:keyref name="kr" refer="t:k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = '<t:root xmlns:t="urn:t"><t:uid val="1"/></t:root>'
        doc = _parse(schema, instance, tmp_path)
        assert not doc.report.has_errors

    def test_invalid_xpath_default_namespace_on_keyref_is_xpath_invalid(self, tmp_path):
        """A bogus ``##`` keyword on the keyref fails like the
        selector/field path does: ``xpath-invalid``, not
        ``identity-refer``."""
        schema = self._schema(
            '    <xs:keyref name="kr" refer="k" xpathDefaultNamespace="##bogus">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:keyref>\n"
            '    <xs:key name="k">\n'
            '      <xs:selector xpath="uid"/>\n'
            '      <xs:field xpath="@val"/>\n'
            "    </xs:key>\n"
        )
        doc = _parse(schema, '<root><uid val="1"/></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "xpath-invalid" in codes
        assert "identity-refer" not in codes


class TestKeyrefScopeTree:
    """Keyrefs validate against the node tables the referenced
    constraint assembles within the keyref's own subtree — the
    keyref's element occurrence plus its descendants (XSD §3.11.5
    upward propagation). An ancestor's table and any other subtree's
    table are never consulted (§3.11.4 keyref clause)."""

    def test_keyref_sees_descendant_occurrence_tables(self, tmp_path):
        """A keyref's element occurrence also sees the tables its
        descendants contribute: the key declared on a child element
        declaration propagates upward into the keyref's table."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="section" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            '        <xs:element name="ref" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:key name="k"><xs:selector xpath="item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '  <xs:keyref name="r" refer="k"><xs:selector xpath=".//ref"/>'
            '<xs:field xpath="@id"/></xs:keyref>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = '<root><section><item id="i1"/><ref id="i1"/></section></root>'
        doc = _parse(schema, instance, tmp_path)
        assert not doc.report.has_errors

    def test_keyref_ancestor_table_is_outside_subtree(self, tmp_path):
        """A keyref may not draw on a key declared only on an ancestor:
        the ancestor's table is outside the keyref's subtree, so its
        value is unmatched even though the key collected it."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="shipper" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="ref" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:keyref name="r" refer="k"><xs:selector xpath="ref"/>'
            '<xs:field xpath="@id"/></xs:keyref>\n'
            "    </xs:element>\n"
            '    <xs:element name="item">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType>\n"
            '  <xs:key name="k"><xs:selector xpath=".//item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            "  </xs:element>\n"
            "</xs:schema>"
        )
        instance = '<root><shipper><ref id="i1"/></shipper><item id="i1"/></root>'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes

    def test_keyref_does_not_see_tables_outside_its_scope(self, tmp_path):
        """A key declared on a sibling subtree is outside the keyref's
        scope: its table is never consulted (no global flatten)."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="keyBranch" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:key name="k"><xs:selector xpath="item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            "    </xs:element>\n"
            '    <xs:element name="refBranch" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="ref" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:keyref name="r" refer="k"><xs:selector xpath="ref"/>'
            '<xs:field xpath="@id"/></xs:keyref>\n'
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>"
        )
        instance = (
            '<root><keyBranch><item id="v1"/></keyBranch>'
            '<refBranch><ref id="v1"/></refBranch></root>'
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes

    def test_keyref_matches_union_of_descendant_scopes(self, tmp_path):
        """A value present only in a descendant occurrence's table still
        matches: the keyref's subtree union gathers every descendant
        occurrence of the referenced constraint (an own-occurrence-only
        lookup wrongly rejected this)."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="node">\n'
            "    <xs:complexType><xs:sequence>\n"
            '      <xs:element name="item" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "      </xs:element>\n"
            '      <xs:element name="ref" minOccurs="0" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="ref" type="xs:string"/></xs:complexType>\n'
            "      </xs:element>\n"
            '      <xs:element ref="node" minOccurs="0" maxOccurs="unbounded"/>\n'
            "    </xs:sequence></xs:complexType>\n"
            '    <xs:key name="k"><xs:selector xpath="item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            '    <xs:keyref name="r" refer="k"><xs:selector xpath="ref"/>'
            '<xs:field xpath="@ref"/></xs:keyref>\n'
            "  </xs:element>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element ref="node" maxOccurs="unbounded"/>\n'
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>"
        )
        # "y" is keyed only by the *inner* node occurrence (the key
        # selector covers direct items only), while the ref sits on the
        # outer occurrence: only the subtree union finds it.
        instance = (
            '<root><node><item id="x"/><ref ref="y"/><node><item id="y"/></node></node></root>'
        )
        doc = _parse(schema, instance, tmp_path)
        assert not doc.report.has_errors

    def test_keyref_ref_site_borrows_its_target(self, tmp_path):
        """An XSD 1.1 ``<xs:keyref ref="..."/>`` site acts as the named
        keyref: it borrows its selector, fields and refer, and its
        borrowed check is enforced within its own subtree."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="box" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            '        <xs:element name="link" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="ref" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:key name="k"><xs:selector xpath="item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            '      <xs:keyref name="kr" refer="k"><xs:selector xpath="link"/>'
            '<xs:field xpath="@ref"/></xs:keyref>\n'
            "    </xs:element>\n"
            '    <xs:element name="box2" maxOccurs="unbounded">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            '        <xs:element name="link" maxOccurs="unbounded">'
            "<xs:complexType>"
            '<xs:attribute name="ref" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:key ref="k"/>\n'
            '      <xs:keyref ref="kr"/>\n'
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>"
        )
        valid = (
            '<root><box><item id="a"/><link ref="a"/></box>'
            '<box2><item id="b"/><link ref="b"/></box2></root>'
        )
        doc = _parse(schema, valid, tmp_path)
        assert not doc.report.has_errors
        # Without the borrow the box2 keyref would be skipped silently;
        # the mismatched link must be caught by the borrowed check.
        invalid = '<root><box2><item id="b"/><link ref="zz"/></box2></root>'
        doc = _parse(schema, invalid, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-keyref" in codes

    def test_keyref_ref_to_wrong_category_is_a_schema_error(self, tmp_path):
        """``<xs:keyref ref>`` must name a keyref (§3.11.3.5): naming a
        key is an ``identity-refer`` schema error."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="root"><xs:complexType><xs:sequence>\n'
            '    <xs:element name="box">\n'
            "      <xs:complexType><xs:sequence>\n"
            '        <xs:element name="item">'
            "<xs:complexType>"
            '<xs:attribute name="id" type="xs:string"/></xs:complexType>\n'
            "        </xs:element>\n"
            "      </xs:sequence></xs:complexType>\n"
            '      <xs:key name="k"><xs:selector xpath="item"/>'
            '<xs:field xpath="@id"/></xs:key>\n'
            '      <xs:keyref ref="k"/>\n'
            "    </xs:element>\n"
            "  </xs:sequence></xs:complexType></xs:element>\n"
            "</xs:schema>"
        )
        doc = _parse(schema, '<root><box><item id="1"/></box></root>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "identity-refer" in codes


class TestUnsupportedPaths:
    """Paths outside the subset are schema-phase ``xpath-invalid`` errors."""

    def test_predicate_selector_is_schema_invalid(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="item[@id]"))
        instance = '<catalog>\n  <item id="a1"><code>c-1</code></item>\n</catalog>\n'
        doc = _parse(schema, instance, tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "xpath-invalid" in codes

    def test_empty_selector(self, tmp_path):
        """An empty ``xpath`` is a schema error (the constraint's
        representation is illegal), not merely an unchecked warning."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="codes">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="code" type="xs:token" maxOccurs="unbounded"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            '    <xs:key name="emptySel">\n'
            '      <xs:selector xpath=""/>\n'
            '      <xs:field xpath="."/>\n'
            "    </xs:key>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, "<codes><code>a</code></codes>", tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "declaration-attribute" in codes

    def test_absolute_selector_xpath_invalid(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="/item"))
        doc = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "xpath-invalid" in codes

    def test_field_predicate_is_a_schema_error(self, tmp_path):
        schema = _catalog_schema(constraints=_key(field="@id[1]"))
        doc = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "xpath-invalid" in codes

    def test_absolute_field_is_a_schema_error(self, tmp_path):
        schema = _catalog_schema(constraints=_key(field="/item/@id"))
        doc = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        codes = {issue.code for issue in doc.report.for_phase("schema")}
        assert "xpath-invalid" in codes

    def test_dot_after_steps_field(self, tmp_path):
        """A field like ``item/.`` evaluates to the item's own value."""
        schema = _catalog_schema(item_attrs=_ID_ATTR, constraints=_key("valKey", field="item/."))
        instance = '<catalog>\n  <item id="dup"><code>c-1</code></item>\n</catalog>\n'
        doc = _parse(schema, instance, tmp_path)
        # Items are complex nodes with no simple-content value; the
        # field resolves but has no value, so key presence reports.
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes


class TestDescendantSelectors:
    """``.//`` (descendant-or-self) selectors and deep field paths."""

    def test_descendant_selector_finds_nested_items(self, tmp_path):
        """A key declared on a wrapper applies through ``.//item``."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="shelf">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="bin" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="item" maxOccurs="unbounded">\n'
            "                <xs:complexType>\n"
            '                  <xs:attribute name="id" type="xs:ID" use="required"/>\n'
            "                </xs:complexType>\n"
            "              </xs:element>\n"
            "            </xs:sequence>\n"
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key(selector='.//item')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        instance = '<shelf>\n  <bin><item id="a1"/></bin>\n  <bin><item id="a1"/></bin>\n</shelf>\n'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_multi_step_field_path(self, tmp_path):
        """A field naming a nested element (``item/code``) works."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="catalog">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="code" type="xs:token"/>\n'
            "            </xs:sequence>\n"
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key('codeKey', field='item/code')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>dup</code></item>\n'
            '  <item id="a2"><code>dup</code></item>\n'
            "</catalog>\n"
        )
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_dot_field_uses_the_selected_node(self, tmp_path):
        """A field of ``.`` takes the selected node's own value."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="codes">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="code" type="xs:token" maxOccurs="unbounded"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key('valueKey', selector='code', field='.')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        instance = "<codes>\n  <code>dup</code>\n  <code>dup</code>\n</codes>\n"
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes

    def test_attribute_field_with_path_steps(self, tmp_path):
        """A field resolving through steps before the attribute works."""
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="shelf">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="bin" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="item" maxOccurs="unbounded">\n'
            "                <xs:complexType>\n"
            '                  <xs:attribute name="id" type="xs:ID" use="required"/>\n'
            "                </xs:complexType>\n"
            "              </xs:element>\n"
            "            </xs:sequence>\n"
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key('binKey', selector='bin', field='item/@id')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        instance = '<shelf>\n  <bin><item id="a1"/></bin>\n  <bin><item id="a1"/></bin>\n</shelf>\n'
        doc = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in doc.report.errors]
        assert "identity-key" in codes


# ---------------------------------------------------------------------------
# Scoping and typed value comparison
# ---------------------------------------------------------------------------

_XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'

_BOX_SCHEMA = f"""<xs:schema {_xs}>
  <xs:element name="r">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="box" maxOccurs="unbounded">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="item" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:attribute name="id" type="xs:int" use="required"/>
                </xs:complexType>
              </xs:element>
              <xs:element name="link" minOccurs="0" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:attribute name="ref" type="xs:int" use="required"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
          <xs:key name="itemKey">
            <xs:selector xpath="item"/>
            <xs:field xpath="@id"/>
          </xs:key>
          <xs:keyref name="linkRef" refer="itemKey">
            <xs:selector xpath="link"/>
            <xs:field xpath="@ref"/>
          </xs:keyref>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""


class TestIdentityScoping:
    def test_sibling_occurrences_have_independent_keys(self, tmp_path):
        doc = _parse(
            _BOX_SCHEMA,
            '<r><box><item id="1"/></box><box><item id="1"/></box></r>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_keyref_does_not_cross_occurrence_scopes(self, tmp_path):
        doc = _parse(
            _BOX_SCHEMA,
            '<r><box><item id="1"/><link ref="2"/></box><box><item id="2"/></box></r>',
            tmp_path,
        )
        assert any(issue.code == "identity-keyref" for issue in doc.report.issues)


class TestIdentityTypedValues:
    def test_numeric_spellings_are_the_same_key(self, tmp_path):
        doc = _parse(
            _catalog_schema(
                _key(),
                item_attrs='<xs:attribute name="id" type="xs:int" use="required"/>',
            ),
            '<catalog><item id="1"/><item id="01"/></catalog>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in doc.report.issues)

    def test_numeric_keyref_spellings_match(self, tmp_path):
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="catalog">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="id" type="xs:int" use="required"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            '        <xs:element name="link" minOccurs="0" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="ref" type="xs:int" use="required"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            f"{_key()}"
            f"{_keyref('linkRef', 'itemKey', 'link', '@ref')}"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(
            schema,
            '<catalog><item id="1"/><link ref="01"/></catalog>',
            tmp_path,
        )
        assert not doc.report.has_errors


class TestIdentityFieldCardinality:
    def test_field_selecting_multiple_nodes_is_rejected(self, tmp_path):
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="r">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="row" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="v" type="xs:string" maxOccurs="2"/>\n'
            "            </xs:sequence>\n"
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            '    <xs:key name="rowKey">\n'
            '      <xs:selector xpath="row"/>\n'
            '      <xs:field xpath="v"/>\n'
            "    </xs:key>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, "<r><row><v>a</v><v>b</v></row></r>", tmp_path)
        assert any(issue.code == "identity-key" for issue in doc.report.issues)

    def test_nilled_field_supplies_no_key_value(self, tmp_path):
        schema = (
            f"<xs:schema {_xs}>\n"
            '  <xs:element name="r">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="v" type="xs:string" nillable="true" maxOccurs="unbounded"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            '    <xs:key name="vKey">\n'
            '      <xs:selector xpath="v"/>\n'
            '      <xs:field xpath="."/>\n'
            "    </xs:key>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(
            schema,
            f'<r {_XSI}><v xsi:nil="true"/></r>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in doc.report.issues)


_SAMPLE_NS = "http://example.com/sample"


def _parse11(schema_text, instance_text, tmp_path):
    """Binds an inline instance against an inline schema under the
    standards (namespaced) policy, where skip wildcards are in effect."""
    from pyxsd.binding import ParseModes

    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return Schema.compile(str(schema_path), mode=ParseModes.NAMESPACED).parse(str(instance_path))


def _skipped_content_schema(constraints: str) -> str:
    """The wild101-wild104 shape: a ``wrapper`` whose type admits any
    content with ``processContents="skip"``, and a ``doc`` carrying
    identity constraints over ``.//s:note``."""
    return (
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:s="{_SAMPLE_NS}" targetNamespace="{_SAMPLE_NS}"'
        f' elementFormDefault="qualified">'
        '  <xs:element name="doc">'
        "    <xs:complexType>"
        "      <xs:sequence>"
        '        <xs:choice maxOccurs="unbounded">'
        '          <xs:element ref="s:note"/>'
        '          <xs:element ref="s:ref"/>'
        '          <xs:element ref="s:wrapper"/>'
        "        </xs:choice>"
        "      </xs:sequence>"
        "    </xs:complexType>"
        f"{constraints}"
        "  </xs:element>"
        '  <xs:element name="note">'
        "    <xs:complexType>"
        '      <xs:attribute name="id" type="xs:string" use="optional"/>'
        "    </xs:complexType>"
        "  </xs:element>"
        '  <xs:element name="ref">'
        "    <xs:complexType>"
        '      <xs:attribute name="to" type="xs:string" use="optional"/>'
        "    </xs:complexType>"
        "  </xs:element>"
        '  <xs:element name="wrapper">'
        '    <xs:complexType mixed="true">'
        "      <xs:sequence>"
        '        <xs:any maxOccurs="unbounded" minOccurs="0"'
        ' namespace="##any" processContents="skip"/>'
        "      </xs:sequence>"
        "    </xs:complexType>"
        "  </xs:element>"
        "</xs:schema>"
    )


_KEY = (
    '    <xs:key name="id-keys">'
    '      <xs:selector xpath=".//s:note"/>'
    '      <xs:field xpath="@id"/>'
    "    </xs:key>\n"
)

_UNIQUE = (
    '    <xs:unique name="id-keys">'
    '      <xs:selector xpath=".//s:note"/>'
    '      <xs:field xpath="@id"/>'
    "    </xs:unique>\n"
)

_KEYREF = (
    _KEY + '    <xs:keyref name="ref-keys" refer="s:id-keys">'
    '      <xs:selector xpath=".//s:ref"/>'
    '      <xs:field xpath="@to"/>'
    "    </xs:keyref>\n"
)

_DOC_OPEN = f'<doc xmlns="{_SAMPLE_NS}">'


class TestIdentityConstraintsSkipSkippedWildcardContent:
    """Content matched by a ``processContents="skip"`` wildcard is
    skipped (XSD 1.1 §3.3.4.2), so identity constraints must not select
    it: a missing or duplicate field value there is legal, and a keyref
    does not resolve through it. The declared ``wrapper`` element itself
    is not skipped."""

    def test_key_ignores_a_missing_field_in_skipped_content(self, tmp_path):
        # wild101.v2: the note inside wrapper has no id.
        doc = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><wrapper><note/></wrapper></doc>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_key_ignores_a_duplicate_value_in_skipped_content(self, tmp_path):
        # wild101.v3: the skipped note repeats the declared note's id.
        doc = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><wrapper><note id="note1"/></wrapper></doc>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_key_still_reports_a_duplicate_in_declared_content(self, tmp_path):
        # wild101.n1: both duplicates are declared content.
        doc = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><note id="note1"/>'
            '<wrapper><note id="note3"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in doc.report.issues)

    def test_key_still_reports_a_missing_field_in_declared_content(self, tmp_path):
        # wild101.n2: the missing id is in declared content.
        doc = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><note/><wrapper><note id="note2"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in doc.report.issues)

    def test_unique_ignores_a_duplicate_value_in_skipped_content(self, tmp_path):
        # wild102.v3 (the unique twin of wild101.v3).
        doc = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><wrapper><note id="note1"/></wrapper></doc>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_unique_ignores_a_missing_field_in_skipped_content(self, tmp_path):
        # wild102.v2 (the unique twin of wild101.v2).
        doc = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><wrapper><note/></wrapper></doc>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_unique_still_reports_a_duplicate_in_declared_content(self, tmp_path):
        # wild102.n1: both duplicates are declared content.
        doc = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><note id="note1"/>'
            '<wrapper><note id="note3"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-unique" for issue in doc.report.issues)

    def test_keyref_does_not_resolve_through_skipped_content(self, tmp_path):
        # A ref inside the skip wildcard is not part of the keyref's
        # selection, so its unmatched value is legal.
        doc = _parse11(
            _skipped_content_schema(_KEYREF),
            _DOC_OPEN + '<note id="note1"/><wrapper><ref to="missing"/></wrapper></doc>',
            tmp_path,
        )
        assert not doc.report.has_errors

    def test_keyref_still_resolves_declared_content(self, tmp_path):
        # Control: the same ref as declared content fails the keyref.
        doc = _parse11(
            _skipped_content_schema(_KEYREF),
            _DOC_OPEN + '<note id="note1"/><ref to="missing"/></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-keyref" for issue in doc.report.issues)

    def test_declared_wrapper_element_itself_is_not_skipped(self, tmp_path):
        # wild101-104 skip only the wildcard content *inside* wrapper:
        # a key selecting the wrapper elements still sees them, so a
        # duplicate wrapper id is reported.
        schema = _skipped_content_schema(
            '    <xs:key name="wrap-keys">'
            '      <xs:selector xpath=".//s:wrapper"/>'
            '      <xs:field xpath="@id"/>'
            "    </xs:key>\n"
        ).replace(
            '<xs:complexType mixed="true">',
            '<xs:complexType mixed="true">'
            '<xs:attribute name="id" type="xs:string" use="optional"/>',
        )
        doc = _parse11(
            schema,
            _DOC_OPEN + '<wrapper id="w1"/><wrapper id="w1"/></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in doc.report.issues)


class TestDocumentIdSpace:
    """Document-level xs:ID/IDREF/IDREFS semantics (XSD §3.3.4).

    ID-typed content — attributes and elements, through restriction,
    list item types, union members and simpleContent bases — feeds one
    document table; duplicates are ``id-duplicate`` and every IDREF
    value, including each IDREFS token, must name a collected ID
    (``idref-unresolved``).
    """

    _ID_SCHEMA = (
        f"<xs:schema {_XS}>\n"
        '  <xs:element name="doc">\n'
        "    <xs:complexType>\n"
        "      <xs:sequence>\n"
        '        <xs:element name="item" maxOccurs="unbounded">\n'
        "          <xs:complexType>\n"
        '            <xs:attribute name="id" type="xs:ID"/>\n'
        '            <xs:attribute name="ref" type="xs:IDREF"/>\n'
        "          </xs:complexType>\n"
        "        </xs:element>\n"
        "      </xs:sequence>\n"
        "    </xs:complexType>\n"
        "  </xs:element>\n"
        "</xs:schema>\n"
    )

    def _codes(self, doc):
        return {issue.code for issue in doc.report.errors}

    def test_idref_must_resolve(self, tmp_path):
        schema = self._ID_SCHEMA
        valid = '<doc><item id="a1"/><item ref="a1"/></doc>'
        assert self._codes(_parse(schema, valid, tmp_path)) == set()
        dangling = '<doc><item ref="missing"/></doc>'
        assert self._codes(_parse(schema, dangling, tmp_path)) == {"idref-unresolved"}

    def test_idref_forward_reference_resolves(self, tmp_path):
        # IDREF may name an ID that appears later in the document.
        doc = _parse(self._ID_SCHEMA, '<doc><item ref="a1"/><item id="a1"/></doc>', tmp_path)
        assert self._codes(doc) == set()

    def test_id_duplicate_is_reported(self, tmp_path):
        doc = _parse(
            self._ID_SCHEMA,
            '<doc><item id="d1"/><item id="d1"/></doc>',
            tmp_path,
        )
        assert self._codes(doc) == {"id-duplicate"}

    def test_id_duplicate_across_attributes_and_elements(self, tmp_path):
        # An ID element value and an ID attribute value share one table.
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="ident" type="xs:ID"/>\n'
            '        <xs:element name="item">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="id" type="xs:ID"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, '<doc><ident>x1</ident><item id="x1"/></doc>', tmp_path)
        assert self._codes(doc) == {"id-duplicate"}
        doc = _parse(schema, '<doc><ident>x1</ident><item id="x2"/></doc>', tmp_path)
        assert self._codes(doc) == set()

    def test_idrefs_bad_token_is_reported(self, tmp_path):
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            '      <xs:attribute name="ids" type="xs:ID"/>\n'
            '      <xs:attribute name="refs" type="xs:IDREFS"/>\n'
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, '<doc ids="a1" refs="a1 a2"/>', tmp_path)
        assert self._codes(doc) == {"idref-unresolved"}
        doc = _parse(schema, '<doc ids="a1" refs="a1"/>', tmp_path)
        assert self._codes(doc) == set()

    def test_id_typed_element_reference(self, tmp_path):
        # s3_3_4ii20 shape: xs:IDREF-typed elements resolve against ID
        # attributes; a value that matches no ID exactly is invalid.
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="root">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="idref" type="xs:IDREF" maxOccurs="unbounded"/>\n'
            "      </xs:sequence>\n"
            '      <xs:attribute name="id1" type="xs:ID"/>\n'
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, '<root id1="asd87123_"><idref>asd87123</idref></root>', tmp_path)
        assert self._codes(doc) == {"idref-unresolved"}
        doc = _parse(schema, '<root id1="asd87123_"><idref>asd87123_</idref></root>', tmp_path)
        assert self._codes(doc) == set()

    def test_id_value_constraint_participates(self, tmp_path):
        # The default/fixed value of an absent ID attribute enters the
        # document table like an explicit one (id010/idZ011).
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="para" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="id" type="xs:ID" default="para001"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, '<doc><para/><para id="para001"/></doc>', tmp_path)
        assert self._codes(doc) == {"id-duplicate"}
        doc = _parse(schema, "<doc><para/><para/></doc>", tmp_path)
        assert self._codes(doc) == {"id-duplicate"}
        doc = _parse(schema, '<doc><para/><para id="other"/></doc>', tmp_path)
        assert self._codes(doc) == set()

    def test_derived_id_types_participate(self, tmp_path):
        # A user type restricting xs:ID keeps the document semantics
        # (idConstrDefs00402m/00501m).
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:simpleType name="derivedID">\n'
            '    <xs:restriction base="xs:ID"/>\n'
            "  </xs:simpleType>\n"
            '  <xs:simpleType name="derivedIDREF">\n'
            '    <xs:restriction base="xs:IDREF"/>\n'
            "  </xs:simpleType>\n"
            '  <xs:element name="root">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="person" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="ssn" type="derivedID" use="required"/>\n'
            '            <xs:attribute name="parent" type="derivedIDREF"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(
            schema,
            '<root><person ssn="s007"/><person ssn="s007"/></root>',
            tmp_path,
        )
        assert self._codes(doc) == {"id-duplicate"}
        doc = _parse(
            schema,
            '<root><person ssn="s007" parent="s009"/></root>',
            tmp_path,
        )
        assert self._codes(doc) == {"idref-unresolved"}
        doc = _parse(
            schema,
            '<root><person ssn="s007"/><person ssn="s008" parent="s007"/></root>',
            tmp_path,
        )
        assert self._codes(doc) == set()

    def test_list_of_id_registers_each_token(self, tmp_path):
        # A list-of-ID value contributes each token; an IDREF matches a
        # single token even when the ID lives in a list (s3_3_4v30).
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:simpleType name="listOfIDs">\n'
            '    <xs:list itemType="xs:ID"/>\n'
            "  </xs:simpleType>\n"
            '  <xs:element name="root">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="ids" type="listOfIDs"/>\n'
            '        <xs:element name="ref" type="xs:IDREF"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, "<root><ids>u123 u456</ids><ref>u456</ref></root>", tmp_path)
        assert self._codes(doc) == set()
        doc = _parse(schema, "<root><ids>u123 u456</ids><ref>u789</ref></root>", tmp_path)
        assert self._codes(doc) == {"idref-unresolved"}

    def test_union_of_id_and_idref_classifies_per_token(self, tmp_path):
        # id006: in a list whose item type is a union of IDREF-derived
        # and non-ID members, each token is classified by the member
        # that validates it — IDREF tokens must resolve, other members
        # stay outside the ID space.
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:simpleType name="idref-or-int">\n'
            '    <xs:union memberTypes="xs:IDREF xs:integer"/>\n'
            "  </xs:simpleType>\n"
            '  <xs:simpleType name="refList">\n'
            '    <xs:list itemType="idref-or-int"/>\n'
            "  </xs:simpleType>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="node" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="id" type="xs:ID"/>\n'
            '            <xs:attribute name="mixed" type="refList"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        # "123" is an integer token (outside the ID space); "A001" is an
        # IDREF token that must resolve against the collected ID.
        doc = _parse(
            schema,
            '<doc><node id="B001" mixed="A001 123"/></doc>',
            tmp_path,
        )
        assert self._codes(doc) == {"idref-unresolved"}
        doc = _parse(
            schema,
            '<doc><node id="A001" mixed="A001 123"/></doc>',
            tmp_path,
        )
        assert self._codes(doc) == set()

    def test_id_value_whitespace_collapse(self, tmp_path):
        # ID values are compared after whitespace collapse.
        doc = _parse(
            self._ID_SCHEMA,
            '<doc><item id=" a1 "/><item id="a1"/></doc>',
            tmp_path,
        )
        assert self._codes(doc) == {"id-duplicate"}

    def test_plain_id_named_attributes_have_no_id_semantics(self, tmp_path):
        # A string-typed attribute that happens to be called "id" is
        # not part of the ID space.
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="item" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            '            <xs:attribute name="id" type="xs:string"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, '<doc><item id="x"/><item id="x"/></doc>', tmp_path)
        assert self._codes(doc) == set()

    def test_id_table_does_not_leak_between_parses(self, tmp_path):
        schema = self._ID_SCHEMA
        first = _parse(schema, '<doc><item id="a1"/></doc>', tmp_path)
        assert self._codes(first) == set()
        second = _parse(schema, '<doc><item ref="a1"/></doc>', tmp_path)
        assert self._codes(second) == {"idref-unresolved"}

    def test_simplecontent_id_base_participates(self, tmp_path):
        # id008: a complexType extending xs:ID in simpleContent makes
        # the element text an ID value.
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="id" type="pseudoID"/>\n'
            '        <xs:element name="idref" type="pseudoIDREF"/>\n'
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            '  <xs:complexType name="pseudoID">\n'
            "    <xs:simpleContent>\n"
            '      <xs:extension base="xs:ID"/>\n'
            "    </xs:simpleContent>\n"
            "  </xs:complexType>\n"
            '  <xs:complexType name="pseudoIDREF">\n'
            "    <xs:simpleContent>\n"
            '      <xs:extension base="xs:IDREF"/>\n'
            "    </xs:simpleContent>\n"
            "  </xs:complexType>\n"
            "</xs:schema>\n"
        )
        doc = _parse(schema, "<doc><id>aaa</id><idref>aaa</idref></doc>", tmp_path)
        assert self._codes(doc) == set()
        doc = _parse(schema, "<doc><id>aaa</id><idref>bbb</idref></doc>", tmp_path)
        assert self._codes(doc) == {"idref-unresolved"}

    def test_id_repeats_on_one_element_are_not_duplicates(self, tmp_path):
        # id001.v01/id003.v01: two ID-typed slots of the SAME element may
        # carry the same value -- both bind that one element. An ID-typed
        # child element binds its PARENT, so an atomic ID child and an
        # attribute of the parent may also share a value.
        doc = _parse(
            self._ID_SCHEMA,
            '<doc><item id="e1" ref="e1"/></doc>',
            tmp_path,
        )
        assert self._codes(doc) == set()
        schema = (
            f"<xs:schema {_XS}>\n"
            '  <xs:element name="doc">\n'
            "    <xs:complexType>\n"
            "      <xs:sequence>\n"
            '        <xs:element name="node" maxOccurs="unbounded">\n'
            "          <xs:complexType>\n"
            "            <xs:sequence>\n"
            '              <xs:element name="id" type="xs:ID" minOccurs="0"/>\n'
            "            </xs:sequence>\n"
            '            <xs:attribute name="id-one" type="xs:ID"/>\n'
            "          </xs:complexType>\n"
            "        </xs:element>\n"
            "      </xs:sequence>\n"
            "    </xs:complexType>\n"
            "  </xs:element>\n"
            "</xs:schema>\n"
        )
        # zzz is carried twice, but both slots bind the same node.
        doc = _parse(
            schema,
            '<doc><node id-one="zzz"><id>zzz</id></node></doc>',
            tmp_path,
        )
        assert self._codes(doc) == set()
        # The same value on two different nodes is a duplicate.
        doc = _parse(
            schema,
            '<doc><node id-one="zzz"><id>zzz</id></node><node><id>zzz</id></node></doc>',
            tmp_path,
        )
        assert self._codes(doc) == {"id-duplicate"}
