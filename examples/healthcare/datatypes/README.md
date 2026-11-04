# CDA datatype tour

From the repository root, fetch sources once, then run:

```sh
uv run python examples/healthcare/download_sources.py
uv run python examples/healthcare/datatypes/inspect_document.py discharge
```

The CC0 Discharge Summary from the fetched HL7 repository yields birth date `2005-05-01`, two problem codes, and four exact vital quantities. Use the [repository inventory](../README.md#discover-and-use-repository-inputs) to select any complete document from HL7, ONC, or the vendor repository; pass its `repo:path` selector or a local CDA filename. Omitting the argument selects `discharge`. This is a selected-field view, not a complete clinical summary or validation verdict.

Raw bindings expose `root`, `extension`, `codeSystem`, `value`, and `unit`. With the compile-time mixins, `node.as_identifier()`, `node.as_coding()`, `node.as_datetime()`, and `node.as_quantity()` return frozen neutral records. Derived types inherit the methods. A `nullFlavor` reason remains distinct from a missing field, and unknown code systems remain explicit `urn:oid:` values.

Repeated children live in `_children_`, not Python list attributes. The example's namespace-aware `children()` helper keeps all occurrences in document order; the public `Document.findall()` method also returns the original bound nodes. The schema compiles once and instance XML uses the protected parser policy.

No FHIR or C-CDA template/profile conformance is claimed here. See [source provenance](../SOURCES.md).
