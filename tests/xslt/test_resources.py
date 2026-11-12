"""Resource access from stylesheets requires an explicit policy."""

import io

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.resource_policy import ResourceData, ResourcePolicyError, validate_purpose
from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.resources import DocumentResources


def test_unparsed_text_does_not_implicitly_read_local_files(tmp_path) -> None:
    secret = tmp_path / "input.txt"
    secret.write_text("private", encoding="utf-8")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f"<xsl:value-of select=\"unparsed-text('{secret.as_uri()}')\"/>"
        "</xsl:template>"
    )
    with pytest.raises(XsltDynamicError) as caught:
        transform(source)
    assert caught.value.code == "FOUT1170"


@pytest.mark.parametrize("uri", ["https://example.test/data.xml", "ftp://example.test/data.xml"])
def test_general_policy_does_not_authorize_stylesheet_remote_reads(uri: str) -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            if hasattr(source, "read"):
                return ResourceData("<memory>", source.read())
            raise AssertionError("general policy must not receive a remote document read")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f"<xsl:sequence select=\"doc('{uri}')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(ResourcePolicyError, match="remote"):
        compiled.transform()


def test_separate_remote_policy_authorizes_document_read() -> None:
    class Remote:
        def __init__(self) -> None:
            self.reads = []

        def read(self, source, *, base, purpose):
            self.reads.append((source, base, purpose))
            return ResourceData(source, b"<remote>allowed</remote>")

    remote = Remote()
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"doc('https://example.test/data.xml')/remote\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(remote_resource_policy=remote).compile(io.BytesIO(source))
    result = compiled.transform()
    assert result.principal_tree.string_value == "allowed"
    assert remote.reads == [("https://example.test/data.xml", None, "instance")]


def test_remote_policy_must_supply_read() -> None:
    with pytest.raises(TypeError, match="remote_resource_policy"):
        XsltProcessor(remote_resource_policy=object())
    with pytest.raises(TypeError, match="remote resource policy"):
        DocumentResources({}, remote_policy=object())


def test_resource_purpose_vocabulary_includes_text_and_collection() -> None:
    assert validate_purpose("text") == "text"
    assert validate_purpose("collection") == "collection"


# ---------------------------------------------------------------------------
# unparsed-text and unparsed-text-lines


def _stylesheet_stream(source: object) -> ResourceData | None:
    """Pass the anonymous stylesheet stream through an invocation-local policy."""
    reader = getattr(source, "read", None)
    if reader is None:
        return None
    return ResourceData("<memory>", reader())


def test_unparsed_text_reads_urn_through_general_policy() -> None:
    class Policy:
        def __init__(self) -> None:
            self.reads: list[tuple[str, str | None, str]] = []

        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            self.reads.append((source, base, purpose))
            return ResourceData(source, b"hello")

    policy = Policy()
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    result = compiled.transform()
    assert result.principal_tree.string_value == "hello"
    assert policy.reads == [("urn:test:text", None, "text")]


def test_unparsed_text_normalizes_line_endings() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"one\r\ntwo\rthree")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "one\ntwo\nthree"


def test_general_policy_does_not_authorize_remote_text_reads() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            raise AssertionError("general policy must not receive a remote text read")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('https://example.test/data.txt')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(ResourcePolicyError, match="remote"):
        compiled.transform()


def test_remote_policy_authorizes_relative_text_read() -> None:
    class Remote:
        def __init__(self) -> None:
            self.reads: list[tuple[str, str | None, str]] = []

        def read(self, source, *, base, purpose):
            self.reads.append((source, base, purpose))
            return ResourceData(source, b"remote text")

    remote = Remote()
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('data.txt')\"/>"
        "</xsl:template>",
        attributes=' xml:base="https://example.test/dir/"',
    )
    compiled = XsltProcessor(remote_resource_policy=remote).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "remote text"
    assert remote.reads == [("https://example.test/dir/data.txt", None, "text")]


def test_unparsed_text_unknown_encoding_is_a_dynamic_error() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"hello")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text', 'x-not-an-encoding')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOUT1190"


def test_unparsed_text_invalid_bytes_are_a_dynamic_error() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"\xff")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOUT1170"
    assert isinstance(caught.value.cause, UnicodeDecodeError)


def test_unparsed_text_read_failure_is_a_dynamic_error() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            raise OSError("disk unavailable")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOUT1170"
    assert isinstance(caught.value.cause, OSError)


def test_unparsed_text_requires_resource_data() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return b"not resource data"

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    with pytest.raises(TypeError, match="ResourceData"):
        compiled.transform()


def test_unparsed_text_is_cached_per_invocation() -> None:
    class Policy:
        def __init__(self) -> None:
            self.reads = 0

        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            self.reads += 1
            return ResourceData(source, b"cached")

    policy = Policy()
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"concat(unparsed-text('urn:test:text'), "
        "unparsed-text('urn:test:text'))\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "cachedcached"
    assert policy.reads == 1


def test_unparsed_text_lines_split_and_drop_the_trailing_empty_line() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"a\r\nb\rc\n")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"string-join(unparsed-text-lines('urn:test:lines'), '|')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "a|b|c"


def test_unparsed_text_lines_preserves_interior_empty_lines() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"a\n\n")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"count(unparsed-text-lines('urn:test:lines'))\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "2"


def test_unparsed_text_functions_accept_an_empty_uri() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            raise AssertionError("an empty URI must not read a resource")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:value-of select="count(unparsed-text(()))"/>'
        '<xsl:value-of select="count(unparsed-text-lines(()))"/></out>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    result = compiled.transform()
    assert result.principal_tree.string_value == "00"


def test_unparsed_text_encoding_argument_selects_the_decoder() -> None:
    class Policy:
        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            return ResourceData(source, b"caf\xe9")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<xsl:value-of select=\"unparsed-text('urn:test:text', 'iso-8859-1')\"/>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(resource_policy=Policy()).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "café"


# ---------------------------------------------------------------------------
# collection and uri-collection


def _collection_document(xml: bytes, uri: str | None) -> XdmDocument:
    return XdmDocument.parse(xml, document_uri=uri)


def test_collection_resolver_returns_nodes() -> None:
    document = _collection_document(b"<root><item/></root>", "urn:doc:one")
    seen: list[str | None] = []

    def resolver(uri: str | None):
        seen.append(uri)
        return (document.root_element,)

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    result = compiled.transform()
    assert result.principal_tree.string_value == "1"
    assert seen == ["urn:set"]


def test_collection_resolves_a_relative_uri_against_the_static_base() -> None:
    document = _collection_document(b"<root/>", "urn:doc:one")
    seen: list[str | None] = []

    def resolver(uri: str | None):
        seen.append(uri)
        return (document.root_element,)

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('data'))\"/></out>"
        "</xsl:template>",
        attributes=' xml:base="https://example.test/sets/"',
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "1"
    assert seen == ["https://example.test/sets/data"]


def test_collection_without_a_resolver_is_an_error() -> None:
    missing = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(missing))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FODC0002"

    default = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:value-of select="count(collection())"/></out>'
        "</xsl:template>"
    )
    compiled = XsltProcessor().compile(io.BytesIO(default))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FODC0002"


def test_collection_resolver_empty_named_result_is_an_error() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=lambda uri: ()).compile(io.BytesIO(source))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FODC0002"


def test_default_collection_may_be_empty() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:value-of select="count(collection())"/>'
        '<xsl:value-of select="count(uri-collection())"/></out>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=lambda uri: ()).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "00"


def test_collection_resolver_exceptions_propagate() -> None:
    def resolver(uri: str | None):
        raise RuntimeError(f"no such collection {uri}")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    with pytest.raises(RuntimeError, match="no such collection"):
        compiled.transform()


def test_uri_collection_returns_document_uris() -> None:
    first = _collection_document(b"<one/>", "urn:doc:one")
    second = _collection_document(b"<two/>", "urn:doc:two")

    def resolver(uri: str | None):
        assert uri == "urn:set"
        return (first, second)

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"string-join(uri-collection('urn:set'), ' ')\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "urn:doc:one urn:doc:two"


def test_uri_collection_requires_an_item_document_uri() -> None:
    document = XdmDocument.parse(b"<root/>")

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(uri-collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=lambda uri: (document,)).compile(
        io.BytesIO(source)
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FODC0002"


def test_collection_resolver_must_return_nodes() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(
        collection_resolver=lambda uri: (XdmAtomic.string("not a node"),)
    ).compile(io.BytesIO(source))
    with pytest.raises(TypeError, match="nodes"):
        compiled.transform()


def test_collection_is_cached_per_invocation() -> None:
    document = _collection_document(b"<root/>", "urn:doc:one")
    calls: list[str | None] = []

    def resolver(uri: str | None):
        calls.append(uri)
        return (document,)

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"count(collection('urn:set'))"
        " + count(collection('urn:set'))\"/></out>"
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "2"
    assert calls == ["urn:set"]
    assert compiled.transform().principal_tree.string_value == "2"
    assert calls == ["urn:set", "urn:set"]


def test_collection_accepts_an_empty_uri_as_the_default_collection() -> None:
    document = _collection_document(b"<root/>", "urn:doc:one")
    seen: list[str | None] = []

    def resolver(uri: str | None):
        seen.append(uri)
        return (document,)

    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        '<out><xsl:value-of select="count(collection(()))"/>'
        '<xsl:value-of select="count(uri-collection(()))"/></out>'
        "</xsl:template>"
    )
    compiled = XsltProcessor(collection_resolver=resolver).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "11"
    assert seen == [None]


def test_collection_resolver_is_validated_at_construction() -> None:
    with pytest.raises(TypeError, match="collection_resolver"):
        XsltProcessor(collection_resolver=object())
    with pytest.raises(TypeError, match="collection resolver"):
        DocumentResources({}, collection_resolver=object())


# ---------------------------------------------------------------------------
# json-doc and the pure JSON functions


def _json_doc_policy(payload: bytes):
    class Policy:
        def __init__(self) -> None:
            self.reads: list[tuple[str, str | None, str]] = []

        def read(self, source, *, base, purpose):
            stream = _stylesheet_stream(source)
            if stream is not None:
                return stream
            self.reads.append((source, base, purpose))
            return ResourceData(source, payload)

    return Policy()


def _json_expression(expression: str) -> bytes:
    return stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f'<out><xsl:value-of select="{expression}"/></out>'
        "</xsl:template>"
    )


def test_json_doc_reads_through_the_text_policy() -> None:
    policy = _json_doc_policy(b'{"name":"value","items":[1,true,null]}')
    source = _json_expression(
        "concat(json-doc('urn:test:json')?name, ':', array:size(json-doc('urn:test:json')?items))"
    )
    compiled = XsltProcessor(resource_policy=policy).compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "value:3"
    assert policy.reads == [("urn:test:json", None, "text")]


def test_json_doc_invalid_json_is_a_dynamic_error() -> None:
    compiled = XsltProcessor(resource_policy=_json_doc_policy(b'{"broken"')).compile(
        io.BytesIO(_json_expression("json-doc('urn:test:json')?broken"))
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOJS0001"


def test_json_doc_preserves_the_first_duplicate_member() -> None:
    policy = _json_doc_policy(b'{"a":1,"a":2}')
    compiled = XsltProcessor(resource_policy=policy).compile(
        io.BytesIO(_json_expression("json-doc('urn:test:json')?a = 1"))
    )
    assert compiled.transform().principal_tree.string_value == "true"


def test_json_doc_rejects_non_finite_constants() -> None:
    compiled = XsltProcessor(resource_policy=_json_doc_policy(b"NaN")).compile(
        io.BytesIO(_json_expression("json-doc('urn:test:json')"))
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOJS0001"


def test_json_doc_empty_uri_is_an_empty_sequence() -> None:
    source = _json_expression("count(json-doc(()))")
    compiled = XsltProcessor().compile(io.BytesIO(source))
    assert compiled.transform().principal_tree.string_value == "0"


def test_json_doc_rejects_options() -> None:
    policy = _json_doc_policy(b'{"a":1}')
    compiled = XsltProcessor(resource_policy=policy).compile(
        io.BytesIO(_json_expression("json-doc('urn:test:json', map{})"))
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform()
    assert caught.value.code == "FOJS0005"


def test_pure_json_functions_parse_convert_and_serialize() -> None:
    source = stylesheet(
        '<xsl:template name="xsl:initial-template"><out>'
        "<xsl:value-of select=\"parse-json('{&quot;a&quot;:[1,2]}')?a?2\"/>"
        "<xsl:value-of select=\"json-to-xml('{&quot;a&quot;:1}') "
        'instance of document-node()"/>'
        "<xsl:value-of select=\"xml-to-json(json-to-xml('{&quot;a&quot;:1}'))\"/>"
        "</out></xsl:template>"
    )
    assert transform(source).principal_tree.string_value == '2true{"a":1}'


def test_pure_json_functions_report_their_errors() -> None:
    parse_error = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"parse-json('{bad')\"/></out>"
        "</xsl:template>"
    )
    with pytest.raises(Exception, match="FOJS0001"):
        transform(parse_error)

    convert_error = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"json-to-xml('{bad')\"/></out>"
        "</xsl:template>"
    )
    with pytest.raises(Exception, match="FOJS0001"):
        transform(convert_error)

    serialize_error = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select=\"xml-to-json(parse-json('1'))\"/></out>"
        "</xsl:template>"
    )
    with pytest.raises(Exception, match="XPTY0004"):
        transform(serialize_error)
