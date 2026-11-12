"""Resource access from stylesheets requires an explicit policy."""

import io

import pytest
from elementpath.exceptions import ElementPathError
from tests.xslt.support import stylesheet, transform

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.runtime.resources import DocumentResources


def test_unparsed_text_does_not_implicitly_read_local_files(tmp_path) -> None:
    secret = tmp_path / "input.txt"
    secret.write_text("private", encoding="utf-8")
    source = stylesheet(
        '<xsl:template name="xsl:initial-template">'
        f"<xsl:value-of select=\"unparsed-text('{secret.as_uri()}')\"/>"
        "</xsl:template>"
    )
    with pytest.raises(ElementPathError, match="FOUT1170"):
        transform(source)


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
