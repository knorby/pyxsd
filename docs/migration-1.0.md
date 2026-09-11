# Migrating from pyxsd 0.1 to 1.0

pyxsd 1.0 is the first release in twenty years, ported to modern Python and
rebuilt for the ecosystem of 2026. It is a **new generation of the same
idea**: schema-compiled Python classes, a validated instance tree, a
transform pipeline, and a zero-dependency runtime. The node shape, the CLI
flags, and the XSD subset all survive; almost everything else around them
was modernized.

This document covers what changed, why, and how to move your code.

## Why the architecture changed

pyxsd 0.1 generated classes with an imperative `type()` factory and kept
class metadata (`_elementNames_`, `_attributeNames_`) in manually managed
class dictionaries. 1.0 keeps the same generated-class idea but uses the
modern machinery:

- **`types.new_class`** replaces the `type()` factory, so generated classes
  behave like ordinary classes under `inspect`, `copy`, and pickle probes.
- **`__set_name__`** on the `Element`/`Attribute` descriptors wires each
  descriptor to its owner and attribute name at class creation — no manual
  bookkeeping lists to keep in sync.
- **`__init_subclass__`** on `SchemaBase` collects the element/attribute
  name lists automatically through the MRO.
- **Descriptive `__getattr__`** on generated classes turns misspelled
  attribute access into an error message that names your class and lists
  its declared elements and attributes.
- **`ValidationReport`** replaces scattered `print` statements: all
  non-fatal issues are structured objects with severity, stable code,
  message, and optional element name, available at `PyXSD.report` and to
  any generated class through the parser back-reference.
- **`logging`** replaces prints for progress/trace output. The library
  never configures handlers; the CLI maps `-v`/`-q` to DEBUG/CRITICAL.
- **`pathlib.Path`, `argparse`, `abc.ABC`, context managers, and lazy
  `importlib` loading** replace the Python-2 era equivalents (`imp`,
  `optparse`, ad-hoc file handling, `sys.path` hacks).
- Schema composition (`include`/`import`/`redefine`) is implemented by
  splicing the composed documents into the main schema tree *before* class
  generation, and identity constraints (`key`/`unique`/`keyref`) run as a
  post-parse validation pass with an XPath subset.

## Breaking changes

### Python and packaging

| 0.1 | 1.0 |
| --- | --- |
| Python 2.3+, separate ElementTree/cElementTree required | Python 3.11+ only, standard library only (zero runtime dependencies) |
| `setup.py` install, Windows exe installer | `pyproject.toml` + hatchling; `pip install pyxsd`; wheel + sdist |
| `pyXSD.py` script | `pyxsd` console script (also `python -m pyxsd`) |

### Modules and imports

| 0.1 | 1.0 |
| --- | --- |
| `pyxsd/pyXSD.py` | `pyxsd/parser.py` |
| `pyxsd/schemaBase.py` | `pyxsd/schema_base.py` |
| `pyxsd/xsdDataTypes.py` | `pyxsd/xsd_data_types.py` |
| `pyxsd/elementRepresentatives/` (camelCase modules) | `pyxsd/element_representatives/` (snake_case modules) |
| `pyxsd/writers/xmlTreeWriter.py` | `pyxsd/writers/xml_tree_writer.py` |
| `pyxsd/writers/xmlTagWriter.py` | `pyxsd/writers/xml_tag_writer.py` |
| `pyxsd/transforms/cellSizer.py` etc. | **moved to `examples/legacy/`** |
| `from pyxsd.pyXSD import PyXSD` | `from pyxsd import PyXSD` |
| `from pyxsd.writers.xmlTreeWriter import XmlTreeWriter` | `from pyxsd.writers.xml_tree_writer import XmlTreeWriter` |

All internal module names are now snake_case and importable as such; every
layout change was a `git mv`, so history follows the files.

### Crystallography transforms

The eight application transforms (`CellSizer`, `SphereCutter`,
`ExpandCell`, `BravaisLattice`, `CoordViewer`, `FormatForVisit`, plus the
`Atom`/`Vector` helper libraries) moved from the package to
`examples/legacy/`. Importing `pyxsd.transforms.cellSizer` (or any
snake_case variant) now raises `ImportError`. The installed package keeps
the framework (`Transform`, `Displayer`, `iter_tree`) and the generic
built-ins (`PrintData`, `SendTreeToPyXSD`). See
`examples/legacy/README.md` for how to run them.

### CLI

- Same flag set (`-i`, `-s`, `-p`, `-k`, `-o`, `-d`, `-t`, `-T`, `-c`,
  `-v`, `-q`), plus new `--strict`.
- Transform calls must be **calls with parentheses**
  (`-t 'PrintData()'`); a bare class name is now a usage error. Calls are
  parsed with `ast.literal_eval` — only literal arguments are accepted
  (0.1 used `eval`).
- `-t` and `-T` are mutually exclusive; `-v` and `-q` are mutually
  exclusive (both exit 2).
- stdin input is read directly (no `stdin.xml` temp file).
- Exit codes are meaningful: `0` success, `1` fatal error or `--strict`
  with errors, `2` usage error. Validation issues go to **stderr** as a
  rendered report.
- Default output filenames changed where 0.1 was buggy: the transformed
  output default is now derived from the input name (0.1 silently reused
  the input file name); nameless/file-like inputs write `output.xml` in
  the current directory.

### Data types

- Class names use the **true XSD spelling**: the class for `xs:string` is
  `String`, but its `.name` attribute is `"string"` (0.1 used
  `"String"`); `Double` no longer reports itself as `"Float"`, and the
  misspelled `"NonPostive"` is now `"NonPositiveInteger"`.
- Lexical validation happens in `__new__` and raises `TypeError` on
  invalid input — previously many invalid values slipped through or
  crashed with exotic errors.
- `Boolean` has a `val` attribute, `str()` → `"true"/"false"`,
  `repr()` → `"True"/"False"` (0.1 raised `NameError` on repr).
- Empty simple content (`<x/>`) yields `""` — not `"True"` (a latent 0.1
  bug where `dataTypeVal = True` was stringified).

### Instances and descriptors

- Typed primitive values are stored as `_value_ = [<one string>]` — a
  **list contract**, consistently (0.1 sometimes stored a bare string,
  which the writers then char-split).
- Element-level assignment now works and validates:
  `item.quantity = Integer(4)` stores the value; in 0.1 the descriptor
  silently discarded it.
- Attributes remain **lexical strings** in `_attribs_` (so round-trip
  output is faithful); typed attribute values from `default`/`fixed`
  application land in the instance `__dict__`.
- Class-level access to element descriptors returns the descriptor (0.1
  crashed); instance-level assignment is type-checked.
- The quirk where an element *named* `name` shadows the generated class's
  metadata `name` is preserved; prefer `__name__` for the class name.

### Errors and reporting

- `print` statements → `logging` (library default WARNING) plus the
  `ValidationReport`. Codes are stable strings — see {doc}`validation`.
- Schema problems raise or record `PyXSDError` (0.1 mixed `ValueError`
  and raw string `raise` statements).
- Instance validation is more complete and more correct: attribute
  defaults/prohibitions/fixed, element defaults/fixed/nil, abstract
  checks, substitution groups, `xsi:type`, and identity constraints all
  report through codes that did not exist in 0.1.
- **The double-processing bug is fixed**: 0.1 constructed every
  grandchild element twice (each child processed by both its parent's
  loop and the factory), causing spurious duplicate children and bogus
  "Order Error"s on inline `complexType` schemas.

## Upgrade guide

### Command-line users

Your existing invocations mostly work as-is. Check:

1. Transform calls need parentheses: `PrintData()` not `PrintData`.
2. Crystallography transforms must be run from
   `examples/legacy/` (or copied next to your data).
3. Add `--strict` where a CI pipeline needs a failure signal.
4. Move your log parsing from "grep stdout" to the rendered report on
   stderr (or the JSON/tuple API on `PyXSD.report`).

### Library users

```python
# 0.1
from pyxsd.pyXSD import PyXSD

parser = PyXSD(
    xmlFileInput="data.xml",
    xsdFile="data.xsd",
    xmlFileOutput="_No_Output_",
    transformOutputName="_No_Output_",
)
# ... hope the prints were useful

# 1.0
from pyxsd import PyXSD

parser = PyXSD(xmlFileInput="data.xml", xsdFile="data.xsd", xmlFileOutput=False)
if parser.report.has_errors:
    for issue in parser.report.issues:
        print(issue.format())
root = parser.schemaRootInstance
```

Notes:

- Construction runs the whole pipeline (parse → validate → write →
  transforms). `PyXSD.report` holds every issue found.
- `xmlFileOutput=False` (or `"_No_Output_"`) suppresses the parsed-tree
  write; a filename or `True` (default name) writes it.
- `transformOutputName` accepts a filename or `"stdout"`; with `transforms`
  present, output is always written somewhere.
- Import paths follow the module renames table above; writer APIs
  (`XmlTreeWriter`, `XmlTagWriter`) are unchanged in behavior.

### Transform authors

- Inherit from `pyxsd.transforms.Transform` (now an ABC — `__init__` must
  be defined, as it always had to be) and import from the package root
  module:

  ```python
  from pyxsd.transforms import Transform, Displayer, iter_tree
  ```

- The visitor/walker API (`walk`, `classCollector`, `tagCollector`,
  `tagFinder`, `getElementsByName`, `find`/`findAll`, `makeElemObj`,
  `makeCommentElem`) is unchanged. `iter_tree(instance)` is new and
  recommended over manual recursion.
- `getInstancesByClassName` no longer crashes (`collection.append` typo
  fixed); transforms that write files now flush and close them reliably.
- User transform libraries import siblings with plain `from library import
  ...` — the loader resolves sibling imports from any directory.

### Schema authors

Your schemas keep working, with more of them validating correctly:

- `xs:all`, groups, attributeGroups, unions, wildcards, substitution
  groups, element refs, `xsi:type`, nil/default/fixed, abstract/final —
  all new or fixed in 1.0.
- Facet declarations on user simpleTypes are parsed but **not enforced**
  (except whitespace collapse on built-ins) — see the gaps table in
  {doc}`supported`.
- Schemas must be well-formed XML; composition errors (missing include,
  cycles, namespace mismatch) are reported with dedicated codes.

## History

The 0.1 codebase's story — ORNL 2006, the mentors, the design intent — is
preserved in {doc}`history/origins`. Every structural move in the 1.0
modernization was done with `git mv` so that history remains traceable
through the repository itself.
