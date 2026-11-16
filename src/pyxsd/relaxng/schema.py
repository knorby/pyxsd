"""Immutable compiled RELAX NG schemas and the public compilation entry point.

Schema compilation is the Phase 1 contract.  Grammatical problems are recorded
as schema-phase findings on :attr:`RelaxNGSchema.report`; only input that
cannot be read or parsed and unsupported capabilities interrupt compilation.
Instance parsing belongs to the validator phase, so :meth:`RelaxNGSchema.parse`
fails explicitly instead of returning a partial document.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NoReturn
from xml.etree.ElementTree import ParseError

from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.locations import IssueLocation
from pyxsd.namespaces import NamespaceContext
from pyxsd.relaxng.checks import check_compiled_grammar
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    DatatypeRegistrationError,
)
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.patterns import PatternPool
from pyxsd.relaxng.resources import ResourceDependency, ResourceLoader, ResourceSource
from pyxsd.relaxng.simplify import CompiledGrammar, Simplifier, compose_grammars
from pyxsd.relaxng.syntax import SchemaFinding, read_full_syntax
from pyxsd.resource_policy import ResourcePolicy
from pyxsd.validation import IssueSeverity, ValidationIssue, ValidationReport
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

__all__ = ["RelaxNGSchema", "compile"]

#: The versioned shape of the registry identity.  Bump it when the digest
#: input or the built-in library identities change meaning.
_DATATYPE_FINGERPRINT_PREFIX = "pyxsd.relaxng.datatypes/1"

#: Library URIs whose implementations are part of the RELAX NG contract.
_RESERVED_DATATYPE_LIBRARIES = {BUILTIN_LIBRARY: "built-in", XSD_LIBRARY: "XSD"}


class _SchemaReport(ValidationReport):
    """One schema-phase report whose logically identical findings appear once.

    The compiler stages write directly into this report, so deduplication runs
    at their single insertion point.  ``add_error`` and ``add_warning``
    increment their counters before ``_retain`` is called; a dropped duplicate
    therefore removes the count it just added.  A finding is a duplicate only
    when its stable code, message, and exact source location all match, so
    distinct restrictions that share a code stay visible.  ``max_issues`` is
    the caller's diagnostic-retention budget: findings beyond it remain counted
    and visible through ``suppressed_count``, making an incomplete report
    explicitly incomplete rather than invalidating the verdict.
    """

    def __init__(self, max_issues: int | None = None) -> None:
        super().__init__(max_issues=max_issues)
        self.phase = "schema"
        self._seen: set[tuple[str, str, IssueLocation | None]] = set()

    def _retain(self, issue: ValidationIssue) -> None:
        key = (issue.code, issue.message, issue.location)
        if key in self._seen:
            self._error_count -= issue.severity is IssueSeverity.ERROR
            self._warning_count -= issue.severity is IssueSeverity.WARNING
            return
        self._seen.add(key)
        super()._retain(issue)


def _record_reader_findings(report: ValidationReport, findings: tuple[SchemaFinding, ...]) -> None:
    """Merge the reader's structural findings into the schema report.

    The Phase 1 reader only emits error-severity findings; the shared
    composition stage records the same findings when it prepares a bundle, and
    the report drops the repeats.
    """
    for finding in findings:
        report.add_error(
            finding.message,
            code=finding.code,
            phase="schema",
            location=finding.location,
        )


def _reject_parser_override(
    resource_policy: ResourcePolicy | None,
    xml_policy: XmlParserPolicy | None,
    xml_limits: XmlLimits | None,
) -> None:
    """Refuse parser arguments that a trusted ResourcePolicy would swallow."""
    if resource_policy is not None and (xml_policy is not None or xml_limits is not None):
        raise ValueError(
            "xml_policy and xml_limits cannot be combined with resource_policy: "
            "ResourcePolicy.parse() is trusted application code"
        )


@contextmanager
def _policy_scope(policy: ResourcePolicy | None, *, root_read: bool) -> Iterator[None]:
    """Enter a policy's optional per-compilation closure scope.

    :class:`~pyxsd.remote.RemoteResourcePolicy` advertises the hook through a
    private capability marker rather than a public method name, so a custom
    policy may use ``compilation`` for anything else.  The scope covers every
    schema read, including includes and external references; ``root_read`` is
    false for an anonymous stream, whose dependencies must still authorize
    local-to-remote loading.
    """
    enter = getattr(policy, "_pyxsd_compilation_scope", None)
    if enter is None:
        yield
    else:
        with enter(root_read=root_read):
            yield


def _datatype_registry(
    datatype_libraries: Mapping[str, DatatypeLibraryRegistration] | None,
) -> DatatypeLibraryRegistry:
    """Build the effective registry for one compilation.

    The mapping is merged over the fixed built-in and XSD identities.  A
    nonreserved URI adds an application library for this compilation; because
    every compilation owns its registry, a later compilation may register the
    same nonreserved URI with a different implementation and fingerprint.  The
    reserved libraries can never be replaced, and every custom registration
    must carry a stable fingerprint.  Validation happens before any schema
    input is read.
    """
    registry = DatatypeLibraryRegistry()
    if datatype_libraries is None:
        return registry
    if not isinstance(datatype_libraries, Mapping):
        raise TypeError("datatype_libraries must be a mapping of library URIs to registrations")
    entries = list(datatype_libraries.items())
    for uri, registration in entries:
        if not isinstance(uri, str):
            raise TypeError("a datatype library URI must be a string")
        if not isinstance(registration, DatatypeLibraryRegistration):
            raise TypeError("expected a DatatypeLibraryRegistration")
    for uri, registration in sorted(entries, key=lambda item: item[0]):
        reserved = _RESERVED_DATATYPE_LIBRARIES.get(uri)
        if reserved is not None:
            raise DatatypeRegistrationError(
                f"the reserved {reserved} datatype library {uri!r} cannot be replaced"
            )
        registry.register(uri, registration)
    return registry


def _datatype_fingerprint(registry: DatatypeLibraryRegistry) -> str:
    """A deterministic identity for the effective datatype registry.

    Length-prefixed URI/fingerprint pairs are hashed in URI order, so mapping
    insertion order, process hash seeds, and object addresses cannot affect
    the result.  The built-in identities participate: a caller can tell a
    schema compiled with only the defaults from one that added a library.
    """
    parts: list[str] = []
    for uri in registry.library_uris():
        fingerprint = registry.registration(uri).fingerprint
        parts.append(f"{len(uri)}:{uri}{len(fingerprint)}:{fingerprint}")
    digest = hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return f"{_DATATYPE_FINGERPRINT_PREFIX}:{digest}"


@dataclass(frozen=True, eq=False)
class RelaxNGSchema:
    """An immutable compiled RELAX NG 1.0 schema.

    ``report`` holds the merged reader, simplification, composition, and
    correctness findings of this compilation.  ``grammar`` is the composed
    semantic graph with every reference bound; its ``start`` may be a
    :class:`~pyxsd.relaxng.patterns.Ref` edge whose ``target`` is the root
    element pattern.  ``dependencies`` is the complete canonical resource
    closure, and ``namespace_context`` is the root resource's namespace
    context, keeping the authored root frame exactly (an undeclared default
    namespace remains an empty entry).  ``limits`` is the schema's effective
    budget declaration (unlimited when none was supplied), and
    ``datatype_fingerprint`` identifies the effective datatype registry.

    Separate schema objects are independent and may be compiled or used
    concurrently.  One schema does not support reentrant instance parsing; the
    parser phase that would impose that restriction is not part of this
    release.
    """

    report: ValidationReport
    grammar: CompiledGrammar
    namespace_context: NamespaceContext
    limits: RelaxNGLimits
    datatype_fingerprint: str

    @classmethod
    def compile(
        cls,
        source: ResourceSource,
        *,
        datatype_libraries: Mapping[str, DatatypeLibraryRegistration] | None = None,
        resource_policy: ResourcePolicy | None = None,
        xml_policy: XmlParserPolicy | None = None,
        xml_limits: XmlLimits | None = None,
        limits: RelaxNGLimits | None = None,
    ) -> RelaxNGSchema:
        """Compile one RELAX NG 1.0 XML-syntax schema.

        - ``source`` is a local path or a readable text/binary stream.  Remote
          resources require an authorizing
          :class:`~pyxsd.resource_policy.ResourcePolicy`; an already-parsed
          ``Element`` is deliberately not accepted because it cannot carry
          policy parser evidence.

        - ``datatype_libraries`` maps library URIs to application
          registrations.  It augments the built-in and XSD libraries and may
          replace a nonreserved library; reserved identities cannot change.

        - ``resource_policy`` is a trusted application policy whose ``read``
          and ``parse`` own every resource read, including includes and
          external references.  ``xml_policy`` and ``xml_limits`` configure
          parsing only without a policy and cannot be combined with one.

        - ``limits`` bounds schema-side compiler work.  ``None`` leaves every
          budget unlimited; exhaustion of a work budget raises
          :class:`~pyxsd.relaxng.errors.RelaxNGLimitError`.  A configured
          ``max_diagnostics`` bounds report retention: beyond it, findings stay
          counted but are not retained, so ``suppressed_count`` exposes the
          incomplete report.

        Grammatical errors are recorded on :attr:`report` instead of raising.
        Unreadable or malformed input and unsupported capabilities raise
        :class:`~pyxsd.exceptions.PyXSDError` or a more specific
        :class:`~pyxsd.exceptions.PyXSDError` subclass.
        """
        _reject_parser_override(resource_policy, xml_policy, xml_limits)
        if limits is not None and not isinstance(limits, RelaxNGLimits):
            raise TypeError("limits must be a RelaxNGLimits value or None")
        effective_limits = limits if limits is not None else RelaxNGLimits()
        registry = _datatype_registry(datatype_libraries)
        loader = ResourceLoader(resource_policy, xml_policy=xml_policy, xml_limits=xml_limits)
        with _policy_scope(resource_policy, root_read=isinstance(source, (str, os.PathLike))):
            try:
                resource = loader.load(source)
            except OSError as exc:
                raise PyXSDError(f"the schema file could not be opened: {exc}") from exc
            except ParseError as exc:
                raise PyXSDError(f"the schema file is not well-formed XML: {exc}") from exc
            report = _SchemaReport(effective_limits.max_diagnostics)
            full = read_full_syntax(resource)
            _record_reader_findings(report, full.findings)
            bundle = Simplifier(
                report,
                PatternPool(BudgetCounter("compile", effective_limits)),
                registry,
            ).simplify(full)
            compiled = compose_grammars(bundle, loader)
            check_compiled_grammar(compiled, report)
        return cls(
            report=report,
            grammar=compiled,
            namespace_context=bundle.resource.namespaces,
            limits=effective_limits,
            datatype_fingerprint=_datatype_fingerprint(registry),
        )

    @property
    def dependencies(self) -> tuple[ResourceDependency, ...]:
        """The complete canonical resource closure of the compilation."""
        return self.grammar.dependencies

    def require_valid(self) -> None:
        """Raise :class:`~pyxsd.exceptions.ValidationError` when compilation failed."""
        if self.report.has_errors:
            raise ValidationError(
                f"the schema is not valid: {self.report.error_count} error(s); "
                "inspect ValidationError.report",
                self.report,
            )

    def parse(self, xml: object, **options: object) -> NoReturn:
        """Reject instance parsing; the validator phase will implement it.

        The argument is accepted for the eventual parser signature but is
        never read, and no partial or fabricated document is returned.
        """
        del xml, options
        raise PyXSDError(
            "RELAX NG instance parsing is not supported in this release; "
            "use RelaxNGSchema.compile for schema compilation"
        )


compile = RelaxNGSchema.compile
