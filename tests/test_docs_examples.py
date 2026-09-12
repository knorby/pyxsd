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
