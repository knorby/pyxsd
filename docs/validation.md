# Validation

pyxsd is a **lax validator**: it never aborts on bad data. Every problem it
finds is recorded as a `ValidationIssue` in a single
{class}`~pyxsd.validation.ValidationReport` reachable at `PyXSD.report`.
A run can produce a complete object tree and a full error report at the same
time.

```{note}
"Lax validator" here means *reporting is non-fatal*. It is separate from
the **parse mode** (`PyXSD(mode=...)`, `--mode`), which controls what is
bound into the tree when a document is invalid. The report is always
strict regardless of mode — see {doc}`binding`.
```

```python
from pyxsd import PyXSD

parser = PyXSD(xmlFileInput="inventory.xml", xsdFile="schema.xsd")
if parser.report.has_errors:
    for issue in parser.report.issues:
        print(issue.format())
```

## Report API

```{eval-rst}
.. autoclass:: pyxsd.validation.ValidationReport
   :members:
   :undoc-members:

.. autoclass:: pyxsd.validation.ValidationIssue
   :members:
   :undoc-members:

.. autoclass:: pyxsd.validation.IssueSeverity
   :members:
```

## Issue codes

Codes are stable strings. `ERROR`-level codes fail under `--strict` or
`report.has_errors`; `WARNING`-level codes never affect exit status.

| Code | Severity | Meaning |
| ---- | -------- | ------- |
| `order` | ERROR | Child order violates the sequence content model. |
| `declaration-child` | ERROR | A child element is not allowed by the declaration's child grammar. |
| `declaration-duplicate` | ERROR | A child that may appear at most once is repeated, two distinct alternatives (for example `simpleContent` and `complexContent`) occupy the same exclusive slot, two declarations share an `xs:ID`, or two named components share one symbol space (a duplicate global element/attribute/type/group/attributeGroup, or two identity constraints of one element with the same name). |
| `declaration-order` | ERROR | Children are out of order, or an exclusive content kind excludes later children. |
| `declaration-name` | ERROR | A declaration is missing its required name. |
| `declaration-attribute` | ERROR | A declaration's XML attribute is illegal for its XSD representation: `default` and `fixed` together, an invalid `use`/`form`/`final`/`block` token, a global-only attribute on a local declaration, a `ref` conflicting with `name`/`type`/`form`/inline type, a `type` attribute together with an inline type, a name or `id` that is not an NCName, a `default`/`fixed` value outside the declared type's lexical/value space (including a user-defined simple type's facets), a substitution member whose derivation the head's `final` excludes, or a declaration in the XML Schema instance namespace. |
| `facet` | ERROR | A constraining facet is not applicable to its base type, or its declared value is not legal for that base (bad lexical form, outside the base's value space, or a digit facet that violates the fixed value on an integer-derived type). |
| `facet-conflict` | ERROR | Two constraining facets in one restriction step cannot hold together: mutually exclusive bounds (`minInclusive`/`minExclusive`, `maxInclusive`/`maxExclusive`), a lower bound above the upper bound, or an empty value space once the base type's own fixed bounds are applied (for example `positiveInteger` with `maxExclusive="1"`, or a list `minLength` below 1). |
| `unexpected-element` | ERROR | Element is not declared in the content model and no wildcard allows it. |
| `wildcard-no-declaration` | ERROR | `processContents="strict"` wildcard matched an element/attribute with no global declaration (namespaced mode). |
| `occurrence-min` | ERROR | Fewer occurrences than `minOccurs` allows. |
| `occurrence-max` | ERROR | More occurrences than `maxOccurs` allows. |
| `missing-attribute` | ERROR | A required attribute is absent. |
| `unexpected-attribute` | WARNING | Attribute not declared in the schema. |
| `invalid-attribute` | ERROR | Attribute value fails its declared type. |
| `prohibited-attribute` | WARNING | Attribute declared `use="prohibited"` present. |
| `fixed-attribute` | ERROR | Attribute present with a value differing from `fixed`. |
| `unknown-type` | ERROR | Referenced type could not be resolved. |
| `atomic-required` | ERROR | A `list`'s `itemType` (or inline item type) is not an atomic simple type or a union with no list type anywhere in its transitive membership, or a `union`'s member type is a complex type rather than a simple type. Atomic, list and union members are all legal union members. |
| `unknown-namespace-prefix` | ERROR | A prefixed name uses a namespace prefix that is not bound in scope (namespaced mode). |
| `value` | ERROR | Text content failed lexical validation for its type. |
| `default` | ERROR | Element default value is not valid for the element's type. |
| `fixed-element` | ERROR | Element content differs from its `fixed` value. |
| `nil` | ERROR | `xsi:nil="true"` on a non-nillable element. |
| `abstract-element` | ERROR | Instance of an abstract element declaration. |
| `abstract-type` | ERROR | Direct instance of an abstract type (allowed only via `xsi:type`). |
| `blocked` | ERROR | Substitution-group member blocked by the head's `block`. |
| `xsi-type` | ERROR | `xsi:type` could not be resolved, is not validly derived from the declared type, or is blocked. |
| `unknown-root` | ERROR | Document root matches no global element declaration. |
| `multiple-roots` | ERROR | More than one global element matches the document root. |
| `identity-key` | ERROR | Key field missing or duplicate key value. |
| `identity-unique` | ERROR | Duplicate value under a `xs:unique` constraint. |
| `identity-keyref` | ERROR | Keyref value has no matching key/unique value. |
| `identity-unsupported` | WARNING | Identity-constraint XPath uses an unsupported construct. |
| `unknown-group` | ERROR | Referenced `xs:group` missing. |
| `circular-group` | ERROR | Group reference cycle. |
| `unknown-attributeGroup` | ERROR | Referenced `xs:attributeGroup` missing. |
| `circular-attributeGroup` | ERROR | Nested attributeGroup reference cycle. |
| `unknown-substitution-head` | ERROR | Substitution group references a missing head. |
| `circular-substitution-group` | ERROR | Substitution-group membership is cyclic (`foo` heads `bar` heads `foo`), so no well-founded head exists. |
| `unknown-elementRef` | ERROR | Element `ref` points to a missing global element. |
| `final` | ERROR | Derivation violates the base type's `final` attribute. |
| `schema` | ERROR | The schema file itself is malformed or unreadable. |
| `schema-hint` | WARNING | Malformed schemaLocation hint in the instance document. |
| `schema-compose` | ERROR | Missing or malformed included/imported schema file. |
| `import-unresolved` | ERROR | Namespace-only `xs:import` could not be satisfied from a schemaLocation or `namespace_schemas` (namespaced mode). |
| `compose-cycle` | WARNING | A repeated include/redefine was deduplicated. |
| `compose-namespace` | ERROR | Include target namespace mismatch. |
| `internal` | WARNING | Parser internal inconsistency — please report. |

## Strict mode

`pyxsd --strict` exits with status 1 when the report contains any
`ERROR`-severity issue. Warnings never affect the exit code.
