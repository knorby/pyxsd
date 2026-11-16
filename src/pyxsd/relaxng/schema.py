"""Immutable compiled RELAX NG schemas and the public compilation entry point.

Schema compilation is the Phase 1 contract.  Grammatical problems are recorded
as schema-phase findings on :attr:`RelaxNGSchema.report`; only input that
cannot be read or parsed and unsupported capabilities interrupt compilation.
Validation-only instance parsing is the Phase 2 contract: a valid schema parses
one source into a lexical :class:`~pyxsd.relaxng.tree.RelaxNGElement` tree and
returns a :class:`~pyxsd.relaxng.document.RelaxNGDocument` with a fresh report.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.relaxng.checks import check_compiled_grammar
from pyxsd.relaxng.datatypes import (
    BUILTIN_LIBRARY,
    XSD_LIBRARY,
    DatatypeLibraryRegistration,
    DatatypeLibraryRegistry,
    DatatypeRegistrationError,
)
from pyxsd.relaxng.diagnostics import report_mismatches
from pyxsd.relaxng.document import RelaxNGDocument
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.patterns import PatternPool
from pyxsd.relaxng.resources import ResourceDependency, ResourceLoader, ResourceSource
from pyxsd.relaxng.simplify import CompiledGrammar, Simplifier, compose_grammars
from pyxsd.relaxng.syntax import SchemaFinding, read_full_syntax
from pyxsd.relaxng.tree import RelaxNGElement
from pyxsd.relaxng.validator import validate_element_recovering
from pyxsd.resource_policy import (
    ResourcePolicy,
    is_root_read_source,
    new_validation_report,
    policy_compilation,
    read_resource,
    reject_parser_override,
    source_identifier,
)
from pyxsd.validation import (
    IssueSeverity,
    ValidationIssue,
    ValidationReport,
    require_valid_report,
)
from pyxsd.xml_policy import XmlLimits, XmlParserPolicy

__all__ = ["RelaxNGSchema", "compile"]

#: The versioned shape of the registry identity.  Bump it when the digest
#: input or the built-in library identities change meaning.
_DATATYPE_FINGERPRINT_PREFIX = "pyxsd.relaxng.datatypes/1"

#: Library URIs whose implementations are part of the RELAX NG contract.
_RESERVED_DATATYPE_LIBRARIES = {BUILTIN_LIBRARY: "built-in", XSD_LIBRARY: "XSD"}


class _SchemaReport(ValidationReport):
    """Deduplicating compiler sink for the policy-owned schema-phase report.

    The compiler stages write directly into this report, so deduplication runs
    at their single insertion point.  ``add_error`` and ``add_warning``
    increment their counters before ``_retain`` is called; a dropped duplicate
    therefore removes the count it just added.  Unique findings are forwarded
    to the original report, preserving its identity, message bounds, counters,
    and application-specific behavior.  A finding is a duplicate only
    when its stable code, message, and exact source location all match, so
    distinct restrictions that share a code stay visible.  ``max_issues`` is
    the caller's diagnostic-retention budget: findings beyond it remain counted
    and visible through ``suppressed_count``, making an incomplete report
    explicitly incomplete rather than invalidating the verdict.
    """

    def __init__(self, report: ValidationReport, max_issues: int | None = None) -> None:
        super().__init__()
        self._report = report
        # Tighten, never replace, the policy's issue-retention budget. Existing
        # counts survive even if the factory supplied already-retained issues.
        report._tighten_issue_retention(max_issues)
        report.phase = "schema"
        self.phase = "schema"
        self._seen: set[tuple[str, str, IssueLocation | None]] = set()

    def _retain(self, issue: ValidationIssue) -> None:
        key = (issue.code, issue.message, issue.location)
        if key in self._seen:
            self._error_count -= issue.severity is IssueSeverity.ERROR
            self._warning_count -= issue.severity is IssueSeverity.WARNING
            return
        self._seen.add(key)
        add = (
            self._report.add_error
            if issue.severity is IssueSeverity.ERROR
            else self._report.add_warning
        )
        add(
            issue.message,
            code=issue.code,
            element=issue.element,
            phase=issue.phase,
            location=issue.location,
            constraint=issue.constraint,
            details=issue.details,
            location_precision=issue.location_precision,
        )


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
    concurrently.  One schema does not support reentrant instance parsing;
    parses of one schema must not run concurrently or reentrantly.
    """

    report: ValidationReport
    grammar: CompiledGrammar
    namespace_context: NamespaceContext
    limits: RelaxNGLimits
    datatype_fingerprint: str
    #: The compilation's trusted resource policy (if any), retained so instance
    #: reads use the same authorized source and parser evidence.
    _resource_policy: ResourcePolicy | None = field(default=None, repr=False, compare=False)

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
          ``max_diagnostics`` only bounds report retention, further tightening
          any policy report bound: beyond it, findings stay counted but are not
          retained, so ``suppressed_count`` exposes the incomplete report and
          ``diagnostics_complete`` is false.  Truncation never raises a limit
          error or removes an invalid verdict.

        Grammatical errors are recorded on :attr:`report` instead of raising.
        Unreadable or malformed input and unsupported capabilities raise
        :class:`~pyxsd.exceptions.PyXSDError` or a more specific
        :class:`~pyxsd.exceptions.PyXSDError` subclass.
        """
        reject_parser_override(resource_policy, xml_policy, xml_limits)
        if limits is not None and not isinstance(limits, RelaxNGLimits):
            raise TypeError("limits must be a RelaxNGLimits value or None")
        effective_limits = limits if limits is not None else RelaxNGLimits()
        registry = _datatype_registry(datatype_libraries)
        loader = ResourceLoader(resource_policy, xml_policy=xml_policy, xml_limits=xml_limits)
        with policy_compilation(resource_policy, root_read=is_root_read_source(source)):
            try:
                resource = loader.load(source)
            except OSError as exc:
                raise PyXSDError(f"the schema file could not be opened: {exc}") from exc
            except ET.ParseError as exc:
                raise PyXSDError(f"the schema file is not well-formed XML: {exc}") from exc
            report = new_validation_report(resource_policy)
            compiler_report = _SchemaReport(report, effective_limits.max_diagnostics)
            full = read_full_syntax(resource)
            _record_reader_findings(compiler_report, full.findings)
            bundle = Simplifier(
                compiler_report,
                PatternPool(BudgetCounter("compile", effective_limits)),
                registry,
            ).simplify(full)
            compiled = compose_grammars(bundle, loader)
            check_compiled_grammar(compiled, compiler_report)
        report._set_diagnostics_complete(report.suppressed_count == 0)
        return cls(
            report=report,
            grammar=compiled,
            namespace_context=bundle.resource.namespaces,
            limits=effective_limits,
            datatype_fingerprint=_datatype_fingerprint(registry),
            _resource_policy=resource_policy,
        )

    @property
    def dependencies(self) -> tuple[ResourceDependency, ...]:
        """The complete canonical resource closure of the compilation."""
        return self.grammar.dependencies

    def require_valid(self) -> None:
        """Raise :class:`~pyxsd.exceptions.ValidationError` when compilation failed."""
        require_valid_report(self.report)

    def parse(
        self,
        xml: str | Path | os.PathLike[str] | IO[str] | IO[bytes] | ET.Element,
        *,
        xml_policy: XmlParserPolicy | None = None,
        xml_limits: XmlLimits | None = None,
        typed: bool = True,
        capture: str = "none",
        capture_xdm: bool = False,
        limits: RelaxNGLimits | None = None,
    ) -> RelaxNGDocument:
        """Parse one instance document with validation-only semantics.

        - ``xml`` accepts a path, a readable text/binary stream, or a
          pre-parsed ``Element`` at the caller-trusted boundary, matching
          :meth:`pyxsd.Schema.parse`.  Malformed or unreadable input raises
          :class:`~pyxsd.exceptions.PyXSDError`; grammar mismatches return a
          document with instance findings.

        - ``xml_policy`` and ``xml_limits`` configure this instance parse only.
          A pre-parsed ``Element`` is a trusted value, so parser options do
          not apply to it.  A schema compiled with a trusted
          :class:`~pyxsd.resource_policy.ResourcePolicy` reads and parses
          instances through that policy instead; combining the two is
          rejected before input is read.

        - ``typed`` must be ``False`` in this phase.  The approved default
          remains ``True`` for the eventual typed contract, but requesting
          typed parsing raises before any input is read.

        - ``capture`` is ``"none"``.  ``"patterns"`` requires typed parsing
          and is rejected with ``ValueError`` before any input is read; other
          values are rejected the same way.

        - ``capture_xdm`` retains the existing opt-in XDM meaning: the same
          source revision also becomes an unannotated XML-tree view.

        - ``limits`` bounds this instance's derivative work.  ``None`` leaves
          it unlimited and value exhaustion raises
          :class:`~pyxsd.relaxng.errors.RelaxNGLimitError` instead of
          reporting invalidity.  A configured ``max_diagnostics`` only bounds
          finding retention; suppressed findings stay counted, the report stays
          invalid, and ``diagnostics_complete`` becomes false.

        The schema report's errors are rejected before instance input is
        consumed.  Every successful call returns a fresh document report:
        frozen schema-phase issue objects followed by this parse's instance
        findings.  Instance findings cover every independent recovery case in
        source order; recovered states never make the document valid.
        """
        if typed is not False:
            raise PyXSDError(
                "RELAX NG instance parsing is not supported with typed=True in this "
                "release; pass typed=False for validation-only parsing"
            )
        if not isinstance(capture, str) or capture not in ("none", "patterns"):
            raise ValueError("capture must be 'none' or 'patterns'")
        if capture == "patterns":
            raise ValueError(
                "capture='patterns' requires typed=True, which is not supported in this release"
            )
        if limits is not None and not isinstance(limits, RelaxNGLimits):
            raise TypeError("limits must be a RelaxNGLimits value or None")
        reject_parser_override(self._resource_policy, xml_policy, xml_limits)
        self.require_valid()

        source = xml
        source_id = source_identifier(source)
        namespace_context = NamespaceContext()
        xdm_source = None
        element_namespaces: NamespaceContext | None = namespace_context
        if self._resource_policy is not None:
            try:
                data = read_resource(source, self._resource_policy, purpose="instance")
                tree = self._resource_policy.parse(data, namespace_context)
            except OSError as exc:
                raise PyXSDError(f"the xml input could not be read: {exc}") from exc
            except ET.ParseError as exc:
                raise PyXSDError(f"the xml file is not well-formed XML: {exc}") from exc
            source_id = data.source_id
            if capture_xdm:
                from pyxsd.xdm.document import CaptureSource

                xdm_source = CaptureSource.from_content(data.content, source_name=data.source_id)
        elif isinstance(source, ET.Element):
            tree = source
            element_namespaces = None
            if capture_xdm:
                from pyxsd.xdm.document import CaptureSource

                xdm_source = CaptureSource.from_input(source)
        else:
            if capture_xdm:
                from pyxsd.xdm.document import CaptureSource

                max_bytes = None if xml_limits is None else xml_limits.max_bytes
                try:
                    xdm_source = CaptureSource.from_input(source, max_bytes=max_bytes)
                except OSError as exc:
                    raise PyXSDError(f"the xml input could not be read: {exc}") from exc
            parse_source: Any = BytesIO(xdm_source.data) if xdm_source is not None else source
            try:
                tree = parse_with_namespaces(
                    parse_source,
                    namespace_context,
                    policy=xml_policy,
                    limits=xml_limits,
                )
            except OSError as exc:
                raise PyXSDError(f"the xml input could not be read: {exc}") from exc
            except ET.ParseError as exc:
                raise PyXSDError(f"the xml file is not well-formed XML: {exc}") from exc

        root = RelaxNGElement.from_etree(
            tree,
            namespaces=element_namespaces,
            base_uri=source_id,
            source=IssueLocation(source_id, (PathStep(tree.tag, 1),)),
        )
        instance_report = new_validation_report(self._resource_policy)
        outcome = validate_element_recovering(root, self.grammar.start, limits=limits)
        report_mismatches(outcome, instance_report, limits=limits)

        merged = new_validation_report(self._resource_policy)
        # A RELAX NG report always specifies completeness.  Start from the
        # completed traversal value so an unspecified policy report is not
        # upgraded by the merge truth table; contributions can only downgrade.
        merged._set_diagnostics_complete(True)
        merged.extend(self.report)
        merged.extend(instance_report)

        xdm_capture = None
        if xdm_source is not None:
            from pyxsd.xdm.annotations import OccurrenceCapture
            from pyxsd.xdm.document import capture_document

            xdm_capture = capture_document(
                xdm_source,
                root,
                annotations=OccurrenceCapture(),
            )
        return RelaxNGDocument(
            schema=self,
            root=root,
            source=source,
            report=merged,
            typed=False,
            capture=capture,
            limits=limits,
            namespace_context=namespace_context,
            xdm_capture=xdm_capture,
        )


compile = RelaxNGSchema.compile
