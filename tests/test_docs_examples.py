"""The transform authoring guide's examples are executable.

The guide is documentation first, but its code blocks are the same source
users copy. Extract them and run them here so they cannot rot.
"""

from pathlib import Path

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


def test_transform_guide_library_example(tmp_path, monkeypatch):
    guide = GUIDE.parent / "using.md"
    monkeypatch.chdir(tmp_path)
    namespace = {}
    for source in _python_blocks(guide.read_text(encoding="utf-8")):
        exec(compile(source, "using.md", "exec"), namespace)
    assert namespace["checked"].is_valid
    assert "HELLO" in (tmp_path / "out.xml").read_text(encoding="utf-8")
