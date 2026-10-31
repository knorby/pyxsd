# Mixin extensions

The classes pyxsd generates are dynamic: they live in `schema.classes`,
built at compile time from the schema's type declarations. **Mixin
extensions** fold your own classes into those generated classes at
compile time, so your methods ride on every bound instance of the
selected types — and a convention method can add validation findings to
the report without touching base XSD validation.

The motivating case is a method the schema cannot express. A generated
`Patient` class knows how to bind `name` and `diagnosis`; a mixin
teaches it to label itself:

```python
from io import StringIO

from pyxsd import Extensions, Schema

ext = Extensions()


@ext.for_type("Patient")
class PatientMixin:
    def label(self):
        return f"{self.name} — {self.diagnosis}"


schema = Schema.compile(StringIO(XSD), extensions=ext)
document = schema.parse(StringIO(XML))
document.root.label()  # a plain method, callable on the bound instance
```

## Passing a registry to compile

An {class}`~pyxsd.extensions.Extensions` instance is a standalone,
per-compile registry — there is no global registry state. Register
mixins on it, then pass it to `Schema.compile`:

```python
ext = Extensions()
ext.for_type("Code", mixin=CodeMixin)
schema = Schema.compile("schema.xsd", extensions=ext)
```

Registration is **compile-time only**: the registry is read while the
classes are built. Mutating the registry after a compile leaves that
compile's classes untouched, and pyxsd never patches classes at runtime.
Recompiling with the same registry rebuilds the classes with the mixins
folded in (every compile produces fresh classes anyway). That includes
path selectors: their resolution cache is per compile — keyed by the
compile's schema, weakly, so a reused registry re-resolves each path
against the new declaration tree instead of answering with a stale
predicate from the previous one.

## Registering a mixin

Every registration method has two equivalent forms. Without a `mixin`
argument it returns a decorator that registers the decorated class:

```python
@ext.for_type("Patient")
class PatientMixin: ...
```

With `mixin=` it registers directly and returns nothing, which suits
mixins that live elsewhere:

```python
ext.for_type("Patient", mixin=PatientMixin)
```

## The five selectors

Each selector compiles to a predicate over the schema's type
declarations immediately, so an instance can be built before any schema
exists. A selector that matches nothing is **silent** — a schema
compiles fine when `for_type("DoesNotExist", ...)` matches nothing.
Only path selectors fail loudly (see [Path selectors](#path-selectors)).

| Selector | Matches |
| -------- | ------- |
| `for_type(name)` | The type declaration named *name* — a plain local name (`"Code"`) or a Clark name (`"{uri}Code"`). |
| `for_namespace(uri)` | Every type declared in namespace *uri*. An empty or `None` URI selects declarations that have no namespace. |
| `for_kind(kind, derived_from=...)` | Types of *kind* — `simpleType` or `complexType` (case-insensitive). With *derived_from*, only types whose XSD base is the named type; the base name is matched by local spelling (a prefixed or Clark spelling contributes its local part). |
| `for_path(path)` | The type of the element at an instance *path* from the document root down (see below). |
| `where(predicate)` | Every type declaration whose element representative satisfies *predicate* — the escape hatch for anything the named selectors cannot say. |

```python
@ext.for_namespace("http://example.com/cda")
class CdaMixin: ...


@ext.for_kind("complexType", derived_from="Patient")
class PatientLikeMixin: ...


@ext.where(lambda er: er.name in ("Patient", "Dead"))
class LongNameMixin: ...
```

## Path selectors

`for_path` names element declarations from the document root down,
every segment a local element name:

```python
ext.for_path("/ClinicalDocument/recordTarget/patient", mixin=PatientMixin)
```

The final segment's declaration contributes the selected type: its
inline anonymous type when one is declared, else the referenced global
type. Matching is by declaration identity, not by name — two same-named
local elements (`/Root/a/shared` and `/Root/b/shared`) select different
mixins even though their anonymous types collide in name.

Paths resolve at class-building time, once per compile, against the
composed declaration tree — so the registry can hold paths before the
schema is compiled, and a registry reused for another compile resolves
its paths again against that compile's tree. A path that cannot resolve
fails the **compile** with `PyXSDError`, naming the offending path:

- a segment that matches no element declaration in the traversal so
  far (a typo dangles loudly);
- a path continuing into a child declared on the **base** of an
  extension-typed element — traversal stays within each element's own
  content model and does not follow the base chain;
- a final segment typed by a builtin (`type="xs:string"` supplies no
  schema-declared type for a mixin to ride on), or an empty or
  ambiguous path.

## Ordering and dedup

**Registration order is precedence: a later registration outranks an
earlier one.** When several mixins match one type, they fold in with
the most recently registered first, and that order is their position in
the class's MRO — on a member both mixins define, the later-registered
one wins.

A mixin matched by several selectors appears once, at its
highest-precedence position; dedup is by identity (two same-named
distinct classes are two mixins).

```python
ext.for_type("Code", mixin=SharedMixin)
ext.for_namespace("http://example.com/a", mixin=SharedMixin)
# SharedMixin is in the generated class's MRO exactly once.
```

## Where mixins sit in the MRO

A generated class's MRO runs: the generated class first, then the
mixins (highest precedence first), then the XSD derivation bases, then
{class}`~pyxsd.schema_base.SchemaBase`. Two consequences, both
deliberate:

- **Mixin methods override inherited members.** A mixin defining
  `kind()` sits in front of the XSD base chain, and `super().kind()`
  reaches the base's implementation — mixins compose with XSD
  derivation the way any Python class does.
- **Mixins never shadow generated descriptors.** The generated class
  is first in the MRO, so its own `__dict__` — the attribute and
  element descriptors the binder routes through — always wins. A mixin
  that declares `id = "MIXIN"` on a type with an `id` attribute does
  not break binding; the mixin's member stays buried in its base and
  the descriptor keeps working.

Derived types see their bases' mixins through the MRO: a mixin
registered for `Patient` is a base of the generated `Inpatient` too.

A **union** simple type's class carries its mixins the same way: the
mixins sit between the union class and the union machinery
(`XsdDataType`), so a mixin's methods are callable on the union's
validated values and its hook runs on them, while the union's own
member-trying construction stays in charge.

## The `__pyxsd_validate__` hook

A mixin may define the convention method `__pyxsd_validate__(self)`,
returning findings that pyxsd appends to the instance report. The
contract, exactly as implemented:

- **The hook runs after base validation, on successfully constructed
  values only.** It adds findings on top of the XSD report, never
  replaces it. A value that fails its XSD type (or is dropped, or is
  bound as a raw/unvalidated fallback under a lax parse mode) runs no
  hook.
- **Call sites:** element instances (complex, simple-content, and bare
  simple-typed elements alike) and converted attribute values. A
  defaulted or fixed attribute value flows through the attribute
  descriptor too, so the hook fires for it as well.
- **Findings.** The hook returns an iterable of findings. `None` and
  empty iterables produce no findings; a bare `str` or single
  `ValidationIssue` counts as that one finding, and any other
  non-iterable return — `0` and `False` included — is reported as one
  unsupported finding rather than treated as an empty result:

  - a plain `str` becomes an **error** with code
    `extension-validation`;
  - a {class}`~pyxsd.validation.ValidationIssue` keeps its own
    severity, code, and message — its `phase` is forced to
    `"instance"`, and `element` is filled from the declaring name
    (the element or attribute the value belongs to) when the issue
    carries none;
  - any other finding type is recorded as one `extension-error`
    naming the unsupported type, and the rest of the findings are
    still processed.
- **Exception isolation.** A hook that raises — or raises while its
  findings are being consumed — becomes exactly one `extension-error`
  issue; binding continues.
- **Where findings land:** the active parse's
  {class}`~pyxsd.validation.ValidationReport` (`Document.report`).
  Outside a parse — for instance on a post-parse attribute write —
  findings are logged, like every other assignment-time diagnostic.

The hook costs nothing when absent: a type without
`__pyxsd_validate__` pays one `getattr` per constructed value.

## Example: a URI type with a method and a hook

A `ServiceUri` simple type restricting `xs:anyURI` — base validation
still owns the lexical space — carrying a plain `fetch()` method and a
hook that flags relative URIs as errors and plain `http` as a warning.
The mixin is registered once and applies to the type wherever it
appears: element text and attribute values.

```python
from io import StringIO

from pyxsd import Extensions, Schema
from pyxsd.validation import IssueSeverity, ValidationIssue

SCHEMA = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:m="http://example.com/u" targetNamespace="http://example.com/u"
    elementFormDefault="qualified">
  <xs:simpleType name="ServiceUri">
    <xs:restriction base="xs:anyURI">
      <xs:pattern value="\\S*"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="Resource">
    <xs:simpleContent>
      <xs:extension base="m:ServiceUri">
        <xs:attribute name="ref" type="m:ServiceUri"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:element name="resource" type="m:Resource"/>
</xs:schema>"""

ext = Extensions()


@ext.for_type("ServiceUri")
class UriMixin:
    def fetch(self):
        return f"fetched:{self}"

    def __pyxsd_validate__(self):
        parsed = self.to_parsed_uri()
        findings = []
        if not parsed.scheme and not parsed.netloc:
            findings.append(f"the service URI {str(self)!r} has no host")
        if parsed.scheme == "http":
            findings.append(
                ValidationIssue(
                    IssueSeverity.WARNING,
                    "insecure-scheme",
                    f"the service URI {str(self)!r} uses plain http",
                )
            )
        return findings


schema = Schema.compile(StringIO(SCHEMA), extensions=ext)
document = schema.parse(
    StringIO(
        '<m:resource xmlns:m="http://example.com/u" ref="relative/path">https://host/x</m:resource>'
    )
)

print(document.root.fetch())  # fetched:https://host/x
print(document.root.ref.fetch())  # fetched:relative/path
print(document.report.has_errors)  # True — the ref value has no host
```

The report carries the hook's error (`extension-validation`,
`element="ref"`, phase `instance`) while base `xs:anyURI` validation
runs untouched. The `https://host/x` text produces no finding, and a
plain-`http` value would produce a warning with code
`insecure-scheme` instead.

## What extensions are not

- **No post-compile extension.** Registration is compile-time; there
  is no runtime class patching and no way to attach a mixin to classes
  a compile already built.
- **No overlay folding.** The overlay mechanism
  (`Schema.compile(..., overlay=...)`, see {doc}`architecture`) is
  untouched: overlays do not fold mixin members, and a mixin's members
  are not overlay code.
- **No builtin-type extension.** Selectors match schema-declared type
  declarations only; `xs:string` and its siblings cannot carry a mixin.
- **Stubs do not emit mixin members.** `Schema.stub()` describes the
  generated surface; mixin members are absent from emitted stubs, so
  code relying on autocomplete against a stub will not see your
  mixin's methods.

See {doc}`validation` for the report the hook feeds, {doc}`binding`
for the parse modes that decide what a failed value binds as, and
{doc}`api` for the `Extensions` reference.
