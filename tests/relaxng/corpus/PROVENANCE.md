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
- License source:
  <https://github.com/relaxng/jing-trang/blob/263a35a5e3ebbfccf8eb9752077e59cde9acc9a1/copying.txt>

`LICENSE` is a verbatim copy of the upstream `copying.txt` and carries the
BSD-3-Clause terms of the Thai Open Source Software Center Ltd.

## Integrity

`SHA256SUMS` records the checksums of the three vendored files. Verify with:

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
