"""Parser for the pinned W3C XSLT 3.0 test catalog.

The catalog is a tree of metadata documents: a master ``catalog`` lists
``test-set`` documents, and each test-set lists its shared environments and
test cases.  This reader follows those references relative to the document
that declares them, resolves shared-environment references, and records the
result without importing or consulting the XSLT implementation under test.

The corpus lives in a pinned git submodule at ``tests/xslt/corpus``.  Catalog
parsing works without the checkout (for synthetic fixtures); the root document
is the only thing that must exist.
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from tests.xslt.model import (
    CATALOG_NS,
    XML_NS,
    Catalog,
    Collation,
    Collection,
    ContextItem,
    Dependency,
    Environment,
    InitialFunction,
    InitialMode,
    InitialTemplate,
    Invocation,
    Output,
    Package,
    Param,
    Resource,
    Schema,
    Source,
    Stylesheet,
    TestCase,
)

#: The pinned corpus submodule, relative to the repository root.
CORPUS_PATH = "tests/xslt/corpus"

#: Repository root, so git queries work from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where the corpus can be checked out.
CORPUS_COMMAND = "git submodule update --init tests/xslt/corpus"


class CatalogError(Exception):
    """Raised when catalog metadata cannot be read or interpreted."""


def local_name(tag: str) -> str:
    """The local part of an expanded XML name."""
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str | None:
    if not tag.startswith("{"):
        return None
    return tag[1:].split("}", 1)[0]


def _in_catalog_namespace(tag: str) -> bool:
    """Whether *tag* belongs to the catalog vocabulary.

    Prefixes are irrelevant.  Documents in the wild use the catalog namespace
    as their default namespace; no-namespace documents are accepted as well so
    hand-written fixtures stay readable.
    """
    namespace = _namespace(tag)
    return namespace is None or namespace == CATALOG_NS


def _elements(element: ET.Element, name: str) -> tuple[ET.Element, ...]:
    return tuple(
        child
        for child in element
        if isinstance(child.tag, str)
        and local_name(child.tag) == name
        and _in_catalog_namespace(child.tag)
    )


def _element(element: ET.Element, name: str) -> ET.Element | None:
    for child in _elements(element, name):
        return child
    return None


def _attribute(element: ET.Element, name: str) -> str | None:
    value = element.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _role(element: ET.Element, kind: str) -> str:
    """The declared role, or the declaration kind when no role is given.

    ``source`` is the schema's ordinary document role when ``@role`` is
    absent; the same rule keeps package and stylesheet declarations
    distinguishable in a mixed view without inventing a principal/secondary
    role the catalog did not state.
    """
    return _attribute(element, "role") or kind


def _description(element: ET.Element) -> str:
    description = _element(element, "description")
    if description is None or description.text is None:
        return ""
    return description.text.strip()


def _content(element: ET.Element) -> str | None:
    content = _element(element, "content")
    if content is None:
        return None
    return content.text or ""


def _optional_bool(value: str | None) -> bool | None:
    """Parse an XML boolean, preserving absence as ``None``."""
    if value is None:
        return None
    if value.strip().lower() in {"true", "1", "yes"}:
        return True
    if value.strip().lower() in {"false", "0", "no"}:
        return False
    return None


def _bool(value: str | None) -> bool:
    return _optional_bool(value) is True


def resolve_reference(base: PurePosixPath, href: str | None) -> PurePosixPath | None:
    """Resolve a relative reference against a corpus-relative base.

    Fragments and query strings are not file references.  Absolute URLs and
    absolute paths are rejected; paths that escape the corpus root are clamped
    to it so a malformed catalog cannot address files outside the checkout.
    """
    if href is None:
        return None
    href = href.strip()
    if not href or "://" in href or href.startswith(("/", "data:", "urn:", "file:")):
        return None
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None
    parts: list[str] = []
    for part in (base / href).parts:
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts)


def _base(element: ET.Element, base: PurePosixPath) -> PurePosixPath:
    """*base* adjusted by the element's ``xml:base``, when usable."""
    declared = element.get(f"{{{XML_NS}}}base")
    resolved = resolve_reference(base, declared)
    return resolved if resolved is not None else base


def _parse_source(element: ET.Element, base: PurePosixPath) -> Source:
    file = _attribute(element, "file")
    return Source(
        role=_role(element, "source"),
        file=file,
        path=resolve_reference(_base(element, base), file),
        uri=_attribute(element, "uri"),
        select=_attribute(element, "select"),
        validation=_attribute(element, "validation"),
        streaming=_bool(element.get("streaming")),
        xinclude=_bool(element.get("xinclude")),
        defines_stylesheet=_bool(element.get("defines-stylesheet")),
        xml_version=_attribute(element, "xml-version"),
        context=_attribute(element, "context"),
        content=_content(element),
        description=_description(element),
    )


def _parse_schema(element: ET.Element, base: PurePosixPath) -> Schema:
    file = _attribute(element, "file")
    return Schema(
        role=_role(element, "schema"),
        file=file,
        path=resolve_reference(_base(element, base), file),
        uri=_attribute(element, "uri"),
        xsd_version=_attribute(element, "xsd-version") or "1.0",
        description=_description(element),
    )


def _parse_resource(element: ET.Element, base: PurePosixPath) -> Resource:
    file = _attribute(element, "file")
    return Resource(
        role=_role(element, "resource"),
        file=file,
        path=resolve_reference(_base(element, base), file),
        uri=_attribute(element, "uri"),
        media_type=_attribute(element, "media-type"),
        encoding=_attribute(element, "encoding"),
        description=_description(element),
    )


def _parse_collection(element: ET.Element, base: PurePosixPath) -> Collection:
    collection_base = _base(element, base)
    return Collection(
        role=_role(element, "collection"),
        uri=_attribute(element, "uri"),
        sources=tuple(
            _parse_source(source, collection_base) for source in _elements(element, "source")
        ),
        description=_description(element),
    )


def _parse_package(element: ET.Element, base: PurePosixPath) -> Package:
    file = _attribute(element, "file")
    return Package(
        role=_role(element, "package"),
        file=file,
        path=resolve_reference(_base(element, base), file),
        uri=_attribute(element, "uri"),
        name=_attribute(element, "name"),
        package_version=_attribute(element, "package-version"),
        description=_description(element),
    )


def _parse_stylesheet(element: ET.Element, base: PurePosixPath) -> Stylesheet:
    file = _attribute(element, "file")
    return Stylesheet(
        role=_role(element, "stylesheet"),
        file=file,
        path=resolve_reference(_base(element, base), file),
        uri=_attribute(element, "uri"),
        xml_version=_attribute(element, "xml-version"),
        description=_description(element),
    )


def _parse_param(element: ET.Element) -> Param:
    return Param(
        name=_attribute(element, "name"),
        select=_attribute(element, "select"),
        as_type=_attribute(element, "as"),
        source=_attribute(element, "source"),
        static=_bool(element.get("static")),
        tunnel=_bool(element.get("tunnel")),
    )


def _parse_params(element: ET.Element) -> tuple[Param, ...]:
    return tuple(_parse_param(param) for param in _elements(element, "param"))


def _parse_output(element: ET.Element, base: PurePosixPath) -> Output:
    file = _attribute(element, "file")
    path = None if file in (None, "#absent") else resolve_reference(_base(element, base), file)
    return Output(
        file=file,
        path=path,
        tree=_optional_bool(element.get("tree")),
        serialize=_optional_bool(element.get("serialize")),
        well_formed=_optional_bool(element.get("well-formed")),
        result_var=_attribute(element, "result-var"),
    )


def _parse_collation(element: ET.Element) -> Collation:
    return Collation(uri=_attribute(element, "uri"), default=_bool(element.get("default")))


def _parse_invocation(element: ET.Element, base: PurePosixPath) -> Invocation:
    invocation_base = _base(element, base)
    initial_template = None
    if (template := _element(element, "initial-template")) is not None:
        initial_template = InitialTemplate(
            name=_attribute(template, "name"),
            params=_parse_params(template),
        )
    initial_mode = None
    if (mode := _element(element, "initial-mode")) is not None:
        initial_mode = InitialMode(
            name=_attribute(mode, "name"),
            select=_attribute(mode, "select"),
            params=_parse_params(mode),
        )
    initial_function = None
    if (function := _element(element, "initial-function")) is not None:
        initial_function = InitialFunction(
            name=_attribute(function, "name"),
            params=_parse_params(function),
        )
    output = None
    if (output_element := _element(element, "output")) is not None:
        output = _parse_output(output_element, invocation_base)
    posture_and_sweep = None
    if (posture := _element(element, "posture-and-sweep")) is not None:
        posture_and_sweep = ET.tostring(posture, encoding="unicode").strip()
    return Invocation(
        packages=tuple(
            _parse_package(package, invocation_base) for package in _elements(element, "package")
        ),
        stylesheets=tuple(
            _parse_stylesheet(stylesheet, invocation_base)
            for stylesheet in _elements(element, "stylesheet")
        ),
        params=_parse_params(element),
        initial_template=initial_template,
        initial_mode=initial_mode,
        initial_function=initial_function,
        output=output,
        posture_and_sweep=posture_and_sweep,
    )


def _parse_environment(element: ET.Element, base: PurePosixPath) -> Environment:
    environment_base = _base(element, base)
    return Environment(
        name=_attribute(element, "name"),
        ref=_attribute(element, "ref"),
        sources=tuple(
            _parse_source(source, environment_base) for source in _elements(element, "source")
        ),
        schemas=tuple(
            _parse_schema(schema, environment_base) for schema in _elements(element, "schema")
        ),
        resources=tuple(
            _parse_resource(resource, environment_base)
            for resource in _elements(element, "resource")
        ),
        collections=tuple(
            _parse_collection(collection, environment_base)
            for collection in _elements(element, "collection")
        ),
        packages=tuple(
            _parse_package(package, environment_base) for package in _elements(element, "package")
        ),
        stylesheets=tuple(
            _parse_stylesheet(stylesheet, environment_base)
            for stylesheet in _elements(element, "stylesheet")
        ),
        params=_parse_params(element),
        context_item=(
            ContextItem(select=_attribute(context, "select"))
            if (context := _element(element, "context-item")) is not None
            else None
        ),
        collations=tuple(
            _parse_collation(collation) for collation in _elements(element, "collation")
        ),
        output=(
            _parse_output(output, environment_base)
            if (output := _element(element, "output")) is not None
            else None
        ),
    )


def _parse_dependencies(element: ET.Element) -> tuple[Dependency, ...]:
    dependencies = _element(element, "dependencies")
    if dependencies is None:
        return ()
    return tuple(
        Dependency(
            kind=local_name(child.tag),
            value=_attribute(child, "value"),
            satisfied=_optional_bool(child.get("satisfied")),
        )
        for child in dependencies
        if isinstance(child.tag, str) and _in_catalog_namespace(child.tag)
    )


def _merge_params(base: tuple[Param, ...], overlay: tuple[Param, ...]) -> tuple[Param, ...]:
    replaced = {param.name for param in overlay}
    return tuple(param for param in base if param.name not in replaced) + overlay


def _merge_environments(base: Environment, overlay: Environment) -> Environment:
    """Shared declarations followed by the test case's local additions."""
    return Environment(
        name=overlay.name or base.name,
        ref=overlay.ref,
        sources=base.sources + overlay.sources,
        schemas=base.schemas + overlay.schemas,
        resources=base.resources + overlay.resources,
        collections=base.collections + overlay.collections,
        packages=base.packages + overlay.packages,
        stylesheets=base.stylesheets + overlay.stylesheets,
        params=_merge_params(base.params, overlay.params),
        context_item=overlay.context_item or base.context_item,
        collations=base.collations + overlay.collations,
        output=overlay.output or base.output,
    )


class _CatalogLoader:
    """Read one catalog tree into a flat case map."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.cases: dict[str, TestCase] = {}
        self.test_sets: list[str] = []
        self.shared_environments: dict[str, Environment] = {}
        self.metadata_errors: list[str] = []
        self._active: list[Path] = []
        self._unnamed = 0

    def load(self, path: Path) -> None:
        self._load_document(path, {})

    def _document_path(self, path: Path) -> PurePosixPath:
        try:
            return PurePosixPath(*path.relative_to(self.root).parts)
        except ValueError:
            return PurePosixPath(path.name)

    def _error(self, message: str) -> None:
        self.metadata_errors.append(message)

    def _load_document(
        self,
        path: Path,
        inherited: dict[str, Environment],
        fallback_set_name: str | None = None,
    ) -> None:
        path = path.resolve()
        if not path.is_file():
            raise CatalogError(f"cannot read test-set document {path}: file not found")
        if path in self._active:
            raise CatalogError(f"cyclic catalog include: {path}")
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise CatalogError(f"cannot read catalog document {path}: {exc}") from exc
        if not isinstance(root.tag, str) or not _in_catalog_namespace(root.tag):
            raise CatalogError(f"{path}: not an XSLT test catalog document")
        document = self._document_path(path)
        base = _base(root, document.parent)
        self._active.append(path)
        try:
            kind = local_name(root.tag)
            if kind == "catalog":
                self._read_catalog(root, document, base, inherited)
            elif kind == "test-set":
                self._read_test_set(root, document, base, inherited, fallback_set_name)
            else:
                raise CatalogError(f"{document}: expected <catalog> or <test-set>, found <{kind}>")
        finally:
            self._active.pop()

    def _read_catalog(
        self,
        root: ET.Element,
        document: PurePosixPath,
        base: PurePosixPath,
        inherited: dict[str, Environment],
    ) -> None:
        environments = dict(inherited)
        declared: set[str] = set()
        for element in _elements(root, "environment"):
            environment = _parse_environment(element, base)
            if environment.name is None:
                self._error(f"{document}: environment without a name")
                continue
            if environment.name in declared:
                self._error(f"{document}: duplicate environment {environment.name!r}")
            declared.add(environment.name)
            environments[environment.name] = environment
            self.shared_environments[environment.name] = environment
        for reference in _elements(root, "test-set"):
            self._follow_test_set(reference, document, base, environments)

    def _read_test_set(
        self,
        root: ET.Element,
        document: PurePosixPath,
        base: PurePosixPath,
        inherited: dict[str, Environment],
        fallback_set_name: str | None,
    ) -> None:
        name = _attribute(root, "name") or fallback_set_name or ""
        if not name:
            self._error(f"{document}: test-set without a name")
        environments = dict(inherited)
        declared: set[str] = set()
        for element in _elements(root, "environment"):
            environment = _parse_environment(element, base)
            if environment.name is None:
                self._error(f"{document}: environment without a name")
                continue
            if environment.name in declared:
                self._error(f"{document}: duplicate environment {environment.name!r}")
            declared.add(environment.name)
            environments[environment.name] = environment
        set_dependencies = _parse_dependencies(root)
        self.test_sets.append(name)
        for element in _elements(root, "test-case"):
            self._read_case(element, document, base, name, environments, set_dependencies)
        for reference in _elements(root, "test-set"):
            self._follow_test_set(reference, document, base, environments)

    def _follow_test_set(
        self,
        reference: ET.Element,
        document: PurePosixPath,
        base: PurePosixPath,
        environments: dict[str, Environment],
    ) -> None:
        name = _attribute(reference, "name")
        file = _attribute(reference, "file")
        if file is None:
            raise CatalogError(f"{document}: test-set {name!r} has no file attribute")
        target = resolve_reference(_base(reference, base), file)
        if target is None:
            raise CatalogError(f"{document}: test-set {name!r} has an unusable file {file!r}")
        self._load_document(self.root / target.as_posix(), environments, name)

    def _read_case(
        self,
        element: ET.Element,
        document: PurePosixPath,
        base: PurePosixPath,
        set_name: str,
        environments: dict[str, Environment],
        set_dependencies: tuple[Dependency, ...],
    ) -> None:
        local_name_value = _attribute(element, "name")
        if local_name_value is None:
            self._unnamed += 1
            local_name_value = f"unnamed-{self._unnamed}"
            self._error(f"{document}: test-case without a name in {set_name!r}")
        name = f"{set_name}/{local_name_value}"
        case_base = _base(element, base)
        environment_element = _element(element, "environment")
        environment = Environment()
        if environment_element is not None:
            environment = _parse_environment(environment_element, case_base)
            if environment.ref:
                shared = environments.get(environment.ref)
                if shared is None:
                    self._error(
                        f"{document}: test case {name!r} references unknown environment "
                        f"{environment.ref!r}"
                    )
                else:
                    environment = _merge_environments(shared, environment)
        test_element = _element(element, "test")
        if test_element is None:
            self._error(f"{document}: test case {name!r} has no <test> element")
            invocation = Invocation()
        else:
            invocation = _parse_invocation(test_element, case_base)
        result_element = _element(element, "result")
        if result_element is None:
            self._error(f"{document}: test case {name!r} has no <result> element")
            result_xml = ""
            early_exit = False
        else:
            result_xml = ET.tostring(result_element, encoding="unicode").strip()
            early_exit = _bool(result_element.get("early-exit-possible"))
        covers = tuple((_attribute(element, "covers") or "").split())
        if name in self.cases:
            existing = self.cases[name]
            raise CatalogError(f"duplicate test case {name!r} in {existing.source} and {document}")
        self.cases[name] = TestCase(
            name=name,
            local_name=local_name_value,
            test_set=set_name,
            description=_description(element),
            source=document,
            environment=environment,
            invocation=invocation,
            dependencies=set_dependencies + _parse_dependencies(element),
            result_xml=result_xml,
            early_exit_possible=early_exit,
            covers=covers,
        )


def _git_revision(arguments: list[str]) -> str:
    """Run one git query, returning a full object name or ``""``."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    value = completed.stdout.strip()
    return value if re.fullmatch(r"[0-9a-fA-F]{40,64}", value) else ""


def corpus_commit() -> str:
    """The XSLT corpus revision identity.

    Prefer the commit actually checked out in the submodule; fall back to the
    gitlink recorded by the superproject so identity survives a checkout that
    has not initialized the corpus.  An exported tree without git yields ``""``.
    """
    checkout = _git_revision(["-C", str(REPO_ROOT / CORPUS_PATH), "rev-parse", "HEAD"])
    if checkout:
        return checkout
    return _git_revision(["rev-parse", f"HEAD:{CORPUS_PATH}"])


def load_catalog(path: Path) -> Catalog:
    """Parse the catalog document at *path* and every test set it includes.

    A missing root document raises :class:`CatalogError` with the command that
    initializes the corpus.  Duplicate case identifiers are rejected; other
    metadata problems are retained in ``Catalog.metadata_errors`` so a report
    can distinguish a broken corpus from an empty one.
    """
    path = Path(path)
    if not path.is_file():
        raise CatalogError(
            f"cannot read XSLT catalog {path}: file not found; "
            f"initialize the pinned corpus with {CORPUS_COMMAND!r}"
        )
    loader = _CatalogLoader(path.parent)
    loader.load(path)
    return Catalog(
        path=path,
        root=loader.root,
        corpus_revision=corpus_commit(),
        cases=loader.cases,
        test_sets=tuple(loader.test_sets),
        shared_environments=loader.shared_environments,
        metadata_errors=tuple(loader.metadata_errors),
    )
