"""Discover complete CDA documents, fragments, and unsuitable XML with provenance."""

from urllib.parse import quote
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from examples.healthcare.common.sources import corpus_root, manifest, resolve_document

REPOSITORIES = ("hl7", "onc", "vendor")
MAX_XML_BYTES = 8_000_000
CDA_FRAGMENT_ROOTS = frozenset(
    [
        "ClinicalDocument",
        "section",
        "component",
        "act",
        "participant",
        "author",
        "entry",
        "recordTarget",
        "performer",
        "documentationOf",
        "observation",
        "organizer",
        "substanceAdministration",
        "informant",
        "inFulfillmentOf",
        "componentOf",
        "participantRole",
        "procedure",
        "manufacturedProduct",
        "supply",
        "informationRecipient",
        "authenticator",
        "custodian",
        "dataEnterer",
        "legalAuthenticator",
        "relatedDocument",
        "entryRelationship",
        "encounter",
        "externalDocument",
        "relatedSubject",
        "assignedAuthor",
        "serviceEvent",
        "encounterParticipant",
        "addr",
        "authorization",
    ]
)


def discover(repository: str) -> list[dict]:
    """Inventory one installed repository without schema compilation or network I/O."""
    root = corpus_root(repository)
    spec = manifest()[repository]
    records = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() != ".xml" or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        selector = f"{repository}:{relative}"
        record = {
            "repository": repository,
            "path": relative,
            "selector": selector,
            "revision": spec["revision"],
            "license": spec["license"],
            "source_url": f"https://github.com/{spec['repository']}/blob/{spec['revision']}/{quote(relative)}",
            "root": None,
            "kind": "malformed",
            "error": None,
        }
        try:
            selected = resolve_document(selector)
            with selected.open("rb") as stream:
                data = stream.read(MAX_XML_BYTES + 1)
            if len(data) > MAX_XML_BYTES:
                raise ValueError(f"XML exceeds discovery budget of {MAX_XML_BYTES} bytes")
            element = fromstring(data, forbid_dtd=True)
            record["root"] = element.tag
            record["kind"] = (
                "document"
                if element.tag == "{urn:hl7-org:v3}ClinicalDocument"
                else "fragment"
                if element.tag.startswith("{urn:hl7-org:v3}") or element.tag in CDA_FRAGMENT_ROOTS
                else "other"
            )
        except (OSError, ValueError, ParseError, DefusedXmlException) as exc:
            record["error"] = str(exc)
        records.append(record)
    return records
