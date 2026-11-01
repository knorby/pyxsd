# Validation

pyxsd reports recoverable schema and instance findings in a
{class}`~pyxsd.validation.ValidationReport` rather than raising on the first
one. Each finding is a `ValidationIssue`. A compiled schema reports its own
findings at `Schema.report`; a bound document reports the merged run —
schema-phase issues followed by instance-phase issues — at
`Document.report`. A run can produce a bound object tree and multiple
validation findings at the same time.

Reporting is non-fatal only when parsing can proceed. An unreadable or
malformed input, or a document whose root is not `xs:schema`, raises
{class}`~pyxsd.exceptions.PyXSDError` instead of returning a partial report.
An undeclared instance root is recoverable: it produces an `unknown-root`
issue and a `Document` whose `root` is `None`.

```{note}
Recoverable reporting is separate from the **parse mode**
(`Schema.compile(..., mode=...)`, `--mode`), which controls whether invalid
values and unrecognized content are dropped or retained in a best-effort
bound tree. A bound tree is not necessarily complete or lossless, even in
lax mode. See {doc}`binding`.
```

The report collects the checks the processor supports; it is not a promise
that every possible violation or a complete, source-faithful tree is
returned. With facet enforcement enabled (the default), a parse mode does
not relax facet validation merely to retain an invalid value. Setting
`facets="off"` skips those checks, so the resulting report is not a full
validity verdict for facet-constrained values. The known schema-legality
limitations below also apply.

```python
import pyxsd

schema = pyxsd.Schema.compile("inventory.xsd")
schema.require_valid()  # raises ValidationError if the schema itself is bad

document = schema.parse("inventory.xml")
if document.report.has_errors:
    for issue in document.report.issues:
        print(issue.format())
```

Both `Schema.require_valid()` and `Document.require_valid()` raise
{class}`~pyxsd.exceptions.ValidationError`, whose `report` attribute is
the report that failed the check.

## Resource and parser policies

`pyxsd.compile(..., resource_policy=policy)` and
`pyxsd.parse(..., resource_policy=policy)` accept an optional
`pyxsd.resource_policy.ResourcePolicy`. The compiled schema retains this
policy for subsequent `Schema.parse()` and `Document.revalidate()` calls.
Without a policy, existing loading and parsing behavior is unchanged.

A policy supplies two methods:

- `read(source, *, base, purpose)` authorizes and loads a named resource,
  returning `ResourceData(source_id, content)` with its original bytes.
  `base` identifies the including document, not its directory; `purpose`
  is `"schema"` (an explicit or mapped schema, or an ordinary dependency),
  `"schema-hint"` (a schema the instance selected through its location
  hint), or `"instance"`. Canonical source IDs can name virtual filesystem
  layouts. They also determine nested dependency bases and composition-cycle
  detection.
- `parse(data, context)` parses admitted bytes and records namespace
  bindings in the supplied `NamespaceContext`.

Includes, imports, redefines, overrides, namespace mappings and instance
schema hints use the same policy. Raise `ResourcePolicyError` to refuse
access: unlike unavailable-resource errors, a denial is never downgraded
to an ignored import or schema-hint warning. A policy's `parse()` owns XML
parsing; `xml_policy` and `xml_limits` cannot be combined with it.

Streams pass through the policy parser too; byte streams preserve original
encoding, whereas text streams are encoded as UTF-8. Anonymous schema
streams have no implicit filesystem base: relative dependencies require a
canonical stream `.name`. Already-parsed `Element` inputs are rejected
when a policy is active, so they cannot bypass its parser.

`Schema.compile` and `Schema.parse` also accept opt-in `xml_policy` and
`xml_limits` arguments without a custom resource policy. `xml_policy=None`
keeps the stdlib parser and its current DTD/entity behavior;
`XmlParserPolicy.protected()` or `.strict()` selects a defusedxml-backed
profile, and `XmlLimits` bounds bytes, depth, and start-tag count. A
compile-time parser policy applies to the schema and its dependencies but is
not inherited by later instance parses, so pass it per parse. These hooks are
**not a sandbox or a secure parser implementation**. A policy is trusted
application code responsible for access checks, parser hardening, limits and
namespace evidence. Python overlays and extensions remain executable
application code, outside this XML-loading contract.

`pyxsd.RemoteResourcePolicy` is the built-in opt-in remote schema loader.
Selecting it authorizes a remote main schema, whether the caller supplied it
explicitly or the instance selected it through a location hint; a hint-selected
remote main also requires `allow_remote_hints=True`, and as the root read it
needs no `allowed_origins` entry. An **additional** remote hint resolved from a
local schema is not a root read and needs **both** `allow_remote_hints=True` and
`allow_local_remote=True`. An ordinary remote include/import from a local schema
needs `allow_local_remote=True`; once admitted, cross-origin dependencies of a
remote document need `allowed_origins`. Every request is HTTPS, address-checked,
budgeted, and subject to `RemoteLimits`; there is no persistent cache. Remote
instance documents, remote-to-local pivots, `file:` URLs, and remote Python
overlays are refused. Once selected, transport/resolver failures abort the
compile even for a document-authored include/import. Full rules, defaults, and
the DNS-rebinding caveat are in {doc}`xml-security`.

Selecting the policy also governs instance parsing: `Schema.parse()` sends the
instance through the policy, which parses it with `XmlParserPolicy.strict()` and
caps it at `limits.response_bytes`. Local instances are affected too, so a
DOCTYPE-bearing or oversized instance is refused while the policy is attached.

## DTD-dependent ENTITY and ENTITIES values

During instance binding, `xs:ENTITY` values must name unparsed (`NDATA`)
entities declared in the instance's DTD; every token of `xs:ENTITIES` must
meet the same requirement. pyxsd collects these declarations from the prolog
and internal subset only when the compiled schema can validate such values,
including through restrictions, lists, unions, and element or attribute
defaults. Schemas unrelated to these types incur no declaration-collection
parse. This is an XSD datatype check, not DTD validation.

With complete declaration evidence, a missing name is invalid. No DOCTYPE
means an empty, complete declaration set, not permission to accept any
`NCName`. A parsed entity is not an unparsed entity. These failures use the
existing value, attribute, or default diagnostics for the binding site.

No external subset or external entity is fetched. An external subset makes
the evidence incomplete: names declared internally can still be checked,
but missing names remain lexical-only because the external subset might
declare them. If a value needs that unavailable evidence, the document gets
one `dtd-external-skipped` advisory, not an error or a warning per token.

Declaration evidence also depends on the input path:

- Paths and seekable streams can be reopened or rewound after XML parsing.
  The source must remain available and unchanged between parsing and
  collection; a failed reread leaves values lexical-only.
- `Schema.parse()` can collect and replay the prolog of a non-seekable
  stream when its compiled schema needs declarations. Top-level
  `pyxsd.parse()` consumes a non-seekable instance stream before binding,
  even when an explicit schema is supplied, so that path remains
  lexical-only. Use compiled `Schema.parse()` for prolog replay. This is an
  input provenance limitation, not a security protection.
- Caller-supplied `Element` trees without pyxsd source provenance and custom
  `ResourcePolicy` parser results remain lexical-only. A tree parsed by
  pyxsd can retain source provenance in its `NamespaceContext`; when that
  context is reused for compilation and the source remains readable,
  `Schema.parse(Element)` can collect declarations from that source.
  Direct datatype construction outside a document parse is lexical-only too.
- `Document.revalidate()` currently serializes and reparses the bound tree
  without preserving its DOCTYPE or declaration evidence. The new parse
  therefore sees an empty, complete declaration set and can report a valid
  `ENTITY`/`ENTITIES` value as undeclared. Revalidation is not currently a
  reliable validity check for these declaration-dependent documents.

Collection preserves the selected parser's declaration guards; it never
replaces security handlers. The default, protected, and strict profiles are
unchanged. See {doc}`xml-security` for their DOCTYPE/entity rules and
{doc}`xsd-validation` for the distinction between DTD and XSD validation.

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
| `declaration-child` | ERROR | A child element is not allowed by the declaration's child grammar. Also reported for a `simpleType` with no `restriction`/`list`/`union`, a simple-type `restriction` with a child outside the facets plus an optional inline `simpleType`, or non-whitespace character content inside `xs:notation`. |
| `declaration-duplicate` | ERROR | A child that may appear at most once is repeated (including a wildcard's second `xs:annotation`), two distinct alternatives (for example `simpleContent` and `complexContent`) occupy the same exclusive slot, two declarations share an `xs:ID`, or two named components share one symbol space (a duplicate global element/attribute/type/group/attributeGroup/notation, or two identity constraints of one target namespace with the same name). Also reported for a `list` carrying both an `itemType` attribute and an inline `simpleType` child. |
| `declaration-order` | ERROR | Children are out of order, or an exclusive content kind excludes later children. |
| `declaration-name` | ERROR | A declaration is missing its required name. |
| `declaration-attribute` | ERROR | A declaration's XML attribute is illegal for its XSD representation: `default` and `fixed` together; an invalid `use`/`form`/`final`/`block` token; a global-only attribute on a local declaration; a `ref` conflicting with `name`/`type`/`form`/inline type; a `type` attribute together with an inline type; a name or `id` that is not an NCName; a `default`/`fixed` value outside the declared type's lexical or value space (a user-defined simple type's facets included); a substitution member whose derivation the head's `final` excludes; occurrence attributes on a top-level `xs:group` or `name` on an `xs:group` reference; a compositor, `xs:any`, or group particle whose `minOccurs` exceeds its `maxOccurs`; or an inline `simpleType` that carries a `name`, which an anonymous local type must not. The substitution-member check is an under-approximation: it reports only a member type whose *immediate* derivation method is excluded, and does not walk a blocked step earlier in the derivation chain or reject a member type wholly unrelated to the head's. |
| `invalid-occurs` | ERROR | An occurrence attribute (`minOccurs`/`maxOccurs`) is not a lexically valid `nonNegativeInteger` (`unbounded` is only valid on `maxOccurs`). The default value 1 is used so class building can continue. |
| `wildcard-invalid` | ERROR | A wildcard's XML representation is illegal: an unknown or misplaced namespace-constraint token (only `##any` or `##other` alone, or a whitespace-separated list of URI references, `##local` and `##targetNamespace`); a `processContents` value other than `skip`/`lax`/`strict`; occurrence attributes on `xs:anyAttribute` (not a particle); or two element wildcards whose namespace constraints overlap in one `sequence`/`choice` (UPA non-determinism — a `sequence` is ambiguous only when the earlier wildcard can repeat or be skipped with everything between the two emptiable). Also reported for the XSD 1.1 attributes: `namespace` and `notNamespace` combined on one wildcard; a `notNamespace` token other than a URI reference, `##local`, or `##targetNamespace`; a `notQName` token that is not a QName with a prefix bound in scope (`xml` counts as declared) or one of the `##defined`/`##definedSibling` keywords, or a `notQName` name in a namespace the wildcard does not admit; and a complex type derived by restriction that widens the base's attribute-wildcard namespace constraint or weakens its `processContents`. The check is skipped, never reported, when the base type or its attribute wildcard cannot be resolved. |
| `all-rule` | ERROR | `xs:all` compositor legality: the `all` itself carries `minOccurs` > 1 or a `maxOccurs` above 1, a contradictory `minOccurs=1 maxOccurs=0`, an emptiable form inside a group definition, an `all` directly inside a `sequence`/`choice` (directly or through a group reference), a `name` attribute, or a particle other than an element declaration, wildcard, or group reference whose content model is an `all`; two element particles with the same expanded name but different type declarations in one compositor (Element Declarations Consistent); or, under an `all`, two particles with the same expanded name, an element in the substitution group of another, or two overlapping wildcards (UPA). Under XSD 1.1 the element and wildcard particles of an `all` may carry relaxed occurrence bounds and the `all` itself may be emptiable, so the XSD 1.0 per-particle and fixed-`maxOccurs` limits are not enforced. |
| `pointless-particle` | ERROR | A `sequence`/`choice` with no particle children that can match zero (an empty compositor) appears inside a group definition referenced with `minOccurs="0"`; such a particle is pointless there and must be eliminated. Compositors with particle children are never reported even when every child is optional or `maxOccurs="0"` — they can still match content, so eliminating them could change the language. An empty compositor with `minOccurs="1"` is unsatisfiable rather than eliminable, compositors outside an optionally-referenced group are left alone, and `xs:all` falls under `all-rule`. |
| `particle-restriction` | ERROR | A complex type derived from a complex base violates a particle-derivation rule (XSD 1.0 §3.9.6 / 1.1 §3.4.6.2; the message names the deciding constraint, such as `cos-ct-extends`, `cos-particle-extend`, or `cos-nonambig`). For **extension**: an `all` suffix may extend only an `all` base, a `sequence`/`choice` suffix may not extend an `all` base, the composed `all` must be unambiguous, and an empty *mixed* all base cannot be extended by an `all`. For **restriction**: forbidden wildcard/element/compositor pairings, name/type/`fixed`/`nillable`/substitution/identity checks on element pairs, wildcard namespace subsumption, occurrence containment, and the mapping of derived members onto base members for each compositor kind. The same code covers the mixed-content variety rule (a mixed restriction needs a mixed base) and the XSD 1.1 open-content derivation rules (mode and wildcard narrowing/widening). The check is skipped, never an error, when the base type is unresolved or either particle tree could not be compiled. |
| `attribute-restriction` | ERROR | Attribute-use derivation on a complex type: a restriction that redeclares a required base attribute use as optional, or that redeclares a base use whose `fixed` value it does not preserve (omitting the use entirely stays valid), or an extension that redeclares a base use with a different `fixed` value (XSD 1.0 *Derivation Valid (Restriction, Complex)* clause 2.1; XSD 1.1 *Derivation Valid (Extension)* clause 1.2). A base `default` may be replaced by a derived `fixed`. The base's effective uses are gathered through its extension chain (a restriction does not inherit); the check is skipped when the base type cannot be resolved or carries an XSD 1.1 `inheritable` use. |
| `upa` | ERROR | Unique Particle Attribution (cos-nonambig) over a complex type's effective content model — the base type's tree composed with an extension's suffix. Two element/wildcard particles with overlapping names are ambiguous when no deterministic separation exists between their positions: a repeated (non-univocal) sequence member followed by an overlapping member or wildcard, a choice or `all` with overlapping alternatives, or a substitution-group member beside its transitive head. Unambiguous shapes stay valid: a univocal duplicate, an element beside a wildcard that admits it (the element declaration takes precedence), choices with disjoint first elements, and a head whose `block` excludes substitution. Wildcards carrying XSD 1.1 `notQName`/`notNamespace` exclusions are skipped. |
| `misplaced-declaration` | ERROR | A declaration sits in a container whose construction-time context cannot hold it: an `xs:attribute` or `xs:attributeGroup` whose containing type has no corresponding declaration table, a compositor (`all`/`choice`/`sequence`) or group reference in a container that cannot accept it, or a non-top-level `xs:notation` or group reference. This legacy placement diagnostic coexists with the table-driven `declaration-child`/`declaration-order` checks. |
| `facet` | ERROR | A constraining facet is not applicable to its base type, or its declared value is not legal for that base (bad lexical form, outside the base's value space, or a digit facet that violates the fixed value on an integer-derived type). Also reported when a restriction *widens* the base's value space: a `maxLength` greater than the base's, a `minLength` or `length` below the base's minimum or above its maximum, or digit facets greater than the base's. |
| `facet-conflict` | ERROR | Two constraining facets in one restriction step cannot hold together: mutually exclusive bounds (`minInclusive`/`minExclusive`, `maxInclusive`/`maxExclusive`), a lower bound above the upper bound, or an empty value space once the base type's own fixed bounds are applied (for example `positiveInteger` with `maxExclusive="1"`, or a list `minLength` below 1). |
| `unexpected-element` | ERROR | Element is not declared in the content model and no wildcard allows it. A wildcard that matches by namespace still refuses an element its XSD 1.1 exclusions bar — `notNamespace`, `notQName`, `##defined`, `##definedSibling` (substitution-group members of a referenced head included) — whatever the wildcard's `processContents` says, and a rejected name never fills a wildcard occurrence. A partially matched model may also report `order` for the declared particles it could not reach. |
| `unexpected-character` | ERROR | A complex-typed element whose content model is element-only has character content other than whitespace (XSD 1.1 §3.4.3.2, Element Locally Valid (Complex Type)). Mixed content and simple content are not checked — their text is legal or is the value, and a mixed content type includes one inherited through an extension with empty explicit content — and neither are elements whose content model could not be compiled. |
| `wildcard-no-declaration` | ERROR | `processContents="strict"` wildcard matched an element/attribute with no global declaration (namespaced mode). |
| `wildcard-namespace` | ERROR | An undeclared instance attribute was not admitted by the governing type's *effective* attribute wildcard — the type's own `xs:anyAttribute` constraints (a local one plus its `xs:attributeGroup` contributions), combined with the base type's effective wildcard by union for an extension and intersection for a restriction, honoring the XSD 1.1 `notNamespace`/`notQName`/`##defined` exclusions (the implicit `xml:*` declarations do not count as resolved globals, so `xml:lang` stays admitted under `##defined`). Only reported when the type has an attribute wildcard at all; a type with none reports the undeclared attribute as `unexpected-attribute` instead. |
| `element-consistent` | ERROR | XSD 1.1 dynamic tighter EDC (Element Locally Valid (Complex Type) clause 5): an element admitted by a `strict` or `lax` wildcard has a governing type definition (an `xsi:type`, or the type of the top-level declaration the wildcard resolves to) that is not the same as, or validly derived from, the type the content model locally declares for the element's expanded name. A skipped item (`processContents="skip"`) has no governing type and is exempt; unresolvable types skip rather than report. |
| `occurrence-min` | ERROR | Fewer occurrences than `minOccurs` allows. |
| `occurrence-max` | ERROR | More occurrences than `maxOccurs` allows. |
| `missing-attribute` | ERROR | A required attribute is absent. |
| `unexpected-attribute` | ERROR | Attribute not declared in the schema and not admitted by any effective attribute wildcard. The check skips the XML Schema instance and XML namespaces and `xmlns*` declarations. In the schema phase, reported for an attribute in the XML Schema namespace on a schema element, an unknown attribute on `xs:notation`, or an attribute other than `id` on `xs:annotation`. |
| `invalid-attribute` | ERROR | Attribute value fails its declared type; or, in the schema phase, an `xs:any`/`xs:anyAttribute` declaration carries an unqualified XML attribute outside its representation's allowed set (a qualified attribute in a non-schema namespace is foreign and legal). |
| `prohibited-attribute` | ERROR | A direct attribute declaration with `use="prohibited"` is present in the instance. The prohibition is not enforced when the type's effective attribute wildcard admits the attribute, and a prohibited use contributed by a referenced `attributeGroup` is not an attribute use of the type at all. |
| `fixed-attribute` | ERROR | Attribute present with a value differing from `fixed`. |
| `unknown-type` | ERROR | Referenced type could not be resolved: a namespaced reference into a namespace no composed schema declares, a reference into the XML Schema namespace that names no built-in, or an unqualified reference that resolves into no namespace when a same-named type is declared in another loaded namespace. An unqualified no-namespace reference with no candidate stays tolerated. |
| `unknown-base` | WARNING | An `xs:extension` base cannot be resolved; compilation continues without that base. |
| `unknown-component` | WARNING | A schema component lookup is missing or ambiguous. The warning is recorded when a report is active. |
| `atomic-required` | ERROR | A `list`'s `itemType` (or inline item type) is not an atomic simple type or a union with no list type anywhere in its transitive membership, or a `union`'s member type is a complex type rather than a simple type. Atomic, list and union members are all legal union members. |
| `invalid-base` | ERROR | A simple-type `restriction` derives from a type that is not a simple type: a complex type, or one of the ur-types (`anyType`, `anySimpleType`, `anyAtomicType`) — the base of a restriction inside an `xs:simpleType` must be a non-ur simple type. Also reported for a `simpleContent` restriction whose base is not a complex type, and, when the base's simple-content primitive is `xs:anySimpleType`, for a restriction that does not supply its own inline `simpleType`. |
| `notation-enumeration-required` | ERROR | `xs:NOTATION` is used directly, which the Schema Component Constraint forbids: a bare `xs:NOTATION` element/attribute type or list `itemType`, or a restriction whose primitive is NOTATION that states no `enumeration` facet. A `union` member NOTATION is deliberately tolerated because the W3C test suite is self-contradictory there. |
| `unknown-notation` | ERROR | An `enumeration` value of a NOTATION restriction does not name a notation declared in the schema. |
| `unknown-namespace-prefix` | ERROR | A prefixed name uses a namespace prefix that is not bound in scope (namespaced mode). |
| `value` | ERROR | Text content failed lexical validation for its type. |
| `default` | ERROR | Element default value is not valid for the element's type. |
| `fixed-element` | ERROR | Element content differs from its `fixed` value. |
| `nil` | ERROR | Any `xsi:nil` attribute on a non-nillable element — the XSD 1.0 §3.3.4 requirement keys on the attribute's *presence*, so even a lexically valid `xsi:nil="false"` is rejected (XSD 1.1 relaxed this to the true value) — or an `xsi:nil` value outside the boolean lexical space (`true`/`false`/`1`/`0`, case-insensitively; surrounding whitespace is tolerated). The value is checked even when an attribute wildcard admits the xsi namespace, because the built-in declaration is typed `xs:boolean`; the declaration's own `nillable` attribute is read as `xs:boolean` too, so `nillable="1"` is nillable while `nillable="0"`/`nillable="false"` are not. |
| `abstract-element` | ERROR | Instance of an abstract element declaration. |
| `abstract-type` | ERROR | Direct instance of an abstract type (allowed only via `xsi:type`). |
| `blocked` | ERROR | Substitution-group member blocked by the head's `block`. |
| `xsi-type` | ERROR | `xsi:type` could not be resolved, is not validly derived from the declared type, or is blocked. |
| `unknown-root` | ERROR | Document root matches no global element declaration. |
| `multiple-roots` | ERROR | More than one global element matches the document root. |
| `identity-key` | ERROR | Key field missing or duplicate key value. |
| `identity-unique` | ERROR | Duplicate value under a `xs:unique` constraint. |
| `identity-keyref` | ERROR | Keyref value has no matching key/unique value. |
| `identity-refer` | ERROR | A keyref's `refer` does not resolve to a key or unique constraint in scope, or is missing or malformed. |
| `xpath-invalid` | ERROR | An identity-constraint `xpath` violates the XSD 1.1 §3.11.6 XPath subset, uses an unbound namespace prefix, or is otherwise unusable; also reported for an `xpathDefaultNamespace` value outside the four legal forms (`##defaultNamespace`, `##targetNamespace`, `##local`, or an absolute URI). |
| `assert-invalid` | ERROR | An `xs:assert`/`xs:assertion` `test` is outside the XSD 1.1 §3.13.1 assertion XPath 2.0 subset (for example `fn:doc`/`fn:collection`, the namespace axis), uses an unbound namespace prefix, is empty, or carries an unusable `xpathDefaultNamespace`. The assertion is not evaluated. |
| `assert-failed` | ERROR | An `xs:assert` on a complex type evaluated to false for the bound element, an `xs:assertion` facet evaluated to false for the simple value (with `$value` bound to the typed value), or the evaluation raised a dynamic error — including an `xs:assertion` test that reads the XPath context, which a simple value does not define. |
| `alternative-invalid` | ERROR | An `xs:alternative` declaration is unusable: it carries none or more than one of a `type` attribute / inline type; its `test` is outside the XSD 1.1 §3.12.6 conditional-type-assignment XPath subset, empty or statically invalid; a non-final alternative omits `test`; its type reference cannot be resolved; or its type is not validly derived from the element's declared type (`xs:error` is always admissible). |
| `open-content-invalid` | ERROR | An `xs:openContent` declaration violates its XML representation (XSD 1.1 §3.4.2.2/§3.4.3): the `mode` is not `none`/`interleave`/`suffix`; `mode="none"` carries an `xs:any` child, or a non-`none` mode has other than exactly one `xs:any` child; or one complex type carries more than one `xs:openContent`. Also reported for an `xs:defaultOpenContent` whose `mode` is outside `interleave`/`suffix` or that lacks exactly one `xs:any` child; the default is scoped to the schema document a complex type is declared in. An illegal child wildcard is reported through the shared wildcard codes (`wildcard-invalid`/`invalid-attribute`), and occurrence attributes on it are `wildcard-invalid`, because the child is a wildcard component and not a particle. |
| `xsd11-construct` | ERROR | An XSD 1.1-only element, attribute, or built-in type appears while compiling in XSD 1.0 mode. |
| `versioning-invalid` | ERROR | A `vc:*` conditional-inclusion attribute carries an illegal value (XSD 1.1 §4.2.2): a `vc:minVersion`/`vc:maxVersion` that is not a valid `xs:decimal`, or a `vc:typeAvailable`/`vc:typeUnavailable`/`vc:facetAvailable`/`vc:facetUnavailable` item that is not a QName or uses a prefix not bound in scope. The selector is ignored so the rest of the document is still processed. An unrecognized attribute in the versioning namespace, or one misspelled outside the six selectors, is ignored rather than reported. |
| `alternative-error` | ERROR | The type alternative conditionally assigned to an instance element is `xs:error`, whose value space is empty, so no content can make the element valid (XSD 1.1 §3.3.4.1). `xsi:type` takes precedence and suppresses the selection. |
| `id-duplicate` | ERROR | Two attribute or element values of type `xs:ID` in one document are equal (XML ID uniqueness). |
| `idref-unresolved` | ERROR | A value of type `xs:IDREF`/`xs:IDREFS` names no `xs:ID` value in the document (DTD-style reference resolution). |
| `dtd-external-skipped` | WARNING | An ENTITY/ENTITIES name could not be checked against complete declarations because an external DTD subset was not retrieved. Emitted at most once per document when such a check is skipped; lexical checks still apply. |
| `identity-unsupported` | WARNING | Identity-constraint XPath uses an unsupported construct. |
| `union-member-skipped` | WARNING | An inline union member could not be built and was skipped; other members remain available. |
| `unknown-group` | ERROR | Referenced `xs:group` missing. |
| `circular-derivation` | ERROR | A type derives from itself directly or through a derivation cycle; the cyclic base is skipped. |
| `conflicting-derivation` | ERROR | A simple type combines `xs:list` with restriction or extension, which cannot form one value representation. |
| `circular-group` | ERROR | Group reference cycle. |
| `unknown-attributeRef` | ERROR | An attribute `ref` does not resolve to a global attribute declaration. |
| `unknown-attributeGroup` | ERROR | Referenced `xs:attributeGroup` missing. Also reported for a schema's `defaultAttributes` whose QName does not resolve to a global attribute group (XSD 1.1 §3.1.2). |
| `duplicate-attribute` | ERROR | An attribute use is contributed twice to one complex type with the same expanded name: the schema document's `defaultAttributes` group colliding with an attribute declared on the type or pulled in by a referenced attribute group, a local declaration colliding with an `attributeGroup` contribution, two referenced groups colliding, or two uses resolving to the same global attribute through a `ref` (XSD 1.1 §3.1.2/§3.4.2.4). Two uses with one local name but different namespaces or forms are distinct. |
| `unknown-substitution-head` | ERROR | Substitution group references a missing head. |
| `substitution-type` | ERROR | A substitution-group member's declared type is not validly derived from its head's type (XSD 1.1 §3.3.5.2). Reported for a member with more than one head and for the clear ur-type category mismatches (a complex member under an `xs:anySimpleType` head and the reverse; an `xs:anyType` head admits every type). Other single-head mismatches stay permitted. |
| `circular-substitution-group` | ERROR | Substitution-group membership is cyclic (`foo` heads `bar` heads `foo`), so no well-founded head exists. |
| `unknown-elementRef` | ERROR | Element `ref` points to a missing global element. |
| `final` | ERROR | Derivation violates the base type's effective `final` (its own `final` attribute, or the declaring schema's `finalDefault` when it states none): a `restriction`/`extension` step the value excludes, or a list item type / union member type whose `final` contains `list` / `union` respectively. |
| `schema` | ERROR | The schema file itself is malformed or unreadable. |
| `schema-hint` | WARNING | Malformed schemaLocation hint in the instance document. |
| `schema-compose` | ERROR / WARNING | A composition directive is invalid or its referenced schema cannot be used. ERROR for a missing `schemaLocation` attribute, a malformed referenced schema, a repeated `annotation` on `xs:include`/`xs:import`, or an `xs:redefine`/`xs:override` whose base schema cannot be opened. WARNING when an `xs:include`/`xs:import`/`xs:redefine`/`xs:override` `schemaLocation` names a document that does not exist (an unresolvable location is a hint, not a rule violation). |
| `import-unresolved` | ERROR / WARNING | An `xs:import` could not be satisfied from a `schemaLocation` or `namespace_schemas` (namespaced mode). WARNING for a namespace-only import that is merely a hint; ERROR when a component from that namespace is referenced by the importing document. An instance- or caller-supplied schema that is missing remains an ERROR. |
| `import-required` | ERROR | An XSD-language component reference names a foreign namespace that the referencing schema document did not directly import. The audit honors the document's effective target namespace and built-in vocabularies; transitive imports and caller- or hint-supplied schemas do not satisfy its authored import duty. Wildcard `notQName` is intentionally not audited; annotation payloads, foreign markup, QName-valued defaults/fixed values, and XPath expressions are also outside this check. |
| `compose-cycle` | WARNING | A repeated include/redefine was deduplicated. |
| `compose-namespace` | ERROR | Include target namespace mismatch. |
| `compose-invalid` | ERROR | A composition rule violation beyond a missing resource: an `xs:import` whose `namespace` does not match the imported document's target namespace; an `xs:redefine` of a component the base document does not define, or in a namespace other than the redefining schema's; the same component of one base document redefined twice; an unqualified self reference in a chameleon redefine; a cyclic `xs:redefine` chain (XSD 1.1 §4.2.4); an `xs:override` whose base document's target namespace differs from the overriding schema's; and an attributeGroup redefine that is not a valid restriction (adds or reorders attributes, drops a `fixed` value, changes a non-optional `use`, or duplicates an attribute pulled in by its self reference). |
| `override-invalid` | ERROR | An `xs:override` composition violates a rule specific to override: a child outside the §4.2.5 grammar, a child that does not name a component, the same component named twice in one override block, or the same component of one base document overridden by two unrelated blocks. A declaration matching nothing in the override's target set is not an error: it is ignored (XSD 1.1 §4.2.5). |
| `extension-validation` | ERROR | A mixin's `__pyxsd_validate__` hook returned a plain string, recorded as an extension finding on top of the XSD report. See {doc}`extensions`. |
| `extension-error` | ERROR | A mixin's `__pyxsd_validate__` hook raised, or returned a finding other than a bare `str` or a `ValidationIssue`. One issue naming the failure is recorded and binding continues. See {doc}`extensions`. |
| `internal` | WARNING | Parser internal inconsistency — please report. |

## Strict mode

`pyxsd --strict` exits with status 1 when the report contains any
`ERROR`-severity issue. Warnings never affect the exit code.

## Reports and transformed documents

A `Document`'s report describes the tree as it was bound, not subsequent
mutations. `Document.transform()` may return a bound root, `None` after an
in-place mutation, or an arbitrary result. When it returns a `Document`,
that document shares the historical report; `require_valid()` therefore
checks the old report rather than proving that the changed tree is valid.
Call `revalidate()` to serialize and parse the tree again, and check the
fresh document's report. An unreadable or malformed serialized result still
raises `PyXSDError`; it does not become an ordinary validation finding.
The declaration-dependent `ENTITY`/`ENTITIES` limitation above also applies:
revalidation currently loses the original DTD evidence.

## Structured and bounded reports

`report.to_dict()` and `report.to_json()` expose issue severity, code, message,
phase and optional location/constraint details. Decimal evidence is encoded as
strings; integers outside JavaScript's exact range are strings too. Nonfinite
floating-point evidence uses the canonical XSD strings `NaN`, `INF`, and `-INF`.
Unsupported detail objects raise `TypeError` instead of silently becoming arbitrary text.

`ValidationReport(max_issues=N)` retains at most the first `N` issue details.
The default is unlimited. `error_count`, `warning_count` and `has_errors` include
suppressed findings, and `suppressed_count` reports how many details are absent.
`issues`, `errors`, `warnings`, iteration and `for_phase()` expose only retained
details. A report can therefore be truthy with zero retained details; `len(report)`
still counts retained details. Merging a report preserves its aggregate counts
even when the receiving report has a different detail budget.

`ValidationReport(max_message_bytes=N)` visibly truncates retained messages to at
most `N` UTF-8 bytes without splitting a character. Truncation does not change
issue counts or suppressed-detail accounting. The default is unlimited. Resource
policies may optionally provide `new_report()` to apply these budgets consistently
to schema, instance, and merged reports; policies without it retain core defaults.

Location and constraint fields are optional: an absent location does not imply
that an issue refers to the first element with that name. Facet violations expose
typed constraint evidence at their source. Existing issue formatting and
positional constructors remain unchanged.

### Finding the offending occurrence

Instance diagnostics carry an `IssueLocation` when the parser can identify the
source occurrence. Its `path` is a tuple of `PathStep(name, index)` records:
expanded XML names and one-based sibling indexes among elements with that exact
name. Namespace URIs can contain slashes; do not split expanded names on `/`.
Attribute findings can additionally name an expanded `attribute`.

`issue.location_precision` distinguishes `exact`, `container`, `component` and
`unknown` evidence. A missing required child points to its existing container,
not an invented child. Schema findings and diagnostics outside parsing may have
unknown locations. No source line or column is fabricated.

`document.locate(issue.location)` returns a `NodeLookup` with a `status`:

- `bound`: `bound` is the original bound object for that occurrence.
- `unbound`: the source element existed, but binding discarded it.
- `attribute`: `bound` is the original lexical attribute value.
- `missing`: the address or attribute did not exist in this parse.

Guard against `issue.location is None` before calling `locate`. These are
parse-specific addresses, not durable identifiers: modifying the tree does not
update them. `revalidate()` returns a freshly parsed Document with fresh
locations. Another parse on the same Schema does not retarget the first
Document's lookups. A manually constructed or transformed Document has no
provenance until freshly parsed. Duplicate identity findings can include
`details["related_locations"]` for the earlier occurrences.

## Known limitations (schema legality)

A few schema-phase checks are intentionally shallow, so some invalid
schemas are accepted:

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
- **One known false reject.** The substitution-derivation check rejects one
  valid schema shape; the fix would regress a cyclic `xs:redefine` case whose
  expected verdicts the W3C suite itself contradicts.
- **Composition residuals.** Direct authored imports are audited for
  cross-namespace component references (`import-required`), but component
  lookup still uses the global composed schema. This does not fix
  cross-document duplicate globals or redefine/override merge and restriction
  gaps. Import-target namespace mismatch (`schF6`) and no-namespace type
  binding (`schF2.v`, `schG2.v`) remain known failures. See {doc}`xsts` for
  the generated residual census.
- **XSD 1.1 built-in types.** Support remains incomplete; schemas using
  unsupported types do not receive full validation.
- **Caller-supplied schemas.** A missing caller-supplied resource
  (`namespace_schemas`, or an instance `xsi:schemaLocation`) is still an
  `ERROR`, unlike a schema's own missing include/import/redefine, which warns.
