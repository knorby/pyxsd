"""The RELAX NG capability page is indexed, truthful, and executable.

The page is the single capability reference for native RELAX NG support.  These
tests parse its contracts from the MyST source, execute every Python and CLI
snippet against small ASCII fixtures, and reject wording that implies XSD
translation, browser XSLT replacement, or generated library markers.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "relax-ng.md"

FORMATS = ("pyxsd", "pyxsd-json", "parker", "badgerfish", "jsonml")
RNG_NS = "http://relaxng.org/ns/structure/1.0"
XSD_TYPES = "http://www.w3.org/2001/XMLSchema-datatypes"
MATRIX_ROWS = (
    "Compile, parse, revalidate, batch",
    "Mutable document and writers",
    "Typed Python access",
    "Stubs",
    "Dictionary/data/JSON export",
    "Data/JSON import",
    "JSON Schema generation",
    "Introspection and search",
    "Pydantic",
    "Arrow, Parquet, and datasets",
    "Schema comparison",
    "MCP and CLI",
)
OPTIONAL_MARKER = "requires the pydantic and arrow extras"


def page_text() -> str:
    assert PAGE.is_file(), "docs/relax-ng.md must describe the RELAX NG capability"
    return PAGE.read_text(encoding="utf-8")


def fenced_blocks(markdown: str, language: str) -> list[str]:
    """Return every fenced block for *language* in document order."""
    blocks: list[str] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].startswith(f"```{language}"):
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def release_section(markdown: str) -> str:
    return markdown.split("## Release capability matrix", 1)[1].split("\n## ", 1)[0]


def run_python_blocks(blocks: list[str], tmp_path: Path) -> dict[str, object]:
    namespace: dict[str, object] = {"__name__": "relaxng_docs"}
    for index, source in enumerate(blocks):
        exec(compile(source, f"relax-ng.md[python:{index}]", "exec"), namespace)
    return namespace


def test_page_is_indexed_and_reference_pages_link_it():
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    assert "relax-ng" in index
    assert "{doc}`relax-ng`" in index

    for name in ("api.md", "data-conversion.md", "integrations.md", "mcp.md", "supported.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "relax-ng" in text, name


def test_release_matrix_names_every_required_row():
    section = release_section(page_text())
    for row in MATRIX_ROWS:
        assert f"| {row} |" in section, row
    assert section.count("\n| ") >= len(MATRIX_ROWS)


def test_formats_native_options_and_defaults_are_exact():
    text = page_text()
    flat = " ".join(text.split())
    for name in FORMATS:
        assert f'`"{name}"`' in text, name
    assert "`typed` and `always_list` are native-only" in flat
    assert 'format="pyxsd"' in text and 'fidelity="best-effort"' in text
    assert 'format="pyxsd-json"' in text and 'fidelity="strict"' in text

    from pyxsd.relaxng import RelaxNGDocument, RelaxNGSchema

    export = inspect.signature(RelaxNGDocument.to_data).parameters
    assert export["format"].default == "pyxsd"
    assert export["fidelity"].default == "best-effort"
    assert export["typed"].default is True
    assert export["always_list"].default is False
    imported = inspect.signature(RelaxNGSchema.from_data).parameters
    assert imported["format"].default == "pyxsd-json"
    assert imported["fidelity"].default == "strict"


def test_tagged_profile_version_and_shape_are_exact():
    text = page_text()
    assert 'x-pyxsd-profile: "relaxng"' in text
    assert "x-pyxsd-profile-version: 1" in text
    assert '{"#value": ...}' in text
    assert '"version": 1' in text

    schema = _ambiguous_schema()
    document = schema.parse(StringIO("<value>12</value>"))
    profile = document.to_data(format="pyxsd-json")
    tag = profile["value"]["#value"]
    assert set(tag) == {"version", "grammar", "lexical", "context", "candidates"}
    assert tag["version"] == 1
    assert tag["lexical"] == "12"
    assert tag["context"] == {"base_uri": None, "namespaces": {}}
    assert len(tag["candidates"]) == 2
    assert tag["candidates"] == sorted(set(tag["candidates"]))

    contract = schema.json_schema(format="pyxsd-json")
    assert contract["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert contract["x-pyxsd-profile"] == "relaxng"
    assert contract["x-pyxsd-profile-version"] == 1
    assert contract["x-pyxsd-validation"]["level"] == "structural"
    assert "relaxng-grammar-validity" in contract["x-pyxsd-validation"]["unvalidated"]


def test_exclusions_custom_library_scope_and_local_model_scope_are_explicit():
    text = " ".join(page_text().split())
    for phrase in (
        "bare `null`",
        "neither empty content nor nil",
        "`xsi:type`",
        "`xsi:nil`",
        "no synthesized defaults",
        "DTD Compatibility",
        "compact syntax",
        "NVDL",
        "schema-aware XSLT",
        "schema-aware XPath",
        "XSD translation",
        "no global declarations",
        "`datatype_libraries`",
        "application-supplied",
        "MCP never accepts",
        "local route",
        "ancestor",
        "sibling",
        "not comparable",
        "language equivalence",
        "inclusion",
        "corpus",
        "`diagnostics_complete`",
        "`suppressed_count`",
    ):
        assert phrase in text, phrase


_NEGATED = re.compile(
    r"\b(?:no|not|never|neither|nor|without|cannot|can't|doesn't|don't|isn't|aren't|"
    r"rejects?|excludes?|rather than|instead of|outside)\b",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"^(?:(?:and|or|nor|but)\s+)?"
    r"(?:invent|adds?|add(?:ed)?|emits?|emit(?:ted)?|produces?|produced?|generates?|"
    r"generated?|converts?|converted?|maps?|mapped?|honou?rs?|honou?red?|respects?|"
    r"supports?|uses?|applies?|translates?|treats?|routes?|selects?|dispatches?|"
    r"requests?|synthesi[sz]es?|creates?|injects?|supplies?|defaults?)\b",
    re.IGNORECASE,
)
_CLAIMS = {
    "XSD translation": re.compile(
        r"\b(?:translat|convert|emit|produc|generat|map)\w*\b.*\bXSD\b.*\bRELAX NG\b|"
        r"\bXSD\b.*\b(?:translat|convert|emit|produc|generat|map)\w*\b.*\bRELAX NG\b|"
        r"\bRELAX NG\b.*\b(?:translat|convert|emit|produc|generat|map)\w*\b.*\bXSD\b",
        re.IGNORECASE,
    ),
    "global declarations": re.compile(r"\bglobal declarations?\b", re.IGNORECASE),
    "xsi semantics": re.compile(
        r"\bxsi:(?:type|nil)\b.*\b(?:select|dispatch|request|default|apply|applies|"
        r"synthesi[sz]e|create|honou?rs?|respects?|supports?|uses?|treats?)\b|"
        r"\b(?:select|dispatch|request|default|apply|applies|synthesi[sz]e|create|"
        r"honou?rs?|respects?|supports?|uses?|treats?)\b.*\bxsi:(?:type|nil)\b",
        re.IGNORECASE,
    ),
    "synthesized defaults": re.compile(
        r"\bdefaults?\b.*\b(?:synthesized|applied|supplied|injected)\b",
        re.IGNORECASE,
    ),
}


def claim_clauses(markdown: str) -> list[list[str]]:
    """Split prose into independently evaluated claim units.

    A table cell is its own unit, so a negation in one cell cannot excuse a
    positive claim in another.  Each prose line becomes one or more sentences,
    then semicolon/comma clauses.  Fenced code is skipped: it is executable
    documentation, not a prose claim.
    """
    units: list[list[str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if not paragraph:
            return
        prose = " ".join(paragraph)
        paragraph.clear()
        for sentence in re.split(r"(?<=[.!?])\s+", prose):
            clauses = [
                clause.strip() for clause in re.split(r"(?:;|,)\s+", sentence) if clause.strip()
            ]
            if clauses:
                units.append(clauses)

    in_fence = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            flush()
            continue
        if line.startswith("#"):
            flush()
            units.append([line.lstrip("#").strip()])
            continue
        if line.startswith("|"):
            flush()
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            units.extend([cell] for cell in cells if cell)
            continue
        if re.match(r"^(?:[-*+]|\d+\.)\s+", line):
            flush()
        paragraph.append(re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line))
    flush()
    return units


def negative_claim_failures(markdown: str) -> list[tuple[str, str]]:
    """Every unnegated positive XSD-translation or xsi-semantics claim."""
    failures: list[tuple[str, str]] = []
    for clauses in claim_clauses(markdown):
        previous_negated = False
        for clause in clauses:
            local = bool(_NEGATED.search(clause))
            inherited = previous_negated and bool(_CONTINUATION.search(clause))
            negated = local or inherited
            if not negated:
                for label, pattern in _CLAIMS.items():
                    if pattern.search(clause):
                        failures.append((label, clause))
            previous_negated = negated
    return failures


def test_no_positive_xsd_translation_or_xsi_semantics_claims():
    assert not negative_claim_failures(page_text())


def test_negative_claim_check_scopes_negation_to_rows_cells_and_clauses():
    # A negation in another cell of the same row must not excuse this claim.
    row = (
        "| Compile, parse, revalidate, batch | no translation | "
        "Adds global declarations for every root |"
    )
    failures = negative_claim_failures(row)
    assert failures == [("global declarations", "Adds global declarations for every root")]

    # A sentence-scoped negation must not hide a later positive claim.
    assert negative_claim_failures(
        "pyxsd does not translate RELAX NG into XSD, and pyxsd emits XSD from RELAX NG."
    ) == [("XSD translation", "and pyxsd emits XSD from RELAX NG.")]
    assert negative_claim_failures("pyxsd emits XSD from RELAX NG") == [
        ("XSD translation", "pyxsd emits XSD from RELAX NG")
    ]
    assert negative_claim_failures("pyxsd honors xsi:type on RELAX NG documents") == [
        ("xsi semantics", "pyxsd honors xsi:type on RELAX NG documents")
    ]

    # The real matrix row still passes when the fallback clause is negated.
    assert not negative_claim_failures(
        "| Data/JSON import | All five import formats | "
        "Start-pattern roots instead of global declarations |"
    )


PUBLIC_OBJECT_NAMES = (
    "compile",
    "RelaxNGSchema",
    "RelaxNGDocument",
    "RelaxNGElement",
    "AlternativeValue",
    "ListInterpretation",
    "NodeAnnotations",
    "RelaxNGLimits",
    "RelaxNGLimitError",
    "StaleInterpretationError",
    "AmbiguousValueError",
    "stub",
    "from_data",
    "from_json",
    "json_schema",
    "projection",
    "projection_classes",
    "dependencies",
    "require_valid",
    "iter_parse",
    "to_dict",
    "to_data",
    "to_json",
    "annotations",
    "revalidate",
    "locate",
    "write",
    "to_string",
    "to_xdm",
    "xpath",
    "walk",
    "transform",
    "describe_schema",
    "find_components",
    "describe_component",
    "related_components",
    "compare_relaxng_structure",
    "models",
    "RelaxNGModelSet",
    "records",
    "RelaxNGRecordProjection",
    "FieldSource",
    "datatype",
    "candidate_id",
)


def test_page_names_every_documented_public_object():
    text = page_text()
    missing = [name for name in PUBLIC_OBJECT_NAMES if name not in text]
    assert not missing, missing


def test_core_python_snippets_execute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocks = [
        block for block in fenced_blocks(page_text(), "python") if OPTIONAL_MARKER not in block
    ]
    assert blocks
    namespace = run_python_blocks(blocks, tmp_path)
    assert namespace["document"].is_valid
    assert "document" in namespace


def test_optional_integration_snippet_executes(tmp_path, monkeypatch):
    pytest.importorskip("pydantic")
    pytest.importorskip("pyarrow")
    monkeypatch.chdir(tmp_path)
    blocks = [block for block in fenced_blocks(page_text(), "python") if OPTIONAL_MARKER in block]
    assert blocks
    namespace = run_python_blocks(blocks, tmp_path)
    assert namespace["catalog"].is_valid
    assert namespace["table"].num_rows == 2


def test_code_fixtures_and_fences_are_ascii():
    text = page_text()
    for language in ("python", "bash", "xml"):
        for block in fenced_blocks(text, language):
            assert block.isascii(), (language, block)


def _ascii_xml_blocks() -> list[str]:
    blocks = fenced_blocks(page_text(), "xml")
    assert len(blocks) >= 3
    for block in blocks:
        assert block.isascii()
    return blocks


def test_cli_snippets_run_against_ascii_fixtures(tmp_path):
    blocks = _ascii_xml_blocks()
    names = ("note.rng", "note.xml", "broken.xml")
    for name, block in zip(names, blocks, strict=False):
        (tmp_path / name).write_text(block + "\n", encoding="utf-8")

    commands = [
        line
        for block in fenced_blocks(page_text(), "bash")
        for line in block.splitlines()
        if line.startswith("pyxsd ")
    ]
    assert len(commands) == 6, commands

    results = []
    for command in commands:
        result = subprocess.run(
            [sys.executable, "-m", "pyxsd", *shlex.split(command)[1:]],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        results.append(result)

    assert results[0].returncode == 0, results[0].stderr
    assert results[0].stderr == ""
    assert results[1].returncode == 0, results[1].stderr
    assert (tmp_path / "printed.xml").read_text(encoding="utf-8").strip() == "<note>hello</note>"
    assert "class Note" in results[2].stdout
    assert "AlternativeValue" in results[2].stdout
    assert results[3].returncode == 0, results[3].stderr
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == {"$": "hello"}
    assert results[4].returncode == 1
    assert "ToData schema-directed formats" in results[4].stderr
    assert "RelaxNGDocument.to_data(format=...)" in results[4].stderr
    assert "plan_layout" not in results[4].stderr
    assert results[5].returncode == 1
    assert "unexpected-element" in results[5].stderr


def test_page_has_no_library_markers_or_xslt_replacement_wording():
    text = page_text().lower()
    for phrase in (
        "generated by pyxsd",
        "rendered by pyxsd",
        "created with pyxsd",
        "xslt replacement",
        "replaces xslt",
        "browser xslt",
    ):
        assert phrase not in text, phrase


def test_source_links_resolve():
    text = page_text()
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    targets += re.findall(r"\{doc\}`([^`]+)`", text)
    for target in targets:
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path.endswith(".md"):
            continue
        assert (PAGE.parent / path).is_file(), target


def test_documented_public_names_exist():
    from pyxsd import relaxng
    from pyxsd.relaxng.comparison import compare_relaxng_structure
    from pyxsd.relaxng.introspection import (
        describe_component,
        describe_schema,
        find_components,
        related_components,
    )

    for name in (
        "compile",
        "RelaxNGSchema",
        "RelaxNGDocument",
        "AlternativeValue",
        "ListInterpretation",
        "NodeAnnotations",
        "RelaxNGLimits",
        "RelaxNGLimitError",
        "StaleInterpretationError",
        "AmbiguousValueError",
    ):
        assert hasattr(relaxng, name), name

    for name in (
        "parse",
        "stub",
        "from_data",
        "from_json",
        "json_schema",
        "projection",
        "projection_classes",
        "dependencies",
        "require_valid",
        "iter_parse",
    ):
        assert hasattr(relaxng.RelaxNGSchema, name), name
    for name in (
        "to_dict",
        "to_data",
        "to_json",
        "annotations",
        "revalidate",
        "locate",
        "write",
        "to_string",
        "to_xdm",
        "xpath",
        "walk",
        "transform",
    ):
        assert hasattr(relaxng.RelaxNGDocument, name), name

    for function in (
        describe_schema,
        describe_component,
        find_components,
        related_components,
        compare_relaxng_structure,
    ):
        assert callable(function)


def test_documented_integration_names_exist():
    pytest.importorskip("pydantic")
    pytest.importorskip("pyarrow")
    from pyxsd import relaxng
    from pyxsd.integrations.arrow import records
    from pyxsd.integrations.projection import FieldSource
    from pyxsd.integrations.pydantic import models

    assert "datatype" in FieldSource.__dataclass_fields__
    assert "candidate_id" in FieldSource.__dataclass_fields__

    compiled = relaxng.compile(
        StringIO(
            f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="value">
      <data type="integer" datatypeLibrary="{XSD_TYPES}"/>
    </element>
  </start>
</grammar>"""
        )
    )
    model_set = models(compiled)
    for name in ("model_for", "from_document", "from_node", "to_document"):
        assert hasattr(model_set, name), name
    projection = records(compiled, element="value")
    for name in (
        "batches",
        "table",
        "write_parquet",
        "write_dataset",
        "dataset_metadata",
    ):
        assert hasattr(projection, name), name


AMBIGUOUS_SCHEMA = f"""<grammar xmlns="{RNG_NS}">
  <start>
    <element name="value">
      <choice>
        <data type="integer" datatypeLibrary="{XSD_TYPES}"/>
        <data type="string" datatypeLibrary="{XSD_TYPES}"/>
      </choice>
    </element>
  </start>
</grammar>"""


def _ambiguous_schema():
    from pyxsd import relaxng

    return relaxng.compile(StringIO(AMBIGUOUS_SCHEMA))


def test_ambiguous_native_to_data_transform_stays_lexical_without_a_tag(tmp_path):
    """The CLI's native ToData() path never fabricates RELAX NG scalar tags.

    ``RelaxNGDocument.to_data(typed=True)`` is the typed schema-directed API
    and tags ambiguous values.  The ordinary ``ToData()`` transform walks the
    lexical tree, so an ambiguous scalar must remain its lexical text with no
    ``#value`` object inventing candidate metadata.
    """
    from pyxsd.transforms import ToData

    document = _ambiguous_schema().parse(StringIO("<value>12</value>"))
    exported = document.transform(ToData())
    assert exported == {"$": "12"}
    assert "#value" not in json.dumps(exported)

    (tmp_path / "value.rng").write_text(AMBIGUOUS_SCHEMA + "\n", encoding="utf-8")
    (tmp_path / "value.xml").write_text("<value>12</value>\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyxsd",
            "-i",
            "value.xml",
            "-s",
            "value.rng",
            "--schema-language",
            "relax-ng",
            "-t",
            "ToData()",
            "-o",
            "data.json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert payload == {"$": "12"}
    assert "#value" not in json.dumps(payload)
