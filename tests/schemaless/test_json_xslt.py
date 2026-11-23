"""Native JSON sequences as XSLT 3 global context items."""

import io

import pytest

from pyxsd.schemaless import from_data, from_json
from pyxsd.xdm import XdmAtomic
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.processor import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"


def _compile(body: str):
    sheet = f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" version="3.0">{body}</xsl:stylesheet>'
    return XsltProcessor().compile(io.BytesIO(sheet.encode()))


def test_named_initial_template_reads_a_map_from_the_global_context():
    compiled = _compile('<xsl:template name="main"><xsl:sequence select=".?total"/></xsl:template>')

    result = compiled.transform(
        global_context_item=from_data({"total": 3}).to_xdm(),
        initial_template="main",
        delivery="raw",
    )

    assert result.principal == (XdmAtomic.integer(3),)


def test_root_null_supplies_no_global_context_item():
    compiled = _compile('<xsl:template name="main"><xsl:sequence select=".?total"/></xsl:template>')
    assert from_json("null").to_xdm() == ()

    with pytest.raises(XsltDynamicError) as error:
        compiled.transform(
            global_context_item=from_json("null").to_xdm(),
            initial_template="main",
            delivery="raw",
        )

    assert error.value.code == "XPDY0002"
