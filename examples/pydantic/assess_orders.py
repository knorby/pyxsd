"""Assess purchase orders against synthetic policy; requires pyxsd[pydantic]."""

import argparse
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pyxsd
from pyxsd.integrations.pydantic import ModelSet, models

DATA = Path(__file__).resolve().parent

# Synthetic policy fixtures for this example only: not XSD constraints and not
# financial advice.
ACCOUNTS = frozenset({"RRL-42", "CWO-17"})
SKUS = frozenset({"LOGGER-USB", "PROBE-EC", "BOTTLE-1L", "LABEL-WP"})
APPROVAL_THRESHOLD = Decimal("600.00")


@dataclass(frozen=True)
class OrderAssessment:
    order_number: str
    disposition: Literal["accepted", "approval_required", "rejected"]
    currency: str | None
    subtotal: Decimal | None
    reasons: tuple[str, ...]


def prepare() -> tuple[pyxsd.Schema, ModelSet]:
    schema = pyxsd.compile(DATA / "orders.xsd", mode=pyxsd.ParseModes.NAMESPACED)
    return schema, models(schema)


def assess_order(
    order: Any,
    *,
    accounts: frozenset[str],
    skus: frozenset[str],
    approval_threshold: Decimal,
) -> OrderAssessment:
    """Apply synthetic policy to one schema-validated order snapshot."""
    reasons: list[str] = []
    if order.customer.attr_account not in accounts:
        reasons.append("unknown_account")
    if any(line.attr_sku not in skus for line in order.line):
        reasons.append("unknown_sku")
    currencies = {line.price.attr_currency for line in order.line}
    if len(currencies) > 1:
        reasons.append("mixed_currencies")
        currency: str | None = None
        subtotal: Decimal | None = None
    else:
        currency = next(iter(currencies))
        subtotal = sum(
            (line.quantity * line.price.xml_value for line in order.line), start=Decimal("0")
        )
    if reasons:
        return OrderAssessment(order.attr_number, "rejected", currency, subtotal, tuple(reasons))
    if subtotal > approval_threshold:
        return OrderAssessment(
            order.attr_number,
            "approval_required",
            currency,
            subtotal,
            ("over_approval_threshold",),
        )
    return OrderAssessment(order.attr_number, "accepted", currency, subtotal, ())


def assess_source(path: Path) -> list[OrderAssessment]:
    """Parse, validate, snapshot, and assess every order in an XML file."""
    schema, registry = prepare()
    snapshot = registry.from_document(schema.parse(path), revalidate=True)
    return [
        assess_order(
            order,
            accounts=ACCOUNTS,
            skus=SKUS,
            approval_threshold=APPROVAL_THRESHOLD,
        )
        for order in snapshot.order
    ]


def report_payload(assessments: list[OrderAssessment]) -> dict[str, object]:
    """Serialize totals as exact decimal strings, never floats."""
    return {
        "orders": [
            {
                "number": assessment.order_number,
                "disposition": assessment.disposition,
                "currency": assessment.currency,
                "subtotal": None if assessment.subtotal is None else str(assessment.subtotal),
                "reasons": list(assessment.reasons),
            }
            for assessment in assessments
        ]
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DATA / "orders.xml")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    args = parser.parse_args(argv)
    report = json.dumps(report_payload(assess_source(args.source)), indent=2)
    print(report)
    if args.output is not None:
        args.output.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
