"""CLI schema-language dispatch and RELAX NG workflow parity.

``--schema-language`` selects exactly one compiler; a filename extension never
does.  RELAX NG has no instance schema hints, so every workflow that compiles a
schema requires an explicit ``-s``.  Parsed output, transforms, reports, and
``--strict`` go through the same document operations as XSD.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from pyxsd.binding import ParseModes
from pyxsd.cli import _build_document, main
from pyxsd.exceptions import PyXSDError

RNG_NS = "http://relaxng.org/ns/structure/1.0"

SCHEMA = f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="doc">
      <mixed>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

INSTANCE = "<doc>a<item>x</item>b</doc>"
INVALID_INSTANCE = "<doc><item>x</item><surprise/></doc>"

ATTRIBUTED_SCHEMA = f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="doc">
      <attribute name="lang"><text/></attribute>
      <mixed>
        <zeroOrMore><element name="item"><text/></element></zeroOrMore>
      </mixed>
    </element>
  </start>
</grammar>"""

ATTRIBUTED_INSTANCE = '<doc lang="en">a<item>x</item>b</doc>'

MIXED_ATTRIBUTED_SCHEMA = f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="doc">
      <attribute name="lang"><text/></attribute>
      <mixed>
        <optional><element name="extra"><empty/></element></optional>
      </mixed>
    </element>
  </start>
</grammar>"""

MIXED_ATTRIBUTED_INSTANCE = '<doc lang="en">hello<extra/>tail</doc>'
MIXED_LEXICAL_DATA = {"@lang": "en", "extra": None, "$": "hellotail"}


class _FakeStdin:
    """A non-tty stdin stand-in, matching the XSD CLI tests."""

    def __init__(self, text: str) -> None:
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text


@pytest.fixture
def rng_files(tmp_path: Path) -> tuple[Path, Path]:
    schema = tmp_path / "schema.rng"
    schema.write_text(SCHEMA, encoding="utf-8")
    instance = tmp_path / "instance.xml"
    instance.write_text(INSTANCE, encoding="utf-8")
    return schema, instance


def _relaxng_args(schema: Path, instance: Path | None = None) -> list[str]:
    args = ["--schema-language", "relax-ng", "-s", str(schema)]
    if instance is not None:
        args[:0] = ["-i", str(instance)]
    return args


# ---------------------------------------------------------------------------
# Parsing, parsed output, transforms, and exit status
# ---------------------------------------------------------------------------


def test_valid_instance_parses_without_report_or_output(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files

    assert main([*_relaxng_args(schema, instance), "-o", "/dev/null", "--strict"]) is None

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_invalid_instance_reports_without_strict(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files
    instance.write_text(INVALID_INSTANCE, encoding="utf-8")

    assert main([*_relaxng_args(schema, instance), "-o", "/dev/null"]) is None

    err = capsys.readouterr().err
    assert "unexpected-element" in err
    assert "surprise" in err


def test_strict_exits_nonzero_on_an_invalid_instance(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files
    instance.write_text(INVALID_INSTANCE, encoding="utf-8")

    with pytest.raises(SystemExit) as exitInfo:
        main([*_relaxng_args(schema, instance), "-o", "/dev/null", "--strict"])

    assert exitInfo.value.code == 1
    assert "unexpected-element" in capsys.readouterr().err


def test_mixed_content_parsed_output_keeps_text_and_tails(
    rng_files: tuple[Path, Path], tmp_path: Path
) -> None:
    schema, instance = rng_files
    parsed = tmp_path / "parsed.xml"

    main(
        [
            *_relaxng_args(schema, instance),
            "-k",
            "-p",
            str(parsed),
            "-o",
            "/dev/null",
        ]
    )

    assert parsed.read_text(encoding="utf-8") == INSTANCE


def test_print_data_preserves_mixed_content(
    rng_files: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema, instance = rng_files
    printed = tmp_path / "printed.xml"
    final = tmp_path / "final.xml"

    main(
        [
            *_relaxng_args(schema, instance),
            "-t",
            f'PrintData("{printed}")',
            "-o",
            str(final),
        ]
    )

    assert printed.read_text(encoding="utf-8") == INSTANCE + "\n"
    assert final.read_text(encoding="utf-8") == INSTANCE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_print_data_stdout_and_final_output_are_exact(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files

    main([*_relaxng_args(schema, instance), "-t", "PrintData()"])

    assert capsys.readouterr().out == INSTANCE + "\n" + INSTANCE


def test_to_dict_preserves_mixed_content(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files

    main([*_relaxng_args(schema, instance), "-t", "ToDict()"])

    assert json.loads(capsys.readouterr().out) == {"item": "x", "$": "ab"}


def test_to_dict_uses_lexical_attributes_on_relaxng(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = tmp_path / "schema.rng"
    schema.write_text(ATTRIBUTED_SCHEMA, encoding="utf-8")
    instance = tmp_path / "instance.xml"
    instance.write_text(ATTRIBUTED_INSTANCE, encoding="utf-8")

    main([*_relaxng_args(schema, instance), "-t", "ToDict()"])

    assert json.loads(capsys.readouterr().out) == {"@lang": "en", "item": "x", "$": "ab"}


def _mixed_attributed_files(tmp_path: Path) -> tuple[Path, Path]:
    schema = tmp_path / "mixed.rng"
    schema.write_text(MIXED_ATTRIBUTED_SCHEMA, encoding="utf-8")
    instance = tmp_path / "mixed.xml"
    instance.write_text(MIXED_ATTRIBUTED_INSTANCE, encoding="utf-8")
    return schema, instance


def test_native_to_data_exports_relaxng_mixed_content_lexically(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = _mixed_attributed_files(tmp_path)

    main([*_relaxng_args(schema, instance), "-t", "ToData()"])

    output = capsys.readouterr().out
    assert json.loads(output) == MIXED_LEXICAL_DATA
    assert "#value" not in output


def test_native_to_data_strict_rejects_mixed_content_loss(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = _mixed_attributed_files(tmp_path)

    with pytest.raises(SystemExit) as exitInfo:
        main([*_relaxng_args(schema, instance), "-t", 'ToData(fidelity="strict")'])

    assert exitInfo.value.code == 1
    err = capsys.readouterr().err
    assert "mixed-content" in err
    assert "character data" in err
    assert "plan_layout" not in err


@pytest.mark.parametrize("format", ["pyxsd-json", "parker", "badgerfish", "jsonml"])
def test_to_data_schema_directed_formats_fail_explicitly_on_relaxng(
    format: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = _mixed_attributed_files(tmp_path)

    with pytest.raises(SystemExit) as exitInfo:
        main([*_relaxng_args(schema, instance), "-t", f'ToData(format="{format}")'])

    assert exitInfo.value.code == 1
    err = capsys.readouterr().err
    assert "ToData schema-directed formats" in err
    assert "XSD-only" in err
    assert "RelaxNGDocument.to_data(format=...)" in err
    assert "pyxsd_export_data" in err
    assert "plan_layout" not in err


def test_to_data_transform_native_and_schema_directed_boundary(tmp_path: Path) -> None:
    from pyxsd import relaxng
    from pyxsd.transforms import ToData

    schema, instance = _mixed_attributed_files(tmp_path)
    document = relaxng.compile(schema).parse(instance)

    assert ToData(document.root)() == MIXED_LEXICAL_DATA
    with pytest.raises(PyXSDError, match=r"schema-directed formats.*XSD-only"):
        ToData(document.root, format="pyxsd-json")()


def test_transform_returning_root_keeps_mixed_content(
    rng_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema, instance = rng_files
    monkeypatch.chdir(tmp_path)
    (tmp_path / "noop_probe.py").write_text(
        "def NoopProbe(root):\n    return root\n", encoding="utf-8"
    )

    main([*_relaxng_args(schema, instance), "-t", "NoopProbe()"])

    assert capsys.readouterr().out == INSTANCE


def test_transform_file_and_output_file(rng_files: tuple[Path, Path], tmp_path: Path) -> None:
    schema, instance = rng_files
    transform_file = tmp_path / "transforms.txt"
    printed = tmp_path / "printed.xml"
    transform_file.write_text(f'PrintData("{printed}")\n', encoding="utf-8")
    output = tmp_path / "out.xml"

    main([*_relaxng_args(schema, instance), "-T", str(transform_file), "-o", str(output)])

    assert printed.read_text(encoding="utf-8") == INSTANCE + "\n"
    assert output.read_text(encoding="utf-8") == INSTANCE


def test_relaxng_ignores_instance_schema_hints(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files
    schema.write_text(
        f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="doc">
      <zeroOrMore><attribute><anyName/></attribute></zeroOrMore>
      <text/>
    </element>
  </start>
</grammar>""",
        encoding="utf-8",
    )
    instance.write_text(
        '<doc xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:noNamespaceSchemaLocation="missing.xsd">a</doc>',
        encoding="utf-8",
    )

    main([*_relaxng_args(schema, instance), "-o", "/dev/null"])

    assert capsys.readouterr().err == ""


def test_quiet_mode_still_prints_the_report(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files
    instance.write_text(INVALID_INSTANCE, encoding="utf-8")

    main([*_relaxng_args(schema, instance), "-q", "-o", "/dev/null"])

    assert "unexpected-element" in capsys.readouterr().err


def test_stdin_input_and_stdout_transform(
    rng_files: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema, _instance = rng_files
    printed = tmp_path / "printed.xml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", _FakeStdin(INSTANCE))

    main([*_relaxng_args(schema), "-t", f'PrintData("{printed}")'])

    assert capsys.readouterr().out == INSTANCE
    assert printed.read_text(encoding="utf-8") == INSTANCE + "\n"
    assert not (tmp_path / "stdin.xml").exists()


def test_relaxng_requires_an_explicit_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "instance.xml"
    instance.write_text(INSTANCE, encoding="utf-8")

    with pytest.raises(SystemExit) as exitInfo:
        main(["-i", str(instance), "--schema-language", "relax-ng"])

    assert exitInfo.value.code == 2
    err = capsys.readouterr().err
    assert "relax-ng" in err
    assert "requires" in err
    assert "-s" in err
    assert "schemaLocation" in err


def test_build_document_relaxng_rejects_a_missing_schema() -> None:
    with pytest.raises(PyXSDError, match="must be named"):
        _build_document(
            io.StringIO(INSTANCE),
            None,
            mode=ParseModes.STRICT,
            overlay=None,
            schema_language="relax-ng",
        )


# ---------------------------------------------------------------------------
# XSD-only option boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--mode", "lax"),
        ("--namespaces", "legacy"),
        ("--xsd-version", "1.0"),
        ("--overlayClassesFile", "overlay.py"),
    ],
)
def test_relaxng_rejects_xsd_only_options(
    flag: str,
    value: str,
    rng_files: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    schema, instance = rng_files

    with pytest.raises(SystemExit) as exitInfo:
        main([*_relaxng_args(schema, instance), flag, value])

    assert exitInfo.value.code == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "XSD-only" in err
    assert "relax-ng" in err


def test_relaxng_accepts_default_xsd_option_values(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files

    main(
        [
            *_relaxng_args(schema, instance),
            "--mode",
            "strict",
            "--namespaces",
            "strict",
            "--xsd-version",
            "1.1",
            "-o",
            "/dev/null",
        ]
    )

    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Stub emission and explicit language selection
# ---------------------------------------------------------------------------


def test_full_stub_emission_uses_relaxng_compiler(
    rng_files: tuple[Path, Path], tmp_path: Path
) -> None:
    schema, _instance = rng_files
    stub = tmp_path / "schema.pyi"

    main(["--emit-stubs", str(stub), *_relaxng_args(schema)])

    text = stub.read_text(encoding="utf-8")
    assert "class Doc" in text
    assert "class Item" in text
    assert "from __future__ import annotations" in text


def test_compact_stub_emission_uses_relaxng_compiler(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, _instance = rng_files

    main(["--emit-stubs", "--stubs-compact", *_relaxng_args(schema)])

    out = capsys.readouterr().out
    assert "class Doc" in out
    assert "from pyxsd" not in out


def test_extension_alone_never_selects_relaxng(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, _instance = rng_files

    with pytest.raises(SystemExit) as exitInfo:
        main(["--emit-stubs", "-s", str(schema)])

    assert exitInfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "xs:schema" in captured.err


def test_omitted_language_keeps_xsd_for_a_dot_rng_instance(
    rng_files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    schema, instance = rng_files

    with pytest.raises(SystemExit) as exitInfo:
        main(["-i", str(instance), "-s", str(schema), "-o", "/dev/null"])

    assert exitInfo.value.code == 1
    assert "xs:schema" in capsys.readouterr().err


def test_explicit_relaxng_language_ignores_the_filename_extension(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    renamed = tmp_path / "schema.xsd"
    renamed.write_text(SCHEMA, encoding="utf-8")

    main(["--emit-stubs", *_relaxng_args(renamed)])

    assert "class Doc" in capsys.readouterr().out


def test_xsd_stub_emission_stays_the_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = Path(__file__).parent / "fixtures" / "inventory" / "schema.xsd"

    main(["--emit-stubs", "-s", str(schema)])

    out = capsys.readouterr().out
    assert "class itemType" in out
    assert "RELAX NG" not in out
