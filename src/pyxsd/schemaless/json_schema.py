"""Draft 2020-12 evaluation over the native JSON data model."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from copy import copy
from dataclasses import dataclass, replace
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
from pyxsd.schemaless.json_values import JsonLimits, NativeJsonValue, copy_native_data, decode_json
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

    def __init__(self, api: Any, limits: JsonLimits | None) -> None:
        from referencing.jsonschema import DRAFT202012

        self.api = api
        self.specification = DRAFT202012
        self.limits = limits
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
        dialect = schema.get("$schema", _DRAFT)
        validator = self.api.validators.validator_for(schema, default=self.draft)
        if dialect.removesuffix("#") != _DRAFT or validator is not self.draft:
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
    false_path: _Path = ()

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
        false_path: _Path = ()
        if target is False and fragment.startswith("/"):
            prefix = self.schemas.locations[id(resource.contents)][0]
            parts = unquote(fragment[1:]).split("/")
            false_path = (*prefix, *(part.replace("~1", "/").replace("~0", "~") for part in parts))
        return _Resolved(
            contents=target,
            resolver=_Resolver(
                self.schemas,
                resolved.resolver._registry,
                resolved.resolver._base_uri,
                parent,
                tuple(reversed([base for base, _ in resolved.resolver.dynamic_scope()])),
                self.runtime,
                false_path,
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

        if self.policy is None:
            raise Unresolvable(ref=uri)
        key = (uri, parent)
        if key not in self.loaded:
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
                    error._pyxsd_path = resolver.false_path
            yield error

    evaluator.descend = descend_located
    return evaluator


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
    ) -> SchemaGuidance:
        api = _load_optional()
        schemas = _Schemas(api, limits)
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
        return cls(validator, resolver, source, limits, resource_policy)

    def validate(self, value: NativeJsonValue) -> ValidationReport:
        """Return path-sorted assertion diagnostics without changing the value."""
        report = new_validation_report(self._policy)
        report._tighten_issue_retention(
            None if self._limits is None else self._limits.max_diagnostics
        )
        try:
            with policy_compilation(self._policy, root_read=False):
                validator = self._validator.evolve(_resolver=self._resolver.for_evaluation())
                failures = sorted(
                    validator.iter_errors(_evaluation_numbers(value)), key=_error_order
                )
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
                    self._source, _pointer(instance), schema_pointer=_pointer(schema)
                ),
                location_precision="exact",
            )
        report._set_diagnostics_complete(report.suppressed_count == 0)
        return report


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
