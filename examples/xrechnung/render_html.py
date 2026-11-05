"""Turn a bound UBL invoice into business facts and a standalone HTML page.

Compile the OASIS UBL 2.3 schema once, bind an EN 16931 / XRechnung
invoice, extract a small typed view with :func:`invoice_facts`, and
render it with plain Python; the same facts drive the PDF renderer in
``render_pdf.py``.

Access pattern: on this ref-based UBL schema, descriptor reads such as
``document.root.ID`` return ``None`` even though binding and XPath work,
so the facts come from ``document.to_dict(typed=False)`` (Clark-notation
keys). Values stay the exact lexical strings from the document: ``"336.9"``
must not become ``336.90`` or a float before rendering. Two decimals are a
display decision, applied by :func:`money` only.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

from pyxsd import Document

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.xrechnung.common.render import esc
from examples.xrechnung.common.schemas import compile_schema
from examples.xrechnung.common.sources import SETUP_HINT, require_cache

CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"

#: First file of the pinned KoSIT XRechnung suite, used when no input is
#: given on the command line.
DEFAULT_SAMPLE = "01.01a-INVOICE_ubl.xml"

_DASH = "\u2014"


def _key(namespace: str, name: str) -> str:
    """Clark-notation key used by ``Document.to_dict``."""
    return f"{{{namespace}}}{name}"


def _first(value: object) -> object:
    """A single export node is a value; repeated children become a list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text(value: object) -> str | None:
    """Character data of an exported element (``$`` once it has attributes)."""
    value = _first(value)
    if isinstance(value, dict):
        value = value.get("$")
    return value if isinstance(value, str) else None


def _mapping(value: object) -> dict:
    """The export node for an element-only child (empty when absent)."""
    value = _first(value)
    return value if isinstance(value, dict) else {}


def _elements(container: dict, key: str) -> list[dict]:
    """Every element-only child under *key*, normalizing single to repeated."""
    value = container.get(key)
    items = value if isinstance(value, list) else [value]
    return [item for item in items if isinstance(item, dict)]


def _strings(value: object) -> list[str]:
    """Every non-empty character value of a (possibly repeated) simple child."""
    items = value if isinstance(value, list) else [value]
    texts = (_text(item) for item in items)
    return [text for text in texts if text]


def _address(party: dict) -> list[str]:
    """Postal address as display lines: street, postal code + city, country."""
    address = _mapping(party.get(_key(CAC, "PostalAddress")))
    lines = []
    street = _text(address.get(_key(CBC, "StreetName")))
    if street:
        lines.append(street)
    city = _text(address.get(_key(CBC, "CityName")))
    postal = _text(address.get(_key(CBC, "PostalZone")))
    locality = " ".join(part for part in (postal, city) if part)
    if locality:
        lines.append(locality)
    country = _text(
        _mapping(address.get(_key(CAC, "Country"))).get(_key(CBC, "IdentificationCode"))
    )
    if country:
        lines.append(country)
    return lines


def _party(container: dict) -> dict:
    """Name, address, tax id, and routing endpoint of one invoice party."""
    party = _mapping(container.get(_key(CAC, "Party")))
    legal = _mapping(party.get(_key(CAC, "PartyLegalEntity")))
    name = _text(_mapping(party.get(_key(CAC, "PartyName"))).get(_key(CBC, "Name")))
    if not name:
        name = _text(legal.get(_key(CBC, "RegistrationName")))
    endpoint = _first(party.get(_key(CBC, "EndpointID")))
    return {
        "name": name,
        "address": _address(party),
        "tax_id": _text(
            _mapping(party.get(_key(CAC, "PartyTaxScheme"))).get(_key(CBC, "CompanyID"))
        ),
        "endpoint": _text(endpoint),
        "endpoint_scheme": endpoint.get("@schemeID") if isinstance(endpoint, dict) else None,
    }


def _totals(root: dict) -> dict:
    """The four ``cac:LegalMonetaryTotal`` amounts, exactly as written."""
    total = _mapping(root.get(_key(CAC, "LegalMonetaryTotal")))
    return {
        "line_extension": _text(total.get(_key(CBC, "LineExtensionAmount"))),
        "tax_exclusive": _text(total.get(_key(CBC, "TaxExclusiveAmount"))),
        "tax_inclusive": _text(total.get(_key(CBC, "TaxInclusiveAmount"))),
        "payable": _text(total.get(_key(CBC, "PayableAmount"))),
    }


def _allowances(root: dict) -> list[dict]:
    """Document-level allowances and charges (``cac:AllowanceCharge``)."""
    entries = []
    for charge in _elements(root, _key(CAC, "AllowanceCharge")):
        entries.append(
            {
                "charge": _text(charge.get(_key(CBC, "ChargeIndicator"))) == "true",
                "amount": _text(charge.get(_key(CBC, "Amount"))),
                "reason": _text(charge.get(_key(CBC, "AllowanceChargeReason"))),
            }
        )
    return entries


def _tax_subtotals(root: dict) -> list[dict]:
    """Per-category VAT breakdown from every ``cac:TaxTotal``."""
    entries = []
    for total in _elements(root, _key(CAC, "TaxTotal")):
        for subtotal in _elements(total, _key(CAC, "TaxSubtotal")):
            category = _mapping(subtotal.get(_key(CAC, "TaxCategory")))
            entries.append(
                {
                    "category": _text(category.get(_key(CBC, "ID"))),
                    "percent": _text(category.get(_key(CBC, "Percent"))),
                    "taxable_amount": _text(subtotal.get(_key(CBC, "TaxableAmount"))),
                    "tax_amount": _text(subtotal.get(_key(CBC, "TaxAmount"))),
                }
            )
    return entries


def _lines(root: dict) -> list[dict]:
    """One entry per ``cac:InvoiceLine``; description falls back to item name."""
    lines = []
    for line in _elements(root, _key(CAC, "InvoiceLine")):
        item = _mapping(line.get(_key(CAC, "Item")))
        quantity = _first(line.get(_key(CBC, "InvoicedQuantity")))
        lines.append(
            {
                "id": _text(line.get(_key(CBC, "ID"))),
                "description": _text(item.get(_key(CBC, "Description")))
                or _text(item.get(_key(CBC, "Name"))),
                "quantity": _text(quantity),
                "unit_code": quantity.get("@unitCode") if isinstance(quantity, dict) else None,
                "line_extension": _text(line.get(_key(CBC, "LineExtensionAmount"))),
                "price": _text(
                    _mapping(line.get(_key(CAC, "Price"))).get(_key(CBC, "PriceAmount"))
                ),
            }
        )
    return lines


def invoice_facts(document: Document) -> dict:
    """Return the invoice's business facts as JSON-safe primitives.

    Monetary and quantity values are the document's **exact lexical
    strings** (``"336.9"``, not ``336.90`` and not a float); formatting
    happens at render time. Missing optional elements are ``None``, and
    ``address`` is a list of display lines.
    """
    root = document.to_dict(typed=False)
    return {
        "id": _text(root.get(_key(CBC, "ID"))),
        "issue_date": _text(root.get(_key(CBC, "IssueDate"))),
        "due_date": _text(root.get(_key(CBC, "DueDate"))),
        "type_code": _text(root.get(_key(CBC, "InvoiceTypeCode"))),
        "currency": _text(root.get(_key(CBC, "DocumentCurrencyCode"))),
        "buyer_reference": _text(root.get(_key(CBC, "BuyerReference"))),
        "notes": _strings(root.get(_key(CBC, "Note"))),
        "supplier": _party(_mapping(root.get(_key(CAC, "AccountingSupplierParty")))),
        "customer": _party(_mapping(root.get(_key(CAC, "AccountingCustomerParty")))),
        "totals": _totals(root),
        "allowances": _allowances(root),
        "tax_subtotals": _tax_subtotals(root),
        "lines": _lines(root),
    }


def money(value: str | None) -> str:
    """Format an exact decimal string with two decimals for display."""
    return _DASH if value is None else f"{Decimal(value):.2f}"


def percent(value: str | None) -> str:
    """Render a percentage without inventing precision (``19.0`` -> ``19``)."""
    return _DASH if value is None else f"{Decimal(value).normalize():f}"


def _amount(value: str | None, currency: str | None) -> str:
    """Formatted amount with the document currency, both escaped."""
    text = esc(money(value))
    return f"{text} {esc(currency)}" if currency else text


def _quantity(line: dict) -> str:
    """Quantity plus the UN/ECE unit code that makes it meaningful."""
    quantity = esc(line["quantity"] or _DASH)
    return f"{quantity} {esc(line['unit_code'])}" if line["unit_code"] else quantity


def _meta(facts: dict) -> str:
    rows = []
    for label, value in (
        ("Issue date", facts["issue_date"]),
        ("Due date", facts["due_date"]),
        ("Invoice type", facts["type_code"]),
        ("Buyer reference", facts["buyer_reference"]),
        ("Currency", facts["currency"]),
    ):
        if value:
            rows.append(f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>")
    return '<dl class="meta">' + "".join(rows) + "</dl>"


def _header(facts: dict) -> str:
    title = facts["id"] or "Invoice"
    return (
        '<header><div class="title-block">'
        '<p class="eyebrow">Electronic invoice</p>'
        f"<h1>{esc(title)}</h1>"
        "</div>"
        f"{_meta(facts)}</header>"
    )


def _party_block(title: str, party: dict) -> str:
    name = esc(party["name"] or _DASH)
    rows = ["<h2>" + esc(title) + "</h2>", f'<p class="name">{name}</p>']
    if party["address"]:
        address = "<br>".join(esc(line) for line in party["address"])
        rows.append(f'<p class="address">{address}</p>')
    if party["tax_id"]:
        rows.append(f'<p class="meta">Tax ID: {esc(party["tax_id"])}</p>')
    if party["endpoint"]:
        scheme = f" ({esc(party['endpoint_scheme'])})" if party["endpoint_scheme"] else ""
        rows.append(f'<p class="meta">E-invoicing endpoint: {esc(party["endpoint"])}{scheme}</p>')
    return '<section class="party">' + "".join(rows) + "</section>"


def _lines_table(facts: dict) -> str:
    rows = []
    for index, line in enumerate(facts["lines"], start=1):
        rows.append(
            "<tr>"
            f"<td>{esc(line['id'] or index)}</td>"
            f'<td class="item">{esc(line["description"] or _DASH)}</td>'
            f'<td class="num">{_quantity(line)}</td>'
            f'<td class="num">{_amount(line["price"], facts["currency"])}</td>'
            f'<td class="num">{_amount(line["line_extension"], facts["currency"])}</td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">No invoice lines in this document.</td></tr>')
    return (
        '<section class="lines"><h2>Invoice lines</h2><table><thead><tr>'
        "<th>Line</th><th>Description</th>"
        '<th class="num">Quantity</th><th class="num">Unit price</th>'
        '<th class="num">Line total</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def _allowance_table(facts: dict) -> str:
    if not facts["allowances"]:
        return ""
    rows = []
    for entry in facts["allowances"]:
        kind = "Charge" if entry["charge"] else "Allowance"
        rows.append(
            "<tr>"
            f"<th>{esc(kind)}</th>"
            f"<td>{esc(entry['reason'] or _DASH)}</td>"
            f'<td class="num">{_amount(entry["amount"], facts["currency"])}</td>'
            "</tr>"
        )
    return (
        '<section class="grid"><h2>Allowances and charges</h2><table><thead><tr>'
        '<th>Kind</th><th>Reason</th><th class="num">Amount</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def _tax_table(facts: dict) -> str:
    if not facts["tax_subtotals"]:
        return ""
    rows = []
    for subtotal in facts["tax_subtotals"]:
        rows.append(
            "<tr>"
            f"<th>{esc(subtotal['category'] or _DASH)}</th>"
            f'<td class="num">{esc(percent(subtotal["percent"]))} %</td>'
            f'<td class="num">{_amount(subtotal["taxable_amount"], facts["currency"])}</td>'
            f'<td class="num">{_amount(subtotal["tax_amount"], facts["currency"])}</td>'
            "</tr>"
        )
    return (
        '<section class="grid"><h2>Tax breakdown</h2><table><thead><tr>'
        '<th>Category</th><th class="num">Rate</th><th class="num">Taxable amount</th>'
        '<th class="num">Tax amount</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></section>"
    )


def _totals_table(facts: dict) -> str:
    totals = facts["totals"]
    rows = [
        ("Line extension", totals["line_extension"]),
        ("Tax exclusive", totals["tax_exclusive"]),
        ("Tax inclusive", totals["tax_inclusive"]),
    ]
    body = "".join(
        f"<tr><th>{esc(label)}</th><td>{_amount(value, facts['currency'])}</td></tr>"
        for label, value in rows
    )
    body += (
        '<tr class="grand"><th>Payable</th>'
        f"<td>{_amount(totals['payable'], facts['currency'])}</td></tr>"
    )
    return f'<section class="totals"><table><tbody>{body}</tbody></table></section>'


def _notes_block(facts: dict) -> str:
    if not facts["notes"]:
        return ""
    notes = "<br>".join(esc(note) for note in facts["notes"])
    return f'<section class="notes"><h2>Notes</h2><p>{notes}</p></section>'


_STYLE = """\
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 40px 20px;
  background: #f4f6f8;
  color: #1f2933;
  font: 14px/1.55 "Helvetica Neue", Helvetica, Arial, sans-serif;
}
main {
  max-width: 900px;
  margin: 0 auto;
  padding: 48px;
  background: #fff;
  border-top: 4px solid #1f4e79;
  box-shadow: 0 1px 4px rgb(15 23 42 / 10%);
}
header { display: flex; justify-content: space-between; gap: 40px; margin-bottom: 36px; }
.eyebrow {
  margin: 0;
  color: #52606d;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .14em;
  text-transform: uppercase;
}
h1 { margin: 4px 0 0; font-size: 30px; line-height: 1.15; }
h2 {
  margin: 0 0 8px;
  color: #1f4e79;
  font-size: 13px;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(140px, auto));
  gap: 8px 28px;
  margin: 0;
  text-align: right;
}
.meta dt { color: #697b8c; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; }
.meta dd { margin: 0; font-weight: 600; }
.parties { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; margin-bottom: 36px; }
.party { padding: 18px 20px; background: #f8fafc; border: 1px solid #e4e9ef; border-radius: 3px; }
.party .name { margin: 0 0 6px; font-size: 15px; font-weight: 700; }
.party p { margin: 0; }
.party .meta { display: block; margin-top: 8px; color: #52606d; font-size: 12px; text-align: left; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 9px 10px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.lines { margin-bottom: 32px; }
.lines thead th {
  border-bottom: 2px solid #1f4e79;
  color: #52606d;
  font-size: 11px;
  letter-spacing: .08em;
  text-align: left;
  text-transform: uppercase;
}
.lines thead th.num { text-align: right; }
.lines tbody tr { border-bottom: 1px solid #e4e9ef; }
.lines .item { font-weight: 600; }
.grid { margin-bottom: 28px; }
.grid thead th {
  background: #eef2f6;
  color: #52606d;
  font-size: 11px;
  letter-spacing: .06em;
  text-transform: uppercase;
}
.grid thead th.num { text-align: right; }
.grid tbody td, .grid tbody th { border-bottom: 1px solid #e4e9ef; font-weight: 400; }
.totals { display: flex; justify-content: flex-end; margin-bottom: 28px; }
.totals table { width: 340px; }
.totals th { color: #52606d; font-weight: 400; text-align: left; }
.totals td { text-align: right; font-variant-numeric: tabular-nums; }
.totals .grand th, .totals .grand td {
  padding-top: 12px;
  border-top: 2px solid #1f4e79;
  color: #1f4e79;
  font-size: 16px;
  font-weight: 700;
}
.notes {
  padding-top: 18px;
  border-top: 1px solid #e4e9ef;
  color: #52606d;
  font-size: 12px;
}
.notes p { margin: 0 0 4px; }
@media print {
  body { padding: 0; background: #fff; }
  main { max-width: none; padding: 0; box-shadow: none; }
}
"""


def render_html(document: Document) -> str:
    """Render *document* as one self-contained HTML page (no external assets)."""
    facts = invoice_facts(document)
    title = f"Invoice {facts['id']}" if facts["id"] else "Invoice"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(title)}</title>\n<style>\n{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        + _header(facts)
        + '<section class="parties">'
        + _party_block("Supplier", facts["supplier"])
        + _party_block("Customer", facts["customer"])
        + "</section>"
        + _lines_table(facts)
        + _allowance_table(facts)
        + _tax_table(facts)
        + _totals_table(facts)
        + _notes_block(facts)
        + "\n</main>\n"
        + "</body>\n</html>\n"
    )


def load_invoice(source: Path) -> Document:
    """Compile the pinned UBL schema (once per process) and parse *source*."""
    return compile_schema().parse(source)


def default_sample() -> Path:
    """Locate the canonical pinned XRechnung invoice inside the cache."""
    base = require_cache("ubl-data").parent
    matches = sorted(base.rglob(DEFAULT_SAMPLE))
    if not matches:
        raise FileNotFoundError(f"missing {DEFAULT_SAMPLE} in {base}; run: {SETUP_HINT}")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a UBL invoice as standalone HTML.")
    parser.add_argument(
        "invoice",
        nargs="?",
        type=Path,
        help=f"UBL XML file (default: the pinned {DEFAULT_SAMPLE})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/invoice.html"),
        help="output path (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    document = load_invoice(args.invoice or default_sample())
    if document.report.has_errors:
        print(
            f"warning: {document.report.error_count} schema error(s); rendering anyway",
            file=sys.stderr,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(document), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
