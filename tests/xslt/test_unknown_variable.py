"""Undefined-variable reporting at the engine seam.

``_Runner._evaluate`` treats an ``XPST0008`` whose message names a variable as
a possible lazy global reference and evaluates that global on demand.  The
code also covers names that are not variables: elementpath raises it for
schema kind tests and parser-level missing variables, and the continuation
seam raises its own shape.  Every unrecognized message must surface the
original error instead of an assertion.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from functools import partial
from typing import Any

import pytest
from elementpath import select
from elementpath.exceptions import ElementPathNameError
from elementpath.xpath3 import XPath31Parser
from tests.xslt.support import stylesheet as _stylesheet
from tests.xslt.support import transform as _shared_transform

from pyxsd.xdm import XdmAtomic
from pyxsd.xpath.continuation import XPathEvaluationError
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.errors import XsltDynamicError, XsltError, XsltStaticError
from pyxsd.xslt.processor import _unknown_variable_name

stylesheet = _stylesheet
transform = partial(_shared_transform, delivery="raw")


def transform_with_engine_error(
    source: bytes,
    error: BaseException,
    monkeypatch: pytest.MonkeyPatch,
    **options: Any,
):
    """Run *source* with ``XPathEngine.evaluate`` raising the given error."""

    def raise_error(self: XPathEngine, *args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(XPathEngine, "evaluate", raise_error)
    compiled = XsltProcessor().compile(io.BytesIO(source))
    return compiled.transform(delivery="raw", **options)


def test_undefined_variable_is_reported_as_a_static_error() -> None:
    """A real expression with an undefined variable surfaces XPST0008.

    The static layer produces the error here; the point is the observable
    contract: an ``XPST0008`` that names the variable, never a bare
    ``AssertionError`` or ``AttributeError`` from the name parser.
    """
    source = stylesheet(
        '<xsl:template name="main"><xsl:value-of select="$missing_gVar"/></xsl:template>'
    )
    with pytest.raises(XsltStaticError) as caught:
        XsltProcessor().compile(io.BytesIO(source))
    assert caught.value.code == "XPST0008"
    assert "missing_gVar" in str(caught.value)


def test_elementpath_shaped_xpst0008_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``XPST0008`` message that names no variable re-raises untouched.

    elementpath historically raised the code as ``Name not found: …``; the
    error must keep its identity (preserved as the boundary wrapper's cause)
    and code, and must not drive a global lookup or an assertion.
    """
    error = XPathEvaluationError("XPST0008", "Name not found: 'x'")
    source = stylesheet('<xsl:template name="main"><xsl:value-of select="\'ok\'"/></xsl:template>')
    with pytest.raises(XsltError) as caught:
        transform_with_engine_error(source, error, monkeypatch, initial_template="main")
    assert not isinstance(caught.value, AssertionError)
    assert isinstance(caught.value, XsltDynamicError)
    assert caught.value.code == "XPST0008"
    assert caught.value.__cause__ is error
    assert "Name not found: 'x'" in str(caught.value)


def test_forward_global_reference_still_evaluates_on_demand() -> None:
    """A global referenced before its declaration compiles and evaluates.

    The same shape as ``test_forward_global_references_are_lazy_and_
    circularity_is_reported``: evaluating ``$a`` raises ``unknown variable
    $b`` at runtime, the recognized-name arm finds the lazy global, evaluates
    it, and retries.
    """
    source = stylesheet(
        """
        <xsl:variable name="a" select="$b + 1"/>
        <xsl:variable name="b" select="41"/>
        <xsl:template name="main"><xsl:sequence select="$a"/></xsl:template>
        """
    )
    assert transform(source, initial_template="main").principal == (XdmAtomic.integer(42),)


def test_recognized_variable_messages_yield_their_name() -> None:
    """The continuation and elementpath variable shapes parse to a name."""
    continuation_shape = XPathEvaluationError("XPST0008", "unknown variable $x")
    assert _unknown_variable_name(continuation_shape) == "x"

    # The rendered shape elementpath produces for a variable reference whose
    # name is absent at evaluation time.
    try:
        select(ET.fromstring("<a/>"), "$nope + 1", parser=XPath31Parser)
    except ElementPathNameError as exc:
        assert _unknown_variable_name(exc) == "nope"
    else:  # pragma: no cover - elementpath without the error would be a drift
        raise AssertionError("elementpath did not raise XPST0008 for an unknown variable")


def test_unrecognized_messages_yield_none() -> None:
    """Messages that name no variable return ``None`` instead of asserting."""
    assert _unknown_variable_name(XPathEvaluationError("XPST0008", "Name not found: 'x'")) is None

    # The census shape: a schema kind test that the parser cannot resolve.
    schema_lookup = AssertionError(
        "'schema-element' kind test at line 1, column 15: "
        "[err:XPST0008] schema element 't:test' not found"
    )
    assert _unknown_variable_name(schema_lookup) is None
