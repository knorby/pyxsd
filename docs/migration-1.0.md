# Migrating from pyxsd 0.1 to 1.0

pyxsd 1.0 is the first release in twenty years, ported to modern Python and
rebuilt for the ecosystem of 2026. It is a **new generation of the same
idea**: schema-compiled Python classes, a validated instance tree, a
transform pipeline, and a pure-Python runtime with one dependency
(`elementpath`, for XSD regular expressions). The node shape, the CLI
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
  message, and optional element name. A schema reports at
  `Schema.report`; a bound document reports the merged run at
  `Document.report`, and `require_valid()` turns errors into a raised
  `ValidationError` carrying that report.
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
| Python 2.3+, separate ElementTree/cElementTree required | Python 3.11+ only; one pure-Python runtime dependency (`elementpath`, for XSD regular expressions) |
| `setup.py` install, Windows exe installer | `pyproject.toml` + hatchling; `pip install pyxsd`; wheel + sdist |
| `pyXSD.py` script | `pyxsd` console script (also `python -m pyxsd`) |

### Modules and imports

| 0.1 | 1.0 |
| --- | --- |
| `pyxsd/pyXSD.py` (the whole pipeline in one module) | `pyxsd/schema.py` (compile) + `pyxsd/document.py` (bind, transform, write) |
| `pyxsd/schemaBase.py` | `pyxsd/schema_base.py` |
| `pyxsd/xsdDataTypes.py` | `pyxsd/xsd_data_types.py` |
| `pyxsd/elementRepresentatives/` (camelCase modules) | `pyxsd/element_representatives/` (snake_case modules) |
| `pyxsd/writers/xmlTreeWriter.py` | `pyxsd/writers/xml_tree_writer.py` |
| `pyxsd/writers/xmlTagWriter.py` | `pyxsd/writers/xml_tag_writer.py` |
| `pyxsd/transforms/cellSizer.py` etc. | **moved to `examples/legacy/`** |
| the `SendTreeToPyXSD` transform | `Document.revalidate()` |
| `from pyxsd.pyXSD import PyXSD` | `import pyxsd` (then `pyxsd.Schema.compile` / `pyxsd.parse`) |
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
built-ins (`PrintData`). See
`examples/legacy/README.md` for how to run them.

### The `PyXSD` pipeline → `Schema` and `Document`

The biggest change: the monolithic `PyXSD` class is gone. In 0.1,
constructing `PyXSD(xmlFileInput=..., xsdFile=..., ...)` ran the whole
pipeline — parse the schema, bind the instance, write outputs, run
transforms — inside `__init__`. 1.0 splits that into explicit objects
with one job each:

```python
# 0.1
from pyxsd.pyXSD import PyXSD

parser = PyXSD(
    xmlFileInput="inventory.xml",
    xsdFile="inventory.xsd",
    xmlFileOutput=False,
)
root = parser.schemaRootInstance
for issue in parser.report.issues:
    print(issue.format())

# 1.0
import pyxsd

schema = pyxsd.Schema.compile("inventory.xsd")
schema.require_valid()  # raises pyxsd.ValidationError if the schema is bad

document = schema.parse("inventory.xml")
document.require_valid()
root = document.root
for issue in document.report.issues:
    print(issue.format())
```

The mapping, piece by piece:

| 0.1 / interim | 1.0 |
| --- | --- |
| `PyXSD(xmlFileInput=..., xsdFile=...)` | `pyxsd.Schema.compile(xsd)` then `schema.parse(xml)` — or the one-call shortcut `pyxsd.parse(xml, xsd=...)`, which reads schema hints from the instance like the CLI does |
| `parser.schemaRootInstance` | `document.root` |
| `parser.report` | `document.report` (the schema's own findings stay at `schema.report`; a document's report merges both) |
| checking `report.has_errors` yourself | `document.is_valid`, or `document.require_valid()` / `schema.require_valid()` which raise `pyxsd.ValidationError` (it carries the failing `.report`) |
| `xmlFileOutput=...` | `document.write(path)` — nothing is written unless you ask |
| `transforms=["PrintData()"]` (call strings) | `document.transform(callable)` — transforms are plain callables taking the tree root (see below) |
| `transformOutputName=...` | write the returned document yourself: `updated.write("out.xml")` |
| walking the tree by hand to read values | `document.xpath(expr)` / `document.find(path)` / `document.findall(path)` return the bound nodes; `document.to_dict()` / `document.to_json()` export plain Python data |
| XSD 1.0-only schemas | `Schema.compile(xsd, xsd_version="1.0")` (the processor otherwise runs XSD 1.1) |

Each parse returns a fresh `Document`, so one compiled schema can serve
many documents (and many parses of the same document) without their
reports mixing.

### Transforms: strings → callables

In 0.1, library users passed transform *call strings*
(`transforms=["PrintData()"]`) that pyxsd resolved by class name at run
time. In 1.0, `Document.transform` takes the callable itself:

```python
def normalize_units(root):
    ...  # mutate the tree or return a new root
    return root


updated = document.transform(normalize_units)
updated.write("normalized.xml")
```

A transform that returns a tree root comes back as a new `Document`
against the same schema; returning anything else returns that value
unchanged; returning `None` leaves the document as it was. A returned
document shares the pre-transform report — after a structural change,
call `updated.revalidate()` for a report that reflects the new shape
(that is also the replacement for 0.1's `SendTreeToPyXSD` transform,
which re-fed the written tree back through the pipeline).

The shipped `Transform` classes still work — `__init__` takes the root,
`__call__` does the work — they are just invoked through a one-line
wrapper (see {doc}`transforms/using`).

**On the command line, nothing changes**: `-t 'PrintData()'`,
`>`-chained calls, and `-T` transform files are exactly the syntax they
were; the CLI resolves the strings to callables for you.

### CLI

- Same flag set (`-i`, `-s`, `-p`, `-k`, `-o`, `-d`, `-t`, `-T`, `-c`,
  `-v`, `-q`), plus `--strict`, `--mode strict|lax`, and
  `--namespaces strict|legacy`.
- Transform calls must be **calls with parentheses**
  (`-t 'PrintData()'`); a bare class name is now a usage error. Calls are
  parsed with `ast.literal_eval` — only literal arguments are accepted
  (0.1 used `eval`). This call-string syntax is unchanged by the
  library-side move to callable transforms: the CLI resolves the strings
  to callables itself.
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
  and raw string `raise` statements). Invalid schemas and documents do
  not raise by default: `Schema.require_valid()` and
  `Document.require_valid()` raise `pyxsd.ValidationError` (carrying the
  failing report) when you want errors to be fatal.
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
   stderr (or the issue objects on `document.report` when scripting the
   library).

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
import pyxsd

schema = pyxsd.Schema.compile("data.xsd")
schema.require_valid()  # raise on a bad schema

document = schema.parse("data.xml")
if document.report.has_errors:
    for issue in document.report.issues:
        print(issue.format())
root = document.root
```

Notes:

- Compilation and binding are separate steps: `Schema.compile` builds
  the schema (and its classes) once; every `schema.parse(xml)` returns a
  fresh `Document` for one instance. `pyxsd.parse(xml)` is the shortcut
  that also resolves the schema from the instance's own schema hints.
- Nothing is written unless you ask: `document.write(path)` /
  `document.to_string()` replace the `xmlFileOutput` /
  `transformOutputName` options.
- Transforms are callables passed to `document.transform(fn)` — call
  strings are a CLI-only syntax now.
- `require_valid()` raises `pyxsd.ValidationError` whose `.report`
  attribute holds every issue; `document.is_valid` is the boolean form.
- Import paths follow the module renames table above; writer APIs
  (`XmlTreeWriter`, `XmlTagWriter`) are unchanged in behavior.

### Transform authors

- Inherit from `pyxsd.transforms.Transform` (now an ABC — `__init__` must
  be defined, as it always had to be) and import from the package root
  module:

  ```python
  from pyxsd.transforms import Transform, Displayer, iter_tree
  ```

- Transforms run through `Document.transform`: your class is invoked as
  `transform_cls(root)(*args, **kwargs)`, and a plain function taking the
  root works too (no wrapper needed). The 0.1 call-string form
  (`transforms=[...]`) is gone from the library; the CLI `-t` syntax is
  unchanged.
- After a transform changes the tree's shape, call
  `transformed.revalidate()` — the 0.1 `SendTreeToPyXSD` transform
  (re-feeding the written tree through the pipeline) is replaced by this
  one call, without the temp files.
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
- Facet declarations on user simpleTypes are enforced where values bind
  through the simple type (pattern uses the XSD 1.1 dialect via the
  `elementpath` dependency); the remaining exception is `simpleContent` —
  see the gaps table in {doc}`supported`.
- Schemas must be well-formed XML; composition errors (missing include,
  cycles, namespace mismatch) are reported with dedicated codes.

## History

The 0.1 codebase's story — ORNL 2006, the mentors, the design intent — is
preserved in {doc}`history/origins`. Every structural move in the 1.0
modernization was done with `git mv` so that history remains traceable
through the repository itself.
