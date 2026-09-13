"""Engines that turn a test case into a schema/instance verdict.

Two drivers implement the same contract: PyXSD (under the standards-oriented
``NAMESPACED`` binding policy) and ``xmlschema`` as an independent oracle.
Both consume a single schema path; a group whose ``schemaTest`` lists several
documents is materialised as an otherwise-empty driver schema that
includes/imports the listed documents in order.

The driver only reports what it observed.  Comparing that to the suite's
expectation is :mod:`tests.xsts.outcomes`.
"""

from __future__ import annotations

import contextlib
import io
import signal
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from pyxsd.binding import ParseModes
from pyxsd.exceptions import PyXSDError
from pyxsd.parser import PyXSD
from pyxsd.validation import IssueSeverity

from .catalog import DocumentRef
from .outcomes import EngineResult

#: Default per-case wall-clock budget, in seconds.
DEFAULT_TIMEOUT = 30.0


class HarnessError(Exception):
    """Raised when the harness cannot prepare a case for a driver."""


def _errors(report: object, phase: str) -> list[object]:
    issues = report.for_phase(phase)  # type: ignore[attr-defined]
    return [issue for issue in issues if issue.severity is IssueSeverity.ERROR]


@contextlib.contextmanager
def time_limit(seconds: float | None) -> Iterator[None]:
    """Raise :class:`TimeoutError` if the block exceeds *seconds*.

    Uses ``SIGALRM`` on the main thread.  Where that is unavailable (not the
    main thread, Windows) the limit is skipped rather than silently wrong.
    """
    if seconds is None or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handle(signum: int, frame: object) -> None:
        raise TimeoutError(f"case exceeded {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, handle)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _target_namespace(path: Path) -> str | None:
    """The ``targetNamespace`` of a schema document, or ``None`` for a chameleon."""
    try:
        for _, element in ET.iterparse(str(path), events=("start",)):
            local = element.tag.rsplit("}", 1)[-1]
            if local != "schema":
                raise HarnessError(f"{path}: root element is <{local}>, not <schema>")
            return element.get("targetNamespace")
    except ET.ParseError as exc:
        raise HarnessError(f"{path}: not well-formed: {exc}") from exc
    raise HarnessError(f"{path}: empty document")


def build_bundle(
    corpus_root: PurePosixPath,
    documents: tuple[DocumentRef, ...],
    workdir: Path,
) -> Path:
    """Return a schema path that loads every document in *documents*.

    A single document is returned directly.  Several are combined by an
    otherwise-empty driver schema that imports namespaced documents and
    includes chameleons, preserving the listed order.  Absolute
    ``schemaLocation`` values are used so each document's own relative
    includes still resolve against its real location.
    """
    if not documents:
        raise HarnessError("schemaTest lists no schema documents")
    root = Path(corpus_root)
    paths = [root / document.path for document in documents]
    for path in paths:
        if not path.is_file():
            raise HarnessError(f"schema document is missing: {path}")
    if len(paths) == 1:
        return paths[0]

    lines = [
        '<?xml version="1.0"?>',
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">',
    ]
    for path in paths:
        location = str(path.resolve())
        namespace = _target_namespace(path)
        if namespace is None:
            lines.append(f'  <xs:include schemaLocation="{location}"/>')
        else:
            lines.append(
                f'  <xs:import namespace="{namespace}" schemaLocation="{location}"/>'
            )
    lines.append("</xs:schema>")
    driver = workdir / "driver.xsd"
    driver.write_text("\n".join(lines), encoding="utf-8")
    return driver


def _schema_only_call(schema_path: Path, timeout: float | None) -> EngineResult:
    """Compile *schema_path* with PyXSD without binding an instance.

    PyXSD always binds an instance during construction, so instance binding
    is stubbed out and only the schema phase is observed.
    """
    result = EngineResult()
    original = PyXSD.parseXML
    PyXSD.parseXML = lambda self: None  # type: ignore[method-assign]
    try:
        with time_limit(timeout):
            parser = PyXSD(
                io.StringIO("<pyxsd-schema-probe/>"),
                str(schema_path),
                xmlFileOutput=False,
                mode=ParseModes.NAMESPACED,
            )
        result.schema_valid = not _errors(parser.report, "schema")
    except PyXSDError as exc:
        result.schema_valid = False
        result.schema_error = str(exc)
    except TimeoutError as exc:
        result.timeout = True
        result.schema_error = str(exc)
    finally:
        PyXSD.parseXML = original  # type: ignore[method-assign]
    return result


class PyXSDDriver:
    """Run cases with PyXSD under the ``NAMESPACED`` standards policy."""

    name = "pyxsd"

    def __init__(self, timeout: float | None = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def compile_schema(self, schema_path: Path) -> EngineResult:
        """Observe only the schema phase."""
        return _schema_only_call(schema_path, self.timeout)

    def validate(self, schema_path: Path, instance_path: Path) -> EngineResult:
        """Observe both phases for an instance document."""
        result = EngineResult()
        try:
            with time_limit(self.timeout):
                parser = PyXSD(
                    str(instance_path),
                    str(schema_path),
                    xmlFileOutput=False,
                    mode=ParseModes.NAMESPACED,
                )
        except PyXSDError as exc:
            schema = self.compile_schema(schema_path)
            if schema.schema_valid is False:
                result.schema_valid = False
                result.schema_error = schema.schema_error or str(exc)
                result.adapter_gap = "group schema did not compile"
            else:
                result.schema_valid = True
                result.instance_valid = False
                result.instance_error = str(exc)
            if schema.timeout:
                result.timeout = True
            return result
        except TimeoutError as exc:
            result.timeout = True
            result.instance_error = str(exc)
            return result
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        result.schema_valid = not _errors(parser.report, "schema")
        if result.schema_valid:
            result.instance_valid = not _errors(parser.report, "instance")
        else:
            result.instance_valid = None
            result.adapter_gap = "group schema did not compile"
        warnings = parser.report.warnings
        if warnings:
            result.instance_error = warnings[0].format()
        return result


class XmlSchemaDriver:
    """Run cases with ``xmlschema`` as an independent oracle."""

    name = "xmlschema"

    def __init__(self, profile_name: str, timeout: float | None = DEFAULT_TIMEOUT) -> None:
        self.profile_name = profile_name
        self.timeout = timeout

    def _schema_class(self) -> Callable[..., object]:
        try:
            import xmlschema
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise HarnessError("the xmlschema oracle is not installed") from exc
        if self.profile_name == "xsd11":
            return xmlschema.XMLSchema11
        return xmlschema.XMLSchema10

    def compile_schema(self, schema_path: Path) -> EngineResult:
        result = EngineResult()
        schema_class = self._schema_class()
        try:
            with time_limit(self.timeout):
                schema_class(str(schema_path))
        except HarnessError:
            raise
        except Exception as exc:
            from xmlschema import XMLSchemaException

            if isinstance(exc, XMLSchemaException):
                result.schema_valid = False
                result.schema_error = f"{type(exc).__name__}: {exc}"
            else:
                result.error = f"{type(exc).__name__}: {exc}"
            return result
        result.schema_valid = True
        return result

    def validate(self, schema_path: Path, instance_path: Path) -> EngineResult:
        result = self.compile_schema(schema_path)
        if result.error is not None or result.timeout:
            return result
        if result.schema_valid is False:
            result.adapter_gap = "group schema did not compile"
            return result
        try:

            schema_class = self._schema_class()
            schema = schema_class(str(schema_path))
            with time_limit(self.timeout):
                result.instance_valid = bool(schema.is_valid(str(instance_path)))  # type: ignore[attr-defined]
        except TimeoutError as exc:
            result.timeout = True
            result.instance_error = str(exc)
        except Exception as exc:
            from xmlschema import XMLSchemaException as _XMLSchemaException

            if isinstance(exc, _XMLSchemaException):
                result.instance_valid = False
                result.instance_error = f"{type(exc).__name__}: {exc}"
            else:
                result.error = f"{type(exc).__name__}: {exc}"
        return result
