"""Opt-in checks of built archives, outside the source checkout.

Run with PYXSD_DIST_DIR pointing at a fresh ``uv build`` output directory.
"""

import email
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from scripts.check_mcp_registry import check_local, check_pypi

from pyxsd import __version__


def test_built_distributions_have_complete_sources_and_bundled_skills(tmp_path):
    directory = os.environ.get("PYXSD_DIST_DIR")
    if directory is None:
        pytest.skip("set PYXSD_DIST_DIR to freshly built distributions")
    dist = Path(directory).resolve()
    with tarfile.open(dist / f"pyxsd-{__version__}.tar.gz") as archive:
        names = archive.getnames()
        root = f"pyxsd-{__version__}/"
        assert root + "benchmarks/mcp_workflows.py" in names
        assert root + "server.json" in names
        assert root + "scripts/check_mcp_registry.py" in names
        assert root + "benchmarks/fixtures/mcp/catalog.xsd" in names
        assert not any(name.startswith(root + "tests/xsts/corpus/") for name in names)
        assert not any(name.startswith(root + "tests/xpath/corpus/") for name in names)
        # All members come from our freshly built source archive. Manual
        # extraction keeps this check compatible with Python 3.11's tar API.
        for member in archive.getmembers():
            assert member.isfile() or member.isdir()
            target = tmp_path / member.name
            assert target.resolve().is_relative_to(tmp_path.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                target.write_bytes(source.read())
    environment = dict(os.environ, PYTHONPATH="")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/mcp/test_benchmarks.py"],
        cwd=tmp_path / f"pyxsd-{__version__}",
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout
    with zipfile.ZipFile(dist / f"pyxsd-{__version__}-py3-none-any.whl") as wheel:
        names = wheel.namelist()
        for skill in ("pyxsd-cli", "pyxsd-development", "pyxsd-mcp", "pyxsd-xml-doctor"):
            assert f"pyxsd/skills/{skill}/SKILL.md" in names
        entries = wheel.read(f"pyxsd-{__version__}.dist-info/entry_points.txt").decode()
        assert "pyxsd = pyxsd.__main__:main" in entries
        assert "pyxsd-mcp = pyxsd.mcp.__main__:main" in entries
        metadata = wheel.read(f"pyxsd-{__version__}.dist-info/METADATA").decode("utf-8")
        assert "<!-- mcp-name: io.github.knorby/pyxsd -->" in metadata
        message = email.message_from_string(metadata)
        check_pypi(
            check_local(Path(__file__).resolve().parents[1]),
            {
                "info": {
                    "name": message["Name"],
                    "version": message["Version"],
                    "description": message.get_payload(),
                    "requires_dist": message.get_all("Requires-Dist"),
                },
            },
        )
