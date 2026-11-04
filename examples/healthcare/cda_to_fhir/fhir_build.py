"""Small constructors around generated FHIR classes, not XML string templates."""

from __future__ import annotations

from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from pyxsd import Document, Schema, XmlParserPolicy
from pyxsd.validation import ValidationReport

FHIR = "http://hl7.org/fhir"


def resource_id(root: str, extension: str, occurrence: str) -> str:
    key = "https://pyxsd.knorby.com/examples/healthcare/" + "/".join(
        quote(part, safe="") for part in (root, extension, occurrence)
    )
    return str(uuid5(NAMESPACE_URL, key))


class Builder:
    def __init__(self, schema: Schema, root: str, extension: str):
        self.schema, self.root, self.extension = schema, root, extension

    def node(self, type_name: str, tag: str, *children, value=None):
        node = self.schema.classes[type_name]()
        node._name_ = f"{{{FHIR}}}{tag}"
        node._children_ = list(children)
        node._attribs_ = {}
        node._value_ = None
        if value is not None:
            node.value = value
            node._attribs_["value"] = str(value)
        return node

    def primitive(self, tag: str, value, type_name: str = "string"):
        return self.node(type_name, tag, value=value)

    def identifier(self, value, tag="identifier"):
        return self.node(
            "Identifier",
            tag,
            self.primitive("system", value.system, "uri"),
            self.primitive("value", value.value),
        )

    def concept(self, tag: str, system: str, code: str, display: str | None = None):
        fields = [self.primitive("system", system, "uri"), self.primitive("code", code, "code")]
        if display:
            fields.append(self.primitive("display", display))
        return self.node("CodeableConcept", tag, self.node("Coding", "coding", *fields))

    def reference(self, tag: str, occurrence: str):
        return self.node("Reference", tag, self.primitive("reference", self.url(occurrence)))

    def url(self, occurrence: str) -> str:
        return "urn:uuid:" + resource_id(self.root, self.extension, occurrence)

    def resource(self, kind: str, occurrence: str, *children):
        return self.node(
            kind,
            kind,
            self.primitive("id", resource_id(self.root, self.extension, occurrence), "id"),
            *children,
        )

    def entry(self, occurrence: str, resource):
        return self.node(
            "Bundle.Entry",
            "entry",
            self.primitive("fullUrl", self.url(occurrence), "uri"),
            self.node("ResourceContainer", "resource", resource),
        )

    def validate(self, bundle) -> Document:
        from io import StringIO

        candidate = Document(self.schema, bundle, None, ValidationReport()).to_string()
        return self.schema.parse(StringIO(candidate), xml_policy=XmlParserPolicy.protected())
