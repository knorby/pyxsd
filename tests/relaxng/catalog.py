"""Readers and materializers for the pinned RELAX NG catalogs.

The upstream catalogs are single inline XML manifests.  ``spectest.xml``
holds correct/incorrect schemas plus valid/invalid instances; ``xsdtest.xml``
holds datatype lexical and facet assertions that the upstream ``xsdtest.xsl``
turns into schema/instance cases; ``compacttest.xml`` pairs accepted and
rejected compact fragments with their XML translations.  Every loader returns
real document paths by materializing the cases under ``<root>/.cases`` using
the upstream split layout from ``test/prep.xsl``.

The manifests stay canonical and unmodified.  Materialized files are derived,
gitignored, and rewritten only when their content differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.dom import Node, minidom
from xml.dom.minidom import Element as DomElement
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape, quoteattr

from .model import (
    PROFILE_COMPACT_SYNTAX,
    PROFILE_CORE,
    PROFILE_XSD_DATATYPE,
    CaseKind,
    CorpusCase,
)

SPECTEST_FILE = "spectest.xml"
XSDTEST_FILE = "xsdtest.xml"
COMPACTTEST_FILE = "compacttest.xml"
CASE_DIRECTORY = ".cases"

RNG_NAMESPACE = "http://relaxng.org/ns/structure/1.0"
XSD_DATATYPE_LIBRARY = "http://www.w3.org/2001/XMLSchema-datatypes"

#: XSD 1.1 additions present in the upstream datatype catalog.  The promised
#: adapter profile is XSD 1.0, so these entries are deliberately not cases.
XSD_1_1_DATATYPES = frozenset({"untypedAtomic", "anyAtomicType"})


class CatalogError(Exception):
    """Raised when a pinned catalog cannot be interpreted."""


@dataclass(frozen=True)
class _CaseSpec:
    id: str
    kind: CaseKind
    filename: str
    text: str


@dataclass(frozen=True)
class _GroupSpec:
    directory: str
    profile: str
    schema: _CaseSpec
    instances: tuple[_CaseSpec, ...] = ()
    resources: tuple[tuple[str, str], ...] = ()
    #: An equivalent XML-syntax schema materialized beside a compact case.
    paired_schema: _CaseSpec | None = None


def load_xml_syntax_catalog(root: Path) -> tuple[CorpusCase, ...]:
    """Read and materialize the pinned XML-syntax catalog under *root*."""
    path = root / SPECTEST_FILE
    document = _parse_xml(path)
    element = document.documentElement
    if element.tagName != "testSuite":
        raise CatalogError(f"{path}: expected <testSuite>, found <{element.tagName}>")
    return _materialize(root, "spectest", _spectest_groups(element, path))


def load_xsd_datatype_catalog(root: Path) -> tuple[CorpusCase, ...]:
    """Read and materialize the pinned XSD datatype catalog under *root*."""
    path = root / XSDTEST_FILE
    document = _parse_xml(path)
    element = document.documentElement
    if element.tagName != "xsdtest":
        raise CatalogError(f"{path}: expected <xsdtest>, found <{element.tagName}>")
    return _materialize(root, "xsdtest", _xsd_groups(element, path))


def load_compact_syntax_catalog(root: Path) -> tuple[CorpusCase, ...]:
    """Read and materialize the pinned compact-syntax catalog under *root*.

    The manifest is a compact-to-XML translation suite.  Every ``testCase``
    contributes one compact schema assertion; a correct case also materializes
    its authored XML counterpart beside it as ``c.rng``.  The upstream
    ``bug`` wrapper is a known non-testCase entry and is deliberately excluded,
    preserving the catalog's 87-case granularity.
    """
    path = root / COMPACTTEST_FILE
    document = _parse_xml(path)
    element = document.documentElement
    if element.tagName != "testSuite":
        raise CatalogError(f"{path}: expected <testSuite>, found <{element.tagName}>")
    return _materialize(root, "compacttest", _compact_groups(element, path))


def _parse_xml(path: Path) -> minidom.Document:
    try:
        return minidom.parse(str(path))
    except OSError as exc:
        raise CatalogError(f"{path}: cannot read catalog: {exc}") from exc
    except ExpatError as exc:
        raise CatalogError(f"{path}: cannot parse catalog: {exc}") from exc


def _element_children(node: Node) -> list[DomElement]:
    return [child for child in node.childNodes if child.nodeType == Node.ELEMENT_NODE]


def _attributes(element: DomElement) -> dict[str, str]:
    attributes = element.attributes
    return {
        attributes.item(index).name: attributes.item(index).value
        for index in range(attributes.length)
    }


def _required_attr(element: DomElement, name: str, what: str) -> str:
    if not element.hasAttribute(name) or not element.getAttribute(name).strip():
        raise CatalogError(f"{what}: missing required {name} attribute")
    return element.getAttribute(name)


def _safe_component(value: str, what: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or any(character.isspace() or ord(character) > 126 for character in value)
    ):
        raise CatalogError(f"{what}: unsafe path component {value!r}")
    return value


def _in_scope_namespaces(element: DomElement) -> dict[str, str]:
    chain: list[DomElement] = []
    node: Node | None = element
    while node is not None and node.nodeType == Node.ELEMENT_NODE:
        chain.append(node)  # type: ignore[arg-type]
        node = node.parentNode
    namespaces: dict[str, str] = {}
    for ancestor in reversed(chain):
        for name, value in _attributes(ancestor).items():
            if name == "xmlns":
                if value:
                    namespaces[name] = value
                else:
                    namespaces.pop(name, None)
            elif name.startswith("xmlns:"):
                namespaces[name] = value
    return namespaces


def _serialize_element(element: DomElement) -> str:
    clone = element.cloneNode(deep=True)
    for name, value in _in_scope_namespaces(element).items():
        if not clone.hasAttribute(name):
            clone.setAttribute(name, value)
    return clone.toxml()


def _serialize_children(container: Node) -> str:
    return "".join(
        _serialize_element(child) if child.nodeType == Node.ELEMENT_NODE else child.toxml()
        for child in container.childNodes
    )


def _string_value(element: Node) -> str:
    parts: list[str] = []
    for child in element.childNodes:
        if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE):
            parts.append(child.data)
        elif child.nodeType == Node.ELEMENT_NODE:
            parts.append(_string_value(child))
    return "".join(parts)


def _with_doctype(text: str, doctype: str | None) -> str:
    return f"{doctype}\n{text}" if doctype else text


def _container_doctype(container: DomElement) -> str | None:
    if not container.hasAttribute("dtd"):
        return None
    value = container.getAttribute("dtd")
    return value if value.strip() else None


def _schema_text(container: DomElement, what: str) -> str:
    if len(_element_children(container)) != 1:
        raise CatalogError(f"{what}: schema must contain exactly one element")
    return _with_doctype(_serialize_children(container), _container_doctype(container))


def _instance_text(container: DomElement) -> str:
    if len(_element_children(container)) != 1:
        raise CatalogError("instance: instance must contain exactly one element")
    return _with_doctype(_serialize_children(container), _container_doctype(container))


def _profile_from_requires(element: DomElement, inherited: str) -> str:
    profile = inherited
    for child in _element_children(element):
        if child.tagName != "requires":
            continue
        library = _required_attr(child, "datatypeLibrary", "requires")
        if library != XSD_DATATYPE_LIBRARY:
            raise CatalogError(f"requires: unsupported datatypeLibrary {library!r}")
        profile = PROFILE_XSD_DATATYPE
    return profile


def _resource_text(resource: DomElement) -> str:
    if _element_children(resource):
        return _serialize_children(resource)
    return _string_value(resource)


def _collect_resources(
    node: DomElement,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    resources: list[tuple[str, str]] = []
    for child in _element_children(node):
        if child.tagName == "resource":
            name = _safe_component(
                _required_attr(child, "name", "resource"),
                "resource",
            )
            resources.append(("/".join((*prefix, name)), _resource_text(child)))
        elif child.tagName == "dir":
            name = _safe_component(_required_attr(child, "name", "dir"), "dir")
            resources.extend(_collect_resources(child, (*prefix, name)))
    return resources


_SPECTEST_CASE_TAGS = frozenset(
    {
        "section",
        "documentation",
        "requires",
        "correct",
        "incorrect",
        "valid",
        "invalid",
        "resource",
        "dir",
    }
)
_SPECTEST_SUITE_TAGS = frozenset(
    {
        "author",
        "email",
        "documentation",
        "section",
        "requires",
        "testCase",
        "testSuite",
    }
)


def _spectest_groups(root: DomElement, path: Path) -> tuple[_GroupSpec, ...]:
    groups: list[_GroupSpec] = []
    index = 0

    def visit(suite: DomElement, inherited: str) -> None:
        nonlocal index
        profile = _profile_from_requires(suite, inherited)
        for child in _element_children(suite):
            tag = child.tagName
            if tag == "requires":
                continue
            if tag not in _SPECTEST_SUITE_TAGS:
                raise CatalogError(f"{path}: unexpected <{tag}> in a testSuite")
            if tag == "testSuite":
                visit(child, profile)
            elif tag == "testCase":
                index += 1
                groups.append(
                    _spectest_case(
                        child,
                        index,
                        _profile_from_requires(child, profile),
                        path,
                    )
                )

    visit(root, PROFILE_CORE)
    return tuple(groups)


def _spectest_case(
    test_case: DomElement,
    index: int,
    profile: str,
    path: Path,
) -> _GroupSpec:
    group_id = f"spectest-{index:04d}"
    case_dir = f"{index:04d}"
    for child in _element_children(test_case):
        if child.tagName not in _SPECTEST_CASE_TAGS:
            raise CatalogError(f"{path}: unexpected <{child.tagName}> in a testCase")

    correct = [child for child in _element_children(test_case) if child.tagName == "correct"]
    incorrect = [child for child in _element_children(test_case) if child.tagName == "incorrect"]
    if len(correct) + len(incorrect) != 1:
        raise CatalogError(f"{path}: {group_id}: schema must be exactly one of correct/incorrect")
    is_correct = bool(correct)
    container = correct[0] if correct else incorrect[0]
    schema_kind: CaseKind = "correct-schema" if is_correct else "incorrect-schema"
    schema = _CaseSpec(
        id=group_id,
        kind=schema_kind,
        filename="c.rng" if is_correct else "i.rng",
        text=_schema_text(container, f"{path}: {group_id}"),
    )

    instances: list[_CaseSpec] = []
    valid_count = 0
    invalid_count = 0
    for child in _element_children(test_case):
        if child.tagName not in {"valid", "invalid"}:
            continue
        if not is_correct:
            raise CatalogError(f"{path}: {group_id}: incorrect schema must not have instances")
        if child.tagName == "valid":
            valid_count += 1
            kind: CaseKind = "valid-instance"
            prefix = "valid"
            number = valid_count
            filename = f"{number}.v.xml"
        else:
            invalid_count += 1
            kind = "invalid-instance"
            prefix = "invalid"
            number = invalid_count
            filename = f"{number}.i.xml"
        instances.append(
            _CaseSpec(
                id=f"{group_id}/{prefix}-{number}",
                kind=kind,
                filename=filename,
                text=_instance_text(child),
            )
        )

    return _GroupSpec(
        directory=case_dir,
        profile=profile,
        schema=schema,
        instances=tuple(instances),
        resources=tuple(_collect_resources(test_case)),
    )


_COMPACT_PART_TAGS = frozenset({"compact", "xml"})
_COMPACT_CASE_TAGS = frozenset({"correct", "incorrect", "resource", "dir"})


def _compact_groups(root: DomElement, path: Path) -> tuple[_GroupSpec, ...]:
    groups: list[_GroupSpec] = []
    index = 0
    for child in _element_children(root):
        if child.tagName == "bug":
            # The upstream manifest keeps one known-bug fragment outside the
            # testCase sequence; it is not one of the 87 catalog cases.
            continue
        if child.tagName != "testCase":
            raise CatalogError(f"{path}: unexpected <{child.tagName}> in a testSuite")
        index += 1
        groups.append(_compact_case(child, index, path))
    return tuple(groups)


def _compact_case(test_case: DomElement, index: int, path: Path) -> _GroupSpec:
    group_id = f"compacttest-{index:04d}"
    parts: dict[str, DomElement] = {}
    for child in _element_children(test_case):
        if child.tagName not in _COMPACT_PART_TAGS:
            raise CatalogError(f"{path}: {group_id}: unexpected <{child.tagName}>")
        if child.tagName in parts:
            raise CatalogError(f"{path}: {group_id}: duplicate <{child.tagName}>")
        parts[child.tagName] = child
    compact = parts.get("compact")
    if compact is None:
        raise CatalogError(f"{path}: {group_id}: missing <compact>")
    for child in _element_children(compact):
        if child.tagName not in _COMPACT_CASE_TAGS:
            raise CatalogError(f"{path}: {group_id}: unexpected <{child.tagName}> in <compact>")

    containers = [
        child for child in _element_children(compact) if child.tagName in {"correct", "incorrect"}
    ]
    if len(containers) != 1:
        raise CatalogError(
            f"{path}: {group_id}: compact must contain exactly one of correct/incorrect"
        )
    container = containers[0]
    is_correct = container.tagName == "correct"
    if _element_children(container):
        raise CatalogError(f"{path}: {group_id}: compact schema must contain only text")
    kind: CaseKind = "correct-schema" if is_correct else "incorrect-schema"
    schema = _CaseSpec(
        id=group_id,
        kind=kind,
        filename="c.rnc" if is_correct else "i.rnc",
        text=_string_value(container),
    )

    resources = list(_collect_resources(compact))
    paired: _CaseSpec | None = None
    xml = parts.get("xml")
    if xml is not None:
        for child in _element_children(xml):
            if child.tagName not in _COMPACT_CASE_TAGS:
                raise CatalogError(f"{path}: {group_id}: unexpected <{child.tagName}> in <xml>")
        xml_containers = [
            child for child in _element_children(xml) if child.tagName in {"correct", "incorrect"}
        ]
        if len(xml_containers) != 1:
            raise CatalogError(
                f"{path}: {group_id}: xml must contain exactly one of correct/incorrect"
            )
        xml_container = xml_containers[0]
        if (xml_container.tagName == "correct") != is_correct:
            raise CatalogError(f"{path}: {group_id}: compact and xml verdicts must agree")
        paired = _CaseSpec(
            id=group_id,
            kind=kind,
            filename="c.rng" if is_correct else "i.rng",
            text=_schema_text(xml_container, f"{path}: {group_id}"),
        )
        resources.extend(_collect_resources(xml))

    return _GroupSpec(
        directory=f"{index:04d}",
        profile=PROFILE_COMPACT_SYNTAX,
        schema=schema,
        resources=tuple(resources),
        paired_schema=paired,
    )


# ---------------------------------------------------------------------------
# XSD datatype catalog generation
# ---------------------------------------------------------------------------


def _schema_document(inner: str, doctype: str | None = None) -> str:
    text = (
        f'<element xmlns="{RNG_NAMESPACE}" name="doc" '
        f'datatypeLibrary="{XSD_DATATYPE_LIBRARY}">{inner}</element>'
    )
    return _with_doctype(text, doctype)


def _data_schema(name: str, params: tuple[tuple[str, str], ...] = ()) -> str:
    inner = f"<data type={quoteattr(name)}>"
    for parameter, value in params:
        inner += f"<param name={quoteattr(parameter)}>{escape(value)}</param>"
    inner += "</data>"
    return _schema_document(inner)


def _namespace_attributes(element: DomElement) -> str:
    return "".join(
        f" {name}={quoteattr(value)}"
        for name, value in sorted(_in_scope_namespaces(element).items())
    )


def _value_schema(name: str, value: DomElement) -> str:
    inner = (
        f"<value type={quoteattr(name)}{_namespace_attributes(value)}>"
        f"{escape(_string_value(value))}</value>"
    )
    return _schema_document(inner, _internal_subset_doctype(value, "element"))


def _internal_subset_doctype(element: DomElement, root_name: str) -> str | None:
    if not element.hasAttribute("internalSubset"):
        return None
    subset = element.getAttribute("internalSubset")
    if not subset.strip():
        return None
    return f"<!DOCTYPE {root_name} [\n{subset}\n]>"


def _instance_document(element: DomElement) -> str:
    return _instance_document_from(element, _string_value(element))


def _instance_document_from(element: DomElement, value: str) -> str:
    text = f"<doc{_namespace_attributes(element)}>{escape(value)}</doc>"
    return _with_doctype(text, _internal_subset_doctype(element, "doc"))


def _instance_specs(
    group_id: str,
    values: list[tuple[CaseKind, str]],
) -> tuple[_CaseSpec, ...]:
    counts = {"valid-instance": 0, "invalid-instance": 0}
    specs: list[_CaseSpec] = []
    for kind, text in values:
        counts[kind] += 1
        number = counts[kind]
        prefix = "valid" if kind == "valid-instance" else "invalid"
        suffix = "v" if kind == "valid-instance" else "i"
        specs.append(
            _CaseSpec(
                id=f"{group_id}/{prefix}-{number}",
                kind=kind,
                filename=f"{number}.{suffix}.xml",
                text=text,
            )
        )
    return tuple(specs)


def _make_group(
    datatype: str,
    number: int,
    schema_text: str,
    instance_values: list[tuple[CaseKind, str]],
) -> _GroupSpec:
    group_id = f"xsdtest-{datatype}-{number:02d}"
    return _GroupSpec(
        directory=f"{datatype}/{number:02d}",
        profile=PROFILE_XSD_DATATYPE,
        schema=_CaseSpec(
            id=group_id,
            kind="correct-schema",
            filename="c.rng",
            text=schema_text,
        ),
        instances=_instance_specs(group_id, instance_values),
    )


def _xsd_groups(root: DomElement, path: Path) -> tuple[_GroupSpec, ...]:
    groups: list[_GroupSpec] = []
    seen: set[str] = set()
    for datatype in _element_children(root):
        if datatype.tagName != "datatype":
            raise CatalogError(f"{path}: unexpected <{datatype.tagName}> in <xsdtest>")
        name = _safe_component(
            _required_attr(datatype, "name", "datatype"),
            "datatype",
        )
        if name in seen:
            raise CatalogError(f"{path}: duplicate datatype {name!r}")
        seen.add(name)
        if name in XSD_1_1_DATATYPES:
            continue
        groups.extend(_xsd_datatype_groups(name, datatype, path))
    return tuple(groups)


def _xsd_datatype_groups(name: str, datatype: DomElement, path: Path) -> list[_GroupSpec]:
    params: list[tuple[str, str]] = []
    base_values: list[tuple[CaseKind, str]] = []
    extras: list[DomElement] = []
    for child in _element_children(datatype):
        tag = child.tagName
        if tag == "param":
            parameters = (_required_attr(child, "name", "param"), _string_value(child))
            params.append(parameters)
        elif tag in {"valid", "invalid"}:
            kind: CaseKind = "valid-instance" if tag == "valid" else "invalid-instance"
            base_values.append((kind, _instance_document(child)))
        elif tag in {"equiv", "length", "lessThan", "incomparable"}:
            extras.append(child)
        else:
            raise CatalogError(f"{path}: unexpected <{tag}> in datatype {name!r}")

    groups = [
        _make_group(
            name,
            1,
            _data_schema(name, tuple(params)),
            base_values,
        )
    ]
    number = 1
    for extra in extras:
        if extra.tagName == "equiv":
            new_groups = _equiv_groups(name, extra, number + 1, path)
        elif extra.tagName == "length":
            new_groups = _length_groups(name, extra, number + 1, path)
        elif extra.tagName == "lessThan":
            new_groups = _comparison_groups(name, extra, number + 1, path, "lessThan")
        else:
            new_groups = _comparison_groups(name, extra, number + 1, path, "incomparable")
        groups.extend(new_groups)
        number += len(new_groups)
    return groups


def _equiv_groups(
    name: str,
    equiv: DomElement,
    first_number: int,
    path: Path,
) -> list[_GroupSpec]:
    classes: list[list[DomElement]] = []
    for child in _element_children(equiv):
        if child.tagName != "class":
            raise CatalogError(f"{path}: unexpected <{child.tagName}> in <equiv>")
        values = _element_children(child)
        if not values or any(value.tagName != "value" for value in values):
            raise CatalogError(f"{path}: equivalence class must contain value elements")
        classes.append(values)
    if not classes:
        raise CatalogError(f"{path}: <equiv> must contain at least one class")

    groups: list[_GroupSpec] = []
    for index, values in enumerate(classes):
        number = first_number + index
        instances: list[tuple[CaseKind, str]] = [
            ("valid-instance", _instance_document(value)) for value in values[1:]
        ]
        for other_index, other in enumerate(classes):
            if other_index == index:
                continue
            instances.extend(("invalid-instance", _instance_document(value)) for value in other)
        groups.append(
            _make_group(
                name,
                number,
                _value_schema(name, values[0]),
                instances,
            )
        )
    return groups


def _length_groups(
    name: str,
    length: DomElement,
    first_number: int,
    path: Path,
) -> list[_GroupSpec]:
    raw = _required_attr(length, "value", "length")
    try:
        length_value = int(raw)
    except ValueError as exc:
        raise CatalogError(f"{path}: length facet is not an integer: {raw!r}") from exc
    if length_value < 0:
        raise CatalogError(f"{path}: length facet must be non-negative: {length_value}")

    facets: list[tuple[int, CaseKind]] = [
        (length_value, "valid-instance"),
        (length_value + 1, "invalid-instance"),
    ]
    if length_value:
        facets.append((length_value - 1, "invalid-instance"))
    return [
        _make_group(
            name,
            first_number + offset,
            _data_schema(name, (("length", str(facet)),)),
            [(kind, _instance_document(length))],
        )
        for offset, (facet, kind) in enumerate(facets)
    ]


def _comparison_groups(
    name: str,
    comparison: DomElement,
    first_number: int,
    path: Path,
    operator: str,
) -> list[_GroupSpec]:
    values = _element_children(comparison)
    if len(values) != 2 or any(value.tagName != "value" for value in values):
        raise CatalogError(f"{path}: <{operator}> must contain exactly two value elements")
    first, second = values
    first_invalid: tuple[CaseKind, str] = (
        "invalid-instance",
        _instance_document_from(comparison, _string_value(first)),
    )
    second_invalid: tuple[CaseKind, str] = (
        "invalid-instance",
        _instance_document_from(comparison, _string_value(second)),
    )
    if operator == "lessThan":
        instances_one = [
            ("valid-instance", _instance_document_from(comparison, _string_value(second))),
            first_invalid,
        ]
        instances_two = [first_invalid, second_invalid]
    else:
        instances_one = [second_invalid, first_invalid]
        instances_two = [first_invalid, second_invalid]
    return [
        _make_group(
            name,
            first_number,
            _data_schema(name, (("minExclusive", _string_value(first)),)),
            instances_one,
        ),
        _make_group(
            name,
            first_number + 1,
            _data_schema(name, (("minExclusive", _string_value(second)),)),
            instances_two,
        ),
    ]


# ---------------------------------------------------------------------------
# Materialization
# ---------------------------------------------------------------------------


def _write(path: Path, text: str, written: dict[Path, str]) -> Path:
    if path in written:
        raise CatalogError(f"{path}: duplicate output path")
    written[path] = text
    encoded = text.encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.read_bytes() == encoded:
            return path
        path.write_bytes(encoded)
    except OSError as exc:
        raise CatalogError(f"{path}: cannot write case file: {exc}") from exc
    return path


def _materialize(
    root: Path,
    catalog: str,
    groups: tuple[_GroupSpec, ...],
) -> tuple[CorpusCase, ...]:
    cache = root / CASE_DIRECTORY / catalog
    written: dict[Path, str] = {}
    cases: list[CorpusCase] = []
    for group in groups:
        case_dir = cache / group.directory
        schema_spec = group.schema
        schema_path = _write(case_dir / schema_spec.filename, schema_spec.text, written)
        cases.append(
            CorpusCase(
                id=schema_spec.id,
                kind=schema_spec.kind,
                schema=schema_path,
                instance=None,
                profile=group.profile,
            )
        )
        if group.paired_schema is not None:
            _write(case_dir / group.paired_schema.filename, group.paired_schema.text, written)
        for resource_name, resource_text in group.resources:
            _write(case_dir / resource_name, resource_text, written)
        for instance in group.instances:
            instance_path = _write(case_dir / instance.filename, instance.text, written)
            cases.append(
                CorpusCase(
                    id=instance.id,
                    kind=instance.kind,
                    schema=schema_path,
                    instance=instance_path,
                    profile=group.profile,
                )
            )
    return tuple(cases)
