"""The PyXSD parser: maps XML and XSD (XML Schema) files into Python.

PyXSD was developed in order to map XML and the related schema (XSD)
files into the Python language. The program builds a pythonic
representation of the XML tree according to the specifications in the
schema and records non-fatal validation issues in a
:class:`~pyxsd.validation.ValidationReport` in order to help the user
validate their XML document. The program allows the user to specify
*transform* classes, which manipulate and transform the XML tree in
various ways. The program then writes the tree back out to XML. PyXSD
allows users to create their own transform classes with the help of a
transform library. These classes are fairly simple to write, making
the system highly adaptable to very specific uses, as one might find
in many scientific applications; however, the program has potential
uses in other fields, since XML is widely used. The program allows the
user to specify the desired transform classes, along with their
arguments and sequence of application, so the user can create
customized tools. The program can be used either as a standalone
command line program or as a library in other programs.

Overview:

- Creates Python classes for all types defined in an XSD schema file
  (xml)

- Reads in an xml file and builds a new pythonic tree according to
  classes. This tree of instances maintains the same overall structure
  of the original xml document.

- Provides some xml/schema validation with non-fatal issues recorded
  in a validation report, in order to help the user write a valid xml
  document, without requiring it

- Transforms the pythonic representation according to built-in and
  add-on 'transform' classes that the user specifies

- Sends data to a writer to write the pythonic tree back out to an
  xml file
"""

import ast
import importlib
import importlib.util
import inspect
import io
import logging
import os.path
import pkgutil
import re
import sys
import tokenize
import warnings
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import IO, Any, ClassVar
from xml.etree import ElementTree as ET

from pyxsd import __version__, xsi
from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.content_model import (
    Particle,
    _content_children,
    all_extension_occurrence,
    all_members,
    all_term,
    compile_content_model,
    compile_own_content,
)
from pyxsd.derivation import (
    blockTokens,
    combinedBlock,
    derivationMessage,
    is_valid_xsi_type,
    is_validly_derived,
)
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
    componentKind,
)
from pyxsd.exceptions import PyXSDError, PyXSDWarning
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
from pyxsd.particle_derivation import (
    derived_wildcard_edc_violations,
    element_namespace,
    is_valid_particle_restriction,
    wildcard_subset,
)
from pyxsd.schema_base import SchemaBase, nil_content_kind
from pyxsd.schema_context import SchemaContext, remember_components, with_schema_context
from pyxsd.upa import upa_violations
from pyxsd.validation import ValidationReport
from pyxsd.versioning import VC_NS, apply_conditional_inclusion
from pyxsd.wildcards import (
    NAMESPACE_ANY,
    PROCESS_SEVERITY,
    WildcardSpec,
    effective_attribute_wildcard,
    invalid_namespace_constraint,
    wildcard_specs_overlap,
)
from pyxsd.writers.xml_tree_writer import XmlTreeWriter
from pyxsd.xpath_subset import XPathError
from pyxsd.xsd_data_types import (
    AnySimpleType,
    AnyType,
    Boolean,
    Integer,
    NCName,
    XsdDataType,
    qname_context,
    whitespace_mode,
    xsd_value_key,
)

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

#: Namespace of the XSD 1.1 conditional-inclusion attributes
#: (``vc:minVersion`` etc.). Version selectors may legitimately leave
#: several same-named declarations for different versions; the selector
#: namespace and its pre-processing live in ``pyxsd.versioning``.


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


class _GroupRedefineDeclaration:
    """A declaration whose built-in integer type is normalized to ``Integer``.

    Used only by the group-redefine restriction check so that narrowing
    one built-in integer type to another (``xs:int`` to ``xs:byte``) is
    not mistaken for an unrelated type change. Every other attribute and
    method delegates to the wrapped declaration.
    """

    __slots__ = ("_declaration",)

    def __init__(self, declaration: Any) -> None:
        self._declaration = declaration

    def __getattr__(self, name: str) -> Any:
        return getattr(self._declaration, name)

    def getType(self) -> Any:
        cls = self._declaration.getType()
        if cls is None:
            return None
        try:
            isInteger = issubclass(cls, Integer)
            isBoolean = issubclass(cls, Boolean)
        except TypeError:
            return cls
        return Integer if isInteger and not isBoolean else cls


def _mixedIsTrue(value: str) -> bool:
    """The xs:boolean reading of a lexical ``mixed`` value."""
    return value.strip().lower() in ("true", "1")


class PyXSD:
    """Main class of the program that is in charge of data flow.

    Has command line support when it is called as a script.
    """

    # Depends on the input form: a path stem for files, ``None`` for a
    # file-like object.
    xmlFileInputName: str | None

    # Resolved to a Path for file inputs, held as-is for file objects.
    xmlFileInput: Path | IO[str]
    xmlPath: Path
    xsdFile: str | Path | os.PathLike[str] | IO[str] | None
    xmlFileOutput: str | Path | bool
    schemaRootInstance: Any

    #: ER class names for the identity-constraint definitions. Their
    #: names share one symbol space scoped to the containing element
    #: declaration (XSD 1.0 §3.11), unlike the per-target-namespace
    #: symbol spaces of the global components.
    _IDENTITY_KINDS = ("Key", "Keyref", "Unique")

    def __init__(
        self,
        xmlFileInput: str | Path | os.PathLike[str] | IO[str],
        xsdFile: str | Path | os.PathLike[str] | IO[str] | None = None,
        xmlFileOutput: str | bool = False,
        transformOutputName: str | None = None,
        transforms: list[str] | None = None,
        classFile: str | Path | os.PathLike[str] | None = None,
        verbose: bool = False,
        quiet: bool = False,
        mode: BindingPolicy = ParseModes.STRICT,
        namespace_schemas: dict[str, str | Path] | None = None,
    ):
        """Initialize the parser and run the whole pipeline.

        - ``xmlFileInput`` - the filename of the xml file to input (a
          string or path); a file object open for reading is also
          accepted. Will raise an error if not specified.

        - ``xsdFile`` - the filename/path information for the schema
          file; a file object open for reading is also accepted. Will
          attempt to use the schemaLocation tag in the xml if not
          specified.

        - ``xmlFileOutput`` - location for xml output to be sent after
          it is parsed. Will use a default name if not specified. Will
          not output if the value is falsy or ``'_No_Output_'``.

        - ``transformOutputName`` - location of the xml output after
          the transform, or ``'stdout'``. Will make a default filename
          if not specified.

        - ``transforms`` - a list containing the transform calls in the
          order they will be performed.

        - ``classFile`` - the location of the overlay class file.
          Experimental.

        - ``verbose`` - a boolean value. If set to true, logs more
          information (the CLI maps this to the DEBUG log level).

        - ``quiet`` - a boolean value. If set to true, logs less
          information (the CLI maps this to the CRITICAL log level).

        - ``mode`` - a :class:`~pyxsd.binding.BindingPolicy` (usually a
          :class:`~pyxsd.binding.ParseModes` preset) controlling how
          invalid values are bound into the tree. Reporting is always
          strict; the mode never suppresses a validation issue. Defaults
          to :attr:`~pyxsd.binding.ParseModes.STRICT`.

        - ``namespace_schemas`` - an optional ``{namespace_uri: path}``
          mapping supplying schemas for namespaces referenced by
          ``xs:import`` without a ``schemaLocation``. Only consulted in
          strict namespace mode.

        After construction, ``self.report`` holds the
        :class:`~pyxsd.validation.ValidationReport` collected while the
        instance document was bound.
        """
        self.verbose = verbose
        self.quiet = quiet
        self.mode = mode
        self.namespaceSchemas: dict[str, str | Path] = dict(namespace_schemas or {})
        # Namespace overrides keyed by ``id(xsdElement)`` for components
        # spliced in from imported schemas; installed before the ER run so
        # an imported declaration reports its own target namespace.
        self._namespaceOverrides: dict[int, str | None] = {}
        # Element ids of the built-in components this parser injects
        # (the implicit ``xml``/``xsi`` attribute declarations). Kept
        # apart from the override map so declaration-legality checks can
        # exempt exactly the injected components without exempting a
        # user schema that targets a well-known namespace.
        self._injectedBuiltinIds: set[int] = set()
        # Source-document form defaults per spliced component:
        # id(xsdElement) -> (elementFormDefault, attributeFormDefault).
        self._formDefaults: dict[int, tuple[str | None, str | None]] = {}
        # Source-document ``xpathDefaultNamespace`` per spliced component
        # (only documents that declare one are recorded), so an included
        # document's XPath default survives the splice.
        self._xpathDefaultNamespaces: dict[int, str | None] = {}
        # Element identities that came from an included/imported schema
        # document. ``id`` uniqueness is an XML (per-document) rule, so
        # components spliced from another document must not be compared
        # against the main document's ids.
        self._composedElementIds: set[int] = set()
        # Source schema-document root per element spliced in from an
        # included/imported document. A ``xs:defaultOpenContent`` is
        # scoped to the schema document a complex type is declared in
        # (XSD 1.1 §3.4.2.4), so an inherited default must be looked up
        # on the type's own source document rather than the host schema.
        self._composedSchemaRoots: dict[int, Any] = {}
        # ``id`` attributes on the main document's composition directives
        # (include/import/redefine). The directives are removed before the
        # ER walk, but their ids still take part in the document's xs:ID
        # uniqueness, so declaration ids are compared against them.
        self._directiveIds: dict[str, Any] = {}
        # The include/redefine ancestry of the document currently being
        # composed (resolved paths, outermost first). Used to tell an
        # independent second redefine of a component (a conflict) from a
        # nested one reached through a shared include (the suite leaves
        # the latter implementation-defined).
        self._composeStack: list[str] = []
        # For each redefined ``(base path, kind, name)``, the
        # ``_composeStack`` snapshot at its first redefine.
        self._redefineOrigins: dict[tuple[str, str, str], tuple[str, ...]] = {}
        # For each overridden ``(base path, symbol space, name)``, the
        # ``_composeStack`` snapshot at its first override. A later
        # override of the same base component from an unrelated ancestry
        # is a conflict (the override duplicate rule).
        self._overrideOrigins: dict[tuple[str, str, str], tuple[str, ...]] = {}
        # Namespaces for which a schema was supplied or successfully
        # loaded. A namespace-only import of one of these is satisfied by
        # that supply rather than an unresolved hint.
        self._resolvedImports: set[str] = set()
        # Resolved paths of schemas collected from the instance's
        # ``xsi:schemaLocation``/``noNamespaceSchemaLocation`` hints.
        # Hints are advisory: one that cannot be loaded is a
        # ``schema-hint`` warning, while an explicitly supplied
        # ``namespace_schemas`` entry stays an ``import-unresolved``
        # error (the distinction Task 9's strictness pass builds on).
        self._hintSchemaPaths: set[str] = set()
        # Target namespaces of the schema documents in the composition.
        # A namespace-only import of a namespace another document in the
        # collection declares is satisfied by it; only the importing
        # document's own target namespace does not count.
        self._composedTargetNamespaces: set[str] = set()
        # The parser's thread-local construction context: schema
        # construction and instance binding run inside it, so concurrent
        # parsers cannot observe each other's overrides or tables. The
        # staged table collects representatives built before the schema
        # root adopts one.
        self.schemaContext = SchemaContext(components=ComponentTable())
        self.classes: dict[str, type[SchemaBase]] = {}
        self.report = ValidationReport()
        # Transform-embedded reports already merged into self.report
        # (see _absorbTransformReport). The objects are retained so
        # identity stays meaningful for the parser's lifetime; storing
        # only id() values allowed a collected report's address to be
        # reused and a new report to be mistaken for one already merged.
        self._absorbedReports: list[ValidationReport] = []
        # Prefix-to-URI bindings captured while parsing the instance and
        # every schema document; one context accumulates them all so a
        # component resolves QNames against its own document's scope.
        self.namespaceContext = NamespaceContext()

        if isinstance(xmlFileInput, (str, os.PathLike)):
            self.xmlFileInput = Path(xmlFileInput).resolve()
            self.xmlPath = self.xmlFileInput.parent
            self.xmlFileInputName = self.xmlFileInput.name
        else:
            self.xmlFileInput = xmlFileInput
            self.xmlPath = Path.cwd()
            self.xmlFileInputName = None

        self.xsdFile = xsdFile
        self.xmlFileOutput = xmlFileOutput

        self.xmlRoot = self.getXmlTree()

        if self.xmlFileOutput is None and isinstance(xmlFileInput, (str, os.PathLike)):
            self.xmlFileOutput = self.getXmlOutputFileName()

        self.transforms = transforms if transforms is not None else []

        if xsdFile is None:
            self.xsdFile = self.getSchemaInfo("l")
            if self.xsdFile is None:
                raise PyXSDError(
                    "no schema file was given and the xml file has no "
                    "schemaLocation or noNamespaceSchemaLocation tag"
                )
            # Schema hints are documented relative to the instance
            # document, not the caller's working directory.
            hintPath = Path(self.xsdFile)
            if not hintPath.is_absolute():
                hintPath = self.xmlPath / hintPath
            self.xsdFile = hintPath
        self.nameSpace = self.getSchemaInfo("n")
        self._additionalSchemas = self._collectAdditionalSchemas()
        self.parseXSD()

        if classFile:
            logger.debug(
                "Attempting to load overlay classes from the file '%s'...",
                classFile,
            )
            self.loadClassFromFile(classFile)

        rootInstance = self.parseXML()

        if self.xmlFileOutput and self.xmlFileOutput != "_No_Output_":
            rootInstance = self.writeParsedXMLFile(rootInstance)

        self.transformOutputName = transformOutputName

        # Library consumers need the in-memory tree after construction;
        # the CLI only drives output through files. Kept before
        # executeAndWriteTransforms so it is set even on transform errors.
        self.schemaRootInstance = rootInstance
        self.executeAndWriteTransforms(rootInstance)

    def executeAndWriteTransforms(self, rootInstance: Any) -> None:
        """Runs each transform in order and writes the transformed tree to
        the transform output, if one was requested.
        """
        if not self.transforms:
            return
        logger.debug("Loading the transforms...")
        transformOutput: str | Path = self.transformOutputName  # type: ignore[assignment]
        if not transformOutput:
            transformOutput = self.getTransformsFileName()
            logger.debug(
                "Loading the file '%s' for the transformed XML output...",
                transformOutput,
            )
        transformedRoot = self.transform(self.transforms, rootInstance)
        if transformedRoot:
            logger.debug("Sending transformed tree to the writer...")
            if transformOutput == "stdout":
                self.writeXML(transformedRoot, sys.stdout)
            else:
                with open(transformOutput, "w") as output:
                    self.writeXML(transformedRoot, output)

    def _injectXmlNamespaceAttributes(self, schemaRoot: Any) -> None:
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
            self._namespaceOverrides[id(attributeElement)] = XML_NS
            self._injectedBuiltinIds.add(id(attributeElement))
            schemaRoot.append(attributeElement)

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

    def _injectXlinkNamespaceAttributes(self, schemaRoot: Any) -> None:
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
        for local in self._XLINK_BUILTIN_ATTRIBUTES:
            attributeElement = ET.Element(
                clark(XSD_NS, "attribute"),
                {"name": local, "type": clark(XSD_NS, "string")},
            )
            self._namespaceOverrides[id(attributeElement)] = XLINK_NS
            self._injectedBuiltinIds.add(id(attributeElement))
            schemaRoot.append(attributeElement)

    def _injectXsiNamespaceAttributes(self, schemaRoot: Any) -> None:
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
        for local, typeName in self._XSI_BUILTIN_ATTRIBUTES:
            attributeElement = ET.Element(
                clark(XSD_NS, "attribute"),
                {"name": local, "type": clark(XSD_NS, typeName)},
            )
            self._namespaceOverrides[id(attributeElement)] = XSI_NS
            self._injectedBuiltinIds.add(id(attributeElement))
            schemaRoot.append(attributeElement)

    @with_schema_context
    def parseXSD(self) -> None:
        """Reads the given xsd file and creates a set of classes that
        correspond to the complex and simple type definitions.
        """
        logger.debug("Sending the schema file to the ElementTree Parser...")

        # Everything reported until the instance document is parsed is a
        # schema-compilation problem: composition, ER building, and class
        # generation.
        self.report.phase = "schema"

        if isinstance(self.xsdFile, (str, os.PathLike)):
            try:
                with open(self.xsdFile, "rb") as schemaFile:
                    root = parse_with_namespaces(schemaFile, self.namespaceContext)
            except OSError as e:
                raise PyXSDError(f"the schema file could not be opened: {e}") from e
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        else:
            # The schema must be a file object here: a ``None`` schema
            # without a location hint is rejected in ``__init__``.
            assert self.xsdFile is not None
            try:
                root = parse_with_namespaces(self.xsdFile, self.namespaceContext)
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        logger.debug("Sending the schema ElementTree to the ElementRepresentative module...")

        # XSD 1.1 §4.2.2 conditional inclusion runs on every schema document
        # before anything else, so the element-representative walk never sees
        # a declaration a ``vc:*`` selector excludes. Included and imported
        # documents are filtered as they are parsed (``_parseIncludedSchema``).
        apply_conditional_inclusion(root, self.namespaceContext, self.report)
        self._checkNamespaceAttributeValues(root)

        baseDir, visited = self._schemaCompositionContext()
        # Documents already fully composed; their components must not be
        # spliced twice (diamond includes) and a repeat encounter is not
        # an error.
        self._composedDocuments: set[str] = set(visited)
        # A namespace is satisfied if a schema for it was supplied up
        # front, so such a namespace-only import is not unresolved.
        self._resolvedImports.update(ns for ns in self.namespaceSchemas if ns)
        self._resolvedImports.update(ns for ns, _ in getattr(self, "_additionalSchemas", []) if ns)
        mainTargetNamespace = root.get("targetNamespace")
        if mainTargetNamespace:
            self._composedTargetNamespaces.add(mainTargetNamespace)
        self._spliceComposedSchemas(root, baseDir, visited, mainDocument=True)
        self._spliceAdditionalSchemas(root, baseDir, visited)
        if getattr(self.mode, "namespaces", "legacy") == "strict":
            self._injectXmlNamespaceAttributes(root)
            self._injectXsiNamespaceAttributes(root)
            self._injectXlinkNamespaceAttributes(root)

        # XSD 1.1 allows a local element or attribute declaration to state
        # its own target namespace, but only inside an xs:restriction
        # (§3.3.2.1, §3.2.2). Register the value as a namespace override
        # before the ER run snapshots ``namespaceOverrides`` so the
        # declaration's expanded name reflects it.
        self._applyLocalTargetNamespaces(root)
        self._checkFormDefaults(root)

        # Stage this parser's snapshot on its own context before the ER
        # run so imported declarations report their own target namespace
        # and form defaults.
        self.schemaContext.namespace_overrides = dict(self._namespaceOverrides)
        self.schemaContext.injected_builtin_ids = set(self._injectedBuiltinIds)
        self.schemaContext.form_defaults = dict(self._formDefaults)
        self.schemaContext.xpath_default_namespaces = dict(self._xpathDefaultNamespaces)
        schemaER = ElementRepresentative.factory(root, None)
        if schemaER is None or schemaER.__class__.__name__ != "Schema":
            # A document that is not an XML Schema at all (for example
            # an instance document passed as the schema) must surface as
            # an input error, not as an AttributeError on ``None``.
            raise PyXSDError(f"schema document root element is not xs:schema: {root.tag!r}")
        # Attach the parser to the schema ER so class building can
        # record schema-reference problems (group/attributeGroup
        # references) on the validation report.
        schemaER.pyXSD = self
        # The captured prefix bindings let every declaration resolve the
        # QNames written in its own document. Install them before the
        # declaration checks run: the atomicity check resolves
        # ``itemType``/``memberTypes`` through the in-scope namespaces.
        schemaER.namespaceContext = self.namespaceContext
        # Expand the 1.1 notQName names on every wildcard before the
        # declaration walk: the attribute-wildcard derivation check runs
        # when a complex type is visited, ahead of the AnyAttribute ERs
        # that declared the specs, and must see the expanded names.
        self._refineWildcardSpecs(schemaER)
        # This parser owns the component table the ER run registered
        # into; expose it on the parser and on the context so registry
        # lookups (xsi:type dispatch, tests, and the declaration-issue
        # walk's type resolution) use this parser's declarations rather
        # than a previous parser's.
        self.components = schemaER.components
        self.schemaContext.components = self.components
        # Module-level lookups from here on (ElementRepresentative
        # .getFromName, the registry proxy) see this parser's table.
        remember_components(self.components)
        self._reportDeclarationIssues(schemaER)
        self._checkKeyrefReferences(schemaER)
        # After the declaration walk (which parses every explicit
        # ``xs:openContent`` and the host document's
        # ``xs:defaultOpenContent``), attach the applicable schema default
        # to each complex type that declares none of its own.
        self._applyDefaultOpenContent(schemaER)
        # XSD 1.1 §3.1.2: attach each schema document's default attribute
        # group to the complex types it declares, unless the type sets
        # ``defaultAttributesApply="false"``. Runs before class building so
        # ``resolveAttributeGroupRefs`` folds the group in with the
        # explicit references.
        self._applyDefaultAttributes(schemaER)
        # With every type's effective open content settled (explicit or
        # inherited default), check the open-content derivation rules
        # (mode rank and wildcard subset for restriction/extension).
        self._reportOpenContentDerivations(schemaER)

        # The schema root is itself the instance class used to dispatch
        # the document root's element declarations.
        self.classes["schema"] = schemaER.clsFor(self)
        # Build a generated class for every named type in the
        # parser-owned component table. The per-schema ``simpleTypes`` /
        # ``complexTypes`` dicts are keyed by local name, so composed
        # schemas that reuse a local name in different namespaces (very
        # common in OOXML) collide there and lose declarations. The
        # component table keeps one entry per expanded name.
        built: set[int] = set()
        for entries in self.components.values():
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
                cls = typeER.clsFor(self)
                self.classes[typeER.name] = cls
                if typeER.expandedName:
                    self.classes[typeER.expandedName] = cls
                logger.debug("Class created for the %s type...", typeER.name)

        self._buildSubstitutionGroups(schemaER)
        self._checkSubstitutionGroupExclusions(schemaER)
        self._checkValueConstraints(schemaER)
        self._checkTypeReferences(schemaER)
        self._checkNotationUses(schemaER)
        self._checkSimpleContentRestrictionBase(schemaER)
        self._checkContentKindDerivation(schemaER)
        self._checkAlternatives(schemaER)
        self._checkGroupRedefineRestrictions(schemaER)

        return None

    def _checkGroupRedefineRestrictions(self, schemaER: Any) -> None:
        """Reports a group redefine that is not a valid restriction.

        XSD 1.0 §4.2.4: the model group of a redefined group must be a
        valid restriction of the original. A redefine written without a
        self reference *replaces* the original outright, so the compiled
        particle trees are compared with the same predicate used for a
        complex-type particle restriction (schL1/schL6/schL8/schO2). A
        redefine that does carry a self reference keeps pyxsd's
        extension-style semantics (the reference pulls the original in),
        which the corpus accepts for the standard pattern.
        """
        groups = getattr(schemaER, "groups", None) or {}
        suffix = "|base"
        for name, base_er in list(groups.items()):
            if not isinstance(name, str) or not name.endswith(suffix):
                continue
            derived_name = name[: -len(suffix)]
            derived = groups.get(derived_name)
            if derived is None:
                continue
            if self._groupRedefineHasSelfReference(derived.xsdElement, derived_name):
                continue
            base_model = compile_own_content(base_er, self)
            derived_model = compile_own_content(derived, self)
            if base_model is None or derived_model is None:
                continue
            resolver = self._groupRedefineResolver
            try:
                reasons = list(
                    is_valid_particle_restriction(
                        base_model,
                        derived_model,
                        resolver,
                        head_lookup=self._substitution_head_lookup(derived),
                        member_lookup=self._substitution_member_lookup(derived),
                    )
                )
            except RecursionError:  # pragma: no cover - defensive
                continue
            for reason in reasons:
                self.report.add_error(
                    f"the redefined group '{derived_name}' does not validly "
                    f"restrict its original: {reason}",
                    code="compose-invalid",
                    phase="schema",
                )

    @staticmethod
    def _groupRedefineResolver(particle: Any) -> Any:
        """Resolves a particle to its declaration for the restriction check.

        Element declarations are wrapped so the type-subsumption clause
        treats the whole built-in integer family as one type: the Python
        storage lattice makes ``xs:byte``/``xs:short``/``xs:int`` siblings
        rather than a derivation chain, so a redefined group that narrows
        ``xs:int`` to ``xs:byte`` (schH1/schH2, valid) would otherwise be
        reported. Unrelated built-ins (``xs:string`` over ``xs:int``) and
        user types still compare normally.
        """
        declaration = getattr(particle, "descriptor", None)
        if declaration is None:
            return None
        return _GroupRedefineDeclaration(declaration)

    @staticmethod
    def _groupRedefineHasSelfReference(declaration: Any, name: str) -> bool:
        """Whether a redefined group refers to itself (or its base copy)."""
        candidates = {name, f"{name}|base"}
        for element in declaration.iter():
            if not isinstance(element.tag, str):
                continue
            if element.tag.split("}")[-1] != "group":
                continue
            ref = element.get("ref")
            if ref and _qnameLocal(ref) in candidates:
                return True
        return False

    def _refineWildcardSpecs(self, schema_root: Any) -> None:
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

    def _reportDeclarationIssues(self, schemaER: Any) -> None:
        """Reports declarations that cannot carry a usable name and
        identity constraints with illegal children.

        A missing or empty ``name`` on these components is a schema
        error; the ER run deliberately tolerates it so the rest of a
        large schema can still load. ``ref`` sites (elements, attributes,
        groups, attribute groups) borrow the referred declaration's name
        and are skipped.
        """
        namedKinds = (
            "Element",
            "Attribute",
            "ComplexType",
            "SimpleType",
            "Group",
            "AttributeGroup",
            "Key",
            "Keyref",
            "Unique",
            "Notation",
        )
        seen: set[int] = set()
        seenIds: dict[str, Any] = dict(self._directiveIds)
        declared: dict[tuple[str, Any, str], Any] = {}
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if (
                type(er).__name__ in namedKinds
                and not er.name
                and getattr(er, "ref", None) is None
                and not getattr(er, "isElementRef", False)
            ):
                self.report.add_error(
                    f"{type(er).__name__} declaration is missing a name",
                    code="declaration-name",
                )
            self._checkChildGrammar(er)
            self._checkSchemaNamespacedAttributes(er)
            self._checkDeclarationId(er, seenIds)
            self._checkDuplicateName(er, declared)
            er.checkDeclarationLegality()
            self._reportContentModelIssues(er)
            misplacement = getattr(er, "misplacement", None)
            if misplacement is not None:
                code, message = misplacement
                self.report.add_error(message, code=code)
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _applyDefaultOpenContent(self, schemaER: Any) -> None:
        """Attaches each schema document's default open content to its types.

        XSD 1.1 §3.4.2.4: the ``xs:defaultOpenContent`` child of the
        ``xs:schema`` ancestor element supplies the {open content} of a
        complex type with no explicit ``xs:openContent``, when the type's
        explicit content type is non-empty or ``appliesToEmpty`` is true.
        The default is scoped to the schema *document* a type is declared
        in, so a type spliced in from an include/import looks up its own
        document's default (which may be absent), never the host's.
        """
        mainDefault = getattr(schemaER, "defaultOpenContent", None)
        composedDefaults = self._composedDefaultOpenContent()
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "ComplexType":
                sourceRoot = self._composedSchemaRoots.get(id(er.xsdElement))
                if sourceRoot is None:
                    default = mainDefault
                else:
                    default = composedDefaults.get(id(sourceRoot))
                if default is not None and er.acceptsDefaultOpenContent(
                    appliesToEmpty=default.applies_to_empty
                ):
                    er.openContent = default.component()
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _applyDefaultAttributes(self, schemaER: Any) -> None:
        """Attaches each schema document's default attribute group to its types.

        XSD 1.1 §3.1.2: the ``defaultAttributes`` attribute of an
        ``xs:schema`` names a global attribute group whose {attribute uses}
        and {attribute wildcard} are added to every complex type definition
        declared in that same schema document, unless the type sets
        ``defaultAttributesApply="false"``. The default is scoped to the
        schema *document* a type is declared in (open044/open205), so a type
        spliced in from an include/import/redefine looks up its own
        document's group (which may be absent), never the host's. Types
        written inside an ``xs:override`` are scoped to the overridden
        document by ``_spliceOverride`` (ii08/ii10).
        """
        mainGroup = self._resolveDefaultAttributeGroup(schemaER.xsdElement)
        composedGroups = self._composedDefaultAttributeGroups()
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "ComplexType":
                sourceRoot = self._composedSchemaRoots.get(id(er.xsdElement))
                group = mainGroup if sourceRoot is None else composedGroups.get(id(sourceRoot))
                if group is not None and er.defaultAttributesApplies():
                    er.defaultAttributeGroup = group
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _composedDefaultAttributeGroups(self) -> dict[int, Any]:
        """Resolves every composed schema document's ``defaultAttributes``.

        Returns ``id(source schema root) -> AttributeGroup | None``.
        """
        result: dict[int, Any] = {}
        roots = list({id(root): root for root in self._composedSchemaRoots.values()}.values())
        for root in roots:
            key = id(root)
            if key in result:
                continue
            result[key] = self._resolveDefaultAttributeGroup(root)
        return result

    def _resolveDefaultAttributeGroup(self, root: Any) -> Any:
        """Resolves a schema document's ``defaultAttributes`` QName.

        Returns the global ``AttributeGroup`` the value names, or ``None``
        when the schema declares no default. A value that does not resolve
        to a global attribute group in the named namespace is a schema error
        (si01/open203/open204).
        """
        raw = root.get("defaultAttributes")
        if raw is None:
            return None
        value = raw.strip()
        try:
            resolved = self.namespaceContext.resolve(root, value)
        except NamespaceError:
            self.report.add_error(
                f"the defaultAttributes value '{value}' uses a prefix that is not bound in scope",
                code="unknown-namespace-prefix",
                phase="schema",
            )
            return None
        local = local_name(resolved)
        uri = namespace_of(resolved)
        for entries in self.components.values():
            for entry in entries:
                if type(entry).__name__ != "AttributeGroup":
                    continue
                if not entry.checkTopLevelType():
                    continue
                if entry.name != local:
                    continue
                if uri is None or entry.getNamespace() == uri:
                    return entry
        self.report.add_error(
            f"the defaultAttributes attribute group '{value}' could not be "
            "resolved to a global xs:attributeGroup",
            code="unknown-attributeGroup",
            phase="schema",
        )
        return None

    def _reportOpenContentDerivations(self, schemaER: Any) -> None:
        """Reports open-content derivation violations on complex types.

        XSD 1.1 §3.4.6.2 clause 1.4.3.2.2 and §3.4.6.4 pin the relation
        between a derived type's effective open content and its base's:
        a restriction may not widen the mode/wildcard (with the
        unobservable empty-particle exception) and an extension may not
        narrow them. Runs after the default-open-content pass so a type
        that inherits its open content is compared with its final
        component. Violations are reported as ``particle-restriction``
        (the existing derivation code); unresolved bases and non-complex
        derivations are skipped.
        """
        from pyxsd.open_content import open_content_derivation_problem

        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "ComplexType":
                self._checkOneOpenContentDerivation(er, open_content_derivation_problem)
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _checkOneOpenContentDerivation(self, er: Any, checker: Any) -> None:
        """Checks one complex type's open-content derivation, if applicable."""
        derivation = er.getDerivation()
        if derivation not in ("restriction", "extension"):
            return
        if er._firstProcessedChild(er, "SimpleContent") is not None:
            return
        base = er._baseComplexType()
        if base is None or base._firstProcessedChild(base, "SimpleContent") is not None:
            return
        derived = er.effectiveOpenContent()
        base_open = base.effectiveOpenContent()
        if derived is None and base_open is None:
            return
        reason = checker(
            derived,
            base_open,
            derivation,
            derived_model=compile_content_model(er, self),
            base_model=compile_content_model(base, self),
            target_namespace=er.getNamespace(),
            derived_variety=er._effectiveContentVariety(),
            base_variety=base._effectiveContentVariety(),
        )
        if reason is not None:
            self.report.add_error(
                f"type '{getattr(er, 'name', '?')}' has invalid open content: {reason}",
                code="particle-restriction",
            )

    def _composedDefaultOpenContent(self) -> dict[int, Any]:
        """Parses the ``defaultOpenContent`` of every composed schema document.

        The element is not part of the spliced main tree, so its legality
        is checked here through the same helper the declaration walk uses
        for the host document. Returns ``id(source schema root) ->
        DefaultOpenContent | None``.
        """
        from pyxsd.open_content import default_open_content_element, parse_default_open_content

        result: dict[int, Any] = {}
        roots = list({id(root): root for root in self._composedSchemaRoots.values()}.values())
        for root in roots:
            key = id(root)
            if key in result:
                continue
            element = default_open_content_element(root)
            if element is None:
                result[key] = None
                continue
            result[key] = parse_default_open_content(
                element,
                report_error=lambda message, code: self.report.add_error(
                    message, code=code, phase="schema"
                ),
                target_namespace=root.get("targetNamespace"),
                resolve_qname=self._openContentResolver(element),
            )
        return result

    def _openContentResolver(self, element: Any) -> Any:
        """A ``notQName`` QName expander for a composed document's element."""
        context = self.namespaceContext

        def resolve(token: str) -> str:
            return context.resolve(element, token)

        return resolve

    def _checkKeyrefReferences(self, schemaER: Any) -> None:
        """Checks every keyref's ``refer`` and every constraint
        reference site's ``ref`` against the schema's constraints.

        XSD 1.0 §3.11.5 / 1.1 §3.13.5: the ``refer`` QName must
        resolve to a *key* or *unique* identity-constraint definition
        (a keyref is not a valid target, idH035) and the keyref must
        carry the same number of fields as the constraint it
        references (idH013/idH014). ``refer`` resolves as a component
        QName: a prefix through the declaration site's in-scope
        bindings, an unprefixed name through the declaration site's
        default namespace — overridden by a declared
        ``xpathDefaultNamespace`` (self → constraint → schema); the
        match against the Key and Unique definitions is by Clark
        name. The XSD 1.1 ``ref`` form must resolve to a constraint
        of the same category (§3.11.3.5); the site then acts as its
        target at instance phase. Failures are schema-phase
        ``identity-refer`` errors. The resolved names are recorded on
        the constraints (``constraintClark``/``referClark``/
        ``borrowedFrom``) for the instance phase's namespace-accurate
        scope matching.
        """
        keys: dict[str, Any] = {}
        named: dict[str, dict[str, Any]] = {"Key": {}, "Unique": {}, "Keyref": {}}
        keyrefs: list[Any] = []
        refSites: list[Any] = []
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            kind = type(er).__name__
            if kind in ("Key", "Unique", "Keyref"):
                if getattr(er, "isConstraintRef", False):
                    refSites.append(er)
                elif kind in ("Key", "Unique"):
                    name = getattr(er, "constraintName", None)
                    if name:
                        clarkName = er._constraintClarkName()
                        er.constraintClark = clarkName
                        if clarkName:
                            keys.setdefault(clarkName, er)
                            named[kind].setdefault(clarkName, er)
                            # Redefine machinery renames base copies with
                            # "|" ("base|Key|n"); index those under their
                            # final segment too so a refer to the original
                            # name still resolves.
                            if "|" in name:
                                alias = clark(namespace_of(clarkName), name.split("|")[-1])
                                keys.setdefault(alias, er)
                                named[kind].setdefault(alias, er)
                else:
                    try:
                        er.referClark = er._referClarkName()
                    except XPathError as exc:
                        self._reportInvalidConstraintDefaultNamespace(er, exc)
                        continue
                    keyrefs.append(er)
                    clarkName = er._constraintClarkName()
                    if clarkName:
                        named[kind].setdefault(clarkName, er)
            stack.extend(getattr(er, "processedChildren", None) or ())
        self._resolveConstraintRefSites(refSites, named)
        for keyref in keyrefs:
            refer = getattr(keyref, "refer", "") or ""
            local = refer.split(":")[-1].strip()
            if not local:
                # An empty refer is reported by the declaration
                # legality walk.
                continue
            referClark = getattr(keyref, "referClark", None)
            resolved = keys.get(referClark) if referClark else None
            if resolved is None:
                prefix, separator, _ = refer.partition(":")
                if separator and referClark is None:
                    message = (
                        f"keyref '{keyref.constraintName}': the prefix '{prefix}' of "
                        f"refer '{refer}' is not declared in the keyref's scope"
                    )
                else:
                    message = (
                        f"keyref '{keyref.constraintName}' refers to '{refer}', but no "
                        "key or unique with that name is declared in the schema"
                    )
                self.report.add_error(
                    message,
                    code="identity-refer",
                    element=getattr(keyref, "rawTag", None) or "keyref",
                    phase="schema",
                )
                continue
            if len(keyref.fieldPaths) != len(resolved.fieldPaths):
                self.report.add_error(
                    f"keyref '{keyref.constraintName}' carries {len(keyref.fieldPaths)} "
                    f"field(s), but the referenced {type(resolved).__name__.lower()} "
                    f"'{resolved.constraintName}' carries {len(resolved.fieldPaths)}",
                    code="identity-refer",
                    element=getattr(keyref, "rawTag", None) or "keyref",
                    phase="schema",
                )

    def _resolveConstraintRefSites(
        self, refSites: list[Any], named: dict[str, dict[str, Any]]
    ) -> None:
        """Resolves XSD 1.1 constraint reference sites to their targets.

        A ``<key>``, ``<unique>`` or ``<keyref>`` carrying ``ref``
        names a constraint of its own category (§3.11.3.5) and acts as
        that constraint: ``borrowedFrom`` records the final target so
        the instance phase borrows its name, selector, fields and
        ``refer``. A reference that does not resolve — no such name,
        or the name only exists for a different category — is a
        schema-phase ``identity-refer`` error and the site is left
        unborrowed (the instance phase skips it).
        """
        for site in refSites:
            kind = type(site).__name__
            where = getattr(site, "rawTag", None) or kind.lower()
            ref = (site.tagAttributes.get("ref") or "").strip()
            try:
                clarkName = site._refClarkName()
            except XPathError as exc:
                self._reportInvalidConstraintDefaultNamespace(site, exc)
                continue
            target = named[kind].get(clarkName) if clarkName else None
            if target is None:
                if clarkName is None:
                    prefix, separator, _ = ref.partition(":")
                    detail = (
                        f"the prefix '{prefix}' is not declared in the constraint's scope"
                        if separator
                        else "the reference is empty"
                    )
                else:
                    detail = (
                        "no identity constraint of this category is declared "
                        "under that name in the schema"
                    )
                self.report.add_error(
                    f"<{where}> reference '{ref}' does not resolve: {detail}",
                    code="identity-refer",
                    element=where,
                    phase="schema",
                )
                continue
            site.borrowedFrom = target

    def _reportInvalidConstraintDefaultNamespace(self, er: Any, exc: XPathError) -> None:
        """Reports an unusable ``xpathDefaultNamespace`` on a constraint.

        The selector/field path reports the same condition as
        ``xpath-invalid``; a ``refer``/``ref`` QName under the same
        value must fail identically rather than surfacing as a
        resolution failure.
        """
        kind = type(er).__name__
        where = getattr(er, "rawTag", None) or kind.lower()
        self.report.add_error(
            f"<{where}> carries an invalid xpathDefaultNamespace: {exc}",
            code="xpath-invalid",
            element=where,
            phase="schema",
        )

    #: ``xs:anyType``'s effective content: a mixed sequence holding an
    #: unrestricted wildcard. It stands in for the built-in type's model
    #: during extension-structure checks (the built-in has no compiled
    #: class model); it is neither empty nor an ``all``, so an ``all``
    #: suffix over it is reported while sequence/choice suffixes are not.
    _ANY_TYPE_CONTENT = Particle("sequence", 1, 1, [Particle("any")])

    #: Compositor ERs whose particle sets the content-model sweep checks.
    _COMPOSITOR_KINDS = ("All", "Sequence", "Choice")

    #: Compositors the pointless-particle rule (particlesHa) applies to.
    _POINTLESS_KINDS = ("Sequence", "Choice")

    def _reportContentModelIssues(self, er: Any) -> None:
        """Reports duplicate or conflicting particles inside one compositor.

        Element declaration particles are collected transitively through
        nested compositor children — never through an element
        declaration's *type* (a different content model) and never
        through a group reference (spliced in later). Two particles with
        the same expanded name but different type declarations violate
        Element Declarations Consistent in any compositor; under an
        ``all`` any two particles with the same expanded name violate
        UPA (identical or not — identical duplicates elsewhere are
        deterministic and legal), as do an element in the substitution
        group of another and two overlapping wildcards. Each conflicting
        name is reported once per compositor.

        Complex types derived by restriction with a particle
        additionally run the particle-valid restriction check
        (``_reportParticleRestriction``, cos-particle-restrict).

        ``sequence``/``choice`` compositors with an empty particle set
        inside a group referenced with ``minOccurs="0"`` additionally
        violate the pointless-particle rule
        (``_reportPointlessParticle``).

        Reference sites are resolved against the document's global
        element declarations: references to the same declaration share
        its type, and a reference that cannot be resolved here (it
        names an import, say) is left out of the comparison — its type
        is unknown, and guessing from the raw QName would compare
        prefixes instead of declarations.
        """
        if type(er).__name__ in self._POINTLESS_KINDS:
            self._reportPointlessParticle(er)
        if type(er).__name__ == "ComplexType":
            self._reportParticleRestriction(er)
            self._reportMixedRestriction(er)
            self._reportMixedConflict(er)
            self._reportMixedExtension(er)
            self._reportComplexContentFromSimpleBase(er)
            self._reportAttributeUseDerivation(er)
            self._reportAttributeWildcardRestriction(er)
            self._reportExtensionStructure(er)
            self._reportUniqueParticleAttribution(er)
        if type(er).__name__ not in self._COMPOSITOR_KINDS:
            return
        if type(er).__name__ in ("Sequence", "Choice"):
            self._checkWildcardParticleOverlap(er)
        rawParticles: list[Any] = []
        self._collectParticles(er, rawParticles, set())
        resolved = self._resolveParticles(rawParticles)
        byName: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        for name, typeKey, particle in resolved:
            byName.setdefault(name, []).append((typeKey, particle))
        isAll = type(er).__name__ == "All"
        for (_, local), sameName in byName.items():
            if len(sameName) < 2:
                continue
            typeKeys = {typeKey for typeKey, _ in sameName}
            if len(typeKeys) > 1:
                self.report.add_error(
                    f"element declarations consistent: element '{local}' is "
                    "declared with conflicting types "
                    f"{sorted(key for key in typeKeys if key) or ['(anonymous)']} "
                    "in the same content model",
                    code="all-rule",
                )
                continue
            if isAll:
                self.report.add_error(
                    f"content model is ambiguous: element '{local}' appears more "
                    "than once in an all",
                    code="all-rule",
                )
        if isAll:
            self._checkSubstitutionOverlap(resolved)
            self._checkAllWildcardOverlap(er)
        self._checkSubstitutionEDC(resolved)

    def _checkSubstitutionEDC(self, resolved: list[Any]) -> None:
        """Reports a substitution member redeclared with a conflicting type.

        XSD 1.1 §3.8.6.4 (Element Declarations Consistent) compares more
        than same-named declarations: wherever a head element appears in
        a content model its substitution members stand in the same place,
        so a member that also appears explicitly must carry the type of
        the global member it duplicates. A mismatched local declaration
        (``edc.xsd``'s local ``e1: integer`` against the abstract global
        member ``e1`` of ``e: string``) therefore rejects the schema.
        Names are compared as expanded ``(namespace, local)`` pairs, so
        an unqualified local never collides with a same-spelled global
        (elemZ020).
        """
        if not resolved:
            return
        try:
            schema = resolved[0][2].getSchema()
        except AttributeError:
            return

        def nameOf(holder: Any) -> tuple[str, str]:
            return (
                element_namespace(holder) or "",
                getattr(holder, "name", None) or "",
            )

        # Effective (form-aware) names for the particles in the model: a
        # reference adopts the referred global's name, a local
        # declaration is qualified-or-not per its ``form``/the document
        # default, so an unqualified local never collides with a
        # same-spelled global (elemZ020).
        entries: list[tuple[tuple[str, str], str, Any]] = []
        for name, typeKey, particle in resolved:
            entryName = name if getattr(particle, "isElementRef", False) else nameOf(particle)
            entries.append((entryName, typeKey, particle))
        modelKeys = {name for name, _typeKey, _particle in entries}

        def headsOf(holder: Any) -> set[tuple[str, str]]:
            names: set[tuple[str, str]] = set()
            for head in holder.getSubstitutionGroupHeads(self):
                if head.startswith("{"):
                    namespace, _, local = head[1:].partition("}")
                    names.add((namespace, local))
                else:
                    names.add((holder.getNamespace() or "", head.split(":")[-1]))
            return names

        memberDecls: dict[tuple[str, str], Any] = {}
        memberHeads: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for element in getattr(schema, "elements", None) or ():
            if type(element).__name__ != "Element" or not element.name:
                continue
            if not element.tagAttributes.get("substitutionGroup"):
                continue
            key = nameOf(element)
            memberDecls[key] = element
            memberHeads[key] = headsOf(element)
        if not memberHeads:
            return

        memo: dict[tuple[str, str], set[tuple[str, str]]] = {}

        def transitiveHeads(
            key: tuple[str, str], seen: frozenset[tuple[str, str]]
        ) -> set[tuple[str, str]]:
            if key in memo:
                return memo[key]
            if key in seen:
                return set()
            heads: set[tuple[str, str]] = set()
            for head in memberHeads.get(key, ()):
                heads.add(head)
                heads |= transitiveHeads(head, seen | {key})
            memo[key] = heads
            return heads

        declared = {key: self._declaredTypeClass(decl) for key, decl in memberDecls.items()}
        reported: set[tuple[str, str]] = set()
        for name, typeKey, particle in entries:
            if name not in memberHeads:
                continue
            if not (transitiveHeads(name, frozenset()) & modelKeys):
                continue
            if typeKey == f"decl:{memberDecls[name].expandedName}":
                continue  # a reference site naming the global member itself
            entryCls = self._classOfParticleEntry(typeKey, particle)
            memberCls = declared.get(name)
            if entryCls is None or memberCls is None or entryCls is memberCls:
                continue
            if name in reported:
                continue
            reported.add(name)
            local = name[1] or "?"
            self.report.add_error(
                f"element declarations consistent: substitution member "
                f"'{local}' is declared with a type conflicting with its "
                "global declaration",
                code="element-consistent",
            )

    def _classOfParticleEntry(self, typeKey: str, particle: Any) -> Any:
        """Returns the compiled class a resolved particle entry stands for."""
        if typeKey.startswith("decl:"):
            expanded = typeKey[5:]
            schema = particle.getSchema()
            for element in getattr(schema, "elements", None) or ():
                if getattr(element, "expandedName", None) == expanded:
                    return self._declaredTypeClass(element)
            return None
        return self._declaredTypeClass(particle)

    def _reportParticleRestriction(self, er: Any) -> None:
        """Reports particle-invalid restriction derivations.

        For a complex type whose derivation is a restriction carrying a
        particle (cos-particle-restrict, XSD 1.0 §3.9.6), compiles the
        restricting type's own tree and the base type's effective tree
        and hands them to the pure predicate in
        ``pyxsd.particle_derivation``. Every violation is reported as
        ``particle-restriction`` with the deciding rule name in the
        message.

        Skips — never errors — when the type is not a particle
        restriction, the base type cannot be resolved to a compiled
        type, or either tree could not be compiled (the legacy flat
        fallback); each skip is logged at debug level with its reason.
        A ``RecursionError`` from a pathologically large or nested pair
        is likewise caught and logged, leaving the pair unverified
        rather than crashing the parse.
        """
        if er.getDerivation() != "restriction":
            return
        if not _content_children(er):
            # No particle slot (simple-content restriction, facets only):
            # the particle rules do not apply.
            return
        derived_model = compile_content_model(er, self)
        if derived_model is None:
            logger.debug(
                "particle restriction: content model of %s could not be compiled; skipped",
                getattr(er, "name", "?"),
            )
            return
        base_class = self._baseTypeClass(er)
        if base_class is None:
            logger.debug(
                "particle restriction: base type %r of %s unresolved; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        base_model = getattr(base_class, "_contentModel_", None)
        if base_model is None:
            logger.debug(
                "particle restriction: base type %s has no compiled content model; skipped",
                getattr(base_class, "name", "?"),
            )
            return
        resolver = lambda particle: getattr(particle, "descriptor", None)  # noqa: E731
        head_lookup = self._substitution_head_lookup(er)
        member_lookup = self._substitution_member_lookup(er)
        try:
            reasons = list(
                is_valid_particle_restriction(
                    base_model,
                    derived_model,
                    resolver,
                    head_lookup=head_lookup,
                    member_lookup=member_lookup,
                )
            )
            reasons.extend(
                derived_wildcard_edc_violations(
                    base_model, derived_model, resolver, self._globalElementLookup(er)
                )
            )
        except RecursionError:
            logger.debug(
                "particle restriction: recursion limit hit checking %s; skipped",
                getattr(er, "name", "?"),
            )
            return
        for reason in reasons:
            self.report.add_error(reason, code="particle-restriction")

    def _reportMixedRestriction(self, er: Any) -> None:
        """Reports a mixed type restricting a non-mixed base type.

        ``Derivation Valid (Restriction, Complex)`` clause 2.4.1 admits a
        mixed derived content type only when the base content type is
        mixed too (2.4.1.2); an element-only derived type may restrict an
        element-only or mixed base (2.4.1.1). The rule bites even when
        the derived particle is empty — a ``maxOccurs=0`` reference makes
        the effective content a synthetic empty sequence, but the content
        type still reads mixed (groupH007v; its non-mixed twin
        groupH008v is valid). A simple-content restriction is not a mixed
        complex-content shape, and an ``xs:anyType`` base is exempt
        (clause 2.1). Skips — never errors — when the base type cannot be
        resolved or is not a complex type definition.
        """
        if er.getDerivation() != "restriction":
            return
        if er._firstProcessedChild(er, "SimpleContent") is not None:
            return
        if not self._typeIsMixed(er):
            return
        if self._baseIsAnyType(er):
            return
        base_er = self._baseTypeER(er)
        if base_er is None or not hasattr(base_er, "effectiveMixed"):
            logger.debug(
                "mixed restriction: base type %r of %s unresolved or not complex; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        if self._typeIsMixed(base_er):
            return
        self.report.add_error(
            "particle restriction (Derivation Valid (Restriction, Complex) "
            "clause 2.4.1): the mixed content type of "
            f"'{getattr(er, 'name', '?')}' cannot restrict the non-mixed "
            f"base type '{getattr(base_er, 'name', '?')}'",
            code="particle-restriction",
        )

    def _reportMixedConflict(self, er: Any) -> None:
        """Reports a ``mixed`` conflict between complexType and complexContent.

        XSD 1.1 complex type XML representation: when both the
        ``xs:complexType`` and its ``xs:complexContent`` child carry a
        ``mixed`` attribute, the two values must be identical
        (complex002). An attribute on only one of the two keeps the
        historical reading — the ``complexContent`` value alone decides
        (§3.4.2.3.3 clause 1) — so a one-sided ``mixed`` is not a
        conflict.
        """
        own = (getattr(er, "tagAttributes", {}) or {}).get("mixed")
        if own is None:
            return
        for child in getattr(er, "processedChildren", None) or ():
            if child is not None and type(child).__name__ == "ComplexContent":
                value = (getattr(child, "tagAttributes", {}) or {}).get("mixed")
                if value is not None and _mixedIsTrue(value) != _mixedIsTrue(own):
                    self.report.add_error(
                        "the mixed attribute of complexType and complexContent "
                        f"must be the same for type '{getattr(er, 'name', '?')}': "
                        f'complexType mixed="{own}" conflicts with '
                        f'complexContent mixed="{value}"',
                        code="declaration-attribute",
                    )
                break

    def _reportMixedExtension(self, er: Any) -> None:
        """Reports an extension whose mixed content diverges from its base.

        Derivation Valid (Extension) clause 1.4.3.2.2 requires the
        {content type} of the derived type to match the base's: a mixed
        type may only extend a mixed base and an element-only type may
        only extend an element-only base. An explicit mixed content over
        an element-only base (complex025) and an element-only content
        that adds particles to a mixed base (complex026) are both errors.
        A bare or attribute-only extension of a mixed base legitimately
        inherits the base content type (§3.4.2.3.3 clause 4.2.2), so it
        is exempt. Hides a simple-content base and an ``xs:anyType``
        base; never errors when the base cannot be resolved or is not a
        complex type definition.
        """
        if er.getDerivation() != "extension":
            return
        if er._firstProcessedChild(er, "SimpleContent") is not None:
            return
        if self._baseIsAnyType(er):
            return
        base_er = self._baseTypeER(er)
        if base_er is None or not hasattr(base_er, "effectiveMixed"):
            logger.debug(
                "mixed extension: base type %r of %s unresolved or not complex; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        own = er._ownMixed()
        base = base_er.effectiveMixed()
        if own == base:
            return
        if not own and base and er._explicitContentEmpty():
            return
        self.report.add_error(
            "particle restriction (Derivation Valid (Extension) clause "
            f"1.4.3.2.2): the {'mixed' if own else 'element-only'} content "
            f"type of '{getattr(er, 'name', '?')}' cannot be derived by "
            f"extension from the {'mixed' if base else 'element-only'} "
            f"base type '{getattr(base_er, 'name', '?')}'",
            code="particle-restriction",
        )

    def _reportComplexContentFromSimpleBase(self, er: Any) -> None:
        """Reports complex content derived from a simple-content base.

        ``Derivation Valid (Extension)`` clause 1.4 and ``Derivation
        Valid (Restriction, Complex)`` clause 2.2 admit a complex-content
        derivation of a complex type only when both sides carry the same
        simple content, both are empty, or the derived content is
        element-only/mixed over an element-only/mixed base. Explicit
        complex content (an empty sequence included) over a base whose
        content type is simple satisfies none of them, so both the
        extension and the restriction shapes are errors under the XSD
        1.1 harness profile (particlesZ031 is the 1.0-valid/1.1-invalid
        split; particlesZ039 is the restriction twin). A base that is
        not a complex type with simple content — ``xs:anyType``, a
        complex base, or an unresolvable reference — is skipped.
        """
        if er.getDerivation() not in ("extension", "restriction"):
            return
        if er._firstProcessedChild(er, "SimpleContent") is not None:
            return
        if er._firstProcessedChild(er, "ComplexContent") is None:
            return
        if self._baseIsAnyType(er):
            return
        base_er = self._baseTypeER(er)
        if base_er is None or not hasattr(base_er, "processedChildren"):
            logger.debug(
                "complex content from simple base: base type %r of %s "
                "unresolved or not complex; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        if base_er._firstProcessedChild(base_er, "SimpleContent") is None:
            return
        self.report.add_error(
            "particle restriction (Derivation Valid, complex type "
            f"'{getattr(er, 'name', '?')}' adopts explicit complex content "
            f"but its base type '{getattr(base_er, 'name', '?')}' has simple "
            "content)",
            code="particle-restriction",
        )

    def _reportUniqueParticleAttribution(self, er: Any) -> None:
        """Reports an ambiguous effective content model (cos-nonambig).

        Unique Particle Attribution is a property of a complex type's
        *effective* content model, so the sweep runs over the compiled
        model — the base type's tree composed with an extension's own
        suffix, which is where an inherited wildcard and a suffix
        wildcard can overlap (particlesZ022). The pure sweep lives in
        :mod:`pyxsd.upa`; it reports the first ambiguity. A type whose
        model could not be compiled, or a model with nothing to compare,
        is skipped.
        """
        if type(er).__name__ != "ComplexType":
            return
        model = compile_content_model(er, self)
        if model is None:
            logger.debug(
                "unique particle attribution: content model of %s could not be compiled; skipped",
                getattr(er, "name", "?"),
            )
            return
        for reason in upa_violations(model, self._substitution_head_lookup(er)):
            self.report.add_error(reason, code="upa")

    def _fixedValuesEqual(self, base_attr: Any, derived_attr: Any) -> bool:
        """Whether two attribute uses carry equal ``fixed`` values.

        Returns ``True`` when the base use has no ``fixed``. Lexically
        different spellings that denote one XSD value compare equal, so
        a list attribute's ``fixed`` may be re-spaced and a ``token``
        collapsed (addB183); comparison falls back to the lexical form
        when the attribute's type cannot be resolved.
        """
        base_fixed = base_attr.getFixed()
        if base_fixed is None:
            return True
        derived_fixed = derived_attr.getFixed()
        if derived_fixed is None:
            return False
        if base_fixed == derived_fixed:
            return True
        try:
            base_value = base_attr.getType()(base_fixed)
            derived_value = derived_attr.getType()(derived_fixed)
        except (AttributeError, TypeError, ValueError):
            return False
        return xsd_value_key(base_value) == xsd_value_key(derived_value)

    def _reportAttributeUseDerivation(self, er: Any) -> None:
        """Reports attribute-use derivation violations on a complex type.

        The bounded slice of the attribute clauses the corpus pins:

        * Restriction (XSD 1.0 §3.4.6 clause 2.1.1): a derived use that
          redeclares a *required* base use must stay required;
          weakening it to optional is reported (particlesZ030_d,
          particlesZ017). Omitting the base use entirely is accepted —
          the Saxon 1.1 Assert suite pins such restrictions valid
          (assert011), so the literal XSD 1.0 clause 3 shape is not
          implemented.
        * Extension (XSD 1.1 §3.4.6.2 clause 1.2): a base use must be
          reproduced with identical properties; a redeclaration that
          changes the ``fixed`` value is reported (particlesZ026a).

        The base's effective uses are gathered through its extension
        chain (a restriction does not inherit uses). The check is
        skipped when the base type cannot be resolved or carries an
        XSD 1.1 ``inheritable`` use, which the restriction mapping
        inherits automatically (so omitting it is legal). Attribute
        type derivation and the remaining value-constraint clauses stay
        out of this slice.
        """
        derivation = self._complexTypeDerivation(er)
        if derivation not in ("restriction", "extension"):
            return
        base_er = self._baseTypeER(er)
        if base_er is None or not hasattr(base_er, "attributes"):
            logger.debug(
                "attribute-use derivation: base type %r of %s unresolved or not complex; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        base_uses, inheritable = self._effectiveAttributeUses(base_er)
        if not base_uses or inheritable:
            return
        own_uses = getattr(er, "attributes", None) or {}
        if derivation == "restriction":
            for name, derived_attr in own_uses.items():
                base_attr = base_uses.get(name)
                if base_attr is None:
                    continue
                if self._attributeUseIsRequired(base_attr) and not self._attributeUseIsRequired(
                    derived_attr
                ):
                    self.report.add_error(
                        f"attribute-use restriction: type "
                        f"'{getattr(er, 'name', '?')}' redeclares the required "
                        f"attribute '{name}' of its base type "
                        f"'{getattr(base_er, 'name', '?')}' as optional",
                        code="attribute-restriction",
                    )
                base_fixed = base_attr.getFixed()
                if not self._fixedValuesEqual(base_attr, derived_attr):
                    self.report.add_error(
                        f"attribute-use restriction: attribute '{name}' of type "
                        f"'{getattr(er, 'name', '?')}' does not preserve the fixed "
                        f"value '{base_fixed}' of its base type "
                        f"'{getattr(base_er, 'name', '?')}'",
                        code="attribute-restriction",
                    )
            return
        for name, derived_attr in own_uses.items():
            base_attr = base_uses.get(name)
            if base_attr is None:
                continue
            base_fixed = base_attr.getFixed()
            if not self._fixedValuesEqual(base_attr, derived_attr):
                self.report.add_error(
                    f"attribute-use extension: attribute '{name}' of type "
                    f"'{getattr(er, 'name', '?')}' changes the fixed value "
                    f"'{base_fixed}' of its base type "
                    f"'{getattr(base_er, 'name', '?')}'",
                    code="attribute-restriction",
                )

    def _effectiveAttributeUses(
        self, er: Any, seen: set[int] | None = None
    ) -> tuple[dict[str, Any], bool]:
        """A type's effective attribute uses, walking its extension chain.

        Returns ``(uses-by-name, has-inheritable)``. A restriction does
        not inherit its base's uses; an extension does. ``seen`` guards
        a derivation cycle (itself an invalid schema).
        """
        uses = dict(getattr(er, "attributes", None) or {})
        has_inheritable = any(self._attributeUseIsInheritable(attr) for attr in uses.values())
        if self._complexTypeDerivation(er) != "extension":
            return uses, has_inheritable
        if seen is None:
            seen = set()
        if id(er) in seen:
            return uses, has_inheritable
        seen.add(id(er))
        base_er = self._baseTypeER(er)
        if base_er is None or not hasattr(base_er, "attributes"):
            return uses, has_inheritable
        base_uses, base_inheritable = self._effectiveAttributeUses(base_er, seen)
        for name, attr in base_uses.items():
            uses.setdefault(name, attr)
        return uses, has_inheritable or base_inheritable

    @staticmethod
    def _complexTypeDerivation(er: Any) -> str | None:
        """The type's derivation method, simpleContent wrappers included.

        ``XsdType.getDerivation`` reads ``complexContent`` wrappers only;
        a ``simpleContent`` extension or restriction reports its method
        through this helper.
        """
        derivation = er.getDerivation()
        if derivation is not None:
            return derivation
        simple = er._firstProcessedChild(er, "SimpleContent")
        if simple is None:
            return None
        for child in getattr(simple, "processedChildren", None) or ():
            kind = type(child).__name__
            if kind == "Extension":
                return "extension"
            if kind == "Restriction":
                return "restriction"
        return None

    @staticmethod
    def _attributeUseIsRequired(attr: Any) -> bool:
        getter = getattr(attr, "getUse", None)
        return callable(getter) and getter() == "required"

    @staticmethod
    def _attributeUseIsInheritable(attr: Any) -> bool:
        attributes = getattr(attr, "tagAttributes", None) or {}
        return str(attributes.get("inheritable") or "").strip().lower() in ("true", "1")

    def _globalElementLookup(self, er: Any) -> Any:
        """A lookup from a Clark name to a top-level element declaration.

        Feeds the XSD 1.1 static tighter EDC check (the binding the
        derived type's wildcard resolves to). Names that name no global
        declaration return ``None``, which the check reads as "nothing
        to compare".
        """
        try:
            schema = er.getSchema()
        except AttributeError:
            return lambda name: None
        elements = [
            element
            for element in getattr(schema, "elements", None) or ()
            if type(element).__name__ == "Element" and element.name
        ]

        def lookup(name: str) -> Any:
            local = local_name(name)
            uri = namespace_of(name)
            for element in elements:
                if getattr(element, "name", None) != local:
                    continue
                try:
                    element_uri = element.getNamespace()
                except AttributeError:
                    element_uri = None
                if element_uri == uri:
                    return element
            return None

        return lookup

    def _reportAttributeWildcardRestriction(self, er: Any) -> None:
        """Reports a restriction that does not narrow the base wildcard.

        The derived type's effective attribute wildcard (its local
        ``anyAttribute`` plus the ones its attribute groups contribute,
        intersected) must be a valid restriction of the base type's
        effective wildcard: the namespace constraint may only narrow and
        ``processContents`` must not weaken. A derived type that declares
        no attribute wildcard of its own accepts fewer attributes and is
        always a valid restriction, so only a type whose complete
        wildcard is non-absent is checked. Skips — never errors — when
        the base type cannot be resolved or carries no attribute
        wildcard of its own (an empty derived wildcard admits nothing,
        making the semantic rule depend on more than the constraint
        shape).
        """
        if er.getDerivation() != "restriction":
            return
        ownSpecs = getattr(er, "wildcardAttributeSpecs", None)
        if not ownSpecs:
            return
        baseER = self._baseTypeER(er)
        if baseER is None:
            logger.debug(
                "attribute wildcard restriction: base type %r of %s unresolved; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        baseSpecs = getattr(baseER, "wildcardAttributeSpecs", None)
        if not baseSpecs:
            # The base has no attribute wildcard; whether a derived
            # wildcard is a valid restriction is a semantic question
            # (an empty derived wildcard admits nothing and is fine), so
            # the structural comparison below has nothing to check.
            logger.debug(
                "attribute wildcard restriction: base type %s has no attribute wildcard; skipped",
                getattr(baseER, "name", "?"),
            )
            return
        target = er.getNamespace()
        own = effective_attribute_wildcard(ownSpecs, target)
        base = effective_attribute_wildcard(baseSpecs, baseER.getNamespace())
        if own is None or base is None:
            return
        if not wildcard_subset(own, base, target):
            self.report.add_error(
                f"restriction of type '{er.name}' widens the base attribute "
                f"wildcard namespace constraint ('{own.namespace}' is not a "
                f"subset of '{base.namespace}')",
                code="wildcard-invalid",
            )
            return
        if PROCESS_SEVERITY.get(own.process_contents, 2) < PROCESS_SEVERITY.get(
            base.process_contents, 2
        ):
            self.report.add_error(
                f"restriction of type '{er.name}' weakens the base attribute "
                f"wildcard processContents ('{own.process_contents}' is weaker "
                f"than '{base.process_contents}')",
                code="wildcard-invalid",
            )

    def _baseTypeClass(self, er: Any) -> Any | None:
        """The generated class of the first resolvable base type."""
        for raw_name in getattr(er, "superClassNames", []) or []:
            resolved = er.resolveSchemaQName(raw_name, parser=self)
            candidate = ElementRepresentative.typeFromName(resolved, self)
            if candidate is not None:
                return candidate
        return None

    def _baseTypeER(self, er: Any) -> Any | None:
        """The element representative of the first resolvable base type.

        Mirrors ``ElementRepresentative.typeFromName``'s lookup but
        returns the representative itself, which still carries the XML
        attributes (``mixed``) a generated class does not.
        """
        table = self.components
        if table is None:
            return None
        strict = getattr(getattr(self, "mode", None), "namespaces", "legacy") == "strict"
        for raw_name in getattr(er, "superClassNames", []) or []:
            resolved = er.resolveSchemaQName(raw_name, parser=self)
            if strict:
                if namespace_of(resolved) == XSD_NS:
                    return None
                found = table.getFromName(
                    local_name(resolved),
                    kind="type",
                    namespace=namespace_of(resolved),
                    warn=False,
                )
            else:
                found = table.getFromName(resolved, kind="type", warn=False)
                if found is None:
                    found = table.getFromName(str(resolved).split(":")[-1], kind="type", warn=False)
            if found is not None:
                return found
        return None

    def _baseIsAnyType(self, er: Any) -> bool:
        """Whether the first base type is the built-in ``xs:anyType``.

        ``xs:anyType`` has no generated class, so ``_baseTypeClass``
        cannot resolve it; the extension-structure check reads it as its
        effective mixed-sequence content instead. A user type named
        ``anyType`` never matches (the reference must be in the XML
        Schema namespace or carry the ``xs:``/``xsd:`` spelling in
        legacy mode).
        """
        for raw_name in getattr(er, "superClassNames", []) or []:
            resolved = er.resolveSchemaQName(raw_name, parser=self)
            if namespace_of(resolved) == XSD_NS and local_name(resolved) == "anyType":
                return True
            if namespace_of(resolved) is not None or not isinstance(raw_name, str):
                continue
            prefix, _, local = raw_name.strip().partition(":")
            if local == "anyType" and prefix in ("xs", "xsd"):
                return True
        return False

    def _reportExtensionStructure(self, er: Any) -> None:
        """Reports invalid particle composition in a complex type extension.

        An extension's explicit content model is appended to the base
        type's effective content model (XSD 1.1 §3.4.2.3.3). The
        ``all`` compositor is special: an ``all`` may extend only an
        ``all`` (the 1.1 relaxation; ``all`` extends ``sequence``/
        ``choice`` and the reverse are invalid even for singletons —
        all309-312, particlesFb002), the two ``minOccurs`` must match
        (all313), and the composed ``all`` must be unambiguous — no
        repeated element particles (all302) and no overlapping
        wildcards (all305). A base whose *effective* content is empty
        takes the suffix as its model unless the base is mixed, which
        cannot be extended by an ``all`` at all (all308, bug 6202).

        Skips — never errors — when the type is not an extension, when
        it has no explicit content (nothing to append), or when either
        content model cannot be compiled or the base type cannot be
        resolved; each skip is logged at debug level with its reason.
        """
        if er.getDerivation() != "extension":
            return
        own = compile_own_content(er, self)
        if own is None:
            if _content_children(er):
                logger.debug(
                    "extension structure: content model of %s could not be compiled; skipped",
                    getattr(er, "name", "?"),
                )
            else:
                logger.debug(
                    "extension structure: %s has no explicit content; skipped",
                    getattr(er, "name", "?"),
                )
            return
        base_is_any_type = self._baseIsAnyType(er)
        base_class = None if base_is_any_type else self._baseTypeClass(er)
        if not base_is_any_type and base_class is None:
            logger.debug(
                "extension structure: base type %r of %s unresolved; skipped",
                list(getattr(er, "superClassNames", []) or []),
                getattr(er, "name", "?"),
            )
            return
        base_model = (
            self._ANY_TYPE_CONTENT
            if base_is_any_type
            else getattr(base_class, "_contentModel_", None)
        )
        if base_model is None:
            logger.debug(
                "extension structure: base type %s has no compiled content model; skipped",
                getattr(base_class, "name", "?"),
            )
            return
        own_term = all_term(own)
        own_kind = "all" if own_term is not None else own.kind
        base_term = all_term(base_model)
        base_kind = "all" if base_term is not None else base_model.kind
        if self._particleIsEmpty(base_model):
            base_er = self._baseTypeER(er)
            if own_kind == "all" and base_er is not None and self._typeIsMixed(base_er):
                self.report.add_error(
                    "particle restriction (cos-ct-extends): an all cannot extend "
                    "empty mixed content (the empty mixed base puts the all inside "
                    "a sequence)",
                    code="particle-restriction",
                )
            return
        if self._particleIsEmpty(own):
            # Empty explicit content appends nothing: the base particle
            # is the effective model, whatever its compositor.
            return
        if own_kind == "all" and base_kind != "all":
            self.report.add_error(
                f"particle restriction (cos-ct-extends): all cannot extend {base_kind} "
                "(only an all may extend an all)",
                code="particle-restriction",
            )
            return
        if own_kind != "all" and base_kind == "all":
            self.report.add_error(
                f"particle restriction (cos-ct-extends): {own_kind} cannot extend all",
                code="particle-restriction",
            )
            return
        if own_kind != "all" or base_term is None:
            return
        occurrence = all_extension_occurrence(own)
        if occurrence is None:
            return
        own_all, own_min = occurrence
        if own_min != base_model.min_occurs:
            self.report.add_error(
                "particle restriction (cos-particle-extend): minOccurs mismatch - "
                "when an all extends an all both must have the same minOccurs "
                f"(base={base_model.min_occurs}, extension={own_min})",
                code="particle-restriction",
            )
        self._reportExtensionAllOverlap(all_members(base_term), all_members(own_all), er)

    def _reportExtensionAllOverlap(
        self, base_members: list[Any], own_members: list[Any], er: Any
    ) -> None:
        """Reports UPA violations in an all-extends-all composition.

        The composed ``all`` holds the base's particles followed by the
        extension's, so a repeated element name (all302) or two
        overlapping wildcards (all305) make it non-deterministic. The
        base's internal and the extension's internal duplicates are
        already reported by the per-compositor sweep; this compares the
        two sides.
        """
        base_names = {
            member.name for member in base_members if member.kind == "element" and member.name
        }
        for member in own_members:
            if member.kind == "element" and member.name and member.name in base_names:
                self.report.add_error(
                    "particle restriction (cos-nonambig): overlapping particles in "
                    f"all extension - element '{member.name}' appears in both the "
                    "base and the extension all",
                    code="particle-restriction",
                )
                break
        base_wildcards = [member for member in base_members if member.kind == "any" and member.spec]
        own_wildcards = [member for member in own_members if member.kind == "any" and member.spec]
        try:
            target = er.getNamespace()
        except AttributeError:
            target = None
        for first in base_wildcards:
            for second in own_wildcards:
                if wildcard_specs_overlap(first.spec, second.spec, target):
                    self.report.add_error(
                        "particle restriction (cos-nonambig): overlapping wildcards "
                        "in all extension",
                        code="particle-restriction",
                    )
                    return

    @staticmethod
    def _particleIsEmpty(model: Any) -> bool:
        """Whether a compiled model is an *empty* explicit content model.

        XSD 1.1 §3.4.2.3.3 clause 2: an absent model group, an empty
        ``all``/``sequence``, an empty ``choice`` with ``minOccurs=0``
        and any model group with ``maxOccurs=0`` are empty content. (An
        empty ``choice`` with ``minOccurs=1`` is unsatisfiable, not
        empty.)
        """
        if model.max_occurs == 0:
            return True
        if model.kind in ("sequence", "all") and not model.children:
            return True
        return model.kind == "choice" and not model.children and model.min_occurs == 0

    @staticmethod
    def _typeIsMixed(er: Any) -> bool:
        """Whether a complex type's effective ``mixed`` value is true.

        Delegates to the representative's own XSD 1.1 §3.4.2.3.3
        clause 1 reading; a representative without the method (a
        built-in, say) is not mixed.
        """
        mixed_method = getattr(er, "effectiveMixed", None)
        return bool(mixed_method()) if mixed_method is not None else False

    def _substitution_head_lookup(self, er: Any) -> Callable[[Any], Any] | None:
        """A declaration-to-head resolver for NameAndTypeOK.

        NameAndTypeOK admits a restricting element that is a (transitive)
        member of the base element's substitution group; the walk needs
        each member's head *declaration*, which only the schema's global
        element table can supply. Under XSD 1.1 a local declaration with
        no ``substitutionGroup`` of its own shares the membership of a
        global declaration with the same expanded name (all226; XSD 1.1
        bug 5296), so the lookup falls back to such a global before
        giving up. ``None`` when this schema has no element table, in
        which case the predicate falls back to exact expanded-name
        equality.
        """
        try:
            schema = er.getSchema()
        except AttributeError:
            return None
        if schema is None:
            return None
        candidates = [
            element
            for element in getattr(schema, "elements", None) or []
            if type(element).__name__ == "Element"
        ]

        def lookup(declaration: Any) -> Any:
            head_name = declaration.getSubstitutionGroupHead(self)
            resolving = declaration
            if not head_name:
                # XSD 1.1: a local declaration whose expanded name is
                # also declared globally inherits that global's
                # substitution-group membership. The expanded names are
                # form-aware: an unqualified local is *not* the same
                # expanded name as a same-named global (elemZ020).
                for candidate in candidates:
                    if candidate is declaration:
                        continue
                    if candidate.name == declaration.name and element_namespace(
                        candidate
                    ) == element_namespace(declaration):
                        head_name = candidate.getSubstitutionGroupHead(self)
                        resolving = candidate
                        break
            if not head_name:
                return None
            resolver = getattr(resolving, "resolveReference", None)
            if resolver is None:
                return None
            return resolver(head_name, candidates, parser=self)

        return lookup

    def _substitution_member_lookup(self, er: Any) -> Callable[[Any], list[Any]] | None:
        """A declaration-to-members resolver for the clause 2.1 expansion.

        §3.9.6 clause 2.1 treats a substitution-group head's element
        particle as a choice group with one particle per member, so the
        particle-derivation predicate needs the reverse of
        ``_substitution_head_lookup``: the declarations that name this
        head. ``None`` when the schema's element table is unavailable,
        in which case the predicate leaves the head unexpanded.
        """
        head_lookup = self._substitution_head_lookup(er)
        if head_lookup is None:
            return None
        try:
            schema = er.getSchema()
        except AttributeError:
            return None
        if schema is None:
            return None
        candidates = [
            element
            for element in getattr(schema, "elements", None) or []
            if type(element).__name__ == "Element"
        ]

        def members(declaration: Any) -> list[Any]:
            found = []
            for element in candidates:
                if element is declaration:
                    continue
                head = head_lookup(element)
                if head is not None and head is declaration:
                    found.append(element)
            return found

        return members

    def _reportPointlessParticle(self, er: Any) -> None:
        """Reports a pointless ``sequence``/``choice`` inside an optional group.

        The particlesHa rule: a compositor with *no particle children*
        (``{particles}`` is empty) whose ``minOccurs`` lets it match
        zero — the ERs' ``emptiable`` property — is pointless when the
        group definition containing it is referenced with
        ``minOccurs="0"``, because the optional reference can always be
        satisfied without it. Such a particle must be eliminated
        (particlesHa008). Deliberately conservative shapes beyond that
        are not reported: a compositor with children can still match
        content even when every child is optional or ``maxOccurs="0"``,
        so eliminating it could change the content model's language
        (MS pins such schemas valid — groupL007 — and real-world
        schemas, ECMA-376 among them, use the shape heavily), and an
        empty compositor with ``minOccurs="1"`` is unsatisfiable rather
        than eliminable. ``all`` compositors are not reported — their
        legality is the ``all-rule``.
        """
        if er._particleChildren() or not getattr(er, "emptiable", False):
            return
        definition = self._containingGroupDefinition(er)
        if definition is None:
            return
        for refSite in self._groupRefSites(definition):
            if self._isOptionalParticle(refSite):
                self.report.add_error(
                    f"pointless particle: the empty {er.rawTag} in group "
                    f"'{definition.name}' matches no elements and the "
                    "group reference is optional (minOccurs=0); the "
                    "particle is pointless and must be eliminated",
                    code="pointless-particle",
                )
                return

    def _containingGroupDefinition(self, er: Any) -> Any | None:
        """The group definition a compositor's occurrence context hangs on.

        Walks up through enclosing compositors (their own occurrence
        does not make an emptiable child required) and returns the
        group definition the chain ends in, or ``None`` for any other
        container (a complex type root, say) or a malformed chain.
        """
        node = er.parent
        while node is not None:
            kind = type(node).__name__
            if kind in self._COMPOSITOR_KINDS:
                node = node.parent
                continue
            if kind == "Group" and not getattr(node, "isRefSite", False):
                return node
            return None
        return None

    def _groupRefSites(self, definition: Any) -> list[Any]:
        """The group reference sites in this schema that name *definition*.

        The whole document tree is scanned (definitions do not record
        who references them); a reference that cannot be resolved to
        this definition — including one naming an import — is skipped.
        """
        try:
            schema = definition.getSchema()
        except AttributeError:
            return []
        refs: list[Any] = []
        stack = [schema] if schema is not None else []
        seen: set[int] = set()
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "Group" and getattr(er, "isRefSite", False):
                try:
                    if definition.resolveGroupRef(er) is definition:
                        refs.append(er)
                except NamespaceError:
                    continue
            stack.extend(getattr(er, "processedChildren", None) or ())
        return refs

    def _isOptionalParticle(self, particle: Any) -> bool:
        """Whether a particle's raw ``minOccurs`` is 0 (silent read)."""
        return getattr(particle, "minOccurs", None) == "0"

    def _globalElements(self, er: Any) -> list[Any]:
        """The schema document's global element declarations."""
        try:
            schema = er.getSchema()
        except AttributeError:
            return []
        return [
            element
            for element in getattr(schema, "elements", None) or ()
            if type(element).__name__ == "Element" and element.name
        ]

    def _resolveParticles(self, particles: list[Any]) -> list[tuple[tuple[str, str], str, Any]]:
        """Maps particles to ``(expanded name, type key, particle)``.

        A reference site adopts the referred declaration's expanded name
        and a shared ``decl:`` type key; a reference that does not
        resolve against this document's globals is dropped (its type is
        unknown). A name site keeps its declared name and the raw (or
        inline-generated) ``type`` attribute as its key.
        """
        globals_ = self._globalElements(particles[0] if particles else None)
        resolved: list[tuple[tuple[str, str], str, Any]] = []
        for particle in particles:
            if getattr(particle, "isElementRef", False):
                referred = None
                resolver = getattr(particle, "resolveReference", None)
                if resolver is not None:
                    referred = resolver(
                        getattr(particle, "ref", None),
                        globals_,
                        parser=getattr(particle.getSchema(), "pyXSD", None),
                    )
                if referred is None:
                    continue
                try:
                    namespace = referred.getNamespace() or ""
                except AttributeError:
                    namespace = ""
                name = (namespace, referred.name or "")
                resolved.append((name, f"decl:{referred.expandedName}", particle))
                continue
            try:
                namespace = particle.getNamespace() or ""
            except AttributeError:
                namespace = ""
            name = (namespace, getattr(particle, "name", None) or "")
            typeKey = particle.tagAttributes.get("type") or ""
            resolved.append((name, typeKey, particle))
        return resolved

    def _collectParticles(self, er: Any, out: list[Any], visited: set[int]) -> None:
        """Collects element particles under *er* transitively.

        Descends nested compositors and — because a group reference's
        content model is spliced into the referencing type's model —
        through group references to their definition's compositor
        (mgR022's conflicting ``e1`` sits inside a referenced sequence).
        A reference that cannot be resolved here (it names an import)
        is skipped: its contents belong to that document's own sweep.
        """
        for child in getattr(er, "processedChildren", None) or ():
            if child is None or id(child) in visited:
                continue
            visited.add(id(child))
            kind = type(child).__name__
            if kind == "Element":
                out.append(child)
            elif kind in self._COMPOSITOR_KINDS:
                self._collectParticles(child, out, visited)
            elif kind == "Group" and getattr(child, "isRefSite", False):
                definition = self._groupRefDefinition(child)
                if definition is None:
                    continue
                compositor = definition.getCompositor()
                if compositor is not None and id(compositor) not in visited:
                    visited.add(id(compositor))
                    self._collectParticles(compositor, out, visited)

    def _groupRefDefinition(self, refSite: Any) -> Any | None:
        """The group definition a reference site names, or ``None``.

        Mirrors the content-model compiler's resolution: QName-aware
        where the document supplies a namespace context, then the
        schema's group table by full reference and local name.
        """
        ref = getattr(refSite, "ref", None)
        if not ref:
            return None
        try:
            schema = refSite.getSchema()
        except AttributeError:
            return None
        groups = getattr(schema, "groups", None)
        if not groups:
            return None
        resolver = getattr(refSite, "resolveReference", None)
        if resolver is not None:
            resolved = resolver(ref, groups.values(), parser=getattr(schema, "pyXSD", None))
            if resolved is not None:
                return resolved
        return groups.get(ref) or groups.get(ref.split(":")[-1])

    def _checkSubstitutionOverlap(self, resolved: list[tuple[tuple[str, str], str, Any]]) -> None:
        """Reports a substitution-related pair meeting inside one ``all``.

        A member can stand wherever its head appears, so two particles
        under one ``all`` are ambiguous when their expanded name sets —
        the particle's own name plus every declaration that may
        substitute for it — intersect. That covers the head/member pair
        (all241), a member shared by two heads (all242: ``q`` substitutes
        for both ``o`` and ``p``), and a member belonging to two groups
        whose heads are both present (subsgroup903). A head whose
        {block} excludes substitution cannot be substituted here, so it
        does not expand and the model stays deterministic (elemZ028a).
        Only the immediate substitution step is consulted; transitive
        chains are left to the substitution-group machinery.
        """
        if not resolved:
            return
        try:
            schema = resolved[0][2].getSchema()
        except AttributeError:
            return
        memberHeads: dict[tuple[str, str], set[tuple[str, str]]] = {}
        blockedHeads: set[tuple[str, str]] = set()

        def headsOf(holder: Any) -> set[tuple[str, str]]:
            names: set[tuple[str, str]] = set()
            for head in holder.getSubstitutionGroupHeads(self):
                if head.startswith("{"):
                    namespace, _, local = head[1:].partition("}")
                    names.add((namespace, local))
                else:
                    names.add((holder.getNamespace() or "", head.split(":")[-1]))
            return names

        def nameOf(holder: Any) -> tuple[str, str]:
            return (holder.getNamespace() or "", getattr(holder, "name", None) or "")

        for element in getattr(schema, "elements", None) or ():
            if type(element).__name__ != "Element" or not element.name:
                continue
            block = (element.tagAttributes.get("block") or "").split()
            if "substitution" in block or "#all" in block:
                blockedHeads.add(nameOf(element))
            if element.tagAttributes.get("substitutionGroup"):
                memberHeads.setdefault(nameOf(element), set()).update(headsOf(element))
        for name, _typeKey, particle in resolved:
            block = (particle.tagAttributes.get("block") or "").split()
            if "substitution" in block or "#all" in block:
                blockedHeads.add(name)
            if particle.tagAttributes.get("substitutionGroup"):
                memberHeads.setdefault(name, set()).update(headsOf(particle))

        expanded: list[tuple[tuple[str, str], set[tuple[str, str]]]] = []
        for name in {name for name, _, _ in resolved}:
            names = {name}
            if name not in blockedHeads:
                for member, heads in memberHeads.items():
                    if name in heads:
                        names.add(member)
            expanded.append((name, names))
        for index, (first, firstNames) in enumerate(expanded):
            for second, secondNames in expanded[index + 1 :]:
                if firstNames & secondNames:
                    firstLabel = first[1] if first[1] else "?"
                    secondLabel = second[1] if second[1] else "?"
                    self.report.add_error(
                        f"content model is ambiguous: elements '{firstLabel}' and "
                        f"'{secondLabel}' can match the same substitution member "
                        "in the same all",
                        code="all-rule",
                    )
                    return

    def _checkAllWildcardOverlap(self, er: Any) -> None:
        """Reports two overlapping wildcards under one all (all243).

        Wildcards carrying XSD 1.1 ``notNamespace``/``notQName``
        constraints are skipped: their exclusion sets can make
        namespace-overlapping wildcards disjoint (wild049), and the
        1.1 wildcard algebra is out of scope here.
        """
        wildcards = [
            child
            for child in getattr(er, "processedChildren", None) or ()
            if child is not None
            and child.__class__.__name__ == "Any"
            and child.xsdElement.get("notNamespace") is None
            and child.xsdElement.get("notQName") is None
        ]
        try:
            target = er.getNamespace()
        except AttributeError:
            target = None
        for index, first in enumerate(wildcards):
            for second in wildcards[index + 1 :]:
                if wildcard_specs_overlap(first.wildcardSpec, second.wildcardSpec, target):
                    self.report.add_error(
                        "content model is ambiguous: an all contains two overlapping wildcards",
                        code="all-rule",
                    )
                    return

    def _checkWildcardParticleOverlap(self, er: Any) -> None:
        """Reports non-deterministic wildcard pairs in a sequence/choice.

        Two element wildcards in one content model whose namespace
        constraints overlap violate Unique Particle Attribution when both
        can match an item at the same point: in a ``choice`` every
        alternative is live at once, while in a ``sequence`` the earlier
        wildcard creates the ambiguity only when it can match again
        (``maxOccurs`` > 1) or be skipped (``minOccurs`` = 0) while every
        particle between the two is emptiable (wildI013/I014 are
        non-deterministic; the single-occurrence wildI011 and the
        second-repeats wildI012 are deterministic and stay valid).
        Wildcards carrying XSD 1.1 ``notNamespace``/``notQName`` are
        skipped (their exclusion sets are a separate task), as is a
        malformed namespace constraint — the declaration check already
        reported that token.
        """
        children = [
            child
            for child in er._particleChildren()
            if not (
                child.__class__.__name__ == "Any"
                and (
                    child.xsdElement.get("notNamespace") is not None
                    or child.xsdElement.get("notQName") is not None
                )
            )
        ]
        wildcards = [
            (index, child)
            for index, child in enumerate(children)
            if child.__class__.__name__ == "Any"
        ]
        if len(wildcards) < 2:
            return
        if self._unreferencedGroupContent(er):
            return
        try:
            target = er.getNamespace()
        except AttributeError:
            target = None
        isChoice = type(er).__name__ == "Choice"
        for position, (index, first) in enumerate(wildcards):
            if not isChoice and not self._wildcardMatchableAgain(first):
                continue
            for laterIndex, second in wildcards[position + 1 :]:
                if not isChoice and not all(
                    getattr(child, "emptiable", False) for child in children[index + 1 : laterIndex]
                ):
                    continue
                firstSpec = first.wildcardSpec
                secondSpec = second.wildcardSpec
                if invalid_namespace_constraint(firstSpec.namespace) is not None:
                    continue
                if invalid_namespace_constraint(secondSpec.namespace) is not None:
                    continue
                if wildcard_specs_overlap(firstSpec, secondSpec, target):
                    self.report.add_error(
                        "content model is ambiguous: two wildcards with "
                        "overlapping namespace constraints are not deterministic",
                        code="wildcard-invalid",
                    )
                    return

    @staticmethod
    def _wildcardMatchableAgain(particle: Any) -> bool:
        """Whether a sequence wildcard is still live when the next is.

        A wildcard that can repeat (``maxOccurs`` > 1) is live again
        after one match, and a skippable one (``minOccurs`` = 0) is live
        next to the following particle from the start. The silent reads
        avoid duplicating the declaration walk's ``invalid-occurs``
        report for a garbage occurrence value.
        """
        return particle._silentOccurs("maxOccurs") > 1 or particle._silentOccurs("minOccurs") == 0

    def _unreferencedGroupContent(self, er: Any) -> bool:
        """Whether *er*'s content model belongs to an unreferenced group.

        Unique Particle Attribution applies to the effective content
        model of a complex type, not to an orphan group definition: the
        corpus pins an ambiguous wildcard sequence inside a group that
        nothing references as *valid* (addB194), and the oracle accepts
        it. A group a complex type's model actually reaches — directly
        or through a chain of group references — becomes part of that
        model, so its compositors are still checked. A reference that
        cannot be resolved here counts as no reference (its contents
        belong to another document's own sweep).
        """
        container = er.getContainingType()
        if container is None or type(container).__name__ != "Group":
            return False
        return id(container) not in self._referencedGroupIds(er)

    def _referencedGroupIds(self, er: Any) -> set[int]:
        """The ids of group definitions a complex type's model can reach.

        UPA is a property of a complex type definition's content model,
        so only group definitions on a path from some complex type are
        swept. The seed set is every complex type declaration in the
        document; the walk then follows each ``group`` reference from
        the referencing model into the definition it names, so a chain
        of referenced groups is covered while a group named only by
        another unreferenced group stays an orphan (I1: the reference
        relation is not one-hop).
        """
        try:
            schema = er.getSchema()
        except AttributeError:
            return set()
        cached = getattr(self, "_referencedGroupIdCache", None)
        if cached is not None and cached[0] is schema:
            return cached[1]
        types: list[Any] = []
        seen: set[int] = set()
        stack = [schema]
        while stack:
            node = stack.pop()
            if node is None or id(node) in seen:
                continue
            seen.add(id(node))
            if type(node).__name__ == "ComplexType":
                types.append(node)
            stack.extend(getattr(node, "processedChildren", None) or ())
        referenced: set[int] = set()
        walked: set[int] = set()
        for complexType in types:
            stack = [complexType]
            while stack:
                node = stack.pop()
                if node is None or id(node) in walked:
                    continue
                walked.add(id(node))
                if type(node).__name__ == "Group" and getattr(node, "isRefSite", False):
                    definition = self._groupRefDefinition(node)
                    if definition is not None:
                        referenced.add(id(definition))
                        stack.append(definition)
                    continue
                stack.extend(getattr(node, "processedChildren", None) or ())
        self._referencedGroupIdCache = (schema, referenced)
        return referenced

    def _checkDeclarationId(self, er: Any, seenIds: dict[str, Any]) -> None:
        """Reports lexical/duplicate ``id`` attributes on declarations.

        Every ``id`` on a schema component is an ``xs:ID``: it must be a
        valid NCName and unique within its schema document. Uniqueness is
        scoped to the main document because "document" is the unit of the
        XML ID rule; components spliced in from includes/imports are not
        compared against it (``_composedElementIds``). The walk visits
        every representative once, so uniqueness is tracked here rather
        than on each subclass.
        """
        raw = getattr(er, "id", None)
        if raw is None:
            return
        # ``id`` is an ``xs:ID`` (an ``xs:NCName`` with whiteSpace=collapse),
        # so compare the collapsed value: two ids differing only in
        # surrounding whitespace are the same XML ID.
        value = str(raw).strip()
        try:
            NCName(value)
        except TypeError:
            self.report.add_error(
                f"id '{value}' on <{er.rawTag}> is not a valid NCName",
                code="declaration-attribute",
                element=er.rawTag,
                phase="schema",
            )
            return
        element = getattr(er, "xsdElement", None)
        if element is not None and id(element) in self._composedElementIds:
            # A component from an included/imported document: ``id``
            # uniqueness is scoped to a single schema document, so it is
            # not compared against the main document's ids.
            return
        if value in seenIds:
            self.report.add_error(
                f"duplicate id '{value}' on <{er.rawTag}>",
                code="declaration-duplicate",
                element=er.rawTag,
                phase="schema",
            )
            return
        seenIds[value] = er

    def _checkDuplicateName(self, er: Any, declared: dict[tuple[str, Any, str], Any]) -> None:
        """Reports a named component declared twice in one symbol space.

        XSD 1.0 §2.5 gives each target namespace one symbol space per
        global component kind, except that simple and complex type
        definitions share one; element, attribute, model group and
        attribute group definitions each have their own. A second
        declaration of the same name in a symbol space is the reported
        error. Identity-constraint names (``key``/``keyref``/
        ``unique``) share one symbol space scoped to their containing
        element declaration. Local element and attribute declarations
        are scoped to their containing complex type and are not
        compared here.
        """
        element = getattr(er, "xsdElement", None)
        name = element.get("name") if element is not None else None
        if not name:
            return
        typeName = type(er).__name__
        isIdentity = typeName in self._IDENTITY_KINDS
        if typeName == "Notation":
            # A notation definition occupies the notation symbol space of
            # its target namespace (notatB005).
            key = ("notation", er.getNamespace(), name)
            if key in declared:
                self.report.add_error(
                    f"duplicate notation declaration '{name}'",
                    code="declaration-duplicate",
                    element=er.rawTag,
                    phase="schema",
                )
                return
            declared[key] = er
            return
        if element is not None and id(element) in self._composedElementIds and not isIdentity:
            # A component spliced in from an included, imported or
            # redefined document is not compared against the main
            # schema's components. Composition may legitimately expose a
            # name twice (nested redefines, a re-parsed document), so the
            # check is scoped to the main document, matching the ``id``
            # uniqueness scope in ``_checkDeclarationId``. Identity
            # constraints are compared across composed documents because
            # their names share the target namespace's symbol space
            # (targetNS00101m2).
            return
        if self._inConditionalInclusion(er):
            # XSD 1.1 conditional-inclusion declarations (``vc:*``) may
            # share a name, selected by version or availability. Without
            # evaluating the selectors, do not report the duplicate.
            return
        if isIdentity:
            parent = getattr(er, "parent", None)
            if parent is None or type(parent).__name__ != "Element":
                return
            # Identity-constraint names are unique per target namespace
            # (not per containing element): the same key name on two
            # elements of one namespace is a schema error, while the same
            # name in another namespace is not (targetNS00101m1/m2).
            key = ("identity", er.getNamespace(), name)
            label = "identity constraint"
        else:
            kind = componentKind(er)
            if kind is None or not er.isGlobalDeclaration():
                return
            namespace = er.getNamespace()
            if namespace in (XML_NS, XLINK_NS):
                # The XML-namespace (xml:lang, xml:space, ...) and XLink
                # attribute declarations are registered as built-ins
                # before the ER run and may also be reached from the
                # namespace's own schema; a repeat there is not an
                # authoring error.
                return
            key = (kind, namespace, name)
            label = {
                "element": "element",
                "attribute": "attribute",
                "type": "type",
                "group": "group",
                "attributeGroup": "attributeGroup",
            }[kind]
        if key in declared:
            self.report.add_error(
                f"duplicate {label} declaration '{name}'",
                code="declaration-duplicate",
                element=er.rawTag,
                phase="schema",
            )
            return
        declared[key] = er

    @staticmethod
    def _inConditionalInclusion(er: Any) -> bool:
        """Whether *er* or an ancestor carries an ``vc:*`` attribute.

        Conditional inclusion can produce declarations that do not
        coexist for any single schema version, so their names must not be
        compared without evaluating the version selectors.
        """
        node = er
        while node is not None:
            element = getattr(node, "xsdElement", None)
            if element is not None and any(
                key.startswith(f"{{{VC_NS}}}") for key in element.attrib
            ):
                return True
            node = getattr(node, "parent", None)
        return False

    def _checkSchemaNamespacedAttributes(self, er: Any) -> None:
        """Reports an XML-Schema-namespace attribute on a schema element.

        Schema components carry unqualified attributes; a qualified
        attribute in the XML Schema namespace (``xsd:targetNamespace``
        on ``schema``, ``xsd:type`` on an ``attribute``) is never a legal
        attribute of a schema element (addB070a, addB082, notatE002).
        Attributes from a genuinely foreign namespace are not examined.
        ``documentation`` bodies are arbitrary XML and are skipped.
        """
        element = getattr(er, "xsdElement", None)
        if element is None or namespace_of(element.tag) != XSD_NS:
            return
        if type(er).__name__ == "Documentation":
            return
        prefix = f"{{{XSD_NS}}}"
        for attr in element.attrib:
            if not attr.startswith(prefix):
                continue
            local = attr[len(prefix) :]
            self.report.add_error(
                f"the attribute '{local}' is in the XML Schema namespace and is "
                f"not a legal attribute of <{er.rawTag}>",
                code="unexpected-attribute",
                element=er.name if isinstance(er.name, str) else None,
                phase="schema",
            )

    def _checkChildGrammar(self, er: Any) -> None:
        """Reports a declaration's children against its grammar table.

        The table lives on the element representative: ``_ALLOWED_CHILDREN``
        (illegal children), ``_MAX_ONE_CHILDREN`` (duplicates),
        ``_CHILD_ORDER``/``_EXCLUSIVE_SLOTS`` (ordering). A class without
        an ``_ALLOWED_CHILDREN`` table is not checked for illegal
        children; its duplicate/order tables default to empty unless it
        declares otherwise.
        """
        if er._ALLOWED_CHILDREN is not None:
            for tag in er.unexpectedChildTags:
                self.report.add_error(
                    f"<{tag}> is not allowed inside <{er.rawTag}>",
                    code="declaration-child",
                    element=er.rawTag,
                    phase="schema",
                )
        counts = Counter(er.childTags)
        for tag in er._MAX_ONE_CHILDREN:
            if counts[tag] > 1:
                self.report.add_error(
                    f"<{er.rawTag}> may contain at most one <{tag}>",
                    code="declaration-duplicate",
                    element=er.rawTag,
                    phase="schema",
                )
        # Every declaration requires a present ``annotation`` to be its
        # first schema child; only the schema root is exempt (it allows
        # annotations before and after declarations). ``childTags`` holds
        # schema-namespace children only, so a foreign element named
        # ``annotation`` never triggers this.
        if (
            er._ANNOTATION_FIRST
            and "annotation" in counts
            and er.childTags.index("annotation") != 0
        ):
            self.report.add_error(
                f"<annotation> must be the first child of <{er.rawTag}>",
                code="declaration-order",
                element=er.rawTag,
                phase="schema",
            )
        if er._CHILD_ORDER:
            slot_of = {tag: i for i, slot in enumerate(er._CHILD_ORDER) for tag in slot}
            seen = [slot_of[t] for t in er.childTags if t != "annotation" and t in slot_of]
            if seen != sorted(seen):
                self.report.add_error(
                    f"children of <{er.rawTag}> are out of order",
                    code="declaration-order",
                    element=er.rawTag,
                    phase="schema",
                )
            occupied = set(seen)
            for slot in er._EXCLUSIVE_SLOTS:
                if slot in occupied and any(x > slot for x in occupied):
                    self.report.add_error(
                        f"<{er.rawTag}> with this content kind cannot also "
                        "contain later declarations",
                        code="declaration-order",
                        element=er.rawTag,
                        phase="schema",
                    )
            # A "one of" slot is an alternative: two *distinct* tags in the
            # same slot are illegal (``choice``+``group``), even though the
            # per-tag max-one check above sees each only once.
            for slot in er._ONE_OF_SLOTS:
                slotTags = {t for t in er.childTags if t != "annotation" and slot_of.get(t) == slot}
                if len(slotTags) > 1:
                    choices = ", ".join(f"<{tag}>" for tag in sorted(slotTags))
                    self.report.add_error(
                        f"<{er.rawTag}> may contain only one of {choices}",
                        code="declaration-duplicate",
                        element=er.rawTag,
                        phase="schema",
                    )

    def _buildSubstitutionGroups(self, schemaER: Any) -> None:
        """Maps substitution-group heads to their member elements.

        XSD 1.0 declares substitution groups on global element
        declarations: a member element carries ``substitutionGroup``
        naming its head. After the ER run, every member is recorded
        under its head's local name so instance parsing can dispatch
        member elements wherever the head is allowed. Heads that name
        no global element are recorded as schema errors.
        """
        # ``schema.elements`` normally holds element declarations, but a
        # malformed schema can put other component kinds there (a
        # top-level group reference, for example); only real element
        # declarations participate in substitution groups.
        elements = [e for e in schemaER.elements if type(e).__name__ == "Element"]
        declaredNames = {element.name for element in elements}
        declaredExpanded = {element.expandedName for element in elements}
        for element in elements:
            heads = element.getSubstitutionGroupHeads(self)
            if not heads:
                continue
            known = [head for head in heads if head in declaredNames or head in declaredExpanded]
            if not known:
                self.report.add_error(
                    f"element '{element.name}' declares substitutionGroup "
                    f"'{element.tagAttributes.get('substitutionGroup', heads[0])}', "
                    "but no global element with that name exists",
                    code="unknown-substitution-head",
                    element=element.name,
                )
                continue
            for head in dict.fromkeys(known):
                schemaER.substitutionGroups.setdefault(head, []).append(element)
        logger.debug("Substitution groups built: %s", list(schemaER.substitutionGroups))
        self._reportSubstitutionGroupCycles(elements)

    def _reportSubstitutionGroupCycles(self, elements: list[Any]) -> None:
        """Reports substitution-group membership cycles.

        A cycle in the ``substitutionGroup`` graph (foo heads bar heads
        foo) has no well-founded head, so the schema is invalid. Each
        element that reaches a cycle while following its head chain is
        reported once; trivial self-reference is included.
        """
        byName: dict[str, Any] = {}
        for element in elements:
            if element.name:
                byName.setdefault(element.name, element)
            expanded = getattr(element, "expandedName", None)
            if expanded:
                byName.setdefault(expanded, element)
        for element in elements:
            if not element.getSubstitutionGroupHeads(self):
                continue
            # Depth-first walk of the head graph from this element; a
            # path that reaches the element again is a cycle. XSD 1.1
            # allows several heads per member, so every edge is followed.
            stack = [element]
            visited: set[int] = set()
            cycle = False
            while stack:
                current = stack.pop()
                for headName in current.getSubstitutionGroupHeads(self):
                    head = byName.get(headName)
                    if head is None:
                        continue
                    if head is element:
                        cycle = True
                        break
                    if id(head) not in visited:
                        visited.add(id(head))
                        stack.append(head)
                if cycle:
                    break
            if cycle:
                self.report.add_error(
                    f"element '{element.name}' is part of a cyclic substitution group",
                    code="circular-substitution-group",
                    element=element.name,
                    phase="schema",
                )

    def _checkSubstitutionGroupExclusions(self, schemaER: Any) -> None:
        """Reports a substitution member whose type the head does not admit.

        Element Declaration Properties Correct requires a member's type
        to be validly derived from the head's type, given the head
        element's {substitution group exclusions} — the ``final``
        attribute (or the schema's ``finalDefault``). A type that is
        unrelated to the head's is ``substitution-type``; a derivation
        performed by an excluded method is ``declaration-attribute``.
        Runs after class building so the generated classes carry the
        derivation hierarchy and method.
        """
        finalDefault = schemaER.tagAttributes.get("finalDefault")
        elements = [e for e in schemaER.elements if type(e).__name__ == "Element"]
        byName: dict[str, Any] = {}
        for element in elements:
            if element.name:
                byName.setdefault(element.name, element)
            if getattr(element, "expandedName", None):
                byName.setdefault(element.expandedName, element)
        for member in elements:
            # A member with no explicit ``type`` attribute inherits the
            # head's declaration (the instance binder resolves it the
            # same way), so there is no independent declaration to
            # compare. An inline anonymous type is still checked for an
            # excluded derivation (substGrpExcl00202m2), but an unrelated
            # anonymous type is tolerated: it shares only the ur-type
            # with the head's own anonymous type, which the corpus treats
            # as admissible.
            declaredType = member.tagAttributes.get("type")
            if not declaredType:
                continue
            memberCls = self._declaredTypeClass(member)
            if memberCls is None:
                continue
            inline = "|" in declaredType
            headNames = member.getSubstitutionGroupHeads(self)
            # Enforcing derivation against a single head rejects
            # corpora pyxsd must accept (a member's type may name a base
            # the ur-type-only head relationship tolerates); only a
            # member naming several heads must be validly derived from
            # every one of them (IBM s2_2_2si02), while addB141's
            # single-head list/union mismatch stays a known false
            # accept.
            strict_heads = len(headNames) > 1
            for headName in headNames:
                head = byName.get(headName)
                if head is None or head is member:
                    continue
                final = head.tagAttributes.get("final")
                if final is None:
                    final = finalDefault
                excluded = blockTokens(final)
                headCls = self._declaredTypeClass(head)
                if headCls is None:
                    continue
                if headCls is SchemaBase:
                    # An untyped head declares the ur-type, from which
                    # every type is validly derived: a member can never
                    # violate derivation against it (subsgroup001's
                    # abstract chapContent/appendixContent heads).
                    continue
                reason = is_validly_derived(memberCls, headCls, excluded)
                if reason == "blocked":
                    self.report.add_error(
                        f"element '{member.name}' has a type whose derivation from "
                        f"substitution head '{head.name}' is excluded by final='{final}'",
                        code="declaration-attribute",
                        element=member.name,
                        phase="schema",
                    )
                elif reason == "not-derived" and not inline:
                    # pyxsd's lattice does not model the ur-types as a
                    # subclass edge, so a plain simple member under an
                    # anySimpleType head reports not-derived even though
                    # the simple ur-type roots every simple derivation.
                    # The ur-type heads are therefore exempt from both the
                    # single-head category check and the multi-head
                    # ``strict_heads`` report; only the clear category
                    # mismatches are reported (stZ048: complex under
                    # anySimpleType; stZ049: anySimpleType under a complex
                    # head; stZ050/stZ053: anyType admits every type). The
                    # general under-approximation for a single ordinary
                    # head stays (the shipped substitution fixture).
                    head_is_simple_ur = headCls is AnySimpleType
                    member_is_simple_ur = memberCls is AnySimpleType
                    member_is_complex = getattr(memberCls, "_contentKind_", None) == "complex"
                    head_admits_all = headCls is AnyType
                    head_exempt = head_admits_all or head_is_simple_ur
                    mismatch = (strict_heads and not head_exempt) or (
                        not head_admits_all
                        and (member_is_simple_ur or (head_is_simple_ur and member_is_complex))
                    )
                    if mismatch:
                        self.report.add_error(
                            f"element '{member.name}' has a type that is not validly "
                            f"derived from substitution head '{head.name}'",
                            code="substitution-type",
                            element=member.name,
                            phase="schema",
                        )

    @staticmethod
    def _declaredTypeClass(element: Any) -> Any:
        """Returns an element's generated type class, or ``None``."""
        try:
            cls = element.getType()
        except (AttributeError, TypeError):
            return None
        return cls if isinstance(cls, type) else None

    def _checkTypeReferences(self, schemaER: Any) -> None:
        """Reports a declaration type QName naming an unloaded namespace.

        The declaration walk tolerates an unresolved type so a large
        schema can still load; this pass turns a reference whose
        resolved QName names a namespace no composed schema declares
        into a schema-phase ``unknown-type``. The loaded-namespace guard
        keeps pyxsd's own resolution gaps (inside a namespace that is
        loaded) out of the report: only a reference the schema itself
        cannot satisfy is a schema error. Runs in strict namespace mode
        only, where a resolved reference has a namespace to test.

        A reference to the unnamed (no-namespace) space is not examined;
        there the plain name is the whole identity and the legacy
        tolerance is the historical behavior. (Extending this pass to
        no-namespace references was tried and reverted: pyxsd does not
        yet resolve every unprefixed reference in the XSD 1.1 feature
        families, so the extension produced 39 vacuous-pass regressions
        across CTA/override/openContent schemas for one real case,
        ``MS-Element/elemM002``, which stays a known false-accept.)
        """
        if getattr(self.mode, "namespaces", "legacy") != "strict":
            return
        loaded = self._loadedNamespaces()
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ in ("Element", "Attribute") and not (
                getattr(er, "isElementRef", False)
                or getattr(er, "isAttributeRef", False)
                or self._inConditionalInclusion(er)
            ):
                raw = er.__dict__.get("type")
                if raw is not None and "|" not in raw:
                    resolved = er.resolvedTypeName()
                    namespace = namespace_of(resolved) if isinstance(resolved, str) else None
                    if namespace is None:
                        # A reference that resolves into no namespace is
                        # normally tolerated (pyxsd does not yet resolve
                        # every unprefixed reference in the XSD 1.1
                        # feature families). Report it only when a global
                        # declaration of the same local name exists in
                        # another loaded namespace: the author meant that
                        # declaration but wrote it unqualified (addB009,
                        # xsd015.e, xsd016.e). Without an actual candidate
                        # the reference stays a known false-accept
                        # (elemM002).
                        # A component spliced in from an included or
                        # redefined (chameleon) document is exempt: its
                        # unprefixed references belong to the adopted
                        # namespace and are resolved by the chameleon pass,
                        # not re-qualified here.
                        if (
                            isinstance(resolved, str)
                            and id(er.xsdElement) not in self._composedElementIds
                        ):
                            self._reportUnqualifiedTypeCandidate(er, resolved)
                    elif namespace not in loaded or namespace == XSD_NS:
                        # The XML Schema namespace is always 'loaded' for
                        # the built-in types, so a reference into it that
                        # names no built-in is reported too (xsd015.e,
                        # xsd016.e, where a default ``xmlns`` of XSD made
                        # an unprefixed name resolve into XSD).
                        try:
                            cls = er.getType()
                        except Exception:  # pragma: no cover - defensive
                            cls = None
                        if cls is None:
                            self.report.add_error(
                                f"the type '{resolved}' of "
                                f"{type(er).__name__.lower()} '{er.name}' is not "
                                "declared by any loaded schema",
                                code="unknown-type",
                                element=er.name,
                                phase="schema",
                            )
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _reportUnqualifiedTypeCandidate(self, er: Any, resolved: str) -> None:
        """Reports an unqualified type reference that likely names a candidate.

        A reference that resolves into no namespace is reported only when a
        global type of the same local name is declared in a loaded, non-
        reserved namespace and no no-namespace type of that name exists.
        This keeps the historical tolerance for a genuinely unknown
        unprefixed reference (see ``_checkTypeReferences``) while catching
        the real error of writing a type declared in the target namespace
        without its prefix.
        """
        local = _qnameLocal(resolved)
        if not local:
            return
        loaded = self._loadedNamespaces()
        reserved = {XSD_NS, XSI_NS, XML_NS, XLINK_NS}
        candidate: str | None = None
        has_no_namespace = False
        for entries in self.components.values():
            for entry in entries:
                if componentKind(entry) != "type" or getattr(entry, "name", None) != local:
                    continue
                namespace = entry.getNamespace()
                if namespace is None:
                    has_no_namespace = True
                elif namespace not in reserved and namespace in loaded:
                    candidate = namespace
        if has_no_namespace or candidate is None:
            return
        self.report.add_error(
            f"the type '{resolved}' of {type(er).__name__.lower()} '{er.name}' is "
            f"not declared in no namespace; a type named '{local}' is declared in "
            f"namespace '{candidate}' and must be referenced with its prefix",
            code="unknown-type",
            element=er.name,
            phase="schema",
        )

    def _checkSimpleContentRestrictionBase(self, schemaER: Any) -> None:
        """Reports a simpleContent restriction with an illegal base.

        XSD 1.1 (bug 14559, Part 2 §2.4.2.1): the base of a simpleContent
        ``restriction`` must be a complex type whose simple content is not
        ``xs:anySimpleType``. A direct simple-type base (stZ009) and a
        complex base whose content primitive is anySimpleType (stZ007,
        stZ010, stZ047, stZ055) are both invalid; a simpleContent
        ``extension`` of anySimpleType stays valid, and a restriction
        that supplies its own inline ``simpleType`` gives the content a
        non-anySimpleType primitive, so it is legal (IBM s3_12v04).
        """
        from pyxsd.xsd_data_types import AnySimpleType

        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "ComplexType":
                restriction = self._simpleContentRestriction(er)
                if restriction is not None and not getattr(restriction, "hasNoBase", False):
                    base_cls = self._baseTypeClass(er)
                    if base_cls is not None:
                        has_inline = any(
                            grandchild is not None and type(grandchild).__name__ == "SimpleType"
                            for grandchild in getattr(restriction, "processedChildren", ()) or ()
                        )
                        if getattr(base_cls, "_contentKind_", None) != "complex":
                            self.report.add_error(
                                f"the base of the simpleContent restriction of type "
                                f"'{er.name}' is not a complex type",
                                code="invalid-base",
                                element=er.name,
                                phase="schema",
                            )
                        elif issubclass(base_cls, AnySimpleType) and not has_inline:
                            self.report.add_error(
                                f"the simpleContent restriction of type '{er.name}' "
                                "derives from a complex type whose simple content is "
                                "xs:anySimpleType",
                                code="invalid-base",
                                element=er.name,
                                phase="schema",
                            )
                        elif (
                            getattr(base_cls, "_simpleContentType_", None) is None
                            and not has_inline
                        ):
                            # The base is a complex type, but its content is
                            # element-only (or mixed), not simple: a
                            # simpleContent restriction cannot derive from it
                            # (xsd020.e).
                            self.report.add_error(
                                f"the base of the simpleContent restriction of type "
                                f"'{er.name}' has element-only content, not simple "
                                "content",
                                code="invalid-base",
                                element=er.name,
                                phase="schema",
                            )
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _checkContentKindDerivation(self, schemaER: Any) -> None:
        """Reports a content kind derived from an inadmissible base kind.

        ``complexContent`` derives from a complex type (or ``xs:anyType``):
        a ``simpleType`` or built-in simple type base is invalid
        (ctJ002/ctJ003). ``simpleContent`` ``extension`` derives from a
        simple type or a complex type whose content is simple; an
        element-only/mixed complex base (ctE003) or ``xs:anyType``
        (ctE004) is invalid. A base that cannot be resolved is left to
        the unknown-type check. ``simpleContent`` ``restriction`` has its
        own rule (``_checkSimpleContentRestrictionBase``).
        """
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "ComplexType":
                self._checkOneContentKindDerivation(er)
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _checkOneContentKindDerivation(self, er: Any) -> None:
        """Applies the base-kind rule to one complex type."""
        if er._firstProcessedChild(er, "SimpleContent") is not None:
            if er.getDerivation() != "extension":
                return
            if self._baseIsAnyType(er):
                self.report.add_error(
                    f"the base of the simpleContent extension of type "
                    f"'{er.name}' is xs:anyType, whose content is not simple",
                    code="invalid-base",
                    element=er.name,
                    phase="schema",
                )
                return
            base_er = self._baseTypeER(er)
            if base_er is not None and type(base_er).__name__ == "ComplexType":
                variety = base_er._effectiveContentVariety()
                if variety in ("element-only", "mixed"):
                    self.report.add_error(
                        f"the base of the simpleContent extension of type "
                        f"'{er.name}' has {variety} content, not simple content",
                        code="invalid-base",
                        element=er.name,
                        phase="schema",
                    )
            return
        if er._firstProcessedChild(er, "ComplexContent") is None:
            return
        # complexContent: the base must be a complex type (or anyType).
        if self._baseIsAnyType(er):
            return
        derivation = er.getDerivation()
        if derivation is None:
            return
        base_er = self._baseTypeER(er)
        if base_er is not None:
            if type(base_er).__name__ != "ComplexType":
                self.report.add_error(
                    f"the base of the complexContent {derivation} of type "
                    f"'{er.name}' is a simple type, not a complex type",
                    code="invalid-base",
                    element=er.name,
                    phase="schema",
                )
            return
        # No ER: a built-in simple type (or an unresolved reference, which
        # the unknown-type check already reports).
        if self._baseTypeClass(er) is not None:
            self.report.add_error(
                f"the base of the complexContent {derivation} of type "
                f"'{er.name}' is a simple type, not a complex type",
                code="invalid-base",
                element=er.name,
                phase="schema",
            )

    @staticmethod
    def _simpleContentRestriction(er: Any) -> Any | None:
        """Returns a complex type's ``simpleContent``/``restriction`` child."""
        for child in er.processedChildren or ():
            if child is not None and type(child).__name__ == "SimpleContent":
                for grandchild in child.processedChildren or ():
                    if grandchild is not None and type(grandchild).__name__ == "Restriction":
                        return grandchild
        return None

    def _isNotationReference(self, er: Any, raw: Any) -> bool:
        """Whether a type reference names the built-in ``xs:NOTATION``."""
        if not raw or (isinstance(raw, str) and "|" in raw):
            return False
        resolved = er.resolveSchemaQName(raw, parser=self)
        if namespace_of(resolved) == XSD_NS and local_name(resolved) == "NOTATION":
            return True
        return raw in ("xs:NOTATION", "xsd:NOTATION")

    def _checkNotationUses(self, schemaER: Any) -> None:
        """Reports a direct use of ``xs:NOTATION`` without an enumeration.

        XSD Schema Component Constraint: "It is an error for NOTATION to
        be used directly in a schema. Only datatypes that are derived
        from NOTATION by specifying a value for enumeration can be used
        in a schema." A bare ``xs:NOTATION`` element/attribute type or
        list ``itemType`` is therefore invalid (Saxon simple090-simple092);
        a restriction that supplies the enumeration is checked separately
        by ``XsdType._checkNotationRestriction``.

        A ``union`` member is deliberately *not* reported here: the
        corpus is self-contradictory there (MS particlesZ007 expects
        ``union memberTypes="xsd:NOTATION"`` valid while Saxon simple093
        expects it invalid), an open question (w3c/xsdtests#12), so the
        historical acceptance is kept.
        """
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            kind = type(er).__name__
            uses: list[tuple[Any, str]] = []
            if kind in ("Element", "Attribute") and not (
                getattr(er, "isElementRef", False) or getattr(er, "isAttributeRef", False)
            ):
                uses.append((er.__dict__.get("type"), f"{kind.lower()} '{er.name}'"))
            elif kind == "List":
                uses.append((getattr(er, "itemType", None), f"list '{er.getContainingTypeName()}'"))
            for raw, owner in uses:
                if self._isNotationReference(er, raw):
                    self.report.add_error(
                        f"{owner} uses xs:NOTATION directly; a NOTATION type must "
                        "include an enumeration facet",
                        code="notation-enumeration-required",
                        element=er.name,
                        phase="schema",
                    )
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _loadedNamespaces(self) -> set:
        """The namespaces a schema reference may resolve into.

        Every composed target namespace and satisfied import, plus the
        namespaces XSD defines itself (XML Schema, XSI, ``xml`` and
        XLink) and the unnamed space.
        """
        loaded: set = set(self._composedTargetNamespaces)
        loaded.update(self._resolvedImports)
        loaded.update({XSD_NS, XSI_NS, XML_NS, XLINK_NS, None})
        return loaded

    def _checkAlternatives(self, schemaER: Any) -> None:
        """Resolves and checks every element's XSD 1.1 type alternatives.

        Runs after the generated classes are built, so each alternative's
        type reference resolves to the same class the instance phase will
        see.  The heavy lifting (resolving each type and checking it is
        validly derived from the declared type) lives in
        :func:`pyxsd.alternatives.check_element_alternatives`.
        """
        from pyxsd.alternatives import check_element_alternatives

        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ == "Element":
                check_element_alternatives(er)
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _checkFormDefaults(self, schemaRoot: Any) -> None:
        """Validates ``elementFormDefault``/``attributeFormDefault`` values.

        ``form`` is an enumeration of ``qualified`` and ``unqualified``
        (XSD 1.1 §3.2.1/§3.3.1). The value is whitespace-collapsed and
        case-sensitive, so the empty string, ``Qualified``,
        ``Unqualified`` and ``qualified unqualified`` are all schema
        errors (MS elemH003-elemH006).
        """
        for attr in ("elementFormDefault", "attributeFormDefault"):
            value = schemaRoot.get(attr)
            if value is None:
                continue
            if value.strip() not in ("qualified", "unqualified"):
                self.report.add_error(
                    f"the schema declaration's '{attr}' value {value!r} is not "
                    "'qualified' or 'unqualified'",
                    code="declaration-attribute",
                    phase="schema",
                )

    def _applyLocalTargetNamespaces(self, schemaRoot: Any) -> None:
        """Applies and validates XSD 1.1 ``targetNamespace`` on locals.

        XSD 1.1 §3.2.3/§3.3.3 let a local element or attribute
        declaration state its own target namespace. The value names the
        declaration's expanded name, so it is recorded in
        ``_namespaceOverrides`` (recovering TargetNS target001/003 and
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
                            self._namespaceOverrides[id(child)] = value
                        else:
                            self.report.add_error(
                                error, code="declaration-attribute", phase="schema"
                            )
                walk(
                    child,
                    childDerivation,
                    childHasComplexType,
                    childAnyTypeRestriction,
                )

        walk(schemaRoot, None, False, False)

    def _checkValueConstraints(self, schemaER: Any) -> None:
        """Validates element/attribute ``default``/``fixed`` values.

        Runs after generated classes are built so a value is checked
        against the declaration's actual type: a user-defined simpleType
        is validated through its constructor (built-in lexical space plus
        every restricting facet), and a complex type with simple content
        delegates to its content type. Built-in types take the same path.
        A declaration whose type has no simple value space (element-only
        complex content, or an unresolved type) is skipped.
        """
        seen: set[int] = set()
        stack = [schemaER]
        while stack:
            er = stack.pop()
            if er is None or id(er) in seen:
                continue
            seen.add(id(er))
            if type(er).__name__ in ("Element", "Attribute"):
                self._checkDeclarationValueConstraint(er)
            stack.extend(getattr(er, "processedChildren", None) or ())

    def _checkDeclarationValueConstraint(self, er: Any) -> None:
        if type(er).__name__ == "Element" and any(
            attr in er.tagAttributes for attr in ("default", "fixed")
        ):
            try:
                declared = er.getType()
            except (AttributeError, TypeError):
                declared = None
            if (
                isinstance(declared, type)
                and getattr(declared, "_contentKind_", None) == "complex"
                and getattr(declared, "_elementOnly_", False)
            ):
                # e-props-correct: an element whose type has element-only
                # content cannot carry a value constraint (there is no
                # character-content value space to default against).
                self.report.add_error(
                    f"element '{er.name}' has a default or fixed value but its "
                    "type has element-only content",
                    code="declaration-attribute",
                    element=er.name,
                    phase="schema",
                )
                return
        factory = self._valueConstraintFactory(er)
        if factory is None:
            return
        label = type(er).__name__.lower()
        for attr in ("default", "fixed"):
            value = er.tagAttributes.get(attr)
            if value is None:
                continue
            try:
                factory(value)
            except (TypeError, ValueError):
                self.report.add_error(
                    f"{label} '{er.name}' {attr} value '{value}' is not valid for its type",
                    code="declaration-attribute",
                    element=er.name,
                    phase="schema",
                )
            except Exception:  # pragma: no cover - defensive
                # A constructor raising anything else is a pyxsd bug, not
                # a schema error; do not turn it into a false rejection.
                logger.debug("could not validate %s value %r of %r", attr, value, er.name)

    @staticmethod
    def _valueConstraintFactory(er: Any) -> Any:
        """Returns the class that validates a declaration's lexical value.

        ``None`` when the declaration's type has no simple value space,
        which leaves element-only complex content and unresolved types
        unchecked.
        """
        try:
            cls = er.getType()
        except (AttributeError, TypeError):
            return None
        if not isinstance(cls, type):
            return None
        content = getattr(cls, "_simpleContentType_", None)
        if isinstance(content, type):
            cls = content
        if not issubclass(cls, XsdDataType):
            return None
        return cls

    def _schemaCompositionContext(self) -> tuple[Path, set[str]]:
        """Returns the (baseDir, visited) context for schema composition.

        ``baseDir`` is the directory relative to which include/import
        locations resolve; ``visited`` starts with the main schema file
        itself so include cycles are detected.
        """
        if isinstance(self.xsdFile, (str, os.PathLike)):
            mainPath = Path(self.xsdFile).resolve()
            return mainPath.parent, {str(mainPath)}
        return Path.cwd(), set()

    def _collectAdditionalSchemas(self) -> list[tuple[str | None, Path]]:
        """Returns additional ``(namespace, path)`` schemas to load.

        Sources are the explicit ``namespace_schemas`` mapping and, in
        strict mode, any extra pairs in the instance's
        ``xsi:schemaLocation`` beyond the main schema.
        """
        additions: list[tuple[str | None, Path]] = []
        seen: set[str] = set()
        if isinstance(self.xsdFile, (str, os.PathLike)):
            seen.add(str(Path(self.xsdFile).resolve()))
        for namespace, location in self.namespaceSchemas.items():
            path = Path(location)
            if not path.is_absolute():
                path = self.xmlPath / path
            path = path.resolve()
            if str(path) in seen:
                continue
            seen.add(str(path))
            additions.append((namespace or None, path))
        if getattr(self.mode, "namespaces", "legacy") == "strict":
            for pair_namespace, location in self.getSchemaLocationPairs():
                path = Path(location)
                if not path.is_absolute():
                    path = self.xmlPath / path
                path = path.resolve()
                if str(path) in seen:
                    continue
                seen.add(str(path))
                # Remember the hint-derived additions: a hint that
                # cannot be loaded downgrades to a ``schema-hint``
                # warning (see ``_spliceAdditionalSchemas``).
                self._hintSchemaPaths.add(str(path))
                additions.append((pair_namespace, path))
        return additions

    def _spliceAdditionalSchemas(self, schemaRoot: Any, baseDir: Path, visited: set[str]) -> None:
        """Splices schemas supplied outside the main document.

        These are ``namespace_schemas`` entries and extra
        ``xsi:schemaLocation`` pairs; each is loaded like an import so
        its components keep their own target namespace. The two sources
        differ in authority: a ``namespace_schemas`` entry is an
        explicit caller input whose failure stays an ``import-unresolved``
        error, while an instance ``xsi:schemaLocation`` pair is an
        advisory hint whose failure downgrades to a ``schema-hint``
        warning. Document-level ``xs:import``/``xs:include`` failures
        keep their own separate severity through ``_spliceComposedSchemas``.
        """
        for namespace, path in getattr(self, "_additionalSchemas", []):
            tag = ET.Element(clark(XSD_NS, "import"), {"schemaLocation": str(path)})
            if namespace:
                tag.set("namespace", namespace)
            is_hint = str(path) in self._hintSchemaPaths
            self._spliceIncludedSchema(
                tag,
                schemaRoot,
                baseDir,
                visited,
                isImport=True,
                missing_severity="warning" if is_hint else "error",
                missing_code="schema-hint" if is_hint else None,
            )

    def _spliceComposedSchemas(
        self,
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
                self._noteDirectiveId(child)
            if local == "include":
                schemaRoot.remove(child)
                self._spliceIncludedSchema(child, schemaRoot, baseDir, visited, isImport=False)
            elif local == "redefine":
                schemaRoot.remove(child)
                self._spliceRedefine(child, schemaRoot, baseDir, visited)
            elif local == "override":
                schemaRoot.remove(child)
                self._spliceOverride(child, schemaRoot, baseDir, visited)
            elif local == "import":
                schemaRoot.remove(child)
                if child.get("namespace") == XSD_NS:
                    # Importing the schema-for-schemas namespace is the
                    # conventional spelling; the built-in types are
                    # always available here.
                    continue
                self._spliceIncludedSchema(
                    child, schemaRoot, baseDir, visited, isImport=True, checkImportNamespace=True
                )
        return None

    def _noteDirectiveId(self, tag: Any) -> None:
        """Records a main-document composition directive's ``id``.

        A directive's id is an ``xs:ID`` too; because the directive is
        removed before the ER walk, declaration ids are compared against
        this set instead. A directive that repeats an earlier id is
        itself reported here.
        """
        value = tag.get("id")
        if not value:
            return
        if value in self._directiveIds:
            self.report.add_error(
                f"duplicate id '{value}' on <{tag.tag.split('}')[-1]}>",
                code="declaration-duplicate",
                phase="schema",
            )
            return
        self._directiveIds[value] = tag

    def _spliceIncludedSchema(
        self,
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
        self._checkDirectiveAnnotation(tag, isImport)
        location = tag.get("schemaLocation")
        strict = getattr(self.mode, "namespaces", "legacy") == "strict"
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
                    otherTargets = self._composedTargetNamespaces - {
                        schemaRoot.get("targetNamespace")
                    }
                    supplied = (
                        namespace in self.namespaceSchemas
                        or namespace in self._resolvedImports
                        or namespace in otherTargets
                        or namespace
                        in {ns for ns, _ in getattr(self, "_additionalSchemas", []) if ns}
                    )
                    if not supplied:
                        self._reportUnresolvedImport(namespace, schemaRoot)
                logger.debug("namespace-only xs:import with no schemaLocation; skipping")
                return None
            self.report.add_error(
                "an include tag has no schemaLocation; the schema could not be composed",
                code="schema-compose",
            )
            return None
        includedPath = (baseDir / location).resolve()
        key = str(includedPath)
        if key in self._composedDocuments:
            logger.debug("the schema '%s' is already composed; skipping", location)
            return None
        if key in visited:
            # A legal include cycle: the document is already being
            # composed, so this repetition is skipped rather than
            # treated as a fatal error.
            self.report.add_warning(
                f"the schema '{location}' is already being composed; "
                "the circular include is skipped",
                code="compose-cycle",
            )
            return None
        includedRoot = self._parseIncludedSchema(
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
            self.report.add_error(
                f"the import '{location}' has no namespace attribute, but the "
                "importing schema also has no targetNamespace",
                code="compose-invalid",
                phase="schema",
            )
        if isImport and checkImportNamespace and mainNS and tag.get("namespace") == mainNS:
            # XSD 1.0 §4.2.3: an import's namespace must differ from the
            # importing schema's targetNamespace (attgB015).
            self.report.add_error(
                f"the import '{location}' imports the schema's own target namespace '{mainNS}'",
                code="compose-invalid",
                phase="schema",
            )
        if isImport:
            # A schema for the namespace was loaded, so a namespace-only
            # import of the same URI is satisfied rather than unresolved.
            if tag.get("namespace"):
                self._resolvedImports.add(tag.get("namespace"))
            if includedNS:
                self._resolvedImports.add(includedNS)
        if not isImport and includedNS is None and mainNS is not None:
            # Chameleon pre-processing (XSD 1.1 §4.2.3 and Appendix F.1):
            # a document with no targetNamespace that is included by a
            # namespaced schema adopts the including target namespace, and
            # its unqualified QName references are rewritten into it. The
            # attribute is set on the tree so nested chameleon includes
            # inherit the same namespace.
            includedRoot.set("targetNamespace", mainNS)
            self._applyChameleonNamespace(includedRoot, mainNS)
            includedNS = mainNS
        if includedNS:
            self._composedTargetNamespaces.add(includedNS)
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
            self.report.add_error(
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
            self.report.add_error(
                f"the imported schema '{location}' declares targetNamespace "
                f"'{includedNS or 'none'}', which does not match the import's "
                f"namespace '{tag.get('namespace')}'",
                code="compose-invalid",
                phase="schema",
            )
        if not isImport and includedNS not in (None, mainNS):
            self.report.add_error(
                f"the schema '{location}' declares targetNamespace "
                f"'{includedNS}', which does not match the including "
                f"schema's namespace ({mainNS or 'none'})",
                code="compose-namespace",
            )
        if (
            isImport
            and mainNS
            and includedNS != mainNS
            and getattr(self.mode, "namespaces", "legacy") != "strict"
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
        self._composeStack.append(key)
        try:
            self._spliceComposedSchemas(
                includedRoot, includedPath.parent, visited | {str(includedPath)}
            )
        finally:
            self._composeStack.pop()
        # Record provenance before the components are appended to the
        # main root: ``id`` uniqueness is scoped to a schema document.
        for element in includedRoot.iter():
            self._composedElementIds.add(id(element))
            self._composedSchemaRoots.setdefault(id(element), includedRoot)
        self._appendNamedComponents(includedRoot, schemaRoot, componentNamespace)
        self._composedDocuments.add(key)
        return None

    def _parseIncludedSchema(
        self,
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
                root = parse_with_namespaces(includedFile, self.namespaceContext)
        except OSError as e:
            message = f"the schema '{location}' could not be opened: {e}"
            code = missing_code if missing_code is not None else error_code
            if missing_severity == "warning":
                self.report.add_warning(message, code=code, phase="schema")
            else:
                self.report.add_error(message, code=code, phase="schema")
            return None
        except ET.ParseError as e:
            self.report.add_error(
                f"the schema '{location}' is not well-formed XML: {e}",
                code=error_code,
                phase="schema",
            )
            return None
        apply_conditional_inclusion(root, self.namespaceContext, self.report)
        if root.tag != clark(XSD_NS, "schema"):
            # schB5/schE6/schE10: the reference resolves to well-formed
            # XML that is not an XML Schema document.
            self.report.add_error(
                f"the schema '{location}' is not an XML Schema document "
                f"(root element {root.tag!r})",
                code="schema-compose",
                phase="schema",
            )
            return None
        self._checkNamespaceAttributeValues(root)
        return root

    def _checkNamespaceAttributeValues(self, root: Any) -> None:
        """Reports an empty ``targetNamespace`` or import ``namespace``.

        The empty string is not a valid namespace name: a schema declaring
        ``targetNamespace=""`` (schZ014_b) and an ``xs:import`` carrying
        ``namespace=""`` (schZ014_a) are both invalid. Absence is written
        by omitting the attribute, never by an empty value.
        """
        if root.get("targetNamespace") == "":
            self.report.add_error(
                "the schema's targetNamespace must not be the empty string; "
                "omit the attribute for no namespace",
                code="declaration-attribute",
                phase="schema",
            )
        for child in list(root):
            if not isinstance(child.tag, str) or child.tag.split("}")[-1] != "import":
                continue
            if child.get("namespace") == "":
                self.report.add_error(
                    "an import's namespace must not be the empty string; "
                    "omit the attribute to import the absent namespace",
                    code="declaration-attribute",
                    phase="schema",
                )

    def _checkDirectiveAnnotation(self, tag: Any, isImport: bool) -> None:
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
            self.report.add_error(
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
            self.report.add_error(
                f"<{localChild}> is not allowed inside <{local}>",
                code="declaration-child",
                phase="schema",
            )

    def _referencedNamespaces(self, schemaRoot: Any) -> set[str]:
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
            tokens = [element.get(attribute) for attribute in self._CHAMELEON_QNAME_ATTRIBUTES]
            for attribute in self._CHAMELEON_QNAME_LIST_ATTRIBUTES:
                value = element.get(attribute)
                if value:
                    tokens.extend(value.split())
            for token in tokens:
                if not token or token.startswith("##"):
                    continue
                try:
                    resolved = self.namespaceContext.resolve(element, token)
                except NamespaceError:
                    continue
                uri = namespace_of(resolved)
                if uri is not None:
                    namespaces.add(uri)
        return namespaces

    def _reportUnresolvedImport(self, namespace: str, schemaRoot: Any) -> None:
        """Reports one unresolved namespace-only ``xs:import``.

        A namespace-only import is a hint: if a component from its
        namespace is referenced by the importing document and nothing
        satisfied the namespace, the schema is invalid; otherwise it is
        only a warning. The reference scan is scoped to the document
        that declared the import, because an import is only needed for
        references made by its own schema document.
        """
        if namespace in self._referencedNamespaces(schemaRoot):
            self.report.add_error(
                f"the import for namespace '{namespace}' has no "
                "schemaLocation and a component from that namespace "
                "is referenced",
                code="import-unresolved",
                phase="schema",
            )
        else:
            self.report.add_warning(
                f"the import for namespace '{namespace}' has no "
                "schemaLocation and no schema was supplied for it",
                code="import-unresolved",
                phase="schema",
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

    def _applyChameleonNamespace(self, includedRoot: Any, namespace: str) -> None:
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
            for attribute in self._CHAMELEON_QNAME_ATTRIBUTES:
                value = element.get(attribute)
                if value is None:
                    continue
                rewritten = self._chameleonQName(element, value, namespace)
                if rewritten != value:
                    element.set(attribute, rewritten)
            for attribute in self._CHAMELEON_QNAME_LIST_ATTRIBUTES:
                value = element.get(attribute)
                if value is None:
                    continue
                tokens = [
                    self._chameleonQName(element, token, namespace) for token in value.split()
                ]
                element.set(attribute, " ".join(tokens))

    def _chameleonQName(self, element: Any, token: str, namespace: str) -> str:
        """Rewrites one lexical QName with an absent namespace, else keeps it."""
        if not token or token.startswith(("##", "{")):
            return token
        try:
            resolved = self.namespaceContext.resolve(element, token)
        except NamespaceError:
            return token
        if namespace_of(resolved) is None:
            return clark(namespace, local_name(resolved))
        return token

    def _appendNamedComponents(
        self, includedRoot: Any, schemaRoot: Any, namespace: str | None
    ) -> None:
        """Appends the named components of an included schema to the main tree.

        In strict mode the namespace each component was declared in is
        recorded so its expanded name reflects the source document
        rather than the main schema's target namespace. That document's
        form defaults are recorded too, so local declarations resolve
        qualification against their own schema instead of the host.
        """
        strict = getattr(self.mode, "namespaces", "legacy") == "strict"
        sourceDefaults = (
            includedRoot.get("elementFormDefault"),
            includedRoot.get("attributeFormDefault"),
        )
        sourceXPathDefault = includedRoot.get("xpathDefaultNamespace")
        self._checkFormDefaults(includedRoot)
        for component in list(includedRoot):
            if component.tag.split("}")[-1] in _COMPOSABLE_TAGS:
                if strict:
                    for element in component.iter():
                        self._namespaceOverrides.setdefault(id(element), namespace)
                        self._formDefaults.setdefault(id(element), sourceDefaults)
                        if sourceXPathDefault is not None:
                            self._xpathDefaultNamespaces.setdefault(id(element), sourceXPathDefault)
                schemaRoot.append(component)
        return None

    def _spliceRedefine(
        self,
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
        self._checkRedefineStructure(redefineTag)
        location = redefineTag.get("schemaLocation")
        if not location:
            self.report.add_error(
                "a redefine tag has no schemaLocation; the schema could not be composed",
                code="schema-compose",
            )
            return None
        includedRoot = self._parseIncludedSchema(location, baseDir, missing_severity="warning")
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
                self.report.add_error(
                    f"the base schema '{location}' for the redefinition could not be opened",
                    code="schema-compose",
                    phase="schema",
                )
            return None
        mainNS = schemaRoot.get("targetNamespace")
        includedNS = includedRoot.get("targetNamespace")
        if includedNS:
            self._composedTargetNamespaces.add(includedNS)
        includedPath = (baseDir / location).resolve()
        if includedNS is not None and mainNS is not None and includedNS != mainNS:
            # A redefine may target a document in the redefining schema's
            # namespace or a no-namespace (chameleon) document; redefining
            # a third namespace would introduce the renamed components
            # into the wrong namespace. A no-namespace redefiner is the
            # disputed circular case (W3C schU1), left unresolved.
            self.report.add_error(
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
                self.report.add_warning(
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
                self.report.add_error(
                    f"the schema '{location}' is already being composed; "
                    "a cyclic redefine is not allowed",
                    code="compose-invalid",
                    phase="schema",
                )
            return None
        redefined: list[tuple[str, str]] = []
        for child in list(redefineTag):
            local = child.tag.split("}")[-1]
            if local in ("complexType", "simpleType", "group", "attributeGroup") and child.get(
                "name"
            ):
                redefined.append((local, child.get("name")))
        self._checkRedefineTargets(includedRoot, location, redefined)
        self._checkRedefineDuplicates(includedPath, location, redefined)
        self._checkRedefineRestrictions(includedRoot, location, redefineTag)
        redefinedNames = {name for _, name in redefined}
        for component in list(includedRoot):
            local = component.tag.split("}")[-1]
            name = component.get("name")
            if (
                local in ("complexType", "simpleType", "group", "attributeGroup")
                and name in redefinedNames
            ):
                component.set("name", f"{name}|base")
        self._composeStack.append(str(includedPath))
        try:
            self._spliceComposedSchemas(
                includedRoot, includedPath.parent, visited | {str(includedPath)}
            )
        finally:
            self._composeStack.pop()
        # The redefined document's components come from another schema
        # document; scope ``id`` uniqueness provenance to it.
        for element in includedRoot.iter():
            self._composedElementIds.add(id(element))
            self._composedSchemaRoots.setdefault(id(element), includedRoot)
        self._appendNamedComponents(includedRoot, schemaRoot, mainNS)
        self._rebindRedefineReferences(redefineTag, redefinedNames, includedNS, mainNS)
        for child in list(redefineTag):
            schemaRoot.append(child)
        return None

    def _spliceOverride(
        self,
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
        targets = self._collectOverrideTargets(overrideTag)
        self._checkOverrideTargetDuplicates(targets)
        location = overrideTag.get("schemaLocation")
        if not location:
            self.report.add_error(
                "an override tag has no schemaLocation; the schema could not be composed",
                code="schema-compose",
            )
            return None
        includedRoot = self._parseIncludedSchema(location, baseDir, missing_severity="warning")
        if includedRoot is None:
            # An override with content needs its base document: without it
            # the target set cannot be established. An override carrying
            # only annotations is just the missing-resource warning.
            hasContent = any(
                isinstance(child.tag, str) and child.tag.split("}")[-1] != "annotation"
                for child in list(overrideTag)
            )
            if hasContent:
                self.report.add_error(
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
            self.report.add_error(
                f"the overridden schema '{location}' declares targetNamespace "
                f"'{includedNS}', which does not match the overriding schema's "
                f"namespace ({mainNS or 'none'})",
                code="compose-invalid",
                phase="schema",
            )
        if str(includedPath) in visited:
            self.report.add_warning(
                f"the schema '{location}' is already being composed; "
                "the circular override is skipped",
                code="compose-cycle",
            )
            return None
        if includedNS is None and mainNS is not None:
            # Chameleon pre-processing (Appendix F.2 on top of F.1): a
            # no-namespace base is ported into the overrider's namespace
            # before the override is applied.
            includedRoot.set("targetNamespace", mainNS)
            self._applyChameleonNamespace(includedRoot, mainNS)
            includedNS = mainNS
        if includedNS:
            self._composedTargetNamespaces.add(includedNS)
        self._checkOverrideDuplicates(includedPath, location, targets)
        # Compose the base document first so its effective component set
        # (its own includes/overrides included) is the override's target.
        self._composeStack.append(str(includedPath))
        try:
            self._spliceComposedSchemas(
                includedRoot, includedPath.parent, visited | {str(includedPath)}
            )
        finally:
            self._composeStack.pop()
        targetKeys = {(space, name) for space, name, _ in targets}
        present: set[tuple[str, str]] = set()
        for component in list(includedRoot):
            if not isinstance(component.tag, str):
                continue
            local = component.tag.split("}")[-1]
            space = self._OVERRIDE_SYMBOL_SPACES.get(local)
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
            self._composedElementIds.add(id(element))
            self._composedSchemaRoots.setdefault(id(element), includedRoot)
        self._appendNamedComponents(
            includedRoot, schemaRoot, includedNS if includedNS is not None else mainNS
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
                    self._composedSchemaRoots.setdefault(id(element), includedRoot)
                schemaRoot.append(child)
        # The base document has been composed; a later include/import of
        # the same document must not splice it a second time (XSD
        # composition treats one document once, §4.2.3). ``xs:override``
        # itself does not consult this set, so two overrides of the same
        # base are still reprocessed and reported as a conflict.
        self._composedDocuments.add(str(includedPath))
        return None

    def _collectOverrideTargets(self, overrideTag: Any) -> list[tuple[str, str, Any]]:
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
            space = self._OVERRIDE_SYMBOL_SPACES.get(local)
            if space is None:
                self.report.add_error(
                    f"<override> does not allow a '{local}' child",
                    code="override-invalid",
                    phase="schema",
                )
                continue
            name = child.get("name")
            if not name:
                self.report.add_error(
                    f"the <override> child '{local}' must name a schema component",
                    code="override-invalid",
                    phase="schema",
                )
                continue
            targets.append((space, name, child))
        return targets

    def _checkOverrideTargetDuplicates(self, targets: list[tuple[str, str, Any]]) -> None:
        """Reports one component named twice inside a single override block."""
        seen: set[tuple[str, str]] = set()
        for space, name, _ in targets:
            key = (space, name)
            if key in seen:
                self.report.add_error(
                    f"the component '{name}' is declared more than once in <override>",
                    code="override-invalid",
                    phase="schema",
                )
            seen.add(key)

    def _checkOverrideDuplicates(
        self, includedPath: Path, location: str, targets: list[tuple[str, str, Any]]
    ) -> None:
        """Reports a base component overridden twice from unrelated ancestries.

        An override chain (the outer block targets the inner redefining
        document) has a different base key and is not a conflict; the same
        base component reached twice from the same or unrelated ancestry
        is (over022).
        """
        current = tuple(self._composeStack)
        seen: list[tuple[str, str, str]] = []
        for space, name, _ in targets:
            key = (str(includedPath), space, name)
            origin = self._overrideOrigins.get(key)
            if origin is None:
                seen.append(key)
                continue
            nested = origin != current and (
                _stackPrefix(origin, current) or _stackPrefix(current, origin)
            )
            if nested:
                continue
            self.report.add_error(
                f"the component '{name}' of the overridden schema "
                f"'{location}' is overridden more than once",
                code="override-invalid",
                phase="schema",
            )
        for key in seen:
            self._overrideOrigins[key] = current

    def _checkRedefineTargets(
        self, includedRoot: Any, location: str, redefined: list[tuple[str, str]]
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
        declared = self._declaredComponentKinds(includedRoot)
        for kind, name in redefined:
            if (kind, name) not in declared:
                self.report.add_error(
                    f"the component '{name}' in <redefine> is not defined "
                    f"in the redefined schema '{location}'",
                    code="compose-invalid",
                    phase="schema",
                )

    _COMPOSABLE_REDEFINE_KINDS = ("complexType", "simpleType", "group", "attributeGroup")

    #: The XSD symbol spaces an ``xs:override`` may replace, keyed by the
    #: declaration's element name. ``simpleType`` and ``complexType``
    #: share the ``type`` space, so an override may replace a complex
    #: type with a simple type of the same name (over013).
    _OVERRIDE_SYMBOL_SPACES: ClassVar[dict[str, str]] = {
        "simpleType": "type",
        "complexType": "type",
        "element": "element",
        "attribute": "attribute",
        "group": "group",
        "attributeGroup": "attributeGroup",
        "notation": "notation",
    }

    def _declaredComponentKinds(self, root: Any) -> set[tuple[str, str]]:
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
            if local in self._COMPOSABLE_REDEFINE_KINDS and child.get("name"):
                kinds.add((local, child.get("name")))
            elif local == "redefine":
                for grandchild in list(child):
                    if not isinstance(grandchild.tag, str):
                        continue
                    grandLocal = grandchild.tag.split("}")[-1]
                    if grandLocal in self._COMPOSABLE_REDEFINE_KINDS and grandchild.get("name"):
                        kinds.add((grandLocal, grandchild.get("name")))
        return kinds

    def _checkRedefineDuplicates(
        self, includedPath: Path, location: str, redefined: list[tuple[str, str]]
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
        current = tuple(self._composeStack)
        seen: list[tuple[str, str, str]] = []
        for kind, name in redefined:
            key = (str(includedPath), kind, name)
            origin = self._redefineOrigins.get(key)
            if origin is None:
                seen.append(key)
                continue
            if _stackPrefix(origin, current) or _stackPrefix(current, origin):
                continue
            self.report.add_error(
                f"the component '{name}' of the redefined schema "
                f"'{location}' is redefined more than once",
                code="compose-invalid",
                phase="schema",
            )
        for key in seen:
            self._redefineOrigins[key] = current

    def _checkRedefineStructure(self, redefineTag: Any) -> None:
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
            self.report.add_error(
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
                self.report.add_error(
                    f"a {local} declaration cannot be redefined; only a "
                    "complexType, simpleType, group or attributeGroup may be "
                    "redefined",
                    code="compose-invalid",
                    phase="schema",
                )
                continue
            if local in ("simpleType", "complexType") and name:
                if not self._redefineTypeDerivesFromSelf(child, name):
                    self.report.add_error(
                        f"the redefined {local} '{name}' must derive from the original '{name}'",
                        code="compose-invalid",
                        phase="schema",
                    )
            elif local == "group" and name:
                self._checkGroupRedefineSelfReference(child, name)

    def _checkGroupRedefineSelfReference(self, declaration: Any, name: str) -> None:
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
                self.report.add_error(
                    f"the self reference of the redefined group '{name}' must "
                    "have minOccurs and maxOccurs of exactly 1",
                    code="compose-invalid",
                    phase="schema",
                )

    @staticmethod
    def _redefineTypeDerivesFromSelf(declaration: Any, name: str) -> bool:
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

    def _checkRedefineRestrictions(
        self, includedRoot: Any, location: str, redefineTag: Any
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
            baseDecl = self._findBaseComponent(includedRoot, "attributeGroup", name)
            if baseDecl is None:
                continue
            baseUses = self._attributeUses(baseDecl)
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
                self.report.add_error(
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
                        self.report.add_error(
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
                    self.report.add_error(
                        f"the required attribute '{baseName}' of the redefined "
                        f"attributeGroup '{name}' is missing",
                        code="compose-invalid",
                        phase="schema",
                    )
            position = 0
            for childName, childAttr in declared:
                if childName not in basePositions:
                    self.report.add_error(
                        f"the attribute '{childName}' is not in the redefined "
                        f"attributeGroup '{name}'",
                        code="compose-invalid",
                        phase="schema",
                    )
                    continue
                nextPosition = basePositions[childName]
                if nextPosition < position:
                    self.report.add_error(
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
                    self.report.add_error(
                        f"the attribute '{childName}' of the redefined "
                        f"attributeGroup '{name}' changes its use",
                        code="compose-invalid",
                        phase="schema",
                    )
                if baseAttr.get("fixed") is not None and childAttr.get("fixed") is None:
                    self.report.add_error(
                        f"the attribute '{childName}' of the redefined "
                        f"attributeGroup '{name}' drops its fixed value",
                        code="compose-invalid",
                        phase="schema",
                    )

    def _findBaseComponent(self, root: Any, kind: str, name: str) -> Any | None:
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

    def _attributeUses(self, declaration: Any) -> list[tuple[str, Any]] | None:
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

    def _rebindRedefineReferences(
        self,
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
                    if self._qnameNamespace(element, value) is None:
                        self.report.add_error(
                            f"the redefinition self reference '{value}' "
                            "must be qualified into the redefining "
                            f"namespace '{mainNS}'",
                            code="compose-invalid",
                            phase="schema",
                        )

    def _qnameNamespace(self, element: Any, value: str) -> str | None:
        """Resolves one QName to its namespace, or ``None`` if unbound/absent."""
        try:
            resolved = self.namespaceContext.resolve(element, value)
        except NamespaceError:
            return None
        return namespace_of(resolved)

    def _checkIdentityConstraints(self, rootInstance: Any) -> None:
        """Runs the identity-constraint check over the bound tree."""
        # Imported lazily: importing pyxsd.identity before
        # element_representative (above) triggers a circular import
        # (identity -> schema_base -> element_representative -> attribute).
        from pyxsd.identity import check_identity_constraints

        check_identity_constraints(rootInstance, self.report)
        return None

    @with_schema_context
    def parseXML(self) -> Any:
        """Reads the given xml file in the context of the xsd file.

        Produces instances of the above classes. Does validation.
        Returns a schema instance object.
        """
        logger.debug("Starting to parse the xml file.")

        # Binding diagnostics from here on belong to the instance phase.
        self.report.phase = "instance"

        # XSD 1.1 attribute inheritance and conditional type assignment
        # both need to relate a bound element to its ancestors: the parent
        # links are indexed once, and the governing class of each bound
        # element is recorded as binding proceeds (schema_base).
        self._elementParents: dict[int, Any] = {}
        self._elementTypes: dict[int, Any] = {}
        stack = [self.xmlRoot]
        while stack:
            parent = stack.pop()
            for child in parent:
                self._elementParents[id(child)] = parent
                stack.append(child)

        schemaClass = self.getClasses()["schema"]

        schemaClassInstance = schemaClass()

        rootName = self.xmlRoot.tag.split("}")[-1]

        topLevelDescriptors = schemaClassInstance._getElements()

        # A schema with no global element declarations is legal (and is what
        # conditional inclusion leaves behind when it empties a document):
        # there is then no descriptor for the root to match, and the
        # ``unknown-root`` branch below reports it instead of aborting.
        if getattr(self.mode, "namespaces", "legacy") == "strict":
            matching = [
                descriptor
                for descriptor in topLevelDescriptors
                if descriptor.instanceName(parser=self) == self.xmlRoot.tag
            ]
        else:
            matching = [
                descriptor for descriptor in topLevelDescriptors if descriptor.name == rootName
            ]

        if len(matching) > 1:
            elementNames = ", ".join(element.name for element in matching)
            self.report.add_error(
                "invalid schema: there is more than one global element named "
                f"'{rootName}' ({elementNames}); parsing only '{matching[0].name}'",
                code="multiple-roots",
                phase="schema",
            )

        if not matching:
            self.report.add_error(
                f"the xml root element '{rootName}' does not correspond to any "
                "global element declaration in the schema",
                code="unknown-root",
            )
            return None

        rootElement = matching[0]
        rootElementName = rootElement.name

        subInstance = None
        if rootElementName == rootName:
            with whitespace_mode(self.mode.whitespace):
                subCls: Any = self._classForRoot(rootElement)
                if subCls is None:
                    return None
                if subCls is SchemaBase:
                    # An element declaration with no type is implicitly
                    # xs:anyType. Bind it through the ur-type class so
                    # its children go through the lax wildcard and a
                    # matching global declaration is validated (so a
                    # required attribute on the child is enforced,
                    # AU_required00101m1_n).
                    subCls = AnyType
                self.generateCorrectSchemaTags()
                contentKind = getattr(subCls, "_contentKind_", None)
                isComplex = (
                    contentKind == "complex"
                    if contentKind is not None
                    else issubclass(subCls, SchemaBase)
                )
                if isComplex:
                    nilled = xsi.xsi_nil_is_true(self.xmlRoot)
                    if xsi.xsi_nil_declared(self.xmlRoot) and not rootElement.isNillable():
                        self.report.add_error(
                            f"the root element '{rootName}' is not nillable but carries xsi:nil",
                            code="nil",
                            element=rootName,
                        )
                        nilled = False
                    if nilled:
                        # A nilled root carries no content to validate:
                        # the emptiness rule is checked, declared
                        # attributes are validated, and an empty shell
                        # is bound. The content check is reported here
                        # rather than through ``_checkNilContent``: the
                        # ur-type stand-in class has no attached parser,
                        # so its classmethod would log instead of record.
                        if rootElement.getFixed() is not None:
                            self.report.add_error(
                                f"the root element '{rootName}' is marked nil "
                                "but its declaration has a fixed value",
                                code="nil",
                                element=rootName,
                            )
                        nilContent = nil_content_kind(self.xmlRoot)
                        if nilContent == "elements":
                            self.report.add_error(
                                f"the root element '{rootName}' is marked nil "
                                "but contains child elements",
                                code="nil",
                                element=rootName,
                            )
                        elif nilContent == "characters":
                            self.report.add_error(
                                f"the root element '{rootName}' is marked nil "
                                "but contains character content",
                                code="nil",
                                element=rootName,
                            )
                        subInstance = subCls._nilledInstance(
                            subCls, self.xmlRoot, subCls._node_name(self.xmlRoot)
                        )
                        subInstance._nil_ = True
                    else:
                        forcedText = None
                        if self.xmlRoot.text is None and not list(self.xmlRoot):
                            forcedText = rootElement.getDefault()
                            if forcedText is None:
                                forcedText = rootElement.getFixed()
                        subInstance = subCls.makeInstanceFromTag(self.xmlRoot, forcedText)
                        if getattr(subCls, "_simpleContentType_", None) is not None:
                            subCls._checkFixedElement(
                                rootElement, subCls, subInstance, rootElementName
                            )
                        else:
                            subCls._checkElementValueConstraint(
                                rootElement, subCls, subInstance, rootElementName
                            )
                else:
                    # The root element's declared type is a primitive
                    # (simple) data type: build a typed instance directly.
                    # ``xs:anyType`` is the exception: its lax ``##any``
                    # content wildcard admits undeclared children, so they
                    # are bound through that wildcard instead of being
                    # rejected as simple-typed content.
                    if subCls is AnyType and list(self.xmlRoot):
                        subInstance = self._anyTypeRootInstance(subCls, rootElement, schemaClass)
                    else:
                        subInstance = self._primitiveRootInstance(subCls, rootElement)
                # xsi:type may replace the declared root type, so the root
                # instance is stored directly instead of validated against
                # the declared element type.
                schemaClassInstance.__dict__[rootElementName] = subInstance
                subInstance._descriptor_ = rootElement
                self._checkIdentityConstraints(subInstance)

        return subInstance

    def _report_undeclared_simple_attributes(self, elementTag: Any, elementName: str) -> None:
        """Rejects attributes on a simple-typed root element.

        A simple type has no attribute uses and no attribute wildcard, so
        any attribute other than the built-in schema-instance bookkeeping
        (``xsi:*``) or a namespace declaration is undeclared. The ur-type
        and primitive root paths do not run the attribute-declaration
        pass, so the check is made here (SUN typeDef01201m1/01202m1).
        """
        for attr in elementTag.attrib:
            if "xmlns" in attr:
                continue
            if xsi.is_builtin_xsi_attribute(attr):
                continue
            self.report.add_error(
                f"attribute '{xsi.xsi_attr_key(attr)}' is not declared in the "
                "schema and was not parsed",
                code="unexpected-attribute",
                element=elementName,
            )

    def _report_unknown_xsi_attributes(self, elementTag: Any, elementName: str) -> None:
        """Rejects schema-instance attributes that are not built-ins.

        The four built-in xsi attributes are handled specially; any
        other attribute in the namespace is an ordinary attribute. The
        ur-type and primitive root paths do not run attribute-declaration
        or wildcard checks, so an undeclared unknown xsi attribute is
        reported here (attMd001-011).
        """
        for attr in elementTag.attrib:
            if xsi.unknown_xsi_attribute(attr):
                self.report.add_error(
                    f"attribute '{attr}' is not declared in the schema and was not parsed",
                    code="unexpected-attribute",
                    element=elementName,
                )

    def _anyTypeRootInstance(self, dataTypeClass: Any, rootElement: Any, binder: Any) -> Any:
        """Builds the root instance for an ``xsd:anyType``-typed element.

        ``xs:anyType`` is the ur-type: its content is mixed character
        data plus a lax ``##any`` wildcard over element children, and
        every attribute is admissible. A root with child elements is
        therefore not "a simple type containing child elements";
        children are bound through the lax wildcard (a global
        declaration or a child ``xsi:type`` is honored, anything else
        is generic). The ``xsi:nil`` emptiness rules match the
        primitive root path.
        """
        rootName = (
            self.xmlRoot.tag
            if getattr(self.mode, "namespaces", "legacy") == "strict"
            else self.xmlRoot.tag.split("}")[-1]
        )
        self._report_unknown_xsi_attributes(self.xmlRoot, rootName)
        nilled = xsi.xsi_nil_is_true(self.xmlRoot)
        if xsi.xsi_nil_declared(self.xmlRoot) and not rootElement.isNillable():
            self.report.add_error(
                f"the root element '{rootName}' is not nillable but carries xsi:nil",
                code="nil",
                element=rootName,
            )
            nilled = False
        if nilled:
            nilContent = nil_content_kind(self.xmlRoot)
            if nilContent == "elements":
                self.report.add_error(
                    f"the root element '{rootName}' is marked nil but contains child elements",
                    code="nil",
                    element=rootName,
                )
            elif nilContent == "characters":
                self.report.add_error(
                    f"the root element '{rootName}' is marked nil but contains character content",
                    code="nil",
                    element=rootName,
                )

        instance = dataTypeClass._unvalidated()
        instance._name_ = rootName
        instance._attribs_ = {
            xsi.xsi_attr_key(key): val for key, val in self.xmlRoot.attrib.items()
        }
        if nilled:
            instance._value_ = None
            instance._children_ = []
            return instance
        text = self.xmlRoot.text
        instance._value_ = [text] if text else None
        instance._children_ = []
        wildcard = WildcardSpec(namespace=NAMESPACE_ANY, process_contents="lax")
        for child in self.xmlRoot:
            binder._bindAnyTypeChild(instance, child, wildcard)
        return instance

    def _anyTypeChildInstance(self, dataTypeClass: Any, elementTag: Any) -> Any:
        """Builds an instance for an ``xs:anyType``-typed child element.

        ``xs:anyType`` is mixed character data plus a lax ``##any``
        wildcard over element children, so an anyType-typed child is not a
        simple type containing child elements: its children bind through
        the wildcard (MS isDefault072, errC007).
        """
        rootName = (
            elementTag.tag
            if getattr(self.mode, "namespaces", "legacy") == "strict"
            else elementTag.tag.split("}")[-1]
        )
        instance = dataTypeClass._unvalidated()
        instance._name_ = rootName
        instance._attribs_ = {xsi.xsi_attr_key(key): val for key, val in elementTag.attrib.items()}
        text = elementTag.text
        instance._value_ = [text] if text else None
        instance._children_ = []
        wildcard = WildcardSpec(namespace=NAMESPACE_ANY, process_contents="lax")
        for child in elementTag:
            SchemaBase._bindAnyTypeChild(instance, child, wildcard)
        return instance

    def _primitiveRootInstance(self, dataTypeClass: Any, rootElement: Any) -> Any:
        """Builds a typed instance for a root element whose declared
        type is a primitive data type rather than a complex type.

        Honors ``xsi:nil`` (rejected on non-nillable elements with code
        ``nil``). The document's text is validated through the data
        type's constructor; an invalid lexical value is reported (code
        ``value``) and the value is dropped so parsing can continue.
        Empty content takes the element's ``default``/``fixed`` value,
        and a ``fixed`` value is enforced with code ``fixed-element``.
        A simple-typed element may not carry child elements.
        """
        rootName = (
            self.xmlRoot.tag
            if getattr(self.mode, "namespaces", "legacy") == "strict"
            else self.xmlRoot.tag.split("}")[-1]
        )
        self._report_unknown_xsi_attributes(self.xmlRoot, rootName)
        if dataTypeClass is not AnyType:
            # The ur-type admits any attribute; every other primitive
            # (simple) type has no attribute uses.
            self._report_undeclared_simple_attributes(self.xmlRoot, rootName)
        nilled = xsi.xsi_nil_is_true(self.xmlRoot)
        if xsi.xsi_nil_declared(self.xmlRoot) and not rootElement.isNillable():
            self.report.add_error(
                f"the root element '{rootName}' is not nillable but carries xsi:nil",
                code="nil",
                element=rootName,
            )
            nilled = False

        nilContent = nil_content_kind(self.xmlRoot) if nilled else None
        if list(self.xmlRoot):
            if nilled:
                self.report.add_error(
                    f"the root element '{rootName}' is marked nil but contains child elements",
                    code="nil",
                    element=rootName,
                )
            else:
                self.report.add_error(
                    f"the root element '{rootName}' has a simple type but contains child elements",
                    code="unexpected-element",
                    element=rootName,
                )

        if nilContent == "characters":
            self.report.add_error(
                f"the root element '{rootName}' is marked nil but contains character content",
                code="nil",
                element=rootName,
            )
        text = self.xmlRoot.text or ""
        value = None
        with qname_context(self._qname_bindings_for(self.xmlRoot)):
            if nilled and rootElement.getFixed() is not None:
                self.report.add_error(
                    f"the root element '{rootName}' is marked nil "
                    "but its declaration has a fixed value",
                    code="nil",
                    element=rootName,
                )
            if not nilled:
                forced = None
                if self.xmlRoot.text is None and not list(self.xmlRoot):
                    forced = rootElement.getDefault()
                    if forced is None:
                        forced = rootElement.getFixed()
                if forced is not None:
                    try:
                        value = dataTypeClass(forced)
                    except (TypeError, ValueError) as exc:
                        self.report.add_error(
                            f"the root element '{rootName}' has an invalid default value: {exc}",
                            code=getattr(exc, "code", "default"),
                            element=rootName,
                        )
                else:
                    try:
                        value = dataTypeClass(text)
                    except (TypeError, ValueError) as exc:
                        self.report.add_error(
                            f"the root element '{rootName}' has an invalid "
                            f"{getattr(dataTypeClass, 'name', dataTypeClass.__name__)} "
                            f"value: {exc}",
                            code=getattr(exc, "code", "value"),
                            element=rootName,
                        )
                        if self.mode.invalid_value == "raw":
                            value = AnySimpleType(text)

            if not nilled and value is not None:
                fixed = rootElement.getFixed()
                if fixed is not None:
                    try:
                        fixedValue = dataTypeClass(fixed)
                    except (TypeError, ValueError):
                        fixedValue = None
                    if fixedValue is not None and xsd_value_key(value) != xsd_value_key(fixedValue):
                        self.report.add_error(
                            f"the root element '{rootName}' has a value that conflicts "
                            f"with its fixed value {fixed!r}",
                            code="fixed-element",
                            element=rootName,
                        )

        instance = dataTypeClass._unvalidated() if value is None else value
        instance._name_ = rootName
        instance._attribs_ = {
            xsi.xsi_attr_key(key): val for key, val in self.xmlRoot.attrib.items()
        }
        if nilled:
            # A nilled element has no value: the content rule above has
            # reported any character content, which is not bound here.
            instance._value_ = None
        else:
            instance._value_ = [str(value)] if value is not None else ([text] if text else None)
        instance._children_ = []
        return instance

    def _qname_bindings_for(self, element: Any) -> dict[str, str] | None:
        """Prefix bindings in scope at ``element``, or ``None``.

        Only strict namespace mode resolves ``xs:QName`` values; legacy
        mode keeps lexical comparison (``None`` disables resolution).
        """
        if getattr(self.mode, "namespaces", "legacy") != "strict":
            return None
        return self.namespaceContext.bindings_for(element)

    def _resolveXsiTypeName(self, value: str, element: Any) -> str | None:
        """Resolves a lexical ``xsi:type`` QName against the instance scope.

        In ``legacy`` namespace mode the raw value is returned unchanged.
        In ``strict`` mode the value is expanded through the instance
        namespace context; an unbound prefix is reported as
        ``unknown-namespace-prefix`` and ``None`` is returned so the
        caller keeps the declared type.
        """
        if getattr(self.mode, "namespaces", "legacy") != "strict":
            return value
        try:
            return self.namespaceContext.resolve(element, value)
        except NamespaceError as exc:
            self.report.add_error(
                str(exc),
                code="unknown-namespace-prefix",
                element=element.tag,
            )
            return None

    def _classForRoot(self, rootElement: Any) -> type[SchemaBase] | None:
        """Resolves the class used to instantiate the root element.

        Honors ``xsi:type`` on the root element (dispatch to another
        schema type) and rejects abstract root element declarations,
        recording problems on the validation report.
        """
        if rootElement.isAbstract():
            self.report.add_error(
                f"root element '{rootElement.name}' is declared abstract; "
                "abstract elements may not appear in instance documents",
                code="abstract-element",
                element=rootElement.name,
            )

        subCls = rootElement.getType()
        if subCls is SchemaBase and rootElement.tagAttributes.get("type") is None:
            # An untyped declaration that is a substitution-group member
            # takes the head's type definition (SUN typeDef00204m).
            headName = rootElement.getSubstitutionGroupHead(parser=self)
            if headName:
                schemaER = rootElement.getSchema()
                for candidate in getattr(schemaER, "elements", None) or []:
                    if candidate is rootElement:
                        continue
                    names = {candidate.name, getattr(candidate, "expandedName", None)}
                    if headName in names:
                        headCls = candidate.getType()
                        if headCls is not None and headCls is not SchemaBase:
                            subCls = headCls
                        break
        if subCls is None:
            self.report.add_error(
                f"the type of root element '{rootElement.name}' could not be resolved",
                code="unknown-type",
                element=rootElement.name,
            )
            return None

        xsiTypeName = xsi.xsi_type_name(self.xmlRoot)
        if xsiTypeName is None:
            # xsi:type takes precedence over conditional type assignment
            # (XSD 1.1 §3.3.4.1); without it, the declaration's
            # alternatives select the governing type.
            from pyxsd.alternatives import ERROR_TYPE, select_alternative_type

            selected = select_alternative_type(rootElement, self.xmlRoot, self)
            if selected is ERROR_TYPE:
                self.report.add_error(
                    f"root element '{rootElement.name}' selects the xs:error "
                    "type, whose value space is empty, so it cannot be valid",
                    code="alternative-error",
                    element=rootElement.name,
                )
                return subCls
            if selected is not None:
                return selected
            return subCls

        resolvedName = self._resolveXsiTypeName(xsiTypeName, self.xmlRoot)
        if resolvedName is None:
            return subCls
        resolved = ElementRepresentative.typeFromName(resolvedName, self)
        if resolved is None:
            self.report.add_error(
                f"xsi:type '{xsiTypeName}' on the root element does not "
                "correspond to a type in the schema",
                code="xsi-type",
                element=rootElement.name,
            )
            return subCls
        blocked = combinedBlock(rootElement.getBlock(), subCls)
        reason = is_valid_xsi_type(resolved, subCls, blocked)
        if reason is not None:
            self.report.add_error(
                derivationMessage(resolved, subCls, reason),
                code="xsi-type",
                element=rootElement.name,
            )
            return subCls
        logger.debug("Root element dispatched via xsi:type to %s", resolved.__name__)
        return resolved

    def generateCorrectSchemaTags(self) -> None:
        """Generates the proper schema information and namespace
        information for a tag.

        ElementTree leaves the schema information in a form that is not
        valid XML on its own.
        """
        locationTagName = self.getSchemaInfo("t")
        if locationTagName is None or locationTagName not in self.xmlRoot.attrib:
            # No schema location information in the xml file; nothing
            # to regenerate.
            return None

        schemaLocation = self.xmlRoot.attrib[locationTagName]

        del self.xmlRoot.attrib[locationTagName]

        ns = self.nameSpace

        self.xmlRoot.attrib["xmlns:xsi"] = "http://www.w3.org/2001/XMLSchema-instance"

        if ns:
            self.xmlRoot.attrib["xsi:schemaLocation"] = schemaLocation
            self.xmlRoot.attrib["xmlns"] = ns
            self.xmlRoot.attrib[f"xmlns:{ns.lower()}"] = ns
            return None

        self.xmlRoot.attrib["xsi:noNamespaceSchemaLocation"] = schemaLocation
        return None

    def writeParsedXMLFile(self, rootInstance: Any) -> Any:
        """Writes the parsed (pre-transform) xml file, if requested."""
        output = self.xmlFileOutput
        if output is True:
            output = self.getXmlOutputFileName()
        if not output or output == "_No_Output_":
            return rootInstance
        if isinstance(output, (str, os.PathLike)):
            with open(output, "w") as outputFile:
                self.writeXML(rootInstance, outputFile)
        else:
            self.writeXML(rootInstance, output)
        return rootInstance

    def writeXML(self, rootInstance: Any, output: str | Path | os.PathLike[str] | IO[str]) -> None:
        """Sends a pythonic instance tree to the tree writer.

        - ``rootInstance``: the root instance of a tree. Must be
          formatted in the program's tree structure.

        - ``output``: the file object (or path) to write the tree to.
          Paths are opened and closed here; file objects passed by the
          caller are flushed but left open.
        """
        if isinstance(output, (str, os.PathLike)):
            with open(output, "w") as outputFile:
                XmlTreeWriter(rootInstance, outputFile)
        else:
            XmlTreeWriter(rootInstance, output)
            output.flush()
        logger.debug("Data sent to the writer...")

    def getClasses(self) -> dict[str, type[SchemaBase]]:
        """Returns the dictionary of classes created by
        ElementRepresentative for each type specified in the schema.
        """
        return self.classes

    def loadClassFromFile(self, classFile: str | Path | os.PathLike[str]) -> None:
        """Loads a file with overlay classes into the class dictionary.

        Overlay classes add to and override the schema type classes to
        allow for a user to create their own types without changing the
        schema file itself.

        **Consider this functionality experimental.**

        - ``classFile``: a string that specifies the location of a
          user-created overlay class file
        """
        filePath = Path(classFile)
        if not filePath.is_file():
            # Fall back to the historical behavior of resolving a
            # module name against the xml file's directory.
            candidate = self.xmlPath / f"{classFile}.py"
            if candidate.is_file():
                filePath = candidate
            else:
                raise ImportError(
                    f"the file '{classFile}' was not found. Please check your spelling."
                )
        module_name = filePath.stem
        module = _loadModuleFromFile(module_name, filePath)
        if module is None:
            raise ImportError(f"the file '{classFile}' could not be loaded.")
        newClasses = {}
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
        self.classes.update(newClasses)

    def getXmlTree(self) -> Any:
        """Sends the xml file into the ElementTree library's parser.

        Allows for the program to get the schemaLocation before parsing
        the xml against the schema.
        """
        logger.debug("The XML file is being parsed by the ElementTree library...")
        try:
            root = parse_with_namespaces(self.xmlFileInput, self.namespaceContext)
        except OSError as e:
            raise PyXSDError(f"the xml input could not be read: {e}") from e
        except ET.ParseError as e:
            raise PyXSDError(f"the xml file is not well-formed XML: {e}") from e
        logger.debug("XML file parsed by the ElementTree library successfully...")
        return root

    def getXmlOutputFileName(self) -> Path:
        """Creates a default name for the xml file that is parsed without
        any transforms.  Uses the name from the input xml file.
        """
        inputPath = Path(str(self.xmlFileInput))
        return inputPath.parent / (inputPath.stem + "Parsed.xml")

    def getTransformModuleAndLoad(self, className: str) -> ModuleType:
        """Loads a transform class from its class name.

        The module it is located in must share the class name, either
        in camelCase with a lowercase first letter (the historical
        convention) or in snake_case. The transform is looked up in
        the installed ``pyxsd.transforms`` package, then in the
        directory the program was called from, and then in the
        directory where the xml file is. If no exact module-name
        candidate matches, an underscore-insensitive fallback matches
        the class name against the available modules, so acronym
        spellings (``SendTreeToPyXSD`` -> ``send_tree_to_pyxsd``)
        still resolve.

        - ``className``: a string of the transform class name being
          called
        """
        candidates = _transformModuleNames(className)
        for fileName in candidates:
            try:
                return importlib.import_module("pyxsd.transforms." + fileName)
            except ImportError:
                pass
        module = _loadTransformModuleByNormalizedName(className)
        if module is not None:
            return module
        searchPaths = [Path.cwd()]
        if self.xmlPath != searchPaths[0]:
            searchPaths.append(self.xmlPath)
        for directory in searchPaths:
            for fileName in candidates:
                candidate = directory / (fileName + ".py")
                if candidate.is_file():
                    module = _loadModuleFromFile(fileName, candidate)
                    if module is not None:
                        return module
            module = _loadTransformFileByNormalizedName(className, directory)
            if module is not None:
                return module
        raise ImportError(
            f"the transform module for '{className}' could not be found in "
            "pyxsd.transforms or in the search paths"
        )

    def transform(self, transforms: list[str], root: Any) -> Any:
        """Calls the transforms specified by the user.

        Each transform is loaded into memory by
        ``getTransformModuleAndLoad()``. The transform class is passed
        the instance of the root element when it is initialized. The
        transform object is called with the specified arguments and the
        new root instance is set to whatever the transform returns,
        which is usually the root, but it is not required. Any user who
        uses a transform that does not return the root tree instance
        should be aware that any transform that uses the root instance
        will fail to work and raise a fatal error.

        - ``transforms``: a list containing the transform calls in the
          order that they should be called. Each call looks like
          ``TransformClass(arg1, arg2, key=value)``.

        - ``root``: the root instance of a tree. Must be formatted in
          the program's tree structure.
        """
        currentRoot = root

        for transform in transforms:
            class_name, args, kwargs = parseTransformCall(transform)
            try:
                transformer = self.getTransformModuleAndLoad(class_name)
            except ImportError as e:
                raise PyXSDError(f"the transform '{class_name}' could not be loaded: {e}") from e
            argDesc = repr(args)
            if kwargs:
                argDesc += " " + repr(kwargs)
            logger.debug(
                "Starting the transform '%s' with the following args: %s",
                class_name,
                argDesc,
            )
            transformCls = getattr(transformer, class_name, None)
            if transformCls is None:
                raise PyXSDError(
                    f"the module for the transform '{class_name}' does not define that class"
                )
            instance = transformCls(currentRoot)
            if getattr(instance, "inheritsParserContext", False):
                # Reparsing transforms default to this run's schema and
                # binding mode, and this run's report surfaces theirs.
                instance.outerParser = self
            try:
                inspect.signature(instance.__call__).bind(*args, **kwargs)
            except TypeError as e:
                # Only the call signature is checked here; exceptions the
                # transform body raises stay untouched.
                raise PyXSDError(
                    f"the transform call '{transform}' does not match the signature "
                    f"of {class_name}: {e}"
                ) from e
            currentRoot = instance(*args, **kwargs)
            self._absorbTransformReport(getattr(instance, "report", None))

        return currentRoot

    def _absorbTransformReport(self, report: ValidationReport | None) -> None:
        """Merges a transform's embedded validation report into this
        run's report, at most once per report object.

        Transforms such as ``SendTreeToPyXSD`` revalidate the tree
        inside their own parser run. Without this merge, problems in
        the transformed content would vanish with the inner run and
        ``--strict`` (which inspects this report) could not see them.
        Only :class:`ValidationReport` values are merged: a transform is
        free to use its ``report`` attribute for its own data.
        """
        if report is None or report is self.report:
            return
        if not isinstance(report, ValidationReport):
            return
        if any(report is absorbed for absorbed in self._absorbedReports):
            return
        self._absorbedReports.append(report)
        self.report.extend(report)

    def getTransformsFileName(self) -> Path:
        """Creates a default name for the xml file that is written after
        all of the transforms.  Uses the name from the input xml file.
        """
        if self.xmlFileInputName is None:
            newName = Path.cwd() / "output.xml"
        else:
            newName = self.xmlPath / (Path(self.xmlFileInputName).stem + "Transformed.xml")
        logger.debug("Setting the transformed xml file name to the default: %s", newName)
        return newName

    def getSchemaInfo(self, nameOrLocation: str | None) -> str | None:
        """Extracts information from the *schemaLocation* tag or the
        *noNamespaceSchemaLocation* tag.

        Depending on the value of the parameter ``nameOrLocation``, the
        function outputs the namespace, the schema location, or the tag
        type. This function is meant for use with other functions to
        easily grab bits of data that are used in various locations in
        the program.

        - ``nameOrLocation``: a one letter string that is either 'l',
          'n', or 't'. If the variable is 'l', the location of the
          schema is returned. If it is 'n', the namespace is returned,
          if there is one. 't' returns the tag name to indicate if the
          xml uses *schemaLocation* or *noNamespaceSchemaLocation*.
        """
        xsiNS = "http://www.w3.org/2001/XMLSchema-instance"
        schemaLocationSplit = None
        if self.makeFullName(xsiNS, "schemaLocation") in self.xmlRoot.attrib:
            schemaLocationTag = self.xmlRoot.attrib[self.makeFullName(xsiNS, "schemaLocation")]
            schemaLocationSplit = schemaLocationTag.split()

            if not schemaLocationSplit or len(schemaLocationSplit) % 2 != 0:
                report = getattr(self, "report", None)
                message = (
                    "the 'schemaLocation' tag must be one or more "
                    "namespace/location pairs separated by whitespace, with "
                    "each namespace stated first, followed by the location of "
                    "the schema; attempting to use the "
                    "'noNamespaceSchemaLocation' tag instead"
                )
                if report is not None:
                    report.add_warning(message, code="schema-hint")
                else:
                    logger.warning(message)
                del self.xmlRoot.attrib[self.makeFullName(xsiNS, "schemaLocation")]
                self.xmlRoot.attrib[self.makeFullName(xsiNS, "noNamespaceSchemaLocation")] = (
                    schemaLocationTag
                )

        if self.makeFullName(xsiNS, "noNamespaceSchemaLocation") in self.xmlRoot.attrib:
            if nameOrLocation == "t":
                return self.makeFullName(xsiNS, "noNamespaceSchemaLocation")
            if nameOrLocation == "n":
                return None
            if nameOrLocation == "l":
                return self.xmlRoot.attrib[self.makeFullName(xsiNS, "noNamespaceSchemaLocation")]

        if schemaLocationSplit is None:
            # No schema location information in the xml file.
            return None

        schemaNS = schemaLocationSplit[0]
        if nameOrLocation == "n":
            return schemaNS

        schemaLocation = schemaLocationSplit[1]
        if nameOrLocation == "l":
            return schemaLocation

        if nameOrLocation == "t":
            return self.makeFullName(xsiNS, "schemaLocation")

        return None

    def getSchemaLocationPairs(self) -> list[tuple[str | None, str]]:
        """Returns every ``(namespace, location)`` hint in the instance.

        ``xsi:schemaLocation`` may carry multiple namespace/location
        pairs; ``xsi:noNamespaceSchemaLocation`` yields a single pair
        with ``None`` for the namespace.
        """
        xsiNS = "http://www.w3.org/2001/XMLSchema-instance"
        pairs: list[tuple[str | None, str]] = []
        schemaLocationTag = self.xmlRoot.attrib.get(self.makeFullName(xsiNS, "schemaLocation"))
        if schemaLocationTag:
            tokens = schemaLocationTag.split()
            if len(tokens) % 2 == 0:
                for i in range(0, len(tokens), 2):
                    pairs.append((tokens[i] or None, tokens[i + 1]))
        noNamespace = self.xmlRoot.attrib.get(self.makeFullName(xsiNS, "noNamespaceSchemaLocation"))
        if noNamespace:
            pairs.append((None, noNamespace))
        return pairs

    def makeFullName(self, ns: str | None, text: str) -> str:
        """Makes a string that looks similar to some of the names in
        ElementTree when it contains namespace information.

        - ``ns``: a string of the namespace used. For this function,
          this variable is usually set to a url.

        - ``text``: a string of the name of the tag that the full name
          is being created for.
        """
        return f"{{{ns}}}{text}"


def _transformModuleNames(className: str) -> list[str]:
    """Return candidate module names for a transform class name.

    Both the historical camelCase convention (``PrintData`` ->
    ``printData``) and snake_case (``PrintData`` -> ``print_data``,
    ``SendTreeToPyXSD`` -> ``send_tree_to_py_xsd``) are supported.
    """
    camel = className[:1].lower() + className[1:]
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", className)
    snake = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", snake).lower()
    names = [camel]
    if snake != camel:
        names.append(snake)
    return names


def _normalizedModuleName(name: str) -> str:
    """Reduce a module or class name for underscore-insensitive match."""
    return name.replace("_", "").lower()


def _loadModuleFromFile(moduleName: str, path: Path) -> ModuleType | None:
    """Import a transform module from an explicit file path.

    The module's directory is added to ``sys.path`` while the module
    executes, so sibling modules (transform libraries such as the
    shipped examples) can be imported by plain name.
    """
    spec = importlib.util.spec_from_file_location(moduleName, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    directory = str(Path(path).parent)
    added = directory not in sys.path
    if added:
        sys.path.insert(0, directory)
    try:
        spec.loader.exec_module(module)
    finally:
        if added and directory in sys.path:
            sys.path.remove(directory)
    return module


def _loadTransformModuleByNormalizedName(className: str) -> ModuleType | None:
    """Find a shipped transform module whose name matches the class.

    The exact-name candidates can miss when the camel-to-snake
    conversion splits acronyms differently than the module file does
    (``send_tree_to_py_xsd`` vs ``send_tree_to_pyxsd``); comparing
    underscore-free names resolves those cases.
    """
    target = _normalizedModuleName(className)
    transformsPackage = importlib.import_module("pyxsd.transforms")
    for moduleInfo in pkgutil.iter_modules(transformsPackage.__path__):
        if _normalizedModuleName(moduleInfo.name) == target:
            return importlib.import_module(f"pyxsd.transforms.{moduleInfo.name}")
    return None


def _loadTransformFileByNormalizedName(className: str, directory: Path) -> ModuleType | None:
    """Find a transform file in ``directory`` matching the class name."""
    target = _normalizedModuleName(className)
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.suffix != ".py" or _normalizedModuleName(entry.stem) != target:
            continue
        return _loadModuleFromFile(entry.stem, entry)
    return None


def parseTransformCall(call: str) -> tuple[str, list[Any], dict[str, Any]]:
    """Parse a transform call string into its parts.

    A transform call looks like ``TransformClass(arg1, arg2, key=value)``
    where all arguments are literal values (numbers, strings, booleans,
    None, and lists/tuples of those). Returns a tuple of
    ``(class_name, args, kwargs)``.

    This replaces the historic ``eval``-based call handling with a safe
    literal parser.
    """
    try:
        tree = ast.parse(call.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(
            f"Transform Call Error: the transform call '{call}' does not use correct syntax."
        ) from e
    if not isinstance(tree.body, ast.Call) or not isinstance(tree.body.func, ast.Name):
        raise ValueError(
            f"Transform Call Error: the transform call '{call}' does not use correct syntax."
        )
    class_name = tree.body.func.id
    args = [ast.literal_eval(arg) for arg in tree.body.args]
    kwargs: dict[str, Any] = {}
    for keyword in tree.body.keywords:
        if keyword.arg is None:
            raise ValueError(
                f"Transform Call Error: the transform call '{call}' does not use correct syntax."
            )
        kwargs[keyword.arg] = ast.literal_eval(keyword.value)
    return class_name, args, kwargs


def _line_start_offsets(chain: str) -> list[int]:
    """Absolute offsets at which each 1-based source line starts."""
    starts = [0]
    for index, character in enumerate(chain):
        if character == "\n":
            starts.append(index + 1)
    return starts


def split_transform_chain(chain: str) -> list[str]:
    """Splits a CLI transform chain on its top-level ``>`` separators.

    The historic implementation split on every ``>`` character, so an
    argument like ``PrintData("a>b.xml")`` was chopped mid-string. The
    chain is now tokenized: a ``>`` only separates calls when it stands
    outside any string literal and outside any call parentheses.
    Tokenizer positions are ``(line, column)`` pairs, so they are
    converted to absolute string offsets before slicing; multi-line
    calls and triple-quoted arguments therefore split correctly too.
    Whitespace around segments is stripped; a chain without separators
    yields a single-element list.
    """
    try:
        tokens = tokenize.generate_tokens(io.StringIO(chain).readline)
        line_starts = _line_start_offsets(chain)
        separators: list[int] = []
        depth = 0
        for tokType, tokString, start, _end, _line in tokens:
            if tokType == tokenize.ERRORTOKEN:
                # Python 3.11's tokenizer reports invalid characters (and
                # unterminated strings outside any call parentheses) as
                # ERRORTOKEN tokens instead of raising; 3.12+ raises
                # TokenError for them. Reject either way.
                raise ValueError(
                    f"Transform Chain Error: the transform chain '{chain}' is not valid Python syntax."
                )
            if tokType != tokenize.OP:
                continue
            if tokString in "([{":
                depth += 1
            elif tokString in ")]}":
                depth -= 1
            elif tokString == ">" and depth == 0:
                separators.append(line_starts[start[0] - 1] + start[1])
    except (tokenize.TokenError, IndentationError, SyntaxError) as e:
        raise ValueError(
            f"Transform Chain Error: the transform chain '{chain}' is not valid Python syntax."
        ) from e
    if not separators:
        stripped = chain.strip()
        return [stripped] if stripped else []
    segments: list[str] = []
    previous = 0
    for position in separators:
        segments.append(chain[previous:position].strip())
        previous = position + 1
    segments.append(chain[previous:].strip())
    return segments


def _configure_logging(verbose: bool, quiet: bool) -> None:
    """Set the root logging level according to the CLI flags."""
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.CRITICAL
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def main(argv: list[str] | None = None) -> None:
    """Run pyxsd from the command line."""
    from argparse import ArgumentParser

    parser = ArgumentParser(
        prog="pyxsd",
        description="Parse an xml file against its schema and optionally run transforms on it.",
    )
    parser.add_argument("--version", action="version", version="PyXSD " + __version__)

    parser.add_argument(
        "-i",
        "--inputXml",
        dest="inputXmlFile",
        default="stdin",
        metavar="INPUTXMLFILE",
        help="filename for the xml file to read in. Reads from stdin by default.",
    )
    parser.add_argument(
        "-s",
        "--inputXsd",
        "--schema",
        dest="inputXsdFile",
        default=None,
        metavar="INPUTXSDFILE",
        help="filename for the xsd (schema) file to read in. Tries to "
        "determine the location from the input xml file by default.",
    )
    parser.add_argument(
        "-p",
        "--parsedXml",
        "--parsedOutput",
        dest="parsedOutputFile",
        default=None,
        metavar="PARSEDOUTPUTFILE",
        help="filename for the xml file that contains the parsed output of "
        "the xml file, which contains no further transformation. By default, "
        "the filename is the xml input filename followed by 'Parsed'.",
    )
    parser.add_argument(
        "-k",
        "--ParsedFile",
        action="store_true",
        dest="outputParsed",
        default=False,
        help="output a parsed version of the xml file without transforms. "
        "Use for debugging. Off by default. If no filename is specified, it "
        "will be determined from the xml filename.",
    )
    parser.add_argument(
        "-o",
        "--transformOutput",
        dest="transformOutputFile",
        default="stdout",
        metavar="TRANSFORMOUTPUTFILE",
        help="filename for the output after the xml has been parsed and "
        "transformed. Output is sent to stdout by default. Any specified "
        "filename will override this option.",
    )
    parser.add_argument(
        "-d",
        "--useDefaultFile",
        action="store_true",
        dest="transformDefaultOutput",
        help="use the default filename for the transformed output. If not "
        "specified and no filename is specified, uses stdout.",
    )
    parser.add_argument(
        "-t",
        "--transform",
        dest="transformCall",
        default=None,
        metavar="TRANSFORMCALL",
        help="the transform class with args, separated by '>' if multiple. "
        "See the documentation for syntax and further information.",
    )
    parser.add_argument(
        "-T",
        "--transformFile",
        dest="transformFile",
        default=None,
        metavar="TRANSFORMFILE",
        help="file with transform class calls, one per line. See the "
        "documentation for information on this function.",
    )
    parser.add_argument(
        "-c",
        "--overlayClassesFile",
        dest="classFile",
        default=None,
        metavar="CLASSFILE",
        help="Experimental. Allows user-defined schemas to override and add "
        "to the types defined in the schema file. See the documentation for "
        "information on this function.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        dest="verbose",
        default=False,
        help="use verbose mode: log progress details (DEBUG level).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=False,
        help="use quiet mode: suppress log output (CRITICAL level). "
        "Validation issues are still reported.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        dest="strict",
        default=False,
        help="exit with status 1 if the xml file has validation errors.",
    )
    parser.add_argument(
        "--mode",
        choices=["strict", "lax"],
        default="strict",
        dest="mode",
        help="binding mode. 'strict' (default) reports invalid values and "
        "drops them; 'lax' keeps the report strict but binds best-effort "
        "values (raw strings, generic subtrees) so no data is lost.",
    )
    parser.add_argument(
        "--namespaces",
        choices=["strict", "legacy"],
        default="legacy",
        dest="namespaces",
        help="namespace handling. 'legacy' (default) matches by local name "
        "and ignores namespace URIs; 'strict' resolves QNames and matches "
        "elements and attributes by expanded name, which rejects documents "
        "that only matched by local name before.",
    )

    options = parser.parse_args(argv)

    if options.quiet and options.verbose:
        parser.error("Both verbose mode and quiet mode cannot be on at the same time")

    _configure_logging(options.verbose, options.quiet)

    if options.transformDefaultOutput and options.transformOutputFile == "stdout":
        options.transformOutputFile = None

    if options.transformCall and options.transformFile:
        parser.error("A transform file and a transform call cannot both be specified.")

    if options.inputXmlFile == "stdin":
        if sys.stdin.isatty():
            parser.error(
                "if no input xml file is specified, the xml must be fed in "
                "through stdin (i.e. pipes)"
            )
        inputXmlFile = io.StringIO(sys.stdin.read())
    else:
        inputXmlFile = options.inputXmlFile

    parsedOutputFile = options.parsedOutputFile
    if not options.outputParsed:
        parsedOutputFile = "_No_Output_"

    try:
        transforms = []
        if options.transformCall:
            transforms = split_transform_chain(options.transformCall)
        if options.transformFile:
            with open(options.transformFile) as fd:
                transforms = [call for line in fd for call in split_transform_chain(line)]
        baseMode = ParseModes.LAX if options.mode == "lax" else ParseModes.STRICT
        app = PyXSD(
            inputXmlFile,
            options.inputXsdFile,
            parsedOutputFile,
            options.transformOutputFile,
            transforms,
            options.classFile,
            options.verbose,
            options.quiet,
            mode=baseMode.replace(namespaces=options.namespaces),
        )
    except (PyXSDError, ValueError, OSError) as e:
        print(f"pyxsd: error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if app.report:
        print(str(app.report), file=sys.stderr)
    if options.strict and app.report.has_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
