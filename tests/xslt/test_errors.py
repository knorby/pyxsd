"""Profile-aware XSLT error diagnostics and catch/recovery eligibility.

These tests are corpus-independent.  They pin the diagnostic model itself:
expanded error names, phases, source/module/package identity, frame-derived
stacks, preserved parser/resource/XPath/Python causes, and the rule-specific
eligibility decision that ``xsl:try/catch`` will consume in Task 26.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from elementpath.exceptions import ElementPathError

from pyxsd.exceptions import PyXSDError, XPathError
from pyxsd.locations import IssueLocation, PathStep
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


def stylesheet(body: str, *, version: str = "3.0") -> str:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="{version}">'
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


def test_xpath_failure_identity_is_preserved_until_a_boundary_wraps_it(
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

    with pytest.raises(ElementPathError) as caught:
        compiled.transform(initial_template="main")
    original = caught.value
    assert original.code == "err:FORG0001"

    transformation = Transformation()
    location = IssueLocation(str(path), (PathStep("template", 1),))
    frame = FrameDiagnostic(
        kind="xpath",
        identity="xpath xs:integer(...)",
        location=location,
    )
    wrapped = transformation.boundary_error(
        original,
        stack=(frame,),
        catchable=True,
    )
    assert isinstance(wrapped, XsltDynamicError)
    assert wrapped.code == "FORG0001"
    assert wrapped.cause is original
    assert wrapped.stack == (frame,)
    assert wrapped.module == str(path)
    assert wrapped.catchable is True
    assert wrapped.matches_catch(["err:FORG0001"])
    try:
        raise wrapped from original
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


def test_scheduler_preserves_callback_identity_and_diagnostics() -> None:
    original = _CallbackFailure("callback failed")
    transformation = _RaisingTransformation(
        original,
        recovery_policy=RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name})),
    )

    with pytest.raises(_CallbackFailure) as caught:
        transformation.execute_frame(_instruction_frame())
    assert caught.value is original
    assert transformation.diagnostics
    assert transformation.diagnostics[-1].identity == "instruction select"


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
