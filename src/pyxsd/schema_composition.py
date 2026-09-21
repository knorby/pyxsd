"""Schema-composition machinery for schema compilation.

Splices ``xs:include``/``xs:import``/``xs:redefine``/``xs:override``
documents into the main schema tree (with chameleon namespace
pre-processing), injects the built-in ``xml``/``xsi``/``xlink``
namespace attributes, and applies the XSD 1.1 local ``targetNamespace``
and wildcard-spec refinements. The functions operate on a
:class:`CompositionContext`, the per-compile state that
:func:`~pyxsd.schema._compile_into_context` builds from the schema and
instance inputs; the container fields are shared with the compiled
:class:`~pyxsd.schema.Schema`, so updates stay visible to the checks
passes.
"""

import decimal
import logging
import os.path
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

from pyxsd.binding import BindingPolicy
from pyxsd.namespaces import (
    XLINK_NS,
    XML_NS,
    XSD_NS,
    XSI_NS,
    NamespaceContext,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
    parse_with_namespaces,
)
from pyxsd.schema_context import SchemaContext
from pyxsd.validation import CompileContextProtocol, ValidationReport
from pyxsd.versioning import PROCESSOR_VERSION, apply_conditional_inclusion

logger = logging.getLogger(__name__)

# Schema components that may be spliced in from included/imported
# schemas before the ElementRepresentative run.
_COMPOSABLE_TAGS = {
    "element",
    "complexType",
    "simpleType",
    "group",
    "attributeGroup",
    "attribute",
    "notation",
}


def _qnameLocal(value: str) -> str:
    """Returns the local part of a lexical QName or Clark name."""
    return local_name(value.rpartition(":")[2])


def _redefined_qname(value: str) -> str:
    """Rewrites a self-reference onto the redefined ``Name|base`` copy.

    The prefix is preserved: dropping it made an unprefixed value resolve
    through the in-scope default namespace (the XML Schema namespace on a
    ``default xmlns`` document), so a namespaced redefine could not find
    its renamed original (ii03/ii05/ii06/ii07).
    """
    prefix, _, local = value.rpartition(":")
    return f"{prefix}:{local}|base" if prefix else f"{value}|base"


def _stackPrefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    """Whether *shorter* is a prefix of *longer* (document ancestry)."""
    return len(shorter) <= len(longer) and longer[: len(shorter)] == shorter


#: The built-in attribute declarations of the XML Schema instance
#: namespace (XSD 1.1 §3.2.7.2). ``schemaLocation`` and
#: ``noNamespaceSchemaLocation`` are typed ``anyURI``, a safe
#: under-approximation of their URI-pair/list value spaces.
_XSI_BUILTIN_ATTRIBUTES = (
    ("type", "QName"),
    ("nil", "boolean"),
    ("schemaLocation", "anyURI"),
    ("noNamespaceSchemaLocation", "anyURI"),
)

#: The built-in attribute declarations of the XLink 1.0 namespace
#: (http://www.w3.org/1999/xlink). XSD 1.1 §4.2.3 resolves a
#: namespace name to the schema for that namespace; the XLink
#: vocabulary is available without retrieving ``xlink.xsd``. The
#: declarations are typed as strings, a safe under-approximation of
#: the canonical schema's token/anyURI/NCName restrictions (the xml
#: built-in attributes are typed the same way).
_XLINK_BUILTIN_ATTRIBUTES = (
    "type",
    "href",
    "role",
    "arcrole",
    "title",
    "show",
    "actuate",
    "label",
    "from",
    "to",
)

#: QName-valued schema attributes that chameleon pre-processing
#: rewrites (XSD 1.1 Appendix F.1). Each holds a single QName.
_CHAMELEON_QNAME_ATTRIBUTES = (
    "ref",
    "base",
    "type",
    "refer",
    "itemType",
    "defaultAttributes",
)
#: QName-valued schema attributes that hold a whitespace-separated list.
_CHAMELEON_QNAME_LIST_ATTRIBUTES = ("memberTypes", "substitutionGroup", "notQName")

_COMPOSABLE_REDEFINE_KINDS = ("complexType", "simpleType", "group", "attributeGroup")

#: The XSD symbol spaces an ``xs:override`` may replace, keyed by the
#: declaration's element name. ``simpleType`` and ``complexType``
#: share the ``type`` space, so an override may replace a complex
#: type with a simple type of the same name (over013).
_OVERRIDE_SYMBOL_SPACES: dict[str, str] = {
    "simpleType": "type",
    "complexType": "type",
    "element": "element",
    "attribute": "attribute",
    "group": "group",
    "attributeGroup": "attributeGroup",
    "notation": "notation",
}


@dataclass
class CompositionContext:
    """State that lives only for the duration of one ``Schema.compile``."""

    mode: BindingPolicy
    namespace_schemas: dict[str, str | Path]
    namespace_context: NamespaceContext
    report: ValidationReport
    classes: dict[str, Any]
    xml_path: Path
    schema_context: SchemaContext
    namespace_overrides: dict[int, str | None]
    injected_builtin_ids: set[int]
    form_defaults: dict[int, tuple[str | None, str | None]]
    xpath_default_namespaces: dict[int, str | None]
    composed_element_ids: set[int]
    composed_schema_roots: dict[int, Any]
    directive_ids: dict[str, Any]
    compose_stack: list[str]
    redefine_origins: dict[tuple[str, str, str], tuple[str, ...]]
    override_origins: dict[tuple[str, str, str], tuple[str, ...]]
    resolved_imports: set[str]
    hint_schema_paths: set[str]
    composed_target_namespaces: set[str]
    composed_documents: set[str] = field(default_factory=set)
    additional_schemas: list[tuple[str | None, Path]] = field(default_factory=list)
    #: The declared XSD processor version (``vc:*`` selectors test against
    #: it). ``Schema.compile(xsd_version=...)`` sets it; the default is the
    #: 1.1 processor.
    processor_version: decimal.Decimal = field(default_factory=lambda: PROCESSOR_VERSION)


def schema_composition_context(
    ctx: CompositionContext,
    xsd_file: str | Path | os.PathLike[str] | IO[str] | None,
) -> tuple[Path, set[str]]:
    """Returns the (baseDir, visited) context for schema composition.

    ``baseDir`` is the directory relative to which include/import
    locations resolve; ``visited`` starts with the main schema file
    itself so include cycles are detected.
    """
    if isinstance(xsd_file, (str, os.PathLike)):
        mainPath = Path(xsd_file).resolve()
        return mainPath.parent, {str(mainPath)}
    return Path.cwd(), set()


def collect_additional_schemas(
    ctx: CompositionContext,
    xsd_file: str | Path | os.PathLike[str] | IO[str] | None,
    schema_location_pairs: list[tuple[str | None, str]],
) -> list[tuple[str | None, Path]]:
    """Returns additional ``(namespace, path)`` schemas to load.

    Sources are the explicit ``namespace_schemas`` mapping and, in
    strict mode, any extra pairs in the instance's
    ``xsi:schemaLocation`` beyond the main schema. The pairs come
    from the caller, which reads the instance document (see
    :func:`pyxsd.parse` and ``schema_location_pairs``).
    """
    additions: list[tuple[str | None, Path]] = []
    seen: set[str] = set()
    if isinstance(xsd_file, (str, os.PathLike)):
        seen.add(str(Path(xsd_file).resolve()))
    for namespace, location in ctx.namespace_schemas.items():
        path = Path(location)
        if not path.is_absolute():
            path = ctx.xml_path / path
        path = path.resolve()
        if str(path) in seen:
            continue
        seen.add(str(path))
        additions.append((namespace or None, path))
    if getattr(ctx.mode, "namespaces", "legacy") == "strict":
        for pair_namespace, location in schema_location_pairs:
            path = Path(location)
            if not path.is_absolute():
                path = ctx.xml_path / path
            path = path.resolve()
            if str(path) in seen:
                continue
            seen.add(str(path))
            # Remember the hint-derived additions: a hint that
            # cannot be loaded downgrades to a ``schema-hint``
            # warning (see ``splice_additional_schemas``).
            ctx.hint_schema_paths.add(str(path))
            additions.append((pair_namespace, path))
    return additions


def splice_additional_schemas(
    ctx: CompositionContext, schemaRoot: Any, baseDir: Path, visited: set[str]
) -> None:
    """Splices schemas supplied outside the main document.

    These are ``namespace_schemas`` entries and extra
    ``xsi:schemaLocation`` pairs; each is loaded like an import so
    its components keep their own target namespace. The two sources
    differ in authority: a ``namespace_schemas`` entry is an
    explicit caller input whose failure stays an ``import-unresolved``
    error, while an instance ``xsi:schemaLocation`` pair is an
    advisory hint whose failure downgrades to a ``schema-hint``
    warning. Document-level ``xs:import``/``xs:include`` failures
    keep their own separate severity through ``splice_composed_schemas``.
    """
    for namespace, path in ctx.additional_schemas:
        tag = ET.Element(clark(XSD_NS, "import"), {"schemaLocation": str(path)})
        if namespace:
            tag.set("namespace", namespace)
        is_hint = str(path) in ctx.hint_schema_paths
        splice_included_schema(
            ctx,
            tag,
            schemaRoot,
            baseDir,
            visited,
            isImport=True,
            missing_severity="warning" if is_hint else "error",
            missing_code="schema-hint" if is_hint else None,
        )


def splice_composed_schemas(
    ctx: CompositionContext,
    schemaRoot: Any,
    baseDir: Path,
    visited: set[str],
    mainDocument: bool = False,
) -> None:
    """Merges composed schemas into ``schemaRoot`` before class building.

    ``xs:include`` (same target namespace or none - the chameleon
    case), ``xs:redefine`` (an included schema whose named
    components may be redefined) and ``xs:import`` (a foreign
    namespace) all splice their named components into the main
    schema tree so the ordinary ER run sees one schema. The parser
    matches names by local name, so imported components are merged
    the same way and namespace differences are recorded as
    warnings rather than hard errors.

    The composition tags are removed from the tree afterwards so
    the ER factory does not warn about them.

    ``mainDocument`` is true only for the top-level call: the ids of
    the main document's directives are recorded so declaration ids
    can be checked against them (an included document's directives
    belong to a different XML document).
    """
    for child in list(schemaRoot):
        local = child.tag.split("}")[-1]
        if local in ("include", "redefine", "override", "import") and mainDocument:
            note_directive_id(ctx, child)
        if local == "include":
            schemaRoot.remove(child)
            splice_included_schema(ctx, child, schemaRoot, baseDir, visited, isImport=False)
        elif local == "redefine":
            schemaRoot.remove(child)
            splice_redefine(ctx, child, schemaRoot, baseDir, visited)
        elif local == "override":
            schemaRoot.remove(child)
            splice_override(ctx, child, schemaRoot, baseDir, visited)
        elif local == "import":
            schemaRoot.remove(child)
            if child.get("namespace") == XSD_NS:
                # Importing the schema-for-schemas namespace is the
                # conventional spelling; the built-in types are
                # always available here.
                continue
            splice_included_schema(
                ctx, child, schemaRoot, baseDir, visited, isImport=True, checkImportNamespace=True
            )
    return None


def note_directive_id(ctx: CompileContextProtocol, tag: Any) -> None:
    """Records a main-document composition directive's ``id``.

    A directive's id is an ``xs:ID`` too; because the directive is
    removed before the ER walk, declaration ids are compared against
    this set instead. A directive that repeats an earlier id is
    itself reported here.
    """
    value = tag.get("id")
    if not value:
        return
    if value in ctx.directive_ids:
        ctx.report.add_error(
            f"duplicate id '{value}' on <{tag.tag.split('}')[-1]}>",
            code="declaration-duplicate",
            phase="schema",
        )
        return
    ctx.directive_ids[value] = tag


def splice_included_schema(
    ctx: CompositionContext,
    tag: Any,
    schemaRoot: Any,
    baseDir: Path,
    visited: set[str],
    isImport: bool,
    missing_severity: str = "warning",
    checkImportNamespace: bool = False,
    missing_code: str | None = None,
) -> None:
    """Splices the named components of one included/imported schema.

    Handles locating and parsing the file, cycle detection and the
    namespace checks; the actual splicing is shared with redefine.

    ``missing_severity`` controls how an unreadable referenced
    document is reported. A schema document's own include/import is
    a hint (warning); a schema the *caller* explicitly supplied via
    ``namespace_schemas`` is a required input, so a missing one
    stays an error. An instance ``xsi:schemaLocation`` pair is also
    caller-visible, but it is an advisory hint: its failure is a
    ``schema-hint`` warning (``missing_code``), not an error.

    ``missing_code`` overrides the issue code used when the
    referenced document cannot be opened (only the not-found case;
    a document that exists but is malformed stays an error under
    the caller's regular code).

    ``checkImportNamespace`` is true for an ``xs:import`` written in
    a schema document: the import's ``namespace`` attribute must
    match the referenced document's target namespace (an import
    cannot absorb a no-namespace document; that is an include).
    Caller-supplied schemas are exempt because their namespace label
    is not an authoring statement in the schema.
    """
    check_directive_annotation(ctx, tag, isImport)
    location = tag.get("schemaLocation")
    strict = getattr(ctx.mode, "namespaces", "legacy") == "strict"
    if not location:
        if isImport:
            # ``schemaLocation`` is optional on xs:import: a
            # namespace-only import is a hint with no document to
            # load. In strict mode it is unresolved unless a schema
            # for the namespace was supplied. A namespace that the
            # importing document actually references is fatal; an
            # unused hint is only a warning.
            namespace = tag.get("namespace")
            if strict and namespace and namespace not in (XML_NS, XLINK_NS, XSD_NS, XSI_NS):
                otherTargets = ctx.composed_target_namespaces - {schemaRoot.get("targetNamespace")}
                supplied = (
                    namespace in ctx.namespace_schemas
                    or namespace in ctx.resolved_imports
                    or namespace in otherTargets
                    or namespace in {ns for ns, _ in ctx.additional_schemas if ns}
                )
                if not supplied:
                    report_unresolved_import(ctx, namespace, schemaRoot)
            logger.debug("namespace-only xs:import with no schemaLocation; skipping")
            return None
        ctx.report.add_error(
            "an include tag has no schemaLocation; the schema could not be composed",
            code="schema-compose",
        )
        return None
    includedPath = (baseDir / location).resolve()
    key = str(includedPath)
    if key in ctx.composed_documents:
        logger.debug("the schema '%s' is already composed; skipping", location)
        return None
    if key in visited:
        # A legal include cycle: the document is already being
        # composed, so this repetition is skipped rather than
        # treated as a fatal error.
        ctx.report.add_warning(
            f"the schema '{location}' is already being composed; the circular include is skipped",
            code="compose-cycle",
        )
        return None
    includedRoot = parse_included_schema(
        ctx,
        location,
        baseDir,
        error_code="import-unresolved" if isImport else "schema-compose",
        missing_severity=missing_severity,
        missing_code=missing_code,
    )
    if includedRoot is None:
        return None
    mainNS = schemaRoot.get("targetNamespace")
    includedNS = includedRoot.get("targetNamespace")
    if isImport and checkImportNamespace and tag.get("namespace") is None and mainNS is None:
        # schF3/addB008/addB035: an import with no namespace attribute
        # imports the absent target namespace. A schema with no
        # targetNamespace cannot import the absent namespace from
        # itself; the imported document's components are already in
        # the importing namespace (an include, not an import).
        ctx.report.add_error(
            f"the import '{location}' has no namespace attribute, but the "
            "importing schema also has no targetNamespace",
            code="compose-invalid",
            phase="schema",
        )
    if isImport and checkImportNamespace and mainNS and tag.get("namespace") == mainNS:
        # XSD 1.0 §4.2.3: an import's namespace must differ from the
        # importing schema's targetNamespace (attgB015).
        ctx.report.add_error(
            f"the import '{location}' imports the schema's own target namespace '{mainNS}'",
            code="compose-invalid",
            phase="schema",
        )
    if isImport:
        # A schema for the namespace was loaded, so a namespace-only
        # import of the same URI is satisfied rather than unresolved.
        if tag.get("namespace"):
            ctx.resolved_imports.add(tag.get("namespace"))
        if includedNS:
            ctx.resolved_imports.add(includedNS)
    if not isImport and includedNS is None and mainNS is not None:
        # Chameleon pre-processing (XSD 1.1 §4.2.3 and Appendix F.1):
        # a document with no targetNamespace that is included by a
        # namespaced schema adopts the including target namespace, and
        # its unqualified QName references are rewritten into it. The
        # attribute is set on the tree so nested chameleon includes
        # inherit the same namespace.
        includedRoot.set("targetNamespace", mainNS)
        apply_chameleon_namespace(ctx, includedRoot, mainNS)
        includedNS = mainNS
    if includedNS:
        ctx.composed_target_namespaces.add(includedNS)
    if (
        isImport
        and checkImportNamespace
        and tag.get("namespace") is None
        and includedNS is not None
    ):
        # XSD 1.1 §4.2.3: an xs:import with no namespace attribute
        # imports the *absent* (no-namespace) target namespace, so the
        # referenced document must itself have no targetNamespace
        # (TargetNS target007). A namespaced document belongs to a
        # namespace-carrying import.
        ctx.report.add_error(
            f"the import '{location}' has no namespace attribute, but the "
            f"imported schema declares targetNamespace '{includedNS}'",
            code="compose-invalid",
            phase="schema",
        )
    if (
        isImport
        and checkImportNamespace
        and tag.get("namespace") is not None
        and includedNS != tag.get("namespace")
    ):
        # An import carrying a namespace attribute must reference a
        # document with exactly that target namespace. A document
        # with no target namespace is absorbed by include, not
        # import; a different namespace is the wrong document.
        ctx.report.add_error(
            f"the imported schema '{location}' declares targetNamespace "
            f"'{includedNS or 'none'}', which does not match the import's "
            f"namespace '{tag.get('namespace')}'",
            code="compose-invalid",
            phase="schema",
        )
    if not isImport and includedNS not in (None, mainNS):
        ctx.report.add_error(
            f"the schema '{location}' declares targetNamespace "
            f"'{includedNS}', which does not match the including "
            f"schema's namespace ({mainNS or 'none'})",
            code="compose-namespace",
        )
    if (
        isImport
        and mainNS
        and includedNS != mainNS
        and getattr(ctx.mode, "namespaces", "legacy") != "strict"
    ):
        logger.warning(
            "imported schema '%s' declares targetNamespace '%s'; pyxsd "
            "matches names by local name, so its components are merged "
            "regardless of the namespace difference",
            location,
            includedNS or "none",
        )
    if isImport:
        componentNamespace = includedNS if includedNS is not None else tag.get("namespace")
    else:
        componentNamespace = includedNS if includedNS is not None else mainNS
    ctx.compose_stack.append(key)
    try:
        splice_composed_schemas(
            ctx, includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
    finally:
        ctx.compose_stack.pop()
    # Record provenance before the components are appended to the
    # main root: ``id`` uniqueness is scoped to a schema document.
    for element in includedRoot.iter():
        ctx.composed_element_ids.add(id(element))
        ctx.composed_schema_roots.setdefault(id(element), includedRoot)
    append_named_components(ctx, includedRoot, schemaRoot, componentNamespace)
    ctx.composed_documents.add(key)
    return None


def parse_included_schema(
    ctx: CompositionContext,
    location: str,
    baseDir: Path,
    error_code: str = "schema-compose",
    missing_severity: str = "error",
    missing_code: str | None = None,
) -> Any | None:
    """Parses one included schema file; returns its root or ``None``.

    A file that cannot be opened is *resource-not-found*: XSD treats
    an unresolvable ``schemaLocation`` as a non-fatal hint, so the
    caller decides (``missing_severity``) whether that is a warning
    or an error, and (``missing_code``) under which code — an
    advisory ``xsi:schemaLocation`` hint reports ``schema-hint``
    while everything else keeps its regular code. A file that
    exists but is not well-formed XML is a rule violation and is
    always an error. Composition proceeds without the missing file.
    """
    includedPath = baseDir / location
    try:
        with open(includedPath, "rb") as includedFile:
            root = parse_with_namespaces(includedFile, ctx.namespace_context)
    except OSError as e:
        message = f"the schema '{location}' could not be opened: {e}"
        code = missing_code if missing_code is not None else error_code
        if missing_severity == "warning":
            ctx.report.add_warning(message, code=code, phase="schema")
        else:
            ctx.report.add_error(message, code=code, phase="schema")
        return None
    except ET.ParseError as e:
        ctx.report.add_error(
            f"the schema '{location}' is not well-formed XML: {e}",
            code=error_code,
            phase="schema",
        )
        return None
    apply_conditional_inclusion(root, ctx.namespace_context, ctx.report, ctx.processor_version)
    if root.tag != clark(XSD_NS, "schema"):
        # schB5/schE6/schE10: the reference resolves to well-formed
        # XML that is not an XML Schema document.
        ctx.report.add_error(
            f"the schema '{location}' is not an XML Schema document (root element {root.tag!r})",
            code="schema-compose",
            phase="schema",
        )
        return None
    check_namespace_attribute_values(ctx, root)
    return root


def check_namespace_attribute_values(ctx: CompileContextProtocol, root: Any) -> None:
    """Reports an empty ``targetNamespace`` or import ``namespace``.

    The empty string is not a valid namespace name: a schema declaring
    ``targetNamespace=""`` (schZ014_b) and an ``xs:import`` carrying
    ``namespace=""`` (schZ014_a) are both invalid. Absence is written
    by omitting the attribute, never by an empty value.
    """
    if root.get("targetNamespace") == "":
        ctx.report.add_error(
            "the schema's targetNamespace must not be the empty string; "
            "omit the attribute for no namespace",
            code="declaration-attribute",
            phase="schema",
        )
    for child in list(root):
        if not isinstance(child.tag, str) or child.tag.split("}")[-1] != "import":
            continue
        if child.get("namespace") == "":
            ctx.report.add_error(
                "an import's namespace must not be the empty string; "
                "omit the attribute to import the absent namespace",
                code="declaration-attribute",
                phase="schema",
            )


def check_directive_annotation(ctx: CompileContextProtocol, tag: Any, isImport: bool) -> None:
    """Reports a repeated ``annotation`` child on include/import.

    The schema-for-schemas allows at most one ``annotation`` on
    ``xs:include`` and ``xs:import`` (unlike ``xs:redefine``, where
    the W3C suite accepts repeats - annotB025). This is a directive
    rule independent of whether the referenced resource resolves.
    """
    local = "import" if isImport else "include"
    count = sum(
        1
        for child in tag
        if isinstance(child.tag, str) and child.tag == clark(XSD_NS, "annotation")
    )
    if count > 1:
        ctx.report.add_error(
            f"<{local}> may carry at most one <annotation>; found {count}",
            code="schema-compose",
            phase="schema",
        )
    for child in tag:
        if not isinstance(child.tag, str):
            continue
        if namespace_of(child.tag) != XSD_NS:
            continue
        localChild = child.tag.split("}")[-1]
        if localChild == "annotation":
            continue
        ctx.report.add_error(
            f"<{localChild}> is not allowed inside <{local}>",
            code="declaration-child",
            phase="schema",
        )


def referenced_namespaces(ctx: CompositionContext, schemaRoot: Any) -> set[str]:
    """Returns the namespaces named by QName-valued schema attributes.

    Used to decide whether an unresolved namespace-only import is
    actually needed: a reference into the namespace that cannot be
    satisfied makes the schema invalid, an unreferenced hint does
    not.
    """
    namespaces: set[str] = set()
    for element in schemaRoot.iter():
        tag = element.tag
        if not isinstance(tag, str) or not tag.startswith(f"{{{XSD_NS}}}"):
            continue
        tokens = [element.get(attribute) for attribute in _CHAMELEON_QNAME_ATTRIBUTES]
        for attribute in _CHAMELEON_QNAME_LIST_ATTRIBUTES:
            value = element.get(attribute)
            if value:
                tokens.extend(value.split())
        for token in tokens:
            if not token or token.startswith("##"):
                continue
            try:
                resolved = ctx.namespace_context.resolve(element, token)
            except NamespaceError:
                continue
            uri = namespace_of(resolved)
            if uri is not None:
                namespaces.add(uri)
    return namespaces


def report_unresolved_import(ctx: CompositionContext, namespace: str, schemaRoot: Any) -> None:
    """Reports one unresolved namespace-only ``xs:import``.

    A namespace-only import is a hint: if a component from its
    namespace is referenced by the importing document and nothing
    satisfied the namespace, the schema is invalid; otherwise it is
    only a warning. The reference scan is scoped to the document
    that declared the import, because an import is only needed for
    references made by its own schema document.
    """
    if namespace in referenced_namespaces(ctx, schemaRoot):
        ctx.report.add_error(
            f"the import for namespace '{namespace}' has no "
            "schemaLocation and a component from that namespace "
            "is referenced",
            code="import-unresolved",
            phase="schema",
        )
    else:
        ctx.report.add_warning(
            f"the import for namespace '{namespace}' has no "
            "schemaLocation and no schema was supplied for it",
            code="import-unresolved",
            phase="schema",
        )


def apply_chameleon_namespace(ctx: CompositionContext, includedRoot: Any, namespace: str) -> None:
    """Simulates XSD chameleon pre-processing (Appendix F.1).

    A no-namespace document absorbed into *namespace* has every QName
    reference that carries no namespace rewritten into it; references
    that already name a namespace (by prefix or default declaration)
    are left untouched. Applying the transform to the tree lets the
    ordinary ER run resolve the absorbed components and nested
    chameleon includes inherit the same target namespace.
    """
    for element in includedRoot.iter():
        tag = element.tag
        if not isinstance(tag, str) or not tag.startswith(f"{{{XSD_NS}}}"):
            continue
        for attribute in _CHAMELEON_QNAME_ATTRIBUTES:
            value = element.get(attribute)
            if value is None:
                continue
            rewritten = chameleon_qname(ctx, element, value, namespace)
            if rewritten != value:
                element.set(attribute, rewritten)
        for attribute in _CHAMELEON_QNAME_LIST_ATTRIBUTES:
            value = element.get(attribute)
            if value is None:
                continue
            tokens = [chameleon_qname(ctx, element, token, namespace) for token in value.split()]
            element.set(attribute, " ".join(tokens))


def chameleon_qname(ctx: CompositionContext, element: Any, token: str, namespace: str) -> str:
    """Rewrites one lexical QName with an absent namespace, else keeps it."""
    if not token or token.startswith(("##", "{")):
        return token
    try:
        resolved = ctx.namespace_context.resolve(element, token)
    except NamespaceError:
        return token
    if namespace_of(resolved) is None:
        return clark(namespace, local_name(resolved))
    return token


def append_named_components(
    ctx: CompositionContext, includedRoot: Any, schemaRoot: Any, namespace: str | None
) -> None:
    """Appends the named components of an included schema to the main tree.

    In strict mode the namespace each component was declared in is
    recorded so its expanded name reflects the source document
    rather than the main schema's target namespace. That document's
    form defaults are recorded too, so local declarations resolve
    qualification against their own schema instead of the host.
    """
    strict = getattr(ctx.mode, "namespaces", "legacy") == "strict"
    sourceDefaults = (
        includedRoot.get("elementFormDefault"),
        includedRoot.get("attributeFormDefault"),
    )
    sourceXPathDefault = includedRoot.get("xpathDefaultNamespace")
    from pyxsd.schema_checks import check_form_defaults

    check_form_defaults(ctx, includedRoot)
    for component in list(includedRoot):
        if component.tag.split("}")[-1] in _COMPOSABLE_TAGS:
            if strict:
                for element in component.iter():
                    ctx.namespace_overrides.setdefault(id(element), namespace)
                    ctx.form_defaults.setdefault(id(element), sourceDefaults)
                    if sourceXPathDefault is not None:
                        ctx.xpath_default_namespaces.setdefault(id(element), sourceXPathDefault)
            schemaRoot.append(component)
    return None


def splice_redefine(
    ctx: CompositionContext,
    redefineTag: Any,
    schemaRoot: Any,
    baseDir: Path,
    visited: set[str],
) -> None:
    """Splices an ``xs:redefine`` block.

    The referenced schema's named components are spliced in first;
    components redefined in the block are renamed to
    ``Name|base`` so the redefining definition can legally derive
    from the original (the standard internal-name trick). ``base``
    attributes inside the block that name the redefined component
    are rewritten to the renamed original.
    """
    check_redefine_structure(ctx, redefineTag)
    location = redefineTag.get("schemaLocation")
    if not location:
        ctx.report.add_error(
            "a redefine tag has no schemaLocation; the schema could not be composed",
            code="schema-compose",
        )
        return None
    includedRoot = parse_included_schema(ctx, location, baseDir, missing_severity="warning")
    if includedRoot is None:
        # A redefine that actually redefines a component needs its
        # base document: without it the redefined component cannot be
        # found, which is a composition rule violation (not merely an
        # unresolvable hint). A block carrying only annotations is
        # just the missing-resource warning (W3C a009/annotB025).
        hasContent = any(
            isinstance(child.tag, str) and child.tag.split("}")[-1] != "annotation"
            for child in list(redefineTag)
        )
        if hasContent:
            ctx.report.add_error(
                f"the base schema '{location}' for the redefinition could not be opened",
                code="schema-compose",
                phase="schema",
            )
        return None
    mainNS = schemaRoot.get("targetNamespace")
    includedNS = includedRoot.get("targetNamespace")
    if includedNS:
        ctx.composed_target_namespaces.add(includedNS)
    includedPath = (baseDir / location).resolve()
    if includedNS is not None and mainNS is not None and includedNS != mainNS:
        # A redefine may target a document in the redefining schema's
        # namespace or a no-namespace (chameleon) document; redefining
        # a third namespace would introduce the renamed components
        # into the wrong namespace. A no-namespace redefiner is the
        # disputed circular case (W3C schU1), left unresolved.
        ctx.report.add_error(
            f"the redefined schema '{location}' declares targetNamespace "
            f"'{includedNS}', which does not match the redefining schema's "
            f"namespace ({mainNS or 'none'})",
            code="compose-invalid",
            phase="schema",
        )
    if str(includedPath) in visited:
        if mainNS is None:
            # A no-namespace (chameleon) redefiner caught in a cycle
            # is the disputed W3C schU1 case; it stays a skipped
            # repetition rather than a rule violation.
            ctx.report.add_warning(
                f"the schema '{location}' is already being composed; "
                "the circular redefine is skipped",
                code="compose-cycle",
            )
        else:
            # XSD 1.1 §4.2.4: a schema document must not redefine,
            # directly or indirectly, a component of a document that
            # (transitively) redefines it. Unlike an include cycle,
            # this is a composition rule violation, not a harmless
            # repetition (IBM S4_2_4 cyclic redefine).
            ctx.report.add_error(
                f"the schema '{location}' is already being composed; "
                "a cyclic redefine is not allowed",
                code="compose-invalid",
                phase="schema",
            )
        return None
    redefined: list[tuple[str, str]] = []
    for child in list(redefineTag):
        local = child.tag.split("}")[-1]
        if local in ("complexType", "simpleType", "group", "attributeGroup") and child.get("name"):
            redefined.append((local, child.get("name")))
    check_redefine_targets(ctx, includedRoot, location, redefined)
    check_redefine_duplicates(ctx, includedPath, location, redefined)
    check_redefine_restrictions(ctx, includedRoot, location, redefineTag)
    redefinedNames = {name for _, name in redefined}
    for component in list(includedRoot):
        local = component.tag.split("}")[-1]
        name = component.get("name")
        if (
            local in ("complexType", "simpleType", "group", "attributeGroup")
            and name in redefinedNames
        ):
            component.set("name", f"{name}|base")
    ctx.compose_stack.append(str(includedPath))
    try:
        splice_composed_schemas(
            ctx, includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
    finally:
        ctx.compose_stack.pop()
    # The redefined document's components come from another schema
    # document; scope ``id`` uniqueness provenance to it.
    for element in includedRoot.iter():
        ctx.composed_element_ids.add(id(element))
        ctx.composed_schema_roots.setdefault(id(element), includedRoot)
    append_named_components(ctx, includedRoot, schemaRoot, mainNS)
    rebind_redefine_references(ctx, redefineTag, redefinedNames, includedNS, mainNS)
    for child in list(redefineTag):
        schemaRoot.append(child)
    return None


def splice_override(
    ctx: CompositionContext,
    overrideTag: Any,
    schemaRoot: Any,
    baseDir: Path,
    visited: set[str],
) -> None:
    """Splices an ``xs:override`` block (XSD 1.1 §4.2.5).

    Unlike ``xs:redefine`` an override need not modify an existing
    component: a declaration matching nothing in the target set is
    silently ignored, not an error (so a brand-new declaration in the
    overriding document is available, but one written *inside* the
    override is not added). A match replaces the base component
    wholesale — the base copy is dropped from the composed tree so a
    reference from inside the overriding declaration resolves to the
    override, not to the definition it replaced (over011/over014).
    The target set is the composed base document (its own includes
    and overrides included), which is why the base is spliced before
    the match.
    """
    targets = collect_override_targets(ctx, overrideTag)
    check_override_target_duplicates(ctx, targets)
    location = overrideTag.get("schemaLocation")
    if not location:
        ctx.report.add_error(
            "an override tag has no schemaLocation; the schema could not be composed",
            code="schema-compose",
        )
        return None
    includedRoot = parse_included_schema(ctx, location, baseDir, missing_severity="warning")
    if includedRoot is None:
        # An override with content needs its base document: without it
        # the target set cannot be established. An override carrying
        # only annotations is just the missing-resource warning.
        hasContent = any(
            isinstance(child.tag, str) and child.tag.split("}")[-1] != "annotation"
            for child in list(overrideTag)
        )
        if hasContent:
            ctx.report.add_error(
                f"the base schema '{location}' for the override could not be opened",
                code="schema-compose",
                phase="schema",
            )
        return None
    mainNS = schemaRoot.get("targetNamespace")
    includedNS = includedRoot.get("targetNamespace")
    includedPath = (baseDir / location).resolve()
    if includedNS is not None and (mainNS is None or includedNS != mainNS):
        # XSD 1.1 §4.2.5 clause 2: a namespaced base may only be
        # overridden by a document with the identical target
        # namespace; a no-namespace base may be ported into a
        # namespaced overrider (chameleon, below).
        ctx.report.add_error(
            f"the overridden schema '{location}' declares targetNamespace "
            f"'{includedNS}', which does not match the overriding schema's "
            f"namespace ({mainNS or 'none'})",
            code="compose-invalid",
            phase="schema",
        )
    if str(includedPath) in visited:
        ctx.report.add_warning(
            f"the schema '{location}' is already being composed; the circular override is skipped",
            code="compose-cycle",
        )
        return None
    if includedNS is None and mainNS is not None:
        # Chameleon pre-processing (Appendix F.2 on top of F.1): a
        # no-namespace base is ported into the overrider's namespace
        # before the override is applied.
        includedRoot.set("targetNamespace", mainNS)
        apply_chameleon_namespace(ctx, includedRoot, mainNS)
        includedNS = mainNS
    if includedNS:
        ctx.composed_target_namespaces.add(includedNS)
    check_override_duplicates(ctx, includedPath, location, targets)
    # Compose the base document first so its effective component set
    # (its own includes/overrides included) is the override's target.
    ctx.compose_stack.append(str(includedPath))
    try:
        splice_composed_schemas(
            ctx, includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
    finally:
        ctx.compose_stack.pop()
    targetKeys = {(space, name) for space, name, _ in targets}
    present: set[tuple[str, str]] = set()
    for component in list(includedRoot):
        if not isinstance(component.tag, str):
            continue
        local = component.tag.split("}")[-1]
        space = _OVERRIDE_SYMBOL_SPACES.get(local)
        name = component.get("name")
        if space is None or not name or name.endswith("|base"):
            continue
        present.add((space, name))
        if (space, name) in targetKeys:
            # The overriding definition replaces the base wholesale:
            # dropping the base copy makes the override the unique
            # plain-named component, so every reference (including a
            # self-reference inside the override) resolves to it. The
            # base cannot be kept under the redefine ``|base`` name
            # because an attribute or notation name is NCName-checked.
            includedRoot.remove(component)
    # The base document's components come from another schema
    # document; scope ``id`` uniqueness provenance to it.
    for element in includedRoot.iter():
        ctx.composed_element_ids.add(id(element))
        ctx.composed_schema_roots.setdefault(id(element), includedRoot)
    append_named_components(
        ctx, includedRoot, schemaRoot, includedNS if includedNS is not None else mainNS
    )
    for space, name, child in targets:
        if (space, name) in present:
            # XSD 1.1 §3.4.2.4 / §3.1.2: for a type defined *within*
            # ``xs:override`` the relevant default open content (and
            # default attribute group) is the overridden document's,
            # not the overriding document's. Scoping the override
            # children to the base root keeps the host's defaults off
            # them (open043/open045).
            for element in child.iter():
                ctx.composed_schema_roots.setdefault(id(element), includedRoot)
            schemaRoot.append(child)
    # The base document has been composed; a later include/import of
    # the same document must not splice it a second time (XSD
    # composition treats one document once, §4.2.3). ``xs:override``
    # itself does not consult this set, so two overrides of the same
    # base are still reprocessed and reported as a conflict.
    ctx.composed_documents.add(str(includedPath))
    return None


def collect_override_targets(
    ctx: CompositionContext, overrideTag: Any
) -> list[tuple[str, str, Any]]:
    """Returns the ``(symbol space, name, child)`` of an override block.

    Reports ``override-invalid`` for a child outside the XSD 1.1
    §4.2.5 grammar or one that does not name a component; the
    offending child is dropped so it is not spliced in.
    """
    targets: list[tuple[str, str, Any]] = []
    for child in list(overrideTag):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.split("}")[-1]
        if local == "annotation":
            continue
        space = _OVERRIDE_SYMBOL_SPACES.get(local)
        if space is None:
            ctx.report.add_error(
                f"<override> does not allow a '{local}' child",
                code="override-invalid",
                phase="schema",
            )
            continue
        name = child.get("name")
        if not name:
            ctx.report.add_error(
                f"the <override> child '{local}' must name a schema component",
                code="override-invalid",
                phase="schema",
            )
            continue
        targets.append((space, name, child))
    return targets


def check_override_target_duplicates(
    ctx: CompositionContext, targets: list[tuple[str, str, Any]]
) -> None:
    """Reports one component named twice inside a single override block."""
    seen: set[tuple[str, str]] = set()
    for space, name, _ in targets:
        key = (space, name)
        if key in seen:
            ctx.report.add_error(
                f"the component '{name}' is declared more than once in <override>",
                code="override-invalid",
                phase="schema",
            )
        seen.add(key)


def check_override_duplicates(
    ctx: CompositionContext, includedPath: Path, location: str, targets: list[tuple[str, str, Any]]
) -> None:
    """Reports a base component overridden twice from unrelated ancestries.

    An override chain (the outer block targets the inner redefining
    document) has a different base key and is not a conflict; the same
    base component reached twice from the same or unrelated ancestry
    is (over022).
    """
    current = tuple(ctx.compose_stack)
    seen: list[tuple[str, str, str]] = []
    for space, name, _ in targets:
        key = (str(includedPath), space, name)
        origin = ctx.override_origins.get(key)
        if origin is None:
            seen.append(key)
            continue
        nested = origin != current and (
            _stackPrefix(origin, current) or _stackPrefix(current, origin)
        )
        if nested:
            continue
        ctx.report.add_error(
            f"the component '{name}' of the overridden schema "
            f"'{location}' is overridden more than once",
            code="override-invalid",
            phase="schema",
        )
    for key in seen:
        ctx.override_origins[key] = current


def check_redefine_targets(
    ctx: CompositionContext, includedRoot: Any, location: str, redefined: list[tuple[str, str]]
) -> None:
    """Reports a redefine of a component the base document lacks.

    A redefine may only modify an existing component, never add a
    new one. The base's own direct declarations and the definitions
    carried by its nested redefines are considered. If the base also
    includes other documents the target may live there, so the check
    is skipped rather than risk a false positive.
    """
    if any(
        isinstance(child.tag, str) and child.tag.split("}")[-1] == "include"
        for child in list(includedRoot)
    ):
        return
    declared = declared_component_kinds(ctx, includedRoot)
    for kind, name in redefined:
        if (kind, name) not in declared:
            ctx.report.add_error(
                f"the component '{name}' in <redefine> is not defined "
                f"in the redefined schema '{location}'",
                code="compose-invalid",
                phase="schema",
            )


def declared_component_kinds(ctx: CompositionContext, root: Any) -> set[tuple[str, str]]:
    """Returns ``(kind, name)`` for the global components *root* declares.

    A nested ``xs:redefine`` counts as declaring the components it
    defines, because after composition they belong to *root*'s
    namespace.
    """
    kinds: set[tuple[str, str]] = set()
    for child in list(root):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.split("}")[-1]
        if local in _COMPOSABLE_REDEFINE_KINDS and child.get("name"):
            kinds.add((local, child.get("name")))
        elif local == "redefine":
            for grandchild in list(child):
                if not isinstance(grandchild.tag, str):
                    continue
                grandLocal = grandchild.tag.split("}")[-1]
                if grandLocal in _COMPOSABLE_REDEFINE_KINDS and grandchild.get("name"):
                    kinds.add((grandLocal, grandchild.get("name")))
    return kinds


def check_redefine_duplicates(
    ctx: CompositionContext, includedPath: Path, location: str, redefined: list[tuple[str, str]]
) -> None:
    """Reports a base component redefined twice in the composition.

    Two schema documents that both redefine the same component of
    the same base document conflict: each redefine replaces the
    component, so the result is ambiguous. A redefine chain is not a
    conflict because the outer redefine targets the inner redefining
    document, a different base. A redefine reached through a shared
    include (one redefining document composing the other) is also
    not reported: the suite marks such circular/nested redefines
    implementation-defined and pyxsd preserves its historical
    resolution.
    """
    current = tuple(ctx.compose_stack)
    seen: list[tuple[str, str, str]] = []
    for kind, name in redefined:
        key = (str(includedPath), kind, name)
        origin = ctx.redefine_origins.get(key)
        if origin is None:
            seen.append(key)
            continue
        if _stackPrefix(origin, current) or _stackPrefix(current, origin):
            continue
        ctx.report.add_error(
            f"the component '{name}' of the redefined schema "
            f"'{location}' is redefined more than once",
            code="compose-invalid",
            phase="schema",
        )
    for key in seen:
        ctx.redefine_origins[key] = current


def check_redefine_structure(ctx: CompositionContext, redefineTag: Any) -> None:
    """Reports redefine-block shape violations the ER walk cannot see.

    XSD 1.0 §4.2.4: a redefine may modify only the four composable
    component kinds (``complexType``, ``simpleType``, ``group``,
    ``attributeGroup``); an ``element``, ``attribute`` or
    ``notation`` child is illegal (SUN xsd003-1.e/xsd003-2.e). A
    redefined type must derive from the original, so its derivation's
    ``base`` must name the redefined type itself (schJ2/schK2/schK3).
    ``xs:redefine`` also carries no ``namespace`` attribute (schH4).
    """
    if redefineTag.get("namespace") is not None:
        ctx.report.add_error(
            "a redefine must not carry a namespace attribute; it "
            "redefines a component of the referenced document",
            code="compose-invalid",
            phase="schema",
        )
    for child in list(redefineTag):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.split("}")[-1]
        if local == "annotation":
            continue
        name = child.get("name")
        if local in ("element", "attribute", "notation"):
            ctx.report.add_error(
                f"a {local} declaration cannot be redefined; only a "
                "complexType, simpleType, group or attributeGroup may be "
                "redefined",
                code="compose-invalid",
                phase="schema",
            )
            continue
        if local in ("simpleType", "complexType") and name:
            if not redefine_type_derives_from_self(child, name):
                ctx.report.add_error(
                    f"the redefined {local} '{name}' must derive from the original '{name}'",
                    code="compose-invalid",
                    phase="schema",
                )
        elif local == "group" and name:
            check_group_redefine_self_reference(ctx, child, name)


def check_group_redefine_self_reference(
    ctx: CompositionContext, declaration: Any, name: str
) -> None:
    """Reports a redefined group's self reference with a changed occurrence.

    The self reference stands for the original group, whose occurrence
    the redefining document may not alter: it must be exactly 1/1
    (schR3 minOccurs=0, schR4 maxOccurs=2).
    """
    for element in declaration.iter():
        if not isinstance(element.tag, str) or element.tag.split("}")[-1] != "group":
            continue
        ref = element.get("ref")
        if not ref or _qnameLocal(ref) != name:
            continue
        minimum = element.get("minOccurs")
        maximum = element.get("maxOccurs")
        if (minimum is not None and minimum != "1") or (maximum is not None and maximum != "1"):
            ctx.report.add_error(
                f"the self reference of the redefined group '{name}' must "
                "have minOccurs and maxOccurs of exactly 1",
                code="compose-invalid",
                phase="schema",
            )


def redefine_type_derives_from_self(declaration: Any, name: str) -> bool:
    """Whether a redefined type's derivation names itself as base.

    The base of a simple-type restriction or a complex-content
    derivation must be the redefined type; the local part matching
    ``name`` is enough because the surrounding redefine namespace
    rules already pin the namespace.
    """
    for element in declaration.iter():
        if not isinstance(element.tag, str):
            continue
        base = element.get("base")
        if base and _qnameLocal(base) == name:
            return True
    return False


def check_redefine_restrictions(
    ctx: CompositionContext, includedRoot: Any, location: str, redefineTag: Any
) -> None:
    """Reports an attributeGroup redefine that is not a valid restriction.

    For an ``attributeGroup`` redefinition without a self reference the
    new content must be a valid restriction of the original: it may
    not add attributes, must keep them in the original order, must
    preserve a non-optional ``use`` and a base ``fixed`` value, and
    must keep every required base attribute. A self reference pulls in
    the original attributes, so a declared attribute that the original
    already contributes is a duplicate. Only the base document's own
    declaration is inspected; if it cannot be found (or uses
    unresolved refs) the check is skipped rather than guessing.
    """
    for declaration in list(redefineTag):
        if (
            not isinstance(declaration.tag, str)
            or declaration.tag.split("}")[-1] != "attributeGroup"
        ):
            continue
        name = declaration.get("name")
        if not name:
            continue
        baseDecl = find_base_component(includedRoot, "attributeGroup", name)
        if baseDecl is None:
            continue
        baseUses = attribute_uses(baseDecl)
        if baseUses is None:
            continue
        declared: list[tuple[str, Any]] = []
        hasSelfReference = False
        selfRefCount = 0
        skip = False
        for child in list(declaration):
            if not isinstance(child.tag, str):
                continue
            local = child.tag.split("}")[-1]
            if local == "attribute":
                if child.get("ref") is not None:
                    # A referenced attribute cannot be compared
                    # structurally.
                    skip = True
                    break
                childName = child.get("name")
                if childName:
                    declared.append((childName, child))
            elif local == "attributeGroup":
                ref = child.get("ref")
                if ref is not None and _qnameLocal(ref) == name:
                    hasSelfReference = True
                    selfRefCount += 1
                else:
                    # A reference to another group hides its
                    # attributes; the content cannot be compared.
                    skip = True
                    break
        if selfRefCount > 1:
            ctx.report.add_error(
                f"attributeGroup '{name}' references itself {selfRefCount} "
                "times; the original attributes would be contributed more "
                "than once",
                code="compose-invalid",
                phase="schema",
            )
        if skip:
            continue
        if hasSelfReference:
            baseNames = {baseName for baseName, _ in baseUses}
            for childName, _ in declared:
                if childName in baseNames:
                    ctx.report.add_error(
                        f"the attribute '{childName}' is already contributed "
                        f"by the redefined attributeGroup '{name}'",
                        code="compose-invalid",
                        phase="schema",
                    )
            continue
        basePositions = {baseName: index for index, (baseName, _) in enumerate(baseUses)}
        baseByName = dict(baseUses)
        derivedNames = [childName for childName, _ in declared]
        for baseName, baseAttr in baseUses:
            if baseName not in derivedNames and baseAttr.get("use") == "required":
                ctx.report.add_error(
                    f"the required attribute '{baseName}' of the redefined "
                    f"attributeGroup '{name}' is missing",
                    code="compose-invalid",
                    phase="schema",
                )
        position = 0
        for childName, childAttr in declared:
            if childName not in basePositions:
                ctx.report.add_error(
                    f"the attribute '{childName}' is not in the redefined attributeGroup '{name}'",
                    code="compose-invalid",
                    phase="schema",
                )
                continue
            nextPosition = basePositions[childName]
            if nextPosition < position:
                ctx.report.add_error(
                    f"the attributes of the redefined attributeGroup "
                    f"'{name}' are not in the original order",
                    code="compose-invalid",
                    phase="schema",
                )
                break
            position = nextPosition
            baseAttr = baseByName[childName]
            if baseAttr.get("use") not in (None, "optional") and childAttr.get(
                "use"
            ) != baseAttr.get("use"):
                ctx.report.add_error(
                    f"the attribute '{childName}' of the redefined "
                    f"attributeGroup '{name}' changes its use",
                    code="compose-invalid",
                    phase="schema",
                )
            if baseAttr.get("fixed") is not None and childAttr.get("fixed") is None:
                ctx.report.add_error(
                    f"the attribute '{childName}' of the redefined "
                    f"attributeGroup '{name}' drops its fixed value",
                    code="compose-invalid",
                    phase="schema",
                )


def find_base_component(root: Any, kind: str, name: str) -> Any | None:
    """Returns *root*'s declaration of ``(kind, name)`` if it has one.

    A direct declaration is preferred; a definition carried by a
    nested ``xs:redefine`` also belongs to the document's namespace
    after composition, so it is accepted as a fallback.
    """
    for child in list(root):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.split("}")[-1]
        if local == kind and child.get("name") == name:
            return child
        if local == "redefine":
            for grandchild in list(child):
                if (
                    isinstance(grandchild.tag, str)
                    and grandchild.tag.split("}")[-1] == kind
                    and grandchild.get("name") == name
                ):
                    return grandchild
    return None


def attribute_uses(declaration: Any) -> list[tuple[str, Any]] | None:
    """Returns ``(name, element)`` for a declaration's direct attributes.

    ``None`` means the declaration cannot be compared structurally: it
    contains an attribute or attributeGroup reference whose target is
    resolved elsewhere.
    """
    uses: list[tuple[str, Any]] = []
    for child in list(declaration):
        if not isinstance(child.tag, str):
            continue
        local = child.tag.split("}")[-1]
        if local == "attributeGroup" or (local == "attribute" and child.get("ref") is not None):
            return None
        if local == "attribute":
            childName = child.get("name")
            if childName:
                uses.append((childName, child))
    return uses


def rebind_redefine_references(
    ctx: CompositionContext,
    redefineTag: Any,
    redefinedNames: set[str],
    includedNS: str | None,
    mainNS: str | None,
) -> None:
    """Rewrites a redefine block's self-references to the original.

    ``base``/``ref`` values that name a redefined component refer to
    the *original* in the redefining block, so they are repointed at
    the renamed ``Name|base`` component. In a chameleon redefine the
    base component is ported into the redefining namespace, so an
    unqualified self-reference names no namespace and is reported
    instead of being silently rebound.
    """
    for child in list(redefineTag):
        for element in child.iter():
            if not isinstance(element.tag, str):
                continue
            tagLocal = element.tag.split("}")[-1]
            base = element.get("base")
            if base and base.split(":")[-1] in redefinedNames:
                element.set("base", _redefined_qname(base))
            localRef = element.get("ref")
            if (
                tagLocal in ("group", "attributeGroup")
                and localRef
                and localRef.split(":")[-1] in redefinedNames
            ):
                element.set("ref", _redefined_qname(localRef))
            if includedNS is not None or mainNS is None:
                continue
            candidates = []
            if base and _qnameLocal(base) in redefinedNames:
                candidates.append(base)
            if (
                tagLocal in ("group", "attributeGroup")
                and localRef
                and _qnameLocal(localRef) in redefinedNames
            ):
                candidates.append(localRef)
            for value in candidates:
                if qname_namespace(ctx, element, value) is None:
                    ctx.report.add_error(
                        f"the redefinition self reference '{value}' "
                        "must be qualified into the redefining "
                        f"namespace '{mainNS}'",
                        code="compose-invalid",
                        phase="schema",
                    )


def qname_namespace(ctx: CompositionContext, element: Any, value: str) -> str | None:
    """Resolves one QName to its namespace, or ``None`` if unbound/absent."""
    try:
        resolved = ctx.namespace_context.resolve(element, value)
    except NamespaceError:
        return None
    return namespace_of(resolved)


def inject_xml_namespace_attributes(ctx: CompositionContext, schemaRoot: Any) -> None:
    """Registers the implicit XML-namespace attributes.

    The ``xml`` namespace has no schema document, but ``xml:space``,
    ``xml:lang``, ``xml:base`` and ``xml:id`` may appear on any
    element. Registering them as global attributes (typed as
    strings) lets attribute references into the XML namespace
    resolve without a vendored XML-namespace schema.
    """
    for local in ("space", "lang", "base", "id"):
        attributeElement = ET.Element(
            clark(XSD_NS, "attribute"),
            {"name": local, "type": clark(XSD_NS, "string")},
        )
        ctx.namespace_overrides[id(attributeElement)] = XML_NS
        ctx.injected_builtin_ids.add(id(attributeElement))
        schemaRoot.append(attributeElement)


def inject_xsi_namespace_attributes(ctx: CompositionContext, schemaRoot: Any) -> None:
    """Registers the built-in XML-Schema-instance attribute declarations.

    The xsi namespace's schema is available in every schema without a
    document (XSD 1.1 §4.2.3): a namespace-only ``xs:import`` of it
    resolves, and ``<xs:attribute ref="xsi:type"/>`` and friends
    resolve to these declarations. As for the built-in ``xsi:nil``
    typing already enforced instance-side, ``type`` is ``xs:QName``
    and ``nil`` is ``xs:boolean``. User declarations in the xsi
    namespace stay illegal (the declaration-legality check exempts
    exactly these injected components).
    """
    for local, typeName in _XSI_BUILTIN_ATTRIBUTES:
        attributeElement = ET.Element(
            clark(XSD_NS, "attribute"),
            {"name": local, "type": clark(XSD_NS, typeName)},
        )
        ctx.namespace_overrides[id(attributeElement)] = XSI_NS
        ctx.injected_builtin_ids.add(id(attributeElement))
        schemaRoot.append(attributeElement)


def inject_xlink_namespace_attributes(ctx: CompositionContext, schemaRoot: Any) -> None:
    """Registers the built-in XLink attribute declarations.

    A conforming processor resolves the XLink namespace to a schema,
    so ``<xs:attribute ref="xlink:type"/>`` resolves even when the
    (remote) ``xlink.xsd`` is unreachable and a namespace-only
    ``xs:import`` of the XLink namespace is satisfied. As with the
    ``xml``/``xsi`` built-ins, a user document targeting the XLink
    namespace is not exempted (its declarations stay subject to the
    ordinary legality checks); only these injected components are
    recorded as built-ins.
    """
    for local in _XLINK_BUILTIN_ATTRIBUTES:
        attributeElement = ET.Element(
            clark(XSD_NS, "attribute"),
            {"name": local, "type": clark(XSD_NS, "string")},
        )
        ctx.namespace_overrides[id(attributeElement)] = XLINK_NS
        ctx.injected_builtin_ids.add(id(attributeElement))
        schemaRoot.append(attributeElement)


def apply_local_target_namespaces(ctx: CompositionContext, schemaRoot: Any) -> None:
    """Applies and validates XSD 1.1 ``targetNamespace`` on locals.

    XSD 1.1 §3.2.3/§3.3.3 let a local element or attribute
    declaration state its own target namespace. The value names the
    declaration's expanded name, so it is recorded in
    ``namespace_overrides`` (recovering TargetNS target001/003 and
    IBM targetNamespace_005). The accompanying constraints are
    enforced as schema errors:

    * ``form`` and ``targetNamespace`` may not both appear
      (§3.2.3.6.2 / §3.3.3.4.2, s3_2_3si03/si06);
    * when the value differs from the schema's target namespace the
      declaration must have a ``complexType`` ancestor
      (§3.2.3.6.3.1 / §3.3.3.4.3.1, s3_2_3si04/si07);
    * inside a ``restriction`` whose base is ``xs:anyType`` the value
      may not differ from the schema's target namespace
      (§3.3.3.4.3.2, s3_2_3si05/si08).
    """
    schemaNS = schemaRoot.get("targetNamespace")

    def walk(
        element: Any,
        derivation: str | None,
        has_complex_type: bool,
        in_anytype_restriction: bool,
    ) -> None:
        for child in element:
            tag = child.tag.split("}")[-1]
            childDerivation = derivation
            childHasComplexType = has_complex_type
            childAnyTypeRestriction = in_anytype_restriction
            if tag == "complexType":
                childHasComplexType = True
            elif tag == "restriction":
                childDerivation = "restriction"
                base = child.get("base")
                if base is not None and base.split(":")[-1] == "anyType":
                    childAnyTypeRestriction = True
            elif tag == "extension":
                childDerivation = "extension"
            if tag in ("element", "attribute"):
                value = child.get("targetNamespace")
                if value is not None:
                    error: str | None = None
                    if child.get("form") is not None:
                        error = (
                            f"the local {tag} declaration may not carry both "
                            "'form' and 'targetNamespace'"
                        )
                    elif element is schemaRoot:
                        error = (
                            f"the global {tag} declaration '{child.get('name')}' "
                            "may not carry 'targetNamespace'"
                        )
                    elif child.get("ref") is not None:
                        error = f"a {tag} reference may not carry 'targetNamespace'"
                    elif value != schemaNS and derivation != "restriction":
                        error = (
                            f"a local {tag} declaration whose 'targetNamespace' "
                            "differs from the schema's is only allowed within "
                            "an xs:restriction"
                        )
                    elif in_anytype_restriction and value != schemaNS:
                        error = (
                            f"the local {tag} declaration's 'targetNamespace' "
                            "may not differ from the schema's inside a "
                            "restriction of xs:anyType"
                        )
                    if error is None:
                        ctx.namespace_overrides[id(child)] = value
                    else:
                        ctx.report.add_error(error, code="declaration-attribute", phase="schema")
            walk(
                child,
                childDerivation,
                childHasComplexType,
                childAnyTypeRestriction,
            )

    walk(schemaRoot, None, False, False)


def refine_wildcard_specs(ctx: CompositionContext, schema_root: Any) -> None:
    """Expands the XSD 1.1 ``notQName`` names on every wildcard ER.

    The ER constructors run before the schema's prefix bindings are
    attached, so they register a raw spec; the declaration walk's
    wildcard grammar/consistency check and the derivation checks
    consult the registered spec, and the attribute-wildcard
    derivation check runs on a complex type *before* the walk
    reaches the type's ``xs:anyAttribute`` children. Refining every
    wildcard once here (same traversal as the walk) makes the
    registered specs expanded for all consumers, binding included.
    """
    seen: set[int] = set()
    stack = [schema_root]
    while stack:
        er = stack.pop()
        if er is None or id(er) in seen:
            continue
        seen.add(id(er))
        refine = getattr(er, "refineWildcardSpec", None)
        if callable(refine):
            refine()
        stack.extend(getattr(er, "processedChildren", None) or ())
