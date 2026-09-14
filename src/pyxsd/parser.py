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
from pathlib import Path
from types import ModuleType
from typing import IO, Any
from xml.etree import ElementTree as ET

from pyxsd import __version__, xsi
from pyxsd.binding import BindingPolicy, ParseModes
from pyxsd.derivation import blockTokens, combinedBlock, derivationMessage, is_validly_derived
from pyxsd.element_representatives.element_representative import (
    ComponentTable,
    ElementRepresentative,
    componentKind,
)
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.namespaces import (
    XML_NS,
    XSD_NS,
    NamespaceContext,
    NamespaceError,
    clark,
    local_name,
    namespace_of,
    parse_with_namespaces,
)
from pyxsd.schema_base import SchemaBase, nil_content_kind
from pyxsd.schema_context import SchemaContext, remember_components, with_schema_context
from pyxsd.validation import ValidationReport
from pyxsd.writers.xml_tree_writer import XmlTreeWriter
from pyxsd.xsd_data_types import (
    AnySimpleType,
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
#: several same-named declarations for different versions.
_VC_NS = "http://www.w3.org/2007/XMLSchema-versioning"


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
        # Source-document form defaults per spliced component:
        # id(xsdElement) -> (elementFormDefault, attributeFormDefault).
        self._formDefaults: dict[int, tuple[str | None, str | None]] = {}
        # Element identities that came from an included/imported schema
        # document. ``id`` uniqueness is an XML (per-document) rule, so
        # components spliced from another document must not be compared
        # against the main document's ids.
        self._composedElementIds: set[int] = set()
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

        baseDir, visited = self._schemaCompositionContext()
        # Documents already fully composed; their components must not be
        # spliced twice (diamond includes) and a repeat encounter is not
        # an error.
        self._composedDocuments: set[str] = set(visited)
        self._spliceComposedSchemas(root, baseDir, visited)
        self._spliceAdditionalSchemas(root, baseDir, visited)
        if getattr(self.mode, "namespaces", "legacy") == "strict":
            self._injectXmlNamespaceAttributes(root)

        # Stage this parser's snapshot on its own context before the ER
        # run so imported declarations report their own target namespace
        # and form defaults.
        self.schemaContext.namespace_overrides = dict(self._namespaceOverrides)
        self.schemaContext.form_defaults = dict(self._formDefaults)
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
        self._reportDeclarationIssues(schemaER)
        # This parser owns the component table the ER run registered
        # into; expose it on the parser and on the context so registry
        # lookups (xsi:type dispatch, tests) use this parser's
        # declarations rather than a previous parser's.
        self.components = schemaER.components
        self.schemaContext.components = self.components
        # Module-level lookups after this parse (ElementRepresentative
        # .getFromName, the registry proxy) see this parser's table.
        remember_components(self.components)

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
                built.add(id(typeER))
                cls = typeER.clsFor(self)
                self.classes[typeER.name] = cls
                if typeER.expandedName:
                    self.classes[typeER.expandedName] = cls
                logger.debug("Class created for the %s type...", typeER.name)

        self._buildSubstitutionGroups(schemaER)
        self._checkSubstitutionGroupExclusions(schemaER)
        self._checkValueConstraints(schemaER)

        return None

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
        )
        seen: set[int] = set()
        seenIds: dict[str, Any] = {}
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
            self._checkDeclarationId(er, seenIds)
            self._checkDuplicateName(er, declared)
            er.checkDeclarationLegality()
            misplacement = getattr(er, "misplacement", None)
            if misplacement is not None:
                code, message = misplacement
                self.report.add_error(message, code=code)
            stack.extend(getattr(er, "processedChildren", None) or ())

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
        value = getattr(er, "id", None)
        if value is None:
            return
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
        if element is not None and id(element) in self._composedElementIds:
            # A component spliced in from an included, imported or
            # redefined document is not compared against the main
            # schema's components. Composition may legitimately expose a
            # name twice (nested redefines, a re-parsed document), so the
            # check is scoped to the main document, matching the ``id``
            # uniqueness scope in ``_checkDeclarationId``.
            return
        if self._inConditionalInclusion(er):
            # XSD 1.1 conditional-inclusion declarations (``vc:*``) may
            # share a name, selected by version or availability. Without
            # evaluating the selectors, do not report the duplicate.
            return
        typeName = type(er).__name__
        if typeName in self._IDENTITY_KINDS:
            parent = getattr(er, "parent", None)
            if parent is None or type(parent).__name__ != "Element":
                return
            key = ("identity", id(parent), name)
            label = "identity constraint"
        else:
            kind = componentKind(er)
            if kind is None or not er.isGlobalDeclaration():
                return
            namespace = er.getNamespace()
            if namespace == XML_NS:
                # The XML-namespace attributes (xml:lang, xml:space, ...)
                # are registered as built-ins before the ER run and may
                # also be imported from the XML namespace schema; a
                # repeat there is not an authoring error.
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
                key.startswith(f"{{{_VC_NS}}}") for key in element.attrib
            ):
                return True
            node = getattr(node, "parent", None)
        return False

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
            head = element.getSubstitutionGroupHead(self)
            if head is None:
                continue
            if head not in declaredNames and head not in declaredExpanded:
                self.report.add_error(
                    f"element '{element.name}' declares substitutionGroup "
                    f"'{element.tagAttributes.get('substitutionGroup', head)}', "
                    "but no global element with that name exists",
                    code="unknown-substitution-head",
                    element=element.name,
                )
                continue
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
            if element.getSubstitutionGroupHead(self) is None:
                continue
            visited: set[int] = set()
            current = element
            while current is not None and id(current) not in visited:
                visited.add(id(current))
                headName = current.getSubstitutionGroupHead(self)
                head = byName.get(headName) if headName is not None else None
                if head is None:
                    break
                if head is element:
                    self.report.add_error(
                        f"element '{element.name}' is part of a cyclic substitution group",
                        code="circular-substitution-group",
                        element=element.name,
                        phase="schema",
                    )
                    break
                current = head

    def _checkSubstitutionGroupExclusions(self, schemaER: Any) -> None:
        """Reports a substitution member whose derivation the head blocks.

        e-props-correct requires a member's type to be validly derived
        from the head's type given the head element's {substitution group
        exclusions}, which the XML representation spells ``final`` (or
        the schema's ``finalDefault``). Runs after class building so the
        generated classes carry the derivation hierarchy and method.
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
            headName = member.getSubstitutionGroupHead(self)
            head = byName.get(headName) if headName is not None else None
            if head is None or head is member:
                continue
            final = head.tagAttributes.get("final")
            if final is None:
                final = finalDefault
            excluded = blockTokens(final)
            if not excluded:
                continue
            memberCls = self._declaredTypeClass(member)
            headCls = self._declaredTypeClass(head)
            if memberCls is None or headCls is None:
                continue
            if is_validly_derived(memberCls, headCls, excluded) == "blocked":
                self.report.add_error(
                    f"element '{member.name}' has a type whose derivation from "
                    f"substitution head '{head.name}' is excluded by final='{final}'",
                    code="declaration-attribute",
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
                additions.append((pair_namespace, path))
        return additions

    def _spliceAdditionalSchemas(self, schemaRoot: Any, baseDir: Path, visited: set[str]) -> None:
        """Splices schemas supplied outside the main document.

        These are ``namespace_schemas`` entries and extra
        ``xsi:schemaLocation`` pairs; each is loaded like an import so
        its components keep their own target namespace.
        """
        for namespace, path in getattr(self, "_additionalSchemas", []):
            tag = ET.Element(clark(XSD_NS, "import"), {"schemaLocation": str(path)})
            if namespace:
                tag.set("namespace", namespace)
            self._spliceIncludedSchema(tag, schemaRoot, baseDir, visited, isImport=True)

    def _spliceComposedSchemas(self, schemaRoot: Any, baseDir: Path, visited: set[str]) -> None:
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
        """
        for child in list(schemaRoot):
            local = child.tag.split("}")[-1]
            if local == "include":
                schemaRoot.remove(child)
                self._spliceIncludedSchema(child, schemaRoot, baseDir, visited, isImport=False)
            elif local == "redefine":
                schemaRoot.remove(child)
                self._spliceRedefine(child, schemaRoot, baseDir, visited)
            elif local == "import":
                schemaRoot.remove(child)
                if child.get("namespace") == XSD_NS:
                    # Importing the schema-for-schemas namespace is the
                    # conventional spelling; the built-in types are
                    # always available here.
                    continue
                self._spliceIncludedSchema(child, schemaRoot, baseDir, visited, isImport=True)
        return None

    def _spliceIncludedSchema(
        self,
        tag: Any,
        schemaRoot: Any,
        baseDir: Path,
        visited: set[str],
        isImport: bool,
    ) -> None:
        """Splices the named components of one included/imported schema.

        Handles locating and parsing the file, cycle detection and the
        namespace checks; the actual splicing is shared with redefine.
        """
        location = tag.get("schemaLocation")
        if not location:
            if isImport:
                # ``schemaLocation`` is optional on xs:import: a
                # namespace-only import is a hint with no document to
                # load. In strict mode it is unresolved unless a schema
                # for the namespace was supplied.
                namespace = tag.get("namespace")
                if (
                    getattr(self.mode, "namespaces", "legacy") == "strict"
                    and namespace
                    and namespace != XML_NS
                    and namespace not in self.namespaceSchemas
                    and namespace
                    not in {ns for ns, _ in getattr(self, "_additionalSchemas", []) if ns}
                ):
                    self.report.add_error(
                        f"the import for namespace '{namespace}' has no "
                        "schemaLocation and no schema was supplied for it",
                        code="import-unresolved",
                        phase="schema",
                    )
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
        )
        if includedRoot is None:
            return None
        mainNS = schemaRoot.get("targetNamespace")
        includedNS = includedRoot.get("targetNamespace")
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
        self._spliceComposedSchemas(
            includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
        # Record provenance before the components are appended to the
        # main root: ``id`` uniqueness is scoped to a schema document.
        for element in includedRoot.iter():
            self._composedElementIds.add(id(element))
        self._appendNamedComponents(includedRoot, schemaRoot, componentNamespace)
        self._composedDocuments.add(key)
        return None

    def _parseIncludedSchema(
        self, location: str, baseDir: Path, error_code: str = "schema-compose"
    ) -> Any | None:
        """Parses one included schema file; returns its root or ``None``.

        Failures (unreadable file, malformed xml) are recorded as
        ``error_code`` errors and the composition proceeds without the
        missing file.
        """
        includedPath = baseDir / location
        try:
            with open(includedPath, "rb") as includedFile:
                root = parse_with_namespaces(includedFile, self.namespaceContext)
        except OSError as e:
            self.report.add_error(
                f"the schema '{location}' could not be opened: {e}",
                code=error_code,
            )
            return None
        except ET.ParseError as e:
            self.report.add_error(
                f"the schema '{location}' is not well-formed XML: {e}",
                code=error_code,
            )
            return None
        return root

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
        for component in list(includedRoot):
            if component.tag.split("}")[-1] in _COMPOSABLE_TAGS:
                if strict:
                    for element in component.iter():
                        self._namespaceOverrides.setdefault(id(element), namespace)
                        self._formDefaults.setdefault(id(element), sourceDefaults)
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
        location = redefineTag.get("schemaLocation")
        if not location:
            self.report.add_error(
                "a redefine tag has no schemaLocation; the schema could not be composed",
                code="schema-compose",
            )
            return None
        includedRoot = self._parseIncludedSchema(location, baseDir)
        if includedRoot is None:
            return None
        includedPath = (baseDir / location).resolve()
        if str(includedPath) in visited:
            self.report.add_warning(
                f"the schema '{location}' is already being composed; "
                "the circular redefine is skipped",
                code="compose-cycle",
            )
            return None
        redefinedNames = set()
        for child in list(redefineTag):
            local = child.tag.split("}")[-1]
            if local in ("complexType", "simpleType", "group", "attributeGroup") and child.get(
                "name"
            ):
                redefinedNames.add(child.get("name"))
        for component in list(includedRoot):
            local = component.tag.split("}")[-1]
            name = component.get("name")
            if (
                local in ("complexType", "simpleType", "group", "attributeGroup")
                and name in redefinedNames
            ):
                component.set("name", f"{name}|base")
        self._spliceComposedSchemas(
            includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
        # The redefined document's components come from another schema
        # document; scope ``id`` uniqueness provenance to it.
        for element in includedRoot.iter():
            self._composedElementIds.add(id(element))
        self._appendNamedComponents(includedRoot, schemaRoot, schemaRoot.get("targetNamespace"))
        for child in list(redefineTag):
            for element in child.iter():
                base = element.get("base")
                if base and base.split(":")[-1] in redefinedNames:
                    element.set("base", f"{base.split(':')[-1]}|base")
                # Inside a redefine block a reference to the redefined
                # group/attributeGroup means the *original*, so rebind it
                # to the renamed |base definition instead of the new one
                # (which would be a circular reference).
                localRef = element.get("ref")
                if (
                    element.tag.split("}")[-1] in ("group", "attributeGroup")
                    and localRef
                    and localRef.split(":")[-1] in redefinedNames
                ):
                    element.set("ref", f"{localRef.split(':')[-1]}|base")
            schemaRoot.append(child)
        return None

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

        schemaClass = self.getClasses()["schema"]

        schemaClassInstance = schemaClass()

        rootName = self.xmlRoot.tag.split("}")[-1]

        topLevelDescriptors = schemaClassInstance._getElements()

        if not topLevelDescriptors:
            raise PyXSDError(
                "invalid XML Schema - the parser could not find any root elements in the schema"
            )

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
                subCls = self._classForRoot(rootElement)
                if subCls is None:
                    return None
                self.generateCorrectSchemaTags()
                contentKind = getattr(subCls, "_contentKind_", None)
                isComplex = (
                    contentKind == "complex"
                    if contentKind is not None
                    else issubclass(subCls, SchemaBase)
                )
                if isComplex:
                    nilled = xsi.xsi_nil_is_true(self.xmlRoot)
                    if nilled and not rootElement.isNillable():
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
                        # is bound.
                        if rootElement.getFixed() is not None:
                            self.report.add_error(
                                f"the root element '{rootName}' is marked nil "
                                "but its declaration has a fixed value",
                                code="nil",
                                element=rootName,
                            )
                        subCls._checkNilContent(self.xmlRoot, rootElementName)
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
                    # The root element's declared type is a primitive
                    # (simple) data type: build a typed instance directly.
                    subInstance = self._primitiveRootInstance(subCls, rootElement)
                # xsi:type may replace the declared root type, so the root
                # instance is stored directly instead of validated against
                # the declared element type.
                schemaClassInstance.__dict__[rootElementName] = subInstance
                subInstance._descriptor_ = rootElement
                self._checkIdentityConstraints(subInstance)

        return subInstance

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
        nilled = xsi.xsi_nil_is_true(self.xmlRoot)
        if nilled and not rootElement.isNillable():
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
                            code="default",
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
                            code="value",
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
        if subCls is None:
            self.report.add_error(
                f"the type of root element '{rootElement.name}' could not be resolved",
                code="unknown-type",
                element=rootElement.name,
            )
            return None

        xsiTypeName = xsi.xsi_type_name(self.xmlRoot)
        if xsiTypeName is None:
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
        reason = is_validly_derived(resolved, subCls, blocked)
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
