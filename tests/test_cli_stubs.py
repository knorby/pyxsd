import subprocess
import sys
from pathlib import Path

ORDERS = Path(__file__).parent / "fixtures" / "inventory" / "schema.xsd"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pyxsd", *args], capture_output=True, text=True)


def test_emit_stubs_to_stdout():
    result = run_cli("--emit-stubs", "-s", str(ORDERS))
    assert result.returncode == 0
    assert "class itemType" in result.stdout


def test_emit_stubs_to_file(tmp_path):
    out = tmp_path / "inventory.pyi"
    result = run_cli("--emit-stubs", str(out), "-s", str(ORDERS))
    assert result.returncode == 0
    assert "class itemType" in out.read_text()


def test_stubs_compact_requires_emit_stubs():
    result = run_cli("--stubs-compact", "-s", str(ORDERS))
    assert result.returncode != 0


def test_stubs_compact_output(tmp_path):
    result = run_cli("--emit-stubs", "--stubs-compact", "-s", str(ORDERS))
    assert result.returncode == 0
    assert "from pyxsd" not in result.stdout
    assert "class itemType" in result.stdout


def test_emit_stubs_needs_schema():
    result = run_cli("--emit-stubs")
    assert result.returncode != 0


def test_emit_stubs_prints_nothing_to_stderr_on_success():
    result = run_cli("--emit-stubs", "-s", str(ORDERS))
    assert result.returncode == 0
    assert result.stderr == ""


def test_emit_stubs_missing_schema_errors(tmp_path):
    result = run_cli("--emit-stubs", "-s", str(tmp_path / "missing.xsd"))
    assert result.returncode == 1
    assert "pyxsd: error:" in result.stderr


def test_emit_stubs_unwritable_output_errors(tmp_path):
    result = run_cli("--emit-stubs", str(tmp_path / "missing" / "out.pyi"), "-s", str(ORDERS))
    assert result.returncode == 1
    assert "pyxsd: error:" in result.stderr


def test_emit_stubs_composes_with_compilation_flags(tmp_path):
    out = tmp_path / "inventory.pyi"
    result = run_cli(
        "--emit-stubs",
        str(out),
        "-s",
        str(ORDERS),
        "--mode",
        "lax",
        "--namespaces",
        "strict",
        "--xsd-version",
        "1.0",
    )
    assert result.returncode == 0
    assert "class itemType" in out.read_text()


def test_emit_stubs_composes_with_overlay_class_file(tmp_path):
    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        "from pyxsd.schema_base import SchemaBase\n"
        "\n"
        "\n"
        "class ExtraType(SchemaBase):\n"
        "    name = 'unusedType'\n"
    )
    out = tmp_path / "inventory.pyi"
    result = run_cli("--emit-stubs", str(out), "-s", str(ORDERS), "-c", str(overlay))
    assert result.returncode == 0
    assert "class itemType" in out.read_text()
