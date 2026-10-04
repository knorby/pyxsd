"""The Arrow-only order-line example must associate real fixture rows correctly."""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest


def test_order_line_example(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    script = Path(__file__).resolve().parents[1] / "examples/arrow/project_order_lines.py"
    target = tmp_path / "lines.parquet"
    # Block Pydantic even when the test runner's environment has both extras.
    command = """import runpy, sys
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'pydantic' or fullname.startswith('pydantic.'):
            raise ModuleNotFoundError('Pydantic must not be used', name=fullname)
sys.meta_path.insert(0, Block())
sys.argv = [sys.argv[1], sys.argv[2]]
runpy.run_path(sys.argv[0], run_name='__main__')
assert 'pydantic' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", command, str(script), str(target)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 order-line rows" in result.stdout
    table = pq.read_table(target)
    assert table.schema.names == [
        "order_number",
        "customer_account",
        "sku",
        "quantity",
        "unit_price",
        "currency",
    ]
    assert table.schema.field("unit_price").type == pa.decimal128(10, 2)
    assert table.schema.field("quantity").type == pa.string()
    assert table.to_pylist() == [
        {
            "order_number": "PO-2026-1041",
            "customer_account": "RRL-42",
            "sku": "LOGGER-USB",
            "quantity": "4",
            "unit_price": Decimal("125.50"),
            "currency": "USD",
        },
        {
            "order_number": "PO-2026-1041",
            "customer_account": "RRL-42",
            "sku": "PROBE-EC",
            "quantity": "2",
            "unit_price": Decimal("89.95"),
            "currency": "USD",
        },
        {
            "order_number": "PO-2026-1042",
            "customer_account": "CWO-17",
            "sku": "BOTTLE-1L",
            "quantity": "120",
            "unit_price": Decimal("3.75"),
            "currency": "EUR",
        },
        {
            "order_number": "PO-2026-1043",
            "customer_account": "RRL-42",
            "sku": "LABEL-WP",
            "quantity": "1000",
            "unit_price": Decimal("0.10"),
            "currency": "USD",
        },
    ]
