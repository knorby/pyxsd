"""Mixin extensions for generated classes.

Mixins fold into the classes pyxsd generates at build time; the
per-compile :class:`Extensions` registry selects which mixins apply to
which type declarations.
"""

from collections.abc import Callable
from typing import Any
from weakref import WeakKeyDictionary

from pyxsd.exceptions import PyXSDError
from pyxsd.namespaces import clark, local_name, local_part, namespace_of
from pyxsd.schema_context import active_report
from pyxsd.validation import IssueSeverity, ValidationIssue, report_or_log

__all__ = ["Extensions", "run_validation_hooks"]

#: A compiled selector: a predicate over a type ER, or a raw path string
#: (an unresolved ``for_path`` entry, resolved by :meth:`_resolve_paths`).
_Selector = Callable[[Any], bool] | str

#: The ER class names that are type declarations, inline or global.
_TYPE_NAMES = ("SimpleType", "ComplexType")

#: One registry entry: ``(selector, mixin, registration order)``.
_Entry = tuple[_Selector, type, int]


def _local_spelling(name: str) -> str:
    """The local part of a prefixed (``a:Patient``) or Clark name.

    A plain name is returned unchanged, so two spellings match when
    their local parts agree.
    """
    if "}" in name:
        return name.rsplit("}", 1)[-1]
    if ":" in name:
        return name.rsplit(":", 1)[-1]
    return name


class Extensions:
    """A per-compile registry of mixin selectors.

    Each registration method may be used as a decorator
    (``@ext.for_type(...)``), which registers the decorated class and
    returns it, or called directly (``ext.for_type(name, mixin=Cls)``).
    Every ``for_*`` selector compiles to a predicate over the type ER
    immediately; ``for_path`` is stored raw for later resolution.

    Registration order is precedence: a later registration outranks an
    earlier one, so :meth:`mixins_for` lists the most recently
    registered match first. Instances are standalone — no global
    registry state.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._counter = 0
        #: Compiled path predicates by owning schema ER, then raw path:
        #: each path is resolved against the composed declaration tree
        #: once per *compile* (a registry may be reused across compiles,
        #: and each compile's tree yields its own predicate holding that
        #: tree's type ER by identity). Raw path entries stay raw in
        #: ``_entries`` so the path is always available to re-resolve;
        #: keys are held weakly, so a schema's resolutions die with the
        #: schema.
        self._resolved_paths: WeakKeyDictionary[Any, dict[str, Callable[[Any], bool]]] = (
            WeakKeyDictionary()
        )

    # -- registration -------------------------------------------------

    def for_type(self, name: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for a type by name.

        *name* is a plain local name (``"Code"``) or a Clark name
        (``"{uri}Code"``). Without ``mixin``, returns a decorator that
        registers the decorated class.
        """

        def matches(er: Any) -> bool:
            return er.name == name or getattr(er, "expandedName", None) == name

        return self._register(matches, mixin)

    def for_namespace(self, uri: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for every type declared in namespace *uri*."""

        def matches(er: Any) -> bool:
            name = getattr(er, "name", None)
            expanded = getattr(er, "expandedName", None)
            # ``expandedName`` is ``clark(namespace, name)``; matching it
            # against the same spelling built from *uri* compares the
            # namespace exactly (a URI containing ``}`` cannot alias a
            # prefix of another).
            return name is not None and expanded == clark(uri, name)

        return self._register(matches, mixin)

    def for_kind(
        self,
        kind: str,
        *,
        derived_from: str | None = None,
        mixin: type | None = None,
    ) -> Any:
        """Registers *mixin* for types of *kind* (``simpleType`` or
        ``complexType``).

        With *derived_from*, only types whose ``superClassNames`` list
        the given base (matched by local or expanded spelling) qualify.
        """

        def matches(er: Any) -> bool:
            if type(er).__name__.lower() != kind.lower():
                return False
            if derived_from is None:
                return True
            supers = getattr(er, "superClassNames", None) or ()
            wanted = _local_spelling(derived_from)
            return any(_local_spelling(str(super)) == wanted for super in supers)

        return self._register(matches, mixin)

    def for_path(self, path: str, mixin: type | None = None) -> Any:
        """Registers *mixin* for the type of the element at an instance *path*.

        *path* names element declarations from the document root down
        (``"/ClinicalDocument/recordTarget/patient"``); every segment is
        a local element name. The final segment's declaration
        contributes the selected type — its inline anonymous type when
        one is declared, else the referenced global type — matched
        later by ER identity, never by the anonymous type's synthesized
        (and collidable) name. Resolution happens at class-building
        time against the composed declaration tree; a segment matching
        no declaration fails the compile loudly. Traversal stays within
        each element's own content model — the base chain of an
        extension-typed element is not followed, so a segment naming a
        base-declared child dangles loudly too.
        """
        return self._register(path, mixin)

    def where(self, predicate: Callable[[Any], bool], mixin: type | None = None) -> Any:
        """Registers *mixin* for every type ER satisfying *predicate*."""
        return self._register(predicate, mixin)

    # -- lookup ---------------------------------------------------------

    def mixins_for(self, er: Any, schema: Any = None) -> tuple[type, ...]:
        """The mixins matching the type ER *er*, in precedence order.

        The first element is the highest-precedence (most recently
        registered) match; a mixin registered by several matching
        selectors appears once, at its highest-precedence position.

        Raw path entries resolve against the owning *schema* ER's
        composed declaration tree — class building happens after schema
        composition, so the tree is complete then — once per compile
        (the resolutions are cached per schema, and a registry reused
        across compiles resolves the same path again against each new
        tree). Without *schema* (an ER-tree-only caller) path entries
        stay skipped and silent.
        """
        resolved = None
        if schema is not None and any(
            isinstance(selector, str) for selector, _, _ in self._entries
        ):
            resolved = self._resolve_paths(schema)
        matched: list[tuple[int, type]] = []
        for selector, mixin, order in self._entries:
            if isinstance(selector, str):
                # No owning schema to resolve against: the path entry
                # is skipped and silent (an ER-tree-only caller).
                if resolved is None:
                    continue
                predicate = resolved[selector]
            else:
                predicate = selector
            if not predicate(er):
                continue
            matched.append((order, mixin))
        matched.sort(key=lambda entry: entry[0], reverse=True)
        seen: list[type] = []
        for _, mixin in matched:
            if not any(mixin is kept for kept in seen):
                seen.append(mixin)
        return tuple(seen)

    # -- internals ------------------------------------------------------

    def _resolve_paths(self, schema: Any) -> dict[str, Callable[[Any], bool]]:
        """Resolves every raw path entry against *schema*'s composed tree.

        Each path compiles once per schema to a predicate holding the
        final segment's type ER by identity; the resolutions are cached
        under *schema* (weakly, so they die with the schema) and paths
        compile in registration order, so a failing path is reported
        deterministically. Because the cache is per schema, a registry
        reused for a second ``Schema.compile`` resolves each path again
        against the new declaration tree instead of answering with a
        stale predicate from the previous one.
        """
        per_schema = self._resolved_paths.setdefault(schema, {})
        for selector, _, _ in self._entries:
            if isinstance(selector, str) and selector not in per_schema:
                per_schema[selector] = self._compile_path(selector, schema)
        return per_schema

    def _compile_path(self, path: str, schema: Any) -> Callable[[Any], bool]:
        """Resolves *path* to a predicate over the final segment's type ER.

        The root segment matches a global element declaration of the
        composed schema; each next segment matches a child element
        declaration inside the current element's type ER. The final
        segment's element contributes the selected type ER. Any
        unresolved segment is a programming error in the registry's
        user: loud, naming the path.
        """
        segments = [segment for segment in path.split("/") if segment]
        if not segments:
            raise PyXSDError(f"extension path {path!r} names no element declarations")
        element = self._match(segments[0], list(getattr(schema, "elements", None) or ()), path)
        for segment in segments[1:]:
            children = self._child_elements(self._declaration_type_er(element))
            element = self._match(segment, children, path)
        type_er = self._declaration_type_er(element)
        if type_er is None:
            raise PyXSDError(
                f"extension path {path!r} reaches a declaration of no schema-declared type "
                "(anyType or anySimpleType); no type ER can carry the mixin"
            )
        selected = type_er
        return lambda er: er is selected

    @staticmethod
    def _match(segment: str, candidates: list, path: str) -> Any:
        """The one element declaration named *segment*, or a loud failure."""
        matches = [er for er in candidates if er is not None and er.name == segment]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise PyXSDError(
                f"extension path {path!r} has no element declaration named '{segment}'"
            )
        raise PyXSDError(
            f"extension path {path!r} is ambiguous: '{segment}' matches several "
            "element declarations in the same content model"
        )

    @staticmethod
    def _child_elements(type_er: Any) -> list:
        """The element declarations inside *type_er*'s content model."""
        getter = getattr(type_er, "getElements", None)
        if not callable(getter):
            return []
        return list(getter() or ())

    @staticmethod
    def _declaration_type_er(element: Any) -> Any:
        """The type ER *element* declares, or ``None`` for a built-in.

        An inline type child wins: the anonymous ER itself, whose
        synthesized name is bookkeeping and can collide. Otherwise the
        ``type`` attribute names a global declaration, looked up in the
        composed component table by ER (full spelling first, then local
        part, as ``typeFromName`` looks classes up). Element reference
        sites contribute the referred declaration.
        """
        if getattr(element, "isElementRef", False):
            return Extensions._declaration_type_er(element.referredElement)
        for child in getattr(element, "processedChildren", None) or ():
            if child is not None and type(child).__name__ in _TYPE_NAMES:
                return child
        reference = element.resolvedTypeName()
        if reference is None:
            return None
        table = getattr(element.getSchema(), "components", None)
        if table is None:
            return None
        if reference.startswith("{"):
            return table.getFromName(
                local_name(reference), kind="type", namespace=namespace_of(reference), warn=False
            )
        found = table.getFromName(reference, kind="type", warn=False)
        if found is None and local_part(reference) != reference:
            found = table.getFromName(local_part(reference), kind="type", warn=False)
        return found

    def _register(self, selector: _Selector, mixin: type | None) -> Any:
        """Appends one entry; without *mixin*, returns a decorator."""
        if mixin is None:

            def decorator(cls: type) -> type:
                self._append(selector, cls)
                return cls

            return decorator
        self._append(selector, mixin)
        return None

    def _append(self, selector: _Selector, mixin: type) -> None:
        self._counter += 1
        self._entries.append((selector, mixin, self._counter))


#: The convention method a mixin may define: ``__pyxsd_validate__(self)``
#: returning an iterable of :class:`~pyxsd.validation.ValidationIssue`
#: findings or plain message strings.
_HOOK_NAME = "__pyxsd_validate__"


def run_validation_hooks(instance: Any, *, element: str | None = None) -> None:
    """Runs the mixin validation hook of *instance*, if its type has one.

    The binding machinery calls this after base XSD validation has
    accepted a successfully constructed value — element instances at the
    end of ``SchemaBase.makeInstanceFromTag``, and attribute values in
    ``Attribute.__set__`` after their conversion succeeded — so a hook
    adds findings on top of base validation instead of replacing it. An
    instance whose type defines no ``__pyxsd_validate__`` costs one
    ``getattr`` and nothing else.

    Each finding is normalized and appended to the active instance
    report (the one the running parse installed, via
    :func:`pyxsd.schema_context.active_report`; outside any parse the
    finding is logged, like every other assignment-time diagnostic):

    - a plain ``str`` becomes an error with code ``"extension-validation"``;

    - a :class:`~pyxsd.validation.ValidationIssue` keeps its own
      severity, code and message, with ``phase`` forced to
      ``"instance"`` and ``element`` filled from the declaring name when
      the issue carries none;

    - any other finding is recorded as one error with code
      ``"extension-error"`` naming the unsupported type, and the rest of
      the findings are still processed.

    A hook returning ``None`` or an empty iterable adds no finding; any
    other non-iterable return — ``0`` and ``False`` included — is one
    unsupported finding, not a silently empty result.

    An exception from the hook — raised or raised while its findings are
    consumed — is caught and recorded as one ``extension-error`` issue;
    binding continues.

    - ``instance`` - the successfully constructed value (the element
      instance, or the converted attribute value).

    - ``element`` - the declaring name to fill a finding's ``element``
      with when the finding carries none: the attribute name at the
      attribute call site; ``None`` at the element call site, where the
      instance's bound ``_name_`` (or the class name) is used.
    """
    hook = getattr(type(instance), _HOOK_NAME, None)
    if hook is None:
        return
    report = active_report()
    fallback = element or getattr(instance, "_name_", None) or type(instance).__name__
    try:
        findings = hook(instance)
        # Normalize the return shape before iterating. ``None`` is no
        # findings, a bare finding is that one finding (a plain ``str``
        # would otherwise come out one error per character), and any
        # other non-iterable — ``0`` and ``False`` included — is one
        # unsupported finding rather than a falsy result with nothing to
        # iterate.
        if findings is None:
            findings = ()
        elif isinstance(findings, (str, ValidationIssue)):
            findings = (findings,)
        else:
            try:
                findings = iter(findings)
            except TypeError:
                findings = (findings,)
        for finding in findings:
            if isinstance(finding, str):
                _record(report, IssueSeverity.ERROR, "extension-validation", finding, fallback)
            elif isinstance(finding, ValidationIssue):
                _record(
                    report,
                    finding.severity,
                    finding.code,
                    finding.message,
                    finding.element if finding.element is not None else fallback,
                )
            else:
                _record(
                    report,
                    IssueSeverity.ERROR,
                    "extension-error",
                    f"the {_HOOK_NAME} hook returned an unsupported finding "
                    f"of type {type(finding).__name__}",
                    fallback,
                )
    except Exception as exc:
        _record(
            report,
            IssueSeverity.ERROR,
            "extension-error",
            f"the {_HOOK_NAME} hook raised {type(exc).__name__}: {exc}",
            fallback,
        )


def _record(
    report: Any,
    severity: IssueSeverity,
    code: str,
    message: str,
    element: str | None,
) -> None:
    """Appends one normalized hook finding, forcing the instance phase.

    ``phase="instance"`` is passed explicitly so a finding is never
    attributed to the schema phase, whatever report it lands on.
    """
    report_or_log(report, severity, code=code, message=message, element=element, phase="instance")
