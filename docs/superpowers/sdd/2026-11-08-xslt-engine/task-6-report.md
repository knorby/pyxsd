# Task 6 report — Package and characterize advisory stylesheet inspection

**Status:** DONE_WITH_CONCERNS

**Commit:** `b6270ad5962420545a334fb9e5c16ff853921748` — `feat: add advisory XSLT stylesheet inspection`
(signed: `git log -1 --format=%G?` reports `G`; not pushed)

**Test summary:** focused inspection/schema probes `39 passed`; full suite
`8874 passed, 400 skipped`; built sdist/wheel distribution check `1 passed`;
clean-wheel offline inspection passed for XSLT 2.0 and 3.0.

## 1. Interface

Created the required public front end:

```python
inspect_stylesheet(
    source: str | os.PathLike[str] | IO[str],
    *,
    language: str = "3.0",
    resource_policy: ResourcePolicy | None = None,
) -> StylesheetInspection
```

`XsltInspectionError` is defined in `src/pyxsd/xslt/errors.py` and extends
`PyXSDError`. Resource/input failures raise it; schema and binding findings do
not. `src/pyxsd/xslt/__init__.py` exports
`inspect_stylesheet`, `StylesheetInspection`, `InspectionDiagnostic`, and
`XsltInspectionError`.

`StylesheetInspection` contains:

- `capability: "bound" | "unsupported"`;
- `language`;
- `document: Document | None` (present only for `bound`);
- `report: ValidationReport`, constructed as a separate report rather than
  reusing `document.report`;
- `diagnostics: tuple[InspectionDiagnostic, ...]`, one classified advisory
  diagnostic per finding, plus explicit truncated-report/no-root diagnostics
  when details cannot be retained.

`InspectionDiagnostic` carries `code`, `message`, `classification`,
`rationale`, `severity`, `phase`, and optional `element`. Classifications are:

- `pyxsd-profile-gap` — the current XSD import/profile model cannot resolve a
  required construct;
- `pyxsd-validation-defect` — pyxsd reports a finding that the official schema
  does not actually violate;
- `stylesheet-finding` — an instance binding finding;
- `unclassified-schema-finding` — visible but not yet classified; it still
  blocks `bound`.

No inspection result contains a normative `XTSE*` code. Raw report and
diagnostic codes are XSD/pyxsd codes only.

## 2. Capability matrix

The packaged canonical schemas were compiled and bound under the current pyxsd
compiler. Both profiles are honestly unsupported today.

| Language | Schema | Classes | Schema errors | Capability | Findings |
| --- | --- | ---: | ---: | --- | --- |
| XSLT 2.0 | `schema-for-xslt20.xsd` (XSD 1.0) | 177 | 4 | `unsupported` | 1 `pyxsd-profile-gap`, 3 `pyxsd-validation-defect` |
| XSLT 3.0 | `schema-for-xslt30.xsd` (XSD 1.1) | 267 | 6 | `unsupported` | 1 `pyxsd-profile-gap`, 5 `pyxsd-validation-defect` |

### XSLT 2.0 — the four known findings, classified

| # | Code | Finding | Classification | Rationale |
| --- | --- | --- | --- | --- |
| 1 | `unknown-elementRef` | `xs:schema` in `import-schema` could not be resolved | `pyxsd-profile-gap` | pyxsd intentionally skips composing the schema-for-schemas global elements when the XML Schema namespace is imported; its built-in type vocabulary is always available, but `xs:schema` is a global element. The official schema is not invalid. |
| 2 | `attribute-restriction` | `version` of `transform-element-base-type` is not admitted by the base's attribute wildcard | `pyxsd-validation-defect` | The base `element-only-versioned-element-type` inherits `version` from `versioned-element-type` through an extension chain. The current attribute-restriction check does not inherit base uses through a restriction. |
| 3 | `wildcard-invalid` | `text-element-base-type` declares a wildcard while its base has none | `pyxsd-validation-defect` | `versioned-element-type` inherits `##other` from `generic-element-type` by extension. The current wildcard check reads only the direct base type's own wildcard. |
| 4 | `wildcard-invalid` | `element-only-versioned-element-type` declares a wildcard while its base has none | `pyxsd-validation-defect` | Same extension-chain inheritance issue as #3. |

### XSLT 3.0 — not inferred from XSLT 2.0

XSLT 3.0 has the same four findings plus two additional ones:

| Extra # | Code | Finding | Classification | Rationale |
| --- | --- | --- | --- | --- |
| 5 | `attribute-restriction` | `_version` of `transform-element-base-type` is not admitted by the base's attribute wildcard | `pyxsd-validation-defect` | Same attribute-use inheritance issue as XSLT 2.0 #2, for the second shadow attribute. |
| 6 | `wildcard-invalid` | `sequence-constructor-or-select` declares a wildcard while its base has none | `pyxsd-validation-defect` | Base `sequence-constructor-and-select` reaches the `generic-element-type` wildcard through extensions; the current check does not. |

The probe test explicitly requires the XSLT 3.0 counts/messages to differ from
the XSLT 2.0 set, so a future XSLT 2.0 pass cannot silently be treated as XSLT
3.0 support.

### Independent oracle evidence

The dev oracle `xmlschema` compiled the packaged schemas with zero errors:

```bash
uv run python - <<'PY'
import xmlschema
from pathlib import Path
base = Path('src/pyxsd/xslt/resources')
print(len(xmlschema.XMLSchema10(str(base / 'schema-for-xslt20.xsd')).all_errors))
print(len(xmlschema.XMLSchema11(str(base / 'schema-for-xslt30.xsd')).all_errors))
PY
# 0
# 0
```

This supports the classification that the four/six findings are pyxsd
validation/profile limitations, not official-schema defects. The automated
probe repeats this oracle check with `pytest.importorskip("xmlschema")`.

Simple fixtures (`literal-result.xsl`, `package.xsl`) did bind to generated
roots despite the schema findings, but capability remains `unsupported` because
the report has errors. Returning a document from a profile with known false
positives would hide the gap; the bound path is tested separately against a
synthetic clean schema.

## 3. Packaged resources and provenance

Packaged under `src/pyxsd/xslt/resources/`:

| Packaged file | Upstream URL | Original SHA-256 | Packaged SHA-256 | Changes |
| --- | --- | --- | --- | --- |
| `schema-for-xslt30.xsd` | https://www.w3.org/TR/xslt-30/schema-for-xslt30.xsd | `1c4c087b7b913f693f43aa9e24b0a06b6db60edee861ca930b1f188b21fec2fc` | `d980fb2a1689f8c2a5526abbbd270dccd9a5afa23bf5edabd2b1f743d661f47a` | `XMLSchema11.xsd` replaces the `xmlschema11-1` URL; `xml.xsd` added to the XML-namespace import. |
| `schema-for-xslt20.xsd` | https://www.w3.org/2007/schema-for-xslt20.xsd | `2614b194decb2ca130d47b831b23adf197af52c458808951bdc13c1783846db4` | `fc7ff19ea914b1c56a39582cbf7d558b814a271f92dde39fe75ba288bda92524` | `XMLSchema10.xsd` and `xml.xsd` replace the absolute W3C URLs. |
| `XMLSchema11.xsd` | https://www.w3.org/TR/xmlschema11-1/XMLSchema.xsd | `047e652e9863065d297651c103e366daa24fb2c0933cebcf1982272cb80600f5` | `186a903e2b99454d537edd36b0ebacbef4934888c36ebd9d39140d5b9913baf5` | Renamed from upstream `XMLSchema.xsd`; `xml.xsd` replaces the absolute URL. |
| `XMLSchema10.xsd` | https://www.w3.org/2001/XMLSchema.xsd | `e767a159c179bd6e4dc2bac2a63a62f50cbd027ec19d6b5cc9eb1c2f33e0cb2e` | `0d3f23eea191a116991a9cec18af2adc67912905eb7e6b11037013fc278ee197` | Renamed from upstream `XMLSchema.xsd`; `xml.xsd` replaces the absolute URL. |
| `xml.xsd` | https://www.w3.org/2001/xml.xsd | `61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c` | `61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c` | None. |
| `LICENSE-W3C.txt` | https://www.w3.org/Consortium/Legal/copyright-software-19980720 | — | — | Full W3C Software Notice and License (1998-07-20). |
| `NOTICE.md` | — | — | — | Provenance table, local rewrite record, non-normative statement, redistribution requirements. |

The only schema-content changes are the local `schemaLocation` rewrites shown
above. `diff -u` against the downloaded originals confirmed no other changes.
`NOTICE.md` records the original and packaged digests; the probe test asserts
every packaged digest and upstream URL appears there.

Because the canonical files contain upstream trailing whitespace and no final
newline in one case, `.pre-commit-config.yaml` now excludes
`src/pyxsd/xslt/resources/.*\.xsd` from `trailing-whitespace` and
`end-of-file-fixer`. Without that exclusion, the repository hooks rewrite the
files and invalidate provenance. The exclusion is documented alongside the
existing digest-pinned fixture exclusion.

## 4. Offline and clean-install evidence

The full XSLT 3.0 closure is `schema-for-xslt30.xsd` →
`XMLSchema11.xsd` → `xml.xsd`; the 2.0 closure is
`schema-for-xslt20.xsd` → `XMLSchema10.xsd` → `xml.xsd`. All schema locations
are relative filenames, and the probe test walks the authored
`xs:import`/`xs:include` locations and asserts every target is a packaged
sibling.

Focused offline policy test:

```bash
uv run pytest tests/xslt/test_inspection.py tests/xslt/probes/test_stylesheet_schemas.py -q
# 39 passed
```

The tests include:

- a recording policy asserting `network_requests == 0` and non-empty local
  reads through the caller policy;
- a `socket.socket.connect` guard proving inspection succeeds with no policy
  and no network;
- an offline compile of both closures with a policy that rejects any
  `http`/`https` read.

Fresh-wheel check outside the checkout:

```bash
WORK=$(mktemp -d .../pyxsd-clean-inspect.XXXXXX)
uv venv "$WORK/venv"
uv pip install --python "$WORK/venv/bin/python" --no-deps \
  dist/pyxsd-1.4.0-py3-none-any.whl
uv pip install --python "$WORK/venv/bin/python" 'defusedxml>=0.7.1,<1' 'elementpath>=4.7,<6'
# socket.socket.connect patched to raise in the probe process
```

Output:

```text
resources: ['LICENSE-W3C.txt', 'NOTICE.md', 'XMLSchema10.xsd', 'XMLSchema11.xsd',
            'schema-for-xslt20.xsd', 'schema-for-xslt30.xsd', 'xml.xsd']
hashes: {'XMLSchema10.xsd': '0d3f23eea191', 'XMLSchema11.xsd': '186a903e2b99',
         'schema-for-xslt20.xsd': 'fc7ff19ea914', 'schema-for-xslt30.xsd': 'd980fb2a1689',
         'xml.xsd': '61960fb3131e'}
2.0 unsupported 4 4
3.0 unsupported 6 6
missing source raised XsltInspectionError
```

No runtime download occurs. The caller-provided resource policy governs all
reads, including the packaged schemas; a policy that denies them produces
`XsltInspectionError`.

## 5. Distribution verification

`tests/test_distributions.py` now asserts, corpus-independently:

- the sdist contains all five schema files, `LICENSE-W3C.txt`, and `NOTICE.md`
  under `src/pyxsd/xslt/resources/`;
- the wheel contains the same seven files under
  `pyxsd/xslt/resources/`;
- the sdist excludes `tests/xslt/corpus/**` (alongside the existing XSTS and
  XPath corpus exclusions).

Commands and output:

```bash
uv build
# Successfully built dist/pyxsd-1.4.0.tar.gz
# Successfully built dist/pyxsd-1.4.0-py3-none-any.whl

PYXSD_DIST_DIR="$PWD/dist" uv run pytest tests/test_distributions.py -q
# 1 passed
```

## 6. Coverage evidence

Focused coverage command:

```bash
uv run pytest tests/xslt/test_inspection.py tests/xslt/probes/test_stylesheet_schemas.py \
  -q --cov=pyxsd.xslt --cov-branch --cov-report=term-missing
```

Result:

```text
Name                           Stmts   Miss Branch BrPart   Cover
src/pyxsd/xslt/__init__.py         3      0      0      0 100.0%
src/pyxsd/xslt/errors.py           3      0      0      0 100.0%
src/pyxsd/xslt/inspection.py     119      0     28      0 100.0%
TOTAL                            125      0     28      0 100.0%
39 passed
```

Every added line and branch is exercised, including:

- valid 2.0/3.0 profiles and invalid/unsupported language rejection;
- `bound` with a clean synthetic schema, ensuring the optional document is
  present and the inspection report is a distinct object;
- `unsupported` for both canonical schemas, missing instance root, and a
  budgeted report with no retained issue details;
- the `unknown-elementRef` profile-gap branch, both attribute-restriction
  defect branches, both wildcard-invalid defect branches, the unclassified
  schema-finding fallback, and the stylesheet-finding branch;
- truncation and no-root diagnostic branches, both independently and together;
- missing source, malformed path source, malformed anonymous stream,
  policy-denied source, and missing packaged schema error wrapping;
- `StylesheetInspection`/`InspectionDiagnostic` invariant failures;
- public package exports and diagnostic formatting;
- the offline policy, socket guard, independent XSD oracle, and provenance/
  resource-closure probes.

## 7. Commands run

```bash
uv run pytest tests/xslt/test_inspection.py tests/xslt/probes/test_stylesheet_schemas.py -q
# 39 passed

uv run ruff check src/pyxsd/xslt tests/xslt/test_inspection.py \
  tests/xslt/probes/test_stylesheet_schemas.py tests/test_distributions.py
# All checks passed

uv run ruff format --check src/pyxsd/xslt ...
# 6 files already formatted

uv run mypy src/pyxsd --exclude 'src/pyxsd/mcp/'
# Success: no issues found in 122 source files

uv run pytest -q
# 8874 passed, 400 skipped in 132.29s

uv build
# built sdist and wheel

PYXSD_DIST_DIR="$PWD/dist" uv run pytest tests/test_distributions.py -q
# 1 passed
```

The pre-commit run during commit passed all hooks, including GPG signing; the
commit signature check reports `G`.

## 8. Limitations and concerns

1. **Canonical profiles are currently unsupported.** The 2.0 and 3.0 packaged
   schemas each produce pyxsd schema reporting errors. Inspection therefore
   returns `capability="unsupported"` and no document. This is deliberate:
   returning a document despite known false positives would hide the gap.
2. **The findings are pyxsd core validation limitations, not schema defects.**
   The preferred long-term resolution is a general pyxsd fix: inherit attribute
   uses and attribute wildcards through extension/restriction chains, and
   expose the schema-for-schemas global element declarations needed by
   `xsl:import-schema`. That core work is outside this advisory front end.
3. **The bound path is covered by a synthetic clean schema.** No canonical
   profile currently exercises `capability="bound"`. The test verifies the API
   and document/report separation, but a future pyxsd core fix must re-run the
   canonical probes and update the capability matrix and expected finding
   counts.
4. **Inspection is not the normative compiler.** The packaged schemas and the
   bound `Document` are inspection views only. The compiler must continue to
   use the authoritative normalized module tree. Inspection does not mutate the
   stylesheet source or packaged resources, and it never converts findings to
   `XTSE*` errors.
5. **Resource policy scope.** A supplied policy governs the packaged schemas
   too, consistent with pyxsd's trusted-policy model. A policy that denies local
   package files makes inspection fail with `XsltInspectionError`; callers that
   restrict reads must allow the seven packaged resources.
6. **Filesystem package assumption.** Resources are resolved through
   `importlib.resources.as_file`; normal wheel/sdist installations (unpacked)
   are verified. Importing pyxsd from a zip archive is not a supported install
   mode for this prototype.
7. **Pre-commit scope change.** `.pre-commit-config.yaml` gained an exclusion
   for the canonical XSD resources so the provenance hashes stay valid. This is
   the only file changed beyond the brief's listed set; it directly protects
   the packaged-resource contract.

## Fix round 1 — TDD evidence

**Post-hoc red-state reconstruction, not a chronological TDD transcript.** The
original Step 2 output was not retained. This reconstruction was performed
after the approved commit `b6270ad5962420545a334fb9e5c16ff853921748`, using an
isolated temporary export of base commit
`f4014372bdce73ddab499191319370bfc0818da2` plus only the Task 6 test files and
fixtures from HEAD. The current checkout was not modified.

### Setup

```bash
BASE=f4014372bdce73ddab499191319370bfc0818da2
WORK=$(mktemp -d /private/var/folders/5r/2nh6117s363bpp1nfqgzpmqm0000gn/T/opencode/pyxsd-task6-red.XXXXXX)
git archive "$BASE" | tar -x -C "$WORK"
mkdir -p "$WORK/tests/xslt/probes" "$WORK/tests/xslt/fixtures/inspection"
cp tests/xslt/test_inspection.py "$WORK/tests/xslt/test_inspection.py"
cp tests/xslt/probes/test_stylesheet_schemas.py "$WORK/tests/xslt/probes/test_stylesheet_schemas.py"
cp tests/xslt/fixtures/inspection/literal-result.xsl "$WORK/tests/xslt/fixtures/inspection/literal-result.xsl"
cp tests/xslt/fixtures/inspection/package.xsl "$WORK/tests/xslt/fixtures/inspection/package.xsl"
test ! -e "$WORK/src/pyxsd/xslt" && echo 'confirmed: base tree has no src/pyxsd/xslt'
```

Output:

```text
confirmed: base tree has no src/pyxsd/xslt
```

The temporary export contained the base tree's `pyproject.toml`, `src/pyxsd`
without `src/pyxsd/xslt`, and only the Task 6 test modules and fixtures from
HEAD. No Task 6 production code or packaged resources were present.

### Brief's focused command

```bash
cd "$WORK"
uv run pytest tests/xslt/test_inspection.py tests/xslt/probes/test_stylesheet_schemas.py -q
```

Exact output (uv selected CPython 3.15.0 in the isolated export; the failure is
import-level and version-independent):

```text
Using CPython 3.15.0
Creating virtual environment at: .venv
   Building pyxsd @ file:///private/var/folders/5r/2nh6117s363bpp1nfqgzpmqm0000gn/T/opencode/pyxsd-task6-red.JA8JTi
      Built pyxsd @ file:///private/var/folders/5r/2nh6117s363bpp1nfqgzpmqm0000gn/T/opencode/pyxsd-task6-red.JA8JTi
Installed 36 packages in 83ms

==================================== ERRORS ====================================
________________ ERROR collecting tests/xslt/test_inspection.py ________________
ImportError while importing test module '/private/var/folders/5r/2nh6117s363bpp1nfqgzpmqm0000gn/T/opencode/pyxsd-task6-red.JA8JTi/tests/xslt/test_inspection.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/Users/knorby/.local/share/uv/python/cpython-3.15.0-macos-aarch64-none/lib/python3.15/importlib/__init__.py:88: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/xslt/test_inspection.py:25: in <module>
    from pyxsd.xslt import inspection as inspection_module
E   ModuleNotFoundError: No module named 'pyxsd.xslt'. Did you mean: 'pyxsd.xsi'?
________ ERROR collecting tests/xslt/probes/test_stylesheet_schemas.py _________
ImportError while importing test module '/private/var/folders/5r/2nh6117s363bpp1nfqgzpmqm0000gn/T/opencode/pyxsd-task6-red.JA8JTi/tests/xslt/probes/test_stylesheet_schemas.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/Users/knorby/.local/share/uv/python/cpython-3.15.0-macos-aarch64-none/lib/python3.15/importlib/__init__.py:88: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
tests/xslt/probes/test_stylesheet_schemas.py:22: in <module>
    from pyxsd.xslt import inspection as inspection_module
E   ModuleNotFoundError: No module named 'pyxsd.xslt'. Did you mean: 'pyxsd.xsi'?
=========================== short test summary info ============================
ERROR tests/xslt/test_inspection.py
ERROR tests/xslt/probes/test_stylesheet_schemas.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
2 errors in 0.47s
```

Exit status: `2`.

### Conclusion

Both Task 6 test modules fail during collection with
`ModuleNotFoundError: No module named 'pyxsd.xslt'`, proving the inspection
front end (`pyxsd.xslt.inspection`, `pyxsd.xslt.errors`) and the packaged
resources are absent at the base tree. This matches the brief's Step 2
expectation: the command fails because inspection and package resources are
absent. The red state is import-level, so the tests never reach the offline or
advisory assertions. This evidence is a faithful post-hoc reconstruction, not a
chronological record of the original TDD run.
