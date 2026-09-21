"""Command-line interface for pyxsd."""

import ast
import importlib
import importlib.util
import inspect
import io
import logging
import pkgutil
import re
import sys
import tokenize
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import IO, Any, Literal
from xml.etree import ElementTree as ET

from pyxsd import __version__
from pyxsd.binding import ParseModes
from pyxsd.exceptions import PyXSDError

logger = logging.getLogger(__name__)


def _transform_module_names(className: str) -> list[str]:
    """Return candidate module names for a transform class name.

    Both the historical camelCase convention (``PrintData`` ->
    ``printData``) and snake_case (``PrintData`` -> ``print_data``,
    ``ParseHTMLTree`` -> ``parse_html_tree``) are supported.
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


def _load_module_from_file(moduleName: str, path: Path) -> ModuleType | None:
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


def _load_transform_module_by_normalized_name(className: str) -> ModuleType | None:
    """Find a shipped transform module whose name matches the class.

    The exact-name candidates can miss when the camel-to-snake
    conversion splits acronyms differently than the module file does
    (a ``Schema.parse``/``Document.revalidate`` round-trip transform
    class ``RevalidateXMLTree`` becomes ``revalidate_xml_tree``,
    which will not match a module saved as ``revalidate_xmltree``);
    comparing underscore-free names resolves those cases.
    """
    target = _normalizedModuleName(className)
    transformsPackage = importlib.import_module("pyxsd.transforms")
    for moduleInfo in pkgutil.iter_modules(transformsPackage.__path__):
        if _normalizedModuleName(moduleInfo.name) == target:
            return importlib.import_module(f"pyxsd.transforms.{moduleInfo.name}")
    return None


def _load_transform_file_by_normalized_name(className: str, directory: Path) -> ModuleType | None:
    """Find a transform file in ``directory`` matching the class name."""
    target = _normalizedModuleName(className)
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.suffix != ".py" or _normalizedModuleName(entry.stem) != target:
            continue
        return _load_module_from_file(entry.stem, entry)
    return None


def resolve_transform_class(
    className: str,
    search_paths: Sequence[Path | str] | Path | str | None = None,
) -> type:
    """Loads a transform class from its class name.

    The module it is located in must share the class name, either
    in camelCase with a lowercase first letter (the historical
    convention) or in snake_case. The transform is looked up in
    the installed ``pyxsd.transforms`` package, then in the
    directory the program was called from, then in each directory
    of *search_paths* in order. If no exact module-name candidate
    matches, an underscore-insensitive fallback matches the class
    name against the available modules, so acronym spellings
    (``ParseHTMLTree`` -> ``parse_htmltree``) still resolve.

    - ``className``: a string of the transform class name being
      called

    - ``search_paths``: optional extra directories to search, after
      the transforms package and the working directory (the
      historic per-run xml-directory search)

    Raises ``ImportError`` if the transform class cannot be found,
    and :class:`~pyxsd.exceptions.PyXSDError` when a candidate
    module imports but does not define the class.
    """
    candidates = _transform_module_names(className)
    if search_paths is None:
        paths: list[Path] = []
    elif isinstance(search_paths, (str, Path)):
        paths = [Path(search_paths)]
    else:
        paths = [Path(path) for path in search_paths]

    def classIn(module: ModuleType | None) -> type | None:
        """The class from a resolved module, or ``None`` without one.

        A module that imports but does not define the class is the
        historic fatal error, not a reason to keep searching.
        """
        if module is None:
            return None
        transformCls: type | None = getattr(module, className, None)
        if transformCls is None:
            raise PyXSDError(
                f"the module for the transform '{className}' does not define that class"
            )
        return transformCls

    for fileName in candidates:
        try:
            module = importlib.import_module("pyxsd.transforms." + fileName)
        except ImportError:
            continue
        found = classIn(module)
        if found is not None:
            return found
    found = classIn(_load_transform_module_by_normalized_name(className))
    if found is not None:
        return found
    for directory in [Path.cwd(), *paths]:
        for fileName in candidates:
            candidate = directory / (fileName + ".py")
            if candidate.is_file():
                found = classIn(_load_module_from_file(fileName, candidate))
                if found is not None:
                    return found
        found = classIn(_load_transform_file_by_normalized_name(className, directory))
        if found is not None:
            return found
    raise ImportError(
        f"the transform module for '{className}' could not be found in "
        "pyxsd.transforms or in the search paths"
    )


def parse_transform_call(call: str) -> tuple[str, list[Any], dict[str, Any]]:
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


def _materialize(
    spec: str,
    search_paths: Sequence[Path | str] | Path | str | None = None,
) -> tuple[Any, list[Any], dict[str, Any]]:
    """Resolves one ``Name(args)`` CLI transform spec to a callable.

    A ``Transform`` subclass is wrapped so the callable receives the
    tree root first and forwards the remaining arguments to the
    instance call; any other callable is used as-is (the document
    calls it with the root, then the spec's arguments). A spec whose
    arguments do not fit the transform's signature raises
    :class:`~pyxsd.exceptions.PyXSDError` before any transform runs.

    - ``spec``: one ``TransformClass(arg1, arg2, key=value)`` call
      string

    - ``search_paths``: extra transform-module directories, passed
      through to :func:`resolve_transform_class`

    Returns ``(callable, args, kwargs)``.
    """
    name, args, kwargs = parse_transform_call(spec)
    try:
        obj = resolve_transform_class(name, search_paths=search_paths)
    except ImportError as e:
        raise PyXSDError(f"the transform '{name}' could not be loaded: {e}") from e
    from pyxsd.transforms.transform import Transform

    if isinstance(obj, type) and issubclass(obj, Transform):
        try:
            # The first bind argument stands in for the instance the
            # wrapper constructs; only the spec's arguments are checked.
            inspect.signature(obj.__call__).bind(None, *args, **kwargs)
        except TypeError as e:
            raise PyXSDError(
                f"the transform call '{spec}' does not match the signature of {name}: {e}"
            ) from e
        # ``Any`` rather than ``type[Transform]``: mypy treats a class
        # with an abstract ``__init__`` as not callable.
        transformCls: Any = obj
        return (lambda root, *a, **k: transformCls(root)(*a, **k)), args, kwargs
    return obj, args, kwargs


def _default_transform_output(xml_source: str | IO[str]) -> Path:
    """The default name for the transformed output (the ``-d`` flag).

    The input xml filename with ``Transformed`` appended, or
    ``output.xml`` in the working directory when the instance came
    from a stream.
    """
    if isinstance(xml_source, str):
        source = Path(xml_source)
        return source.parent / (source.stem + "Transformed.xml")
    return Path.cwd() / "output.xml"


def _transform_search_paths(xml_source: str | IO[str]) -> list[Path]:
    """The instance document's directory as an extra transform search path."""
    if isinstance(xml_source, str):
        return [Path(xml_source).resolve().parent]
    return [Path.cwd()]


def _build_document(
    xml_source: str | IO[str],
    xsd: str | Path | IO[str] | None,
    *,
    mode: Any,
    overlay: str | Path | None,
    xsd_version: Literal["1.0", "1.1"] = "1.1",
) -> Any:
    """Compiles the schema and binds the instance into a ``Document``.

    Mirrors the historic engine flow: the instance tree is parsed
    once, its schemaLocation hints are read (a fatal
    :class:`~pyxsd.exceptions.PyXSDError` when no schema was given
    and the instance names none), and the same tree object is bound
    against the compiled schema. The hint findings land on the
    document's report so the CLI surfaces them like any other issue,
    and the instance's prefix bindings are shared with the schema
    compilation.
    """
    # Imported here rather than at module level: pyxsd.schema imports
    # this module, so a module-level import would be circular.
    from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
    from pyxsd.schema import Schema
    from pyxsd.schema_hints import absolute_schema_location_pairs, resolve_schema_hint
    from pyxsd.validation import ValidationReport

    context = NamespaceContext()
    try:
        tree = parse_with_namespaces(xml_source, context)
    except OSError as e:
        raise PyXSDError(f"the xml input could not be read: {e}") from e
    except ET.ParseError as e:
        raise PyXSDError(f"the xml file is not well-formed XML: {e}") from e
    hint_report = ValidationReport()
    base_dir = Path(xml_source).resolve().parent if isinstance(xml_source, str) else Path.cwd()
    _namespace, hint = resolve_schema_hint(tree, base_dir, report=hint_report)
    pairs = absolute_schema_location_pairs(tree, base_dir)
    if xsd is None:
        if hint is None:
            raise PyXSDError(
                "no schema file was given and the xml file has no "
                "schemaLocation or noNamespaceSchemaLocation tag"
            )
        xsd = hint
    schema = Schema.compile(
        xsd,
        mode=mode,
        xsd_version=xsd_version,
        overlay=overlay,
        namespace_context=context,
        # Extra instance schemaLocation pairs are advisory composition
        # hints (strict namespace mode only), like the main hint above.
        schema_location_pairs=pairs,
    )
    document = schema.parse(tree)
    document.report.extend(hint_report)
    return document


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
    parser.add_argument(
        "--xsd-version",
        choices=["1.0", "1.1"],
        default="1.1",
        dest="xsdVersion",
        help="XSD processor version to compile as. '1.1' (default) honors "
        "all vc:* conditional-inclusion selectors against 1.1; '1.0' tests "
        'them against 1.0, so a declaration carrying vc:minVersion="1.1" '
        "is dropped (XSD 1.1 §4.2.2).",
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
        inputXmlFile: str | IO[str] = io.StringIO(sys.stdin.read())
    else:
        inputXmlFile = options.inputXmlFile

    parsedOutputFile = options.parsedOutputFile
    if not options.outputParsed:
        parsedOutputFile = "_No_Output_"

    # Imported here rather than at module level: pyxsd.schema imports
    # this module, so this keeps the CLI's import graph rooted at the
    # entry point.
    from pyxsd.document import Document

    try:
        transforms = []
        if options.transformCall:
            transforms = split_transform_chain(options.transformCall)
        if options.transformFile:
            with open(options.transformFile) as fd:
                transforms = [call for line in fd for call in split_transform_chain(line)]
        mode = (ParseModes.LAX if options.mode == "lax" else ParseModes.STRICT).replace(
            namespaces=options.namespaces
        )
        document = _build_document(
            inputXmlFile,
            options.inputXsdFile,
            mode=mode,
            overlay=options.classFile,
            xsd_version=options.xsdVersion,
        )

        if options.outputParsed and document.root is not None:
            parsedTarget: str | Path | None = parsedOutputFile
            if parsedTarget is None and isinstance(inputXmlFile, str):
                source = Path(inputXmlFile)
                parsedTarget = source.parent / (source.stem + "Parsed.xml")
            if parsedTarget is not None:
                document.write(parsedTarget)

        searchPaths = _transform_search_paths(inputXmlFile)
        lastWasDocument = False
        for spec in transforms:
            fn, args, kwargs = _materialize(spec, search_paths=searchPaths)
            result = document.transform(fn, *args, **kwargs)
            if isinstance(result, Document):
                document = result
                lastWasDocument = True
            else:
                lastWasDocument = False

        if transforms and lastWasDocument:
            transformOutput: str | Path = options.transformOutputFile
            if not transformOutput:
                transformOutput = _default_transform_output(inputXmlFile)
                logger.debug(
                    "Setting the transformed xml file name to the default: %s",
                    transformOutput,
                )
            text = document.to_string()
            if transformOutput == "stdout":
                sys.stdout.write(text)
            else:
                with open(transformOutput, "w") as output:
                    output.write(text)
    except (PyXSDError, ValueError, OSError) as e:
        print(f"pyxsd: error: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    if document.report:
        print(str(document.report), file=sys.stderr)
    if options.strict and document.report.has_errors:
        raise SystemExit(1)
