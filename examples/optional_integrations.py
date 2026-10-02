"""Generate Pydantic models and write selected XML records as Parquet.

Install pyxsd[pydantic,arrow], then run this script with an output path.
"""

import argparse
from io import StringIO
from pathlib import Path

import pyxsd
from pyxsd.integrations.arrow import records
from pyxsd.integrations.pydantic import models

XSD = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="orders"><xs:complexType><xs:sequence>
    <xs:element name="order" maxOccurs="unbounded"><xs:complexType><xs:sequence>
      <xs:element name="number" type="xs:int"/>
      <xs:element name="note" type="xs:string" minOccurs="0"/>
    </xs:sequence></xs:complexType></xs:element>
  </xs:sequence></xs:complexType></xs:element>
</xs:schema>"""
XML = """<orders><order><number>1</number></order>
<order><number>2</number><note>second row</note></order></orders>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Parquet output path")
    args = parser.parse_args()
    schema = pyxsd.compile(StringIO(XSD), mode=pyxsd.ParseModes.NAMESPACED)
    registry = models(schema)
    Order = registry.model_for(element="orders", path=("order",))
    print(Order.model_validate({"number": 3}).model_dump(exclude_unset=True))
    document = schema.parse(StringIO(XML))
    print(registry.from_document(document).model_dump(exclude_unset=True, by_alias=True))
    projection = records(schema, element="orders", path=("order",))
    print(projection.table(document, selector="order"))
    projection.write_parquet(document, args.destination, selector="order", batch_size=1)
    print(f"Wrote {args.destination}")


if __name__ == "__main__":
    main()
