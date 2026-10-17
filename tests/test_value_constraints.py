"""Value-constraint checks over composed schemas (default/fixed values).

The schema-phase value-constraint check validates a declaration's
``default``/``fixed`` value against the type its ``type`` reference
denotes *in its own schema document*.  Legacy namespace mode merges
every composed document into one namespace and matches components by
local name, so a composed tree may hold two type declarations that
share a local name: the declaration a reference names can be the one
that lost the merge.  These tests pin the ECMA-376/DrawingML shape of
that collision — a percentage union whose member reference names a
same-local-name string restriction in another namespace, and
enumerated types shadowed by a same-named host type — and the
rejection net for genuinely invalid values.

These cases come from the real-schema corpus (``ooxml-wml``'s 26
``declaration-attribute`` findings and ``ooxml-sml``'s one).
"""

from __future__ import annotations

import pytest

from pyxsd.binding import ParseModes
from pyxsd.schema import Schema

XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'

#: The shared-types document: a string restriction whose lexical space
#: is the percentage spelling (this is the declaration that loses the
#: legacy merge to the same-named union).
SHARED = f"""<xs:schema {XS} targetNamespace="urn:shared" xmlns:sh="urn:shared">
  <xs:simpleType name="ST_Percentage">
    <xs:restriction base="xs:string">
      <xs:pattern value="-?[0-9]+(\\.[0-9]+)?%"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>"""

#: The drawing document: a union whose ``sh:ST_Percentage`` member is
#: the shared-types declaration, plus a union whose member names the
#: local union (the flattening path), and attributes carrying the
#: corpus' percentage defaults.
MAIN_TEMPLATE = f"""<xs:schema {XS} targetNamespace="urn:main" xmlns:m="urn:main"
    xmlns:sh="urn:shared">
  <xs:import namespace="urn:shared" schemaLocation="shared.xsd"/>
  <xs:simpleType name="ST_PercentageDecimal">
    <xs:restriction base="xs:int"/>
  </xs:simpleType>
  <xs:simpleType name="ST_Percentage">
    <xs:union memberTypes="m:ST_PercentageDecimal sh:ST_Percentage"/>
  </xs:simpleType>
  <xs:simpleType name="ST_SpacingPercent">
    <xs:restriction base="xs:int">
      <xs:minInclusive value="0"/>
      <xs:maxInclusive value="100000"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:simpleType name="ST_SpacingPercentOrPercentString">
    <xs:union memberTypes="m:ST_SpacingPercent sh:ST_Percentage"/>
  </xs:simpleType>
  <xs:complexType name="CT_Autofit">
    <xs:attribute name="fontScale" type="m:ST_Percentage" default="{{font}}"/>
    <xs:attribute name="lnSpcReduction" type="m:ST_SpacingPercentOrPercentString"
      default="{{lspc}}"/>
  </xs:complexType>
</xs:schema>"""

#: The host document, importing the drawing document (which pulls in
#: the shared-types document transitively) and declaring its own
#: same-local-name enumerated types — the sml/wml shadowing shape.
HOST_TEMPLATE = f"""<xs:schema {XS} targetNamespace="urn:host" xmlns:h="urn:host"
    xmlns:m="urn:main" xmlns:sh="urn:shared">
  <xs:import namespace="urn:main" schemaLocation="main.xsd"/>
  <xs:import namespace="urn:shared" schemaLocation="shared.xsd"/>
  <xs:element name="root" type="m:CT_Autofit"/>
</xs:schema>"""

#: A host type whose local name shadows the imported document's
#: enumerated type (the WordprocessingML ``ST_Direction`` shape: the
#: host restricts ``xs:string``, the shadowed document restricts
#: ``xs:token`` — same lexical space, different enumerations).
SHADOWED = """<xs:schema {xs} targetNamespace="urn:dir" xmlns:d="urn:dir">
  <xs:simpleType name="ST_Direction">
    <xs:restriction base="{base}">
      <xs:enumeration value="{head_enum}"/>
      <xs:enumeration value="{tail_enum}"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="CT_Direction">
    <xs:attribute name="val" type="d:ST_Direction" default="{default}"/>
  </xs:complexType>
</xs:schema>"""

HOST_WITH_SHADOW = f"""<xs:schema {XS} targetNamespace="urn:host" xmlns:h="urn:host"
    xmlns:d="urn:dir">
  <xs:import namespace="urn:dir" schemaLocation="dir.xsd"/>
  <xs:simpleType name="ST_Direction">
    <xs:restriction base="xs:string">
      <xs:enumeration value="ltr"/>
      <xs:enumeration value="rtl"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="CT_Host">
    <xs:attribute name="hostVal" type="ST_Direction" default="ltr"/>
  </xs:complexType>
  <xs:element name="root" type="d:CT_Direction"/>
</xs:schema>"""

#: The ref-form shape: two documents each declare a global attribute
#: ``code`` whose (uniquely named) type accepts disjoint values, and
#: the host's complex type references ``a:code`` with a default only
#: the ``a`` declaration's type accepts. The legacy merge keeps one
#: ``code`` — with this import order the winner is ``b``'s declaration,
#: so the merged resolution rejects a value the referenced
#: declaration's type accepts (and accepts values it rejects).
REF_A = f"""<xs:schema {XS} targetNamespace="urn:a" xmlns:a="urn:a">
  <xs:simpleType name="T_A">
    <xs:restriction base="xs:token">
      <xs:enumeration value="norm"/>
      <xs:enumeration value="rev"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:attribute name="code" type="a:T_A"/>
</xs:schema>"""

REF_B = f"""<xs:schema {XS} targetNamespace="urn:b" xmlns:b="urn:b">
  <xs:simpleType name="T_B">
    <xs:restriction base="xs:token">
      <xs:enumeration value="ltr"/>
      <xs:enumeration value="rtl"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:attribute name="code" type="b:T_B"/>
</xs:schema>"""

REF_HOST = f"""<xs:schema {XS} targetNamespace="urn:host" xmlns:h="urn:host"
    xmlns:a="urn:a" xmlns:b="urn:b">
  <xs:import namespace="urn:a" schemaLocation="a.xsd"/>
  <xs:import namespace="urn:b" schemaLocation="b.xsd"/>
  <xs:complexType name="CT_Ref">
    <xs:attribute ref="a:code" default="{{default}}"/>
  </xs:complexType>
  <xs:element name="root" type="h:CT_Ref"/>
</xs:schema>"""

#: A pathological document targeting the XML Schema namespace and
#: declaring a type whose local name equals a built-in's: references
#: into that namespace must still resolve to the built-in (the legacy
#: lookup short-circuits them), never to the user declaration.
XSD_NAMESPACED = f"""<xs:schema {XS} targetNamespace="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="string">
    <xs:restriction base="xs:string">
      <xs:enumeration value="zzz"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="CT">
    <xs:attribute name="val" type="xs:string" default="{{default}}"/>
  </xs:complexType>
  <xs:element name="root" type="xs:CT"/>
</xs:schema>"""

#: The same shadowing declaration in an ordinary namespace: the
#: document's own type is the reference's target and its facets bind.
ORDINARY_NAMESPACED = f"""<xs:schema {XS} targetNamespace="urn:ordinary" xmlns:o="urn:ordinary">
  <xs:simpleType name="string">
    <xs:restriction base="xs:string">
      <xs:enumeration value="zzz"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="CT">
    <xs:attribute name="val" type="o:string" default="{{default}}"/>
  </xs:complexType>
  <xs:element name="root" type="o:CT"/>
</xs:schema>"""


def _compile(tmp_path, host_xml, files, *, mode=None):
    """Writes *files* beside ``host.xsd`` and compiles the host schema."""
    (tmp_path / "host.xsd").write_text(host_xml, encoding="utf-8")
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    kwargs = {} if mode is None else {"mode": mode}
    return Schema.compile(str(tmp_path / "host.xsd"), **kwargs)


def _declaration_errors(schema, element=None):
    """The schema's ``declaration-attribute`` errors, by element name."""
    return [
        issue
        for issue in schema.report.errors
        if issue.code == "declaration-attribute" and (element is None or issue.element == element)
    ]


def _percent_schema(tmp_path, *, font="100%", lspc="0%", mode=None):
    return _compile(
        tmp_path,
        HOST_TEMPLATE,
        {"main.xsd": MAIN_TEMPLATE.format(font=font, lspc=lspc), "shared.xsd": SHARED},
        mode=mode,
    )


# ---------------------------------------------------------------------------
# The corpus values are accepted
# ---------------------------------------------------------------------------


def test_percentage_default_on_union_typed_attribute_is_accepted(tmp_path):
    """``fontScale``/``CT_Percentage.val``: the value is valid against the
    union's cross-namespace member (the shared-types string restriction)."""
    schema = _percent_schema(tmp_path, font="100%")
    assert _declaration_errors(schema, "fontScale") == []


def test_percentage_default_through_nested_union_is_accepted(tmp_path):
    """``lnSpcReduction``: the member reference names the colliding union's
    cross-namespace namesake, not the local union (flattening must not
    substitute the wrong member set)."""
    schema = _percent_schema(tmp_path, lspc="0%")
    assert _declaration_errors(schema, "lnSpcReduction") == []


def test_percentage_defaults_are_accepted_in_namespaced_mode(tmp_path):
    """The main repro under strict namespaces (``ParseModes.NAMESPACED``).

    The mechanism runs in every mode: with namespace-aware resolution
    there is no merge collision at all — each reference resolves by
    expanded name — and the same corpus values validate.
    """
    schema = _percent_schema(tmp_path, font="100%", lspc="0%", mode=ParseModes.NAMESPACED)
    assert _declaration_errors(schema, "fontScale") == []
    assert _declaration_errors(schema, "lnSpcReduction") == []


def test_invalid_percentage_default_is_rejected_in_namespaced_mode(tmp_path):
    """The rejection net stays closed under strict namespaces too."""
    schema = _percent_schema(tmp_path, font="abc", lspc="abc", mode=ParseModes.NAMESPACED)
    assert _declaration_errors(schema, "fontScale")
    assert _declaration_errors(schema, "lnSpcReduction")


def _ref_schema(tmp_path, default):
    return _compile(
        tmp_path,
        REF_HOST.format(default=default),
        {"a.xsd": REF_A, "b.xsd": REF_B},
    )


def test_ref_form_default_validates_against_the_referenced_declaration(tmp_path):
    """A ref-form use's default is validated against the declaration the
    ``ref`` names in the writing document — here the ``a`` document's
    ``code``, whose type accepts ``norm`` — not the same-local-name
    declaration that won the legacy merge (``b``'s, which rejects it)."""
    schema = _ref_schema(tmp_path, "norm")
    assert _declaration_errors(schema, "code") == []


@pytest.mark.parametrize(
    ("base", "head_enum", "tail_enum", "default"),
    [
        ("xs:token", "norm", "rev", "norm"),
        ("xs:string", "maxMin", "minMax", "minMax"),
    ],
    ids=["token-enumeration", "string-enumeration"],
)
def test_enumeration_default_of_shadowed_type_is_accepted(
    tmp_path, base, head_enum, tail_enum, default
):
    """The declaration whose type lost the merge keeps its own value space.

    ``dml-diagram``'s ``ST_Direction`` (``norm``) and ``dml-chart``'s
    ``ST_Orientation`` (``minMax``) are shadowed by a same-named
    declaration the composing document declares; the value-constraint
    check must validate against the reference's own document.
    """
    schema = _compile(
        tmp_path,
        HOST_WITH_SHADOW,
        {
            "dir.xsd": SHADOWED.format(
                xs=XS,
                base=base,
                head_enum=head_enum,
                tail_enum=tail_enum,
                default=default,
            )
        },
    )
    assert _declaration_errors(schema) == []


# ---------------------------------------------------------------------------
# The rejection net stays closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1%x", "abc", "100%%"])
def test_invalid_percentage_default_is_rejected(tmp_path, value):
    """A value matching no member of the union stays a schema error.

    ``0%``-style percentages and bare integers each match one of the
    union's two members; the values here match neither.
    """
    schema = _percent_schema(tmp_path, font=value, lspc=value)
    assert _declaration_errors(schema, "fontScale")
    assert _declaration_errors(schema, "lnSpcReduction")


@pytest.mark.parametrize("value", ["normx", "minMaxx"])
def test_invalid_enumeration_default_is_rejected(tmp_path, value):
    """A value outside the shadowed type's enumeration stays an error."""
    schema = _compile(
        tmp_path,
        HOST_WITH_SHADOW,
        {
            "dir.xsd": SHADOWED.format(
                xs=XS, base="xs:token", head_enum="norm", tail_enum="rev", default=value
            )
        },
    )
    assert _declaration_errors(schema)


def test_ref_form_rejects_a_value_only_the_merge_winner_accepts(tmp_path):
    """The authored resolution — not the merged winner — drives the
    check: ``ltr`` is valid for ``b``'s ``code`` (the merge winner) but
    the use references ``a:code``, whose type rejects it."""
    schema = _ref_schema(tmp_path, "ltr")
    assert _declaration_errors(schema, "code")


def test_ref_form_invalid_default_is_rejected(tmp_path):
    """A value the referenced declaration's type rejects stays an error."""
    schema = _ref_schema(tmp_path, "bogus")
    assert _declaration_errors(schema, "code")


def test_builtin_wins_over_a_declaration_in_the_xsd_namespace(tmp_path):
    """A document targeting the XML Schema namespace cannot shadow a
    built-in in this check: ``hello`` is rejected by the user
    declaration's enumeration but is valid for ``xs:string``, and the
    value constraint must see the built-in (the user type does not
    leak)."""
    schema = _compile(tmp_path, XSD_NAMESPACED.format(default="hello"), {})
    assert _declaration_errors(schema, "val") == []


def test_user_type_in_an_ordinary_namespace_still_validates_its_facets(tmp_path):
    """The built-in exclusion reaches only the XML Schema namespace: the
    same shadowing declaration in an ordinary namespace is the
    reference's target and rejects what its facets reject."""
    schema = _compile(tmp_path, ORDINARY_NAMESPACED.format(default="hello"), {})
    assert _declaration_errors(schema, "val")


def test_shadowed_type_does_not_accept_the_other_declarations_values(tmp_path):
    """The check validates against the reference's document, not any
    same-local-name declaration: ``ltr`` is valid only for the host's
    own ``ST_Direction``, so the shadowed declaration rejects it.

    The imported document's attribute (``val``) is the one that must
    error — exactly once — while the host's own attribute (``hostVal``,
    carrying the same default against the host's own enumeration)
    stays clean.
    """
    schema = _compile(
        tmp_path,
        HOST_WITH_SHADOW,
        {
            "dir.xsd": SHADOWED.format(
                xs=XS, base="xs:token", head_enum="norm", tail_enum="rev", default="ltr"
            )
        },
    )
    errors = _declaration_errors(schema, "val")
    assert len(errors) == 1
    assert "default value 'ltr' is not valid for its type" in errors[0].message
    assert _declaration_errors(schema, "hostVal") == []
