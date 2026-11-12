"""Resource access from stylesheets requires an explicit policy."""

import io

import pytest
from tests.xslt.support import stylesheet, transform

from pyxsd.resource_policy import ResourceData, ResourcePolicyError, validate_purpose
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
