"""Errors raised by the advisory inspector and the normative XSLT front end.

The advisory inspection error is an input/resource failure only.  Normative
errors carry the expanded XSLT or XPath error name, the phase that discovered
the problem, the stylesheet module and package identity when known, the source
location, any frame-derived dynamic stack, and the preserved underlying cause.
Static errors use the exact code the specification assigns; a generic exception
must never be substituted for one of them.

Errors keep their own identity at XSLT boundaries.  A parser, resource, XPath,
or Python exception is wrapped only where the runtime has deliberately converted
it into an XSLT dynamic error; its original object is retained as ``cause`` and
``__cause__``.  Callback failures that were not deliberately converted are not
catchable by a coded ``xsl:catch`` and are not eligible for implementation
recovery.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation
from pyxsd.xdm import XdmItem

if TYPE_CHECKING:
    from pyxsd.xslt.runtime.frames import FrameDiagnostic

#: Namespace of every specification-defined XSLT and XPath error code.
ERROR_NAMESPACE = "http://www.w3.org/2005/xqt-errors"

_ERROR_CODE = re.compile(r"\b([A-Z]{4}\d{4})\b")


def normalized_error_code(code: str | None) -> str | None:
    """Return the local part of one error code without assuming a namespace."""
    if code is None:
        return None
    value = code.strip()
    if not value:
        return None
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            return value
        local = value[close + 1 :]
        return local or None
    if value.startswith("{"):
        local = value.rsplit("}", 1)[-1]
        return local or None
    return value.rsplit(":", 1)[-1] or None


def is_static_error_code(code: str | None) -> bool:
    """Whether *code* identifies a static XPath or XSLT error.

    ``XPST``/``XQST`` are static XPath/XQuery errors and ``XTSE`` is the XSLT
    static-error family.  XSLT 3.0 section 8.3 says static errors are never
    caught, so these codes stay outside ``xsl:try`` even if a processor reports
    them while executing an expression.
    """
    expanded = expanded_error_code(code)
    prefix = f"{{{ERROR_NAMESPACE}}}"
    if expanded is None or not expanded.startswith(prefix):
        return False
    local = expanded[len(prefix) :]
    return local.startswith(("XPST", "XQST", "XTSE"))


def expanded_error_code(code: str | None) -> str | None:
    """Return one error code as an expanded QName when its namespace is known.

    A bare local code belongs to the standard error namespace.  A prefixed code
    with an unknown prefix cannot be expanded without a static context, so it is
    returned unchanged rather than guessed.
    """
    if code is None:
        return None
    value = code.strip()
    if not value:
        return None
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            return value
        uri = value[2:close]
        local = value[close + 1 :]
        return f"{{{uri}}}{local}" if uri else local
    if value.startswith("{"):
        return value
    prefix, separator, local = value.partition(":")
    if not separator:
        return f"{{{ERROR_NAMESPACE}}}{value}"
    if prefix == "err":
        return f"{{{ERROR_NAMESPACE}}}{local}"
    return value


def error_code_from_exception(error: BaseException) -> str | None:
    """Extract a specification error code from an exception without rewriting it.

    Standard error-namespace codes are returned in their familiar local form.
    A custom QName is preserved as an expanded name so an extension error in a
    non-standard namespace can still match a coded ``xsl:catch``.
    """
    raw = getattr(error, "code", None)
    if isinstance(raw, str) and raw:
        expanded = expanded_error_code(raw)
        if expanded is not None:
            prefix = f"{{{ERROR_NAMESPACE}}}"
            if expanded.startswith(prefix):
                return expanded[len(prefix) :]
            return expanded
    match = _ERROR_CODE.search(str(error))
    return None if match is None else match.group(1)


def _split_expanded_name(value: str) -> tuple[str, str]:
    if value.startswith("{"):
        close = value.find("}")
        if close >= 0:
            return value[1:close], value[close + 1 :]
    return "", value


def _normalize_name_test(
    value: str,
    namespaces: Mapping[str, str] | None,
) -> tuple[str, str] | None:
    """Normalize one ``xsl:catch/@errors`` NameTest to (namespace, local)."""
    if value == "*":
        return "*", "*"
    if value.startswith("Q{"):
        close = value.find("}")
        if close < 0:
            return None
        return value[2:close], value[close + 1 :]
    if value.startswith("{"):
        close = value.find("}")
        if close < 0:
            return None
        return value[1:close], value[close + 1 :]
    if ":" in value:
        prefix, _, local = value.partition(":")
        if not prefix or not local:
            return None
        if prefix == "*":
            return "*", local
        uri = None if namespaces is None else namespaces.get(prefix)
        if uri is None:
            if prefix != "err":
                return None
            uri = ERROR_NAMESPACE
        return uri, local
    return "", value


def _name_test_matches(
    pattern: str,
    expanded_code: str,
    namespaces: Mapping[str, str] | None,
) -> bool:
    test = _normalize_name_test(pattern.strip(), namespaces)
    if test is None:
        return False
    namespace, local = test
    code_namespace, code_local = _split_expanded_name(expanded_code)
    if namespace == "*" and local == "*":
        return True
    if namespace == "*":
        return local == code_local
    if local == "*":
        return namespace == code_namespace
    return namespace == code_namespace and local == code_local


class XsltInspectionError(PyXSDError):
    """Advisory stylesheet inspection could not read or bind its inputs.

    This is an input/resource failure, not an XSLT static error. Schema
    legality findings and instance binding findings are reported on
    :class:`~pyxsd.xslt.inspection.StylesheetInspection.report` instead.
    """


class XsltError(PyXSDError):
    """Base class for normative XSLT compilation and execution errors.

    ``code`` is an XSLT (``XTSE``/``XTTE``/``XTDE``) or XPath (``XPST``/
    ``XPTY``) error code, stored as supplied.  ``phase`` names the processing
    phase, ``uri``/``module`` the relevant stylesheet or resource, ``package``
    the containing package, and ``location`` one source occurrence.  ``stack``
    is derived from explicit execution frames, never from the Python call stack.
    ``cause`` preserves a parser, resource, expression, or callback exception
    instead of replacing its identity with this error.

    ``description``, ``value``, ``line_number``, and ``column_number`` carry
    the standard ``err:*`` catch metadata.  ``has_description`` distinguishes
    an explicitly absent description (``fn:error`` with one argument) from an
    empty one.  Line and column numbers remain ``None`` when the source
    occurrence does not carry them.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str | None = None,
        uri: str | None = None,
        module: str | None = None,
        package: str | None = None,
        location: IssueLocation | None = None,
        stack: Sequence[FrameDiagnostic] = (),
        cause: BaseException | None = None,
        description: str | None = None,
        has_description: bool = True,
        value: Sequence[object] = (),
        line_number: int | None = None,
        column_number: int | None = None,
    ) -> None:
        if code is not None and not isinstance(code, str):
            raise TypeError("an XSLT error code must be a string or None")
        if phase is not None and not isinstance(phase, str):
            raise TypeError("an XSLT error phase must be a string or None")
        for label, value_text in (
            ("uri", uri),
            ("module", module),
            ("package", package),
            ("description", description),
        ):
            if value_text is not None and not isinstance(value_text, str):
                raise TypeError(f"the XSLT error {label} must be a string or None")
        if location is not None and not isinstance(location, IssueLocation):
            raise TypeError("the XSLT error location must be an IssueLocation or None")
        if cause is not None and not isinstance(cause, BaseException):
            raise TypeError("the XSLT error cause must be an exception or None")
        if not isinstance(has_description, bool):
            raise TypeError("has_description must be a boolean")
        items = tuple(value)
        if not all(isinstance(item, XdmItem) for item in items):
            raise TypeError("the XSLT error value must contain XDM items")
        typed_items = cast(tuple[XdmItem, ...], items)
        for label, number in (
            ("line_number", line_number),
            ("column_number", column_number),
        ):
            if number is not None and (
                isinstance(number, bool) or not isinstance(number, int) or number < 0
            ):
                raise TypeError(f"the XSLT error {label} must be an integer or None")
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.uri = uri
        self.module = module
        self.package = package
        self.location = location
        self.stack: tuple[FrameDiagnostic, ...] = ()
        self.cause = cause
        self.description = message if description is None else description
        self.has_description = has_description
        self.value: tuple[XdmItem, ...] = typed_items
        self.line_number = line_number
        self.column_number = column_number
        self.attach_diagnostics(stack, module=module, package=package)

    @property
    def expanded_code(self) -> str | None:
        """The error code as an expanded QName, when its namespace is known."""
        return expanded_error_code(self.code)

    @property
    def catchable(self) -> bool:
        """Whether a coded ``xsl:catch`` may consume this error.

        Only dynamically signaled XSLT/XPath errors are catchable.  Static and
        serialization errors are outside ``xsl:try``, and a foreign callback
        failure is not converted merely because it crossed an XSLT boundary.
        """
        return (
            self.phase in (None, "dynamic")
            and bool(self.code)
            and not is_static_error_code(self.code)
        )

    def matches_catch(
        self,
        errors: Iterable[str] | str | None = None,
        *,
        namespaces: Mapping[str, str] | None = None,
    ) -> bool:
        """Whether one ``xsl:catch/@errors`` list selects this error."""
        if not self.catchable:
            return False
        if errors is None:
            return True
        patterns: Iterable[str] = (errors,) if isinstance(errors, str) else errors
        expanded = self.expanded_code or ""
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise TypeError("xsl:catch error tests must be strings")
            if _name_test_matches(pattern, expanded, namespaces):
                return True
        return False

    def attach_diagnostics(
        self,
        stack: Sequence[FrameDiagnostic],
        *,
        module: str | None = None,
        package: str | None = None,
    ) -> None:
        """Attach frame-derived context only where it is not already known."""
        frames = tuple(stack)
        if not self.stack:
            self.stack = frames
        if self.module is None:
            if module is not None:
                self.module = module
            elif self.location is not None:
                self.module = self.location.source
            elif frames and frames[-1].location is not None:
                self.module = frames[-1].location.source
        if self.package is None:
            if frames and frames[-1].package is not None:
                self.package = frames[-1].package
            elif package is not None:
                self.package = package
        if self.phase is None:
            self.phase = "dynamic"
        if self.location is None and frames:
            self.location = frames[-1].location
        if self.uri is None:
            self.uri = self.module

    def __str__(self) -> str:
        message = self.args[0] if self.args else ""
        label = f"[{self.code}] " if self.code else ""
        where = ""
        if self.location is not None:
            path = "/".join(f"{step.name}[{step.index}]" for step in self.location.path)
            source = self.location.source or self.uri
            if source:
                where = f" at {source}"
                if path:
                    where += f"#{path}"
        elif self.uri:
            where = f" in {self.uri}"
        return f"{label}{message}{where}"


class XsltStaticError(XsltError):
    """A static error, reported before any transformation output starts.

    A static-expression failure keeps its original XPath code; a stylesheet
    resource or structural failure uses the exact ``XTSE`` code for that rule.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str = "static",
        uri: str | None = None,
        module: str | None = None,
        package: str | None = None,
        location: IssueLocation | None = None,
        stack: Sequence[FrameDiagnostic] = (),
        cause: BaseException | None = None,
        description: str | None = None,
        has_description: bool = True,
        value: Sequence[object] = (),
        line_number: int | None = None,
        column_number: int | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            phase=phase,
            uri=uri,
            module=module,
            package=package,
            location=location,
            stack=stack,
            cause=cause,
            description=description,
            has_description=has_description,
            value=value,
            line_number=line_number,
            column_number=column_number,
        )


class XsltDynamicError(XsltError):
    """A dynamic error raised while executing a transformation.

    ``code`` is normally an XSLT ``XTDE``/``XTTE`` code or an XPath error code.
    The execution runtime's own resource limits use the stable implementation
    code ``PYXSD-XSLT-001``; callers can test it exactly without depending on
    message text.  ``allow_catch`` is false for foreign callback failures that
    crossed an XSLT boundary without being deliberately converted.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str = "dynamic",
        uri: str | None = None,
        module: str | None = None,
        package: str | None = None,
        location: IssueLocation | None = None,
        stack: Sequence[FrameDiagnostic] = (),
        cause: BaseException | None = None,
        allow_catch: bool = True,
        description: str | None = None,
        has_description: bool = True,
        value: Sequence[object] = (),
        line_number: int | None = None,
        column_number: int | None = None,
    ) -> None:
        if not isinstance(allow_catch, bool):
            raise TypeError("allow_catch must be a boolean")
        self.allow_catch = allow_catch
        super().__init__(
            message,
            code=code,
            phase=phase,
            uri=uri,
            module=module,
            package=package,
            location=location,
            stack=stack,
            cause=cause,
            description=description,
            has_description=has_description,
            value=value,
            line_number=line_number,
            column_number=column_number,
        )

    @property
    def catchable(self) -> bool:
        return self.allow_catch and super().catchable


class XsltSerializationError(XsltError):
    """A serialization error, distinct from the transformation's dynamic phase.

    A serialization error while writing a secondary result is treated as a
    dynamic error of the ``xsl:result-document`` instruction and may be caught;
    a principal-result serialization error happens after the transformation and
    cannot be caught.  ``secondary`` records which boundary raised it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str = "serialization",
        uri: str | None = None,
        module: str | None = None,
        package: str | None = None,
        location: IssueLocation | None = None,
        stack: Sequence[FrameDiagnostic] = (),
        cause: BaseException | None = None,
        secondary: bool = False,
        description: str | None = None,
        has_description: bool = True,
        value: Sequence[object] = (),
        line_number: int | None = None,
        column_number: int | None = None,
    ) -> None:
        if not isinstance(secondary, bool):
            raise TypeError("secondary must be a boolean")
        self.secondary = secondary
        super().__init__(
            message,
            code=code,
            phase=phase,
            uri=uri,
            module=module,
            package=package,
            location=location,
            stack=stack,
            cause=cause,
            description=description,
            has_description=has_description,
            value=value,
            line_number=line_number,
            column_number=column_number,
        )

    @property
    def catchable(self) -> bool:
        return self.secondary and bool(self.code) and not is_static_error_code(self.code)


__all__ = [
    "ERROR_NAMESPACE",
    "XsltDynamicError",
    "XsltError",
    "XsltInspectionError",
    "XsltSerializationError",
    "XsltStaticError",
    "error_code_from_exception",
    "expanded_error_code",
    "is_static_error_code",
    "normalized_error_code",
]
