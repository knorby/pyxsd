# Pinned RELAX NG conformance catalogs

These files are vendored test inputs for the native RELAX NG compiler. They
are not part of the published package and are excluded from source
distributions and Ruff's source scan.

## Upstream source

- Repository: <https://github.com/relaxng/jing-trang>
- Pinned commit: `263a35a5e3ebbfccf8eb9752077e59cde9acc9a1`
- XML-syntax catalog:
  <https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/mod/rng-validate/test/spectest.xml>
- XSD datatype catalog:
  <https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/mod/xsd-datatype/test/xsdtest.xml>
- Compact-syntax translation catalog:
  <https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/mod/rng-schema/test/compacttest.xml>
- License source:
  <https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/copying.txt>

`LICENSE` is a verbatim copy of the upstream `copying.txt` and carries the
BSD-3-Clause terms of the Thai Open Source Software Center Ltd.

## Integrity

`SHA256SUMS` records the checksums of the four vendored files. Verify with:

```bash
cd tests/relaxng/corpus
shasum -a 256 -c SHA256SUMS
```

## Catalog shape

`spectest.xml` contains 385 `testCase` entries: 172 correct schemas and 213
incorrect schemas. Those cases carry 289 valid and 291 invalid instance
assertions, for 965 catalog assertions in total. This matches the research
catalog measurement at the pinned commit. Changing any of those counts
requires reviewing the upstream catalog, not editing an expected constant.

The upstream format is inline XML. The harness materializes each case under
a gitignored `.cases/` directory using the layout from the upstream
[`test/prep.xsl`](https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/test/prep.xsl):
one case directory per test case, `c.rng` or `i.rng` for the schema, and
numbered `.v.xml`/`.i.xml` files for instances. Resources and nested resource
directories are materialized beside them. Embedded schema/instance fragments
are copied with their in-scope namespaces, and ancestor namespace declarations
needed by QName values remain usable.

`xsdtest.xml` has 44 datatype entries. The harness applies the generated-case
rules from the upstream
[`mod/xsd-datatype/test/xsdtest.xsl`](https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/mod/xsd-datatype/test/xsdtest.xsl):
datatype validity values, equivalence classes, `length`, `lessThan`, and
`incomparable` assertions become schema and instance cases. `untypedAtomic`
and `anyAtomicType` are XSD 1.1 additions and are deliberately excluded
because the promised adapter profile is XSD 1.0. The remaining 42 datatypes
produce 238 schema cases and 1041 instance assertions.

`compacttest.xml` contains 87 `testCase` entries: 56 accepted compact fragments
and 31 rejected fragments. It is a compact-to-XML translation manifest, not a
schema-validity suite: an accepted fragment may translate to a pattern that is
not a legal whole schema. The harness materializes each compact source as
`c.rnc` or `i.rnc`, every accepted case's authored XML counterpart beside it as
`c.rng`, and each inline `<resource>` at its manifest-relative path. The
manifest's separate `<bug>` wrapper is a known non-testCase fragment and is not
one of the 87 cases. Because every resource is inline, there is no separate
committed compact resource tree; the derived files live under the gitignored
`.cases/compacttest/` directory.
