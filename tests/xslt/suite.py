"""Per-profile execution of the pinned W3C XSLT test catalog.

Each profile is a distinct processor identity plus the declared catalog
capabilities it implements.  The 3.0 profile runs the default processor; the
native 2.0 and 1.0 profiles construct ``XsltProcessor(language=..., xpath=...)``
themselves — the 2.0 one schema-aware, the 1.0 one not — so their verdicts never
describe a ``version`` attribute on a 3.0 processor.

The executor covers the invocation shapes the catalog declares in
``<environment>`` and ``<test>`` (context items, sources, collections, principal
selection, packages, parameters, entry points, and delivery).  A shape the
harness cannot drive is reported as ``adapter-gap``; an engine exception is a
``fail`` with its message, never a silent pass.  Every executed case is bounded
by a per-case budget, so one pathological stylesheet cannot stall a profile
run.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from elementpath.regex import unicode_version
from tests.xslt.assertions import ResultSnapshot
from tests.xslt.environment import build_schema_pool, environment_fingerprint
from tests.xslt.model import XML_NS, Param, Source, Stylesheet, TestCase
from tests.xslt.outcomes import CaseResult
from tests.xslt.runner import CaseExecutionError, run_cases
from tests.xslt.selection import XSLT30_NON_STREAMING, FeatureProfile

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap
from pyxsd.xdm.document import DocumentValidationError, parse_typed_document
from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xdm.schema import SchemaPool
from pyxsd.xpath.engine import XPathEngine
from pyxsd.xpath.environment import ExpressionEnvironment, Focus, XPathStaticContext
from pyxsd.xslt import CompiledStylesheet, TransformationResult, XsltProcessor
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltDynamicError, XsltError

#: Capabilities a native XSLT 2.0 processor implements in addition to its
#: language versions.  Streaming, higher-order functions, dynamic evaluation,
#: packages, maps, arrays, XML 1.1, and embedded stylesheet PIs are XSLT 3.0
#: features and stay outside this profile.
_XSLT20_FEATURES = frozenset(
    {
        "schema_aware",
        "serialization",
        "backwards_compatibility",
        "namespace_axis",
        "dtd",
        "disabling_output_escaping",
        "built_in_derived_types",
        "HTML4",
    }
)

#: Capabilities a native XSLT 1.0 processor implements.  Everything that
#: arrived with 2.0 or 3.0 is deliberately outside the profile.
_XSLT10_FEATURES = frozenset(
    {
        "namespace_axis",
        "dtd",
        "disabling_output_escaping",
        "HTML4",
    }
)

#: 3.0-only capabilities that both native profiles deliberately exclude.
_PRE_30_DECLINED = frozenset(
    {
        "streaming",
        "streaming-fallback",
        "higher_order_functions",
        "dynamic_evaluation",
        "packages",
        "maps",
        "arrays",
        "serialization",
        "schema_aware",
        "backwards_compatibility",
        "built_in_derived_types",
        "HTML5",
        "XML_1.1",
        "xsl-stylesheet-processing-instruction",
    }
)

#: A code-less engine exception is reported under this sentinel so an
#: expected error never masquerades as a matching code.
UNCODED_ERROR = "pyxsd-error"

#: The Unicode version of the character tables the engine grades against.
#:
#: It is read from the engine, never from a census, a baseline or a case list:
#: a catalog case that pins another version is not applicable to these
#: profiles.  The value moves only when the engine's tables move.
ENGINE_UNICODE_VERSION = unicode_version()

#: Headroom over a case budget before the parent treats a worker as wedged.
#: The in-worker alarm normally fires first; the parent deadline catches a
#: C-level hang that Python signals cannot interrupt.
_PARENT_GRACE = 5.0


class UnsupportedCase(Exception):
    """The harness cannot drive one catalog case's declared shape."""


@dataclass(frozen=True)
class SuiteProfile:
    """One conformance profile: catalog capabilities and processor identity."""

    name: str
    feature_profile: FeatureProfile
    processor: XsltProcessor

    def __post_init__(self) -> None:
        if self.feature_profile.name != self.name:
            raise ValueError("the feature profile name must match the suite profile")
        if self.processor.language != self.feature_profile.language:
            raise ValueError("the processor language must match the feature profile")
        if self.processor.xpath != self.feature_profile.xpath_version:
            raise ValueError("the processor XPath version must match the feature profile")


def _profile(
    name: str,
    language: str,
    xpath: str,
    xsd_version: str,
    features: frozenset[str],
    declined: frozenset[str],
    description: str,
    *,
    schema_aware: bool,
) -> SuiteProfile:
    return SuiteProfile(
        name=name,
        feature_profile=FeatureProfile(
            name=name,
            language=language,
            xpath_version=xpath,
            xsd_version=xsd_version,
            features=features,
            declined_features=declined - features,
            unicode_version=ENGINE_UNICODE_VERSION,
            description=description,
        ),
        processor=XsltProcessor(
            language=language,
            xpath=xpath,
            schema_aware=schema_aware,
            xsd_version=xsd_version,
        ),
    )


#: The three conformance profiles, keyed by their CI job and baseline name.
SUITE_PROFILES: Mapping[str, SuiteProfile] = {
    "xslt30": SuiteProfile(
        name="xslt30",
        feature_profile=replace(XSLT30_NON_STREAMING, unicode_version=ENGINE_UNICODE_VERSION),
        processor=XsltProcessor(language="3.0", xpath="3.1"),
    ),
    "xslt20": _profile(
        name="xslt20",
        language="2.0",
        xpath="2.0",
        xsd_version="1.0",
        features=_XSLT20_FEATURES,
        declined=_PRE_30_DECLINED,
        description="native schema-aware XSLT 2.0 with XPath 2.0 and XSD 1.0",
        schema_aware=True,
    ),
    "xslt10": _profile(
        name="xslt10",
        language="1.0",
        xpath="1.0",
        xsd_version="1.0",
        features=_XSLT10_FEATURES,
        declined=_PRE_30_DECLINED | _XSLT20_FEATURES,
        description="native XSLT 1.0 with XPath 1.0 result-tree fragments",
        schema_aware=False,
    ),
}


# ---------------------------------------------------------------------------
# Result conversion


def _document_xml(node: XdmNode) -> str:
    """One XDM tree as a deterministic XML string for the assertion layer."""

    def build(source: XdmNode) -> Any:
        element = ET.Element(source.name or source.local_name or "node")
        for attribute in source.attributes:
            element.set(attribute.name, attribute.string_value)
        for child in source.children:
            if child.kind is NodeKind.TEXT:
                if len(element):
                    element[-1].tail = (element[-1].tail or "") + child.string_value
                else:
                    element.text = (element.text or "") + child.string_value
            elif child.kind is NodeKind.ELEMENT:
                element.append(build(child))
            elif child.kind is NodeKind.COMMENT:
                element.append(ET.Comment(child.string_value))
            elif child.kind is NodeKind.PROCESSING_INSTRUCTION:
                element.append(ET.ProcessingInstruction(child.target or "", child.string_value))
        return element

    elements = [child for child in node.children if child.kind is NodeKind.ELEMENT]
    if len(elements) == 1 and len(node.children) == 1:
        return ET.tostring(build(elements[0]), encoding="unicode")
    return escape("".join(child.string_value for child in node.children))


def _value(item: Any) -> Any:
    if isinstance(item, XdmAtomic):
        return item.value
    if isinstance(item, XdmMap):
        return {key.value: _value_sequence(value) for key, value in item.entries}
    if isinstance(item, XdmArray):
        return [_value_sequence(member) for member in item.members]
    if isinstance(item, XdmFunction):
        return item
    if isinstance(item, XdmNode):
        return _document_xml(item)
    return item


def _value_sequence(sequence: Any) -> Any:
    values = tuple(_value(item) for item in sequence)
    return values[0] if len(values) == 1 else values


def _snapshot_entry(entry: Any) -> ResultSnapshot:
    method = entry.serialization.get("method")
    encoding = entry.serialization.get("encoding")
    if entry.tree is not None:
        return ResultSnapshot(
            xml=_document_xml(entry.tree),
            serialized=entry.serialized,
            serialization_method=method,
            serialization_encoding=encoding,
            messages=(),
            warnings=(),
        )
    return ResultSnapshot(
        raw=tuple(_value(item) for item in entry.raw),
        serialized=entry.serialized,
        serialization_method=method,
        serialization_encoding=encoding,
        messages=(),
        warnings=(),
    )


def _snapshot(result: TransformationResult) -> ResultSnapshot:
    entry = _snapshot_entry(result.principal_entry)
    messages = tuple(
        ResultSnapshot(raw=(message.text,), xml=message.text) for message in result.messages
    )
    return ResultSnapshot(
        raw=entry.raw,
        xml=entry.xml,
        serialized=entry.serialized,
        messages=messages,
        secondary={uri: _snapshot_entry(value) for uri, value in result.secondary.items()},
        warnings=(),
        serialization_method=entry.serialization_method,
        serialization_encoding=entry.serialization_encoding,
    )


# ---------------------------------------------------------------------------
# Invocation inputs


def _evaluate(
    select: str,
    xpath_version: str,
    *,
    focus: XdmDocument | XdmNode | None = None,
) -> tuple[Any, ...]:
    engine = XPathEngine(xpath_version)
    compiled = engine.compile(select, XPathStaticContext())
    resolved = None if focus is None else Focus(item=focus)
    return tuple(engine.evaluate(compiled, ExpressionEnvironment(), focus=resolved))


def _parameter_values(
    parameters: Iterable[Param],
    xpath_version: str,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.name is None or parameter.select is None:
            continue
        values[parameter.name] = _evaluate(parameter.select, xpath_version)
    return values


#: Catalog ``source/@validation`` values that name a processor validation mode.
#: The catalog's ``skip`` asks for no validation at all, which leaves exactly
#: the untyped document the ``strip`` default produces, so it names no mode.
_SOURCE_VALIDATION_MODES = frozenset({"strict", "lax"})

#: The XSLT code one failed validated-source assessment reports, keyed by the
#: document layer's reason.  A harness-evaluated selection validates its source
#: outside the invocation, so the harness reports the code the invocation would
#: have raised for the same assessment failure.
_VALIDATION_CODES = {
    "unknown": "XTTE1512",
    "invalid": "XTTE1510",
    "lax-invalid": "XTTE1515",
}


@dataclass(frozen=True)
class _SourceInput:
    """One invocation's source value and the validation it declares.

    ``validation`` is set only when the raw source itself is handed to the
    invocation: a selection is already evaluated over a document the harness
    parsed from that source.
    """

    value: Any
    validation: str | None = None


def _declared_validation(source: Source | None) -> str | None:
    """The processor validation mode *source* declares, or ``None``."""
    validation = None if source is None else source.validation
    return validation if validation in _SOURCE_VALIDATION_MODES else None


def source_validation(case: TestCase) -> str | None:
    """The context source's declared validation, or ``None`` for the default.

    ``None`` covers a case that omits ``@validation`` and one the catalog marks
    ``skip``: neither asks the processor to validate, so the source keeps the
    untyped document the processor's ``strip`` default produces.
    """
    return _declared_validation(_context_source(case))


def _context_source(case: TestCase) -> Source | None:
    sources = [source for source in case.environment.sources if source.role == "."]
    if not sources:
        return None
    if len(sources) > 1:
        raise UnsupportedCase("multiple context-item sources")
    return sources[0]


def _raw_source(case: TestCase, source: Source) -> Path | bytes:
    """One source in the form the invocation itself parses.

    Inline content stays bytes and a file stays its absolute corpus path, so
    the invocation parses — and, when the catalog declares it, validates — the
    document instead of inheriting an already-parsed untyped tree.
    """
    if case.document_root is None:
        raise UnsupportedCase("case without a corpus root")
    if source.content is not None:
        return source.content.encode("utf-8")
    if source.path is not None:
        return case.document_root / Path(str(source.path))
    raise UnsupportedCase("context source without a file or inline content")


def _parse_source(case: TestCase, source: Source) -> XdmDocument:
    """One source parsed for a selection the harness evaluates itself."""
    return XdmDocument.parse(_raw_source(case, source))


def _selection_document(
    case: TestCase,
    profile: SuiteProfile,
    source: Source,
    validation: str | None,
) -> XdmDocument:
    """The document a harness-evaluated selection reads in document order.

    The invocation validates only a source it parses itself, so a declared
    validation is assessed here through the engine's typed parse and the pool
    the case declares before the selection is made.
    """
    if validation is None:
        return _parse_source(case, source)
    try:
        return parse_typed_document(
            _raw_source(case, source),
            schema_pool=_case_schema_pool(profile, case),
            validation=validation,
        )
    except DocumentValidationError as exc:
        raise CaseExecutionError(_VALIDATION_CODES[exc.reason], str(exc)) from exc


def _source_input(case: TestCase, profile: SuiteProfile) -> _SourceInput | None:
    """The initial context item or match selection the catalog declares."""
    xpath_version = profile.processor.xpath
    if case.environment.context_item is not None:
        select = case.environment.context_item.select
        return _SourceInput(() if select is None else _evaluate(select, xpath_version))
    source = _context_source(case)
    mode = case.invocation.initial_mode
    if source is None:
        if mode is not None and mode.select:
            return _SourceInput(_evaluate(mode.select, xpath_version))
        return None
    select = source.select or (None if mode is None else mode.select)
    validation = _declared_validation(source)
    if select is None:
        return _SourceInput(_raw_source(case, source), validation)
    document = _selection_document(case, profile, source, validation)
    return _SourceInput(_evaluate(select, xpath_version, focus=document))


def _delivery(case: TestCase) -> str:
    """The engine delivery that yields every view this case's output asks for.

    ``<output serialize="yes">`` means serialization takes place, so it wins over
    ``tree``: only stylesheet delivery serializes, and it retains the raw
    sequence (and a tree when the stylesheet's build-tree allows one).  With no
    serialization, ``tree="no"`` is raw and ``tree="yes"`` is document; an
    unspecified tree keeps the existing stylesheet default.
    """
    output = case.invocation.output or case.environment.output
    if output is not None and output.serialize is not True and output.tree is False:
        return "raw"
    if output is not None and output.serialize is not True and output.tree is True:
        return "document"
    return "stylesheet"


def _base_output_uri(case: TestCase) -> str:
    """The absolute base output URI every relative result-document href uses.

    A declared output file is the base the catalog's own runner resolves
    ``xsl:result-document`` hrefs against.  Without one, a synthetic
    per-case principal output URI supplies the identity — the same convention
    the corpus runner uses — so a relative href always resolves and nothing is
    written.
    """
    output = case.invocation.output or case.environment.output
    root = _corpus_root(case)
    if output is not None and output.path is not None:
        return (root / Path(str(output.path))).as_uri()
    return (root / "__pyxsd_output__" / case.name / "principal-output.xml").as_uri()


def _principal_candidates(case: TestCase) -> tuple[Stylesheet, ...]:
    """The distinct non-secondary stylesheet declarations of one case.

    A catalog case may repeat one declaration — the environment and the test can
    both name the same file — and repeating one compilation unit must not look
    like two.  Declaration order is preserved.  A declaration without a file
    stays its own candidate: nothing says it is the same unit as another.
    """
    candidates: list[Stylesheet] = []
    seen: set[str] = set()
    for sheet in case.stylesheets:
        if sheet.role == "secondary":
            continue
        if sheet.path is not None:
            key = str(sheet.path)
            if key in seen:
                continue
            seen.add(key)
        candidates.append(sheet)
    return tuple(candidates)


def _declared_only_as_module(case: TestCase, selected: Stylesheet) -> bool:
    """Whether the environment declares the selected file as a module only.

    A test that names a file the environment declares ``secondary`` asks the
    harness to compile a module as the principal.  Only an environment
    declaration that also names the file non-secondary promotes it back, so a
    test can never promote a module by naming it.
    """
    if selected.path is None:
        return False
    path = str(selected.path)
    declared = [
        sheet
        for sheet in case.environment.stylesheets
        if sheet.path is not None and str(sheet.path) == path
    ]
    return bool(declared) and all(sheet.role == "secondary" for sheet in declared)


def _principal_declaration(case: TestCase) -> Stylesheet:
    """The stylesheet declaration one case compiles as its principal.

    A test may name the stylesheet it executes among several the environment
    offers, and an invocation stylesheet is a file reference rather than a
    selection: it may name a file the environment never declares.  With no
    named stylesheet a lone non-secondary declaration is the principal; several
    are never guessed.
    """
    named = [sheet for sheet in case.invocation.stylesheets if sheet.role != "secondary"]
    named_paths = {str(sheet.path) for sheet in named if sheet.path is not None}
    if len(named_paths) > 1:
        raise UnsupportedCase(f"{len(named_paths)} selected stylesheets")
    principals = _principal_candidates(case)
    if named:
        selected = named[0]
        if _declared_only_as_module(case, selected):
            raise UnsupportedCase("selected stylesheet is a secondary module")
    elif len(principals) != 1:
        raise UnsupportedCase(f"{len(principals)} principal stylesheets")
    else:
        selected = principals[0]
    if selected.path is None:
        raise UnsupportedCase("principal stylesheet without a file")
    return selected


def _corpus_root(case: TestCase) -> Path:
    """The corpus root every declared reference of one case is relative to."""
    if case.document_root is None:
        raise UnsupportedCase("case without a corpus root")
    return case.document_root


def _stylesheet(case: TestCase) -> Path:
    declaration = _principal_declaration(case)
    return _corpus_root(case) / Path(str(declaration.path))


@dataclass(frozen=True)
class _Units:
    """The compilation units one case compiles: a principal and its libraries.

    ``libraries`` are the case's secondary packages, in declaration order.  The
    principal is the module the invocation compiles; a library is a package the
    principal reaches through ``xsl:use-package``.
    """

    principal: Path
    libraries: tuple[Path, ...] = ()


def _package_paths(case: TestCase, root: Path, *, secondary: bool) -> tuple[Path, ...]:
    """The distinct corpus paths of one role's package declarations.

    A catalog case may repeat one declaration — the environment and the test can
    both name the same package file — and repeating one compilation unit must
    not look like two.  Declaration order is preserved.
    """
    paths: list[Path] = []
    for package in case.packages:
        if (package.role == "secondary") is not secondary:
            continue
        if package.path is None:
            raise UnsupportedCase(
                "secondary package without a file"
                if secondary
                else "principal package without a file"
            )
        path = root / Path(str(package.path))
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _compilation_units(case: TestCase) -> _Units:
    """The principal a case compiles plus the secondary packages it reaches.

    A case that declares packages compiles one principal package with every
    secondary package registered as a library.  A case that names no principal
    package still has a principal stylesheet — an ordinary ``xsl:stylesheet``
    may use packages — so that stylesheet is the principal.  A case with neither
    has no compilation unit at all.
    """
    if not case.packages:
        return _Units(principal=_stylesheet(case))
    root = _corpus_root(case)
    principals = _package_paths(case, root, secondary=False)
    if len(principals) > 1:
        raise UnsupportedCase(f"{len(principals)} principal packages")
    if principals:
        principal = principals[0]
    elif not any(sheet.role != "secondary" for sheet in case.stylesheets):
        raise UnsupportedCase("packages without a principal")
    else:
        principal = _stylesheet(case)
    libraries = _package_paths(case, root, secondary=True)
    return _Units(principal=principal, libraries=libraries)


# ---------------------------------------------------------------------------
# Declared collections


#: One application collection as the engine asks for it: an absolute URI, or
#: ``None`` for the default collection.
CollectionResolver = Callable[[str | None], Sequence[XdmNode]]


@dataclass(frozen=True)
class _Member:
    """One declared collection member: where its bytes are and its identity."""

    source: Source
    uri: str | None


def _collection_bases(case: TestCase, units: _Units) -> tuple[Path, ...]:
    """Every base a declared relative collection URI is resolved against.

    The engine resolves a collection URI against the canonical base URI of the
    module that calls it, so the principal's directory and every library's
    directory are bases; the test-set directory is where the declaration was
    written.  Each is canonical, so a symlinked checkout still matches.
    """
    bases: list[Path] = []
    for base in (
        units.principal.parent,
        *(library.parent for library in units.libraries),
        _corpus_root(case) / Path(str(case.source.parent)),
    ):
        resolved = base.resolve()
        if resolved not in bases:
            bases.append(resolved)
    return tuple(bases)


def _member_uri(root: Path, test_set: Path, source: Source) -> str | None:
    """The document URI one declared member is served under.

    A member's own ``uri`` is kept verbatim when absolute and resolved against
    the test-set directory otherwise.  A member that declares no URI still has
    the identity of the corpus file it names; inline content without a URI has
    none.
    """
    if source.uri is not None:
        if urlsplit(source.uri).scheme:
            return source.uri
        return urljoin(test_set.as_uri() + "/", source.uri)
    if source.path is not None:
        return (root / Path(str(source.path))).resolve().as_uri()
    return None


def _collection_resolver(case: TestCase, units: _Units) -> CollectionResolver | None:
    """A resolver serving the collections one case declares, or ``None``.

    Members are the local documents the declaration names, keyed by the
    absolute URI the engine resolves a call to; a relative declaration is
    registered under every module directory, because the caller's module is the
    base the engine uses.  Building the mapping reads no member: parsing is
    deferred to the first request, so a declaration a case never uses costs
    nothing and cannot fail.  A collection the case never declared stays
    unknown, and the engine reports its own ``FODC0002``.
    """
    declarations = case.environment.collections
    if not declarations:
        return None
    root = _corpus_root(case)
    test_set = (root / Path(str(case.source.parent))).resolve()
    bases = _collection_bases(case, units)
    mapping: dict[str | None, tuple[_Member, ...]] = {}
    for declaration in declarations:
        members = tuple(
            _Member(source=source, uri=_member_uri(root, test_set, source))
            for source in declaration.sources
        )
        if declaration.uri is None:
            keys: tuple[str | None, ...] = (None,)
        elif not members:
            # The engine reports an empty named collection as unavailable, so
            # the harness cannot serve the declaration it reads.
            raise UnsupportedCase("named collection without sources")
        elif urlsplit(declaration.uri).scheme:
            keys = (declaration.uri,)
        else:
            keys = tuple(
                dict.fromkeys(urljoin(base.as_uri() + "/", declaration.uri) for base in bases)
            )
        for key in keys:
            existing = mapping.get(key)
            if existing is not None and existing != members:
                raise UnsupportedCase(f"ambiguous collection {key!r}")
            mapping[key] = members
    parsed: dict[str | None, tuple[XdmNode, ...]] = {}

    def resolve(uri: str | None) -> tuple[XdmNode, ...]:
        members = mapping.get(uri)
        if members is None:
            return ()
        cached = parsed.get(uri)
        if cached is None:
            cached = tuple(_member_node(case, root, member) for member in members)
            parsed[uri] = cached
        return cached

    return resolve


def _member_node(case: TestCase, root: Path, member: _Member) -> XdmNode:
    """Parse one collection member, in the form its declaration describes.

    Only a local corpus file or inline content is ever read.  A member naming an
    ``xml:id`` fragment serves that element rather than its document.
    """
    source = member.source
    if source.content is None:
        if source.path is None:
            # A remote member with no local file lands here: it is never
            # fetched, so the harness cannot serve it.
            raise UnsupportedCase("collection source without a file or inline content")
        relative = Path(str(source.path))
        target = (root / relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not target.is_relative_to(root.resolve())
        ):
            raise UnsupportedCase("collection source outside the corpus root")
    document = XdmDocument.parse(_raw_source(case, source), document_uri=member.uri)
    if source.uri is None or "#" not in source.uri:
        return document
    fragment = source.uri.split("#", 1)[1]
    for candidate in (document.root_element, *document.root_element.descendants):
        identifier = candidate.attribute(f"{{{XML_NS}}}id")
        if identifier is not None and identifier.string_value == fragment:
            return candidate
    raise XsltDynamicError(
        f"the collection member {source.uri!r} does not exist",
        code="FODC0002",
    )


def _processor_for(
    profile: SuiteProfile,
    units: _Units,
    *,
    resolver: CollectionResolver | None = None,
) -> XsltProcessor:
    """The profile's processor with this case's libraries and collections.

    A case without libraries and without a collection resolver keeps the
    profile processor itself, so an ordinary stylesheet compiles exactly as
    before.  ``from_sources`` reads the local corpus files only: a package name
    is never dereferenced, so a lookup can never reach the network.
    """
    if not units.libraries and resolver is None:
        return profile.processor
    package_resolver = (
        PackageResolver.from_sources(*(str(path) for path in units.libraries))
        if units.libraries
        else profile.processor.resolver
    )
    return replace(
        profile.processor,
        resolver=package_resolver,
        collection_resolver=resolver,
    )


#: How many compiled stylesheets one worker keeps for reuse.  The bulk
#: conformance sets compile the same stylesheet for hundreds of consecutive
#: cases, so a handful of entries covers them without holding every artifact
#: of a whole profile in memory.
_COMPILED_CACHE_SIZE = 8

#: Compiled stylesheets by ``(profile, principal path, bytes digest,
#: environment fingerprint)``.  Only units compiled with no static parameters
#: are cached: the artifact is then a pure function of the compilation units,
#: the profile identity, and the environment the case declares.  The corpus is
#: pinned and immutable for a run, so the digest guards against a unit rewritten
#: between cases; it covers the principal and every library the case registers,
#: but not a module one of them reaches through ``xsl:include`` or
#: ``xsl:import``.  The fingerprint keeps two cases that share a principal apart
#: when their schema sets or package declarations differ.
_COMPILED: dict[tuple[str, str, bytes, str], CompiledStylesheet] = {}


def _closure_digest(units: _Units) -> bytes:
    """One digest over every unit the compile registers, principal first.

    Two cases with the same principal and different libraries must never share
    an artifact, so the libraries' bytes are part of the identity.  With no
    libraries the digest is the principal's own digest.
    """
    digest = hashlib.sha256(units.principal.read_bytes())
    for library in units.libraries:
        digest.update(library.read_bytes())
    return digest.digest()


def _compile_stylesheet(
    profile: SuiteProfile,
    units: _Units,
    static_parameters: Mapping[str, Any],
    *,
    schema: SchemaPool | None = None,
    environment: str = "",
    resolver: CollectionResolver | None = None,
) -> CompiledStylesheet:
    """Compile one case's compilation units, reusing an invariant artifact.

    *schema* is the case's compile-time schema pool and *environment* its
    fingerprint; both are part of the cache identity, so an artifact is never
    shared between cases whose declared schemas differ.  A case that declares a
    collection resolver compiles fresh: the artifact carries the resolver, and a
    cached one would bind a later case's members to this case's declarations.
    """
    if static_parameters or resolver is not None:
        return _processor_for(profile, units, resolver=resolver).compile(
            units.principal,
            static_parameters=static_parameters,
            schema=schema,
        )
    try:
        digest = _closure_digest(units)
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        # Let the compiler report a missing or unreadable unit itself.
        # `TimeoutError` is deliberately absent: it subclasses `OSError`, and a
        # budget that expires during this read must keep its alarm instead of
        # being mistaken for an unreadable file.
        return _processor_for(profile, units).compile(units.principal, schema=schema)
    key = (profile.name, str(units.principal), digest, environment)
    compiled = _COMPILED.get(key)
    if compiled is None:
        compiled = _processor_for(profile, units).compile(units.principal, schema=schema)
        if len(_COMPILED) >= _COMPILED_CACHE_SIZE:
            _COMPILED.pop(next(iter(_COMPILED)))
        _COMPILED[key] = compiled
    return compiled


def _case_schema_pool(profile: SuiteProfile, case: TestCase) -> SchemaPool | None:
    """The compile-time schema pool a case declares, for a schema-aware profile.

    A non-schema-aware processor refuses a schema outright, so a profile that
    does not declare schema awareness compiles without one.
    """
    if not profile.processor.schema_aware or not case.environment.schemas:
        return None
    return build_schema_pool(
        case,
        xsd_version=profile.processor.xsd_version,
        resource_policy=profile.processor.resource_policy,
    )


def execute_case(case: TestCase, profile: SuiteProfile) -> ResultSnapshot:
    """Compile and run one case, returning its result for independent assertion."""
    if case.environment.resources:
        raise UnsupportedCase("resources")
    output = case.invocation.output or case.environment.output
    if output is not None and output.result_var is not None:
        raise UnsupportedCase("result-variable output")
    units = _compilation_units(case)
    resolver = _collection_resolver(case, units)
    output_uri = _base_output_uri(case)
    invocation = case.invocation
    xpath_version = profile.processor.xpath
    declared_parameters = (*case.environment.params, *invocation.params)
    static_parameters = _parameter_values(
        (parameter for parameter in declared_parameters if parameter.static),
        xpath_version,
    )
    parameters = _parameter_values(
        (parameter for parameter in declared_parameters if not parameter.static),
        xpath_version,
    )
    template_parameters: dict[str, Any] = {}
    tunnel_parameters: dict[str, Any] = {}
    if invocation.initial_template is not None:
        for parameter in invocation.initial_template.params:
            if parameter.name is None or parameter.select is None:
                continue
            value = _evaluate(parameter.select, xpath_version)
            target = tunnel_parameters if parameter.tunnel else template_parameters
            target[parameter.name] = value
    elif invocation.initial_mode is not None:
        for parameter in invocation.initial_mode.params:
            if parameter.name is None or parameter.select is None:
                continue
            value = _evaluate(parameter.select, xpath_version)
            target = tunnel_parameters if parameter.tunnel else template_parameters
            target[parameter.name] = value
    arguments: tuple[Any, ...] = ()
    if invocation.initial_function is not None:
        arguments = tuple(
            value
            for parameter in invocation.initial_function.params
            if parameter.select is not None
            for value in (_evaluate(parameter.select, xpath_version),)
        )
    options: dict[str, Any] = {
        "parameters": parameters,
        "template_parameters": template_parameters,
        "tunnel_parameters": tunnel_parameters,
        "initial_template": (
            invocation.initial_template.name if invocation.initial_template is not None else None
        ),
        "initial_mode": invocation.initial_mode.name
        if invocation.initial_mode is not None
        else None,
        "initial_function": (
            invocation.initial_function.name if invocation.initial_function is not None else None
        ),
        "initial_function_arguments": arguments,
        "delivery": _delivery(case),
        "output_uri": output_uri,
    }
    try:
        source = _source_input(case, profile)
        if source is not None:
            options["source"] = source.value
            if source.validation is not None:
                options["validation"] = source.validation
        compiled = _compile_stylesheet(
            profile,
            units,
            static_parameters,
            schema=_case_schema_pool(profile, case),
            environment=environment_fingerprint(case),
            resolver=resolver,
        )
        return _snapshot(compiled.transform(**options))
    except XsltError as exc:
        raise CaseExecutionError(exc.code or UNCODED_ERROR, str(exc)) from exc


# ---------------------------------------------------------------------------
# Bounded execution


def _alarm_supported() -> bool:
    return hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")


@contextmanager
def case_budget(seconds: float | None):
    """Bound one case, or run unbounded when no budget is configured."""
    if seconds is None or not _alarm_supported():
        yield
        return

    def expire(signum: int, frame: Any) -> None:
        raise TimeoutError(f"the case exceeded its {seconds:g} s budget")

    try:
        previous = signal.signal(signal.SIGALRM, expire)
    except ValueError:  # not the main thread: no process-wide timer is available
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run_case(
    case: TestCase,
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
) -> CaseResult:
    """Run one case, classifying harness gaps and engine crashes honestly."""
    try:
        with case_budget(timeout):
            return run_cases(
                [case],
                lambda item: execute_case(item, profile),
                profile.feature_profile,
            )[0]
    except UnsupportedCase as exc:
        return CaseResult.adapter_gap(case.name, detail=str(exc))
    except TimeoutError as exc:
        return CaseResult.timeout(case.name, detail=str(exc))
    except Exception as exc:  # the engine raising is a verdict, not a harness abort
        return CaseResult.failed(case.name, message=f"{type(exc).__name__}: {exc}")


def run_suite(
    cases: Iterable[TestCase],
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
) -> tuple[CaseResult, ...]:
    """Run every case once, in catalog order."""
    return tuple(run_case(case, profile, timeout=timeout) for case in cases)


def with_unicode_version(profile: SuiteProfile, unicode_version: str) -> SuiteProfile:
    """*profile* with another declared Unicode version.

    The suite profile is the same processor identity; only the version the
    applicability gate compares moves.  An empty version makes the profile
    claim none, which is how a gated measurement reaches the cases the normal
    selection excludes.
    """
    if profile.feature_profile.unicode_version == unicode_version:
        return profile
    return SuiteProfile(
        name=profile.name,
        feature_profile=replace(profile.feature_profile, unicode_version=unicode_version),
        processor=profile.processor,
    )


def _run_payload(
    payload: tuple[str, str, TestCase, float | None],
) -> tuple[str, CaseResult]:
    """Worker entry point: the payload names the profile's gated identity.

    The profile name resolves to an immutable processor identity here, and the
    payload's Unicode version rebuilds the applicability gate the parent used,
    so a version-cleared measurement is not re-gated inside a worker.
    """
    profile_name, unicode_version, case, timeout = payload
    profile = with_unicode_version(SUITE_PROFILES[profile_name], unicode_version)
    return case.name, run_case(case, profile, timeout=timeout)


def default_jobs() -> int:
    """The worker count for a full corpus run, overridable for CI accounting."""
    configured = os.environ.get("PYXSD_XSLT_JOBS")
    if configured is None:
        return os.cpu_count() or 1
    return max(1, int(configured))


def run_parallel(
    cases: Iterable[TestCase],
    profile: SuiteProfile,
    *,
    timeout: float | None = None,
    jobs: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[CaseResult, ...]:
    """Run every case across worker processes, preserving catalog order.

    Workers receive the profile name rather than the profile object: the
    immutable processor configuration is not picklable, and resolving the
    checked-in profile by name is the same identity every worker executes.  The
    profile's declared Unicode version travels with the payload, so a worker
    gates exactly as the parent did.
    """
    cases = tuple(cases)
    workers = default_jobs() if jobs is None else max(1, jobs)
    if workers == 1 or len(cases) <= 1:
        return run_suite(cases, profile, timeout=timeout)
    unicode_version = profile.feature_profile.unicode_version
    method = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    context = multiprocessing.get_context(method)
    budget = None if timeout is None else timeout + _PARENT_GRACE
    results: dict[str, CaseResult] = {}
    pending = list(cases)
    pool = context.Pool(processes=workers)
    try:
        while pending:
            batch = pending[:workers]
            pending = pending[workers:]
            submitted = [
                (
                    case,
                    pool.apply_async(
                        _run_payload, ((profile.name, unicode_version, case, timeout),)
                    ),
                )
                for case in batch
            ]
            for case, handle in submitted:
                try:
                    name, result = handle.get(timeout=budget)
                except multiprocessing.TimeoutError:
                    results[case.name] = CaseResult.timeout(
                        case.name,
                        detail=f"the case exceeded its {timeout:g} s budget",
                    )
                    # A C-level hang ignores the in-worker alarm.  The other
                    # workers in this batch were terminated with the pool, so
                    # every uncollected case runs again in a fresh pool.
                    pending[0:0] = [
                        other
                        for other, other_result in submitted
                        if other.name != case.name and other.name not in results
                    ]
                    pool.terminate()
                    pool.join()
                    pool = context.Pool(processes=workers)
                    break
                else:
                    results[name] = result
                    if on_progress is not None:
                        on_progress(len(results), len(cases))
    finally:
        pool.close()
        pool.join()
    return tuple(results[case.name] for case in cases)
