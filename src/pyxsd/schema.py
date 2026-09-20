"""The public ``Schema`` object: one compiled XML Schema and its classes.

``Schema.compile`` runs the shared compilation pipeline
(:func:`_compile_into_context`) without an instance document:
fatal input problems raise :class:`~pyxsd.exceptions.PyXSDError`, while
schema-legality problems are collected on the schema's
:class:`~pyxsd.validation.ValidationReport` for
:meth:`Schema.require_valid` to surface.
"""

import logging
import os.path
import warnings
from pathlib import Path
from typing import IO, Any
from xml.etree import ElementTree as ET

from pyxsd.binding import ParseModes
from pyxsd.cli import _load_module_from_file
from pyxsd.document import Document
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
    componentKind,
)
from pyxsd.exceptions import PyXSDError, PyXSDWarning, ValidationError
from pyxsd.instance_binding import bind_instance
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.schema_base import SchemaBase
from pyxsd.schema_checks import (
    apply_default_attributes,
    apply_default_open_content,
    build_substitution_groups,
    check_alternative_table_edc,
    check_alternatives,
    check_conditional_type_substitutable,
    check_content_kind_derivation,
    check_form_defaults,
    check_group_redefine_restrictions,
    check_keyref_references,
    check_notation_uses,
    check_simple_content_restriction_base,
    check_substitution_group_exclusions,
    check_type_references,
    check_value_constraints,
    report_declaration_issues,
    report_open_content_derivations,
)
from pyxsd.schema_composition import (
    CompositionContext,
    apply_local_target_namespaces,
    check_namespace_attribute_values,
    collect_additional_schemas,
    inject_xlink_namespace_attributes,
    inject_xml_namespace_attributes,
    inject_xsi_namespace_attributes,
    refine_wildcard_specs,
    schema_composition_context,
    splice_additional_schemas,
    splice_composed_schemas,
)
from pyxsd.schema_context import (
    SchemaContext,
    active_context,
    context_report,
    remember_components,
)
from pyxsd.schema_hints import absolute_schema_location_pairs, resolve_schema_hint
from pyxsd.validation import ValidationReport
from pyxsd.versioning import apply_conditional_inclusion

logger = logging.getLogger(__name__)


class _CompileHost:
    """Minimal parser-like host for ``Schema.compile``.

    The compilation pipeline and the ElementRepresentative APIs read a
    handful of attributes off the running parser (via ``host``/``parser``
    parameters). A grep over the moved code (``pyxsd.schema_composition``,
    ``pyxsd.schema_checks``, ``pyxsd.content_model``, the
    ``element_representatives`` package and ``pyxsd.schema_base``)
    enumerates exactly: ``report``, ``components``, ``mode``,
    ``classes``, ``namespaceContext`` and ``namespaceSchemas`` (the
    latter only read by the pipeline body itself). Every container
    aliases the corresponding :class:`CompositionContext` field, so the
    products compiled through this host are the context's objects.
    """

    components: Any

    def __init__(self, ctx: CompositionContext) -> None:
        self.report = ctx.report
        self.classes = ctx.classes
        self.mode = ctx.mode
        self.namespaceContext = ctx.namespace_context
        self.namespaceSchemas = ctx.namespace_schemas
        # Replaced by the pipeline once the schema representative
        # exists; ``None`` up to that point resolves lookups onto the
        # module-level registry exactly as an un-run parser does.
        self.components = None


class Schema:
    """A compiled XML Schema and the classes generated from it."""

    #: Generated classes keyed by declaration name (bare local names
    #: and, where the type has an expanded name, Clark names).
    classes: dict[str, type[SchemaBase]]
    #: The component table registered during compilation.
    components: Any
    #: Prefix-to-URI bindings captured while parsing the schema documents.
    namespace_context: Any
    #: The binding policy the compilation ran under.
    mode: Any
    #: The main schema document's ``targetNamespace``, if it declared one.
    target_namespace: str | None
    #: Schema-phase validation issues (composition, declaration, and
    #: derivation problems). Instance-phase issues never land here.
    report: ValidationReport
    #: The source the schema was compiled from, as given to ``compile``.
    source: str | Path | os.PathLike[str] | IO[str] | None
    #: The parser-like host the compilation ran under (reached from a
    #: generated class via its ``schema`` stamp). Per-parse binding
    #: indexes attach here.
    _host: Any

    def __init__(
        self,
        classes: dict[str, type[SchemaBase]],
        components: Any,
        namespace_context: Any,
        mode: Any,
        target_namespace: str | None,
        report: ValidationReport,
        source: str | Path | os.PathLike[str] | IO[str] | None,
        schema_context: SchemaContext | None = None,
    ) -> None:
        self.classes = classes
        self.components = components
        self.namespace_context = namespace_context
        self.mode = mode
        self.target_namespace = target_namespace
        self.report = report
        self.source = source
        self._schema_context = schema_context
        self._host = None

    @classmethod
    def compile(
        cls,
        source: str | Path | os.PathLike[str] | IO[str],
        *,
        mode: Any = ParseModes.STRICT,
        namespace_schemas: dict[str, str | Path] | None = None,
        overlay: str | Path | os.PathLike[str] | None = None,
        namespace_context: NamespaceContext | None = None,
        schema_location_pairs: list[tuple[str | None, str]] | None = None,
    ) -> "Schema":
        """Compiles *source* into a :class:`Schema`.

        - ``source`` - a schema file path or a file object open for
          reading. An unopenable file, malformed XML, or a document
          whose root is not ``xs:schema`` raise
          :class:`~pyxsd.exceptions.PyXSDError`; schema-legality
          problems are collected on :attr:`report` instead.

        - ``mode`` - a :class:`~pyxsd.binding.BindingPolicy` (usually a
          :class:`~pyxsd.binding.ParseModes` preset). Defaults to
          :attr:`~pyxsd.binding.ParseModes.STRICT`.

        - ``namespace_schemas`` - an optional ``{namespace_uri: path}``
          mapping supplying schemas for namespaces referenced by
          ``xs:import`` without a ``schemaLocation``.

        - ``overlay`` - an optional overlay class file, loaded after
          class generation (experimental; see
          :func:`load_overlay_classes`).

        - ``namespace_context`` - an existing prefix-to-URI binding
          context for the schema documents' bindings to accumulate
          into, instead of a fresh one (:func:`parse` passes the
          context that already holds the instance's bindings, so one
          context accumulates them all).

        - ``schema_location_pairs`` - extra ``(namespace, location)``
          pairs from an instance document's ``xsi:schemaLocation``
          beyond the main schema. Advisory hints: consulted in strict
          namespace mode only, and one that cannot be loaded is a
          ``schema-hint`` warning (:func:`parse` passes the instance's
          pairs; a schema-only compile has none).

        Use :meth:`require_valid` to reject a schema whose report
        holds errors.
        """
        if isinstance(source, (str, os.PathLike)):
            xml_path = Path(source).resolve().parent
        else:
            xml_path = Path.cwd()
        ctx = CompositionContext(
            mode=mode,
            namespace_schemas=dict(namespace_schemas or {}),
            namespace_context=(
                namespace_context if namespace_context is not None else NamespaceContext()
            ),
            report=ValidationReport(),
            classes={},
            xml_path=xml_path,
            schema_context=SchemaContext(components=ComponentTable()),
            namespace_overrides={},
            injected_builtin_ids=set(),
            form_defaults={},
            xpath_default_namespaces={},
            composed_element_ids=set(),
            composed_schema_roots={},
            directive_ids={},
            compose_stack=[],
            redefine_origins={},
            override_origins={},
            resolved_imports=set(),
            hint_schema_paths=set(),
            composed_target_namespaces=set(),
        )
        # Without an instance document there are no schemaLocation
        # hints of our own; the caller (:func:`parse`) may forward the
        # instance document's own pairs. Namespace-supplied schemas
        # still resolve relative to the schema's own directory.
        ctx.additional_schemas = collect_additional_schemas(
            ctx, source, schema_location_pairs or []
        )
        host = _CompileHost(ctx)
        schema = _compile_into_context(ctx, source, host)
        if overlay:
            load_overlay_classes(schema, overlay, fallback_dir=xml_path)
        return schema

    def require_valid(self) -> None:
        """Raises :class:`pyxsd.ValidationError` if the report has errors."""
        if self.report.has_errors:
            raise ValidationError(
                f"the schema is not valid: {len(self.report.errors)} error(s); "
                "inspect ValidationError.report",
                self.report,
            )

    def parse(self, xml: str | Path | os.PathLike[str] | IO[str] | ET.Element) -> "Document":
        """Binds *xml* against this schema and returns a :class:`Document`.

        - ``xml`` - the instance document: a file path, a file object
          open for reading, or a parsed ``xml.etree.ElementTree.Element``
          used as the document root directly. An unopenable file or
          malformed XML raises :class:`~pyxsd.exceptions.PyXSDError`
          (the fatal-input rule); content problems instead land on the
          document's report.

        Each parse binds with its own fresh instance report, so a
        schema may serve many documents without their findings mixing:
        the returned document's :attr:`~pyxsd.document.Document.report`
        merges this schema's (frozen) compilation issues with the
        document's own binding issues. ``Document.root`` is ``None``
        when the document root matched no global element declaration
        (the ``unknown-root`` issue records why).
        """
        if isinstance(xml, ET.Element):
            tree = xml
        else:
            logger.debug("The XML file is being parsed by the ElementTree library...")
            try:
                tree = parse_with_namespaces(xml, self.namespace_context)
            except OSError as e:
                raise PyXSDError(f"the xml input could not be read: {e}") from e
            except ET.ParseError as e:
                raise PyXSDError(f"the xml file is not well-formed XML: {e}") from e
            logger.debug("XML file parsed by the ElementTree library successfully...")

        instance_report = ValidationReport()
        context = self._schema_context
        if context is None:
            context = SchemaContext(components=self.components)
            self._schema_context = context
        # XSD 1.1 attribute inheritance and conditional type assignment
        # both need to relate a bound element to its ancestors: the
        # parent links are indexed once per parse (fresh dicts, so
        # concurrent parses never see each other's stale ids), and the
        # governing class of each bound element is recorded as binding
        # proceeds. Binding code (schema_base) reads both back through
        # the host the generated classes reach via their ``schema``
        # stamp.
        host = self._host
        if host is not None:
            host._elementParents = {}
            host._elementTypes = {}
            stack = [tree]
            while stack:
                parent = stack.pop()
                for child in parent:
                    host._elementParents[id(child)] = parent
                    stack.append(child)
        # Binding diagnostics route through the active context's report,
        # so this parse's fresh report is installed (and restored) around
        # the binding run; generated classes keep their ``cls.schema``
        # stamp, whose report is the frozen compilation report.
        with active_context(context), context_report(context, instance_report):
            root = bind_instance(self, tree, instance_report)

        # One merged report per document: the schema phase's issues
        # followed by this parse's instance issues. Issue objects are
        # shared, so the phase tags recorded when each was found ride
        # along, and the cached object's identity is stable for
        # require_valid.
        merged = ValidationReport()
        merged.extend(self.report)
        merged.extend(instance_report)
        return Document(schema=self, root=root, source=xml, report=merged)


#: :meth:`Schema.compile` exposed as a module-level function.
compile = Schema.compile


def parse(
    xml: str | Path | os.PathLike[str] | IO[str] | ET.Element,
    *,
    xsd: str | Path | os.PathLike[str] | IO[str] | None = None,
    mode: Any = ParseModes.STRICT,
    namespace_schemas: dict[str, str | Path] | None = None,
    overlay: str | Path | os.PathLike[str] | None = None,
) -> "Document":
    """Parses *xml* against its schema and returns a :class:`Document`.

    The schema is the one named by ``xsd`` when given; otherwise the
    instance's own ``xsi:schemaLocation`` /
    ``xsi:noNamespaceSchemaLocation`` hints name it, resolved relative
    to the instance document's directory (the working directory for
    stream sources). An instance that names no schema raises
    :class:`~pyxsd.exceptions.PyXSDError`.

    - ``xml`` - the instance document: a file path, a file object
      open for reading, or a parsed ``xml.etree.ElementTree.Element``
      used as the document root directly.

    - ``xsd`` - the schema to bind against: a path or a file object
      open for reading. Defaults to the instance's schema hints.

    - ``mode``, ``namespace_schemas``, ``overlay`` - passed to
      :meth:`Schema.compile`.
    """
    context = NamespaceContext()
    # The instance is parsed once, up front: for non-element sources
    # its prefix-to-URI bindings land in *context* (shared with the
    # schema compile, mirroring the parser's one-context-accumulates-all
    # design), and the same tree object is bound below, so the hint
    # rewrite a malformed schemaLocation triggers is visible to the
    # binding pass.
    if isinstance(xml, ET.Element):
        tree = xml
    else:
        try:
            tree = parse_with_namespaces(xml, context)
        except (OSError, ET.ParseError) as e:
            raise PyXSDError(f"the xml input could not be read: {e}") from e
    # Extra ``xsi:schemaLocation`` pairs beyond the main schema are
    # advisory composition hints (strict namespace mode only), read
    # from the instance document like the main hint below and resolved
    # against the instance document's own directory.
    base_dir = Path(xml).resolve().parent if isinstance(xml, (str, os.PathLike)) else Path.cwd()
    pairs = [] if isinstance(xml, ET.Element) else absolute_schema_location_pairs(tree, base_dir)
    if xsd is None:
        _namespace, hint = resolve_schema_hint(tree, base_dir)
        if hint is None:
            raise PyXSDError(
                "no schema file was given and the xml file has no "
                "schemaLocation or noNamespaceSchemaLocation tag"
            )
        xsd = hint
    schema = Schema.compile(
        xsd,
        mode=mode,
        namespace_schemas=namespace_schemas,
        overlay=overlay,
        namespace_context=None if isinstance(xml, ET.Element) else context,
        schema_location_pairs=pairs,
    )
    return schema.parse(tree)


def load_overlay_classes(
    schema: Schema,
    class_file: str | Path | os.PathLike[str],
    fallback_dir: Path | None = None,
) -> None:
    """Loads a file with overlay classes into the schema's class table.

    Overlay classes add to and override the schema type classes to
    allow for a user to create their own types without changing the
    schema file itself. Every loaded class is stamped with the owning
    :class:`Schema`.

    **Consider this functionality experimental.**

    - ``class_file``: a string that specifies the location of a
      user-created overlay class file
    - ``fallback_dir``: directory searched for ``<class_file>.py`` when
      *class_file* does not name an existing file (the historical
      module-name fallback against the instance document's directory)
    """
    filePath = Path(class_file)
    if not filePath.is_file():
        # Fall back to the historical behavior of resolving a
        # module name against the instance file's directory.
        if fallback_dir is not None:
            candidate = fallback_dir / f"{class_file}.py"
            if candidate.is_file():
                filePath = candidate
        if not filePath.is_file():
            raise ImportError(f"the file '{class_file}' was not found. Please check your spelling.")
    module_name = filePath.stem
    module = _load_module_from_file(module_name, filePath)
    if module is None:
        raise ImportError(f"the file '{class_file}' could not be loaded.")
    newClasses: dict[str, Any] = {}
    for _varName, var in vars(module).items():
        if isinstance(var, type) and issubclass(var, SchemaBase) and var is not SchemaBase:
            className = getattr(var, "name", None)
            if className is None:
                warnings.warn(
                    f"the class {var} must have a 'name' attribute; using '__name__' instead",
                    PyXSDWarning,
                    stacklevel=2,
                )
                className = var.__name__
            newClasses[className] = var
            logger.debug("Loaded the %s class", className)
    schema.classes.update(newClasses)
    for cls in newClasses.values():
        cls.schema = schema


def _compile_into_context(
    ctx: CompositionContext,
    xsd_file: str | Path | os.PathLike[str] | IO[str] | None,
    host: Any,
) -> Schema:
    """Runs the schema-compilation pipeline into *ctx*.

    The single shared body behind :meth:`Schema.compile`. *host* is
    the parser-like object the
    ElementRepresentative APIs read (``report``, ``mode``,
    ``components``, ``classes``, ``namespaceContext``); the container
    fields of *ctx* alias the host's own containers, so the compiled
    products stay visible to both. Returns the built :class:`Schema`
    with every generated class stamped with it.
    """
    logger.debug("Sending the schema file to the ElementTree Parser...")

    # Everything reported until the instance document is parsed is a
    # schema-compilation problem: composition, ER building, and class
    # generation.
    ctx.report.phase = "schema"

    # The report rides the active context for the whole run, so
    # diagnostics raised anywhere in the pipeline (ER building, class
    # building, the declaration sweeps) resolve it without needing the
    # parser passed down; the previous value is restored afterwards.
    with active_context(ctx.schema_context), context_report(ctx.schema_context, ctx.report):
        if isinstance(xsd_file, (str, os.PathLike)):
            try:
                with open(xsd_file, "rb") as schemaFile:
                    root = parse_with_namespaces(schemaFile, ctx.namespace_context)
            except OSError as e:
                raise PyXSDError(f"the schema file could not be opened: {e}") from e
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        else:
            # The schema must be a file object here: a ``None`` schema
            # without a location hint is rejected by the caller.
            assert xsd_file is not None
            try:
                root = parse_with_namespaces(xsd_file, ctx.namespace_context)
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        logger.debug("Sending the schema ElementTree to the ElementRepresentative module...")

        # XSD 1.1 §4.2.2 conditional inclusion runs on every schema document
        # before anything else, so the element-representative walk never sees
        # a declaration a ``vc:*`` selector excludes. Included and imported
        # documents are filtered as they are parsed (``parse_included_schema``).
        apply_conditional_inclusion(root, ctx.namespace_context, ctx.report)
        check_namespace_attribute_values(ctx, root)

        baseDir, visited = schema_composition_context(ctx, xsd_file)
        # Documents already fully composed; their components must not be
        # spliced twice (diamond includes) and a repeat encounter is not
        # an error.
        ctx.composed_documents = set(visited)
        # A namespace is satisfied if a schema for it was supplied up
        # front, so such a namespace-only import is not unresolved.
        ctx.resolved_imports.update(ns for ns in ctx.namespace_schemas if ns)
        ctx.resolved_imports.update(ns for ns, _ in ctx.additional_schemas if ns)
        mainTargetNamespace = root.get("targetNamespace")
        if mainTargetNamespace:
            ctx.composed_target_namespaces.add(mainTargetNamespace)
        splice_composed_schemas(ctx, root, baseDir, visited, mainDocument=True)
        splice_additional_schemas(ctx, root, baseDir, visited)
        if getattr(ctx.mode, "namespaces", "legacy") == "strict":
            inject_xml_namespace_attributes(ctx, root)
            inject_xsi_namespace_attributes(ctx, root)
            inject_xlink_namespace_attributes(ctx, root)

        # XSD 1.1 allows a local element or attribute declaration to state
        # its own target namespace, but only inside an xs:restriction
        # (§3.3.2.1, §3.2.2). Register the value as a namespace override
        # before the ER run snapshots ``namespaceOverrides`` so the
        # declaration's expanded name reflects it.
        apply_local_target_namespaces(ctx, root)
        check_form_defaults(ctx, root)

        # Stage this compile's snapshot on its context before the ER
        # run so imported declarations report their own target namespace
        # and form defaults.
        ctx.schema_context.namespace_overrides = dict(ctx.namespace_overrides)
        ctx.schema_context.injected_builtin_ids = set(ctx.injected_builtin_ids)
        ctx.schema_context.form_defaults = dict(ctx.form_defaults)
        ctx.schema_context.xpath_default_namespaces = dict(ctx.xpath_default_namespaces)
        schemaER = ElementRepresentative.factory(root, None)
        if schemaER is None or schemaER.__class__.__name__ != "Schema":
            # A document that is not an XML Schema at all (for example
            # an instance document passed as the schema) must surface as
            # an input error, not as an AttributeError on ``None``.
            raise PyXSDError(f"schema document root element is not xs:schema: {root.tag!r}")
        # Attach the host and the compilation report to the schema ER:
        # the host so class building and schema-time QName resolution
        # can reach the parser-like containers, the report so
        # schema-reference problems recorded during the ER walk and the
        # declaration checks land on the validation report.
        schemaER.host = host
        schemaER.report = ctx.report
        # The captured prefix bindings let every declaration resolve the
        # QNames written in its own document. Install them before the
        # declaration checks run: the atomicity check resolves
        # ``itemType``/``memberTypes`` through the in-scope namespaces.
        schemaER.namespaceContext = ctx.namespace_context
        # Expand the 1.1 notQName names on every wildcard before the
        # declaration walk: the attribute-wildcard derivation check runs
        # when a complex type is visited, ahead of the AnyAttribute ERs
        # that declared the specs, and must see the expanded names.
        refine_wildcard_specs(ctx, schemaER)
        # This compile owns the component table the ER run registered
        # into; expose it on the host and on the context so registry
        # lookups (xsi:type dispatch, tests, and the declaration-issue
        # walk's type resolution) use this compile's declarations rather
        # than a previous one's.
        host.components = schemaER.components
        ctx.schema_context.components = host.components
        # Module-level lookups from here on (ElementRepresentative
        # .getFromName, the registry proxy) see this compile's table.
        remember_components(host.components)
        report_declaration_issues(ctx, schemaER, host)
        check_keyref_references(ctx, schemaER)
        # After the declaration walk (which parses every explicit
        # ``xs:openContent`` and the host document's
        # ``xs:defaultOpenContent``), attach the applicable schema default
        # to each complex type that declares none of its own.
        apply_default_open_content(ctx, schemaER)
        # XSD 1.1 §3.1.2: attach each schema document's default attribute
        # group to the complex types it declares, unless the type sets
        # ``defaultAttributesApply="false"``. Runs before class building so
        # ``resolveAttributeGroupRefs`` folds the group in with the
        # explicit references.
        apply_default_attributes(ctx, schemaER)
        # With every type's effective open content settled (explicit or
        # inherited default), check the open-content derivation rules
        # (mode rank and wildcard subset for restriction/extension).
        report_open_content_derivations(ctx, schemaER, host)

        # The schema root is itself the instance class used to dispatch
        # the document root's element declarations.
        ctx.classes["schema"] = schemaER.clsFor(host)
        # Build a generated class for every named type in the
        # compile-owned component table. The per-schema ``simpleTypes`` /
        # ``complexTypes`` dicts are keyed by local name, so composed
        # schemas that reuse a local name in different namespaces (very
        # common in OOXML) collide there and lose declarations. The
        # component table keeps one entry per expanded name.
        built: set[int] = set()
        for entries in host.components.values():
            for typeER in entries:
                if componentKind(typeER) != "type" or id(typeER) in built:
                    continue
                if getattr(typeER, "_alternativeInline", False):
                    # An ``xs:alternative``'s inline type is built on
                    # demand (when a test selects it, or when the
                    # derivation check needs it), never eagerly: building
                    # every one here would surface an unimplemented base
                    # as a schema error even for alternatives the instance
                    # phase never selects.
                    continue
                built.add(id(typeER))
                cls = typeER.clsFor(host)
                ctx.classes[typeER.name] = cls
                if typeER.expandedName:
                    ctx.classes[typeER.expandedName] = cls
                logger.debug("Class created for the %s type...", typeER.name)

        build_substitution_groups(ctx, schemaER, host)
        check_substitution_group_exclusions(ctx, schemaER, host)
        check_value_constraints(ctx, schemaER)
        check_type_references(ctx, schemaER)
        check_notation_uses(ctx, schemaER, host)
        check_simple_content_restriction_base(ctx, schemaER, host)
        check_content_kind_derivation(ctx, schemaER, host)
        check_alternatives(ctx, schemaER)
        check_alternative_table_edc(ctx, schemaER)
        check_conditional_type_substitutable(ctx, schemaER, host)
        check_group_redefine_restrictions(ctx, schemaER, host)

        schema = Schema(
            classes=ctx.classes,
            components=host.components,
            namespace_context=ctx.namespace_context,
            mode=ctx.mode,
            target_namespace=mainTargetNamespace,
            report=ctx.report,
            source=xsd_file,
            schema_context=ctx.schema_context,
        )
        # The Schema object only exists after the harvest, so the
        # back-reference is stamped in one pass here (overlay classes
        # are stamped by ``load_overlay_classes``).
        for cls in ctx.classes.values():
            cls.schema = schema
        schema._host = host
        # Classes built lazily after the compile (an ``xs:alternative``'s
        # inline type selected at binding time) stamp their owner from
        # the schema ER, which outlives the compile on the component
        # table.
        schemaER.compiledSchema = schema
        return schema
