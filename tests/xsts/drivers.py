"""Engines that turn a test case into a schema/instance verdict.

Two drivers implement the same contract: pyxsd (under the standards-oriented
``NAMESPACED`` binding policy) and ``xmlschema`` as an independent oracle.
Both consume a single schema path; a group whose ``schemaTest`` lists several
documents is materialised as an otherwise-empty driver schema that
includes/imports the listed documents in order.

The driver only reports what it observed.  Comparing that to the suite's
expectation is :mod:`tests.xsts.outcomes`.
"""

from __future__ import annotations

import contextlib
import signal
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import quoteattr

from pyxsd.binding import ParseModes
from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import NamespaceContext, parse_with_namespaces
from pyxsd.schema import Schema
from pyxsd.schema_hints import absolute_schema_location_pairs
from pyxsd.validation import IssueSeverity

from .catalog import DocumentRef
from .outcomes import EngineResult

#: Default per-case wall-clock budget, in seconds.
DEFAULT_TIMEOUT = 30.0

_XSD_NS = "http://www.w3.org/2001/XMLSchema"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_COMPOSITION_ELEMENTS = frozenset({"include", "import", "redefine", "override"})


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


def _composition_targets(path: Path) -> list[Path]:
    """Resolve the include/import/redefine/override targets of *path*.

    Unreadable or non-schema documents yield no targets; the engine, not the
    harness, reports a malformed document.  A target is returned whether or
    not it is itself a bundle member.
    """
    try:
        schema = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return []
    if schema.tag.rsplit("}", 1)[-1] != "schema":
        return []
    targets: list[Path] = []
    for child in schema:
        if child.tag.rsplit("}", 1)[-1] not in _COMPOSITION_ELEMENTS:
            continue
        location = child.get("schemaLocation")
        if not location:
            continue
        target = Path(location)
        if not target.is_absolute():
            target = path.parent / target
        targets.append(target.resolve())
    return targets


def _composition_reachable(start: Path, listed: set[Path]) -> set[Path]:
    """The *listed* documents reachable from *start* through composition."""
    start = start.resolve()
    reachable: set[Path] = set()
    seen: set[Path] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for target in _composition_targets(current):
            if target in listed:
                reachable.add(target)
            stack.append(target)
    reachable.discard(start)
    return reachable


def _principal_documents(paths: list[Path]) -> list[Path]:
    """The subset of *paths* the wrapper must load to compose the whole set.

    A document that another bundle member already composes — as its
    ``include``, ``import``, ``redefine`` or ``override`` target, which is
    what the catalogue's ``schemaDocument/@role`` records — is redundant:
    loading it again declares its global components twice and makes the
    wrapper schema invalid (e.g. ``multiple-roots``).  Documents that compose
    one another cyclically form a single load unit; the first-listed member
    of each unit that nothing outside it reaches is kept.  Independent
    documents are all kept.
    """
    resolved = [path.resolve() for path in paths]
    listed = set(resolved)
    reach = {path: _composition_reachable(path, listed) for path in resolved}
    loaded: list[Path] = []
    loaded_units: list[frozenset[Path]] = []
    for path in paths:
        document = path.resolve()
        unit = frozenset(
            other
            for other in resolved
            if other == document or (other in reach[document] and document in reach[other])
        )
        if any(document in reach[other] for other in resolved if other not in unit):
            continue
        if unit in loaded_units:
            continue
        loaded_units.append(unit)
        loaded.append(path)
    return loaded


def build_bundle(
    corpus_root: PurePosixPath,
    documents: tuple[DocumentRef, ...],
    workdir: Path,
) -> Path:
    """Return a schema path that loads every document in *documents*.

    A single document is returned directly.  Several are combined by an
    otherwise-empty driver schema that imports namespaced documents and
    includes chameleons, preserving the listed order.  Documents another
    member already composes are not loaded separately (see
    :func:`_principal_documents`).  Absolute ``schemaLocation`` values are
    used so each document's own relative includes still resolve against its
    real location.
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

    selected = _principal_documents(paths)
    lines = [
        '<?xml version="1.0"?>',
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">',
    ]
    for path in selected:
        location = str(path.resolve())
        namespace = _target_namespace(path)
        if namespace is None:
            lines.append(f'  <xs:include schemaLocation="{location}"/>')
        else:
            lines.append(f'  <xs:import namespace="{namespace}" schemaLocation="{location}"/>')
    lines.append("</xs:schema>")
    driver = workdir / "driver.xsd"
    driver.write_text("\n".join(lines), encoding="utf-8")
    return driver


def _split_expanded_name(tag: str) -> tuple[str | None, str]:
    """Split an ElementTree tag into ``(namespace, local-name)``."""
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return None, tag


def _instance_schema_hints(instance_path: Path) -> list[tuple[str | None, Path]]:
    """Resolve an instance's ``xsi`` schema-location hints to existing files.

    Only hints that resolve to an on-disk document are returned; an
    unresolved hint is left for the engine to report as it sees fit.
    """
    try:
        root = ET.parse(str(instance_path)).getroot()
    except (OSError, ET.ParseError):
        return []
    raw: list[tuple[str | None, str]] = []
    no_namespace = root.get(f"{{{_XSI_NS}}}noNamespaceSchemaLocation")
    if no_namespace:
        raw.append((None, no_namespace))
    location = root.get(f"{{{_XSI_NS}}}schemaLocation")
    if location:
        parts = location.split()
        for index in range(0, len(parts) - 1, 2):
            raw.append((parts[index] or None, parts[index + 1]))
    resolved: list[tuple[str | None, Path]] = []
    for namespace, href in raw:
        path = Path(href)
        if not path.is_absolute():
            path = instance_path.parent / path
        if path.is_file():
            resolved.append((namespace, path.resolve()))
    return resolved


def _schema_provides_root(
    path: Path,
    context_namespace: str | None,
    root_namespace: str | None,
    local: str,
    seen: set[Path],
) -> bool:
    """Whether a schema document (or its composition graph) declares *local*.

    Follows ``include``/``import``/``redefine``/``override`` so a wrapper can
    tell when an instance's own schema hint already supplies the root element
    declaration, and therefore must not be duplicated.
    """
    path = path.resolve()
    if path in seen:
        return False
    seen.add(path)
    try:
        schema = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return False
    if schema.tag.rsplit("}", 1)[-1] != "schema":
        return False
    target = schema.get("targetNamespace")
    effective = target if target is not None else context_namespace
    if (effective or None) == (root_namespace or None):
        for child in schema:
            if child.tag.rsplit("}", 1)[-1] != "element":
                continue
            if child.get("name") == local:
                return True
    for child in schema:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag not in _COMPOSITION_ELEMENTS:
            continue
        location = child.get("schemaLocation")
        if not location:
            continue
        sub = Path(location)
        if not sub.is_absolute():
            sub = path.parent / sub
        child_context = (child.get("namespace") or None) if tag == "import" else effective
        if _schema_provides_root(sub, child_context, root_namespace, local, seen):
            return True
    return False


def _supplied_schema_declares_instance_root(schema_path: Path, instance_path: Path) -> bool:
    """Whether *schema_path* declares the instance's root element.

    Used to tell a genuine schema error from a collision between the
    stipulated schema and an instance's advisory ``xsi`` schema hint.
    """
    try:
        root = ET.parse(str(instance_path)).getroot()
    except (OSError, ET.ParseError):
        return False
    root_namespace, local = _split_expanded_name(root.tag)
    return _schema_provides_root(schema_path, None, root_namespace, local, set())


def _strip_instance_schema_hints(instance_path: Path, workdir: Path) -> Path | None:
    """Copy *instance_path* without its advisory ``xsi`` schema-location hints.

    Returns ``None`` when the instance carries no such hint (or cannot be
    parsed), so the caller can leave the original document untouched.
    """
    try:
        tree = ET.parse(str(instance_path))
    except (OSError, ET.ParseError):
        return None
    root = tree.getroot()
    hints = (f"{{{_XSI_NS}}}schemaLocation", f"{{{_XSI_NS}}}noNamespaceSchemaLocation")
    if not any(root.get(name) is not None for name in hints):
        return None
    for name in hints:
        root.attrib.pop(name, None)
    stripped = workdir / instance_path.name
    tree.write(stripped, encoding="utf-8", xml_declaration=True)
    return stripped


def build_permissive_schema(instance_path: Path, workdir: Path) -> Path:
    """Synthesize a wrapper schema for a group whose ``schemaTest`` is absent.

    The suite documents such a group's schema as "built-in components only":
    validation starts at the outermost element with no stipulated declaration.
    pyxsd needs *a* schema, so the harness writes one that declares the
    instance's root element with ``xs:anyType`` (so ``xsi:type`` and built-in
    datatypes are still checked) in the root's namespace.

    When the instance carries a resolvable ``xsi`` schema-location hint whose
    composition already declares that root, the declaration is omitted: the
    engine composes the hinted schema itself, and duplicating the global
    element would be a schema error. A hinted schema that fails to compile
    therefore still surfaces as a schema-phase problem rather than being
    masked by the wrapper.
    """
    try:
        root = ET.parse(str(instance_path)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise HarnessError(f"cannot read instance for schema synthesis: {exc}") from exc
    root_namespace, local = _split_expanded_name(root.tag)
    provided = any(
        _schema_provides_root(path, namespace, root_namespace, local, set())
        for namespace, path in _instance_schema_hints(instance_path)
    )
    target = f" targetNamespace={quoteattr(root_namespace)}" if root_namespace is not None else ""
    declaration = (
        f'  <xs:element name={quoteattr(local)} type="xs:anyType"/>\n' if not provided else ""
    )
    text = (
        '<?xml version="1.0"?>\n'
        f'<xs:schema xmlns:xs="{_XSD_NS}"{target}>\n'
        f"{declaration}</xs:schema>\n"
    )
    driver = workdir / "synthesized.xsd"
    driver.write_text(text, encoding="utf-8")
    return driver


def _schema_only_call(
    schema_path: Path, timeout: float | None, xsd_version: str = "1.1"
) -> EngineResult:
    """Compile *schema_path* without binding an instance."""
    result = EngineResult()
    try:
        with time_limit(timeout):
            schema = Schema.compile(
                str(schema_path), mode=ParseModes.NAMESPACED, xsd_version=xsd_version
            )
        result.schema_valid = not _errors(schema.report, "schema")
    except PyXSDError as exc:
        result.schema_valid = False
        result.schema_error = str(exc)
    except TimeoutError as exc:
        result.timeout = True
        result.schema_error = str(exc)
    return result


class PyXSDDriver:
    """Run cases with pyxsd under the ``NAMESPACED`` standards policy."""

    name = "pyxsd"

    def __init__(
        self,
        timeout: float | None = DEFAULT_TIMEOUT,
        *,
        synthesize_missing_schema: bool = False,
        xsd_version: str = "1.1",
    ) -> None:
        self.timeout = timeout
        #: Opt-in: let the runner synthesize a permissive wrapper schema for
        #: groups with no ``schemaTest`` instead of reporting adapter-gap.
        self.synthesize_missing_schema = synthesize_missing_schema
        #: The processor version to compile with; the runner derives it from
        #: the active profile (an ``xsd10`` run uses XSD 1.0 mode).
        self.xsd_version = xsd_version

    def compile_schema(self, schema_path: Path) -> EngineResult:
        """Observe only the schema phase."""
        return _schema_only_call(schema_path, self.timeout, self.xsd_version)

    def validate(
        self, schema_path: Path, instance_path: Path, *, synthesized: bool = False
    ) -> EngineResult:
        """Observe both phases for an instance document.

        A ``synthesized`` schema is the harness's permissive wrapper for a
        group with no ``schemaTest``; its design deliberately surfaces a
        hinted schema that fails to compile, so no hint is dropped for it.
        """
        result = self._bind_and_observe(schema_path, instance_path)
        if synthesized or result.adapter_gap is None:
            return result
        if not _supplied_schema_declares_instance_root(schema_path, instance_path):
            return result
        with tempfile.TemporaryDirectory(prefix="pyxsd-hints-") as scratch:
            stripped = _strip_instance_schema_hints(instance_path, Path(scratch))
            if stripped is None:
                return result
            retry = self._bind_and_observe(schema_path, stripped)
        if retry.schema_valid is True:
            return retry
        return result

    def _bind_and_observe(self, schema_path: Path, instance_path: Path) -> EngineResult:
        """Compile *schema_path* and bind one instance, observing both phases.

        The instance is parsed once and its extra ``xsi:schemaLocation``
        pairs are forwarded into the schema compile (the same flow
        :func:`pyxsd.parse` implements), so a hinted schema that fails
        to compose surfaces in the schema phase. A fatal error during
        the compile is a schema verdict; a fatal error during the bind
        is an instance one (the schema already compiled, so no second,
        schema-only pass is needed to tell them apart).
        """
        result = EngineResult()
        stage = "read"
        try:
            with time_limit(self.timeout):
                context = NamespaceContext()
                try:
                    tree = parse_with_namespaces(str(instance_path), context)
                except OSError as e:
                    raise PyXSDError(f"the xml input could not be read: {e}") from e
                except ET.ParseError as e:
                    raise PyXSDError(f"the xml file is not well-formed XML: {e}") from e
                stage = "compile"
                schema = Schema.compile(
                    str(schema_path),
                    mode=ParseModes.NAMESPACED,
                    xsd_version=self.xsd_version,
                    namespace_context=context,
                    schema_location_pairs=absolute_schema_location_pairs(
                        tree, instance_path.parent
                    ),
                )
                stage = "bind"
                document = schema.parse(tree)
        except PyXSDError as exc:
            if stage == "read":
                # A fatal instance-input problem (unreadable or not
                # well-formed XML): attribute it the way a schema-only
                # probe would — the schema phase is only implicated
                # when the schema itself does not compile.
                probe = self.compile_schema(schema_path)
                if probe.schema_valid is False:
                    result.schema_valid = False
                    result.schema_error = probe.schema_error or str(exc)
                    result.adapter_gap = "group schema did not compile"
                else:
                    result.schema_valid = True
                    result.instance_valid = False
                    result.instance_error = str(exc)
                if probe.timeout:
                    result.timeout = True
            elif stage == "compile":
                result.schema_valid = False
                result.schema_error = str(exc)
                result.adapter_gap = "group schema did not compile"
            else:
                result.schema_valid = True
                result.instance_valid = False
                result.instance_error = str(exc)
            return result
        except TimeoutError as exc:
            result.timeout = True
            result.instance_error = str(exc)
            return result
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            return result

        result.schema_valid = not _errors(schema.report, "schema")
        if result.schema_valid:
            result.instance_valid = not _errors(document.report, "instance")
        else:
            result.instance_valid = None
            result.adapter_gap = "group schema did not compile"
        warnings = document.report.warnings
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

    def validate(
        self, schema_path: Path, instance_path: Path, *, synthesized: bool = False
    ) -> EngineResult:
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
