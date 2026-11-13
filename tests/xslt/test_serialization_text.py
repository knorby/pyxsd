"""The text output method and binary writing.

Text serialization joins item string values without markup.  ``write`` keeps
encoding and the byte-order mark separate from producing the string, and a
destination failure propagates unchanged.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt.errors import XsltSerializationError
from pyxsd.xslt.serialization import SerializationParameters, Serializer


def _document(xml: bytes) -> XdmDocument:
    return XdmDocument.parse(xml)


def _serialize(sequence: tuple[object, ...], **values: object) -> str:
    parameters = SerializationParameters.merge(explicit=values)
    return Serializer.serialize(sequence, parameters)  # type: ignore[arg-type]


def test_text_joining_item_separator_and_atomic_string_values() -> None:
    assert _serialize((XdmAtomic.string("a"), XdmAtomic.integer(2)), method="text") == "a 2"
    assert (
        _serialize(
            (XdmAtomic.string("a"), XdmAtomic.integer(2)),
            method="text",
            item_separator="|",
        )
        == "a|2"
    )
    assert (
        _serialize((XdmAtomic.string("a"), XdmAtomic.string("b")), method="text", item_separator="")
        == "ab"
    )
    assert _serialize((XdmAtomic.string(""), XdmAtomic.string("b")), method="text") == " b"


def test_text_uses_node_string_values() -> None:
    document = _document(b"<r>text<b>bold</b></r>")

    assert _serialize((document, XdmAtomic.string("tail")), method="text") == "textbold tail"
    assert _serialize((), method="text") == ""


def test_text_rejects_function_and_structured_items() -> None:
    function = XdmFunction(lambda arguments, environment, closure: (), arity=0)
    array = XdmArray([1])

    for item in (function, array):
        with pytest.raises(XsltSerializationError) as error:
            _serialize((item,), method="text")
        assert error.value.code == "SENR0001"


def test_write_encodes_declared_encoding_and_propagates_destination_errors() -> None:
    document = _document(b"<root>caf\xc3\xa9</root>")
    parameters = SerializationParameters(method="xml", encoding="UTF-8", byte_order_mark=True)

    destination = io.BytesIO()
    Serializer.write((document,), parameters, destination)

    assert destination.getvalue() == (
        b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?><root>caf\xc3\xa9</root>'
    )

    class Failing:
        def write(self, data: bytes) -> None:
            raise OSError("destination closed")

    with pytest.raises(OSError, match="destination closed"):
        Serializer.write((document,), parameters, Failing())


def test_write_supports_utf16_utf32_and_plain_utf8() -> None:
    document = _document(b"<r>caf\xc3\xa9</r>")
    no_declaration = SerializationParameters(omit_xml_declaration=True)

    utf16 = io.BytesIO()
    Serializer.write(
        (document,),
        SerializationParameters(omit_xml_declaration=True, encoding="utf-16", byte_order_mark=True),
        utf16,
    )
    assert utf16.getvalue().startswith(b"\xff\xfe")
    assert utf16.getvalue()[2:].decode("utf-16-le") == "<r>caf\xe9</r>"

    utf16_be = io.BytesIO()
    Serializer.write(
        (document,),
        SerializationParameters(
            omit_xml_declaration=True, encoding="utf-16-be", byte_order_mark=True
        ),
        utf16_be,
    )
    assert utf16_be.getvalue().startswith(b"\xfe\xff")
    assert utf16_be.getvalue()[2:].decode("utf-16-be") == "<r>caf\xe9</r>"

    utf32 = io.BytesIO()
    Serializer.write(
        (document,),
        SerializationParameters(omit_xml_declaration=True, encoding="utf-32", byte_order_mark=True),
        utf32,
    )
    assert utf32.getvalue().startswith(b"\xff\xfe\x00\x00")
    assert utf32.getvalue()[4:].decode("utf-32-le") == "<r>caf\xe9</r>"

    utf32_be = io.BytesIO()
    Serializer.write(
        (document,),
        SerializationParameters(
            omit_xml_declaration=True, encoding="utf-32-be", byte_order_mark=True
        ),
        utf32_be,
    )
    assert utf32_be.getvalue().startswith(b"\x00\x00\xfe\xff")
    assert utf32_be.getvalue()[4:].decode("utf-32-be") == "<r>caf\xe9</r>"

    plain = io.BytesIO()
    Serializer.write((document,), no_declaration, plain)
    assert plain.getvalue() == b"<r>caf\xc3\xa9</r>"

    unsupported_bom = io.BytesIO()
    Serializer.write(
        (_document(b"<r/>"),),
        SerializationParameters(omit_xml_declaration=True, encoding="ascii", byte_order_mark=True),
        unsupported_bom,
    )
    assert unsupported_bom.getvalue() == b"<r/>"


def test_write_accepts_a_filesystem_path(tmp_path: Path) -> None:
    document = _document(b"<r/>")
    target = tmp_path / "serialized.xml"

    Serializer.write((document,), SerializationParameters(omit_xml_declaration=True), target)

    assert target.read_bytes() == b"<r/>"
