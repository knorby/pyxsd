"""Inline admission checks bytes and declarations without parsing XML."""

import pytest

from pyxsd.mcp import contracts, safe_io
from pyxsd.mcp.config import Limits
from pyxsd.mcp.errors import ToolError


@pytest.mark.parametrize("encoding", ["UTF-8", "utf8", "uTf-8"])
def test_inline_xml_preserves_utf8_bytes(encoding):
    text = f'\ufeff<?xml version="1.0" encoding="{encoding}"?><r>caf\u00e9</r>'
    admitted = safe_io.admit_inline(
        [contracts.InlineFile(path="nested/data.xml", text=text)], Limits()
    )
    assert [(item.path, item.content) for item in admitted] == [
        ("nested/data.xml", text.encode("utf-8"))
    ]


@pytest.mark.parametrize("encoding", ["UTF-16", "ISO-8859-1", "US-ASCII", "", "unknown"])
def test_inline_conflicting_encoding_is_rejected(encoding):
    text = f"<?xml version='1.0' encoding = '{encoding}'?><r/>"
    with pytest.raises(ToolError, match="invalid_encoding"):
        safe_io.admit_inline([contracts.InlineFile(path="data.xml", text=text)], Limits())


def test_inline_budgets_count_encoded_bytes_across_files():
    files = [
        contracts.InlineFile(path="a.txt", text="\u00e9"),
        contracts.InlineFile(path="b.txt", text="x"),
    ]
    assert len(safe_io.admit_inline(files, Limits(inline_bytes=3))) == 2
    with pytest.raises(ToolError, match="quota_exceeded"):
        safe_io.admit_inline(files, Limits(inline_bytes=2))
    with pytest.raises(ToolError, match="quota_exceeded"):
        safe_io.admit_inline(files, Limits(xml_file_bytes=1))


def test_inline_paths_and_unicode_are_checked_before_publication():
    with pytest.raises(ToolError, match="invalid_path"):
        safe_io.admit_inline([contracts.InlineFile(path="../outside", text="")], Limits())
    with pytest.raises(ToolError, match="invalid_encoding"):
        safe_io.admit_inline([contracts.InlineFile(path="data.xml", text="\ud800")], Limits())
    with pytest.raises(ToolError, match="path_collision"):
        safe_io.admit_inline(
            [contracts.InlineFile(path="A", text=""), contracts.InlineFile(path="a", text="")],
            Limits(),
        )


@pytest.mark.parametrize(
    "text",
    ["", "<r/>", "<?xml", "<?xml version='1.0'", "<?xml version='1.0'?><r/>", "print('hello')"],
)
def test_inline_admission_does_not_parse_or_repair_contents(text):
    result = safe_io.admit_inline([contracts.InlineFile(path="source.txt", text=text)], Limits())
    assert result[0].content == text.encode("utf-8")


def test_inline_empty_bundle_is_a_noop():
    assert safe_io.admit_inline([], Limits()) == ()
