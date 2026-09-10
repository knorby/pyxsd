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
import io
import logging
import os.path
import pkgutil
import re
import sys
import warnings
from pathlib import Path
from types import ModuleType
from typing import IO, Any
from xml.etree import ElementTree as ET

from pyxsd import __version__, xsi
from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.exceptions import PyXSDError, PyXSDWarning
from pyxsd.schema_base import SchemaBase
from pyxsd.validation import ValidationReport
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

logger = logging.getLogger(__name__)

# Schema components that may be spliced in from included/imported
# schemas before the ElementRepresentative run.
_COMPOSABLE_TAGS = {"element", "complexType", "simpleType", "group", "attributeGroup"}


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
    xsdFile: str | Path | os.PathLike[str] | None
    xmlFileOutput: str | Path | bool

    def __init__(
        self,
        xmlFileInput: str | Path | os.PathLike[str] | IO[str],
        xsdFile: str | Path | os.PathLike[str] | None = None,
        xmlFileOutput: str | bool = False,
        transformOutputName: str | None = None,
        transforms: list[str] | None = None,
        classFile: str | Path | os.PathLike[str] | None = None,
        verbose: bool = False,
        quiet: bool = False,
    ):
        """Initialize the parser and run the whole pipeline.

        - ``xmlFileInput`` - the filename of the xml file to input (a
          string or path); a file object open for reading is also
          accepted. Will raise an error if not specified.

        - ``xsdFile`` - the filename/path information for the schema
          file. Will attempt to use the schemaLocation tag in the xml
          if not specified.

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

        After construction, ``self.report`` holds the
        :class:`~pyxsd.validation.ValidationReport` collected while the
        instance document was bound.
        """
        self.verbose = verbose
        self.quiet = quiet
        self.classes: dict[str, type[SchemaBase]] = {}
        self.report = ValidationReport()

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
        self.nameSpace = self.getSchemaInfo("n")
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

    def parseXSD(self) -> None:
        """Reads the given xsd file and creates a set of classes that
        correspond to the complex and simple type definitions.
        """
        logger.debug("Sending the schema file to the ElementTree Parser...")

        if isinstance(self.xsdFile, (str, os.PathLike)):
            try:
                with open(self.xsdFile, "rb") as schemaFile:
                    tree = ET.parse(schemaFile)
            except OSError as e:
                raise PyXSDError(f"the schema file could not be opened: {e}") from e
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        else:
            # The schema must be a file object here: a ``None`` schema
            # without a location hint is rejected in ``__init__``.
            assert self.xsdFile is not None
            try:
                tree = ET.parse(self.xsdFile)
            except ET.ParseError as e:
                raise PyXSDError(f"the schema file is not well-formed XML: {e}") from e
        root = tree.getroot()
        logger.debug("Sending the schema ElementTree to the ElementRepresentative module...")

        baseDir, visited = self._schemaCompositionContext()
        self._spliceComposedSchemas(root, baseDir, visited)

        schemaER = ElementRepresentative.factory(root, None)
        # Attach the parser to the schema ER so class building can
        # record schema-reference problems (group/attributeGroup
        # references) on the validation report.
        schemaER.pyXSD = self

        for simpleType in schemaER.simpleTypes.values():
            cls = simpleType.clsFor(self)
            self.classes[simpleType.name] = cls
            logger.debug("Class created for the %s type...", simpleType.name)
        for complexType in schemaER.complexTypes.values():
            cls = complexType.clsFor(self)
            self.classes[complexType.name] = cls
            logger.debug("Class created for the %s type...", complexType.name)

        self._buildSubstitutionGroups(schemaER)

        return None

    def _buildSubstitutionGroups(self, schemaER: Any) -> None:
        """Maps substitution-group heads to their member elements.

        XSD 1.0 declares substitution groups on global element
        declarations: a member element carries ``substitutionGroup``
        naming its head. After the ER run, every member is recorded
        under its head's local name so instance parsing can dispatch
        member elements wherever the head is allowed. Heads that name
        no global element are recorded as schema errors.
        """
        declaredNames = {element.name for element in schemaER.elements}
        for element in schemaER.elements:
            head = element.getSubstitutionGroupHead()
            if head is None:
                continue
            if head not in declaredNames:
                self.report.add_error(
                    f"element '{element.name}' declares substitutionGroup "
                    f"'{head}', but no global element with that name exists",
                    code="unknown-substitution-head",
                    element=element.name,
                )
                continue
            schemaER.substitutionGroups.setdefault(head, []).append(element)
        logger.debug("Substitution groups built: %s", list(schemaER.substitutionGroups))

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
                if child.get("namespace") == "http://www.w3.org/2001/XMLSchema":
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
            self.report.add_error(
                f"an {'import' if isImport else 'include'} tag has no "
                "schemaLocation; the schema could not be composed",
                code="schema-compose",
            )
            return None
        includedRoot = self._parseIncludedSchema(location, baseDir)
        if includedRoot is None:
            return None
        includedPath = (baseDir / location).resolve()
        if str(includedPath) in visited:
            self.report.add_error(
                f"the schema '{location}' is already being composed; "
                "circular include/import relationships are not allowed",
                code="compose-cycle",
            )
            return None
        mainNS = schemaRoot.get("targetNamespace")
        includedNS = includedRoot.get("targetNamespace")
        if not isImport and includedNS not in (None, mainNS):
            self.report.add_error(
                f"the schema '{location}' declares targetNamespace "
                f"'{includedNS}', which does not match the including "
                f"schema's namespace ({mainNS or 'none'})",
                code="compose-namespace",
            )
        if isImport and mainNS and includedNS != mainNS:
            logger.warning(
                "imported schema '%s' declares targetNamespace '%s'; pyxsd "
                "matches names by local name, so its components are merged "
                "regardless of the namespace difference",
                location,
                includedNS or "none",
            )
        self._spliceComposedSchemas(
            includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
        self._appendNamedComponents(includedRoot, schemaRoot)
        return None

    def _parseIncludedSchema(self, location: str, baseDir: Path) -> Any | None:
        """Parses one included schema file; returns its root or ``None``.

        Failures (unreadable file, malformed xml) are recorded as
        ``schema-compose`` errors and the composition proceeds without
        the missing file.
        """
        includedPath = baseDir / location
        try:
            with open(includedPath, "rb") as includedFile:
                tree = ET.parse(includedFile)
        except OSError as e:
            self.report.add_error(
                f"the schema '{location}' could not be opened: {e}",
                code="schema-compose",
            )
            return None
        except ET.ParseError as e:
            self.report.add_error(
                f"the schema '{location}' is not well-formed XML: {e}",
                code="schema-compose",
            )
            return None
        return tree.getroot()

    def _appendNamedComponents(self, includedRoot: Any, schemaRoot: Any) -> None:
        """Appends the named components of an included schema to the main tree."""
        for component in list(includedRoot):
            if component.tag.split("}")[-1] in _COMPOSABLE_TAGS:
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
            self.report.add_error(
                f"the schema '{location}' is already being composed; "
                "circular include/redefine relationships are not allowed",
                code="compose-cycle",
            )
            return None
        redefinedNames = set()
        for child in list(redefineTag):
            local = child.tag.split("}")[-1]
            if local in ("complexType", "simpleType", "group") and child.get("name"):
                redefinedNames.add(child.get("name"))
        for component in list(includedRoot):
            local = component.tag.split("}")[-1]
            name = component.get("name")
            if local in ("complexType", "simpleType", "group") and name in redefinedNames:
                component.set("name", f"{name}|base")
        self._spliceComposedSchemas(
            includedRoot, includedPath.parent, visited | {str(includedPath)}
        )
        self._appendNamedComponents(includedRoot, schemaRoot)
        for child in list(redefineTag):
            for element in child.iter():
                base = element.get("base")
                if base and base.split(":")[-1] in redefinedNames:
                    element.set("base", f"{base.split(':')[-1]}|base")
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

    def parseXML(self) -> Any:
        """Reads the given xml file in the context of the xsd file.

        Produces instances of the above classes. Does validation.
        Returns a schema instance object.
        """
        logger.debug("Starting to parse the xml file.")

        schemaClass = self.getClasses()["schema"]

        schemaClassInstance = schemaClass()

        rootName = self.xmlRoot.tag.split("}")[-1]

        topLevelDescriptors = schemaClassInstance._getElements()

        if not topLevelDescriptors:
            raise PyXSDError(
                "invalid XML Schema - the parser could not find any root elements in the schema"
            )

        matching = [descriptor for descriptor in topLevelDescriptors if descriptor.name == rootName]

        if len(matching) > 1:
            elementNames = ", ".join(element.name for element in matching)
            self.report.add_error(
                "invalid schema: there is more than one global element named "
                f"'{rootName}' ({elementNames}); parsing only '{matching[0].name}'",
                code="multiple-roots",
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
            subCls = self._classForRoot(rootElement)
            self.generateCorrectSchemaTags()
            if issubclass(subCls, SchemaBase):
                subInstance = subCls.makeInstanceFromTag(self.xmlRoot)
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
        """
        rootName = self.xmlRoot.tag.split("}")[-1]
        if xsi.xsi_nil_is_true(self.xmlRoot) and not rootElement.isNillable():
            self.report.add_error(
                f"the root element '{rootName}' is not nillable but carries xsi:nil",
                code="nil",
                element=rootName,
            )
            text = ""
        else:
            text = "".join(self.xmlRoot.itertext()).strip()
        try:
            value = dataTypeClass(text) if text else None
        except (TypeError, ValueError) as exc:
            self.report.add_error(
                f"the root element '{rootName}' has an invalid "
                f"{getattr(dataTypeClass, 'name', dataTypeClass.__name__)} value: {exc}",
                code="value",
                element=rootName,
            )
            value = None
        instance = dataTypeClass._unvalidated() if value is None else value
        instance._name_ = rootName
        instance._attribs_ = {
            xsi.xsi_attr_key(key): val for key, val in self.xmlRoot.attrib.items()
        }
        instance._value_ = [text] if text else None
        instance._children_ = []
        return instance

    def _classForRoot(self, rootElement: Any) -> type[SchemaBase]:
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

        xsiTypeName = xsi.xsi_type_name(self.xmlRoot)
        if xsiTypeName is None:
            return subCls

        resolved = ElementRepresentative.typeFromName(xsiTypeName, self)
        if resolved is None:
            self.report.add_error(
                f"xsi:type '{xsiTypeName}' on the root element does not "
                "correspond to a type in the schema",
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
            tree = ET.parse(self.xmlFileInput)
        except ET.ParseError as e:
            raise PyXSDError(f"the xml file is not well-formed XML: {e}") from e
        logger.debug("XML file parsed by the ElementTree library successfully...")
        return tree.getroot()

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
            transformer = self.getTransformModuleAndLoad(class_name)
            argDesc = repr(args)
            if kwargs:
                argDesc += " " + repr(kwargs)
            logger.debug(
                "Starting the transform '%s' with the following args: %s",
                class_name,
                argDesc,
            )
            transformCls = getattr(transformer, class_name)
            currentRoot = transformCls(currentRoot)(*args, **kwargs)

        return currentRoot

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
            if "\n" in schemaLocationTag:
                schemaLocationSplit = schemaLocationTag.split("\n")
            else:
                schemaLocationSplit = schemaLocationTag.split(" ")

            if len(schemaLocationSplit) != 2:
                report = getattr(self, "report", None)
                message = (
                    "the 'schemaLocation' tag must be a pair of values "
                    "separated by a space or line break, with the namespace "
                    "stated first, followed by the location of the schema; "
                    "attempting to use the 'noNamespaceSchemaLocation' tag "
                    "instead"
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

        schemaLocation = schemaLocationSplit[-1]
        if nameOrLocation == "l":
            return schemaLocation

        if nameOrLocation == "t":
            return self.makeFullName(xsiNS, "schemaLocation")

        return None

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

    transforms = []

    if options.transformCall:
        if ">" in options.transformCall:
            transforms = [t.strip().strip(">").strip() for t in options.transformCall.split(">")]
        else:
            transforms.append(options.transformCall)
    if options.transformFile:
        with open(options.transformFile) as fd:
            transforms = [t.strip().strip(">").strip() for t in fd]

    parsedOutputFile = options.parsedOutputFile
    if not options.outputParsed:
        parsedOutputFile = "_No_Output_"

    try:
        app = PyXSD(
            inputXmlFile,
            options.inputXsdFile,
            parsedOutputFile,
            options.transformOutputFile,
            transforms,
            options.classFile,
            options.verbose,
            options.quiet,
        )
    except (PyXSDError, OSError) as e:
        print(f"pyxsd: error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if app.report:
        print(str(app.report), file=sys.stderr)
    if options.strict and app.report.has_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
