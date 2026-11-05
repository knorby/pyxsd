# Resolve multi-file XML schemas

```{eval-rst}
.. meta::
   :description: Compile local XSD includes and namespace imports, resolve instance schema hints, and diagnose missing schema dependencies in Python.
```

Use `xs:include` for declarations in the same target namespace (including
no namespace), and `xs:import` for another namespace. Relative
`schemaLocation` values resolve from the schema containing the directive,
not from the instance document.

## Create the files

Create a `schemas` directory and save these four files inside it. The main
schema includes a local type and imports a code type from `urn:codes`.

`schemas/orders.xsd`:

```xml
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:c="urn:codes">
  <xs:include schemaLocation="fields.xsd"/>
  <xs:import namespace="urn:codes"/>
  <xs:element name="order">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="code" type="c:ProductCode"/>
        <xs:element name="quantity" type="Quantity"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
```

`schemas/fields.xsd`:

```xml
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="Quantity">
    <xs:restriction base="xs:positiveInteger"/>
  </xs:simpleType>
</xs:schema>
```

`schemas/codes.xsd`:

```xml
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:codes">
  <xs:simpleType name="ProductCode">
    <xs:restriction base="xs:string">
      <xs:pattern value="[A-Z]{2}"/>
    </xs:restriction>
  </xs:simpleType>
</xs:schema>
```

`schemas/order.xml`:

```xml
<order xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:noNamespaceSchemaLocation="orders.xsd">
  <code>AB</code><quantity>2</quantity>
</order>
```

## Compile with an explicit namespace mapping

Run Python from the directory containing `schemas`:

```python
from pathlib import Path

from pyxsd import ParseModes, Schema

base = Path("schemas").resolve()
schema = Schema.compile(
    base / "orders.xsd",
    mode=ParseModes.NAMESPACED,
    namespace_schemas={"urn:codes": "codes.xsd"},
)
schema.require_valid()
document = schema.parse(base / "order.xml")
document.require_valid()
print(document.root.code, document.root.quantity)  # AB 2
```

The mapping supplies the location omitted by `xs:import`. Its relative path
resolves from the main schema, so `codes.xsd` means `schemas/codes.xsd` here.
Alternatively, put `schemaLocation="codes.xsd"` on that import.

Loading a schema for a namespace does not add missing authored imports to
other schema documents. Each document using foreign components must declare
the appropriate `xs:import`; an `xmlns` prefix binding alone is not an import.
Keep `require_valid()` after compilation to catch composition problems
before processing instances.

## Let an instance select the main schema

The same XML supplies `xsi:noNamespaceSchemaLocation` for its no-namespace
root. The one-call parser can use that hint:

```python
import pyxsd

hinted = pyxsd.parse(
    base / "order.xml",
    mode=pyxsd.ParseModes.NAMESPACED,
    namespace_schemas={"urn:codes": "codes.xsd"},
)
hinted.require_valid()
print(hinted.is_valid)  # True
```

For namespaced roots, `xsi:schemaLocation` contains namespace/location pairs,
for example `urn:orders orders.xsd urn:codes codes.xsd`. Instance-relative
locations resolve from the instance file. Additional hints are advisory;
inspect `schema-hint` warnings rather than assuming every hint was loaded.
`schema.parse(...)` instead uses the already compiled schema; an instance
hint does not replace it.

## Paths, streams, and remote resources

Use absolute input paths, as above, when the working directory can change.
In-memory streams do not inherently identify the directory containing their
dependencies. Prefer file paths for composed schemas, or provide resolvable
absolute references. A trusted resource policy can supply canonical source
identities and base paths; see {doc}`xml-security` for that interface and
remote-access rules. Namespace URIs are identifiers, not URLs pyxsd fetches
automatically.

If a dependency fails to load, inspect `schema.report` first. Confirm the
resolved path, target namespace, import directive, and namespace-aware mode.
Do not use lax binding as a substitute for resolving a missing type.
