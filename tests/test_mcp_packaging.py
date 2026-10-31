"""The optional MCP dependency contract must survive distribution metadata."""

from importlib.metadata import metadata

from packaging.requirements import Requirement


def test_mcp_extra_resolves_without_installing_arrow():
    requirements = [Requirement(raw) for raw in metadata("pyxsd").get_all("Requires-Dist", [])]
    enabled = {
        req.name: req
        for req in requirements
        if req.marker is None or req.marker.evaluate({"extra": "mcp"})
    }
    assert {"mcp", "pydantic", "defusedxml", "elementpath", "anyio"} <= enabled.keys()
    assert "pyarrow" not in enabled
    assert "2.3.0" in enabled["mcp"].specifier
    assert "3.0.0" not in enabled["mcp"].specifier
    assert "2.12.5" in enabled["pydantic"].specifier
    assert "3.0.0" not in enabled["pydantic"].specifier
    assert "0.7.1" in enabled["defusedxml"].specifier
    assert "1.0.0" not in enabled["defusedxml"].specifier
    assert "4.10.0" in enabled["anyio"].specifier
    assert "4.9.0" not in enabled["anyio"].specifier
    assert "5.0.0" not in enabled["anyio"].specifier
    base = [
        req.name for req in requirements if req.marker is None or req.marker.evaluate({"extra": ""})
    ]
    assert base == ["defusedxml", "elementpath"]
    defusedxml = [req for req in requirements if req.name == "defusedxml"]
    assert len(defusedxml) == 1
    assert defusedxml[0].marker is None
