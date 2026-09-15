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
| `declaration-attribute` | ERROR | A declaration's XML attribute is illegal for its XSD representation: `default` and `fixed` together, an invalid `use`/`form`/`final`/`block` token, a global-only attribute on a local declaration, a `ref` conflicting with `name`/`type`/`form`/inline type, a `type` attribute together with an inline type, a name or `id` that is not an NCName, a `default`/`fixed` value outside the declared type's lexical/value space (including a user-defined simple type's facets), a substitution member whose derivation the head's `final` excludes, a declaration in the XML Schema instance namespace, occurrence attributes on a top-level `xs:group` or `name` on an `xs:group` reference, or a compositor/`group` particle whose `minOccurs` exceeds its `maxOccurs`. The substitution-member check is deliberately an under-approximation: it reports only a member type whose *immediate* derivation method is excluded, and does not walk a blocked step earlier in the derivation chain or reject a member type that is wholly unrelated to the head's type. |
| `invalid-occurs` | ERROR | An occurrence attribute (`minOccurs`/`maxOccurs`) is not a lexically valid `nonNegativeInteger` (`unbounded` is only valid on `maxOccurs`). The default value 1 is used so class building can continue. |
| `all-rule` | ERROR | `xs:all` compositor legality: the `all` itself carries `minOccurs` > 1 or a `maxOccurs` above 1, a contradictory `minOccurs=1 maxOccurs=0`, an emptiable (`minOccurs=maxOccurs=0`) form inside a group definition, appears directly inside a `sequence`/`choice` (directly or through a group reference), carries a `name` attribute, or contains a particle other than an element declaration, wildcard, or group reference whose content model is an `all` with `minOccurs=maxOccurs=1`; two element particles with the same expanded name but different type declarations in one compositor (Element Declarations Consistent); under an `all`, two particles with the same expanded name, an element in the substitution group of another, or two overlapping wildcards (UPA). Under XSD 1.1 the element and wildcard particles of an `all` may carry relaxed occurrence bounds and the `all` itself may be emptiable as a complex type's content model, so the XSD 1.0 per-particle and fixed-`maxOccurs` limits are deliberately not enforced. |
| `pointless-particle` | ERROR | A `sequence`/`choice` with no particle children whose `minOccurs` lets it match zero (an empty compositor) appears inside a group definition that is referenced with `minOccurs="0"`; such a particle is pointless there and must be eliminated (the particlesHa rule). Deliberately conservative: compositors with particle children are never reported even when every child is optional or `maxOccurs="0"` — they can still match content, so eliminating them could change the language (MS pins such schemas valid, groupL007) — an empty compositor with `minOccurs="1"` is unsatisfiable rather than eliminable, compositors outside an optionally-referenced group are left alone, and `xs:all` falls under `all-rule`. |
| `particle-restriction` | ERROR | A complex type derived by restriction from a complex type with a particle violates a shape of cos-particle-restrict (XSD 1.0 §3.9.6; the deciding rule name is in the message). Reported today: a wildcard restricting an element declaration or a `sequence`/`choice` (Forbidden), an `all` restricting or restricted by a different compositor, a `choice`/wildcard over an `all` whose branches reach outside the base (RecurseUnordered per branch: a required member no branch covers, a mapped member exceeding the base member's range, or a wildcard no base wildcard covers), wildcard-over-wildcard subsumption (NSSubset: namespace constraint subset by extension, `processContents` non-widening, occurrence containment), occurrence containment on the derivation pair itself, an element over a wildcard whose namespace the wildcard does not admit or whose occurrence range widens, the Forbidden cells at every nesting level inside same-kind compositors, the NameAndTypeOK clauses on element pairs — expanded-name equality or substitution-group membership (transitive, including a local declaration that shares a same-named global's membership under XSD 1.1), type subsumption with no `extension` step, `fixed` value preservation, `nillable` direction, the base's disallowed substitutions as a subset of the derived's, the base's identity constraints as a subset of the derived's, and occurrence containment under compositors that cannot repeat — and the Recurse pairing over same-kind compositors and over an `all` base: sequences align order-preservingly with optional base members absorbed (a shared repeated base member may serve consecutive derived members, the 1.1 absorption reading), choices map each derived branch onto a distinct base branch (order-insensitive, injective), `all`-over-`all` and sequence-over-`all` map order-insensitively with every required base member covered and the mapped members' occurrence ranges summed per base member (a derived wildcard that fits no single base wildcard may split across the overlapping base wildcards when no base wildcard's `processContents` is weakened and no base minimum is starved), and a choice over an `all` restricts it when every branch restricts the `all` on its own; the combinatorial cells — an element restricting a choice/sequence/`all` (EltOverGroup, RecurseAsIfGroup) by mapping onto one member with the group's occurrence multiplied into that member's range (a `maxOccurs=0` element is vacuous and matches anything), a sequence restricting a choice (MapAndSum) where every derived member maps onto some base member and the sequence's effective total range (its own occurrence times its member count) stays inside the choice's, a group restricting a wildcard (NSRecurseCheckCardinality) where every member is admitted to the wildcard and the group's effective total range stays inside the wildcard's, and a base member left over by a sequence alignment being emptiable in the §3.9.6 sense rather than merely `minOccurs=0`; local element declarations are read form-aware — an unqualified local element has the absent namespace, so it is a different expanded name from a same-named global or `ref`, and a `maxOccurs=0` element is never excluded by a namespace constraint. Deliberately deferred: group-over-element (XSD 1.0 forbids the shape, and under the 1.1 profile the exemplars are valid through a language-inclusion reading in which a derived choice inside a sequence spreads over several base members — particlesHb008/Hb011 — which a per-pair check would reject), so the cell stays silent rather than approximate; the check is skipped (never an error) when the base type is unresolved or either particle tree could not be compiled. |
| `misplaced-declaration` | ERROR | A declaration sits in a container whose construction-time context cannot hold it: an `xs:attribute` or `xs:attributeGroup` whose containing type has no corresponding declaration table, a compositor (`all`/`choice`/`sequence`) or group reference in a container that cannot accept it, or a non-top-level `xs:notation` or group reference. This is a legacy context-sensitive placement diagnostic emitted while the tree is built; it coexists with the newer table-driven `declaration-child`/`declaration-order` checks. |
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
| `schema-compose` | ERROR / WARNING | A composition directive is invalid or its referenced schema cannot be used. ERROR for a missing `schemaLocation` attribute, a malformed referenced schema, a repeated `annotation` on `xs:include`/`xs:import`, or an `xs:redefine` that redefines components whose base schema cannot be opened. WARNING when an `xs:include`/`xs:import`/`xs:redefine` `schemaLocation` names a document that does not exist (an unresolvable location is a hint, not a rule violation). |
| `import-unresolved` | ERROR / WARNING | An `xs:import` could not be satisfied from a `schemaLocation` or `namespace_schemas` (namespaced mode). WARNING for a namespace-only import that is merely a hint; ERROR when a component from that namespace is referenced by the importing document, so the unresolved import is fatal. An instance- or caller-supplied schema that is missing remains an ERROR. |
| `compose-cycle` | WARNING | A repeated include/redefine was deduplicated. |
| `compose-namespace` | ERROR | Include target namespace mismatch. |
| `compose-invalid` | ERROR | A composition rule violation beyond a missing resource: an `xs:import` whose `namespace` does not match the imported document's target namespace; an `xs:redefine` of a component the base document does not define, or in a namespace other than the redefining schema's; the same component of one base document redefined twice; an unqualified self reference in a chameleon redefine; and an attributeGroup redefine that is not a valid restriction (adds or reorders attributes, drops a `fixed` value, changes a non-optional `use`, or duplicates an attribute pulled in by its self reference). |
| `internal` | WARNING | Parser internal inconsistency — please report. |

## Strict mode

`pyxsd --strict` exits with status 1 when the report contains any
`ERROR`-severity issue. Warnings never affect the exit code.

## Known limitations (schema legality)

The schema-phase checks are intentionally shallow in a few places, and a
handful of conformance-corpus cases are deferred. Recorded here so they
survive outside the branch's uncommitted working notes:

- **Substitution-derivation under-approximation.** The `declaration-attribute`
  substitution-member check reports only a member type whose *immediate*
  derivation method the head's `final` excludes. It does not walk a blocked
  step earlier in the derivation chain, and it accepts a member type that is
  wholly unrelated to the head's type.
- **Duplicate names and `xs:ID` uniqueness are main-document only.** Duplicate
  component names and duplicate `id` values contributed by separate
  included/imported documents are not reported; both checks are scoped to the
  main schema document.
- **`vc:*` conditional inclusion.** In a document that uses XSD 1.1 `vc:*`
  conditional inclusion, the ancestor walk used to evaluate it suppresses
  same-document duplicate-name detection.
- **Shared-include conflicting redefine.** A conflicting `xs:redefine` reached
  through a shared include (the same base document reached by more than one
  path) is suppressed rather than reported.
- **Inline list/union members.** `atomic-required` under-reports for *inline*
  anonymous list/union members; atomicity is checked for named and
  `memberTypes`-referenced members.
- **`attP032` false reject.** A valid schema is rejected by the
  substitution-derivation check. The only fix for it regresses the disputed
  cyclic `s4_2_4si01`, and the two expectations contradict each other
  (`attP032` vs `schU1`) within the same suite.
- **Composition residuals.** `schN10`/`schN12` need group-redefine
  content-model restriction validation; `schG9`/`schG10` need per-schema
  import visibility (high-risk under global composition); `schG2.v` is an
  instance-binding gap.
- **XSD 1.1 built-in types.** 41 XSD 1.1 built-in types remain unimplemented
  (the Area H feature gap); schemas that use them are not validated.
- **Caller-supplied schemas.** A missing caller-supplied resource
  (`namespace_schemas`, or an instance `xsi:schemaLocation`) is still an
  `ERROR`, unlike a schema's own missing include/import/redefine, which warns
  (`Override/over029.v01`).

```{note}
When re-running the XSTS suite, use a clean bytecode cache
(`PYTHONPYCACHEPREFIX=$(mktemp -d)`). A same-length source edit can leave a
header-valid but stale `.pyc` in place and silently invalidate a measurement.
```
