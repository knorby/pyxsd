"""Tests for the invoice showcase workflow (UBL 2.3 / EN 16931).

The offline half builds a tiny UBL-shaped schema in ``tmp_path`` so the
facts and HTML renderer stay covered in a clean checkout. The real-schema
half binds the pinned KoSIT XRechnung corpus and skips when the gitignored
cache is absent.
"""

from __future__ import annotations

import json

import pytest
from examples.showcase.invoice.render_html import (
    FOOTER_COMMENT,
    invoice_facts,
    money,
    percent,
    render_html,
)

from pyxsd import Schema
from pyxsd.binding import ParseModes

CBC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC_NS = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
INV_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"

CBC_SCHEMA = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:cbc="{CBC_NS}"
           targetNamespace="{CBC_NS}"
           elementFormDefault="qualified">
  <xs:element name="ID" type="xs:string"/>
  <xs:element name="IssueDate" type="xs:date"/>
  <xs:element name="DueDate" type="xs:date"/>
  <xs:element name="InvoiceTypeCode" type="xs:string"/>
  <xs:element name="DocumentCurrencyCode" type="xs:string"/>
  <xs:element name="BuyerReference" type="xs:string"/>
  <xs:element name="Note" type="xs:string"/>
  <xs:element name="Name" type="xs:string"/>
  <xs:element name="Description" type="xs:string"/>
  <xs:element name="StreetName" type="xs:string"/>
  <xs:element name="CityName" type="xs:string"/>
  <xs:element name="PostalZone" type="xs:string"/>
  <xs:element name="IdentificationCode" type="xs:string"/>
  <xs:element name="CompanyID" type="xs:string"/>
  <xs:element name="RegistrationName" type="xs:string"/>
  <xs:element name="EndpointID">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:string">
          <xs:attribute name="schemeID" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
  <xs:element name="InvoicedQuantity">
    <xs:complexType>
      <xs:simpleContent>
        <xs:extension base="xs:decimal">
          <xs:attribute name="unitCode" type="xs:string"/>
        </xs:extension>
      </xs:simpleContent>
    </xs:complexType>
  </xs:element>
  <xs:element name="LineExtensionAmount" type="xs:decimal"/>
  <xs:element name="PriceAmount" type="xs:decimal"/>
  <xs:element name="TaxableAmount" type="xs:decimal"/>
  <xs:element name="TaxAmount" type="xs:decimal"/>
  <xs:element name="TaxExclusiveAmount" type="xs:decimal"/>
  <xs:element name="TaxInclusiveAmount" type="xs:decimal"/>
  <xs:element name="PayableAmount" type="xs:decimal"/>
  <xs:element name="Percent" type="xs:decimal"/>
  <xs:element name="ChargeIndicator" type="xs:boolean"/>
  <xs:element name="Amount" type="xs:decimal"/>
  <xs:element name="AllowanceChargeReason" type="xs:string"/>
</xs:schema>
"""

CAC_SCHEMA = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:cac="{CAC_NS}"
           xmlns:cbc="{CBC_NS}"
           targetNamespace="{CAC_NS}"
           elementFormDefault="qualified">
  <xs:import namespace="{CBC_NS}" schemaLocation="cbc.xsd"/>

  <xs:complexType name="PartyNameType">
    <xs:sequence>
      <xs:element ref="cbc:Name" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="PartyName" type="cac:PartyNameType"/>

  <xs:complexType name="CountryType">
    <xs:sequence>
      <xs:element ref="cbc:IdentificationCode" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Country" type="cac:CountryType"/>

  <xs:complexType name="PostalAddressType">
    <xs:sequence>
      <xs:element ref="cbc:StreetName" minOccurs="0"/>
      <xs:element ref="cbc:CityName" minOccurs="0"/>
      <xs:element ref="cbc:PostalZone" minOccurs="0"/>
      <xs:element ref="cac:Country" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="PostalAddress" type="cac:PostalAddressType"/>

  <xs:complexType name="TaxSchemeType">
    <xs:sequence>
      <xs:element ref="cbc:ID" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="TaxScheme" type="cac:TaxSchemeType"/>

  <xs:complexType name="PartyTaxSchemeType">
    <xs:sequence>
      <xs:element ref="cbc:CompanyID" minOccurs="0"/>
      <xs:element ref="cac:TaxScheme" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="PartyTaxScheme" type="cac:PartyTaxSchemeType"/>

  <xs:complexType name="PartyLegalEntityType">
    <xs:sequence>
      <xs:element ref="cbc:RegistrationName" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="PartyLegalEntity" type="cac:PartyLegalEntityType"/>

  <xs:complexType name="PartyType">
    <xs:sequence>
      <xs:element ref="cbc:EndpointID" minOccurs="0"/>
      <xs:element ref="cac:PartyName" minOccurs="0"/>
      <xs:element ref="cbc:ID" minOccurs="0"/>
      <xs:element ref="cac:PostalAddress" minOccurs="0"/>
      <xs:element ref="cac:PartyTaxScheme" minOccurs="0"/>
      <xs:element ref="cac:PartyLegalEntity" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Party" type="cac:PartyType"/>

  <xs:complexType name="SupplierPartyType">
    <xs:sequence>
      <xs:element ref="cac:Party"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="AccountingSupplierParty" type="cac:SupplierPartyType"/>

  <xs:complexType name="CustomerPartyType">
    <xs:sequence>
      <xs:element ref="cac:Party"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="AccountingCustomerParty" type="cac:CustomerPartyType"/>

  <xs:complexType name="AllowanceChargeType">
    <xs:sequence>
      <xs:element ref="cbc:ChargeIndicator"/>
      <xs:element ref="cbc:Amount"/>
      <xs:element ref="cbc:AllowanceChargeReason" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="AllowanceCharge" type="cac:AllowanceChargeType"/>

  <xs:complexType name="TaxCategoryType">
    <xs:sequence>
      <xs:element ref="cbc:ID" minOccurs="0"/>
      <xs:element ref="cbc:Percent" minOccurs="0"/>
      <xs:element ref="cac:TaxScheme" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="TaxCategory" type="cac:TaxCategoryType"/>

  <xs:complexType name="TaxSubtotalType">
    <xs:sequence>
      <xs:element ref="cbc:TaxableAmount" minOccurs="0"/>
      <xs:element ref="cbc:TaxAmount" minOccurs="0"/>
      <xs:element ref="cac:TaxCategory" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="TaxSubtotal" type="cac:TaxSubtotalType"/>

  <xs:complexType name="TaxTotalType">
    <xs:sequence>
      <xs:element ref="cbc:TaxAmount" minOccurs="0"/>
      <xs:element ref="cac:TaxSubtotal" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="TaxTotal" type="cac:TaxTotalType"/>

  <xs:complexType name="LegalMonetaryTotalType">
    <xs:sequence>
      <xs:element ref="cbc:LineExtensionAmount" minOccurs="0"/>
      <xs:element ref="cbc:TaxExclusiveAmount" minOccurs="0"/>
      <xs:element ref="cbc:TaxInclusiveAmount" minOccurs="0"/>
      <xs:element ref="cbc:PayableAmount" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="LegalMonetaryTotal" type="cac:LegalMonetaryTotalType"/>

  <xs:complexType name="PriceType">
    <xs:sequence>
      <xs:element ref="cbc:PriceAmount" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Price" type="cac:PriceType"/>

  <xs:complexType name="ItemType">
    <xs:sequence>
      <xs:element ref="cbc:Description" minOccurs="0"/>
      <xs:element ref="cbc:Name" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Item" type="cac:ItemType"/>

  <xs:complexType name="InvoiceLineType">
    <xs:sequence>
      <xs:element ref="cbc:ID" minOccurs="0"/>
      <xs:element ref="cbc:InvoicedQuantity" minOccurs="0"/>
      <xs:element ref="cbc:LineExtensionAmount" minOccurs="0"/>
      <xs:element ref="cac:Item" minOccurs="0"/>
      <xs:element ref="cac:Price" minOccurs="0"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="InvoiceLine" type="cac:InvoiceLineType"/>
</xs:schema>
"""

INVOICE_SCHEMA = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns="{INV_NS}"
           xmlns:cbc="{CBC_NS}"
           xmlns:cac="{CAC_NS}"
           targetNamespace="{INV_NS}"
           elementFormDefault="qualified">
  <xs:import namespace="{CBC_NS}" schemaLocation="cbc.xsd"/>
  <xs:import namespace="{CAC_NS}" schemaLocation="cac.xsd"/>

  <xs:complexType name="InvoiceType">
    <xs:sequence>
      <xs:element ref="cbc:ID"/>
      <xs:element ref="cbc:IssueDate"/>
      <xs:element ref="cbc:DueDate" minOccurs="0"/>
      <xs:element ref="cbc:InvoiceTypeCode" minOccurs="0"/>
      <xs:element ref="cbc:DocumentCurrencyCode" minOccurs="0"/>
      <xs:element ref="cbc:BuyerReference" minOccurs="0"/>
      <xs:element ref="cbc:Note" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element ref="cac:AccountingSupplierParty"/>
      <xs:element ref="cac:AccountingCustomerParty"/>
      <xs:element ref="cac:AllowanceCharge" minOccurs="0" maxOccurs="unbounded"/>
      <xs:element ref="cac:TaxTotal"/>
      <xs:element ref="cac:LegalMonetaryTotal"/>
      <xs:element ref="cac:InvoiceLine" maxOccurs="unbounded"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="Invoice" type="InvoiceType"/>
</xs:schema>
"""

#: Two lines, a document-level allowance, and 19 % VAT. ``PayableAmount``
#: is written ``404.6`` on purpose: facts must preserve that lexical form
#: while the renderer shows ``404.60``. The supplier name carries both an
#: ampersand (escaping) and a German label (UTF-8).
INVOICE_INSTANCE = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="{INV_NS}"
         xmlns:cbc="{CBC_NS}"
         xmlns:cac="{CAC_NS}">
  <cbc:ID>INV-2026-001</cbc:ID>
  <cbc:IssueDate>2026-03-01</cbc:IssueDate>
  <cbc:DueDate>2026-03-31</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cbc:BuyerReference>04011000-12345-03</cbc:BuyerReference>
  <cbc:Note>Lieferung frei Haus.</cbc:Note>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cbc:EndpointID schemeID="EM">billing@example.de</cbc:EndpointID>
      <cac:PartyName>
        <cbc:Name>Müller &amp; Söhne GmbH</cbc:Name>
      </cac:PartyName>
      <cac:PostalAddress>
        <cbc:StreetName>Hauptstraße 1</cbc:StreetName>
        <cbc:CityName>Berlin</cbc:CityName>
        <cbc:PostalZone>10115</cbc:PostalZone>
        <cac:Country>
          <cbc:IdentificationCode>DE</cbc:IdentificationCode>
        </cac:Country>
      </cac:PostalAddress>
      <cac:PartyTaxScheme>
        <cbc:CompanyID>DE123456789</cbc:CompanyID>
        <cac:TaxScheme>
          <cbc:ID>VAT</cbc:ID>
        </cac:TaxScheme>
      </cac:PartyTaxScheme>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyName>
        <cbc:Name>Beispiel AG</cbc:Name>
      </cac:PartyName>
      <cac:PostalAddress>
        <cbc:StreetName>Musterstraße 2</cbc:StreetName>
        <cbc:CityName>Hamburg</cbc:CityName>
        <cbc:PostalZone>20095</cbc:PostalZone>
        <cac:Country>
          <cbc:IdentificationCode>DE</cbc:IdentificationCode>
        </cac:Country>
      </cac:PostalAddress>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>Beispiel AG</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:AllowanceCharge>
    <cbc:ChargeIndicator>false</cbc:ChargeIndicator>
    <cbc:Amount>10.00</cbc:Amount>
    <cbc:AllowanceChargeReason>Rabatt</cbc:AllowanceChargeReason>
  </cac:AllowanceCharge>
  <cac:TaxTotal>
    <cbc:TaxAmount>64.60</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount>340.00</cbc:TaxableAmount>
      <cbc:TaxAmount>64.60</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>19</cbc:Percent>
        <cac:TaxScheme>
          <cbc:ID>VAT</cbc:ID>
        </cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount>350.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount>340.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount>404.6</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount>404.6</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">2</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount>200.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Description>Widget &amp; Gasket</cbc:Description>
      <cbc:Name>Widget &amp; Gasket</cbc:Name>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount>100.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>2</cbc:ID>
    <cbc:InvoicedQuantity unitCode="H87">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount>150.00</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>Montage</cbc:Name>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount>150.00</cbc:PriceAmount>
    </cac:Price>
  </cac:InvoiceLine>
</Invoice>
"""


@pytest.fixture(scope="module")
def synthetic_document(tmp_path_factory):
    root = tmp_path_factory.mktemp("ubl-subset")
    (root / "cbc.xsd").write_text(CBC_SCHEMA, encoding="utf-8")
    (root / "cac.xsd").write_text(CAC_SCHEMA, encoding="utf-8")
    (root / "invoice.xsd").write_text(INVOICE_SCHEMA, encoding="utf-8")
    instance = root / "invoice.xml"
    instance.write_text(INVOICE_INSTANCE, encoding="utf-8")
    schema = Schema.compile(root / "invoice.xsd", mode=ParseModes.NAMESPACED)
    document = schema.parse(instance)
    assert document.report.error_count == 0, document.report
    return document


def test_invoice_facts_extract_exact_values(synthetic_document):
    facts = invoice_facts(synthetic_document)
    assert facts["id"] == "INV-2026-001"
    assert facts["issue_date"] == "2026-03-01"
    assert facts["due_date"] == "2026-03-31"
    assert facts["type_code"] == "380"
    assert facts["currency"] == "EUR"
    assert facts["buyer_reference"] == "04011000-12345-03"
    assert facts["notes"] == ["Lieferung frei Haus."]
    assert facts["supplier"] == {
        "name": "Müller & Söhne GmbH",
        "address": ["Hauptstraße 1", "10115 Berlin", "DE"],
        "tax_id": "DE123456789",
        "endpoint": "billing@example.de",
        "endpoint_scheme": "EM",
    }
    # No PartyTaxScheme / EndpointID on the buyer: absent stays absent.
    assert facts["customer"]["name"] == "Beispiel AG"
    assert facts["customer"]["address"] == ["Musterstraße 2", "20095 Hamburg", "DE"]
    assert facts["customer"]["tax_id"] is None
    assert facts["customer"]["endpoint"] is None
    assert facts["customer"]["endpoint_scheme"] is None
    assert facts["totals"] == {
        "line_extension": "350.00",
        "tax_exclusive": "340.00",
        "tax_inclusive": "404.6",
        "payable": "404.6",
    }
    # Exact lexical form: 404.6 must not be normalized to 404.60 or a float.
    assert facts["totals"]["payable"] != "404.60"
    assert facts["allowances"] == [{"charge": False, "amount": "10.00", "reason": "Rabatt"}]
    assert facts["tax_subtotals"] == [
        {
            "category": "S",
            "percent": "19",
            "taxable_amount": "340.00",
            "tax_amount": "64.60",
        }
    ]
    assert facts["lines"] == [
        {
            "id": "1",
            "description": "Widget & Gasket",
            "quantity": "2",
            "unit_code": "C62",
            "line_extension": "200.00",
            "price": "100.00",
        },
        {
            "id": "2",
            # No cbc:Description on this line: the item name is the fallback.
            "description": "Montage",
            "quantity": "1",
            "unit_code": "H87",
            "line_extension": "150.00",
            "price": "150.00",
        },
    ]


def test_invoice_facts_are_json_safe(synthetic_document):
    facts = invoice_facts(synthetic_document)
    assert json.loads(json.dumps(facts, ensure_ascii=False)) == facts
    assert isinstance(facts["totals"]["payable"], str)


def test_money_and_percent_format_only_for_display():
    assert money("336.9") == "336.90"
    assert money("350.00") == "350.00"
    assert money(None) == "\u2014"
    assert percent("19.0") == "19"
    assert percent(None) == "\u2014"


def test_render_html_escapes_and_formats_the_invoice(synthetic_document):
    html = render_html(synthetic_document)
    assert html.startswith("<!DOCTYPE html>")
    # Text nodes are escaped; the German label survives as UTF-8 content.
    assert "Müller &amp; Söhne GmbH" in html
    assert "Müller & Söhne GmbH" not in html
    assert "Widget &amp; Gasket" in html
    assert "Hauptstraße 1" in html
    assert "Musterstraße 2" in html
    # Amounts are formatted with two decimals only at this layer.
    assert "404.60" in html
    assert "350.00" in html
    assert "64.60" in html
    assert "19 %" in html
    # Unit codes make the quantities meaningful; both rendered lines appear.
    assert "C62" in html
    assert "H87" in html
    assert "Montage" in html
    assert 'class="grand"' in html
    assert FOOTER_COMMENT in html
    assert "<!-- Rendered server-side by pyxsd" in html


def test_render_html_is_deterministic(synthetic_document):
    first = render_html(synthetic_document).encode("utf-8")
    second = render_html(synthetic_document).encode("utf-8")
    assert first == second
