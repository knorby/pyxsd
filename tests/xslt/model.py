"""Metadata model for the W3C XSLT 3.0 test catalog.

These objects are a lossless-enough description of the catalog's metadata:
declaration kinds and roles, resolved corpus-relative paths, invocation
parameters and entry points, declared dependencies, and the expected-result
fragment.  They deliberately contain no execution or XPath machinery; the
future engine is never imported here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

#: Namespace of the XSLT 3.0 test catalog and test-set documents.
CATALOG_NS = "http://www.w3.org/2012/10/xslt-test-catalog"

#: Namespace of ``xml:id``, ``xml:base`` and ``xml:lang``.
XML_NS = "http://www.w3.org/XML/1998/namespace"

#: Namespace of XLink attributes; older catalog documents use it for hrefs.
XLINK_NS = "http://www.w3.org/1999/xlink"


@dataclass(frozen=True)
class Source:
    """A ``source`` declaration, including its effective role.

    ``role`` is the declared ``@role`` when present.  When it is absent the
    schema gives the declaration no special role, so the element kind
    (``"source"``) is the effective role: an ordinary document reachable with
    ``doc()``.  A context-item source keeps its declared ``"."``.
    """

    role: str
    file: str | None = None
    path: PurePosixPath | None = None
    uri: str | None = None
    select: str | None = None
    validation: str | None = None
    streaming: bool = False
    xinclude: bool = False
    defines_stylesheet: bool = False
    xml_version: str | None = None
    context: str | None = None
    content: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Schema:
    """A ``schema`` declaration."""

    role: str
    file: str | None = None
    path: PurePosixPath | None = None
    uri: str | None = None
    xsd_version: str = "1.0"
    description: str = ""


@dataclass(frozen=True)
class Resource:
    """A ``resource`` declaration for ``unparsed-text``-style access."""

    role: str
    file: str | None = None
    path: PurePosixPath | None = None
    uri: str | None = None
    media_type: str | None = None
    encoding: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Collection:
    """A ``collection`` declaration and the sources that make it up."""

    role: str = "collection"
    uri: str | None = None
    sources: tuple[Source, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Package:
    """A ``package`` declaration used as the compilation unit."""

    role: str
    file: str | None = None
    path: PurePosixPath | None = None
    uri: str | None = None
    name: str | None = None
    package_version: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Stylesheet:
    """A ``stylesheet`` declaration used as the compilation unit."""

    role: str
    file: str | None = None
    path: PurePosixPath | None = None
    uri: str | None = None
    xml_version: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Param:
    """A parameter supplied to a transform or to an initial entry point.

    ``name`` is ``None`` for the positional arguments of an initial function.
    ``as_type`` is the catalog's documentation-only ``@as`` hint.
    """

    name: str | None = None
    select: str | None = None
    as_type: str | None = None
    source: str | None = None
    static: bool = False
    tunnel: bool = False


@dataclass(frozen=True)
class ContextItem:
    """The initial context item expression declared by an environment."""

    select: str | None = None


@dataclass(frozen=True)
class Collation:
    """A collation declaration inside an environment."""

    uri: str | None = None
    default: bool = False


@dataclass(frozen=True)
class Output:
    """Post-processing options for the principal result."""

    file: str | None = None
    path: PurePosixPath | None = None
    tree: bool | None = None
    serialize: bool | None = None
    well_formed: bool | None = None
    result_var: str | None = None


@dataclass(frozen=True)
class InitialTemplate:
    """An ``initial-template`` entry point and its tunnel parameters."""

    name: str | None = None
    params: tuple[Param, ...] = ()


@dataclass(frozen=True)
class InitialMode:
    """An ``initial-mode`` entry point, match selection and parameters."""

    name: str | None = None
    select: str | None = None
    params: tuple[Param, ...] = ()


@dataclass(frozen=True)
class InitialFunction:
    """An ``initial-function`` entry point and positional arguments."""

    name: str | None = None
    params: tuple[Param, ...] = ()


@dataclass(frozen=True)
class Invocation:
    """Everything in a test case's ``test`` element."""

    packages: tuple[Package, ...] = ()
    stylesheets: tuple[Stylesheet, ...] = ()
    params: tuple[Param, ...] = ()
    initial_template: InitialTemplate | None = None
    initial_mode: InitialMode | None = None
    initial_function: InitialFunction | None = None
    output: Output | None = None
    posture_and_sweep: str | None = None

    @property
    def modules(self) -> tuple[Package | Stylesheet, ...]:
        """The compilation units in declaration order (packages first)."""
        return self.packages + self.stylesheets


@dataclass(frozen=True)
class Environment:
    """The declarations available to a test case.

    A test-case ``environment`` that names a shared environment is resolved at
    parse time: this object carries both the shared declarations and any local
    additions.  ``name`` and ``ref`` record the declaration as written.
    """

    name: str | None = None
    ref: str | None = None
    sources: tuple[Source, ...] = ()
    schemas: tuple[Schema, ...] = ()
    resources: tuple[Resource, ...] = ()
    collections: tuple[Collection, ...] = ()
    packages: tuple[Package, ...] = ()
    stylesheets: tuple[Stylesheet, ...] = ()
    params: tuple[Param, ...] = ()
    context_item: ContextItem | None = None
    collations: tuple[Collation, ...] = ()
    output: Output | None = None


@dataclass(frozen=True)
class Dependency:
    """One child of a ``dependencies`` element.

    ``kind`` is the element's local name; ``value`` and ``satisfied`` retain
    the corresponding attributes.  ``satisfied`` is ``None`` when the catalog
    omits it, which means the feature is required in the usual sense.
    """

    kind: str
    value: str | None = None
    satisfied: bool | None = None


@dataclass(frozen=True)
class TestCase:
    """One catalog test case."""

    name: str
    local_name: str
    test_set: str
    description: str
    source: PurePosixPath
    environment: Environment
    invocation: Invocation
    dependencies: tuple[Dependency, ...] = ()
    result_xml: str = ""
    early_exit_possible: bool = False
    covers: tuple[str, ...] = ()
    document_root: Path | None = None
    result_base: PurePosixPath | None = None
    namespace_scopes: tuple[tuple[tuple[str, str], ...], ...] = ()

    @property
    def specs(self) -> tuple[str, ...]:
        """Every whitespace-separated spec token, in declaration order."""
        return tuple(
            token
            for dependency in self.dependencies
            if dependency.kind == "spec"
            for token in (dependency.value or "").split()
        )

    @property
    def spec(self) -> str | None:
        """The first declared spec token, or ``None``."""
        specs = self.specs
        return specs[0] if specs else None

    @property
    def features(self) -> frozenset[str]:
        """Feature names required by the case (``satisfied`` is not false)."""
        return frozenset(
            dependency.value
            for dependency in self.dependencies
            if dependency.kind == "feature"
            and dependency.value is not None
            and dependency.satisfied is not False
        )

    @property
    def feature_exclusions(self) -> frozenset[str]:
        """Feature names the case declares as explicitly not required."""
        return frozenset(
            dependency.value
            for dependency in self.dependencies
            if dependency.kind == "feature"
            and dependency.value is not None
            and dependency.satisfied is False
        )

    @property
    def packages(self) -> tuple[Package, ...]:
        """Environment packages followed by invocation packages."""
        return self.environment.packages + self.invocation.packages

    @property
    def stylesheets(self) -> tuple[Stylesheet, ...]:
        """Environment stylesheets followed by invocation stylesheets."""
        return self.environment.stylesheets + self.invocation.stylesheets

    @property
    def stylesheet_role(self) -> str | None:
        """The effective role of the principal stylesheet.

        The catalog treats a stylesheet as principal unless it is explicitly
        ``role="secondary"``; a case with no stylesheet has no role.
        """
        stylesheets = self.stylesheets
        if not stylesheets:
            return None
        if any(sheet.role != "secondary" for sheet in stylesheets):
            return "principal"
        return "secondary"

    @property
    def source_document(self) -> bool:
        """Whether the environment declares a source document."""
        return bool(self.environment.sources)


@dataclass(frozen=True)
class Catalog:
    """A parsed catalog, its corpus identity and every reachable test case."""

    path: Path
    root: Path
    corpus_revision: str
    cases: dict[str, TestCase]
    test_sets: tuple[str, ...] = ()
    shared_environments: dict[str, Environment] = field(default_factory=dict)
    metadata_errors: tuple[str, ...] = ()

    def document(self, reference: PurePosixPath | str) -> Path:
        """Absolute path of a corpus-relative document reference."""
        return self.root / Path(str(reference))

    def result_element(self, case: TestCase) -> ET.Element:
        """Parse a case's expected-result fragment."""
        return ET.fromstring(case.result_xml)
