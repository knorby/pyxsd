# API reference

```{eval-rst}
.. meta::
   :description: Look up pyxsd Schema and Document APIs, XML queries, validation errors, sequential batches, stubs, and schema introspection.
```

Use {doc}`quickstart` for a complete first example. This page documents public
entry points; {doc}`data-model` describes node storage and export behavior.

## Schema and documents

`Schema.compile(source, ...)` generates binding classes once;
`schema.parse(source, ...)` returns a `Document` for each instance.
The module-level `pyxsd.parse` resolves a supplied schema or instance hints,
compiles, and binds in one call. Call `require_valid()` when error-severity
findings should stop processing; returning a schema or document does not
itself establish validity.

Parser protections are opt-in and apply per call. Compile-time `xml_policy`
and `xml_limits` apply to the schema closure, not later instance parses.
A custom `resource_policy` instead owns reading/parsing and is retained by
the schema. See {doc}`xml-security`.

Common schema entry points; full signatures and contracts below.

```{eval-rst}
.. autosummary::
   :signatures: none

   ~pyxsd.schema.Schema.compile
   ~pyxsd.schema.Schema.parse
   ~pyxsd.schema.Schema.iter_parse
   ~pyxsd.schema.Schema.stub
   ~pyxsd.schema.Schema.require_valid

```

```{eval-rst}
.. autoclass:: pyxsd.schema.Schema
   :members:

.. autoclass:: pyxsd.document.Document
   :members:

.. autofunction:: pyxsd.schema.compile

.. autofunction:: pyxsd.schema.parse
```

## Querying a document

Common document entry points; full signatures and contracts are in the
`Document` reference above.

```{eval-rst}
.. autosummary::
   :signatures: none

   ~pyxsd.document.Document.find
   ~pyxsd.document.Document.findall
   ~pyxsd.document.Document.xpath
   ~pyxsd.document.Document.to_dict
   ~pyxsd.document.Document.to_json
   ~pyxsd.document.Document.to_string
   ~pyxsd.document.Document.transform
   ~pyxsd.document.Document.revalidate
   ~pyxsd.document.Document.write
   ~pyxsd.document.Document.require_valid

```

`find(path, *, namespaces=None)` returns a bound node or `None`;
`findall(...)` returns a list. Both use ElementTree paths relative to the
document root. `xpath(expr, *, namespaces=None)` returns original bound
nodes for node results and passes scalar results through. Absolute XPath
paths include the root name. See {doc}`data-model` for whitespace, namespace,
and source-fidelity limits, and {doc}`quickstart` for runnable queries.

## Modes and the node protocol

See {doc}`binding` for `BindingPolicy` and `ParseModes`. Some complex child
descriptors read as `None` after parsing even when the node exists in
`_children_`; use the query methods to retrieve it.

```{eval-rst}
.. autoclass:: pyxsd.binding.BindingPolicy
   :members:

.. autoclass:: pyxsd.binding.ParseModes

.. py:attribute:: pyxsd.binding.ParseModes.STRICT

   Drop invalid values; retain legacy local-name binding.

.. py:attribute:: pyxsd.binding.ParseModes.LAX

   Retain best-effort values without suppressing findings.

.. py:attribute:: pyxsd.binding.ParseModes.NAMESPACED

   Match expanded XML names with strict value retention.

.. py:class:: pyxsd.schema_base.SchemaBase

   Base of generated complex binding classes. See the data-model guide.

.. autoclass:: pyxsd.nodes.XMLNode
   :members:
```

## Errors

`PyXSDError` signals fatal input problems, such as unreadable or malformed
files. `ValidationError` is raised by `require_valid()` and carries the failing
report. See {doc}`validation` for report classes, issue codes, and locations.

```{eval-rst}
.. autoclass:: pyxsd.exceptions.PyXSDError
   :undoc-members:

.. autoclass:: pyxsd.exceptions.ValidationError
   :members:

.. autoclass:: pyxsd.exceptions.PyXSDWarning
   :undoc-members:
```

## Batch parsing

See {doc}`batch` for stop/report policies, sequential reuse, and resource limits.

```{eval-rst}
.. autoclass:: pyxsd.batch.DocumentSource
   :members:

.. autoclass:: pyxsd.batch.ParseOutcome
   :members:

.. autoclass:: pyxsd.batch.InputFailure
   :members:

.. autoclass:: pyxsd.batch.BatchParseError
   :members:

.. autofunction:: pyxsd.batch.iter_parse
```

### Optional Parquet datasets

See {doc}`integrations` for the optional adapter entry points and
{doc}`batch` for dataset publication and failure behavior.

```{eval-rst}
.. autoclass:: pyxsd.integrations.dataset.DatasetResult
   :members:

.. autoclass:: pyxsd.integrations.dataset.DatasetExportError
   :members:
```

## Type stubs for generated classes

`Schema.stub(*, compact=False, component_ids=None, depth=None) -> str`
renders the live class table. It does not call `require_valid()`.
See {doc}`stubs` for consumption, annotations, filtering, and limitations.

```{eval-rst}
.. autofunction:: pyxsd.stubs.emit_stub
```

### Native schema exploration and filtered views

`pyxsd.introspection` describes declarations without optional dependencies:

```{eval-rst}
.. autofunction:: pyxsd.introspection.describe_schema

.. autofunction:: pyxsd.introspection.find_components

.. autofunction:: pyxsd.introspection.describe_component

.. autofunction:: pyxsd.introspection.related_components
```

Global IDs combine kind and expanded name. Local/anonymous IDs use declaration
addresses rather than Python object identities. Duplicate globals retain all
occurrences, with `#2`, `#3`, etc. on later IDs. Unknown IDs raise `KeyError`.
Descriptions are cached frozen snapshots; recompile after schema or overlay
changes. Origins refer to the original schema before composition.

Descriptions include facets, assertions, derivation, substitution/identity
constraints, accessors, and particle trees. Member occurrence summaries are
conservative; exact choice/group relationships are in `particle`. Attribute-use
bounds are exact. Pattern facets preserve their AND-of-OR restriction groups.
These descriptions cover shapes the optional data adapters cannot project.

(the-two-modes)=
(truthfulness-contract)=
(the-overlay-extension-loop)=
(example-full-mode)=
(example-compact-mode)=
The stub tutorial previously on this page is in {doc}`stubs`.

## Mixin extensions

See {doc}`extensions` for selectors, precedence, and validation hooks.

```{eval-rst}
.. autoclass:: pyxsd.extensions.Extensions
   :members:
```

## Parser and resource policies

See {doc}`xml-security` for profile choices, defaults, remote authorization,
and the `ResourcePolicy` protocol. A policy replaces parsing and cannot be
combined with `xml_policy` or `xml_limits`.

```{eval-rst}
.. autoclass:: pyxsd.xml_policy.XmlParserPolicy
   :members:

.. autoclass:: pyxsd.xml_policy.XmlLimits
   :members:

.. autoclass:: pyxsd.remote.RemoteResourcePolicy
   :members:

.. autoclass:: pyxsd.remote.RemoteLimits
   :members:

.. autoclass:: pyxsd.remote.RemoteTransport
   :members:

.. autoclass:: pyxsd.remote.HttpClientTransport
   :members:

.. autoclass:: pyxsd.resource_policy.ResourcePolicy
   :members:

.. autoclass:: pyxsd.resource_policy.ResourcePolicyError
   :undoc-members:
```

## Source spans

`SourceIndex` indexes exact original-byte ranges without building another
tree. It underlies MCP source-subtree queries; `Document` does not expose
spans directly. The whole input is parsed and policy-checked before returning
a span. Paths use expanded names and same-name sibling indexes, as issue
locations do.

```{eval-rst}
.. autoclass:: pyxsd.source_spans.SourceIndex
   :members:

.. autoclass:: pyxsd.source_spans.SourceSpan
   :members:

.. autoclass:: pyxsd.locations.PathStep

.. autoclass:: pyxsd.locations.IssueLocation

.. autoclass:: pyxsd.locations.ConstraintInfo

.. autoclass:: pyxsd.locations.NodeLookup
```

## Transforms

See {doc}`transforms/using` for return and revalidation behavior and
{doc}`transforms/class` for `Transform`, `Displayer`, `iter_tree`, and built-ins.

### Supporting records and entry-point aliases

These records describe introspection and resource-policy results. The aliases
below point to the same implementations documented above.

```{eval-rst}
.. autoclass:: pyxsd.introspection.SchemaDescription

.. autoclass:: pyxsd.introspection.ComponentDescription

.. autoclass:: pyxsd.introspection.MemberDescription

.. autoclass:: pyxsd.introspection.ParticleDescription

.. autoclass:: pyxsd.introspection.FacetDescription

.. autoclass:: pyxsd.introspection.WildcardDescription

.. autoclass:: pyxsd.introspection.IdentityDescription

.. autoclass:: pyxsd.resource_policy.ResourceData

.. autoclass:: pyxsd.remote.RemoteResponse

.. autoclass:: pyxsd.remote.RemoteTransportError

.. autofunction:: pyxsd.schema.load_overlay_classes

.. autofunction:: pyxsd.dict_export.bound_to_dict

.. py:module:: pyxsd.introspection

.. py:module:: pyxsd.version_gates

.. py:currentmodule:: None

.. py:function:: pyxsd.compile
   :canonical: pyxsd.schema.compile

.. py:function:: pyxsd.parse
   :canonical: pyxsd.schema.parse

.. py:class:: pyxsd.ValidationError
   :canonical: pyxsd.exceptions.ValidationError

.. py:class:: Document
   :canonical: pyxsd.document.Document

   Alias for :class:`pyxsd.document.Document`.

.. py:class:: Schema
   :canonical: pyxsd.schema.Schema

   Alias for :class:`pyxsd.schema.Schema`.

.. py:method:: to_dict

   See :meth:`pyxsd.document.Document.to_dict` for dictionary export.

.. py:method:: XmlParserPolicy.strict
   :canonical: pyxsd.xml_policy.XmlParserPolicy.strict

.. py:method:: pyxsd.transforms.Transform.makeElemObj
   :canonical: pyxsd.transforms.transform.Transform.makeElemObj
```
