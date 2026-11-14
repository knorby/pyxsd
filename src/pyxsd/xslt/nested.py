"""Nested XSLT transformations behind ``fn:transform``.

``fn:transform($options as map(*)) as map(*)`` loads a stylesheet (or package)
and runs it as a transformation nested inside the calling one.  This module
owns the whole function: it reads and validates the option map, selects the
processor by requested XSLT version, compiles the nested stylesheet through
the public processor layer, prepares the invocation through the Task 16
validator, and maps the result into the keyed delivery map.

Policy invariants:

- The nested call runs on the outer processor's immutable snapshot (extension,
  resource, package, and output policies come with it); it never constructs a
  default or unrestricted processor.
- Only stable context values (current date/time and implicit timezone) and the
  cumulative budget ledger cross the nesting boundary, via
  :class:`~pyxsd.xslt.policy.NestedTransformationPolicy`; runtime stacks,
  caches, global values, and results stay separate.
- Only compiled artifacts are cached, keyed by stylesheet identity; results
  never enter the cache, and the cache is consulted only when the caller did
  not supply static parameters.
- Producing a result never writes anywhere.  ``base-output-uri`` and
  ``xsl:result-document`` destinations are only authorization decisions.

Option notes, where the F&O 3.1 latitude is exercised:

- ``stylesheet-location``/``package-location`` are resolved against the
  calling expression's static base URI; without one, a relative value is
  handled by the loader as a local path.
- ``stylesheet-base-uri`` supplies the static base URI of an otherwise
  base-less ``stylesheet-text``/``package-text``/``*-node`` source.  A
  located stylesheet already has a base URI, so the option has no effect
  there (the implementation-defined allowance).
- The requested XSLT version is taken from ``xslt-version``; when the option
  is absent the stylesheet always runs on this processor's 3.0 profile, whose
  backwards-compatibility handling covers 1.0/2.0-declared modules.  Requests
  for versions above 3.0 raise ``FOXT0001``.
- ``enable-messages``/``enable-trace``/``enable-assertions``: this processor
  always evaluates messages, trace, and assertions into in-memory results and
  can never disable them, so an explicit ``false()`` requests an unsupported
  feature and raises ``FOXT0001``.
- ``vendor-options`` entries are validated as a map and ignored; no vendor
  option namespace is recognized.  Unlisted option keys are ignored, as the
  specification directs.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

from pyxsd.xdm import XdmArray, XdmAtomic, XdmFunction, XdmMap, XdmSequence, as_xdm
from pyxsd.xdm.nodes import NodeKind, XdmNode
from pyxsd.xpath.functions import call_host_function
from pyxsd.xslt.errors import XsltDynamicError, XsltError
from pyxsd.xslt.policy import NestedTransformationPolicy
from pyxsd.xslt.processor import (
    _ABSENT,
    CompiledStylesheet,
    XsltInvocationError,
    _Invocation,
    _prepare_invocation,
)
from pyxsd.xslt.results import (
    DELIVERY_FORMATS,
    TransformationResult,
    delivered_entries,
)
from pyxsd.xslt.runtime.functions import (
    SYSTEM_PROPERTIES,
    XSLT_NS,
    _serialization_parameter_map,
)
from pyxsd.xslt.runtime.transformation import Transformation
from pyxsd.xslt.serialization import SerializationParameters, Serializer

__all__ = ["invoke_nested_transform"]

FOXT0001 = "FOXT0001"
FOXT0002 = "FOXT0002"
FOXT0003 = "FOXT0003"

_PROCESSOR_VERSION = Decimal("3.0")

_DISABLEABLE_OPTIONS = ("enable-messages", "enable-trace", "enable-assertions")


class _NamedSource(io.BytesIO):
    """An in-memory stylesheet source whose name supplies its base URI."""

    def __init__(self, data: bytes, name: str) -> None:
        super().__init__(data)
        self.name = name


# ---------------------------------------------------------------------------
# Error helpers


def _invalid_options(message: str, cause: BaseException | None = None) -> XsltDynamicError:
    return XsltDynamicError(f"invalid transform options: {message}", code=FOXT0002, cause=cause)


def _unavailable(message: str, cause: BaseException | None = None) -> XsltDynamicError:
    return XsltDynamicError(
        f"no suitable XSLT processor is available: {message}", code=FOXT0001, cause=cause
    )


# ---------------------------------------------------------------------------
# Entry point


def invoke_nested_transform(
    outer: Any,
    options: XdmMap,
    *,
    base_uri: str | None,
) -> XdmSequence:
    """Run one ``fn:transform`` call nested inside *outer*.

    *outer* is the calling transformation; *options* is the already evaluated
    option map; *base_uri* is the static base URI of the calling expression.
    Returns the keyed result map as a single-item sequence.
    """
    if not isinstance(options, XdmMap):
        raise _invalid_options("the options must be a single map")
    if not isinstance(outer, Transformation):
        raise _unavailable("no XSLT processor is available in this context")
    policy = NestedTransformationPolicy.from_transformation(outer)
    request = _read_options(
        options,
        base_uri=base_uri,
        resolver=getattr(policy.processor, "resolver", None),
    )
    compiled = _compiled_stylesheet(policy, request, caller=outer.stylesheet)
    return _run(policy, request, compiled)


# ---------------------------------------------------------------------------
# The translated request


@dataclass(frozen=True, slots=True)
class _Request:
    """One ``fn:transform`` option map translated into host values."""

    source: Any
    identity: str | None
    identity_cacheable: bool
    cacheable: bool
    static_params: dict[str, XdmSequence]
    stylesheet_params: dict[str, XdmSequence]
    template_params: dict[str, XdmSequence]
    tunnel_params: dict[str, XdmSequence]
    has_source_node: bool
    source_node: Any
    has_global_context: bool
    global_context_item: Any
    has_match_selection: bool
    initial_match_selection: Any
    initial_template: str | None
    initial_mode: str | None
    initial_function: str | None
    function_arguments: tuple[XdmSequence, ...]
    output_uri: str | None
    delivery: str
    delivery_format: str
    serialization_parameters: dict[str, Any] | None
    post_process: Any


def _read_options(
    options: XdmMap,
    *,
    base_uri: str | None,
    resolver: Any,
) -> _Request:
    _validate_vendor_options(options)
    _validate_boolean_toggles(options)
    _check_requested_properties(options)

    location = _optional_string(options, "stylesheet-location")
    node = _optional_root_node(options, "stylesheet-node")
    text = _optional_string(options, "stylesheet-text")
    package_location = _optional_string(options, "package-location")
    package_node = _optional_root_node(options, "package-node")
    package_text = _optional_string(options, "package-text")
    package_name = _optional_string(options, "package-name")
    package_version = _optional_string(options, "package-version")

    stylesheet_sources = sum(value is not None for value in (location, node, text))
    package_sources = sum(
        value is not None for value in (package_location, package_node, package_text, package_name)
    )
    if stylesheet_sources + package_sources != 1:
        raise _invalid_options(
            "exactly one of the stylesheet or package source options must be supplied"
        )
    if package_version is not None and package_name is None:
        raise _invalid_options("package-version requires package-name")

    requested_version = _requested_version(options)
    if requested_version > _PROCESSOR_VERSION:
        raise _unavailable(
            f"no XSLT processor implementing version {requested_version} is available"
        )

    base_output = _optional_string(options, "base-output-uri")
    stylesheet_base = _optional_string(options, "stylesheet-base-uri")
    output_uri = (
        None
        if base_output is None
        else _resolve_uri(base_output, base_uri, "base-output-uri", absolute=True)
    )
    stylesheet_base_uri = (
        None
        if stylesheet_base is None
        else _resolve_uri(stylesheet_base, base_uri, "stylesheet-base-uri", absolute=True)
    )

    has_source_node = options.contains(XdmAtomic.string("source-node"))
    source_node = None
    if has_source_node:
        source_node = _optional_any_node(options, "source-node")
    has_global_context = options.contains(XdmAtomic.string("global-context-item"))
    global_context_item = None
    if has_global_context:
        global_context_item = _optional_item(options, "global-context-item")
    has_match_selection = options.contains(XdmAtomic.string("initial-match-selection"))
    initial_match_selection = as_xdm(_optional_sequence(options, "initial-match-selection"))

    identity: str | None
    if package_name is not None:
        source, identity = _package_by_name(resolver, package_name, package_version)
        identity_cacheable = True
    elif text is not None or package_text is not None:
        lexical = text if text is not None else package_text
        assert lexical is not None
        source = _text_source(lexical, stylesheet_base_uri)
        identity = stylesheet_base_uri
        identity_cacheable = False
    elif node is not None:
        source = _node_source(node)
        identity = stylesheet_base_uri
        identity_cacheable = False
    elif package_node is not None:
        source = _node_source(package_node)
        identity = stylesheet_base_uri
        identity_cacheable = False
    else:
        located = cast(str, location if location is not None else package_location)
        source = _resolve_uri(located, base_uri, "stylesheet-location", absolute=False)
        identity = source
        identity_cacheable = True

    delivery = _delivery(options)
    return _Request(
        source=source,
        identity=identity,
        identity_cacheable=identity_cacheable,
        cacheable=_optional_boolean(options, "cache") is not False,
        static_params=_parameter_map(options, "static-params"),
        stylesheet_params=_parameter_map(options, "stylesheet-params"),
        template_params=_parameter_map(options, "template-params"),
        tunnel_params=_parameter_map(options, "tunnel-params"),
        has_source_node=has_source_node,
        source_node=source_node,
        has_global_context=has_global_context,
        global_context_item=global_context_item,
        has_match_selection=has_match_selection,
        initial_match_selection=initial_match_selection,
        initial_template=_optional_name(options, "initial-template"),
        initial_mode=_optional_name(options, "initial-mode"),
        initial_function=_optional_name(options, "initial-function"),
        function_arguments=_function_arguments(options),
        output_uri=output_uri,
        delivery=delivery,
        delivery_format=_format_for(delivery),
        serialization_parameters=_serialization_params(options),
        post_process=_post_process(options),
    )


# ---------------------------------------------------------------------------
# Option readers


def _entry(options: XdmMap, name: str) -> XdmSequence:
    return options.get(XdmAtomic.string(name))


def _supplied(options: XdmMap, name: str) -> bool:
    return options.contains(XdmAtomic.string(name))


def _single_option(options: XdmMap, name: str) -> XdmSequence:
    value = _entry(options, name)
    if len(value) != 1:
        raise _invalid_options(f"the {name!r} option must supply a single value")
    return value


def _optional_string(options: XdmMap, name: str) -> str | None:
    if not _supplied(options, name):
        return None
    value = _single_option(options, name)
    item = value[0]
    if not isinstance(item, XdmAtomic):
        raise _invalid_options(f"the {name!r} option must be a string value")
    return item.lexical


def _optional_root_node(options: XdmMap, name: str) -> XdmNode | None:
    if not _supplied(options, name):
        return None
    value = _single_option(options, name)
    item = value[0]
    if not isinstance(item, XdmNode) or item.kind not in (
        NodeKind.DOCUMENT,
        NodeKind.ELEMENT,
    ):
        raise _invalid_options(f"the {name!r} option must be a document or element node")
    return item


def _optional_any_node(options: XdmMap, name: str) -> XdmNode:
    value = _single_option(options, name)
    item = value[0]
    if not isinstance(item, XdmNode):
        raise _invalid_options(f"the {name!r} option must be a node")
    return item


def _optional_item(options: XdmMap, name: str) -> Any:
    value = _single_option(options, name)
    return value[0]


def _optional_sequence(options: XdmMap, name: str) -> XdmSequence:
    return _entry(options, name)


def _optional_map(options: XdmMap, name: str) -> XdmMap | None:
    if not _supplied(options, name):
        return None
    value = _single_option(options, name)
    if not isinstance(value[0], XdmMap):
        raise _invalid_options(f"the {name!r} option must be a map")
    return value[0]


def _optional_boolean(options: XdmMap, name: str) -> bool | None:
    if not _supplied(options, name):
        return None
    value = _single_option(options, name)
    item = value[0]
    if not isinstance(item, XdmAtomic) or not item.type_name.endswith("}boolean"):
        raise _invalid_options(f"the {name!r} option must be an xs:boolean")
    return bool(item.value)


def _parameter_map(options: XdmMap, name: str) -> dict[str, XdmSequence]:
    mapping = _optional_map(options, name)
    if mapping is None:
        return {}
    return {name_value(key): value for key, value in mapping.entries}


def _optional_name(options: XdmMap, name: str) -> str | None:
    if not _supplied(options, name):
        return None
    value = _single_option(options, name)
    item = value[0]
    if not isinstance(item, XdmAtomic):
        raise _invalid_options(f"the {name!r} option must be an xs:QName")
    if _qname_parts(item) is None and item.type_name.endswith(
        ("}string", "}anyURI", "}untypedAtomic")
    ):
        return item.lexical
    if _qname_parts(item) is not None:
        return name_value(item)
    raise _invalid_options(f"the {name!r} option must be an xs:QName")


def _function_arguments(options: XdmMap) -> tuple[XdmSequence, ...]:
    if not _supplied(options, "function-params"):
        return ()
    value = _single_option(options, "function-params")
    if not isinstance(value[0], XdmArray):
        raise _invalid_options("the 'function-params' option must be an array")
    return value[0].members


def name_value(item: XdmAtomic) -> str:
    """Return one option name as an expansion-ready string.

    QName keys become ``Q{uri}local`` (or the plain local name when the URI is
    empty); string keys are kept as written so lexical ``prefix:local`` names
    resolve through the nested package's own namespaces.
    """
    parts = _qname_parts(item)
    if parts is None:
        return item.lexical
    uri, local = parts
    return f"Q{{{uri}}}{local}" if uri else local


def _qname_parts(item: Any) -> tuple[str, str] | None:
    value = getattr(item, "value", None)
    local = getattr(value, "local_name", None)
    if isinstance(local, str):
        return (getattr(value, "namespace", None) or "", local)
    return None


def _delivery(options: XdmMap) -> str:
    requested = _optional_string(options, "delivery-format")
    if requested is None:
        requested = "document"
    delivery = DELIVERY_FORMATS.get(requested)
    if delivery is None:
        raise _invalid_options(
            f"the 'delivery-format' option must be one of "
            f"{sorted(DELIVERY_FORMATS)}, got {requested!r}"
        )
    return delivery


def _format_for(delivery: str) -> str:
    return next(
        format_name
        for format_name, invocation in DELIVERY_FORMATS.items()
        if invocation == delivery
    )


def _serialization_params(options: XdmMap) -> dict[str, Any] | None:
    if not _supplied(options, "serialization-params"):
        return None
    value = _single_option(options, "serialization-params")
    try:
        return _serialization_parameter_map(value)
    except XsltDynamicError as error:
        raise _invalid_options(
            f"the 'serialization-params' option is not a valid serialization map: {error}",
            cause=error,
        ) from error


def _post_process(options: XdmMap) -> Any:
    if not _supplied(options, "post-process"):
        return None
    value = _single_option(options, "post-process")
    if not isinstance(value[0], XdmFunction):
        raise _invalid_options("the 'post-process' option must be a function item")
    return value[0]


def _requested_version(options: XdmMap) -> Decimal:
    if not _supplied(options, "xslt-version"):
        return _PROCESSOR_VERSION
    value = _single_option(options, "xslt-version")
    item = value[0]
    if not isinstance(item, XdmAtomic):
        raise _invalid_options("the 'xslt-version' option must be an xs:decimal")
    try:
        version = Decimal(item.lexical)
    except InvalidOperation as error:
        raise _invalid_options(
            f"the 'xslt-version' option must be an xs:decimal, got {item.lexical!r}",
            cause=error,
        ) from error
    if not version.is_finite():
        raise _invalid_options("the 'xslt-version' option must be a finite decimal")
    return version


def _validate_boolean_toggles(options: XdmMap) -> None:
    for name in _DISABLEABLE_OPTIONS:
        value = _optional_boolean(options, name)
        if value is False:
            raise _unavailable(
                f"this processor cannot disable {name.replace('enable-', '')} evaluation"
            )


def _validate_vendor_options(options: XdmMap) -> None:
    vendor = _optional_map(options, "vendor-options")
    del vendor  # validated as a map; no vendor option namespace is recognized


def _check_requested_properties(options: XdmMap) -> None:
    requested = _optional_map(options, "requested-properties")
    if requested is None:
        return
    for key, value in requested.entries:
        uri, local = _requested_property_name(key)
        if uri != XSLT_NS or local == "version":
            continue
        actual = SYSTEM_PROPERTIES.get(f"xsl:{local}")
        if actual is None:
            continue
        wanted = _requested_property_value(value)
        if wanted != actual:
            raise _unavailable(f"no available processor sets xsl:{local} to {wanted!r}")


def _requested_property_name(key: XdmAtomic) -> tuple[str, str]:
    parts = _qname_parts(key)
    if parts is not None:
        return parts
    lexical = key.lexical
    prefix, separator, local = lexical.partition(":")
    if not separator:
        return "", lexical
    if prefix == "xsl":
        return XSLT_NS, local
    return "", local


def _requested_property_value(value: XdmSequence) -> str:
    if len(value) != 1 or not isinstance(value[0], XdmAtomic):
        raise _invalid_options("requested-properties values must be single atomic values")
    item = value[0]
    if item.type_name.endswith("}boolean"):
        return "yes" if bool(item.value) else "no"
    return item.lexical


# ---------------------------------------------------------------------------
# Source materialization


def _resolve_uri(
    value: str,
    base_uri: str | None,
    label: str,
    *,
    absolute: bool,
) -> str:
    resolved = urljoin(base_uri, value) if base_uri is not None else value
    parts = urlsplit(resolved)
    if parts.scheme == "file":
        # A file URI under this resolution is a local path the loader reads
        # directly, matching the loader's own include/import convention.
        resolved = str(Path(url2pathname(parts.path)).resolve())
    elif absolute and not parts.scheme:
        raise _invalid_options(
            f"the {label!r} option must resolve to an absolute URI, got {value!r}"
        )
    return resolved


def _text_source(lexical: str, stylesheet_base_uri: str | None) -> Any:
    data = lexical.encode("utf-8")
    if stylesheet_base_uri is not None:
        return _NamedSource(data, stylesheet_base_uri)
    return io.BytesIO(data)


def _node_source(node: XdmNode) -> Any:
    """Serialize one stylesheet node into the loader's byte-stream form.

    The F&O contract permits crossing the boundary as a copy of the node's
    subtree, and the module loader consumes streams, so serialization is the
    lossless-for-the-infoset bridge here.
    """
    sequence: XdmSequence = (node,)
    return io.BytesIO(Serializer.serialize(sequence, SerializationParameters()).encode("utf-8"))


def _package_by_name(resolver: Any, name: str, version: str | None) -> tuple[Any, str]:
    """Resolve one registered package through the inherited processor resolver."""
    if resolver is None:
        raise _unavailable(f"the package {name!r} is not registered with this processor")
    try:
        descriptor = resolver.resolve(name, version or "*")
    except XsltError as error:
        raise _unavailable(str(error)) from error
    return descriptor.module, f"{descriptor.name}@{descriptor.version}"


# ---------------------------------------------------------------------------
# Compilation


def _compiled_stylesheet(
    policy: NestedTransformationPolicy,
    request: _Request,
    *,
    caller: Any,
) -> CompiledStylesheet:
    """Compile (or reuse) the nested stylesheet on the inherited processor."""
    processor = policy.processor
    extensions = caller.extensions
    schema_pool = caller.schema_pool
    if request.cacheable and request.identity_cacheable and not request.static_params:
        key = (id(processor), id(extensions), id(schema_pool), request.identity)
        cached = _COMPILED_CACHE.get(key)
        if cached is not None:
            return cached.compiled
        compiled = processor.compile(
            request.source,
            static_parameters=None,
            schema=schema_pool,
            extensions=extensions,
        )
        # The key is built from object identities, so the entry retains the
        # keyed objects and no later allocation can collide with the id.
        _COMPILED_CACHE[key] = _CacheEntry(processor, extensions, schema_pool, compiled)
        return compiled
    return processor.compile(
        request.source,
        static_parameters=request.static_params or None,
        schema=schema_pool,
        extensions=extensions,
    )


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One cached compiled artifact plus the objects its key identity names."""

    processor: Any
    extensions: Any
    schema_pool: Any
    compiled: CompiledStylesheet


_COMPILED_CACHE: dict[tuple[Any, ...], _CacheEntry] = {}
#: Process-lifetime and never evicted: one entry per (processor, extensions,
#: schema pool, stylesheet identity) tuple, each entry retaining its keyed
#: objects.  Entries hold compiled artifacts only, which are immutable and
#: invocation-independent, so sharing them across transformations is safe.


# ---------------------------------------------------------------------------
# Invocation and result mapping


def _run(
    policy: NestedTransformationPolicy,
    request: _Request,
    compiled: CompiledStylesheet,
) -> XdmSequence:
    try:
        invocation_options = _prepare_invocation(
            compiled,
            source=request.source_node if request.has_source_node else _ABSENT,
            parameters=request.stylesheet_params or None,
            global_context_item=(
                request.global_context_item if request.has_global_context else _ABSENT
            ),
            initial_match_selection=(
                request.initial_match_selection if request.has_match_selection else _ABSENT
            ),
            template_parameters=request.template_params or None,
            tunnel_parameters=request.tunnel_params or None,
            initial_template=request.initial_template,
            initial_mode=request.initial_mode,
            initial_function=request.initial_function,
            initial_function_arguments=request.function_arguments,
            output_uri=request.output_uri,
            delivery=request.delivery,
        )
        invocation = _Invocation(compiled, invocation_options, shared=policy)
        result = invocation.run()
    except XsltInvocationError as error:
        # The Task 16 validator's host-level conflicts are fn:transform
        # parameter errors (F&O's FOXT0002); its message stays as the detail.
        raise _invalid_options(str(error), cause=error) from error
    except ValueError as error:
        # Other validator ValueErrors (for example function parameters
        # without an initial function) are the same category.
        raise _invalid_options(str(error), cause=error) from error
    except XsltError as error:
        if error.code is None:
            raise XsltDynamicError(
                f"the nested transformation failed: {error}",
                code=FOXT0003,
                cause=error,
            ) from error
        raise
    return _result_map(result, request)


def _result_map(
    result: TransformationResult,
    request: _Request,
) -> XdmSequence:
    entries = delivered_entries(
        result,
        delivery=request.delivery_format,
        principal_parameters=request.serialization_parameters,
    )
    if request.post_process is not None:
        entries = [
            (key, _apply_post_process(request.post_process, key, value)) for key, value in entries
        ]
    return (XdmMap([(XdmAtomic.string(key), value) for key, value in entries]),)


def _apply_post_process(post: Any, key: str, value: XdmSequence) -> XdmSequence:
    try:
        return call_host_function(post, (XdmAtomic.string(key), value))
    except TypeError as error:
        raise _invalid_options(
            f"the post-process function rejected an entry: {error}",
            cause=error,
        ) from error
