"""Shared helpers for the public XSLT execution-budget tests.

The literal-recursion, output-budget, and annotation tests compile the same
small initial-template stylesheets, assert the same exhausted-budget shape, and
one child probe imports the deep-stylesheet builder.  Test modules import the
names they use explicitly; nothing here is a test.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

from tests.xslt.support import run_probe, stylesheet

from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError


def compile_body(body, *, output=""):
    return XsltProcessor().compile(
        io.BytesIO(
            stylesheet(f'{output}<xsl:template name="xsl:initial-template">{body}</xsl:template>')
        )
    )


def assert_exhausted(error, label, field, limit, amount):
    assert type(error) is XsltDynamicError
    assert error.code == "PYXSD-XSLT-001"
    assert error.catchable is False
    assert error.args[0] == (
        f"the transformation exceeded its {label} budget: {field}={limit}, "
        f"cumulative amount={amount}"
    )


def deep_stylesheet(shape):
    function = """<xsl:function name="local:f">
      <xsl:param name="n"/>
      {body}
    </xsl:function>
    <xsl:template match="/"><xsl:sequence select="local:f(160)"/></xsl:template>"""
    source = b"<n/>"
    if shape == "push":
        source = b"<n>" * 160 + b"</n>" * 160
        body = """<xsl:template match="/"><xsl:apply-templates/></xsl:template>
        <xsl:template match="n">
          <out><xsl:apply-templates select="n"/></out>
        </xsl:template>"""
    elif shape == "sequence":
        body = function.format(
            body='<xsl:sequence select="if ($n eq 0) then () else ($n, local:f($n - 1))"/>'
        )
    elif shape == "function-constructor":
        body = function.format(
            body="""<xsl:if test="$n gt 0"><out>
              <xsl:sequence select="local:f($n - 1)"/>
            </out></xsl:if>"""
        )
    elif shape == "named-template":
        body = """<xsl:template match="/">
          <xsl:call-template name="nested"><xsl:with-param name="n" select="160"/></xsl:call-template>
        </xsl:template>
        <xsl:template name="nested">
          <xsl:param name="n"/>
          <xsl:if test="$n gt 0"><out><xsl:call-template name="nested">
            <xsl:with-param name="n" select="$n - 1"/>
          </xsl:call-template></out></xsl:if>
        </xsl:template>"""
    else:
        select = "/".join(["."] * 550)
        body = f'<xsl:template match="/"><xsl:sequence select="{select}"/></xsl:template>'
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, namespaces={"local": "urn:local"}))
    )
    return compiled, source


def assert_literal_python_stack(source):
    run = run_probe(
        [
            sys.executable,
            "-c",
            """
import io
import json
import sys
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.frames import ExecutionLimits

compiled = XsltProcessor(static_options={"assertions": True}).compile(io.BytesIO(sys.argv[1].encode()))
try:
    compiled.transform(b"<n/>", delivery="raw", limits=ExecutionLimits(max_frames=None),
                       output_uri="file:///unused/primary.xml")
except XsltDynamicError as error:
    print(json.dumps([type(error).__name__, error.code, error.catchable, error.args[0]]))
""",
            source.decode(),
        ],
        cwd=Path(__file__).resolve().parents[2],
        timeout=40,
    )
    name, code, catchable, message = json.loads(run.stdout)
    assert (name, code, catchable) == ("XsltDynamicError", "PYXSD-XSLT-001", False)
    assert_python_stack_message(message)


def assert_python_stack_message(message):
    assert re.fullmatch(
        r"the transformation exceeded the Python recursion limit: Python recursion limit=[1-9][0-9]*, "
        r"max_frames=None \(no frame budget\), frame depth=[1-9][0-9]*",
        message,
    )
