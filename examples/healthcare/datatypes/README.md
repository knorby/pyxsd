# CDA datatype tour

From the repository root, fetch sources once, then run:

```sh
uv run python examples/healthcare/download_sources.py
uv run python examples/healthcare/datatypes/inspect_document.py examples/healthcare/documents/hl7-discharge-summary.xml
```

The CC0 Discharge Summary yields birth date `2005-05-01`, two problem codes, and four exact vital quantities. Supply a different local CDA file as the argument to inspect it instead. This is a selected-field view, not a complete clinical summary or validation verdict.

Raw bindings expose `root`, `extension`, `codeSystem`, `value`, and `unit`. With the compile-time mixins, `node.as_identifier()`, `node.as_coding()`, `node.as_datetime()`, and `node.as_quantity()` return frozen neutral records. Derived types inherit the methods. A `nullFlavor` reason remains distinct from a missing field, and unknown code systems remain explicit `urn:oid:` values.

Repeated children live in `_children_`, not Python list attributes. The example's namespace-aware `children()` helper keeps all occurrences in document order; the public `Document.findall()` method also returns the original bound nodes. The schema compiles once and instance XML uses the protected parser policy.

No FHIR or C-CDA template/profile conformance is claimed here. See [source provenance](../documents/README.md).
