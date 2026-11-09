"""Errors raised by the advisory inspector and the normative XSLT front end.

The advisory inspection error is an input/resource failure only.  Normative
errors carry the XSLT or XPath error code, the phase that discovered the
problem, the stylesheet URI and source location when known, and the preserved
underlying cause.  Static errors use the exact code the specification assigns;
a generic exception must never be substituted for one of them.
"""

from __future__ import annotations

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation


class XsltInspectionError(PyXSDError):
    """Advisory stylesheet inspection could not read or bind its inputs.

    This is an input/resource failure, not an XSLT static error. Schema
    legality findings and instance binding findings are reported on
    :class:`~pyxsd.xslt.inspection.StylesheetInspection.report` instead.
    """


class XsltError(PyXSDError):
    """Base class for normative XSLT compilation and execution errors.

    ``code`` is an XSLT (``XTSE``/``XTTE``/``XTDE``) or XPath (``XPST``/
    ``XPTY``) error code.  ``phase`` names the processing phase, ``uri`` the
    relevant stylesheet or resource, and ``location`` one source occurrence.
    ``cause`` preserves a parser, resource, or expression exception instead of
    replacing its identity with this error.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        phase: str | None = None,
        uri: str | None = None,
        location: IssueLocation | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.uri = uri
        self.location = location
        self.cause = cause

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
        location: IssueLocation | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            phase=phase,
            uri=uri,
            location=location,
            cause=cause,
        )
