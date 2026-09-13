"""Reader for the W3C XML Schema Test Suite catalogue.

The corpus lives in a pinned git submodule at ``tests/xsts/corpus``.  Its
metadata is described by ``common/xsts.xsd`` and is arranged as::

    suite.xml
      ts:testSetRef xlink:href=...   -> one .testSet document
        ts:testGroup                  -> one or more tests
          ts:schemaTest               -> 0..1 per group, 0..n schema documents
          ts:instanceTest             -> 0..n per group, one instance document

Every document reference is resolved relative to the file that contains it
(XML Base aware).  Test identifiers are the stable ``set/group/test`` triple.
Tag matching is namespace-aware but prefix-agnostic: NIST contributes a
``testSet`` using the test-suite namespace as the default namespace, while
other contributors use a ``ts:`` prefix; both parse to the same expanded name.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

TS_NS = "http://www.w3.org/XML/2004/xml-schema-test-suite/"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

#: Expected-outcome values defined by ``common/xsts.xsd``.
VALIDITY_VALUES = frozenset(
    {
        "valid",
        "invalid",
        "invalid-latent",
        "notKnown",
        "runtime-schema-error",
        "implementation-defined",
        "implementation-dependent",
        "indeterminate",
    }
)

#: ``current/@status`` values defined by ``common/xsts.xsd``.
STATUS_VALUES = frozenset(
    {
        "submitted",
        "accepted",
        "stable",
        "queried",
        "disputed-test",
        "disputed-spec",
    }
)


class CatalogError(Exception):
    """Raised when catalogue metadata cannot be interpreted."""


def local_name(tag: str) -> str:
    """The local part of an expanded XML name."""
    return tag.rsplit("}", 1)[-1]


def _in_suite_namespace(tag: str) -> bool:
    return tag.startswith(f"{{{TS_NS}}}") or not tag.startswith("{")


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for candidate in element:
        if local_name(candidate.tag) == name and _in_suite_namespace(candidate.tag):
            return candidate
    return None


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for candidate in element:
        if local_name(candidate.tag) == name and _in_suite_namespace(candidate.tag):
            yield candidate


def _tokens(value: str | None) -> tuple[str, ...]:
    """Whitespace-separated version tokens (``xsd:list`` of ``version-token``)."""
    if not value:
        return ()
    return tuple(value.split())


def _href(element: ET.Element) -> str | None:
    value = element.get(f"{{{XLINK_NS}}}href")
    if value is None:
        value = element.get("href")
    return value


def _base(element: ET.Element) -> str | None:
    return element.get(f"{{{XML_NS}}}base")


def resolve_reference(base_dir: PurePosixPath, href: str | None) -> PurePosixPath | None:
    """Resolve an ``xlink:href`` against a corpus-relative base directory.

    Fragments are dropped.  Absolute URLs and empty references return
    ``None`` — they are not local documents.
    """
    if href is None:
        return None
    href = href.strip()
    if not href or "://" in href:
        return None
    href = href.split("#", 1)[0]
    if not href:
        return None
    combined = base_dir / href
    parts: list[str] = []
    for part in combined.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part in ("", "."):
            continue
        else:
            parts.append(part)
    return PurePosixPath(*parts)


def _effective_base(element: ET.Element, base_dir: PurePosixPath) -> PurePosixPath:
    declared = _base(element)
    if not declared:
        return base_dir
    resolved = resolve_reference(base_dir, declared)
    return resolved if resolved is not None else base_dir


@dataclass(frozen=True)
class DocumentRef:
    """One ``schemaDocument`` or ``instanceDocument`` reference."""

    path: PurePosixPath
    raw_href: str
    role: str | None = None


@dataclass(frozen=True)
class Expected:
    """One ``expected`` element: an outcome for an optional version range."""

    validity: str
    version: tuple[str, ...] = ()

    @property
    def is_version_tagged(self) -> bool:
        return bool(self.version)


@dataclass(frozen=True)
class TestCurrent:
    """The ``current`` provenance element of a test."""

    status: str | None = None
    date: str | None = None
    bugzilla: str | None = None


@dataclass(frozen=True)
class SchemaTest:
    """A ``schemaTest``: schema documents and their expected validity."""

    name: str
    documents: tuple[DocumentRef, ...]
    expected: tuple[Expected, ...]
    version: tuple[str, ...] = ()
    current: TestCurrent | None = None


@dataclass(frozen=True)
class InstanceTest:
    """An ``instanceTest``: an instance document and its expected validity."""

    name: str
    document: DocumentRef
    expected: tuple[Expected, ...]
    version: tuple[str, ...] = ()
    current: TestCurrent | None = None


@dataclass(frozen=True)
class TestGroup:
    """A ``testGroup`` with at most one schema and any number of instances."""

    name: str
    version: tuple[str, ...]
    schema_test: SchemaTest | None
    instance_tests: tuple[InstanceTest, ...]
    source: PurePosixPath


@dataclass(frozen=True)
class TestSet:
    """A ``testSet`` contributed by one organisation."""

    name: str
    contributor: str
    version: tuple[str, ...]
    groups: tuple[TestGroup, ...]
    source: PurePosixPath


@dataclass(frozen=True)
class TestSuite:
    """The root ``suite.xml`` and the test sets it references."""

    name: str
    release_date: str | None
    version: tuple[str, ...]
    test_sets: tuple[TestSet, ...]


@dataclass(frozen=True)
class Catalog:
    """A parsed test suite plus the root it was read from."""

    root: PurePosixPath
    suite: TestSuite

    def document(self, reference: DocumentRef) -> PurePosixPath:
        """Absolute corpus path for a document reference."""
        return self.root / reference.path


def test_id(set_name: str, group_name: str, test_name: str) -> str:
    """The stable identifier for a schema or instance test."""
    return f"{set_name}/{group_name}/{test_name}"


def _parse_expected(element: ET.Element) -> Expected:
    return Expected(validity=element.get("validity", ""), version=_tokens(element.get("version")))


def _parse_current(element: ET.Element) -> TestCurrent | None:
    current = _child(element, "current")
    if current is None:
        return None
    return TestCurrent(
        status=current.get("status"),
        date=current.get("date"),
        bugzilla=current.get("bugzilla"),
    )


def _parse_document(element: ET.Element, base_dir: PurePosixPath) -> DocumentRef | None:
    path = resolve_reference(base_dir, _href(element))
    if path is None:
        return None
    return DocumentRef(path=path, raw_href=_href(element) or "", role=element.get("role"))


def _parse_schema_test(element: ET.Element, base_dir: PurePosixPath) -> SchemaTest:
    documents = tuple(
        doc
        for doc_element in _children(element, "schemaDocument")
        if (doc := _parse_document(doc_element, base_dir)) is not None
    )
    return SchemaTest(
        name=element.get("name", ""),
        documents=documents,
        expected=tuple(_parse_expected(e) for e in _children(element, "expected")),
        version=_tokens(element.get("version")),
        current=_parse_current(element),
    )


def _parse_instance_test(element: ET.Element, base_dir: PurePosixPath) -> InstanceTest | None:
    document_element = _child(element, "instanceDocument")
    if document_element is None:
        return None
    document = _parse_document(document_element, base_dir)
    if document is None:
        return None
    return InstanceTest(
        name=element.get("name", ""),
        document=document,
        expected=tuple(_parse_expected(e) for e in _children(element, "expected")),
        version=_tokens(element.get("version")),
        current=_parse_current(element),
    )


def _parse_group(element: ET.Element, base_dir: PurePosixPath, source: PurePosixPath) -> TestGroup:
    base = _effective_base(element, base_dir)
    schema_element = _child(element, "schemaTest")
    schema_test = _parse_schema_test(schema_element, base) if schema_element is not None else None
    instances = tuple(
        instance
        for instance_element in _children(element, "instanceTest")
        if (instance := _parse_instance_test(instance_element, base)) is not None
    )
    return TestGroup(
        name=element.get("name", ""),
        version=_tokens(element.get("version")),
        schema_test=schema_test,
        instance_tests=instances,
        source=source,
    )


def _parse_test_set(tree: ET.ElementTree, source: PurePosixPath) -> TestSet:
    root = tree.getroot()
    if local_name(root.tag) != "testSet":
        raise CatalogError(f"{source}: expected <testSet>, found <{local_name(root.tag)}>")
    base = _effective_base(root, source.parent)
    groups = tuple(
        _parse_group(group, base, source)
        for group in _children(root, "testGroup")
    )
    return TestSet(
        name=root.get("name", ""),
        contributor=root.get("contributor", ""),
        version=_tokens(root.get("version")),
        groups=groups,
        source=source,
    )


def load_catalog(root: PurePosixPath | str, suite_file: str = "suite.xml") -> Catalog:
    """Parse *suite_file* under *root* and return every referenced test set.

    Raises :class:`CatalogError` when a referenced document cannot be read or
    is not a test-suite document.
    """
    root_path = PurePosixPath(root)
    suite_path = PurePosixPath(suite_file)
    try:
        suite_root = ET.parse(str(root_path / suite_path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CatalogError(f"cannot read suite {suite_path}: {exc}") from exc
    if local_name(suite_root.tag) != "testSuite":
        raise CatalogError(
            f"{suite_path}: expected <testSuite>, found <{local_name(suite_root.tag)}>"
        )
    base = _effective_base(suite_root, suite_path.parent)

    test_sets: list[TestSet] = []
    for reference in _children(suite_root, "testSetRef"):
        path = resolve_reference(base, _href(reference))
        if path is None:
            raise CatalogError(f"{suite_path}: testSetRef with unusable href")
        try:
            tree = ET.parse(str(root_path / path))
        except (OSError, ET.ParseError) as exc:
            raise CatalogError(f"cannot read test set {path}: {exc}") from exc
        test_sets.append(_parse_test_set(tree, path))

    return Catalog(
        root=root_path,
        suite=TestSuite(
            name=suite_root.get("name", ""),
            release_date=suite_root.get("releaseDate"),
            version=_tokens(suite_root.get("version")),
            test_sets=tuple(test_sets),
        ),
    )
