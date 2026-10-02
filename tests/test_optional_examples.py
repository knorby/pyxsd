"""Runnable tutorials must demonstrate schema-derived types using real data."""

import importlib.util
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_example(name):
    path = EXAMPLES / name / "demo.py"
    assert path.is_file(), f"missing runnable example: {path}"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pydantic_orders_are_dynamic_typed_models_with_presence_and_validation():
    pd = pytest.importorskip("pydantic")
    example = load_example("pydantic")
    schema, registry, Order = example.prepare()
    assert issubclass(Order, pd.BaseModel)
    candidate = Order.model_validate(example.python_order())
    assert candidate.customer.name == "River Research Lab"
    assert candidate.line[0].quantity == 4
    assert type(candidate.line[0].quantity) is int
    assert candidate.line[0].price.xml_value == Decimal("125.50")
    assert candidate.line[0].price.attr_currency == "USD"
    assert candidate.expedited is False
    assert "expedited" in candidate.model_fields_set
    assert "attr_status" not in candidate.model_fields_set
    supplied = example.python_order()
    supplied.pop("expedited")
    omitted = Order.model_validate(supplied)
    assert omitted.expedited is None
    assert "expedited" not in omitted.model_fields_set
    snapshot = registry.from_document(schema.parse(example.DATA / "orders.xml"), revalidate=True)
    assert len(snapshot.order) == 3
    assert len(snapshot.order[0].line) == 2
    assert snapshot.order[1].expedited is True
    assert snapshot.order[2].line[0].price.xml_value == Decimal("0.10")
    with pytest.raises(pd.ValidationError):
        Order.model_validate({**example.python_order(), "expedited": "true"})
    invalid = example.python_order()
    invalid["line"][0]["quantity"] = 0
    with pytest.raises(pd.ValidationError):
        Order.model_validate(invalid)


def test_arrow_observations_preserve_nested_types_and_real_parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    example = load_example("arrow")
    schema, projection = example.prepare()
    document = schema.parse(example.DATA / "observations.xml")
    table = projection.table(document, selector="observation", revalidate=True)
    assert table.num_rows == 4
    assert table.schema.field("sequence").type == pa.uint32()
    assert table.schema.field("reading").type.field("$").type == pa.decimal128(8, 3)
    assert table.schema.field("location").type.field("latitude").type == pa.decimal128(11, 5)
    assert table.schema.field("replicates").type.value_type == pa.decimal128(8, 3)
    rows = table.to_pylist()
    assert rows[0]["reading"] == {"@unit": "mg/L", "$": Decimal("7.125"), "$nil": False}
    assert rows[1]["quality"] is False
    assert rows[2]["reading"] == {"$": None, "@unit": "mg/L", "$nil": True}
    assert rows[3]["accession"] == "123456789012345678901234567890"
    assert rows[0]["replicates"] == [Decimal("7.100"), Decimal("7.125"), Decimal("7.150")]
    batches = list(projection.batches(document, selector="observation", batch_size=2))
    assert [batch.num_rows for batch in batches] == [2, 2]
    output = tmp_path / "observations.parquet"
    projection.write_parquet(document, output, selector="observation", batch_size=2)
    assert pq.read_table(output).equals(table)
    empty = projection.table(document, selector="observation[@station='absent']")
    assert empty.num_rows == 0
    assert empty.schema.equals(table.schema)


@pytest.mark.parametrize("name,dependency", [("pydantic", "pydantic"), ("arrow", "pyarrow")])
def test_tutorial_runs_outside_repo_directory(name, dependency, tmp_path):
    pytest.importorskip(dependency)
    command = [sys.executable, str(EXAMPLES / name / "demo.py")]
    if name == "arrow":
        command += [str(tmp_path / "observations.parquet")]
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Schema-derived" in result.stdout
    if name == "pydantic":
        assert "Rejected invalid quantity" in result.stdout
        assert "3 orders" in result.stdout
    else:
        assert "4 rows" in result.stdout
        assert "Empty selection: 0 rows" in result.stdout
