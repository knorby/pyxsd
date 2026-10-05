"""Purchase-order assessment example: exact policy outcomes and validation paths."""

import hashlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from pyxsd.exceptions import PyXSDError, ValidationError

pytest.importorskip("pydantic")

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "pydantic"
ORDERS = EXAMPLES / "orders.xml"
REVIEW = EXAMPLES / "orders_review.xml"


def load_assess():
    spec = importlib.util.spec_from_file_location("assess_orders", EXAMPLES / "assess_orders.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["assess_orders"] = module
    spec.loader.exec_module(module)
    return module


def _by_number(module, path):
    return {assessment.order_number: assessment for assessment in module.assess_source(path)}


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_orders_xml_dispositions():
    module = load_assess()
    by_number = _by_number(module, ORDERS)
    assert by_number["PO-2026-1041"] == module.OrderAssessment(
        "PO-2026-1041", "approval_required", "USD", Decimal("681.90"), ("over_approval_threshold",)
    )
    assert by_number["PO-2026-1042"] == module.OrderAssessment(
        "PO-2026-1042", "accepted", "EUR", Decimal("450.00"), ()
    )
    assert by_number["PO-2026-1043"] == module.OrderAssessment(
        "PO-2026-1043", "accepted", "USD", Decimal("100.00"), ()
    )


def test_threshold_boundary_accepted():
    module = load_assess()
    by_number = _by_number(module, REVIEW)
    assert by_number["PO-2026-2004"] == module.OrderAssessment(
        "PO-2026-2004", "accepted", "USD", Decimal("600.00"), ()
    )


def test_unknown_account_rejected():
    module = load_assess()
    by_number = _by_number(module, REVIEW)
    assert by_number["PO-2026-2001"] == module.OrderAssessment(
        "PO-2026-2001", "rejected", "USD", Decimal("1.00"), ("unknown_account",)
    )


def test_unknown_sku_rejected():
    module = load_assess()
    by_number = _by_number(module, REVIEW)
    assert by_number["PO-2026-2002"] == module.OrderAssessment(
        "PO-2026-2002", "rejected", "USD", Decimal("10.00"), ("unknown_sku",)
    )


def test_mixed_currency_rejected_without_subtotal():
    module = load_assess()
    by_number = _by_number(module, REVIEW)
    assert by_number["PO-2026-2003"] == module.OrderAssessment(
        "PO-2026-2003", "rejected", None, None, ("mixed_currencies",)
    )


def test_malformed_xml_never_reaches_policy(tmp_path, monkeypatch):
    module = load_assess()

    def fail(*args, **kwargs):
        raise AssertionError("policy evaluated on malformed input")

    monkeypatch.setattr(module, "assess_order", fail)
    truncated = tmp_path / "orders.xml"
    truncated.write_text('<orders source="x"><order number="PO-X">', encoding="utf-8")
    with pytest.raises(PyXSDError):
        module.main([str(truncated)])


def test_model_validation_errors_are_not_business_rejections(tmp_path, monkeypatch):
    module = load_assess()

    def fail(*args, **kwargs):
        raise AssertionError("policy evaluated on schema-invalid input")

    monkeypatch.setattr(module, "assess_order", fail)
    invalid = tmp_path / "orders.xml"
    invalid.write_text(
        """<orders source="x"><order number="PO-X"><placed>2026-01-01T00:00:00Z</placed>
<customer account="RRL-42"><name>N</name><contact>c</contact></customer>
<line sku="LABEL-WP"><description>d</description><quantity>0</quantity><price>0.10</price></line>
</order></orders>""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        module.main([str(invalid)])


def test_json_report_decimal_strings(tmp_path):
    module = load_assess()
    output = tmp_path / "report.json"
    module.main([str(ORDERS), "--output", str(output)])
    payload = json.loads(output.read_text(encoding="utf-8"))
    orders = {entry["number"]: entry for entry in payload["orders"]}
    assert orders["PO-2026-1041"]["subtotal"] == "681.90"
    assert orders["PO-2026-1042"]["subtotal"] == "450.00"
    assert orders["PO-2026-1041"]["reasons"] == ["over_approval_threshold"]
    assert all(
        isinstance(entry["subtotal"], str) or entry["subtotal"] is None
        for entry in payload["orders"]
    )


def test_source_xml_unchanged(tmp_path):
    module = load_assess()
    before = {path: _sha256(path) for path in (ORDERS, REVIEW)}
    module.main([str(ORDERS), "--output", str(tmp_path / "orders.json")])
    module.main([str(REVIEW), "--output", str(tmp_path / "review.json")])
    after = {path: _sha256(path) for path in (ORDERS, REVIEW)}
    assert before == after


def test_nil_vs_absent_note():
    module = load_assess()
    schema, registry = module.prepare()
    snapshot = registry.from_document(schema.parse(ORDERS), revalidate=True)
    by_number = {order.attr_number: order for order in snapshot.order}
    nil = by_number["PO-2026-1042"]
    assert nil.note is None
    assert "note" in nil.model_fields_set
    absent = by_number["PO-2026-1043"]
    assert absent.note is None
    assert "note" not in absent.model_fields_set
