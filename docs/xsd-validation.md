# XML and XSD validation

“Is this XML valid?” needs a qualification: valid against which rules, using
which schema and language version? XML parsing, DTD validation, XSD assessment,
and Python object binding answer different questions. This guide explains those
distinctions and what a test-suite validity result tells you.

## Well-formed XML is the starting point

An XML parser checks the document's syntax: legal characters, properly nested
tags, quoted attribute values, entity-reference rules, and a single document
element. These are **well-formedness** requirements, defined by
[XML 1.0 §2.1][xml-well-formed]. A schema cannot make malformed XML well-formed.

For example, `<quantity>many</quantity>` is well-formed XML. It is invalid
against an XSD declaration requiring `quantity` to contain an `xs:integer`.
`<quantity>3</count>` is not well-formed, so instance assessment cannot proceed
normally at all.

**DTD validation** checks another set of rules. A document type definition
(DTD) declares element content models, attributes, entities, and notations.
The XML specification uses “valid” for a document that has a document type
declaration and satisfies the associated DTD's constraints
([XML 1.0 §2.8][xml-dtd]). Merely accepting a `DOCTYPE` does not mean a parser
has performed DTD validation; XML defines both
[validating and non-validating processors][xml-processors].

**XSD assessment** checks an XML information set against XML Schema Definition
Language rules. XSD supplies namespace-aware declarations, types, datatype
facets, content models, and identity constraints. A document can be
XSD-valid without having a DTD. If it has both a DTD and an XSD, satisfying one
does not imply satisfying the other. pyxsd is an XSD processor, not a DTD
validator.

## DTDs can affect the information XSD sees

XSD assessment works on information supplied by XML parsing, rather than on the
original character sequence alone. The [XML Information Set][infoset] models
that information as items and properties: elements, attributes, character
data, namespace names, and declaration information. It is an abstract model,
not a requirement to use a particular tree API.

Processing DTD declarations can affect this input even without DTD validation:

- An internal parsed entity's replacement text can contribute character data
  or markup.
- An `ATTLIST` declaration can supply an omitted attribute's default value.
- A declared attribute type can change attribute-value normalization before
  XSD checks the resulting value.

These effects follow XML's rules for [attribute defaults][xml-defaults],
[attribute-value normalization][xml-normalization], and
[entity processing][xml-entities]. They are distinct from XSD defaults and
datatype whitespace processing, which apply during schema assessment.

There is also a direct DTD dependency in the XSD datatypes
[`xs:ENTITY`][xsd11-entity] and [`xs:ENTITIES`][xsd11-entities]. An `ENTITY`
value must name an **unparsed entity declared in the instance document's DTD**;
`ENTITIES` is a nonempty list of such values. Checking only that each token is
an `NCName` checks its lexical form, not this declaration-dependent requirement.
An unparsed entity, declared with `NDATA`, is not a parsed entity reference to
expand into XML text. Naming it does not require fetching its resource.

pyxsd's default ElementTree/Expat backend expands internal parsed entities
and can apply `ATTLIST` defaults. When the compiled schema can validate
`ENTITY`/`ENTITIES`, pyxsd also lazily collects unparsed-entity declarations
from the prolog/internal subset. Complete evidence lets it reject undeclared
names, including values supplied by XSD defaults or checked through derived
types. An absent DOCTYPE supplies an empty, complete declaration set.
Unrelated schemas skip this declaration-collection parse.

No built-in profile retrieves external DTD subsets or entities. An external
subset makes declaration evidence incomplete, so a missing name remains
lexical-only with one advisory per document when a check needs the missing
evidence. Caller-parsed and custom-policy trees lack core declaration
evidence. Non-seekable top-level schema-discovery streams also remain
lexical-only; compiled `Schema.parse()` can collect and replay their prolog.
These are implementation limits, not alternative validity rules or security
protections. See {doc}`validation` for source-provenance and reread limits.

Parser policy is a separate decision from validity. The protected profile
rejects entity declarations and external identifiers; the strict profile
rejects every `DOCTYPE`. Those refusals are not DTD-validation modes or
evidence that the refused document violates its XSD. Internal subsets remain
untrusted input when permitted. See {doc}`xml-security` for the exact policies,
limits, and Expat requirements; XSD validation is not a security boundary.

## Schema compilation and instance assessment

An XSD schema document is itself XML, but well-formed XML is not necessarily a
legal schema. XSD defines constraints both on the XML representation of schema
components and on the components themselves. A schema can also draw components
from several documents through inclusion and import. The Structures
specifications define the [component model][xsd11-structures] and
[schema composition][xsd11-composition].

It helps to separate two stages:

1. **Schema compilation:** read the schema documents, resolve and compose
   declarations and types, and check schema legality. A type-derivation cycle
   or an illegal content model is a schema problem, even before any instance
   is supplied. Checking a schema document against the schema for schema
   documents alone does not establish all component constraints.
2. **Instance assessment:** use the resulting schema components to assess
   elements and attributes. Missing required children, invalid datatype values,
   and broken key references are instance problems. A legal schema does not
   imply that any particular instance satisfies it.

In pyxsd, `Schema.compile()` also constructs Python binding classes and records
recoverable schema findings in `Schema.report`. `Schema.parse()` assesses and
binds an instance; its `Document.report` merges schema findings with instance
findings. Compilation returning a `Schema`, or parsing returning a `Document`,
does not by itself establish validity. Check the reports or use
`require_valid()`. Unreadable or malformed input can instead raise
`PyXSDError` when processing cannot proceed. The reporting contract and its
known limitations are documented in {doc}`validation`.

In particular, loading a component and authorizing a reference to its
namespace are separate concerns. pyxsd audits XSD-language component QNames
against each referencing document's direct authored imports, effective
target namespace, and built-in vocabularies. A component supplied transitively,
by a caller mapping, or by an instance hint does not excuse a missing
authored import (`import-required`). This audit leaves global component
lookup in place; it does not establish complete composition conformance.

Binding determines which Python objects and values an application receives.
An invalid document can still yield a best-effort bound tree and a report
containing errors. Strict versus lax **binding** choices control retention of
invalid or unrecognized content, not permission to ignore its findings.
Namespace handling is a separate policy field; the XSTS harness uses
`ParseModes.NAMESPACED`. See {doc}`binding`. These binding choices are not the
XSD wildcard `processContents="strict"`, `"lax"`, and `"skip"` rules, which
control assessment of wildcard-matched content.

## XML versions and XSD versions are independent

Two different languages have versions called “1.0” and “1.1”:

| Version choice | What it governs | Where it is selected |
| --- | --- | --- |
| XML 1.0 or XML 1.1 | XML syntax, character rules, and parsing behavior | The document's XML declaration, such as `<?xml version="1.0"?>`, and the XML parser's capabilities |
| XSD 1.0 or XSD 1.1 | Schema vocabulary, component constraints, datatypes, and assessment rules | The schema processor's version mode |

XML 1.1 changes rules such as permitted control-character references and
end-of-line normalization; see [XML 1.1 §1.3][xml11-changes]. XML 1.0's fifth
edition also broadened name characters, so “newer name characters” alone is not
a reliable way to identify an XML 1.1 document.

An XSD 1.1 schema can be written as an XML 1.0 document and assess XML 1.0
instances. Changing the XML declaration to `version="1.1"` does not enable
XSD 1.1 assertions. Conversely, selecting XSD 1.1 does not give the underlying
XML parser XML 1.1 support. XSD 1.1 explicitly addresses its dependencies on
XML and Namespaces versions; see [Structures, changes to related
specifications][xsd11-xml-dependencies].

pyxsd defaults to XSD 1.1 and offers `xsd_version="1.0"` on `Schema.compile()`
and `--xsd-version 1.0` on the CLI. Its ElementTree/Expat XML backend does not
implement XML 1.1 parsing semantics. That lexer limitation is independent of
pyxsd's XSD version mode. Neither an `xs:` prefix nor the shared
`http://www.w3.org/2001/XMLSchema` namespace distinguishes XSD 1.0 from 1.1.
See {doc}`supported` for version-mode behavior and remaining differences.

### What changes between XSD 1.0 and 1.1?

XSD 1.1 adds vocabulary and changes some rules for existing constructs. The
following examples explain why the same schema or instance can have different
expected verdicts under the two versions; they are not a pyxsd support matrix.

- **Relationships between values:** XSD 1.1 introduces complex-type
  [`xs:assert`][xsd11-assertions] and simple-type
  [`xs:assertion` facets][xsd11-assertion-facet], using XPath expressions to
  express constraints beyond individual datatype bounds. Conditional type
  assignment with [`xs:alternative`][xsd11-alternatives] selects a governing
  type according to conditions, rather than requiring the instance author to
  supply `xsi:type`.
- **Extensible content models:** XSD 1.1 adds open content and richer wildcard
  exclusions, and relaxes restrictions on `xs:all` members. It also changes
  Unique Particle Attribution: competition between an element particle and a
  wildcard particle is allowed, with the element taking precedence. Competition
  between two element particles or two wildcard particles remains constrained.
  See [Structures, content-model changes][xsd11-model-changes].
- **Schema evolution and composition:** XSD 1.1 adds conditional inclusion
  through `vc:*` attributes and an `xs:override` composition mechanism.
  Conditional inclusion chooses which declarations to process based on version
  or capabilities; it is not the same as instance-time conditional type
  assignment. See [Structures §4.2][xsd11-composition].
- **Datatypes and value semantics:** XSD 1.1 adds types such as
  `xs:dateTimeStamp`, which requires an explicit timezone, and the
  `xs:yearMonthDuration` and `xs:dayTimeDuration` duration subtypes. It also
  changes existing lexical spaces: `+INF` is added for `float` and `double`,
  and year `0000` becomes permitted in the affected date/time types. See
  [Datatypes, changes since 1.0][xsd11-datatype-changes].

Compatibility was a design goal, not a guarantee of identical verdicts for
every edge case. Version-specific expected results can therefore be
intentional. An implementation's failure to match its selected version is
still a failure, not something corrected by taking the better result from a
different version.

## Reports, PSVI, and a Boolean verdict

XSD's **post-schema-validation infoset** (PSVI) is the input infoset augmented
with assessment information, such as governing declarations and types, typed
values, and validity properties. Assessment is broader than a single yes/no
answer. In particular, the specification's `[validity]` property can be
`valid`, `invalid`, or `notKnown`; not every information item is necessarily
strictly assessed. See [XSD 1.0 Structures §2.1][xsd10-psvi] and
[XSD 1.1 Structures §2.1][xsd11-psvi].

A pyxsd `ValidationReport` is an application-facing collection of findings with
severities, codes, phases, and optional location or constraint evidence. Its
`has_errors` property answers whether the run recorded error-severity findings.
It is not a complete PSVI, and the bound Python tree is not a promise to expose
every PSVI property or preserve all source information. A report with no errors
is evidence from the checks performed under that run's settings and known
implementation limits, not proof that every possible XSD constraint was checked.

The XSTS adapter reduces the relevant phase's findings to a top-level validity
verdict for comparison with the test metadata. That comparison does not check
whether binding retained a particular invalid value, whether every descendant
has the correct PSVI properties, or whether a diagnostic identifies the right
source occurrence. Those require separate tests.

## What XSTS establishes

The W3C XML Schema Test Suite (XSTS) contains catalogued schema and instance
cases with expected outcomes and version information. pyxsd pins the upstream
corpus revision. Its [README, “Running the tests”][xsts-readme] explicitly
limits the suite to top-level `valid`/`invalid` outcomes, not correct PSVI
generation or conformity of error reporting.

For cases with a checkable Boolean expectation, an agreement result means the
adapter's verdict matches that expectation for the selected profile. Schema
cases test schema acceptance or rejection; instance cases test assessment using
the supplied schema context. Non-Boolean expectations are not forced into
yes/no answers. A timeout, adapter limitation, or unexpected exception is not
itself a schema-validity verdict.

Agreement is useful regression and interoperability evidence, but it does not
establish:

- W3C certification or complete coverage of XSD features and their interactions;
- correct or complete PSVI output, diagnostic quality, or API completeness;
- performance, memory use, security, or suitability for untrusted input;
- correctness on untested documents, or a universal ranking of schema engines.

The selected corpus, version profile, adapter behavior, and excluded or
uncheckable cases define the measurement's scope. Disagreements need analysis
against the specifications and test metadata; another implementation's answer
is useful evidence, not normative authority.

Continue with {doc}`xsts` for case selection, outcome definitions, baseline
provenance, current results, and residual failures. For engine-selection
tradeoffs, see {doc}`comparison-xmlschema` and {doc}`comparison-libxml2`.

## Primary specifications

The language definitions are the W3C Recommendations:

- [XML 1.0, fifth edition][xml-well-formed] and [XML 1.1, second edition][xml11-changes].
- [XML Information Set, second edition][infoset].
- XSD 1.0, second edition: [Structures][xsd10-structures] and [Datatypes][xsd10-datatypes].
- XSD 1.1: [Structures][xsd11-structures] and [Datatypes][xsd11-datatypes].

The XSD 1.1 change summaries linked above are non-normative reading aids within
the specifications; the component and validation rules define the requirements.
The pinned [XSTS README][xsts-readme] defines the upstream suite's stated scope.
For pyxsd's public contracts, continue with {doc}`validation`, {doc}`binding`,
and {doc}`xml-security`.

[xml-well-formed]: https://www.w3.org/TR/xml/#sec-well-formed
[xml-dtd]: https://www.w3.org/TR/xml/#sec-prolog-dtd
[xml-processors]: https://www.w3.org/TR/xml/#proc-types
[xml-defaults]: https://www.w3.org/TR/xml/#sec-attr-defaults
[xml-normalization]: https://www.w3.org/TR/xml/#AVNormalize
[xml-entities]: https://www.w3.org/TR/xml/#entproc
[xml11-changes]: https://www.w3.org/TR/xml11/#sec-xml11
[infoset]: https://www.w3.org/TR/xml-infoset/
[xsd10-structures]: https://www.w3.org/TR/xmlschema-1/
[xsd10-datatypes]: https://www.w3.org/TR/xmlschema-2/
[xsd10-psvi]: https://www.w3.org/TR/xmlschema-1/#key-psvi
[xsd11-structures]: https://www.w3.org/TR/xmlschema11-1/
[xsd11-datatypes]: https://www.w3.org/TR/xmlschema11-2/
[xsd11-entity]: https://www.w3.org/TR/xmlschema11-2/#ENTITY
[xsd11-entities]: https://www.w3.org/TR/xmlschema11-2/#ENTITIES
[xsd11-composition]: https://www.w3.org/TR/xmlschema11-1/#composition
[xsd11-xml-dependencies]: https://www.w3.org/TR/xmlschema11-1/#ch_specs
[xsd11-assertions]: https://www.w3.org/TR/xmlschema11-1/#cAssertions
[xsd11-assertion-facet]: https://www.w3.org/TR/xmlschema11-2/#rf-assertions
[xsd11-alternatives]: https://www.w3.org/TR/xmlschema11-1/#cTypeAlternative
[xsd11-model-changes]: https://www.w3.org/TR/xmlschema11-1/#ch_models
[xsd11-datatype-changes]: https://www.w3.org/TR/xmlschema11-2/#changes
[xsd11-psvi]: https://www.w3.org/TR/xmlschema11-1/#key-psvi
[xsts-readme]: https://github.com/w3c/xsdtests/blob/7bc3365c652a322f3d762021b3879eb92dae7e30/README.md#running-the-tests
