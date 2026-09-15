"""Tests for identity constraints (Phase 9).

Covers ``xs:key`` (presence + uniqueness), ``xs:unique``
(uniqueness of present values), ``xs:keyref`` (values must match a
key), constraint placement rules and the supported XPath subset.
"""

import pytest

from conftest import run_parser
from pyxsd.element_representatives.element_representative import registry
from pyxsd.parser import PyXSD

_xs = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

_XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'


def _parse(schema_text, instance_text, tmp_path):
    """Parses an inline instance against an inline schema."""
    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
    )


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
    def parser(self):
        return run_parser("identity")

    def test_valid_document_has_clean_report(self, parser):
        assert len(parser.report) == 0

    def test_constraints_recorded_on_root_element(self, parser):
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        assert not parser.report.has_errors


class TestKeyref:
    """xs:keyref: field values must match a collected key or unique."""

    def test_unmatched_keyref_is_reported(self, tmp_path):
        schema = _link_schema(constraints=_key() + _keyref("linkRef", "itemKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="no-such-id"/>\n</catalog>\n'
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
        assert "identity-keyref" in codes

    def test_matching_keyref_passes(self, tmp_path):
        schema = _link_schema(constraints=_key() + _keyref("linkRef", "itemKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="a1"/>\n</catalog>\n'
        parser = _parse(schema, instance, tmp_path)
        assert not parser.report.has_errors

    def test_keyref_to_unknown_refer_is_reported(self, tmp_path):
        schema = _link_schema(constraints=_keyref("linkRef", "noSuchKey", "link", "@ref"))
        instance = '<catalog>\n  <item id="a1"/>\n  <link ref="a1"/>\n</catalog>\n'
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, "<root><x>1</x></root>", tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
        assert "identity-key" in codes

    def test_wildcard_selector(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="*"))
        instance = (
            "<catalog>\n"
            '  <item id="a1"><code>c-1</code></item>\n'
            '  <item id="a1"><code>c-2</code></item>\n'
            "</catalog>\n"
        )
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
        assert "identity-key" in codes


class TestUnsupportedPaths:
    """Unsupported path constructs are skipped with report warnings."""

    def test_predicate_selector_is_skipped_with_warning(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="item[@id]"))
        instance = '<catalog>\n  <item id="a1"><code>c-1</code></item>\n</catalog>\n'
        parser = _parse(schema, instance, tmp_path)
        warnings = [issue.code for issue in parser.report.warnings]
        assert "identity-unsupported" in warnings
        assert not parser.report.has_errors

    def test_empty_selector(self, tmp_path):
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
        parser = _parse(schema, "<codes><code>a</code></codes>", tmp_path)
        warnings = [issue.code for issue in parser.report.warnings]
        assert "identity-unsupported" in warnings
        assert not parser.report.has_errors

    def test_absolute_selector_is_unsupported(self, tmp_path):
        schema = _catalog_schema(constraints=_key(selector="/item"))
        parser = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        warnings = [issue.code for issue in parser.report.warnings]
        assert "identity-unsupported" in warnings
        assert not parser.report.has_errors

    def test_field_predicate_is_unsupported(self, tmp_path):
        schema = _catalog_schema(constraints=_key(field="@id[1]"))
        parser = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        warnings = [issue.code for issue in parser.report.warnings]
        assert "identity-unsupported" in warnings
        assert not parser.report.has_errors

    def test_absolute_field_is_unsupported(self, tmp_path):
        schema = _catalog_schema(constraints=_key(field="/item/@id"))
        parser = _parse(schema, '<catalog><item id="a1"><code>c</code></item></catalog>', tmp_path)
        warnings = [issue.code for issue in parser.report.warnings]
        assert "identity-unsupported" in warnings
        assert not parser.report.has_errors

    def test_dot_after_steps_field(self, tmp_path):
        """A field like ``item/.`` evaluates to the item's own value."""
        schema = _catalog_schema(item_attrs=_ID_ATTR, constraints=_key("valKey", field="item/."))
        instance = '<catalog>\n  <item id="dup"><code>c-1</code></item>\n</catalog>\n'
        parser = _parse(schema, instance, tmp_path)
        # Items are complex nodes with no simple-content value; the
        # field resolves but has no value, so key presence reports.
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
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
        parser = _parse(schema, instance, tmp_path)
        codes = [issue.code for issue in parser.report.errors]
        assert "identity-key" in codes


# ---------------------------------------------------------------------------
# Scoping and typed value comparison (R9-R11)
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
        parser = _parse(
            _BOX_SCHEMA,
            '<r><box><item id="1"/></box><box><item id="1"/></box></r>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_keyref_does_not_cross_occurrence_scopes(self, tmp_path):
        parser = _parse(
            _BOX_SCHEMA,
            '<r><box><item id="1"/><link ref="2"/></box><box><item id="2"/></box></r>',
            tmp_path,
        )
        assert any(issue.code == "identity-keyref" for issue in parser.report.issues)


class TestIdentityTypedValues:
    def test_numeric_spellings_are_the_same_key(self, tmp_path):
        parser = _parse(
            _catalog_schema(
                _key(),
                item_attrs='<xs:attribute name="id" type="xs:int" use="required"/>',
            ),
            '<catalog><item id="1"/><item id="01"/></catalog>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in parser.report.issues)

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
        parser = _parse(
            schema,
            '<catalog><item id="1"/><link ref="01"/></catalog>',
            tmp_path,
        )
        assert not parser.report.has_errors


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
        parser = _parse(schema, "<r><row><v>a</v><v>b</v></row></r>", tmp_path)
        assert any(issue.code == "identity-key" for issue in parser.report.issues)

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
        parser = _parse(
            schema,
            f'<r {_XSI}><v xsi:nil="true"/></r>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in parser.report.issues)


_SAMPLE_NS = "http://example.com/sample"


def _parse11(schema_text, instance_text, tmp_path):
    """Parses an inline instance against an inline schema under the
    standards (namespaced) policy, where skip wildcards are in effect."""
    from pyxsd.binding import ParseModes

    schema_path = tmp_path / "schema.xsd"
    schema_path.write_text(schema_text)
    instance_path = tmp_path / "instance.xml"
    instance_path.write_text(instance_text)
    return PyXSD(
        instance_path,
        xsdFile=schema_path,
        xmlFileOutput="_No_Output_",
        transformOutputName="_No_Output_",
        mode=ParseModes.NAMESPACED,
    )


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
        parser = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><wrapper><note/></wrapper></doc>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_key_ignores_a_duplicate_value_in_skipped_content(self, tmp_path):
        # wild101.v3: the skipped note repeats the declared note's id.
        parser = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><wrapper><note id="note1"/></wrapper></doc>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_key_still_reports_a_duplicate_in_declared_content(self, tmp_path):
        # wild101.n1: both duplicates are declared content.
        parser = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><note id="note1"/>'
            '<wrapper><note id="note3"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in parser.report.issues)

    def test_key_still_reports_a_missing_field_in_declared_content(self, tmp_path):
        # wild101.n2: the missing id is in declared content.
        parser = _parse11(
            _skipped_content_schema(_KEY),
            _DOC_OPEN + '<note id="note1"/><note/><wrapper><note id="note2"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in parser.report.issues)

    def test_unique_ignores_a_duplicate_value_in_skipped_content(self, tmp_path):
        # wild102.v3 (the unique twin of wild101.v3).
        parser = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><wrapper><note id="note1"/></wrapper></doc>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_unique_ignores_a_missing_field_in_skipped_content(self, tmp_path):
        # wild102.v2 (the unique twin of wild101.v2).
        parser = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><wrapper><note/></wrapper></doc>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_unique_still_reports_a_duplicate_in_declared_content(self, tmp_path):
        # wild102.n1: both duplicates are declared content.
        parser = _parse11(
            _skipped_content_schema(_UNIQUE),
            _DOC_OPEN + '<note id="note1"/><note id="note1"/>'
            '<wrapper><note id="note3"/></wrapper></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-unique" for issue in parser.report.issues)

    def test_keyref_does_not_resolve_through_skipped_content(self, tmp_path):
        # A ref inside the skip wildcard is not part of the keyref's
        # selection, so its unmatched value is legal.
        parser = _parse11(
            _skipped_content_schema(_KEYREF),
            _DOC_OPEN + '<note id="note1"/><wrapper><ref to="missing"/></wrapper></doc>',
            tmp_path,
        )
        assert not parser.report.has_errors

    def test_keyref_still_resolves_declared_content(self, tmp_path):
        # Control: the same ref as declared content fails the keyref.
        parser = _parse11(
            _skipped_content_schema(_KEYREF),
            _DOC_OPEN + '<note id="note1"/><ref to="missing"/></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-keyref" for issue in parser.report.issues)

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
        parser = _parse11(
            schema,
            _DOC_OPEN + '<wrapper id="w1"/><wrapper id="w1"/></doc>',
            tmp_path,
        )
        assert any(issue.code == "identity-key" for issue in parser.report.issues)
