"""The transform authoring guide's examples are executable.

The guide is documentation first, but its code blocks are the same source
users copy. Extract them and run them here so they cannot rot.
"""

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pyxsd.transforms import Transform

GUIDE = Path(__file__).parent.parent / "docs" / "transforms" / "writing.md"


def _python_blocks(markdown: str) -> list[str]:
    """Returns every fenced ``python`` code block in document order."""
    blocks = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith("```python"):
            body = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def _load(index: int, name: str) -> dict:
    namespace: dict = {"__name__": name}
    source = _python_blocks(GUIDE.read_text())[index]
    exec(compile(source, f"writing.md[{index}]", "exec"), namespace)
    return namespace


def test_minimal_example_defines_a_transform():
    namespace = _load(0, "docs_minimal_example")
    assert issubclass(namespace["UpperValues"], Transform)


def test_in_memory_example_runs(capsys):
    namespace = _load(1, "docs_in_memory_example")
    namespace["main"]()
    out = capsys.readouterr().out
    assert "body before: hello & goodbye" in out
    assert "<body>HELLO &amp; GOODBYE</body>" in out
    assert "revalidation errors: []" in out


def test_readme_first_example_is_self_contained(capsys):
    readme = GUIDE.parents[2] / "README.md"
    source = _python_blocks(readme.read_text(encoding="utf-8"))[0]
    namespace = {}
    exec(compile(source, "README.md", "exec"), namespace)
    assert capsys.readouterr().out == "42\n"
    assert namespace["document"].is_valid


def test_quickstart_from_supplied_files(tmp_path, monkeypatch, capsys):
    guide = GUIDE.parent.parent / "quickstart.md"
    markdown = guide.read_text(encoding="utf-8")
    xml_blocks = markdown.split("```xml\n")[1:]
    assert len(xml_blocks) == 2, "Supply the complete schema and XML input"
    for filename, block in zip(("inventory.xsd", "inventory.xml"), xml_blocks, strict=True):
        (tmp_path / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    namespace = {}
    for index, source in enumerate(_python_blocks(markdown)):
        exec(compile(source, f"quickstart.md[{index}]", "exec"), namespace)
    assert "Bolts 4" in capsys.readouterr().out
    checked = namespace["checked"]
    assert checked.is_valid
    saved = namespace["schema"].parse(tmp_path / "updated.xml")
    assert saved.is_valid
    assert saved.xpath("string(/i:inventory/i:item/i:name)", namespaces=namespace["ns"]) == "BOLTS"
    stubs = GUIDE.parent.parent / "stubs.md"
    for source in _python_blocks(stubs.read_text(encoding="utf-8")):
        exec(compile(source, "stubs.md", "exec"), namespace)
    assert namespace["declaration"].id.startswith("element:")
    assert "inventory" in capsys.readouterr().out


def test_lxml_comparison_pyxsd_example_uses_schema_types(tmp_path, monkeypatch):
    from pyxsd.xsd_data_types import XsdDataType

    quickstart = (GUIDE.parent.parent / "quickstart.md").read_text(encoding="utf-8")
    for filename, block in zip(
        ("inventory.xsd", "inventory.xml"), quickstart.split("```xml\n")[1:], strict=True
    ):
        (tmp_path / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    guide = (GUIDE.parent.parent / "comparison-lxml.md").read_text(encoding="utf-8")
    namespace = {}
    exec(compile(_python_blocks(guide)[1], "comparison-lxml.md[pyxsd]", "exec"), namespace)
    assert namespace["document"].is_valid
    assert namespace["quantity"] == 4
    assert isinstance(namespace["quantity"], XsdDataType)


def test_transform_guide_library_example(tmp_path, monkeypatch):
    guide = GUIDE.parent / "using.md"
    monkeypatch.chdir(tmp_path)
    namespace = {}
    for source in _python_blocks(guide.read_text(encoding="utf-8")):
        exec(compile(source, "using.md", "exec"), namespace)
    assert namespace["checked"].is_valid
    assert "HELLO" in (tmp_path / "out.xml").read_text(encoding="utf-8")


def test_schema_resolution_from_supplied_files(tmp_path, monkeypatch, capsys):
    guide = GUIDE.parent.parent / "schema-resolution.md"
    assert guide.is_file(), "Supply the multi-file schema recipe"
    markdown = guide.read_text(encoding="utf-8")
    xml_blocks = markdown.split("```xml\n")[1:]
    filenames = ("orders.xsd", "fields.xsd", "codes.xsd", "order.xml")
    assert len(xml_blocks) == len(filenames)
    inputs = tmp_path / "schemas"
    inputs.mkdir()
    for filename, block in zip(filenames, xml_blocks, strict=True):
        (inputs / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    namespace = {}
    for source in _python_blocks(markdown):
        exec(compile(source, "schema-resolution.md", "exec"), namespace)
    assert capsys.readouterr().out.splitlines() == ["AB 2", "True"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    document = namespace["schema"].parse(inputs / "order.xml")
    assert document.is_valid
    assert document.xpath("string(/order/code)") == "AB"


def test_integration_examples_use_identified_inputs(tmp_path, monkeypatch):
    pytest.importorskip("pydantic")
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    quickstart = (GUIDE.parent.parent / "quickstart.md").read_text(encoding="utf-8")
    for filename, block in zip(
        ("inventory.xsd", "inventory.xml"), quickstart.split("```xml\n")[1:], strict=True
    ):
        (tmp_path / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    examples = tmp_path / "examples" / "pydantic"
    examples.mkdir(parents=True)
    for filename in ("orders.xsd", "orders.xml"):
        source = GUIDE.parents[2] / "examples" / "pydantic" / filename
        (examples / filename).write_bytes(source.read_bytes())
    monkeypatch.chdir(tmp_path)
    namespace = {}
    guide = GUIDE.parent.parent / "integrations.md"
    walkthrough, contextual = guide.read_text(encoding="utf-8").split(
        "### Named scalar columns with row context", 1
    )
    for source in _python_blocks(walkthrough):
        exec(compile(source, "integrations.md", "exec"), namespace)
    assert namespace["snapshot"].model_dump(by_alias=True) == namespace["inventory"].model_dump(
        by_alias=True
    )
    assert namespace["item"].model_dump(by_alias=True)["{urn:inventory}quantity"] == 4
    assert pq.read_table("inventory.parquet").num_rows == 1
    namespace = {}
    for source in _python_blocks(contextual):
        exec(compile(source, "integrations.md[contextual]", "exec"), namespace)
    assert namespace["table"].column_names == [
        "order_number",
        "customer_account",
        "sku",
        "quantity",
        "unit_price",
        "currency",
    ]
    assert namespace["table"]["sku"].to_pylist() == [
        "LOGGER-USB",
        "PROBE-EC",
        "BOTTLE-1L",
        "LABEL-WP",
    ]


def test_batch_guide_reports_valid_invalid_and_missing_inputs(tmp_path, monkeypatch, capsys):
    quickstart = (GUIDE.parent.parent / "quickstart.md").read_text(encoding="utf-8")
    for filename, block in zip(
        ("inventory.xsd", "inventory.xml"), quickstart.split("```xml\n")[1:], strict=True
    ):
        (tmp_path / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    guide = (GUIDE.parent.parent / "batch.md").read_text(encoding="utf-8")
    namespace = {}
    for source in _python_blocks(guide.split("## Sources and outcomes", 1)[0]):
        exec(compile(source, "batch.md", "exec"), namespace)
    output = capsys.readouterr().out
    assert output.splitlines() == ["original valid", "zero invalid", "missing input_error"]
    assert namespace["outcomes"][1].document.report.has_errors
    assert namespace["outcomes"][2].error.kind == "io"


def test_batch_guide_checkout_command(capsys):
    from examples.batch.validate_documents import main

    guide = (GUIDE.parent.parent / "batch.md").read_text(encoding="utf-8")
    section = guide.split("## Runnable validation example", 1)[1]
    command = section.split("```bash\n", 1)[1].split("```", 1)[0].strip()
    arguments = shlex.split(command)[4:]
    root = GUIDE.parents[2]
    assert main([str(root / arguments[0]), str(root / arguments[1]), *arguments[2:]]) == 0
    assert capsys.readouterr().out == "instance.xml: valid\n"


def test_demo_guide_library_example(tmp_path, monkeypatch, capsys):
    demo = GUIDE.parents[2] / "examples" / "demo"
    for filename in ("schema.xsd", "instance.xml", "CountElements.py"):
        (tmp_path / filename).write_bytes((demo / filename).read_bytes())
    monkeypatch.chdir(tmp_path)
    guide = (GUIDE.parent.parent / "demo.md").read_text(encoding="utf-8")
    namespace = {}
    exec(compile(_python_blocks(guide)[0], "demo.md", "exec"), namespace)
    assert namespace["document"].is_valid
    saved = namespace["document"].schema.parse(tmp_path / "transformed.xml")
    assert saved.is_valid
    assert len(saved.findall("item")) == 2
    assert "CountElements: element frequency" in capsys.readouterr().out


def test_cli_reference_examples_use_quickstart_inputs(tmp_path):
    quickstart = (GUIDE.parent.parent / "quickstart.md").read_text(encoding="utf-8")
    for filename, block in zip(
        ("inventory.xsd", "inventory.xml"), quickstart.split("```xml\n")[1:], strict=True
    ):
        (tmp_path / filename).write_text(block.split("```", 1)[0], encoding="utf-8")
    guide = (GUIDE.parent.parent / "cli.md").read_text(encoding="utf-8")
    examples = guide.split("## Examples", 1)[1]
    if "```text\n" in examples:
        calls = examples.split("```text\n", 1)[1].split("```", 1)[0]
        (tmp_path / "transforms.txt").write_text(calls, encoding="utf-8")
    commands = [line for line in examples.splitlines() if line.startswith("pyxsd ")]
    assert commands
    for line in commands:
        completed = subprocess.run(
            [sys.executable, "-m", "pyxsd", *shlex.split(line)[1:]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert completed.returncode == 0, (line, completed.stderr)
    assert "Bolts" in completed.stdout
    from pyxsd import ParseModes, Schema

    saved = Schema.compile(tmp_path / "inventory.xsd", mode=ParseModes.NAMESPACED).parse(
        tmp_path / "parsed.xml"
    )
    assert saved.is_valid
    assert (
        saved.xpath("string(/i:inventory/i:item/i:name)", namespaces={"i": "urn:inventory"})
        == "Bolts"
    )
