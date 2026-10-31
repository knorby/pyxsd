"""Opt-in XML parser policies: default compatibility, protected, and strict.

The default profile must stay on stdlib ElementTree with today's DTD and
entity behavior. Protected and strict profiles opt in to defusedxml and
must fail closed through :class:`ResourcePolicyError`.
"""

from __future__ import annotations

import io
import socket
from dataclasses import FrozenInstanceError
from xml.etree import ElementTree as ET
from xml.parsers import expat

import pytest

from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy, configure_expat_security

ENTITY_DOC = b'<!DOCTYPE root [<!ENTITY value "expanded">]><root>&value;</root>'
ATTLIST_DOC = b'<!DOCTYPE root [<!ATTLIST root fallback CDATA "defaulted">]><root/>'
EXTERNAL_DTD_DOC = b'<!DOCTYPE root SYSTEM "file:///must-not-read"><root/>'
EXTERNAL_ENTITY_DOC = (
    b'<!DOCTYPE root [<!ENTITY value SYSTEM "file:///must-not-read">]><root>&value;</root>'
)
EXTERNAL_ONLY_DOC = (
    b'<!DOCTYPE root [<!ENTITY value SYSTEM "file:///must-not-read">]><root>&value;</root>'
)


def parse(data, *, context=None, **kwargs):
    return parse_with_namespaces(
        io.BytesIO(data),
        context if context is not None else NamespaceContext(),
        **kwargs,
    )


def utf16(document: bytes) -> bytes:
    text = document.decode("utf-8")
    return f'<?xml version="1.0" encoding="utf-16"?>{text}'.encode("utf-16")


def test_policy_types_are_exported_from_the_package():
    import pyxsd

    assert pyxsd.XmlParserPolicy is XmlParserPolicy
    assert pyxsd.XmlLimits is XmlLimits
    assert {"XmlParserPolicy", "XmlLimits"} <= set(pyxsd.__all__)


def test_presets_are_immutable_opt_in_values():
    default = XmlParserPolicy.default()
    assert default == XmlParserPolicy()
    assert default == XmlParserPolicy(False, False, False, False)
    assert XmlParserPolicy.protected() == XmlParserPolicy(True, False, True, True)
    assert XmlParserPolicy.strict() == XmlParserPolicy(True, True, True, True)
    with pytest.raises(FrozenInstanceError):
        default.use_defusedxml = True  # type: ignore[misc]


@pytest.mark.parametrize(
    "flag",
    ["forbid_dtd", "forbid_entities", "forbid_external"],
)
def test_forbid_flags_require_defusedxml(flag):
    with pytest.raises(ValueError, match="use_defusedxml"):
        XmlParserPolicy(**{flag: True})


@pytest.mark.parametrize("name", ["max_bytes", "max_depth", "max_elements"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_limits_must_be_positive_integers(name, value):
    with pytest.raises(ValueError, match=name):
        XmlLimits(**{name: value})


def test_limits_accept_none_and_positive_integers():
    assert XmlLimits() == XmlLimits(None, None, None)
    assert XmlLimits(max_bytes=4, max_depth=2, max_elements=1).max_elements == 1


def test_default_expands_internal_entities():
    for policy in (None, XmlParserPolicy.default()):
        root = parse(ENTITY_DOC, policy=policy)
        assert root.text == "expanded"


def test_default_accepts_a_plain_doctype_and_attlist_defaults():
    root = parse(ATTLIST_DOC)
    assert root.get("fallback") == "defaulted"


def test_default_accepts_utf16_entities():
    root = parse(utf16(ENTITY_DOC))
    assert root.text == "expanded"


@pytest.mark.parametrize("data", [ENTITY_DOC, utf16(ENTITY_DOC)])
def test_protected_rejects_entity_declarations_with_the_original_cause(data):
    from defusedxml.common import DefusedXmlException

    with pytest.raises(ResourcePolicyError) as excinfo:
        parse(data, policy=XmlParserPolicy.protected())
    assert isinstance(excinfo.value.__cause__, DefusedXmlException)


def test_protected_preserves_harmless_attlist_defaults():
    root = parse(ATTLIST_DOC, policy=XmlParserPolicy.protected())
    assert root.get("fallback") == "defaulted"


def test_protected_accepts_a_plain_doctype():
    root = parse(b"<!DOCTYPE root><root/>", policy=XmlParserPolicy.protected())
    assert root.tag == "root"


@pytest.mark.parametrize(
    "doctype",
    [
        b'<!DOCTYPE root SYSTEM "file:///must-not-read">',
        b'<!DOCTYPE root PUBLIC "-//EXAMPLE//DTD Test//EN" "file:///must-not-read">',
    ],
)
def test_protected_rejects_external_doctype_identifiers_without_access(monkeypatch, doctype):
    def deny_access(*args, **kwargs):
        raise AssertionError("the parser tried to read a file or open a socket")

    data = doctype + b"<root/>"
    with monkeypatch.context() as context:
        context.setattr("builtins.open", deny_access)
        context.setattr(socket, "socket", deny_access)
        with pytest.raises(ResourcePolicyError, match="external"):
            parse(data, policy=XmlParserPolicy.protected())


def test_protected_rejects_external_entity_declarations(monkeypatch):
    def deny_access(*args, **kwargs):
        raise AssertionError("the parser tried to read a file or open a socket")

    with monkeypatch.context() as context:
        context.setattr("builtins.open", deny_access)
        context.setattr(socket, "socket", deny_access)
        with pytest.raises(ResourcePolicyError, match="entity"):
            parse(EXTERNAL_ENTITY_DOC, policy=XmlParserPolicy.protected())


def test_external_denial_without_entity_denial(monkeypatch):
    def deny_access(*args, **kwargs):
        raise AssertionError("the parser tried to read a file or open a socket")

    policy = XmlParserPolicy(use_defusedxml=True, forbid_external=True)
    with monkeypatch.context() as context:
        context.setattr("builtins.open", deny_access)
        context.setattr(socket, "socket", deny_access)
        with pytest.raises(ResourcePolicyError, match="external"):
            parse(EXTERNAL_ONLY_DOC, policy=policy)


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE root><root/>",
        EXTERNAL_DTD_DOC,
        b'<!DOCTYPE root PUBLIC "-//EXAMPLE//DTD Test//EN" "file:///must-not-read"><root/>',
        ENTITY_DOC,
        utf16(b"<!DOCTYPE root><root/>"),
    ],
)
def test_strict_rejects_every_doctype(data):
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        parse(data, policy=XmlParserPolicy.strict())


@pytest.mark.parametrize("policy", [None, XmlParserPolicy.protected(), XmlParserPolicy.strict()])
def test_malformed_xml_keeps_the_parse_error_contract(policy):
    with pytest.raises(ET.ParseError):
        parse(b"<root><child></root>", policy=policy)


def test_protected_preserves_nested_namespace_scopes():
    context = NamespaceContext()
    root = parse(
        b'<r xmlns:p="urn:one"><c xmlns:p="urn:two"/></r>',
        context=context,
        policy=XmlParserPolicy.protected(),
    )
    assert context.resolve(root, "p:T") == "{urn:one}T"
    assert context.resolve(root[0], "p:T") == "{urn:two}T"


@pytest.mark.parametrize(
    ("limits", "data"),
    [
        (XmlLimits(max_bytes=11), b"<r><c/></r>"),
        (XmlLimits(max_depth=2), b"<r><c/></r>"),
        (XmlLimits(max_elements=2), b"<r><c/></r>"),
    ],
)
def test_limits_allow_the_exact_boundary(limits, data):
    root = parse(data, limits=limits)
    assert root.tag == "r"


@pytest.mark.parametrize(
    ("limits", "data"),
    [
        (XmlLimits(max_bytes=10), b"<r><c/></r>"),
        (XmlLimits(max_depth=1), b"<r><c/></r>"),
        (XmlLimits(max_elements=1), b"<r><c/></r>"),
    ],
)
def test_limits_reject_one_past_the_boundary(limits, data):
    with pytest.raises(ResourcePolicyError, match="exceeds"):
        parse(data, limits=limits)


def test_byte_limits_apply_to_named_paths(tmp_path):
    path = tmp_path / "input.xml"
    path.write_bytes(b"<r><c/></r>")
    assert parse_with_namespaces(path, limits=XmlLimits(max_bytes=11)).tag == "r"
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        parse_with_namespaces(path, limits=XmlLimits(max_bytes=10))


def test_text_stream_byte_limits_count_utf8_bytes():
    text = "<r>" + "\u00e9" * 10 + "</r>"
    assert len(text) == 17
    assert len(text.encode("utf-8")) == 27
    root = parse_with_namespaces(io.StringIO(text), limits=XmlLimits(max_bytes=27))
    assert root.tag == "r"
    assert root.text == "\u00e9" * 10
    with pytest.raises(ResourcePolicyError, match="max_bytes"):
        parse_with_namespaces(io.StringIO(text), limits=XmlLimits(max_bytes=26))


def test_limits_and_policies_apply_together():
    with pytest.raises(ResourcePolicyError, match="max_elements"):
        parse(
            b"<r><c/></r>",
            policy=XmlParserPolicy.protected(),
            limits=XmlLimits(max_elements=1),
        )


def test_default_and_protected_select_their_parser_backends(monkeypatch):
    from defusedxml import ElementTree as DefusedET
    from defusedxml.ElementTree import DefusedXMLParser

    stdlib_calls = []
    defused_calls = []
    stdlib_iterparse = ET.iterparse
    defused_iterparse = DefusedET.iterparse

    def record_stdlib(*args, **kwargs):
        stdlib_calls.append(kwargs)
        return stdlib_iterparse(*args, **kwargs)

    def record_defused(*args, **kwargs):
        defused_calls.append(kwargs)
        return defused_iterparse(*args, **kwargs)

    monkeypatch.setattr(ET, "iterparse", record_stdlib)
    monkeypatch.setattr(DefusedET, "iterparse", record_defused)

    parse(b"<r/>", policy=XmlParserPolicy.default())
    assert len(stdlib_calls) == 1
    assert defused_calls == []

    parse(b"<r/>", policy=XmlParserPolicy.protected())
    assert len(stdlib_calls) == 1
    assert len(defused_calls) == 1
    parser = defused_calls[0]["parser"]
    assert isinstance(parser, DefusedXMLParser)
    assert parser.forbid_dtd is False
    assert parser.forbid_entities is True
    assert parser.forbid_external is True


def test_default_policy_with_limits_stays_on_stdlib(monkeypatch):
    from defusedxml import ElementTree as DefusedET

    defused_calls = []
    monkeypatch.setattr(
        DefusedET, "iterparse", lambda *args, **kwargs: defused_calls.append(kwargs)
    )
    root = parse(b"<r/>", policy=XmlParserPolicy.default(), limits=XmlLimits(max_bytes=4))
    assert root.tag == "r"
    assert defused_calls == []


def test_configure_expat_security_default_keeps_entity_expansion():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.default())
    characters: list[str] = []
    parser.CharacterDataHandler = characters.append
    parser.Parse(ENTITY_DOC, True)
    assert "".join(characters) == "expanded"


def test_configure_expat_security_protected_rejects_entities():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.protected())
    with pytest.raises(ResourcePolicyError, match="entity"):
        parser.Parse(ENTITY_DOC, True)


def test_configure_expat_security_protected_rejects_external_doctypes():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.protected())
    with pytest.raises(ResourcePolicyError, match="external"):
        parser.Parse(EXTERNAL_DTD_DOC, True)


def test_configure_expat_security_protected_accepts_a_plain_doctype():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.protected())
    parser.Parse(b"<!DOCTYPE r><r/>", True)


def test_configure_expat_security_rejects_unparsed_entities():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.protected())
    document = (
        b'<!DOCTYPE r [<!NOTATION gif SYSTEM "image/gif">'
        b'<!ENTITY logo SYSTEM "logo.gif" NDATA gif>]><r/>'
    )
    with pytest.raises(ResourcePolicyError, match="entity"):
        parser.Parse(document, True)


def test_configure_expat_security_protected_rejects_external_references(monkeypatch):
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy(use_defusedxml=True, forbid_external=True))

    def deny_open(*args, **kwargs):
        raise AssertionError("the parser tried to read a file")

    with monkeypatch.context() as context:
        context.setattr("builtins.open", deny_open)
        with pytest.raises(ResourcePolicyError, match="external"):
            parser.Parse(EXTERNAL_ONLY_DOC, True)


def test_configure_expat_security_accepts_a_defusedxml_parser():
    from defusedxml.ElementTree import DefusedXMLParser

    parser = DefusedXMLParser(
        target=ET.TreeBuilder(),
        forbid_dtd=False,
        forbid_entities=False,
        forbid_external=False,
    )
    configure_expat_security(parser, XmlParserPolicy.strict())
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        parser.feed(b"<!DOCTYPE r><r/>")


def test_configure_expat_security_strict_rejects_doctypes():
    parser = expat.ParserCreate()
    configure_expat_security(parser, XmlParserPolicy.strict())
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        parser.Parse(b"<!DOCTYPE r><r/>", True)


def test_configure_expat_security_protected_chains_an_existing_doctype_handler():
    """The external guard must wrap a handler the parser already installs.

    CPython 3.13.18+ and 3.14+ ElementTree install their own start-DOCTYPE
    handler, so "leave an existing handler alone" silently dropped the guard
    on those versions. This hand-installed handler reproduces that condition
    on every interpreter.
    """
    seen = []

    def record(name, sysid, pubid, has_internal_subset):
        seen.append((name, sysid, pubid, has_internal_subset))

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = record
    configure_expat_security(parser, XmlParserPolicy.protected())
    with pytest.raises(ResourcePolicyError, match="external"):
        parser.Parse(EXTERNAL_DTD_DOC, True)
    assert seen == []

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = record
    configure_expat_security(parser, XmlParserPolicy.protected())
    parser.Parse(ATTLIST_DOC, True)
    assert seen == [("root", None, None, True)]


def test_configure_expat_security_strict_replaces_an_existing_doctype_handler():
    seen = []

    def record(*args):
        seen.append(args)

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = record
    configure_expat_security(parser, XmlParserPolicy.strict())
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        parser.Parse(b"<!DOCTYPE r><r/>", True)
    assert seen == []


def test_protected_parse_chains_a_parser_installed_doctype_handler(monkeypatch):
    """End-to-end proof through the semantic parse on any interpreter."""
    from defusedxml.ElementTree import DefusedXMLParser

    seen = []

    def record(name, sysid, pubid, has_internal_subset):
        seen.append((name, sysid, pubid, has_internal_subset))

    def install_doctype_handler(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.parser.StartDoctypeDeclHandler = record

    original_init = DefusedXMLParser.__init__
    monkeypatch.setattr(DefusedXMLParser, "__init__", install_doctype_handler)

    with pytest.raises(ResourcePolicyError, match="external"):
        parse(EXTERNAL_DTD_DOC, policy=XmlParserPolicy.protected())
    assert seen == []

    root = parse(ATTLIST_DOC, policy=XmlParserPolicy.protected())
    assert root.get("fallback") == "defaulted"
    assert seen == [("root", None, None, True)]

    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        parse(b"<!DOCTYPE root><root/>", policy=XmlParserPolicy.strict())
    assert seen == [("root", None, None, True)]


def test_the_documented_expat_version_report_is_vendor_neutral():
    """`docs/xml-security.md` tells operators to print `pyexpat.EXPAT_VERSION`."""
    import pyexpat
    import re

    version = expat.EXPAT_VERSION
    assert version == pyexpat.EXPAT_VERSION
    assert re.fullmatch(r"expat_\d+\.\d+\.\d+", version)
    major, _, _ = version.removeprefix("expat_").split(".")
    assert int(major) >= 2
