"""Render a UBL invoice as a PDF report with reportlab's platypus engine.

Optional example extra: this module imports reportlab at module level, so
import it only when the extra is installed (``uv sync --group
examples-reportlab``). ``render_html.py`` stays dependency-free. Both
renderers share :func:`~examples.xrechnung.render_html.invoice_facts`
and the same render-time amount formatting.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from pyxsd import Document

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.xrechnung.common.render import esc
from examples.xrechnung.render_html import (
    _DASH,
    DEFAULT_SAMPLE,
    default_sample,
    invoice_facts,
    load_invoice,
    money,
    percent,
)

INK = colors.HexColor("#1f2933")
ACCENT = colors.HexColor("#1f4e79")
MUTED = colors.HexColor("#52606d")
RULE = colors.HexColor("#e4e9ef")
PANEL = colors.HexColor("#f8fafc")


def _styles() -> dict[str, ParagraphStyle]:
    sheet = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "InvoiceTitle",
            parent=sheet["Title"],
            fontSize=22,
            leading=26,
            alignment=TA_LEFT,
            textColor=ACCENT,
        ),
        "label": ParagraphStyle(
            "Label", parent=sheet["Normal"], fontSize=7.5, leading=10, textColor=MUTED
        ),
        "value": ParagraphStyle(
            "Value", parent=sheet["Normal"], fontSize=10, leading=13, textColor=INK
        ),
        "heading": ParagraphStyle(
            "Section",
            parent=sheet["Heading2"],
            fontSize=10,
            leading=12,
            spaceBefore=10,
            spaceAfter=4,
            textColor=ACCENT,
        ),
        "body": ParagraphStyle(
            "Body", parent=sheet["Normal"], fontSize=9, leading=12, textColor=INK
        ),
        "small": ParagraphStyle(
            "Small", parent=sheet["Normal"], fontSize=7.5, leading=10, textColor=MUTED
        ),
    }


def _amount(value: str | None, currency: str | None) -> str:
    text = money(value)
    return f"{text} {currency}" if currency else text


def _party_cell(title: str, party: dict, styles: dict[str, ParagraphStyle]) -> list:
    """One party block as a list of flowables for a table cell."""
    block = [
        Paragraph(esc(title), styles["label"]),
        Paragraph(esc(party["name"] or _DASH), styles["value"]),
    ]
    block.extend(Paragraph(esc(line), styles["body"]) for line in party["address"])
    if party["tax_id"]:
        block.append(Paragraph("Tax ID: " + esc(party["tax_id"]), styles["small"]))
    if party["endpoint"]:
        scheme = f" ({esc(party['endpoint_scheme'])})" if party["endpoint_scheme"] else ""
        block.append(
            Paragraph("E-invoicing endpoint: " + esc(party["endpoint"]) + scheme, styles["small"])
        )
    return block


def _meta_line(facts: dict, styles: dict[str, ParagraphStyle]) -> Paragraph:
    parts = []
    for label, value in (
        ("Issue date", facts["issue_date"]),
        ("Due date", facts["due_date"]),
        ("Invoice type", facts["type_code"]),
        ("Buyer reference", facts["buyer_reference"]),
        ("Currency", facts["currency"]),
    ):
        if value:
            parts.append(f"<b>{esc(label)}:</b> {esc(value)}")
    return Paragraph(" &nbsp;&nbsp;|&nbsp;&nbsp; ".join(parts), styles["small"])


def _parties_table(facts: dict, styles: dict[str, ParagraphStyle]) -> Table:
    table = Table(
        [
            [
                _party_cell("Supplier", facts["supplier"], styles),
                _party_cell("Customer", facts["customer"], styles),
            ]
        ],
        colWidths=[87 * mm, 87 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), PANEL),
                ("BOX", (0, 0), (-1, -1), 0.5, RULE),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _lines_table(facts: dict, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [["Line", "Description", "Quantity", "Unit price", "Line total"]]
    for index, line in enumerate(facts["lines"], start=1):
        quantity = line["quantity"] or _DASH
        if line["unit_code"]:
            quantity = f"{quantity} {line['unit_code']}"
        rows.append(
            [
                Paragraph(esc(line["id"] or str(index)), styles["body"]),
                Paragraph(esc(line["description"] or _DASH), styles["body"]),
                quantity,
                _amount(line["price"], facts["currency"]),
                _amount(line["line_extension"], facts["currency"]),
            ]
        )
    if len(rows) == 1:
        rows.append(["-", "No invoice lines in this document.", "", "", ""])
    table = Table(
        rows,
        colWidths=[30 * mm, 74 * mm, 24 * mm, 23 * mm, 23 * mm],
        repeatRows=1,
    )
    table.setStyle(_grid_style(len(rows), numeric=(2, 3, 4)))
    return table


def _allowance_table(facts: dict, styles: dict[str, ParagraphStyle]) -> Table:
    rows = [["Kind", "Reason", "Amount"]]
    for entry in facts["allowances"]:
        rows.append(
            [
                "Charge" if entry["charge"] else "Allowance",
                Paragraph(esc(entry["reason"] or _DASH), styles["body"]),
                _amount(entry["amount"], facts["currency"]),
            ]
        )
    table = Table(rows, colWidths=[35 * mm, 105 * mm, 34 * mm], repeatRows=1)
    table.setStyle(_grid_style(len(rows), numeric=(2,)))
    return table


def _tax_table(facts: dict) -> Table:
    rows = [["Category", "Rate", "Taxable amount", "Tax amount"]]
    for subtotal in facts["tax_subtotals"]:
        rows.append(
            [
                subtotal["category"] or _DASH,
                f"{percent(subtotal['percent'])} %",
                _amount(subtotal["taxable_amount"], facts["currency"]),
                _amount(subtotal["tax_amount"], facts["currency"]),
            ]
        )
    table = Table(rows, colWidths=[30 * mm, 24 * mm, 60 * mm, 60 * mm], repeatRows=1)
    table.setStyle(_grid_style(len(rows), numeric=(1, 2, 3)))
    return table


def _totals_table(facts: dict) -> Table:
    totals = facts["totals"]
    rows = [
        ["Line extension", _amount(totals["line_extension"], facts["currency"])],
        ["Tax exclusive", _amount(totals["tax_exclusive"], facts["currency"])],
        ["Tax inclusive", _amount(totals["tax_inclusive"], facts["currency"])],
        ["Payable", _amount(totals["payable"], facts["currency"])],
    ]
    table = Table(rows, colWidths=[110 * mm, 64 * mm], hAlign="RIGHT")
    table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, -1), (-1, -1), ACCENT),
                ("LINEABOVE", (0, -1), (-1, -1), 1, ACCENT),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _grid_style(row_count: int, *, numeric: tuple[int, ...]) -> TableStyle:
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f6")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    style.extend(("LINEBELOW", (0, row), (-1, row), 0.4, RULE) for row in range(1, row_count))
    style.extend(("ALIGN", (column, 0), (column, -1), "RIGHT") for column in numeric)
    return TableStyle(style)


def _story(facts: dict, styles: dict[str, ParagraphStyle]) -> list:
    document_title = f"Invoice {facts['id']}" if facts["id"] else "Invoice"
    story = [
        Paragraph(esc(document_title), styles["title"]),
        _meta_line(facts, styles),
        Spacer(1, 5 * mm),
        _parties_table(facts, styles),
        Paragraph("Invoice lines", styles["heading"]),
        _lines_table(facts, styles),
    ]
    if facts["allowances"]:
        story.extend(
            [
                Paragraph("Allowances and charges", styles["heading"]),
                _allowance_table(facts, styles),
            ]
        )
    if facts["tax_subtotals"]:
        story.extend([Paragraph("Tax breakdown", styles["heading"]), _tax_table(facts)])
    story.extend(
        [
            Paragraph("Totals", styles["heading"]),
            _totals_table(facts),
        ]
    )
    if facts["notes"]:
        story.extend(
            [
                Paragraph("Notes", styles["heading"]),
                Paragraph("<br/>".join(esc(note) for note in facts["notes"]), styles["small"]),
            ]
        )
    return story


def render_pdf(document: Document, dest: Path) -> Path:
    """Render *document* as a PDF report and return the written path."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    facts = invoice_facts(document)
    styles = _styles()
    document_title = f"Invoice {facts['id']}" if facts["id"] else "Invoice"
    pdf = SimpleDocTemplate(
        str(dest),
        pagesize=A4,
        title=document_title,
        subject="Server-side invoice rendering",
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    pdf.build(_story(facts, styles))
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a UBL invoice as a PDF report.")
    parser.add_argument(
        "invoice",
        nargs="?",
        type=Path,
        help=f"UBL XML file (default: the pinned {DEFAULT_SAMPLE})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("out/invoice.pdf"),
        help="output path (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    document = load_invoice(args.invoice or default_sample())
    if document.report.has_errors:
        print(
            f"warning: {document.report.error_count} schema error(s); rendering anyway",
            file=sys.stderr,
        )
    print(f"wrote {render_pdf(document, args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
