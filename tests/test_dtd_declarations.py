"""Prolog-only DTD declaration collection.

The collector reads a document's prolog and internal subset and stops at
the root start tag. It must never retrieve an external subset or entity,
never parse or expand the instance body, return ``None`` for malformed
prologs so the ordinary parser can diagnose them, and propagate policy
denials from :func:`pyxsd.xml_policy.configure_expat_security` unchanged.
"""

from __future__ import annotations

import io
import socket
from contextlib import contextmanager

import pytest

from pyxsd import dtd_declarations
from pyxsd.dtd_declarations import DocumentDeclarations, collect_document_declarations
from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xml_policy import XmlParserPolicy

UNPARSED_DOC = b'<!DOCTYPE r [<!NOTATION n SYSTEM "n"><!ENTITY logo SYSTEM "logo" NDATA n>]><r/>'
PARSED_AND_UNPARSED_DOC = (
    b'<!DOCTYPE r [<!NOTATION n SYSTEM "n"><!ENTITY value "expanded">'
    b'<!ENTITY logo SYSTEM "logo" NDATA n>]><r>&value;</r>'
)
PLAIN_DOCTYPE_DOC = b"<!DOCTYPE r><r/>"
NO_ENTITY_DTD_DOC = (
    b'<!DOCTYPE r [<!NOTATION n SYSTEM "n"><!ATTLIST r fallback CDATA "defaulted">]><r/>'
)
EXTERNAL_SYSTEM_DOC = b'<!DOCTYPE r SYSTEM "file:///must-not-read"><r/>'
EXTERNAL_PUBLIC_DOC = b'<!DOCTYPE r PUBLIC "-//EXAMPLE//DTD Test//EN" "file:///must-not-read"><r/>'
EXTERNAL_AND_INTERNAL_DOC = (
    b'<!DOCTYPE r SYSTEM "file:///must-not-read" [<!NOTATION n SYSTEM "n">'
    b'<!ENTITY logo SYSTEM "logo" NDATA n>]><r/>'
)


def utf16(document: bytes) -> bytes:
    text = document.decode("utf-8")
    return f'<?xml version="1.0" encoding="utf-16"?>{text}'.encode("utf-16")


@contextmanager
def no_external_resources():
    """Fail the test if collection tries to read a file or open a socket."""

    def deny_access(*args, **kwargs):
        raise AssertionError("the parser tried to read a file or open a socket")

    with pytest.MonkeyPatch.context() as context:
        context.setattr("builtins.open", deny_access)
        context.setattr(socket, "socket", deny_access)
        yield


def test_collects_unparsed_entities_from_an_internal_subset():
    assert collect_document_declarations(
        UNPARSED_DOC,
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


@pytest.mark.parametrize(
    "data",
    [
        b"<r/>",
        b'<?xml version="1.0"?><r/>',
        b"<!-- prolog note --><?target ?><r/>",
    ],
)
def test_no_doctype_gives_an_empty_complete_set(data):
    assert collect_document_declarations(
        data,
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset(), True)


def test_only_ndata_declarations_enter_the_set():
    assert collect_document_declarations(
        PARSED_AND_UNPARSED_DOC,
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


def test_utf16_input_is_collected():
    assert collect_document_declarations(
        utf16(UNPARSED_DOC),
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


@pytest.mark.parametrize("data", [EXTERNAL_SYSTEM_DOC, EXTERNAL_PUBLIC_DOC])
def test_external_subsets_mark_evidence_incomplete_without_retrieval(data):
    with no_external_resources():
        assert collect_document_declarations(
            data,
            policy=XmlParserPolicy.default(),
        ) == DocumentDeclarations(frozenset(), False)


def test_an_external_subset_does_not_hide_internal_declarations():
    with no_external_resources():
        assert collect_document_declarations(
            EXTERNAL_AND_INTERNAL_DOC,
            policy=XmlParserPolicy.default(),
        ) == DocumentDeclarations(frozenset({"logo"}), False)


@pytest.mark.parametrize(
    "tail",
    [
        b"&missing;</r>",
        b"<unclosed",
        b"</wrong>",
    ],
)
def test_malformed_bodies_are_never_parsed(tail):
    data = (
        b'<!DOCTYPE r [<!NOTATION n SYSTEM "n">'
        b'<!ENTITY logo SYSTEM "logo" NDATA n>]><r attr="v">' + tail
    )
    assert collect_document_declarations(
        data,
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


def test_binary_stream_input_is_collected():
    assert collect_document_declarations(
        io.BytesIO(UNPARSED_DOC),
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


def test_stream_collection_spans_multiple_chunks(monkeypatch):
    monkeypatch.setattr(dtd_declarations, "_CHUNK_SIZE", 7)
    assert collect_document_declarations(
        io.BytesIO(UNPARSED_DOC),
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)


def test_stream_collection_leaves_the_body_unread():
    document = UNPARSED_DOC + b"body padding " * 20_000
    stream = io.BytesIO(document)
    assert collect_document_declarations(
        stream,
        policy=XmlParserPolicy.default(),
    ) == DocumentDeclarations(frozenset({"logo"}), True)
    position = stream.tell()
    assert 0 < position < len(document)
    assert stream.read()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b'<?xml version="1.0"?>',
        b"<!DOCTYPE r [<!ENTITY",
        b"<r attr=&broken;/>",
        io.BytesIO(b""),
        io.BytesIO(b"<!DOCTYPE r [<!ENTITY"),
    ],
)
def test_malformed_prologs_return_none(data):
    assert collect_document_declarations(data, policy=XmlParserPolicy.default()) is None


def test_protected_denies_unparsed_entity_declarations():
    with pytest.raises(ResourcePolicyError, match="entity"):
        collect_document_declarations(UNPARSED_DOC, policy=XmlParserPolicy.protected())


def test_protected_denies_external_subsets_instead_of_collecting():
    with (
        no_external_resources(),
        pytest.raises(ResourcePolicyError, match="external"),
    ):
        collect_document_declarations(
            EXTERNAL_SYSTEM_DOC,
            policy=XmlParserPolicy.protected(),
        )


@pytest.mark.parametrize(
    "data",
    [UNPARSED_DOC, PLAIN_DOCTYPE_DOC, utf16(PLAIN_DOCTYPE_DOC)],
)
def test_strict_denies_every_doctype(data):
    with pytest.raises(ResourcePolicyError, match="DOCTYPE"):
        collect_document_declarations(data, policy=XmlParserPolicy.strict())


def test_protected_still_collects_from_a_doctype_without_entity_declarations():
    assert collect_document_declarations(
        NO_ENTITY_DTD_DOC,
        policy=XmlParserPolicy.protected(),
    ) == DocumentDeclarations(frozenset(), True)
