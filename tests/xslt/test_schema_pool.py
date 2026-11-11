"""The immutable schema pool and its elementpath schema adapter.

``SchemaPool`` composes already-compiled pyxsd schemas: it owns imported
types, elements, attributes, substitution groups, and atomic-type
constructors, without introducing a second schema model.  The
``PyxsdSchemaProxy`` adapts those declarations to elementpath's schema
protocols.  These tests are corpus-independent.
"""

from __future__ import annotations

import io
from decimal import Decimal
from pathlib import Path

import pytest

from pyxsd import xsd_data_types as xsd
from pyxsd.schema import Schema
from pyxsd.xdm.schema import (
    AtomicConstructor,
    PyxsdElement,
    PyxsdSchemaProxy,
    PyxsdType,
    SchemaPool,
)
from pyxsd.xdm.values import XdmAtomic, XdmFunction, XdmMap

XSD_NS = "http://www.w3.org/2001/XMLSchema"
MONEY = "urn:money"
SHOP = "urn:shop"
OTHER = "urn:other"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _compile_text(text: str) -> Schema:
    return Schema.compile(io.StringIO(text))


def money_schema() -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MONEY}"
        xmlns:m="{MONEY}" elementFormDefault="qualified"
        attributeFormDefault="unqualified">
      <xs:simpleType name="Amount">
        <xs:restriction base="xs:decimal">
          <xs:totalDigits value="8"/>
        </xs:restriction>
      </xs:simpleType>
      <xs:simpleType name="AmountCode">
        <xs:restriction base="xs:string">
          <xs:pattern value="[A-Z]{{3}}"/>
        </xs:restriction>
      </xs:simpleType>
      <xs:simpleType name="Codes">
        <xs:list itemType="xs:string"/>
      </xs:simpleType>
      <xs:simpleType name="Kind">
        <xs:union memberTypes="xs:int m:AmountCode"/>
      </xs:simpleType>
      <xs:simpleType name="Identifier">
        <xs:restriction base="xs:ID"/>
      </xs:simpleType>
      <xs:simpleType name="QNameAlias">
        <xs:restriction base="xs:QName"/>
      </xs:simpleType>
      <xs:simpleType name="ID"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="IDREF"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="IDREFS"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="ENTITIES"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="QName"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="NOTATION"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="XsdList"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="TypeList"><xs:restriction base="xs:string"/></xs:simpleType>
      <xs:simpleType name="SmallInt">
        <xs:restriction base="xs:int"><xs:minInclusive value="0"/></xs:restriction>
      </xs:simpleType>
      <xs:simpleType name="IntList"><xs:list itemType="xs:int"/></xs:simpleType>
      <xs:simpleType name="SmallIntList"><xs:list itemType="m:SmallInt"/></xs:simpleType>
      <xs:simpleType name="RenamedIDREFS">
        <xs:restriction base="xs:IDREFS"/>
      </xs:simpleType>
      <xs:simpleType name="RenamedNMTOKENS">
        <xs:restriction base="xs:NMTOKENS"/>
      </xs:simpleType>
      <xs:complexType name="SimpleText">
        <xs:simpleContent><xs:extension base="xs:string"/></xs:simpleContent>
      </xs:complexType>
      <xs:complexType name="Empty"/>
      <xs:complexType name="Mixed" mixed="true">
        <xs:sequence><xs:element name="child" type="xs:string" minOccurs="0"/></xs:sequence>
      </xs:complexType>
      <xs:element name="typed" type="m:Amount"/>
      <xs:element name="payment">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="note" type="xs:string" minOccurs="0"/>
          </xs:sequence>
          <xs:attribute name="amount" type="m:Amount"/>
          <xs:attribute name="kind" type="m:Kind"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>"""


def shop_schema() -> str:
    return f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{SHOP}"
        xmlns:s="{SHOP}" elementFormDefault="qualified">
      <xs:simpleType name="Sku">
        <xs:restriction base="xs:string">
          <xs:pattern value="[A-Z]{{2}}[0-9]+"/>
        </xs:restriction>
      </xs:simpleType>
      <xs:attribute name="currency" type="xs:string"/>
      <xs:element name="head" type="xs:string" abstract="true"/>
      <xs:element name="special" type="s:Sku" substitutionGroup="s:head"/>
      <xs:element name="order">
        <xs:complexType>
          <xs:sequence>
            <xs:element ref="s:head" minOccurs="0"/>
          </xs:sequence>
          <xs:attribute name="status" type="xs:string" use="required"/>
          <xs:attribute ref="s:currency"/>
        </xs:complexType>
      </xs:element>
    </xs:schema>"""


def _pool(tmp_path: Path, *, xsd_version: str = "1.1") -> SchemaPool:
    money = _write(tmp_path / "money.xsd", money_schema())
    shop = _write(tmp_path / "shop.xsd", shop_schema())
    return SchemaPool.from_imports([money, shop], xsd_version=xsd_version)


def test_imported_constructor_uses_schema_pool(tmp_path: Path):
    pool = _pool(tmp_path)
    constructor = pool.constructor(f"{{{MONEY}}}Amount")
    assert constructor.arity == 1
    assert isinstance(constructor, AtomicConstructor)
    assert constructor.name == f"{{{MONEY}}}Amount"
    assert str(constructor("12.5")) == "12.5"

    function = constructor.as_function()
    assert isinstance(function, XdmFunction)
    assert function.arity == (1, 1)
    assert function.invoke(("12.5",))[0].lexical == "12.5"

    with pytest.raises(KeyError):
        pool.constructor(f"{{{MONEY}}}Codes")
    with pytest.raises(KeyError):
        pool.constructor(f"{{{MONEY}}}Kind")
    with pytest.raises(KeyError):
        pool.constructor("{urn:unknown}Missing")


def test_builtin_constructors_and_imported_atomic_types(tmp_path: Path):
    pool = _pool(tmp_path)
    builtin = pool.constructor(f"{{{XSD_NS}}}decimal")
    assert builtin.arity == 1
    assert builtin("1.25") == Decimal("1.25")

    atomic = {item.name for item in pool.proxy(MONEY).iter_atomic_types()}
    assert f"{{{MONEY}}}Amount" in atomic
    assert f"{{{MONEY}}}AmountCode" in atomic
    assert f"{{{MONEY}}}Identifier" in atomic
    assert f"{{{MONEY}}}QNameAlias" in atomic
    assert f"{{{MONEY}}}Codes" not in atomic
    assert f"{{{MONEY}}}Kind" not in atomic
    assert all(not item.is_builtin for item in pool.proxy(MONEY).iter_atomic_types())


def test_type_protocol_root_derivation_and_validation(tmp_path: Path):
    pool = _pool(tmp_path)
    amount = pool.get_type(f"{{{MONEY}}}Amount")
    assert isinstance(amount, PyxsdType)
    assert amount.name == f"{{{MONEY}}}Amount"
    assert amount.local_name == "Amount"
    assert amount.is_builtin is False
    assert amount.is_simple()
    assert amount.is_atomic()
    assert not amount.is_list()
    assert not amount.is_union()
    assert amount.root_type.name == f"{{{XSD_NS}}}decimal"
    assert amount.simple_type is amount
    assert amount.model_group is None
    assert amount.ref is None
    assert amount.xsd_version == "1.1"
    assert amount.maps is pool
    assert amount.is_matching("Amount")
    assert amount.is_matching(f"{{{MONEY}}}Amount")
    assert not amount.is_matching("Other")
    assert amount.is_valid("12.5")
    assert not amount.is_valid("not-a-decimal")
    amount.validate("12.5")
    with pytest.raises(ValueError):
        amount.validate("not-a-decimal")
    assert str(amount.decode("12.5")) == "12.5"

    codes = pool.get_type(f"{{{MONEY}}}Codes")
    assert codes.is_list()
    assert not codes.is_atomic()
    assert codes.root_type.name == f"{{{XSD_NS}}}string"
    assert codes.is_valid("a b")
    assert [str(item) for item in codes.decode("a b")] == ["a", "b"]

    kind = pool.get_type(f"{{{MONEY}}}Kind")
    assert kind.is_union()
    assert not kind.is_atomic()
    assert kind.root_type is kind
    assert kind.is_valid("5")
    assert kind.is_valid("ABC")
    assert not kind.is_valid("a")
    assert kind.decode("5") == 5

    identifier = pool.get_type(f"{{{MONEY}}}Identifier")
    assert identifier.is_key()
    qname = pool.get_type(f"{{{MONEY}}}QNameAlias")
    assert qname.is_qname()

    decimal = pool.get_type(f"{{{XSD_NS}}}decimal")
    assert decimal.is_builtin
    assert decimal.root_type is decimal
    assert decimal.name == f"{{{XSD_NS}}}decimal"

    assert pool.get_type("{urn:missing}Type") is None


def test_complex_type_and_element_attribute_adapters(tmp_path: Path):
    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)
    payment = pool.get_element(f"{{{MONEY}}}payment")
    assert payment is not None
    assert payment.name == f"{{{MONEY}}}payment"
    assert payment.tag == payment.name
    assert payment.text is None
    assert payment.tail is None
    assert payment.parent is proxy
    assert payment.type.name is None
    assert not payment.type.is_simple()
    assert payment.type.is_element_only()
    assert not payment.type.has_simple_content()
    assert not payment.type.has_mixed_content()
    assert payment.type.model_group is not None
    assert [element.name for element in payment.type.model_group.iter_elements()] == [
        f"{{{MONEY}}}note"
    ]

    amount_attribute = payment.attrib.get("amount")
    assert amount_attribute is not None
    assert amount_attribute.type.name == f"{{{MONEY}}}Amount"
    assert "amount" in payment.attrib
    assert "missing" not in payment.attrib
    assert dict(payment.attrib.items())["kind"].type.name == f"{{{MONEY}}}Kind"
    with pytest.raises(KeyError):
        payment.attrib["missing"]

    shop_proxy = pool.proxy(SHOP)
    order = pool.get_element(f"{{{SHOP}}}order")
    assert order is not None
    assert order.type.is_element_only()
    status = order.attrib["status"]
    assert status.name == "status"
    assert order.attrib.get("currency").type.name == f"{{{XSD_NS}}}string"
    assert order.attrib["status"].parent is order.type
    assert shop_proxy.get_type(f"{{{SHOP}}}Sku").is_simple()


def test_element_and_substitution_maps(tmp_path: Path):
    pool = _pool(tmp_path)
    names = set(pool.elements)
    assert f"{{{MONEY}}}payment" in names
    assert f"{{{SHOP}}}head" in names
    assert f"{{{SHOP}}}special" in names

    head = pool.get_element(f"{{{SHOP}}}head")
    special = pool.get_element(f"{{{SHOP}}}special")
    assert head is not None and special is not None
    members = pool.get_substitution_group(f"{{{SHOP}}}head")
    assert members == frozenset({special})
    assert pool.get_substitution_group(f"{{{SHOP}}}special") is None
    assert pool.get_element("{urn:missing}Missing") is None

    currency = pool.get_attribute(f"{{{SHOP}}}currency")
    assert currency is not None
    assert currency.type.name == f"{{{XSD_NS}}}string"
    assert pool.get_attribute("{urn:missing}Missing") is None


def test_schema_proxy_protocol_and_lookup(tmp_path: Path):
    from elementpath.etree import is_etree_element

    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)
    assert isinstance(proxy, PyxsdSchemaProxy)
    assert pool.proxy("{urn:unknown}") is None
    assert proxy.schema is proxy
    assert is_etree_element(proxy)
    assert proxy.tag == f"{{{XSD_NS}}}schema"
    assert dict(proxy.attrib) == {}
    assert proxy.text is None
    assert proxy.validity == "valid"
    assert proxy.validation_attempted == "full"
    assert proxy.is_fully_valid()
    assert proxy.xsd_version == "1.1"
    assert not proxy.is_assertion_based()
    assert proxy.is_matching(MONEY)
    assert not proxy.is_matching("urn:other")

    elements = list(proxy)
    assert {element.name for element in elements} == {
        f"{{{MONEY}}}payment",
        f"{{{MONEY}}}typed",
    }
    assert proxy.find(f"{{{MONEY}}}payment") is pool.get_element(f"{{{MONEY}}}payment")
    assert proxy.find("{urn:missing}Nothing") is None
    assert len(proxy) == len(elements)

    context = proxy.get_context()
    assert context.schema is proxy
    assert context.root.value is proxy


def test_proxy_lookup_prefix_resolution_and_cast(tmp_path: Path):
    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)
    from elementpath import XPath2Parser

    parser = XPath2Parser(namespaces={"m": MONEY})
    proxy.bind_parser(parser)
    assert parser.schema is proxy
    assert proxy.get_type("m:Amount") is pool.get_type(f"{{{MONEY}}}Amount")
    assert proxy.get_element("m:payment") is pool.get_element(f"{{{MONEY}}}payment")
    assert proxy.get_attribute("m:amount") is None

    compiled = parser.parse("m:Amount('12.5')")
    assert str(compiled.evaluate()) == "12.5"

    assert proxy.cast_as("12.5", "m:Amount") == Decimal("12.5")
    with pytest.raises((TypeError, ValueError)):
        proxy.cast_as("nope", "m:Amount")
    with pytest.raises(KeyError):
        proxy.cast_as("1", "m:Missing")
    assert proxy.is_instance("12.5", "m:Amount")
    assert not proxy.is_instance("nope", "m:Amount")
    with pytest.raises(KeyError):
        proxy.is_instance("1", "m:Missing")


def test_schema_pool_is_immutable(tmp_path: Path):
    pool = _pool(tmp_path)
    with pytest.raises(TypeError):
        pool.types["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        pool.elements["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        pool.attributes["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        pool.substitution_groups["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        pool.constructors["x"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        pool.schemas["x"] = None  # type: ignore[index]
    with pytest.raises(AttributeError):
        pool.types = {}  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pool.unexpected = None  # type: ignore[attr-defined]

    builtins = pool.get_type(f"{{{XSD_NS}}}string")
    assert builtins is not None


def test_pool_input_validation_and_versions(tmp_path: Path):
    money = _write(tmp_path / "money.xsd", money_schema())
    with pytest.raises(ValueError, match="duplicate"):
        SchemaPool.from_imports([money, money])

    pool = SchemaPool.from_imports([money], xsd_version="1.0")
    assert pool.xsd_version == "1.0"
    assert pool.proxy(MONEY).xsd_version == "1.0"
    assert pool.get_type(f"{{{MONEY}}}Amount").xsd_version == "1.0"

    with pytest.raises(ValueError, match="xsd_version"):
        SchemaPool.from_imports([money], xsd_version="2.0")


def test_from_schema_accepts_compiled_schema(tmp_path: Path):
    from pyxsd.schema import Schema

    money = _write(tmp_path / "money.xsd", money_schema())
    schema = Schema.compile(money)
    pool = SchemaPool.from_schema(schema)
    assert pool.proxy(MONEY) is not None
    assert pool.constructor(f"{{{MONEY}}}Amount").arity == 1
    assert str(pool.constructor(f"{{{MONEY}}}Amount")("1")) == "1"
    assert pool.get_type(f"{{{XSD_NS}}}string") is not None


def test_constructor_invoke_atomizes_xdm_arguments(tmp_path: Path):
    pool = _pool(tmp_path)
    constructor = pool.constructor(f"{{{MONEY}}}Amount")
    result = constructor.invoke((XdmAtomic.string("12.5"),))
    assert len(result) == 1
    assert result[0].lexical == "12.5"
    assert constructor.invoke((5,))[0].lexical == "5"
    with pytest.raises(TypeError):
        constructor.invoke(())
    with pytest.raises(TypeError):
        constructor.invoke(("1", "2"))
    with pytest.raises(TypeError):
        constructor.invoke((XdmMap.empty(),))


# ---------------------------------------------------------------------------
# Adapter surface and defensive branches


def test_type_adapter_surface_and_derivation(tmp_path: Path):
    from elementpath import XPath2Parser

    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)
    assert proxy is not None
    amount = pool.get_type(f"{{{MONEY}}}Amount")
    assert amount.schema is proxy
    assert amount.parent is proxy
    assert amount.ref is None
    assert amount.xsd_version == "1.1"
    assert amount.maps is pool
    assert repr(amount) == f"PyxsdType('{{{MONEY}}}Amount')"
    assert amount.is_matching(None) is False
    assert amount.is_matching("{urn:other}Amount") is False
    assert amount.has_simple_content() is True

    parser = XPath2Parser(namespaces={"m": MONEY})
    proxy.bind_parser(parser)
    assert amount.is_matching("m:Amount")
    assert not amount.is_matching("m:Other")

    typed = pool.get_element(f"{{{MONEY}}}typed")
    assert typed is not None
    assert typed.type is amount
    assert typed.type.schema is proxy
    assert typed.type.is_matching("m:Amount")
    assert pool.type_for_class(amount._type_class) is amount

    notation = pool.get_type(f"{{{XSD_NS}}}NOTATION")
    assert notation.is_notation()
    assert not notation.is_qname()
    idrefs = pool.get_type(f"{{{XSD_NS}}}IDREFS")
    assert idrefs.is_list()
    assert idrefs.root_type.name == f"{{{XSD_NS}}}IDREF"

    assert pool.get_type(f"{{{XSD_NS}}}anyAtomicType") is not None
    assert pool.get_type(f"{{{XSD_NS}}}List") is None
    assert f"{{{XSD_NS}}}List" not in pool.types

    assert pool.get_type(f"{{{XSD_NS}}}int").root_type.name == f"{{{XSD_NS}}}decimal"
    assert pool.get_type(f"{{{XSD_NS}}}ID").root_type.name == f"{{{XSD_NS}}}ID"
    assert pool.get_type(f"{{{XSD_NS}}}IDREF").root_type.name == f"{{{XSD_NS}}}IDREF"
    assert pool.get_type(f"{{{XSD_NS}}}anySimpleType").root_type.name == (
        f"{{{XSD_NS}}}anySimpleType"
    )
    assert pool.get_type(f"{{{XSD_NS}}}NMTOKENS").root_type.name == f"{{{XSD_NS}}}NMTOKEN"

    int_list = pool.get_type(f"{{{MONEY}}}IntList")
    assert int_list.is_list()
    assert int_list.root_type.name == f"{{{XSD_NS}}}decimal"
    small_int_list = pool.get_type(f"{{{MONEY}}}SmallIntList")
    assert small_int_list.root_type.name == f"{{{XSD_NS}}}decimal"

    user_id = pool.get_type(f"{{{MONEY}}}ID")
    assert not user_id.is_key()
    assert user_id.root_type.name == f"{{{XSD_NS}}}string"
    assert not pool.get_type(f"{{{MONEY}}}IDREF").is_key()
    assert not pool.get_type(f"{{{MONEY}}}IDREFS").is_list()
    assert not pool.get_type(f"{{{MONEY}}}ENTITIES").is_list()
    assert not pool.get_type(f"{{{MONEY}}}QName").is_qname()
    assert not pool.get_type(f"{{{MONEY}}}NOTATION").is_notation()
    assert not pool.get_type(f"{{{MONEY}}}XsdList").is_list()
    assert not pool.get_type(f"{{{MONEY}}}TypeList").is_list()
    assert pool.get_type(f"{{{MONEY}}}RenamedIDREFS").is_list()
    assert pool.get_type(f"{{{MONEY}}}RenamedIDREFS").root_type.name == (f"{{{XSD_NS}}}IDREF")
    assert pool.get_type(f"{{{MONEY}}}RenamedNMTOKENS").root_type.name == (f"{{{XSD_NS}}}NMTOKEN")

    assert amount.is_empty() is True

    simple_text = pool.get_type(f"{{{MONEY}}}SimpleText")
    assert simple_text.simple_type.name == f"{{{XSD_NS}}}string"
    assert simple_text.is_empty() is False
    assert simple_text.has_simple_content() is True

    empty = pool.get_type(f"{{{MONEY}}}Empty")
    assert empty.is_empty() is True

    class BareEmptyType:
        _contentKind_ = "complex"
        _elementOnly_ = True

    assert PyxsdType(pool, BareEmptyType).is_empty() is True

    mixed = pool.get_type(f"{{{MONEY}}}Mixed")
    assert mixed.has_mixed_content() is True
    assert not mixed.is_element_only()

    payment = pool.get_element(f"{{{MONEY}}}payment")
    assert payment is not None
    assert payment.type.root_type.name == f"{{{XSD_NS}}}anyType"
    assert payment.type.cast("x") == "x"
    assert payment.type.decode("x") == "x"
    assert payment.type.is_valid(object()) is False
    assert payment.type.simple_type is None
    assert payment.type.attributes is payment.type.attributes
    assert payment.type.model_group is payment.type.model_group

    value = amount._type_class("1.5")
    assert amount.cast(value) is value


def test_complex_type_accepts_bound_instance(tmp_path: Path):
    money = _write(tmp_path / "money.xsd", money_schema())
    schema = Schema.compile(money)
    document = schema.parse(io.BytesIO(b'<m:payment xmlns:m="urn:money" amount="1.5"/>'))
    pool = SchemaPool.from_schema(schema)
    payment = pool.get_element(f"{{{MONEY}}}payment")
    assert payment is not None
    assert payment.type.is_valid(document.root)
    payment.type.validate(document.root)


def test_attribute_and_group_protocol_surface(tmp_path: Path):
    pool = _pool(tmp_path)
    payment = pool.get_element(f"{{{MONEY}}}payment")
    assert payment is not None
    amount = payment.attrib["amount"]
    assert amount.tag == amount.name
    assert amount.attrib == {}
    assert amount.text is None
    assert amount.tail is None
    assert amount.ref is None
    assert amount.xsd_version == "1.1"
    assert amount.maps is pool
    assert amount.is_matching(None) is False
    assert amount.is_matching("amount")
    assert not amount.is_matching("other")
    assert amount.is_matching("{urn:other}amount") is False
    assert amount.get("missing", "default") == "default"
    assert list(amount) == []
    assert len(amount) == 0
    assert isinstance(hash(amount), int)

    group = payment.attrib
    assert group is payment.type.attributes
    assert group.get(None) is None
    assert group.get("missing") is None
    assert dict(group.items())["amount"] is amount
    assert "amount" in group
    assert "missing" not in group
    assert 3 not in group
    assert list(group) == ["amount", "kind"]
    assert len(group) == 2
    assert isinstance(hash(group), int)
    assert group.parent is payment.type
    assert group.xsd_version == "1.1"
    assert group.maps is pool
    assert group.is_matching("amount")
    assert not group.is_matching("missing")

    model = payment.type.model_group
    assert model is not None
    assert model.name is None
    assert model.parent is payment.type
    assert model.ref is None
    assert model.xsd_version == "1.1"
    assert model.maps is pool
    assert model.is_matching("note")
    assert model.is_matching(f"{{{MONEY}}}note")
    assert model.is_matching(None) is False
    assert len(list(model.iter_elements())) == 1


def test_element_protocol_surface(tmp_path: Path):
    pool = _pool(tmp_path)
    order = pool.get_element(f"{{{SHOP}}}order")
    assert order is not None
    assert order.tag == order.name
    assert order.text is None
    assert order.tail is None
    assert order.ref is None
    assert order.xsd_version == "1.1"
    assert order.maps is pool
    assert order.is_matching(None) is False
    assert order.is_matching(order.name)
    assert order.is_matching("{urn:other}order") is False
    assert order.is_matching(f"{{{SHOP}}}order")
    assert not order.is_matching("missing")
    assert order.is_matching("order", SHOP)
    assert not order.is_matching("order", OTHER)
    from elementpath import XPath2Parser

    order.parent.bind_parser(XPath2Parser(namespaces={"s": SHOP}))
    assert order.is_matching("s:order")
    assert order.get("missing", "default") == "default"
    assert list(order) == list(iter(order))
    assert order.find("./missing") is None
    assert order.find(f"./{{{SHOP}}}head") is not None
    assert len(order) == len(list(order))
    assert isinstance(hash(order), int)
    assert order.is_valid(object()) is False

    child = next(iter(order))
    assert list(order.iter(child.name)) == [child]
    assert next(iter(order.iter("*"))) is order

    with pytest.raises(ValueError):
        order.validate(object())
    assert order.type.decode("x") == "x"
    assert order.decode("x") == "x"


def test_element_without_resolved_type_uses_empty_surface(tmp_path: Path):
    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)

    class NoType:
        name = "broken"
        expandedName = "broken"

    element = PyxsdElement(pool, NoType(), proxy, name="broken")
    assert element.type is None
    assert element.attrib == {}
    assert element.is_valid("x") is False
    with pytest.raises(ValueError):
        element.validate("x")
    assert element.decode("x") == "x"

    parentless = PyxsdElement(pool, NoType(), None, name="broken")
    assert not parentless.is_matching("p:broken")


def test_schema_proxy_surface(tmp_path: Path):
    from elementpath import XPath2Parser

    pool = _pool(tmp_path)
    proxy = pool.proxy(MONEY)
    assert proxy is not None
    assert proxy.pool is pool
    assert proxy.name == MONEY
    assert proxy.base_element is None
    assert proxy.attrib == {}
    assert proxy.text is None
    assert proxy.tail is None
    assert proxy.maps is pool
    assert list(proxy.iter("*")) == list(proxy)
    assert list(proxy.iter("missing")) == []
    assert proxy.get("missing", "default") == "default"
    assert proxy.find(".") is proxy
    assert proxy.find("./payment") is pool.get_element(f"{{{MONEY}}}payment")
    assert proxy.find("./missing") is None
    assert proxy.get_substitution_group("{urn:missing}head") is None
    assert isinstance(hash(proxy), int)
    assert "urn:money" in repr(proxy)

    parser = XPath2Parser(namespaces={"m": MONEY})
    proxy.bind_parser(parser)
    proxy.bind_parser(parser)  # idempotent second bind
    assert parser.schema is proxy

    bare = XPath2Parser(namespaces={})
    proxy.bind_parser(bare)  # unprefixed type QNames are not registered
    assert bare.schema is proxy

    assert proxy.expand_name("Amount") == "Amount"
    with pytest.raises(KeyError, match="unknown namespace prefix"):
        proxy.expand_name("missing:Amount")
    assert proxy.cast_as("1", f"{{{MONEY}}}Amount") == Decimal("1")
    assert proxy.is_instance("1", f"{{{MONEY}}}Amount")


def test_pool_lookup_and_registration_edges(tmp_path: Path):
    pool = _pool(tmp_path)
    assert pool.get_type(None) is None
    assert pool.get_element(None) is None
    assert pool.get_attribute(None) is None
    assert pool.get_substitution_group(None) is None
    assert pool.get_type("Missing") is None
    assert pool.get_type("Amount") is None
    assert pool.get_type(f"{{{MONEY}}}Amount") is not None

    with pytest.raises(ValueError, match="xsd_version"):
        SchemaPool([], xsd_version="2.0")

    class NoTypeGetter:
        pass

    assert pool.type_for_declaration(NoTypeGetter()) is None

    class RaisingType:
        def getType(self):
            raise RuntimeError("broken")

    assert pool.type_for_declaration(RaisingType()) is None

    class NonType:
        def getType(self):
            return "not-a-class"

    assert pool.type_for_declaration(NonType()) is None


def test_duplicate_local_type_lookup_is_ambiguous(tmp_path: Path):
    left = _write(
        tmp_path / "left.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="urn:left">
          <xs:simpleType name="Dup"><xs:restriction base="xs:string"/></xs:simpleType>
        </xs:schema>""",
    )
    right = _write(
        tmp_path / "right.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="urn:right">
          <xs:simpleType name="Dup"><xs:restriction base="xs:int"/></xs:simpleType>
        </xs:schema>""",
    )
    pool = SchemaPool.from_imports([left, right])
    assert pool.get_type("Dup") is None
    assert pool.get_type("{urn:left}Dup") is not None
    assert pool.get_type("{urn:right}Dup") is not None


def test_imported_global_attributes_are_registered(tmp_path: Path):
    _write(
        tmp_path / "other.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}">
          <xs:attribute name="otherAttr" type="xs:string"/>
        </xs:schema>""",
    )
    main = _write(
        tmp_path / "main.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MONEY}">
          <xs:import namespace="{OTHER}" schemaLocation="other.xsd"/>
          <xs:attribute name="ownAttr" type="xs:string"/>
        </xs:schema>""",
    )
    pool = SchemaPool.from_imports([main])
    assert pool.get_attribute(f"{{{MONEY}}}ownAttr") is not None
    imported = pool.get_attribute(f"{{{OTHER}}}otherAttr")
    assert imported is not None
    assert imported.type.name == f"{{{XSD_NS}}}string"


def test_schema_invalidity_and_identity_matching(tmp_path: Path):
    bad = _write(
        tmp_path / "bad.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="urn:bad">
          <xs:element name="root" type="missing:Type"/>
        </xs:schema>""",
    )
    schema = Schema.compile(bad)
    assert schema.report.has_errors
    pool = SchemaPool([schema])
    proxy = pool.proxy("urn:bad")
    assert proxy is not None
    assert proxy.validity == "invalid"
    assert proxy.validation_attempted == "partial"
    assert not proxy.is_fully_valid()
    assert proxy.is_matching(None)
    assert proxy.is_matching("")
    assert not proxy.is_matching("urn:other")


def test_constructor_invoke_rejects_non_iterable(tmp_path: Path):
    pool = _pool(tmp_path)
    constructor = pool.constructor(f"{{{MONEY}}}Amount")
    with pytest.raises(TypeError):
        constructor.invoke("12.5")


def test_malformed_global_declarations_are_skipped_without_crashing():
    anon_type = _compile_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:simpleType><xs:restriction base="xs:string"/></xs:simpleType>
          <xs:element name="root" type="xs:string"/>
        </xs:schema>"""
    )
    pool = SchemaPool.from_schema(anon_type)
    assert pool.get_element("root") is not None

    nameless = _compile_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:element type="xs:string"/>
          <xs:attribute type="xs:string"/>
          <xs:element name="root" type="xs:string"/>
        </xs:schema>"""
    )
    nameless_pool = SchemaPool.from_schema(nameless)
    assert nameless_pool.get_element("root") is not None
    assert nameless_pool.elements == {"root": nameless_pool.get_element("root")}

    headless_member = _compile_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"
            xmlns:o="{OTHER}" elementFormDefault="qualified">
          <xs:element name="head" type="xs:string" abstract="true"/>
          <xs:element type="xs:string" substitutionGroup="o:head"/>
          <xs:element name="root" type="xs:string"/>
        </xs:schema>"""
    )
    assert (
        SchemaPool.from_schema(headless_member).get_substitution_group(f"{{{OTHER}}}head") is None
    )


def test_no_target_namespace_type_names_remain_local():
    schema = _compile_text(
        f"""<xs:schema xmlns:xs="{XSD_NS}">
          <xs:simpleType name="Local"><xs:restriction base="xs:string"/></xs:simpleType>
          <xs:element name="root" type="Local"/>
        </xs:schema>"""
    )
    pool = SchemaPool.from_schema(schema)
    local = pool.get_type("Local")
    assert local is not None
    assert local.name == "Local"
    assert pool.get_element("root").type is local


def test_canonical_named_type_adapter_is_reused_by_clark_name(tmp_path: Path):
    pool = _pool(tmp_path)
    amount = pool.get_type(f"{{{MONEY}}}Amount")
    assert amount is not None
    # A second class object carrying the same schema type identity resolves
    # to the one canonical adapter rather than creating a proxy-less clone.
    clone = type(
        "Amount",
        (xsd.String,),
        {"_targetNamespace_": MONEY, "schema": amount.schema},
    )
    assert pool.type_for_class(clone) is amount

    unknown = type(
        "Unknown",
        (xsd.String,),
        {"_targetNamespace_": "urn:unknown", "schema": amount.schema},
    )
    unknown_adapter = pool.type_for_class(unknown)
    assert unknown_adapter.name == "{urn:unknown}Unknown"
    assert unknown_adapter.schema is None


def test_imported_substitution_group_registers_once(tmp_path: Path):
    other = _write(
        tmp_path / "other.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{OTHER}"
            xmlns:o="{OTHER}" elementFormDefault="qualified">
          <xs:element name="head" type="xs:string" abstract="true"/>
          <xs:element name="member" type="xs:string" substitutionGroup="o:head"/>
        </xs:schema>""",
    )
    main = _write(
        tmp_path / "main.xsd",
        f"""<xs:schema xmlns:xs="{XSD_NS}" targetNamespace="{MONEY}"
            xmlns:m="{MONEY}">
          <xs:import namespace="{OTHER}" schemaLocation="other.xsd"/>
          <xs:element name="root" type="xs:string"/>
        </xs:schema>""",
    )
    pool = SchemaPool.from_imports([main, other])
    group = pool.get_substitution_group(f"{{{OTHER}}}head")
    assert group is not None
    assert len(group) == 1
    assert next(iter(group)).name == f"{{{OTHER}}}member"
