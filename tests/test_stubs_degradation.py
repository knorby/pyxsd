"""Degradation rules for constructs a stub cannot type truthfully.

Truthfulness beats completeness: an ``xs:union`` member, a recursive
back edge, and an unresolvable type annotate as ``Any`` with a comment
saying what the annotation cannot say, and mixed content, wildcards,
open content and substitution groups get comment lines on the class or
member they degrade, so no construct is silently omitted. Every rule is
pinned in both modes (the comment text is shared through the member
model), and a schema whose report carries errors — one that never
passes ``require_valid`` — still renders text instead of raising.
"""

import io

from conftest import fixture_dir
from pyxsd.schema import Schema
from pyxsd.stubs import emit_stub


def render(xsd: str, **kwargs) -> tuple[str, str]:
    """Returns the full and compact stub text for an inline schema."""
    schema = Schema.compile(io.StringIO(xsd), **kwargs)
    return emit_stub(schema), emit_stub(schema, compact=True)


UNION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="u"><xs:union memberTypes="xs:int xs:string"/></xs:simpleType>
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="v" type="u"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""


def test_union_member_degrades():
    full, compact = render(UNION)
    assert "v: Any" in full and "union" in full
    # The comment names the members the parsed value may actually be.
    assert "union of: Int | String" in full
    assert "v: Any" in compact and "union of: Int | String" in compact


RECURSIVE = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="node"><xs:sequence>
    <xs:element name="child" type="node" minOccurs="0"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="node"/>
</xs:schema>"""


def test_recursive_type_back_edge_degrades():
    full, compact = render(RECURSIVE)
    assert "child: Any | None" in full and "recursive" in full
    assert "child: Any | None" in compact and "recursive" in compact


MUTUAL = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="a"><xs:sequence>
    <xs:element name="b" type="b" minOccurs="0"/></xs:sequence></xs:complexType>
  <xs:complexType name="b"><xs:sequence>
    <xs:element name="a" type="a" minOccurs="0"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="a"/>
</xs:schema>"""


def test_mutual_recursion_keeps_the_real_class_names():
    # The guard degrades only a class declaration re-entered while its
    # own block resolves — a direct self-reference. Two classes that
    # reference each other render at class level, so each annotation
    # names the real class; no resolution loop exists to guard.
    full, compact = render(MUTUAL)
    assert "b: b | None" in full and "a: a | None" in full
    assert "b: b | None" in compact and "a: a | None" in compact


UNION_RESTRICTION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="u"><xs:union memberTypes="xs:int xs:string"/></xs:simpleType>
  <xs:simpleType name="ru"><xs:restriction base="u"/></xs:simpleType>
  <xs:complexType name="rec"><xs:sequence>
    <xs:element name="v" type="ru"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="rec"/>
</xs:schema>"""


def test_restriction_of_a_union_is_not_itself_degraded():
    # A restriction inherits ``_unionMembers`` through its MRO but is
    # not itself a union: its parsed values are its own instances, so
    # the stub names the class rather than reading Any.
    full, compact = render(UNION_RESTRICTION)
    assert "v: ru" in full and "v: ru" in compact


SUBSTITUTION = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="item" type="xs:string"/>
  <xs:element name="special" type="xs:string" substitutionGroup="item"/>
  <xs:complexType name="bag"><xs:sequence>
    <xs:element ref="item" maxOccurs="unbounded"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="bag"/>
</xs:schema>"""


def test_substitution_group_head_annotation():
    full, compact = render(SUBSTITUTION)
    # A member occurrence satisfies the head's particle without filling
    # the head accessor (members bind under their own names), so the
    # degraded annotation carries the absence arm; the comment names the
    # declarations that can appear instead.
    assert "item: list[String] | None" in full
    assert "substitution group" in full
    # The comment names the declarations the accessor may receive.
    assert "members: special" in full
    assert "item: list[str] | None" in compact and "substitution group" in compact


def test_blocked_substitution_head_keeps_the_plain_annotation():
    # A head blocking ``substitution`` admits no member at all, so the
    # plain head annotation is already the whole truth: no comment and no
    # absence arm.
    xsd = SUBSTITUTION.replace(
        '<xs:element name="item" type="xs:string"/>',
        '<xs:element name="item" type="xs:string" block="substitution"/>',
    )
    full, compact = render(xsd)
    assert "item: list[String]\n" in full
    assert "item: list[String] | None" not in full
    assert "substitution group" not in full and "substitution group" not in compact
    assert "item: list[str]\n" in compact
    assert "item: list[str] | None" not in compact


def test_substitution_group_fixture_names_every_member():
    schema = Schema.compile(str(fixture_dir("substitution") / "schema.xsd"))
    full = emit_stub(schema)
    compact = emit_stub(schema, compact=True)
    assert "substitution group" in full and "members: dot, peg" in full
    # The head is required, yet a substitute-only document is valid and
    # leaves the head accessor None: the annotation carries the arm.
    assert "marker: list[AnySimpleType] | None" in full
    assert "marker: list[AnySimpleType] | None" in compact
    # Member elements do not invent class-body accessors of their own
    # (the member names still appear as the global elements they are).
    assert "\n    dot:" not in full and "\n    peg:" not in full
    assert "substitution group" in compact


MIXED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="p" mixed="true"><xs:sequence>
    <xs:element name="b" type="xs:string" minOccurs="0"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="p"/>
</xs:schema>"""


def test_mixed_content_class_comment():
    full, compact = render(MIXED)
    assert "mixed content: text not modeled" in full
    assert "b: String | None" in full  # members still annotate
    assert "mixed content: text not modeled" in compact
    assert "b: str | None" in compact


def test_wildcard_content_class_comments():
    # The fixture declares both an xs:any child model and an
    # xs:anyAttribute; each degradation is its own comment line.
    schema = Schema.compile(str(fixture_dir("wildcards") / "schema.xsd"))
    full = emit_stub(schema)
    assert "# admits undeclared children (wildcard)" in full
    assert "# admits undeclared attributes (wildcard)" in full
    assert "title: String" in full
    compact = emit_stub(schema, compact=True)
    assert "# admits undeclared children (wildcard)" in compact
    assert "# admits undeclared attributes (wildcard)" in compact
    assert "title: str" in compact


OPEN_CONTENT = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="op"><xs:openContent mode="interleave"><xs:any namespace="##other"/></xs:openContent>
    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="op"/>
</xs:schema>"""


def test_open_content_class_comment():
    full, compact = render(OPEN_CONTENT, xsd_version="1.1")
    assert "# admits undeclared children (open content)" in full
    assert "# admits undeclared children (open content)" in compact
    assert "a: String" in full and "a: str" in compact


ERRORS = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="9bad"><xs:sequence>
    <xs:element name="x" type="missingType"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="9bad"/>
</xs:schema>"""


def test_error_bearing_schema_still_renders():
    schema = Schema.compile(io.StringIO(ERRORS))
    assert len(schema.report) > 0  # the schema never passes require_valid
    full = emit_stub(schema)
    compact = emit_stub(schema, compact=True)
    assert "_9bad" in full and "x: Any" in full
    assert "unresolved" in full
    assert "_9bad" in compact and "x: Any" in compact


ALIAS_SHADOW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="str"><xs:sequence>
    <xs:element name="n" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:complexType name="holder"><xs:sequence>
    <xs:element name="s" type="xs:string"/>
    <xs:element name="k" type="str"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_compact_keeps_long_spelling_when_a_class_takes_the_alias_text():
    # A class literally named ``str`` would make the alias ``str=String``
    # ambiguous, so the builtin keeps its long spelling instead.
    text = emit_stub(Schema.compile(io.StringIO(ALIAS_SHADOW)), compact=True)
    assert "class str:" in text
    assert "s: String" in text  # the builtin, spelled long
    assert "k: str" in text  # the schema class
    assert "str=String" not in text


ALIAS_SHADOW_FOLDED = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="SKU"><xs:restriction base="xs:string"/></xs:simpleType>
  <xs:complexType name="str"><xs:sequence>
    <xs:element name="n" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:complexType name="holder"><xs:sequence>
    <xs:element name="sku" type="SKU"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_compact_folded_simple_type_keeps_the_long_builtin_spelling():
    # The dropped simple type's own name is not the alias text; with the
    # alias text taken by a class, it spells as the builtin's own name.
    text = emit_stub(Schema.compile(io.StringIO(ALIAS_SHADOW_FOLDED)), compact=True)
    assert "class SKU" not in text  # still folded onto its user
    assert "sku: String" in text
    assert "str=String" not in text


BUILTIN_NAME_SHADOW = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="String"><xs:sequence>
    <xs:element name="n" type="xs:int"/></xs:sequence></xs:complexType>
  <xs:complexType name="holder"><xs:sequence>
    <xs:element name="s" type="xs:string"/></xs:sequence></xs:complexType>
  <xs:element name="root" type="holder"/>
</xs:schema>"""


def test_full_import_scan_skips_a_builtin_name_a_class_shadows():
    # A schema class named ``String`` takes the name; the import scan
    # must not emit a second binding for it (the class would shadow the
    # import anyway), while the builtin's own name still annotates the
    # member. This pins the seam Task 4's ledger flagged.
    text = emit_stub(Schema.compile(io.StringIO(BUILTIN_NAME_SHADOW)))
    assert "class String(SchemaBase):" in text
    assert "s: String" in text
    import_lines = [
        line for line in text.splitlines() if line.startswith("from pyxsd.xsd_data_types import")
    ]
    assert import_lines and "String" not in import_lines[0]
    assert "Int" in import_lines[0]
