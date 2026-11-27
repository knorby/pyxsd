"""Profile-aware XSLT error diagnostics and catch/recovery eligibility.

These tests are corpus-independent.  They pin the diagnostic model itself:
expanded error names, phases, source/module/package identity, frame-derived
stacks, preserved parser/resource/XPath/Python causes, and the rule-specific
eligibility decision that ``xsl:try/catch`` will consume in Task 26.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from elementpath.exceptions import ElementPathError

from pyxsd.exceptions import PyXSDError, XPathError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.xdm import XdmAtomic
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import (
    ERROR_NAMESPACE,
    XsltDynamicError,
    XsltError,
    XsltSerializationError,
    XsltStaticError,
    error_code_from_exception,
    expanded_error_code,
    normalized_error_code,
)
from pyxsd.xslt.profiles import (
    DOCUMENT_RETRIEVAL,
    LanguageProfile,
    RecoveryPolicy,
    RecoveryRule,
)
from pyxsd.xslt.runtime.frames import Frame, FrameDiagnostic
from pyxsd.xslt.runtime.transformation import Transformation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
ERR_NS = "http://www.w3.org/2005/xqt-errors"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> str:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        f'xmlns:my="urn:my" '
        f'version="{version}" {attributes}>'
        f"{body}</xsl:stylesheet>"
    )


def write_stylesheet(tmp_path: Path, name: str, body: str, *, version: str = "3.0") -> Path:
    path = tmp_path / name
    path.write_text(stylesheet(body, version=version), encoding="utf-8")
    return path


def compile_path(path: Path) -> Any:
    return XsltProcessor().compile(path)


# ---------------------------------------------------------------------------
# Error names and phases


def test_expanded_and_normalized_error_names() -> None:
    assert normalized_error_code(None) is None
    assert normalized_error_code("") is None
    assert normalized_error_code("XTDE0420") == "XTDE0420"
    assert normalized_error_code("err:XTDE0420") == "XTDE0420"
    assert normalized_error_code("Q{urn:custom}E1") == "E1"
    assert normalized_error_code("{urn:custom}E1") == "E1"
    assert normalized_error_code("Q{urn:custom}") is None
    assert normalized_error_code("Q{urn:broken") == "Q{urn:broken"

    assert expanded_error_code(None) is None
    assert expanded_error_code("") is None
    assert expanded_error_code("XTDE0420") == f"{{{ERROR_NAMESPACE}}}XTDE0420"
    assert expanded_error_code("err:XTDE0420") == f"{{{ERROR_NAMESPACE}}}XTDE0420"
    assert expanded_error_code("Q{urn:custom}E1") == "{urn:custom}E1"
    assert expanded_error_code("Q{}local") == "local"
    assert expanded_error_code("{urn:custom}E1") == "{urn:custom}E1"
    assert expanded_error_code("other:CODE") == "other:CODE"
    assert expanded_error_code("Q{urn:broken") == "Q{urn:broken"


def test_error_code_from_exception_uses_code_then_message() -> None:
    class Coded(Exception):
        code = "err:XPTY0004"

    class BlankCode(Exception):
        code = " "

    class NumericCode(Exception):
        code = 3

    assert error_code_from_exception(Coded("ignored")) == "XPTY0004"
    assert error_code_from_exception(BlankCode("[err:XTDE0420] failed")) == "XTDE0420"
    assert error_code_from_exception(NumericCode("[err:XPTY0004] failed")) == "XPTY0004"
    assert error_code_from_exception(RuntimeError("failed [err:XTDE0420]")) == "XTDE0420"
    assert error_code_from_exception(RuntimeError("plain failure")) is None


def test_error_phases_and_expanded_names() -> None:
    static = XsltStaticError("static", code="XTSE0010")
    dynamic = XsltDynamicError("dynamic", code="XTDE0420")
    serialization = XsltSerializationError("serialization", code="SERE0006")

    assert static.phase == "static"
    assert dynamic.phase == "dynamic"
    assert serialization.phase == "serialization"
    assert static.expanded_code == f"{{{ERROR_NAMESPACE}}}XTSE0010"
    assert dynamic.expanded_code == f"{{{ERROR_NAMESPACE}}}XTDE0420"
    assert serialization.expanded_code == f"{{{ERROR_NAMESPACE}}}SERE0006"
    assert XsltError("plain").expanded_code is None


def test_error_construction_validates_fields() -> None:
    invalid: list[dict[str, Any]] = [
        {"code": 1},
        {"phase": 1},
        {"uri": 1},
        {"module": 1},
        {"package": 1},
        {"location": "file:///x.xsl"},
        {"cause": "not-an-exception"},
        {"stack": 3},
    ]
    for kwargs in invalid:
        with pytest.raises(TypeError):
            XsltError("bad", **kwargs)

    with pytest.raises(TypeError):
        XsltDynamicError("bad", allow_catch="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        XsltSerializationError("bad", secondary="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Static and dynamic boundaries


def test_static_error_has_expanded_code_location_and_preserved_cause(
    tmp_path: Path,
) -> None:
    path = write_stylesheet(
        tmp_path,
        "invalid-static.xsl",
        '<xsl:template name="main"><xsl:value-of select="1 +"/></xsl:template>',
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_path(path)

    error = caught.value
    assert error.code == "XPST0003"
    assert error.expanded_code == f"{{{ERROR_NAMESPACE}}}XPST0003"
    assert error.phase == "static"
    assert error.uri == path.as_uri()
    assert error.module in {str(path), path.as_uri()}
    assert error.package is None
    assert error.location is not None
    assert error.location.source == str(path)
    assert isinstance(error.cause, ElementPathError)
    assert error.__cause__ is error.cause


def test_parser_and_resource_failures_keep_original_exception_types(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.xsl"
    broken.write_text(
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}"><xsl:template name="main"></xsl:stylesheet>',
        encoding="utf-8",
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_path(broken)
    parser_error = caught.value
    assert parser_error.code == "XTSE0165"
    assert isinstance(parser_error.cause, PyXSDError)
    assert not isinstance(parser_error.cause, XPathError)
    assert parser_error.__cause__ is parser_error.cause

    missing = write_stylesheet(
        tmp_path,
        "missing-include.xsl",
        '<xsl:include href="does-not-exist.xsl"/>',
    )
    with pytest.raises(XsltStaticError) as caught:
        compile_path(missing)
    resource_error = caught.value
    assert resource_error.code == "XTSE0165"
    assert isinstance(resource_error.cause, FileNotFoundError)
    assert resource_error.__cause__ is resource_error.cause
    assert resource_error.location is not None
    assert resource_error.location.source == str(missing)
    assert resource_error.module == str(missing)


def test_dynamic_xslt_error_has_frame_stack_and_source_identity(
    tmp_path: Path,
) -> None:
    path = write_stylesheet(
        tmp_path,
        "dynamic.xsl",
        """
        <xsl:template name="main">
          <xsl:call-template name="boom"/>
          <xsl:value-of select="'after'"/>
        </xsl:template>
        <xsl:template name="boom">
          <xsl:sequence select="document('urn:missing')"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main")

    error = caught.value
    assert error.code == "FODC0002"
    assert error.expanded_code == f"{{{ERROR_NAMESPACE}}}FODC0002"
    assert error.phase == "dynamic"
    assert error.cause is None
    assert error.location is not None
    assert error.module == str(path)
    identities = [frame.identity for frame in error.stack]
    assert "template main" in identities
    assert "template boom" in identities


def test_used_package_error_reports_the_library_package(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:library" package-version="1.0" version="3.0">
              <xsl:template name="library:boom" visibility="public">
                <xsl:sequence select="document('urn:missing')"/>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library"
              name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0"/>
              <xsl:template name="main" visibility="public">
                <xsl:call-template name="library:boom"/>
                <xsl:value-of select="'after'"/>
              </xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    resolver = PackageResolver.from_sources(library)
    compiled = XsltProcessor(resolver=resolver).compile(principal)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main", delivery="document")

    error = caught.value
    assert error.code == "FODC0002"
    assert error.package == "urn:library"
    assert error.module == str(library)
    assert error.stack[0].package == "urn:principal"
    assert error.stack[-1].package == "urn:library"


def test_xpath_failure_is_wrapped_at_the_transformation_boundary(
    tmp_path: Path,
) -> None:
    path = write_stylesheet(
        tmp_path,
        "xpath-failure.xsl",
        """
        <xsl:template name="main">
          <xsl:value-of select="xs:integer('not-a-number')"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main")
    wrapped = caught.value
    original = wrapped.cause
    assert isinstance(original, ElementPathError)
    assert original.code == "err:FORG0001"
    assert wrapped.code == "FORG0001"
    assert wrapped.__cause__ is original
    assert wrapped.allow_catch is False
    assert wrapped.stack
    assert wrapped.module == str(path)
    assert wrapped.location is not None
    assert wrapped.location.source == str(path)

    # A conversion site that has not left the constructor yet may still offer
    # the same wrapped error to xsl:catch.
    transformation = Transformation()
    location = IssueLocation(str(path), (PathStep("template", 1),))
    frame = FrameDiagnostic(
        kind="xpath",
        identity="xpath xs:integer(...)",
        location=location,
    )
    catchable = transformation.boundary_error(original, stack=(frame,), catchable=True)
    assert isinstance(catchable, XsltDynamicError)
    assert catchable.code == "FORG0001"
    assert catchable.cause is original
    assert catchable.stack == (frame,)
    assert catchable.module == str(path)
    assert catchable.catchable is True
    assert catchable.matches_catch(["err:FORG0001"])
    try:
        raise catchable from original
    except XsltDynamicError as raised:
        assert raised.__cause__ is original


def test_base_xslt_errors_keep_identity_and_receive_dynamic_context(
    tmp_path: Path,
) -> None:
    path = write_stylesheet(
        tmp_path,
        "typed-variable.xsl",
        """
        <xsl:template name="main">
          <xsl:variable name="v" as="xs:integer" select="'not-an-integer'"/>
          <xsl:sequence select="$v"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltError) as caught:
        compiled.transform(initial_template="main")

    error = caught.value
    assert type(error) is XsltError
    assert error.phase == "dynamic"
    assert error.code == "XTTE0570"
    assert error.stack
    assert error.location is not None
    assert error.module == str(path)


def test_diagnostics_are_only_added_when_missing() -> None:
    location = IssueLocation("file:///module.xsl", (PathStep("template", 1),))
    frame = FrameDiagnostic(
        kind="rule",
        identity="rule /",
        package="urn:package",
        location=location,
    )

    error = XsltError("plain")
    error.attach_diagnostics((frame,))
    assert error.stack == (frame,)
    assert error.phase == "dynamic"
    assert error.module == "file:///module.xsl"
    assert error.package == "urn:package"
    assert error.uri == "file:///module.xsl"
    assert error.location is location

    later = FrameDiagnostic(kind="other", identity="later")
    error.attach_diagnostics((later,), module="urn:other", package="urn:other")
    assert error.stack == (frame,)
    assert error.module == "file:///module.xsl"
    assert error.package == "urn:package"
    assert error.location is location

    initialized = XsltError("initialized", stack=(frame,))
    assert initialized.module == "file:///module.xsl"
    assert initialized.package == "urn:package"
    assert initialized.location is location

    explicit = XsltError("explicit", module="urn:module", package="urn:package")
    assert explicit.module == "urn:module"
    assert explicit.package == "urn:package"
    assert explicit.uri == "urn:module"

    attached = XsltError("attached")
    attached.attach_diagnostics((), module="urn:attached", package="urn:attached")
    assert attached.module == "urn:attached"
    assert attached.package == "urn:attached"
    assert attached.uri == "urn:attached"


# ---------------------------------------------------------------------------
# Coded XPath errors at the execution boundary

#: Each function passes a malformed regular expression, so elementpath signals
#: ``FORX0002`` as a coded foreign exception rather than an XSLT error.
REGEX_FAILURES = (
    "matches('a', '?a')",
    "replace('a', '?a', 'x')",
    "tokenize('a', '?a')",
    "analyze-string('a', '?a')",
)


@pytest.mark.parametrize("call", REGEX_FAILURES)
def test_coded_xpath_failure_becomes_a_dynamic_error_with_its_cause(
    tmp_path: Path,
    call: str,
) -> None:
    path = write_stylesheet(
        tmp_path,
        "regex-failure.xsl",
        f"""
        <xsl:template name="main">
          <xsl:sequence select="{call}"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main")

    error = caught.value
    assert error.code == "FORX0002"
    assert isinstance(error.cause, ElementPathError)
    assert error.__cause__ is error.cause
    assert error.catchable is False
    assert error.stack
    assert error.module == str(path)


def test_coded_xpath_failure_surfaces_through_an_avt(tmp_path: Path) -> None:
    path = write_stylesheet(
        tmp_path,
        "regex-avt.xsl",
        """
        <xsl:template name="main">
          <out title="{matches('a', '?a')}"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main", delivery="document")

    assert caught.value.code == "FORX0002"
    assert isinstance(caught.value.cause, ElementPathError)
    assert caught.value.stack


def test_coded_xpath_failure_surfaces_through_a_parameter_default(tmp_path: Path) -> None:
    path = write_stylesheet(
        tmp_path,
        "regex-parameter.xsl",
        """
        <xsl:template name="main">
          <xsl:call-template name="inner"/>
        </xsl:template>
        <xsl:template name="inner">
          <xsl:param name="probe" select="matches('a', '?a')"/>
          <xsl:sequence select="$probe"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main")

    assert caught.value.code == "FORX0002"
    assert isinstance(caught.value.cause, ElementPathError)


@pytest.mark.parametrize(
    "errors",
    ["err:FORX0002", "err:*", f"Q{{{ERR_NS}}}FORX0002"],
)
def test_a_coded_xpath_failure_is_caught_by_its_code(tmp_path: Path, errors: str) -> None:
    path = write_stylesheet(
        tmp_path,
        "regex-catch.xsl",
        f"""
        <xsl:template name="main">
          <xsl:try>
            <xsl:sequence select="matches('a', '?a')"/>
            <xsl:catch errors="{errors}" select="'caught'"/>
          </xsl:try>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    result = compiled.transform(initial_template="main", delivery="raw")

    assert result.principal == (XdmAtomic.string("caught"),)


def test_a_coded_xpath_failure_is_caught_across_a_nested_scheduler(tmp_path: Path) -> None:
    # Temporary tree construction runs on a nested scheduler; the enclosing
    # xsl:try belongs to the outer one and must still see a catchable error.
    path = write_stylesheet(
        tmp_path,
        "regex-nested-catch.xsl",
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:variable name="probe">
              <xsl:call-template name="inner"/>
            </xsl:variable>
            <xsl:sequence select="$probe"/>
            <xsl:catch errors="err:FORX0002" select="'caught'"/>
          </xsl:try>
        </xsl:template>
        <xsl:template name="inner">
          <xsl:sequence select="matches('a', '?a')"/>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    result = compiled.transform(initial_template="main", delivery="raw")

    assert result.principal == (XdmAtomic.string("caught"),)


def test_a_coded_xpath_failure_is_not_caught_by_a_different_code(tmp_path: Path) -> None:
    path = write_stylesheet(
        tmp_path,
        "regex-unmatched-catch.xsl",
        """
        <xsl:template name="main">
          <xsl:try>
            <xsl:sequence select="matches('a', '?a')"/>
            <xsl:catch errors="err:XTDE0001 FORX0002" select="'wrong'"/>
          </xsl:try>
        </xsl:template>
        """,
    )
    compiled = compile_path(path)

    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="main", delivery="raw")

    assert caught.value.code == "FORX0002"
    assert isinstance(caught.value.cause, ElementPathError)


def test_coded_boundary_error_leaves_an_uncoded_failure_alone() -> None:
    transformation = Transformation()
    original = RuntimeError("plain failure")

    assert transformation.coded_boundary_error(original) is None

    xslt_error = XsltDynamicError("already owned", code="XTDE0001")
    assert transformation.coded_boundary_error(xslt_error) is xslt_error


# ---------------------------------------------------------------------------
# Catch eligibility


def test_coded_dynamic_errors_match_catch_name_tests() -> None:
    dynamic = XsltDynamicError("boom", code="Q{urn:example}E1")
    assert dynamic.catchable is True
    assert dynamic.matches_catch()
    assert dynamic.matches_catch("Q{urn:example}E1")
    assert dynamic.matches_catch(["Q{urn:example}E1"])
    assert dynamic.matches_catch(["{urn:example}E1"])
    assert dynamic.matches_catch(["Q{urn:example}*"])
    assert dynamic.matches_catch(["{urn:example}*"])
    assert dynamic.matches_catch(["x:*"], namespaces={"x": "urn:example"})
    assert not dynamic.matches_catch(["x:*"], namespaces={"x": "urn:other"})
    assert not dynamic.matches_catch(["Q{urn:example}E2"])
    assert not dynamic.matches_catch(["unknown:E1"])
    assert not dynamic.matches_catch(["E1"])
    assert not dynamic.matches_catch([])
    assert not dynamic.matches_catch(["Q{broken"])
    assert not dynamic.matches_catch(["{broken"])
    assert not dynamic.matches_catch([":E1"])
    assert not dynamic.matches_catch(["x:"])
    with pytest.raises(TypeError):
        dynamic.matches_catch([1])  # type: ignore[list-item]

    other = XsltDynamicError("other", code="other:CODE")
    assert other.matches_catch(["Q{}other:CODE"])

    broken = XsltDynamicError("broken", code="{broken")
    assert not broken.matches_catch(["Q{}anything"])

    legacy = XsltDynamicError("legacy", code="err:XTDE0420")
    assert legacy.expanded_code == f"{{{ERROR_NAMESPACE}}}XTDE0420"
    assert legacy.matches_catch(["*"])
    assert legacy.matches_catch(["err:XTDE0420"])
    assert legacy.matches_catch(["err:*"])
    assert not legacy.matches_catch(["err:XPTY0004"])


def test_uncoded_and_non_dynamic_errors_are_not_catchable() -> None:
    base_dynamic = XsltError("base dynamic", code="XTDE0001")
    assert base_dynamic.catchable is True
    assert base_dynamic.matches_catch(["err:XTDE0001"])

    base_uncoded = XsltError("plain")
    assert base_uncoded.catchable is False
    assert base_uncoded.matches_catch(["*"]) is False
    base_static_phase = XsltError("static phase", code="XTSE0001", phase="static")
    assert base_static_phase.catchable is False

    uncoded = XsltDynamicError("uncoded")
    assert uncoded.catchable is False
    assert uncoded.matches_catch(["*"]) is False

    foreign = XsltDynamicError(
        "foreign",
        code="XPTY0004",
        allow_catch=False,
    )
    assert foreign.catchable is False
    assert foreign.matches_catch(["*"]) is False

    static = XsltStaticError("static", code="XTSE0010")
    assert static.catchable is False
    assert static.matches_catch(["*"]) is False

    serialization = XsltSerializationError("serialization", code="SERE0006")
    assert serialization.catchable is False
    assert serialization.matches_catch(["*"]) is False

    secondary = XsltSerializationError(
        "secondary serialization",
        code="SERE0006",
        secondary=True,
    )
    assert secondary.phase == "serialization"
    assert secondary.catchable is True
    assert secondary.matches_catch()
    assert secondary.matches_catch(["err:SERE0006"])
    assert secondary.matches_catch(["err:*"])
    assert not secondary.matches_catch(["err:XTDE0420"])
    with pytest.raises(TypeError):
        secondary.matches_catch([1])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Python callback boundaries


class _CallbackFailure(RuntimeError):
    code = "err:XPTY0004"


class _RaisingTransformation(Transformation):
    def __init__(
        self,
        error: BaseException,
        *,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        super().__init__(recovery_policy=recovery_policy)
        self._error = error

    def step(self, frame: Frame) -> Any:
        raise self._error


def _instruction_frame() -> Frame:
    return Frame(
        kind="instruction",
        identity="instruction select",
        location=IssueLocation("file:///module.xsl", (PathStep("template", 1),)),
    )


def test_scheduler_wraps_a_coded_callback_and_keeps_its_cause() -> None:
    original = _CallbackFailure("callback failed")
    transformation = _RaisingTransformation(
        original,
        recovery_policy=RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name})),
    )

    with pytest.raises(XsltDynamicError) as caught:
        transformation.execute_frame(_instruction_frame())
    wrapped = caught.value
    assert wrapped.cause is original
    assert wrapped.__cause__ is original
    assert wrapped.code == "XPTY0004"
    assert wrapped.catchable is False
    assert transformation.diagnostics
    assert transformation.diagnostics[-1].identity == "instruction select"


def test_scheduler_keeps_an_uncoded_callback_identity() -> None:
    original = RuntimeError("callback failed")
    transformation = _RaisingTransformation(original)

    with pytest.raises(RuntimeError) as caught:
        transformation.execute_frame(_instruction_frame())
    assert caught.value is original
    assert transformation.diagnostics


def test_callback_failure_is_wrapped_with_its_cause_at_an_extension_boundary() -> None:
    original = _CallbackFailure("callback failed")
    transformation = _RaisingTransformation(
        original,
        recovery_policy=RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name})),
    )
    frame = _instruction_frame().diagnostic()
    error = transformation.boundary_error(original, stack=(frame,))
    assert isinstance(error, XsltDynamicError)
    assert error.cause is original
    assert error.__cause__ is original
    assert error.code == "XPTY0004"
    assert error.stack == (frame,)
    assert error.stack[-1].identity == "instruction select"
    assert error.location == frame.location
    assert error.module == "file:///module.xsl"
    assert error.uri == "file:///module.xsl"
    assert error.catchable is False
    assert transformation.recover(error, DOCUMENT_RETRIEVAL) is False


def test_custom_error_qname_survives_extension_boundary_wrapping() -> None:
    class CustomFailure(Exception):
        code = "Q{urn:custom}E1"

    original = CustomFailure("custom extension failure")
    transformation = Transformation()
    wrapped = transformation.boundary_error(original, catchable=True)
    assert isinstance(wrapped, XsltDynamicError)
    assert wrapped.code == "{urn:custom}E1"
    assert wrapped.expanded_code == "{urn:custom}E1"
    assert wrapped.cause is original
    assert wrapped.__cause__ is original
    assert wrapped.matches_catch(["Q{urn:custom}E1"])
    assert wrapped.matches_catch(["{urn:custom}E1"])
    assert wrapped.matches_catch(["x:E1"], namespaces={"x": "urn:custom"})
    assert wrapped.matches_catch(["x:*"], namespaces={"x": "urn:custom"})
    assert not wrapped.matches_catch(["err:*"])
    assert not wrapped.matches_catch(["err:E1"])


def test_boundary_keeps_xslt_error_identity_and_attaches_context() -> None:
    original = XsltDynamicError("already an XSLT error", code="XTDE0001")
    transformation = _RaisingTransformation(original)

    with pytest.raises(XsltDynamicError) as caught:
        transformation.execute_frame(_instruction_frame())

    assert caught.value is original
    assert original.stack
    assert original.phase == "dynamic"
    assert original.module == "file:///module.xsl"
    assert original.location == _instruction_frame().location

    static = XsltStaticError("static", code="XTSE0001")
    transformation = _RaisingTransformation(static)
    with pytest.raises(XsltStaticError) as caught:
        transformation.execute_frame(_instruction_frame())
    assert caught.value is static
    assert static.phase == "static"
    assert static.stack

    policy = RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name}))
    callback_error = XsltDynamicError("foreign", code="FODC0002", allow_catch=False)
    transformation = _RaisingTransformation(callback_error, recovery_policy=policy)
    wrapped = transformation.boundary_error(
        callback_error,
        stack=(_instruction_frame().diagnostic(),),
    )
    assert wrapped is callback_error
    assert wrapped.catchable is False
    assert transformation.recover(callback_error, DOCUMENT_RETRIEVAL) is False


def test_transformation_recovery_requires_a_coded_dynamic_error() -> None:
    policy = RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name}))
    transformation = _RaisingTransformation(XsltDynamicError("unused"), recovery_policy=policy)

    allowed = XsltDynamicError("missing document", code="FODC0002")
    rejected_code = XsltDynamicError("fragment", code="XTDE1160")
    uncoded = XsltDynamicError("uncoded")
    callback = ValueError("callback")
    legacy = RecoveryRule("legacy", frozenset({"XTRE1500"}), "not available in XSLT 3.0")

    assert transformation.recover(allowed, DOCUMENT_RETRIEVAL) is True
    assert transformation.recover(allowed, DOCUMENT_RETRIEVAL.name) is True
    assert transformation.recover(allowed, "FODC0002") is True
    assert transformation.recover(rejected_code, DOCUMENT_RETRIEVAL) is False
    assert transformation.recover(uncoded, DOCUMENT_RETRIEVAL) is False
    assert transformation.recover(callback, DOCUMENT_RETRIEVAL) is False
    assert transformation.recover(allowed, legacy) is False

    strict = _RaisingTransformation(
        XsltDynamicError("unused"),
        recovery_policy=RecoveryPolicy(
            rules=frozenset({DOCUMENT_RETRIEVAL.name}),
            strict=True,
        ),
    )
    assert strict.recover(allowed, DOCUMENT_RETRIEVAL) is False


def test_transformation_profile_conversion_and_policy_validation() -> None:
    profile_options = SimpleNamespace(
        processor_profile="xslt-3.0",
        schema_aware=False,
    )
    stylesheet = SimpleNamespace(
        profile=profile_options,
        package=SimpleNamespace(name="urn:principal", output=lambda name: None),
    )
    transformation = Transformation(stylesheet)
    profile = transformation.profile_for("1.0")
    assert isinstance(profile, LanguageProfile)
    assert profile.compatibility_mode is True
    assert profile.schema_aware is False
    assert transformation.profile_for().processor_version == "3.0"

    with pytest.raises(TypeError):
        Transformation(recovery_policy="impact")  # type: ignore[arg-type]


def test_boundary_error_without_frames_uses_available_identity() -> None:
    transformation = Transformation()
    wrapped = transformation.boundary_error(RuntimeError())
    assert wrapped.code is None
    assert wrapped.stack == ()
    assert wrapped.module is None
    assert wrapped.package is None
    assert wrapped.location is None
    assert "RuntimeError" in str(wrapped)

    stylesheet = SimpleNamespace(
        profile=SimpleNamespace(processor_profile="xslt-3.0", schema_aware=True),
        package=SimpleNamespace(name="urn:stylesheet", output=lambda name: None),
    )
    transformation = Transformation(stylesheet)
    untyped_frame = FrameDiagnostic(kind="rule")
    untyped = transformation.boundary_error(RuntimeError("boom"), stack=(untyped_frame,))
    assert untyped.package == "urn:stylesheet"
    assert untyped.module is None

    location = IssueLocation("file:///module.xsl", (PathStep("template", 1),))
    packaged_frame = FrameDiagnostic(
        kind="rule",
        package="urn:frame",
        location=location,
    )
    packaged = transformation.boundary_error(RuntimeError("boom"), stack=(packaged_frame,))
    assert packaged.package == "urn:frame"
    assert packaged.module == "file:///module.xsl"
    assert packaged.location is location

    with pytest.raises(TypeError):
        transformation.boundary_error(
            RuntimeError("boom"),
            catchable="yes",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Static and dynamic taxonomy at its owning site
#
# The public ``XsltError.code`` is the normative XTSE/XTDE/XTTE code.  These
# tests pin the phase and source identity for the clusters the census measured,
# so an internal exception can never stand in for a specification code.


def _static_error(body: str, *, version: str = "3.0", attributes: str = "") -> XsltStaticError:
    with pytest.raises(XsltStaticError) as caught:
        compile_bytes(stylesheet(body, version=version, attributes=attributes))
    return caught.value


def compile_bytes(source: str) -> Any:
    return XsltProcessor().compile(io.BytesIO(source.encode()))


def test_choose_rejects_otherwise_before_a_later_when() -> None:
    error = _static_error(
        '<xsl:template match="/"><xsl:choose>'
        "<xsl:otherwise>otherwise</xsl:otherwise>"
        '<xsl:when test="true()">when</xsl:when>'
        "</xsl:choose></xsl:template>"
    )

    assert error.code == "XTSE0010"
    assert error.phase == "static"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}when"
    assert error.module is not None


@pytest.mark.parametrize(
    ("body", "element"),
    [
        (
            '<xsl:template match="/"><xsl:call-template name="t" as="xs:integer"/>'
            '</xsl:template><xsl:template name="t">5</xsl:template>',
            "call-template",
        ),
        ('<xsl:variable name="v" department="IT"/>', "variable"),
        ('<xsl:template match="/" xsl:version="3.0"/>', "template"),
        (
            '<xsl:template name="t"><xsl:context-item use="optional" select="3"/></xsl:template>',
            "context-item",
        ),
    ],
)
def test_unknown_xslt_attribute_is_xtse0090(body: str, element: str) -> None:
    error = _static_error(body)

    assert error.code == "XTSE0090"
    assert error.phase == "static"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}{element}"


def test_unknown_attribute_check_respects_forwards_compatible_version() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template match="/"><xsl:value-of select="1" future="yes"/></xsl:template>',
            version="4.0",
        )
    )

    assert compiled.language == "3.0"


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template name="t"><xsl:param name="p"/>'
        '<xsl:context-item as="xs:integer"/></xsl:template>',
        '<xsl:template name="t"><xsl:context-item use="absent"/>'
        '<xsl:context-item use="absent"/></xsl:template>',
        '<xsl:function name="my:f"><xsl:context-item use="absent"/>'
        '<xsl:sequence select="1"/></xsl:function>',
        '<xsl:variable name="v"><xsl:context-item as="xs:integer"/></xsl:variable>',
        '<xsl:template name="t">text<xsl:context-item use="absent"/></xsl:template>',
    ],
)
def test_context_item_placement_is_xtse0010(body: str) -> None:
    error = _static_error(body)

    assert error.code == "XTSE0010"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}context-item"


def test_context_item_as_must_be_an_item_type() -> None:
    error = _static_error(
        '<xsl:template name="t"><xsl:context-item as="xs:integer?"/></xsl:template>'
    )

    assert error.code == "XTSE0020"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}context-item"


def test_match_only_context_item_requires_use_required() -> None:
    error = _static_error(
        '<xsl:template match="doc"><xsl:context-item use="absent"/></xsl:template>'
    )

    assert error.code == "XTSE0020"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}context-item"


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template match="/"><xsl:for-each select="1">'
        '<xsl:sort select="." stable="maybe"/></xsl:for-each></xsl:template>',
        '<xsl:template match="/"><xsl:for-each select="1">'
        '<xsl:sort select="." order="sideways"/></xsl:for-each></xsl:template>',
        '<xsl:template match="/"><xsl:message terminate="NO">x</xsl:message></xsl:template>',
        '<xsl:template match="/"><xsl:number value="1" start-at="alpha"/></xsl:template>',
        '<xsl:template match="/"><xsl:value-of select="1"/>'
        '<xsl:evaluate xpath="\'1\'" schema-aware="TRUE"/></xsl:template>',
    ],
)
def test_static_attribute_value_taxonomy_is_xtse0020(body: str) -> None:
    error = _static_error(body)

    assert error.code == "XTSE0020"
    assert error.phase == "static"
    assert error.location is not None


@pytest.mark.parametrize(
    "body",
    [
        '<xsl:template name="xsl:main"/>',
        '<xsl:function name="xs:test"/>',
        '<xsl:variable name="err:x" select="1"/>',
        '<xsl:function name="map:show"/>',
        '<xsl:mode name="xsl:mode"/>',
    ],
)
def test_reserved_namespace_component_name_is_xtse0080(body: str) -> None:
    error = _static_error(
        body,
        attributes='xmlns:map="http://www.w3.org/2005/xpath-functions/map" '
        'xmlns:err="http://www.w3.org/2005/xqt-errors"',
    )

    assert error.code == "XTSE0080"
    assert error.phase == "static"
    assert error.location is not None


def test_initial_template_reserved_name_remains_permitted() -> None:
    compiled = compile_bytes(
        stylesheet('<xsl:template name="xsl:initial-template"><out/></xsl:template>')
    )

    result = compiled.transform(delivery="document")
    assert result.principal_tree is not None


def test_private_declared_mode_is_not_an_eligible_initial_mode() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:mode name="m" visibility="private"/>'
            '<xsl:template match="/" mode="m"><out/></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(source=b"<doc/>", initial_mode="m")

    assert caught.value.code == "XTDE0045"


def test_package_default_mode_is_eligible_even_when_private() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:mode name="m" visibility="private"/>'
            '<xsl:template match="/" mode="m"><out/></xsl:template>',
            attributes='default-mode="m"',
        )
    )

    result = compiled.transform(source=b"<doc/>", initial_mode="m", delivery="document")
    assert result.principal_tree is not None


def test_context_item_conversion_does_not_atomize_or_cast() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template name="t"><xsl:context-item as="xs:string"/>'
            '<xsl:sequence select="."/></xsl:template>'
            '<xsl:template name="main"><xsl:for-each select="1 to 3">'
            '<xsl:call-template name="t"/></xsl:for-each></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(initial_template="main", delivery="raw")

    assert caught.value.code == "XTTE0590"
    assert caught.value.phase == "dynamic"
    assert caught.value.location is not None


def test_non_atomic_context_item_does_not_satisfy_an_atomic_type() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template name="t"><xsl:context-item as="xs:string"/>'
            '<xsl:sequence select="."/></xsl:template>'
            '<xsl:template match="/doc"><xsl:call-template name="t"/></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(source=b"<doc/>", delivery="raw")

    assert caught.value.code == "XTTE0590"


@pytest.mark.parametrize(
    ("body", "code", "element"),
    [
        (
            '<xsl:template name="main"><xsl:include href="x.xsl"/></xsl:template>',
            "XTSE0170",
            "include",
        ),
        (
            '<xsl:template name="main"><xsl:import href="x.xsl"/></xsl:template>',
            "XTSE0190",
            "import",
        ),
        (
            '<xsl:template name="main"><xsl:character-map name="cm"/></xsl:template>',
            "XTSE0010",
            "character-map",
        ),
        (
            '<xsl:template name="main"><xsl:stylesheet version="3.0"/></xsl:template>',
            "XTSE0010",
            "stylesheet",
        ),
    ],
)
def test_top_level_declaration_placement_is_static_not_uncoded(
    body: str, code: str, element: str
) -> None:
    error = _static_error(body)

    assert error.code == code
    assert error.phase == "static"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}{element}"


def test_call_template_rejects_undeclared_non_tunnel_parameter() -> None:
    error = _static_error(
        '<xsl:template name="main"><xsl:call-template name="x">'
        '<xsl:with-param name="my:p" select="2"/>'
        "</xsl:call-template></xsl:template>"
        '<xsl:template name="x"/>'
    )

    assert error.code == "XTSE0680"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}call-template"


def test_call_template_rejects_non_tunnel_parameter_for_a_tunnel_declaration() -> None:
    error = _static_error(
        '<xsl:template name="main"><xsl:call-template name="t">'
        '<xsl:with-param name="p" select="2"/>'
        "</xsl:call-template></xsl:template>"
        '<xsl:template name="t"><xsl:param name="p" tunnel="yes"/></xsl:template>'
    )

    assert error.code == "XTSE0680"


# ---------------------------------------------------------------------------
# Global context item declarations


def test_duplicate_global_context_item_in_one_module_is_xtse3087() -> None:
    error = _static_error(
        '<xsl:global-context-item use="optional"/>'
        '<xsl:global-context-item use="optional"/>'
        '<xsl:template match="/"><out/></xsl:template>'
    )

    assert error.code == "XTSE3087"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}global-context-item"


def test_inconsistent_global_context_item_across_modules_is_xtse3087(tmp_path: Path) -> None:
    included = tmp_path / "included.xsl"
    included.write_text(
        stylesheet('<xsl:global-context-item use="required"/>'),
        encoding="utf-8",
    )
    principal = write_stylesheet(
        tmp_path,
        "principal.xsl",
        '<xsl:global-context-item use="optional"/>'
        '<xsl:include href="included.xsl"/>'
        '<xsl:template match="/"><out/></xsl:template>',
    )

    with pytest.raises(XsltStaticError) as caught:
        compile_path(principal)

    assert caught.value.code == "XTSE3087"
    assert caught.value.module == str(included)


def test_global_context_item_absent_with_as_is_xtse3089() -> None:
    error = _static_error(
        '<xsl:global-context-item use="absent" as="item()"/>'
        '<xsl:template match="/"><out/></xsl:template>'
    )

    assert error.code == "XTSE3089"


def test_required_global_context_item_without_one_is_xtde3086() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="required"/>'
            '<xsl:template name="main"><out/></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(initial_template="main", delivery="raw")

    assert caught.value.code == "XTDE3086"


def test_global_context_item_type_is_checked_with_xtte0590() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="required" as="document-node(element(codd))"/>'
            '<xsl:template match="/"><out/></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(source=b"<doc/>", delivery="raw")

    assert caught.value.code == "XTTE0590"


def test_global_context_item_as_requires_an_item_type() -> None:
    error = _static_error(
        '<xsl:global-context-item use="required" as="document-node()?"/>'
        '<xsl:template match="/"><out/></xsl:template>'
    )

    assert error.code == "XTSE0020"


def test_global_context_item_absent_clears_the_global_focus() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="absent"/>'
            '<xsl:variable name="v" select=". instance of node()"/>'
            '<xsl:template match="/"><xsl:sequence select="$v"/></xsl:template>'
        )
    )

    with pytest.raises(XsltError):
        compiled.transform(source=b"<doc/>", delivery="raw")


# ---------------------------------------------------------------------------
# Attribute-value helpers


def test_attribute_value_helpers_distinguish_static_and_dynamic_values() -> None:
    from pyxsd.xslt.compiler.attribute_values import (
        constant_value,
        require_boolean_value,
        require_choice_value,
        require_integer_sequence_value,
        require_integer_value,
        require_item_type,
    )
    from pyxsd.xslt.compiler.expressions import CompiledSequenceType, ItemType

    location = IssueLocation("file:///module.xsl", (PathStep("template", 1),))
    assert constant_value("yes") == "yes"
    assert constant_value("{{literal}}") == "{literal}"
    assert constant_value("a}}b") == "a}b"
    assert constant_value("{dynamic}") is None

    require_boolean_value("yes", label="flag", uri=None, location=location)
    require_boolean_value("{dynamic}", label="flag", uri=None, location=location)
    require_integer_value("-3", label="count", uri=None, location=location)
    require_integer_value("{dynamic}", label="count", uri=None, location=location)
    require_integer_sequence_value("{dynamic}", label="start", uri=None, location=location)
    require_integer_sequence_value("3 0 -1", label="start", uri=None, location=location)
    require_choice_value(
        "ascending",
        frozenset({"ascending", "descending"}),
        label="order",
        uri=None,
        location=location,
    )
    require_item_type(
        CompiledSequenceType(ItemType("item"), "", "item()"),
        label="context item",
        uri=None,
        location=location,
    )
    for invalid_type in (
        CompiledSequenceType(),
        CompiledSequenceType(ItemType("item"), "?", "item()?"),
    ):
        with pytest.raises(XsltStaticError) as caught:
            require_item_type(
                invalid_type,
                label="context item",
                uri=None,
                location=location,
            )
        assert caught.value.code == "XTSE0020"

    for call, code in (
        (
            lambda: require_integer_sequence_value("", label="start", uri=None, location=location),
            "XTSE0020",
        ),
        (
            lambda: require_integer_value("x", label="count", uri=None, location=location),
            "XTSE0020",
        ),
        (
            lambda: require_integer_sequence_value(
                "3 x", label="start", uri=None, location=location
            ),
            "XTSE0020",
        ),
        (
            lambda: require_choice_value(
                "sideways",
                frozenset({"ascending"}),
                label="order",
                uri=None,
                location=location,
            ),
            "XTSE0020",
        ),
    ):
        with pytest.raises(XsltStaticError) as caught:
            call()
        assert caught.value.code == code


def test_foreign_and_shadow_attributes_remain_permitted() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template _name="main" my:extension="yes">'
            '<xsl:value-of select="1" my:other="yes"/></xsl:template>'
        )
    )

    result = compiled.transform(initial_template="main", delivery="raw")
    assert result.principal[0].string_value == "1"


def test_forwards_compatibility_on_a_literal_result_element_is_inherited() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template name="main"><out xsl:version="4.0">'
            '<xsl:value-of select="1" future="yes"/></out></xsl:template>'
        )
    )

    assert compiled.language == "3.0"


def test_invalid_forwards_compatibility_version_keeps_the_strict_reading() -> None:
    from types import SimpleNamespace

    from pyxsd.xdm.nodes import NodeKind
    from pyxsd.xslt.compiler.native_profile import _forwards_compatible_30

    node = SimpleNamespace(
        kind=NodeKind.ELEMENT,
        name=f"{{{XSLT_NS}}}template",
        attributes=[SimpleNamespace(name="version", string_value="not-a-number")],
    )

    assert _forwards_compatible_30(node, False) is False


def test_declaration_placement_is_ignored_in_forwards_compatible_mode() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template name="main"><xsl:include href="x.xsl"/></xsl:template>',
            version="4.0",
        )
    )

    assert compiled.language == "3.0"


def test_invalid_global_context_item_use_is_xtse0020() -> None:
    error = _static_error(
        '<xsl:global-context-item use="sometimes"/><xsl:template match="/"><out/></xsl:template>'
    )

    assert error.code == "XTSE0020"


def test_optional_global_context_item_keeps_the_supplied_item() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="optional"/>'
            '<xsl:template match="/"><out><xsl:value-of select="name(/*)"/></out></xsl:template>'
        )
    )

    result = compiled.transform(source=b"<doc/>", delivery="document")
    assert result.principal_tree is not None


def test_required_library_global_context_item_is_xtte0590(tmp_path: Path) -> None:
    library = tmp_path / "library.xsl"
    library.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" xmlns:library="urn:library" name="urn:library"
              package-version="1.0" version="3.0">
              <xsl:global-context-item use="required"/>
              <xsl:template name="library:t" visibility="public"><out/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    principal = tmp_path / "principal.xsl"
    principal.write_text(
        f"""<xsl:package xmlns:xsl="{XSLT_NS}" name="urn:principal" version="3.0">
              <xsl:use-package name="urn:library" package-version="1.0"/>
              <xsl:template name="main"><out/></xsl:template>
            </xsl:package>""",
        encoding="utf-8",
    )
    compiled = XsltProcessor(resolver=PackageResolver.from_sources(library)).compile(principal)

    with pytest.raises(XsltError) as caught:
        compiled.transform(initial_template="main", delivery="raw")

    assert caught.value.code == "XTTE0590"


def test_optional_global_context_item_accepts_an_absent_item() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="optional"/>'
            '<xsl:template name="main"><out/></xsl:template>'
        )
    )

    result = compiled.transform(initial_template="main", delivery="document")
    assert result.principal_tree is not None


def test_template_rule_context_item_as_requires_an_item_type() -> None:
    error = _static_error(
        '<xsl:template match="doc">'
        '<xsl:context-item use="required" as="xs:integer?"/>'
        "</xsl:template>"
    )

    assert error.code == "XTSE0020"


def test_required_global_context_item_without_as_keeps_the_item() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="required"/>'
            '<xsl:template match="/"><out><xsl:value-of select="name(/*)"/></out></xsl:template>'
        )
    )

    result = compiled.transform(source=b"<doc/>", delivery="document")
    assert result.principal_tree is not None


def test_consistent_global_context_item_across_modules_compiles(tmp_path: Path) -> None:
    included = tmp_path / "included.xsl"
    included.write_text(
        stylesheet(
            '<xsl:global-context-item use="required" as=" document-node( element( doc ))"/>'
        ),
        encoding="utf-8",
    )
    principal = write_stylesheet(
        tmp_path,
        "principal.xsl",
        '<xsl:global-context-item use="required" as="document-node(element(doc))"/>'
        '<xsl:include href="included.xsl"/>'
        '<xsl:template match="/"><out/></xsl:template>',
    )

    compiled = compile_path(principal)
    result = compiled.transform(source=b"<doc/>", delivery="document")
    assert result.principal_tree is not None


def test_whitespace_before_a_context_item_is_permitted() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:template name="t" xml:space="preserve">  '
            '<xsl:context-item use="absent"/>'
            '<xsl:sequence select="1"/></xsl:template>'
            '<xsl:template name="main"><xsl:call-template name="t"/></xsl:template>'
        )
    )

    result = compiled.transform(initial_template="main", delivery="raw")
    assert result.principal == (XdmAtomic.integer(1),)


def test_on_completion_outside_iterate_is_xtse0010() -> None:
    error = _static_error(
        '<xsl:template name="main"><out><xsl:on-completion/></out></xsl:template>'
    )

    assert error.code == "XTSE0010"
    assert error.location is not None
    assert error.location.path[-1].name == f"{{{XSLT_NS}}}on-completion"


def test_global_context_item_document_node_rejects_an_atomic_item() -> None:
    compiled = compile_bytes(
        stylesheet(
            '<xsl:global-context-item use="required" as="document-node()"/>'
            '<xsl:template name="main"><out/></xsl:template>'
        )
    )

    with pytest.raises(XsltError) as caught:
        compiled.transform(
            initial_template="main",
            global_context_item=XdmAtomic.integer(1),
            delivery="raw",
        )

    assert caught.value.code == "XTTE0590"
