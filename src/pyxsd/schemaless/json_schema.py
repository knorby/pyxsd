"""Draft 2020-12 evaluation over the native JSON data model."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from copy import copy
from dataclasses import dataclass, field, replace
from decimal import Decimal
from importlib import import_module
from os.path import dirname, isabs, relpath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urldefrag, urljoin

from pyxsd.locations import DataLocation
from pyxsd.resource_policy import (
    ResourcePolicy,
    ResourcePolicyError,
    new_validation_report,
    policy_compilation,
)
from pyxsd.resource_refs import classify_resource
from pyxsd.schemaless.errors import JsonLimitError, JsonParseError, JsonSchemaError
from pyxsd.schemaless.json_values import (
    JsonLimits,
    NativeJsonValue,
    _pointer_token,
    copy_native_data,
    decode_json,
    resolve_pointer,
)
from pyxsd.schemaless.json_xdm import escape_pointer_segment
from pyxsd.validation import ValidationReport

if TYPE_CHECKING:
    from referencing.jsonschema import Schema, SchemaRegistry, SchemaResource

_Path = tuple[str | int, ...]
_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _pointer(path: Iterable[str | int]) -> str:
    return "".join(f"/{escape_pointer_segment(str(part))}" for part in path)


def _load_optional() -> Any:
    try:
        return import_module("jsonschema")
    except ImportError as exc:
        raise JsonSchemaError("JSON Schema guidance requires the pyxsd[jsonschema] extra") from exc


def _read_schema(source: object, limits: JsonLimits | None) -> Schema:
    try:
        value = (
            decode_json(source, limits=limits)
            if isinstance(source, (str, bytes))
            else copy_native_data(
                dict(source) if isinstance(source, Mapping) else source, limits=limits
            )
        )
    except (JsonParseError, JsonLimitError) as exc:
        raise JsonSchemaError("Invalid JSON Schema input; expected strict JSON") from exc
    return _as_schema(_evaluation_numbers(value))


def _as_schema(value: object) -> Schema:
    if not isinstance(value, (bool, dict)):
        raise JsonSchemaError("A JSON Schema must be a boolean or object")
    return value


def _evaluation_numbers(value: NativeJsonValue) -> NativeJsonValue:
    """Use a float's JSON spelling for comparison without changing storage."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _evaluation_numbers(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_evaluation_numbers(child) for child in value]
    return value


def _same_schema_value(left: NativeJsonValue, right: NativeJsonValue) -> bool:
    """Compare copied JSON values without conflating booleans and numbers.

    Keep numeric types and mapping order: the evaluator combines
    ``patternProperties`` expressions in declaration order.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        obj = cast(dict[str, NativeJsonValue], right)
        return tuple(left) == tuple(obj) and all(
            _same_schema_value(member, obj[key]) for key, member in left.items()
        )
    if isinstance(left, list):
        arr = cast(list[NativeJsonValue], right)
        return len(left) == len(arr) and all(
            _same_schema_value(one, two) for one, two in zip(left, arr, strict=True)
        )
    return left == right


def _schema_objects(
    value: NativeJsonValue, path: _Path = ()
) -> Iterator[tuple[dict[str, Any], _Path]]:
    """Index object occurrences; never interpret instance or annotation objects."""
    if isinstance(value, dict):
        yield value, path
        for key, child in value.items():
            yield from _schema_objects(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _schema_objects(child, (*path, index))


def _coefficient(value: Decimal) -> tuple[int, int]:
    """Get the coefficient and exponent of a finite native number."""
    _, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return coefficient, cast(int, exponent)


def _is_multiple(value: Decimal, divisor: Decimal) -> bool:
    # Avoid Decimal context rounding and expanding compact exponents into huge
    # integers. The required power of ten is only needed modulo the divisor.
    numerator, value_exponent = _coefficient(value)
    denominator, divisor_exponent = _coefficient(divisor)
    shift = value_exponent - divisor_exponent
    if shift >= 0:
        return (numerator * pow(10, shift, denominator)) % denominator == 0
    if numerator == 0:
        return True
    if -shift >= len(value.as_tuple().digits):
        return False
    return numerator % (denominator * 10**-shift) == 0


def _keep_extensions(validator_class: Any) -> Any:
    """Keep the native evaluator when a nested schema repeats the same draft."""

    def evolve(validator: Any, **changes: Any) -> Any:
        return validator_class(
            changes.pop("schema", validator.schema),
            format_checker=validator.format_checker,
            registry=validator._registry,
            _resolver=changes.pop("_resolver", validator._resolver),
        )

    validator_class.evolve = evolve
    return validator_class


class _Schemas:
    """Checked resources and their diagnostic locations for one compilation."""

    def __init__(
        self, api: Any, limits: JsonLimits | None, openapi_dialects: frozenset[str]
    ) -> None:
        from referencing.jsonschema import DRAFT202012

        self.api = api
        self.specification = DRAFT202012
        self.limits = limits
        self.openapi_dialects = openapi_dialects
        self.draft = api.Draft202012Validator
        self.vocabularies = self.draft.META_SCHEMA["$vocabulary"]
        self.locations: dict[int, tuple[_Path, str | None]] = {}
        self.checked: dict[int, Schema] = {}
        default_types = self.draft.TYPE_CHECKER

        def integer(checker: Any, value: object) -> bool:
            if isinstance(value, Decimal):
                return value == value.to_integral_value()
            return default_types.is_type(value, "integer")

        self.integer_checker = default_types.redefine("integer", integer)
        self.meta_validator = _keep_extensions(
            api.validators.extend(self.draft, type_checker=self.integer_checker)
        )

    def check(self, schema: Schema) -> None:
        if id(schema) in self.checked:
            return
        # Use the actual meta-schema, with the same mathematical integer type
        # as instances (e.g., minLength: 1.0 is a legal JSON Schema).
        validator = self.meta_validator(
            self.draft.META_SCHEMA,
            format_checker=self.draft.FORMAT_CHECKER,
            registry=_offline_registry(),
        )
        error = next(validator.iter_errors(schema), None)
        if error is not None:
            raise JsonSchemaError(f"Invalid JSON Schema at {_pointer(error.absolute_path)!r}")
        self._check_dialect_tree(schema)
        self.checked[id(schema)] = schema

    def _check_dialect_tree(self, schema: Schema) -> None:
        if isinstance(schema, bool):
            return
        dialect = schema.get("$schema", _DRAFT).removesuffix("#")
        if dialect != _DRAFT and dialect not in self.openapi_dialects:
            raise JsonSchemaError(
                f"Unsupported JSON Schema dialect {dialect!r}; expected Draft 2020-12"
            )
        for uri, required in schema.get("$vocabulary", {}).items():
            if required and uri not in self.vocabularies:
                raise JsonSchemaError(f"Unsupported required vocabulary {uri!r}")
        for child in self.specification.subresources_of(schema):
            self._check_dialect_tree(child)

    def resource(self, schema: Schema, parent: str | None) -> SchemaResource:
        self.check(schema)
        for contents, path in _schema_objects(cast(NativeJsonValue, schema)):
            self.locations[id(contents)] = (path, parent)
        return self.specification.create_resource(schema)

    def for_evaluation(self) -> _Schemas:
        result = copy(self)
        result.locations = self.locations.copy()
        result.checked = self.checked.copy()
        return result


def _offline_registry() -> SchemaRegistry:
    from referencing import Registry

    return Registry()


@dataclass(frozen=True)
class _Resolver:
    """Connect referencing's URI resolution to pyxsd's resource authorization.

    The resolver's logical base can come from an untrusted `$id`. The
    retrieval parent is kept separately and never derived from that ID.
    """

    schemas: _Schemas
    registry: SchemaRegistry
    uri: str
    parent: str | None
    scope: tuple[str, ...]
    runtime: _Resources
    boolean_path: _Path = ()

    def lookup(self, ref: str) -> _Resolved:
        from referencing.exceptions import Unresolvable

        try:
            uri, fragment = (
                (self.uri, ref[1:]) if ref.startswith("#") else urldefrag(urljoin(self.uri, ref))
            )
            registry = self.registry.crawl()
            if uri not in registry or self.runtime.needs_authorization(uri, self.parent):
                registry = self.runtime.retrieve(registry, uri, self.parent)
            resource = registry[uri]
            target_uri = self.runtime.aliases.get(uri, uri)
            # Keep the reference's actual fragment syntax and dynamic scope
            # under the upstream resolver (including pointers and anchors).
            resolver = registry.resolver()
            for seen in (*self.scope, self.uri):
                resolver = resolver._evolve(base_uri=seen, registry=registry)
            # The lookup base may differ from the URI it was registered or
            # retrieved at. The underlying resolver takes an absolute URI,
            # or an already resolved relative URI from an empty base.
            resolver = resolver._evolve(base_uri=target_uri, registry=registry)
            resolved = resolver.lookup(f"#{fragment}")
        except (Unresolvable, LookupError, TypeError, ValueError) as exc:
            raise JsonSchemaError(f"Cannot resolve JSON Schema reference {ref!r}") from exc
        target = _as_schema(resolved.contents)
        self.schemas.check(target)
        parent = None
        if isinstance(target, dict):
            parent = self.schemas.locations[id(target)][1]
        boolean_path: _Path = ()
        if isinstance(target, bool) and fragment.startswith("/"):
            prefix = self.schemas.locations[id(resource.contents)][0]
            parts = unquote(fragment[1:]).split("/")
            boolean_path = (
                *prefix,
                *(part.replace("~1", "/").replace("~0", "~") for part in parts),
            )
        return _Resolved(
            contents=target,
            resolver=_Resolver(
                self.schemas,
                resolved.resolver._registry,
                resolved.resolver._base_uri,
                parent,
                tuple(reversed([base for base, _ in resolved.resolver.dynamic_scope()])),
                self.runtime,
                boolean_path,
            ),
        )

    def in_subresource(self, resource: SchemaResource) -> _Resolver:
        return replace(self, uri=urljoin(self.uri, resource.id() or ""))

    def for_evaluation(self) -> _Resolver:
        schemas = self.schemas.for_evaluation()
        runtime = _Resources(schemas, self.runtime.policy)
        runtime.aliases.update(self.runtime.aliases)
        return replace(self, schemas=schemas, runtime=runtime)


@dataclass(frozen=True)
class _Resolved:
    contents: Schema
    resolver: _Resolver


class _Resources:
    """Reference resources shared across branches of one evaluation."""

    def __init__(self, schemas: _Schemas, policy: ResourcePolicy | None) -> None:
        self.schemas = schemas
        self.policy = policy
        self.aliases: dict[str, str] = {}
        self.loaded: dict[tuple[str, str | None], tuple[str, SchemaResource]] = {}
        self.fetched: dict[str, str] = {}

    def needs_authorization(self, uri: str, parent: str | None) -> bool:
        # A resource already in the reference scope is not permission for
        # another document to read it. Same-document refs don't perform a read.
        return uri in self.fetched and self.fetched[uri] != parent

    def retrieve(self, registry: SchemaRegistry, uri: str, parent: str | None) -> SchemaRegistry:
        from referencing.exceptions import Unresolvable

        key = (uri, parent)
        if key not in self.loaded:
            if self.policy is None:
                raise Unresolvable(ref=uri)
            try:
                data = self.policy.read(_policy_locator(uri, parent), base=parent, purpose="schema")
            except (ResourcePolicyError, OSError) as exc:
                raise JsonSchemaError(f"Cannot retrieve JSON Schema reference {uri!r}") from exc
            schema = _read_schema(data.content, self.schemas.limits)
            self.loaded[key] = data.source_id, self.schemas.resource(schema, data.source_id)
        source, resource = self.loaded[key]
        updated = self.register(registry, uri, source, resource).crawl()
        for discovered in set(updated).difference(registry) | {uri}:
            self.fetched[discovered] = source
        return updated

    def register(
        self, registry: SchemaRegistry, uri: str, source: str, resource: SchemaResource
    ) -> SchemaRegistry:
        canonical = urldefrag(urljoin(source, resource.id() or ""))[0]
        discovered = _offline_registry().with_resource(source, resource).crawl()
        for address in {*discovered, uri}.intersection(registry):
            if registry[address] != discovered.get(address, resource):
                raise JsonSchemaError(f"Conflicting JSON Schema resources at {address!r}")
        self.aliases[uri] = canonical
        return registry.combine(discovered.with_resource(uri, resource))


def _policy_locator(uri: str, parent: str | None) -> str:
    """Avoid joining an already-resolved local relative locator twice."""
    if (
        parent is not None
        and classify_resource(parent).kind == "local"
        and classify_resource(uri).kind == "local"
        and not isabs(uri)
    ):
        return relpath(uri, dirname(parent))
    return uri


def _evaluator(schemas: _Schemas) -> Any:
    """Extend only the upstream boundary for native numbers and exact locations."""
    api = schemas.api
    draft = schemas.draft

    def multiple_of(validator: Any, divisor: Any, instance: Any, schema: Any) -> Iterator[Any]:
        if validator.is_type(instance, "number") and not _is_multiple(
            Decimal(instance), Decimal(divisor)
        ):
            yield api.ValidationError("The value is not a multiple of the required number")

    def locate(keyword: str, evaluate: Any) -> Any:
        def evaluate_located(
            validator: Any, constraint: Any, instance: Any, schema: Schema
        ) -> Iterator[Any]:
            for error in evaluate(validator, constraint, instance, schema) or ():
                if not hasattr(error, "_pyxsd_path"):
                    locations = validator._resolver.schemas.locations
                    error._pyxsd_path = (*locations[id(schema)][0], keyword, *error.schema_path)
                yield error

        return evaluate_located

    keywords = {key: locate(key, evaluate) for key, evaluate in draft.VALIDATORS.items()}
    keywords["multipleOf"] = locate("multipleOf", multiple_of)
    evaluator = _keep_extensions(
        api.validators.extend(draft, validators=keywords, type_checker=schemas.integer_checker)
    )
    descend = evaluator.descend

    def descend_located(
        validator: Any,
        instance: Any,
        schema: Schema,
        path: str | int | None = None,
        schema_path: str | int | None = None,
        resolver: _Resolver | None = None,
    ) -> Iterator[Any]:
        for error in descend(
            validator, instance, schema, path=path, schema_path=schema_path, resolver=resolver
        ):
            # The upstream boolean shortcut does not add the passed paths.
            if schema is False:
                if path is not None:
                    error.path.appendleft(path)
                if schema_path is not None:
                    error.schema_path.appendleft(schema_path)
                if resolver is not None:
                    error._pyxsd_path = resolver.boolean_path
            yield error

    evaluator.descend = descend_located
    return evaluator


@dataclass(frozen=True, slots=True)
class SchemaOccurrence:
    """A schema applied at an instance pointer, with its resource-local pointer."""

    schema: Schema
    instance_pointer: str
    schema_pointer: str
    _schema_source: str | None = field(default=None, kw_only=True, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _Application:
    """An applied schema and the evaluator's reference context at that point."""

    validator: Any
    path: _Path

    def child(self, schema: Schema, *path: str | int) -> _Application:
        resolver = self.validator._resolver
        resolver = resolver.in_subresource(resolver.schemas.specification.create_resource(schema))
        return _Application(
            self.validator.evolve(schema=schema, _resolver=resolver), (*self.path, *path)
        )

    def in_place(self, value: NativeJsonValue) -> Iterator[_Application]:
        """Find same-instance applications; never treat tests or negations as hints."""
        schema = self.validator.schema
        if isinstance(schema, bool):
            return
        current = self.validator._resolver
        for keyword in ("$ref", "$dynamicRef") if current.schemas.openapi_dialects else ("$ref",):
            if keyword not in schema:
                continue
            ref = schema[keyword]
            resolved = current.lookup(ref)
            target, resolver = resolved.contents, resolved.resolver
            if isinstance(target, bool):
                yield _Application(
                    self.validator.evolve(schema=target, _resolver=resolver), resolver.boolean_path
                )
            elif (
                current.schemas.openapi_dialects
                or target.get("$dynamicAnchor") != urldefrag(ref)[1]
            ):
                yield _Application(
                    self.validator.evolve(schema=target, _resolver=resolver),
                    resolver.schemas.locations[id(target)][0],
                )
        for index, branch in enumerate(schema.get("allOf", ())):
            yield self.child(branch, "allOf", index)
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                yield from self._successful(schema[keyword], keyword, value)
        if "if" in schema:
            # Match the evaluator's predicate context. It evolves in place for
            # the test and enters a subresource only for the selected branch.
            branch = (
                "then" if self.validator.evolve(schema=schema["if"]).is_valid(value) else "else"
            )
            if branch in schema:
                yield self.child(schema[branch], branch)
        if isinstance(value, dict):
            for key, dependent in schema.get("dependentSchemas", {}).items():
                if key in value:
                    yield self.child(dependent, "dependentSchemas", key)

    def _successful(
        self, branches: Iterable[Schema], keyword: str, value: NativeJsonValue
    ) -> list[_Application]:
        successful = []
        for index, schema in enumerate(branches):
            child = self.child(schema, keyword, index)
            if child.validator.is_valid(value):
                successful.append(child)
                if keyword == "oneOf":
                    # The whole instance validated, so the evaluator already
                    # checked uniqueness after this first successful branch.
                    break
        return successful

    def at_member(self, value: NativeJsonValue, token: str) -> Iterator[_Application]:
        """Follow only rules known to apply to this particular child address."""
        schema = self.validator.schema
        if isinstance(schema, bool):
            return
        if isinstance(value, dict):
            properties = schema.get("properties", {})
            matched = token in properties
            if matched:
                yield self.child(properties[token], "properties", token)
            for pattern, member in schema.get("patternProperties", {}).items():
                if re.search(pattern, token):
                    matched = True
                    yield self.child(member, "patternProperties", pattern)
            if not matched and "additionalProperties" in schema:
                # Require agreement with the evaluator (which can combine
                # patterns differently when they contain backreferences).
                find_additional_properties = import_module(
                    "jsonschema._utils"
                ).find_additional_properties
                if next(find_additional_properties((token,), schema), None) == token:
                    yield self.child(schema["additionalProperties"], "additionalProperties")
        else:  # A validated child pointer can only descend into an object or array.
            index = int(token)
            prefix = schema.get("prefixItems", ())
            if index < len(prefix):
                yield self.child(prefix[index], "prefixItems", index)
            elif "items" in schema:
                yield self.child(schema["items"], "items")
            if self.validator._resolver.schemas.openapi_dialects and "contains" in schema:
                # contains tests members via evolve, without entering a new scope.
                matching = self.validator.evolve(schema=schema["contains"])
                if matching.is_valid(cast(list[NativeJsonValue], value)[index]):
                    yield _Application(matching, (*self.path, "contains"))
        # unevaluatedProperties/Items need cross-applicator annotations.

    def evaluated_members(self, value: NativeJsonValue, keyword: str) -> frozenset[str | int]:
        """Ask the evaluator what was covered before this unevaluated keyword."""
        utils = import_module("jsonschema._utils")
        find = (
            utils.find_evaluated_property_keys_by_schema
            if keyword == "unevaluatedProperties"
            else utils.find_evaluated_item_indexes_by_schema
        )
        schema = {key: child for key, child in self.validator.schema.items() if key != keyword}
        return frozenset(find(self.validator, value, schema))


class _GuidanceCycle(Exception):
    """An applied schema re-entered the same instance without descending."""


class _AppliedSchema:
    """A validated instance and its resource snapshot, with lazy pointer guidance."""

    def __init__(self, guidance: SchemaGuidance, validator: Any, value: NativeJsonValue) -> None:
        self.guidance = guidance
        self._root = _Application(validator, ())
        self._value = value
        self._at: dict[str, tuple[_Application, ...]] = {}
        self._incomplete: dict[str, str] = {}
        self._unevaluated: dict[str, list[tuple[str, SchemaOccurrence]]] = {}
        self._evaluated_members: dict[tuple[str, int], frozenset[str | int]] = {}

    def occurrences_at(self, pointer: str) -> tuple[SchemaOccurrence, ...]:
        return tuple(self._occurrence(item, pointer) for item in self._applications(pointer))

    @staticmethod
    def _occurrence(item: _Application, pointer: str) -> SchemaOccurrence:
        schema = item.validator.schema
        return SchemaOccurrence(
            schema,
            pointer,
            _pointer(item.path),
            _schema_source=item.validator._resolver.schemas.locations.get(id(schema), ((), None))[
                1
            ],
        )

    def metadata_gaps_at(
        self, pointer: str
    ) -> tuple[str | None, tuple[tuple[str, SchemaOccurrence], ...]]:
        """Distinguish uninspectable alternatives from an empty annotation set."""
        return self._incomplete.get(pointer), tuple(self._unevaluated.get(pointer, ()))

    def _applications(self, pointer: str) -> tuple[_Application, ...]:
        if pointer not in self._at:
            path: _Path = ()
            try:
                selected = resolve_pointer(self._value, pointer)
                candidates: list[_Application] = []
                for item in self._initial(pointer):
                    path = item.path
                    candidates.extend(self._expand(item, selected, set()))
                applications = tuple(candidates)
            except (JsonSchemaError, RecursionError, _GuidanceCycle, re.error, OverflowError):
                # Validation could short-circuit an alternative whose
                # assertions or resources cannot be inspected. Do not publish
                # a partial set of candidates or affect the validation verdict.
                applications = ()
                self._incomplete[pointer] = _pointer(path)
            self._at[pointer] = applications
        return self._at[pointer]

    def _initial(self, pointer: str) -> Iterator[_Application]:
        if not pointer:
            yield self._root
            return
        parent, _, raw = pointer.rpartition("/")
        token = _pointer_token(raw, pointer)
        value = resolve_pointer(self._value, parent)
        for item in self._applications(parent):
            yield from item.at_member(value, token)
            if item.validator._resolver.schemas.openapi_dialects:
                self._record_unevaluated(item, parent, pointer, token, value)

    def _record_unevaluated(
        self, item: _Application, parent: str, pointer: str, token: str, value: NativeJsonValue
    ) -> None:
        """Record possible metadata gaps, not unevaluated keyword applications."""
        keyword = "unevaluatedProperties" if isinstance(value, dict) else "unevaluatedItems"
        schema = item.validator.schema
        if isinstance(schema, bool) or keyword not in schema:
            return
        key = (parent, id(item))
        if key not in self._evaluated_members:
            self._evaluated_members[key] = item.evaluated_members(value, keyword)
        member = token if isinstance(value, dict) else int(token)
        if member not in self._evaluated_members[key]:
            entry = self._occurrence(item.child(schema[keyword], keyword), pointer)
            self._unevaluated.setdefault(pointer, []).append((keyword, entry))

    def _expand(
        self, item: _Application, value: NativeJsonValue, active: set[int]
    ) -> Iterator[_Application]:
        identity = id(item.validator.schema)
        if identity in active:
            raise _GuidanceCycle
        yield item
        active.add(identity)
        try:
            for child in item.in_place(value):
                yield from self._expand(child, value, active)
        finally:
            active.remove(identity)


@dataclass(frozen=True)
class SchemaGuidance:
    """A compiled JSON Schema evaluated separately from native document storage."""

    _validator: Any
    _resolver: _Resolver
    _source: str
    _limits: JsonLimits | None
    _policy: ResourcePolicy | None

    @classmethod
    def compile(
        cls,
        schema: object,
        *,
        source: str = "<memory>",
        limits: JsonLimits | None = None,
        base_uri: str | None = None,
        reference_registry: SchemaRegistry | None = None,
        resource_policy: ResourcePolicy | None = None,
        format_assertion: bool = False,
        _openapi_dialects: frozenset[str] = frozenset(),
    ) -> SchemaGuidance:
        api = _load_optional()
        try:
            schemas = _Schemas(api, limits, _openapi_dialects)
            runtime = _Resources(schemas, resource_policy)
            root = _read_schema(schema, limits)
            resource = schemas.resource(root, base_uri)
            registry = _offline_registry()
            if reference_registry is not None:
                for uri, value in reference_registry.items():
                    item = schemas.resource(_read_schema(value.contents, limits), uri)
                    registry = runtime.register(registry, uri, uri, item)
            registry = runtime.register(registry, base_uri or "", base_uri or "", resource)
            resolver = _Resolver(
                schemas, registry, runtime.aliases[base_uri or ""], base_uri, (), runtime
            )
            evaluator = _evaluator(schemas)
            validator = evaluator(
                root,
                _resolver=resolver,
                registry=registry,
                format_checker=schemas.draft.FORMAT_CHECKER if format_assertion else None,
            )
        except RecursionError as exc:
            raise JsonSchemaError(
                "JSON Schema compilation exceeded the host recursion limit"
            ) from exc
        return cls(validator, resolver, source, limits, resource_policy)

    def validate(self, value: NativeJsonValue) -> ValidationReport:
        """Return path-sorted assertion diagnostics without changing the value."""
        return self._evaluate(value)[0]

    def occurrences_at(self, pointer: str, value: NativeJsonValue) -> tuple[SchemaOccurrence, ...]:
        """Return soundly applicable schemas at *pointer* in the full instance.

        An invalid instance supplies no guidance. Pointers must address a
        present value. Returned schemas are copies, independent of evaluation.
        """
        resolve_pointer(value, pointer)
        _, applied = self._evaluate(value)
        if applied is None:
            return ()
        return tuple(
            replace(item, schema=_as_schema(copy_native_data(item.schema)))
            for item in applied.occurrences_at(pointer)
        )

    def _matches(self, other: SchemaGuidance) -> bool:
        """Whether an applied evaluation is reusable in this compiled context."""
        if (
            self._resolver.parent != other._resolver.parent
            or self._policy is not other._policy
            or self._validator.format_checker is not other._validator.format_checker
            or self._resolver.schemas.openapi_dialects != other._resolver.schemas.openapi_dialects
        ):
            return False
        # Compare the compiled snapshots, not caller registry identities:
        # resources can contain mutable objects even in an immutable registry.
        left, right = self._resolver.registry, other._resolver.registry
        return left.keys() == right.keys() and all(
            _same_schema_value(
                cast(NativeJsonValue, left[uri].contents),
                cast(NativeJsonValue, right[uri].contents),
            )
            for uri in left
        )

    def _evaluate(
        self, value: NativeJsonValue, *, source: str | None = None
    ) -> tuple[ValidationReport, _AppliedSchema | None]:
        """Validate once and retain the authorized inputs for optional guidance."""
        report = new_validation_report(self._policy)
        report._tighten_issue_retention(
            None if self._limits is None else self._limits.max_diagnostics
        )
        try:
            with policy_compilation(self._policy, root_read=False):
                validator = self._validator.evolve(_resolver=self._resolver.for_evaluation())
                normalized = _evaluation_numbers(value)
                failures = sorted(validator.iter_errors(normalized), key=_error_order)
        except RecursionError as exc:
            raise JsonSchemaError(
                "JSON Schema evaluation exceeded the host recursion limit"
            ) from exc
        for error in failures:
            instance, schema = _error_paths(error)
            keyword = error.validator
            message = (
                "The boolean schema rejects this value"
                if keyword is None
                else f"The value fails the JSON Schema {keyword!r} constraint"
            )
            report.add_error(
                message,
                code="json-schema-invalid",
                location=DataLocation(
                    self._source if source is None else source,
                    _pointer(instance),
                    schema_pointer=_pointer(schema),
                ),
                location_precision="exact",
            )
        report._set_diagnostics_complete(report.suppressed_count == 0)
        # Guidance may need to inspect alternatives the evaluator did not
        # visit. Only resources authorized during this evaluation are reusable.
        validator._resolver.runtime.policy = None
        return report, None if report.has_errors else _AppliedSchema(self, validator, normalized)


def _error_paths(error: Any) -> tuple[_Path, _Path]:
    return tuple(error.absolute_path), getattr(
        error, "_pyxsd_path", tuple(error.absolute_schema_path)
    )


def _error_order(error: Any) -> tuple[tuple[tuple[bool, str | int], ...], ...]:
    # Preserve numeric index ordering across schema resources where the same
    # path prefix may lead to an array in one and an object in another.
    return tuple(
        tuple((isinstance(part, str), part) for part in path) for path in _error_paths(error)
    )
