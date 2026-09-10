"""The PyXSD parser: maps XML and XSD (XML Schema) files into Python.

PyXSD was developed in order to map XML and the related schema (XSD)
files into the Python language. The program builds a pythonic
representation of the XML tree according to the specifications in the
schema and raises non-fatal parser errors whenever possible in order
to help the user validate their XML document. The program allows the
user to specify *transform* classes, which manipulate and transform
the XML tree in various ways. The program then writes the tree back
out to XML. PyXSD allows users to create their own transform classes
with the help of a transform library. These classes are fairly simple
to write, making the system highly adaptable to very specific uses, as
one might find in many scientific applications; however, the program
has potential uses in other fields, since XML is widely used. The
program allows the user to specify the desired transform classes,
along with their arguments and sequence of application, so the user
can create customized tools. The program can be used either as a
standalone command line program or as a library in other programs.

Overview:

- Creates Python classes for all types defined in an XSD schema file
  (xml)

- Reads in an xml file and builds a new pythonic tree according to
  classes. This tree of instances maintains the same overall structure
  of the original xml document.

- Provides some xml/schema parsing with non-fatal errors in order to
  help the user write a valid xml document, without requiring it

- Transforms the pythonic representation according to built-in and
  add-on 'transform' classes that the user specifies

- Sends data to a writer to write the pythonic tree back out to an
  xml file
"""

import ast
import importlib
import importlib.util
import os.path
import pkgutil
import re
import sys
from xml.etree import ElementTree as ET

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.schema_base import SchemaBase
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

__version__ = "1.0.0.dev0"


class PyXSD:
    """Main class of the program that is in charge of data flow.

    Has command line support when it is called as a script.
    """

    def __init__(
        self,
        xmlFileInput,
        xsdFile=None,
        xmlFileOutput=False,
        transformOutputName=None,
        transforms=None,
        classFile=None,
        verbose=False,
        quiet=False,
    ):
        """Initialize the parser.

        - ``xmlFileInput`` - the filename of the xml file to input. Can
          include path information. Will raise an error if not
          specified.

        - ``xsdFile`` - the filename/path information for the schema
          file. Will attempt to use the schemaLocation tag in the xml
          if not specified.

        - ``xmlFileOutput`` - location for xml output to be sent after
          it is parsed. Will use a default name if not specified. Will
          not output if the value is set to ``'_No_Output_'``.

        - ``transformOutputName`` - location of the xml output after
          the transform. Will make a default filename if not specified.

        - ``transforms`` - a list containing the transform calls in the
          order they will be performed.

        - ``classFile`` - the location of the overlay class file.
          Experimental.

        - ``verbose`` - a boolean value. If set to true, will output
          more information.

        - ``quiet`` - a boolean value. If set to true, will output less
          information and errors than normal.
        """
        self.verbose = verbose
        self.quiet = quiet
        self.classes = {}

        if isinstance(xmlFileInput, str):
            self.xmlFileInput = os.path.abspath(xmlFileInput)
            self.xmlPath, self.xmlFileInputName = os.path.split(self.xmlFileInput)
        else:
            self.xmlFileInput = xmlFileInput

        self.xsdFile = xsdFile
        self.xmlFileOutput = xmlFileOutput

        self.xmlRoot = self.getXmlTree()

        if (
            self.xmlFileOutput != "_No_Output_"
            and self.xmlFileOutput is None
            and isinstance(xmlFileInput, str)
        ):
            self.xmlFileOutput = self.getXmlOutputFileName()

        self.transforms = transforms if transforms is not None else []

        if xsdFile is None:
            self.xsdFile = self.getSchemaInfo("l")
            if self.xsdFile is None:
                raise ValueError(
                    "Error: no schema file was given and the xml file has no "
                    "schemaLocation or noNamespaceSchemaLocation tag"
                )
        self.getSchemaFile()
        self.nameSpace = self.getSchemaInfo("n")
        self.parseXSD()

        if classFile:
            if self.verbose:
                print(f"Attempting to load overlay classes from the file '{classFile}'...")
            self.loadClassFromFile(classFile)

        rootInstance = self.parseXML()

        if self.xmlFileOutput != "_No_Output_":
            rootInstance = self.writeParsedXMLFile(rootInstance)

        self.transformOutputName = transformOutputName
        self.executeAndWriteTransforms(rootInstance)

    def executeAndWriteTransforms(self, rootInstance):
        """Runs each transform in order and writes the transformed tree to
        the transform output, if one was requested.
        """
        if self.transforms:
            if self.verbose:
                print("Loading the transforms...")
            transformOutput = None
            if not self.transformOutputName:
                self.transformOutputName = self.getTransformsFileName()
                if self.verbose:
                    print(
                        f"Loading the file '{self.transformOutputName}' for the transformed XML output..."
                    )
                transformOutput = open(self.transformOutputName, "w")  # noqa: SIM115 - held open for the writer pipeline
            elif self.transformOutputName == "stdout":
                transformOutput = sys.stdout
            else:
                if self.verbose:
                    print(
                        f"Loading the file '{self.transformOutputName}' for the transformed XML output..."
                    )
                transformOutput = open(self.transformOutputName, "w")  # noqa: SIM115 - closed after the write below
            transformedRoot = self.transform(self.transforms, rootInstance)
            if transformedRoot:
                if self.verbose:
                    print("Sending transformed tree to the writer...")
                self.writeXML(transformedRoot, transformOutput)
            if transformOutput is not None and transformOutput is not sys.stdout:
                transformOutput.close()

    def getSchemaFile(self):
        """Opens the schema file for reading."""
        try:
            if isinstance(self.xsdFile, str):
                self.xsdFile = open(self.xsdFile)  # noqa: SIM115 - held open for the writer pipeline
        except OSError as e:
            print("Program Error: the schema file could not be opened.")
            print("The program's error message is as follows:")
            print(f"   {e}")
            raise

    def writeParsedXMLFile(self, rootInstance):
        """Writes the parsed (pre-transform) xml file, if requested."""
        if isinstance(self.xmlFileOutput, str):
            self.xmlFileOutput = open(self.xmlFileOutput, "w")  # noqa: SIM115 - closed after the write below
        if self.xmlFileOutput:
            self.writeXML(rootInstance, self.xmlFileOutput)
            if self.xmlFileOutput is not sys.stdout:
                self.xmlFileOutput.close()
        return rootInstance

    def parseXSD(self):
        """Reads the given xsd file and creates a set of classes that
        correspond to the complex and simple type definitions.
        """
        if self.verbose:
            print("Sending the schema file to the ElementTree Parser...")

        tree = ET.parse(self.xsdFile)
        root = tree.getroot()
        if self.verbose:
            print("Sending the schema ElementTree to the ElementRepresentative module...")

        schemaER = ElementRepresentative.factory(root, None)

        for simpleType in schemaER.simpleTypes.values():
            cls = simpleType.clsFor(self)
            self.classes[simpleType.name] = cls
            if self.verbose:
                print(f"Class created for the {simpleType.name} type...")
        for complexType in schemaER.complexTypes.values():
            cls = complexType.clsFor(self)
            self.classes[complexType.name] = cls
            if self.verbose:
                print(f"Class created for the {complexType.name} type...")

        return None

    def parseXML(self):
        """Reads the given xml file in the context of the xsd file.

        Produces instances of the above classes. Does validation.
        Returns a schema instance object.
        """
        if self.verbose:
            print("Starting to parse the xml file.")

        schemaClass = self.getClasses()["schema"]

        schemaClassInstance = schemaClass()

        rootName = self.xmlRoot.tag.split("}")[-1]

        topLevelDescriptors = schemaClassInstance._getElements()

        if len(topLevelDescriptors) > 1 and not self.quiet:
            print("Error: Invalid XML Schema-there is more than one root element in this document.")
            print(f"There are {len(topLevelDescriptors)!r} root elements in this document:")
            for element in topLevelDescriptors:
                print(element.name)
            print(
                f"The parser will proceed and attempt to parse only {topLevelDescriptors[0].name!r}"
            )
            print()

        if not topLevelDescriptors:
            raise ValueError(
                "Error: Invalid XML Schema-the parser could not find any root "
                "elements in the schema"
            )

        rootElement = topLevelDescriptors[0]
        rootElementName = rootElement.name

        subInstance = None
        if rootElementName == rootName:
            subCls = rootElement.getType()
            self.generateCorrectSchemaTags()
            subInstance = subCls.makeInstanceFromTag(self.xmlRoot)
            setattr(schemaClassInstance, rootElementName, subInstance)

        return subInstance

    def generateCorrectSchemaTags(self):
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

    def writeXML(self, rootInstance, output):
        """Sends a pythonic instance tree to the tree writer.

        - ``rootInstance``: the root instance of a tree. Must be
          formatted in the program's tree structure.

        - ``output``: the file object to write the tree to.
        """
        if isinstance(output, str):
            output = open(output, "w")  # noqa: SIM115 - flushed below
        XmlTreeWriter(rootInstance, output)
        output.flush()
        if self.verbose:
            print("Data sent to the writer...")

    def getClasses(self):
        """Returns the dictionary of classes created by
        ElementRepresentative for each type specified in the schema.
        """
        return self.classes

    def loadClassFromFile(self, classFile):
        """Loads a file with overlay classes into the class dictionary.

        Overlay classes add to and override the schema type classes to
        allow for a user to create their own types without changing the
        schema file itself.

        **Consider this functionality experimental.**

        - ``classFile``: a string that specifies the location of a
          user-created overlay class file
        """
        filePath = classFile
        if not os.path.isfile(filePath):
            # Fall back to the historical behavior of resolving a
            # module name against the xml file's directory.
            candidate = os.path.join(getattr(self, "xmlPath", ""), classFile + ".py")
            if os.path.isfile(candidate):
                filePath = candidate
            else:
                raise ImportError(
                    f"the file '{classFile}' was not found. Please check your spelling."
                )
        module_name = os.path.splitext(os.path.basename(filePath))[0]
        spec = importlib.util.spec_from_file_location(module_name, filePath)
        if spec is None or spec.loader is None:
            raise ImportError(f"the file '{classFile}' could not be loaded.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        newClasses = {}
        for _varName, var in vars(module).items():
            if isinstance(var, type) and issubclass(var, SchemaBase) and var is not SchemaBase:
                className = getattr(var, "name", None)
                if className is None:
                    if not self.quiet:
                        print(
                            f"Load Error: the class {var} must have a 'name' "
                            "attribute. Will attempt to use '__name__' instead."
                        )
                    className = var.__name__
                newClasses[className] = var
                if self.verbose:
                    print(f"Loaded the {className} class")
        self.classes.update(newClasses)

    def getXmlTree(self):
        """Sends the xml file into the ElementTree library's parser.

        Allows for the program to get the schemaLocation before parsing
        the xml against the schema.
        """
        if self.verbose:
            print("The XML file is being parsed by the ElementTree library...")
        tree = ET.parse(self.xmlFileInput)
        if self.verbose:
            print("XML file parsed by the ElementTree library successfully...")
        return tree.getroot()

    def getXmlOutputFileName(self):
        """Creates a default name for the xml file that is parsed without
        any transforms.  Uses the name from the input xml file.
        """
        inputName = self.xmlFileInput
        path, inputName = os.path.split(inputName)
        nonExtensionName = inputName.rsplit(".", 1)[0] if "." in inputName else inputName
        nonExtensionName += "Parsed"
        return os.path.join(path, nonExtensionName + ".xml")

    def getTransformModuleAndLoad(self, className):
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
        searchPaths = [os.getcwd()]
        xmlPath = getattr(self, "xmlPath", None)
        if xmlPath:
            searchPaths.append(xmlPath)
        for directory in searchPaths:
            for fileName in candidates:
                candidate = os.path.join(directory, fileName + ".py")
                if os.path.isfile(candidate):
                    spec = importlib.util.spec_from_file_location(fileName, candidate)
                    if spec is None or spec.loader is None:
                        continue
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    return module
            module = _loadTransformFileByNormalizedName(className, directory)
            if module is not None:
                return module
        raise ImportError(
            f"the transform module for '{className}' could not be found in "
            "pyxsd.transforms or in the search paths"
        )

    def transform(self, transforms, root):
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
            if self.verbose:
                print(f"Starting the transform '{class_name}' with the following args: {argDesc}")
            transformCls = getattr(transformer, class_name)
            currentRoot = transformCls(currentRoot)(*args, **kwargs)

        return currentRoot

    def getTransformsFileName(self):
        """Creates a default name for the xml file that is written after
        all of the transforms.  Uses the name from the input xml file.
        """
        inputName = self.xmlFileInputName
        if inputName is None:
            inputName = "output"
            path = os.getcwd()
        else:
            path = self.xmlPath
            nonExtensionName = inputName.rsplit(".", 1)[0] if "." in inputName else inputName
            inputName = nonExtensionName + "Transformed"
        newName = inputName + ".xml"
        newName = os.path.join(path, newName)
        if self.verbose:
            print("Setting the transformed xml file name to the default:", newName)
        return newName

    def getSchemaInfo(self, nameOrLocation):
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
            if os.linesep in schemaLocationTag:
                schemaLocationSplit = schemaLocationTag.split("\n")
            else:
                schemaLocationSplit = schemaLocationTag.split(" ")

            if len(schemaLocationSplit) != 2:
                print(
                    "Parser Error: the 'schemaLocation' tag must be a pair of values separated by a space or line break"
                )
                print("with the namespace stated first, followed by the location of the schema.")
                print(
                    "The program will attempt to use the 'noNamespaceSchemaLocation' tag instead."
                )
                print()
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

    def makeFullName(self, ns, text):
        """Makes a string that looks similar to some of the names in
        ElementTree when it contains namespace information.

        - ``ns``: a string of the namespace used. For this function,
          this variable is usually set to a url.

        - ``text``: a string of the name of the tag that the full name
          is being created for.
        """
        return f"{{{ns}}}{text}"


def _transformModuleNames(className):
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


def _normalizedModuleName(name):
    """Reduce a module or class name for underscore-insensitive match."""
    return name.replace("_", "").lower()


def _loadTransformModuleByNormalizedName(className):
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


def _loadTransformFileByNormalizedName(className, directory):
    """Find a transform file in ``directory`` matching the class name."""
    target = _normalizedModuleName(className)
    try:
        entries = os.listdir(directory)
    except OSError:
        return None
    for entry in entries:
        stem, ext = os.path.splitext(entry)
        if ext != ".py" or _normalizedModuleName(stem) != target:
            continue
        spec = importlib.util.spec_from_file_location(stem, os.path.join(directory, entry))
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


def parseTransformCall(call):
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
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in tree.body.keywords}
    return class_name, args, kwargs


def main(argv=None):
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
        help="use verbose mode. Experts only. (limited functionality)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=False,
        help="use quiet mode. Fewer errors reported. (limited functionality)",
    )

    options = parser.parse_args(argv)

    if options.transformDefaultOutput and options.transformOutputFile == "stdout":
        options.transformOutputFile = None

    if options.inputXmlFile == "stdin":
        if sys.stdin.isatty():
            parser.error(
                "if no input xml file is specified, the xml must be fed in "
                "through stdin (i.e. pipes)"
            )
        inputXmlFile = "stdin.xml"
        with open(inputXmlFile, "w") as newFile:
            newFile.write(sys.stdin.read())
    else:
        inputXmlFile = options.inputXmlFile

    if options.transformCall and options.transformFile:
        parser.error("A transform file and a transform call cannot both be specified.")

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

    if options.quiet and options.verbose:
        parser.error("Both verbose mode and quiet mode cannot be on at the same time")

    PyXSD(
        inputXmlFile,
        options.inputXsdFile,
        parsedOutputFile,
        options.transformOutputFile,
        transforms,
        options.classFile,
        options.verbose,
        options.quiet,
    )


if __name__ == "__main__":
    main()
