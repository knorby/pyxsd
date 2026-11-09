# Packaged stylesheet schema resources

These files are the canonical W3C schemas used only as **advisory,
non-normative inspection input** for XSLT stylesheets. They are never used by
the normative XSLT compiler to accept, reject, or execute a stylesheet, and
their findings are never translated into `XTSE*` errors.

All files are distributed under the **W3C Software Notice and License**
(1998-07-20), reproduced in `LICENSE-W3C.txt`. They were retrieved on
2026-11-09 from the URLs below. The only changes are the local-identifier
rewrites recorded in the last column: absolute W3C schema locations were
replaced with sibling packaged filenames, and the XSLT 3.0 schema's
namespace-only XML import gained a local `schemaLocation`. No declarations,
types, assertions, or documentation text were otherwise altered.

| Packaged file | Upstream URL | Original SHA-256 | Packaged SHA-256 | Local changes |
| --- | --- | --- | --- | --- |
| `schema-for-xslt30.xsd` | https://www.w3.org/TR/xslt-30/schema-for-xslt30.xsd | `1c4c087b7b913f693f43aa9e24b0a06b6db60edee861ca930b1f188b21fec2fc` | `d980fb2a1689f8c2a5526abbbd270dccd9a5afa23bf5edabd2b1f743d661f47a` | `XMLSchema11.xsd` replaces the `xmlschema11-1` URL; `xml.xsd` added to the XML-namespace import. |
| `schema-for-xslt20.xsd` | https://www.w3.org/2007/schema-for-xslt20.xsd | `2614b194decb2ca130d47b831b23adf197af52c458808951bdc13c1783846db4` | `fc7ff19ea914b1c56a39582cbf7d558b814a271f92dde39fe75ba288bda92524` | `XMLSchema10.xsd` and `xml.xsd` replace the absolute W3C URLs. |
| `XMLSchema11.xsd` | https://www.w3.org/TR/xmlschema11-1/XMLSchema.xsd | `047e652e9863065d297651c103e366daa24fb2c0933cebcf1982272cb80600f5` | `186a903e2b99454d537edd36b0ebacbef4934888c36ebd9d39140d5b9913baf5` | Renamed from upstream `XMLSchema.xsd`; `xml.xsd` replaces the absolute URL. |
| `XMLSchema10.xsd` | https://www.w3.org/2001/XMLSchema.xsd | `e767a159c179bd6e4dc2bac2a63a62f50cbd027ec19d6b5cc9eb1c2f33e0cb2e` | `0d3f23eea191a116991a9cec18af2adc67912905eb7e6b11037013fc278ee197` | Renamed from upstream `XMLSchema.xsd`; `xml.xsd` replaces the absolute URL. |
| `xml.xsd` | https://www.w3.org/2001/xml.xsd | `61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c` | `61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c` | None. |

## Why these schemas are non-normative

The XSLT 3.0 schema says in its own documentation that it does not express
every stylesheet constraint, does not implement all forwards-compatible
behavior, and handles literal result elements only approximately. The XSLT 2.0
schema carries the same limitations. Neither schema checks XPath syntax or
semantics, component references across documents, or all static errors.

`inspect_stylesheet` reports the raw compilation and binding findings in an
inspection report and classifies why the current pyxsd XSD compiler cannot
faithfully bind a profile. `capability="unsupported"` is an honest statement
about the inspection profile, not a statement that the stylesheet is invalid
XSLT. `capability="bound"` does not certify XSLT validity either.

## Redistribution

The W3C license requires the full notice to accompany all copies. Keep
`LICENSE-W3C.txt` and this file in every wheel and source distribution that
contains the schema files. Note any future modification to a schema here,
including its date and the URI it came from.
