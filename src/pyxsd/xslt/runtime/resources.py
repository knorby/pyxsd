"""Grounded XSLT document resources with explicit authorization.

A document is available to an instruction only when the invocation already
holds it or an application-supplied resource policy admits a read.  There is no
implicit filesystem or network retrieval, no streaming cursor, and no
bounded-memory claim. Remote URI schemes require a separate application policy;
that trusted policy is responsible for origins, redirects, timeouts and budgets.
File URIs and application-resolved URNs use the ordinary resource policy.
One invocation owns one resolver, so cached documents
and canonical URI identities never leak between transformations.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm.document import DocumentValidationError, parse_typed_document
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.whitespace import apply_source_space_rules

if TYPE_CHECKING:
    from pyxsd.xslt.compiler.static import SpaceRule

__all__ = ["DocumentResources", "resolve_document_uri"]

#: Resource-policy purpose for an XSLT source document.  Source documents are
#: XML instances, so the core policy vocabulary already covers them.
DOCUMENT_PURPOSE = "instance"

#: Resource-policy purpose for a raw text resource read.
TEXT_PURPOSE = "text"
_VALIDATION_MODES = frozenset({"strict", "lax", "preserve", "strip"})
_INPUT_TYPE_ANNOTATIONS = frozenset({"strip", "preserve", "unspecified"})
_UNSET = object()


class _DocumentPolicy(Protocol):
    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        """Authorize and read one document resource."""
        ...


def resolve_document_uri(uri: str, base: str | None) -> str:
    """Return the absolute identity for one grounded document reference.

    A fragment identifier is rejected with the XSLT document-fragment error;
    a relative reference without a base cannot be resolved and reports the same
    error the XSLT ``document()`` bridge uses.
    """
    if not isinstance(uri, str):
        raise TypeError("a document URI must be a string")
    value = uri.strip()
    if "#" in value:
        raise XsltDynamicError(
            f"document fragment identifiers are not resolved for {uri!r}",
            code="XTDE1160",
        )
    if urlsplit(value).scheme:
        return value
    target = urljoin(base, value) if base is not None else value
    if not urlsplit(target).scheme:
        raise XsltDynamicError(
            f"the relative document URI {uri!r} has no base URI",
            code="XTDE1162",
        )
    return target


class DocumentResources:
    """Resolve grounded documents for one transformation invocation.

    *documents* is the invocation's shared document registry.  A document that
    is already present is reused; a policy read is cached under the same
    canonical URI so later ``document()`` or ``xsl:source-document`` calls
    observe the same grounded nodes.  *space_rules* maps each package name to
    that package's strip/preserve declarations, so one loaded source is filtered
    by the rules of the package that requested it without changing the shared
    captured document.
    """

    def __init__(
        self,
        documents: MutableMapping[str, XdmDocument],
        *,
        policy: object = None,
        remote_policy: object = None,
        collection_resolver: Callable[[str | None], Sequence[XdmNode]] | None = None,
        schema_pool: object = None,
        validation: str = "strip",
        validation_type: str | None = None,
        input_type_annotations: str = "unspecified",
        space_rules: Mapping[str | None, Sequence[SpaceRule]] | None = None,
    ) -> None:
        if not isinstance(documents, MutableMapping):
            raise TypeError("document resources require a mutable document mapping")
        if policy is not None and not callable(getattr(policy, "read", None)):
            raise TypeError("a document resource policy must provide read()")
        if remote_policy is not None and not callable(getattr(remote_policy, "read", None)):
            raise TypeError("a remote resource policy must provide read()")
        if collection_resolver is not None and not callable(collection_resolver):
            raise TypeError("a collection resolver must be callable")
        if validation not in _VALIDATION_MODES:
            raise ValueError(f"invalid validation mode {validation!r}")
        if validation_type is not None and not isinstance(validation_type, str):
            raise TypeError("validation_type must be a string or None")
        if input_type_annotations not in _INPUT_TYPE_ANNOTATIONS:
            raise ValueError(f"invalid input-type-annotations value {input_type_annotations!r}")
        if space_rules is not None and not isinstance(space_rules, Mapping):
            raise TypeError("source space rules must be a package mapping")
        self.documents = documents
        self.policy = cast(_DocumentPolicy | None, policy)
        self.remote_policy = cast(_DocumentPolicy | None, remote_policy)
        self.collection_resolver = collection_resolver
        self.schema_pool = schema_pool
        self.validation = validation
        self.validation_type = validation_type
        self.input_type_annotations = input_type_annotations
        self.space_rules = {} if space_rules is None else dict(space_rules)
        self._sources: dict[tuple[str, str, str | None, int, str], XdmDocument] = {}
        self._variants: dict[
            tuple[str, str, str | None, int, str, str | None],
            XdmDocument,
        ] = {}
        self._texts: dict[tuple[str, str | None], str] = {}
        self._collections: dict[str | None, tuple[XdmNode, ...]] = {}

    def resolve(
        self,
        uri: str,
        *,
        base: str | None,
        validation: str | None = None,
        type_name: str | None = None,
        schema_pool: object = _UNSET,
        input_type_annotations: str | object = _UNSET,
        package: str | None = None,
    ) -> XdmDocument:
        """Return the grounded document identified by *uri*.

        A missing document without an authorizing policy is ``FODC0002``.  A
        policy denial propagates as :class:`ResourcePolicyError`; an ordinary
        read failure or malformed XML becomes ``FODC0002`` with the original
        exception preserved as the cause.  Schema-aware validation uses the
        requested pool and validation mode; a cached document is reused only
        for the same validation, type, pool, annotation mode, and package.
        The package's space declarations are applied to the captured document;
        a caller-owned registry entry is returned unchanged when no rule
        removes anything.
        """
        target = resolve_document_uri(uri, base)
        mode = self.validation if validation is None else validation
        if mode not in _VALIDATION_MODES:
            raise ValueError(f"invalid validation mode {mode!r}")
        requested_type = self.validation_type if type_name is None else type_name
        pool = self.schema_pool if schema_pool is _UNSET else schema_pool
        annotations = (
            self.input_type_annotations
            if input_type_annotations is _UNSET
            else input_type_annotations
        )
        if annotations not in _INPUT_TYPE_ANNOTATIONS:
            raise ValueError(f"invalid input-type-annotations value {annotations!r}")
        rules = self.space_rules.get(package, ())
        source_key = (target, mode, requested_type, id(pool), annotations)
        key = (*source_key, package)
        cached = self._variants.get(key)
        if cached is not None:
            return cached
        document = self._sources.get(source_key)
        if document is None:
            existing = self.documents.get(target)
            if existing is not None and not isinstance(existing, XdmDocument):  # pragma: no cover
                raise TypeError("a document registry entry must be an XdmDocument")
            policy = self._policy_for(target, label="document")
            if policy is None:
                if existing is None:
                    raise XsltDynamicError(
                        f"the document {uri!r} is not available to this transformation",
                        code="FODC0002",
                    )
                document = existing
            else:
                try:
                    data = policy.read(target, base=None, purpose=DOCUMENT_PURPOSE)
                except OSError as exc:
                    raise XsltDynamicError(
                        f"the document {uri!r} could not be read",
                        code="FODC0002",
                        cause=exc,
                    ) from exc
                if not isinstance(data, ResourceData):
                    raise TypeError("a resource policy read must return ResourceData")
                try:
                    document = parse_typed_document(
                        data.content,
                        schema_pool=cast("Any", pool),
                        validation=mode,
                        input_type_annotations=annotations,
                        type_name=requested_type,
                        document_uri=target,
                        base_uri=target,
                        source_name=data.source_id or target,
                    )
                except DocumentValidationError as exc:
                    code = {
                        "unknown": "XTTE1512",
                        "invalid": "XTTE1510",
                        "lax-invalid": "XTTE1515",
                        "type": "XTTE1540",
                    }[exc.reason]
                    raise XsltDynamicError(str(exc), code=code, cause=exc) from exc
                except PyXSDError as exc:
                    raise XsltDynamicError(
                        f"the document {uri!r} is not well-formed XML",
                        code="FODC0002",
                        cause=exc,
                    ) from exc
                if existing is None:
                    self.documents[target] = document
            self._sources[source_key] = document
        filtered = apply_source_space_rules(document, rules=rules)
        self._variants[key] = filtered
        return filtered

    def read_text(
        self,
        uri: str,
        *,
        base: str | None,
        encoding: str | None = None,
    ) -> str:
        """Return one authorized text resource with normalized newlines.

        The URI identifies the resource; the effective policy is the same one
        :meth:`resolve` uses for source documents.  A missing authorization is
        ``FOUT1170``, an unsupported encoding is ``FOUT1190``, and a policy
        denial keeps its own :class:`ResourcePolicyError` identity.  The cache
        belongs to this invocation and is keyed by target and encoding.
        """
        target = resolve_document_uri(uri, base)
        key = (target, encoding)
        if key in self._texts:
            return self._texts[key]
        policy = self._policy_for(target, label="text")
        if policy is None:
            raise XsltDynamicError(
                f"the text resource {uri!r} is not available to this transformation",
                code="FOUT1170",
            )
        try:
            data = policy.read(target, base=None, purpose=TEXT_PURPOSE)
        except OSError as exc:
            raise XsltDynamicError(
                f"the text resource {uri!r} could not be read",
                code="FOUT1170",
                cause=exc,
            ) from exc
        if not isinstance(data, ResourceData):
            raise TypeError("a resource policy read must return ResourceData")
        name = "utf-8" if encoding is None else encoding
        try:
            text = data.content.decode(name)
        except LookupError as exc:
            raise XsltDynamicError(
                f"the text encoding {name!r} is not supported",
                code="FOUT1190",
                cause=exc,
            ) from exc
        except UnicodeDecodeError as exc:
            raise XsltDynamicError(
                f"the text resource {uri!r} could not be decoded as {name}",
                code="FOUT1170",
                cause=exc,
            ) from exc
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._texts[key] = normalized
        return normalized

    def read_text_lines(
        self,
        uri: str,
        *,
        base: str | None,
        encoding: str | None = None,
    ) -> tuple[str, ...]:
        """Return the lines of one text resource after newline normalization."""
        text = self.read_text(uri, base=base, encoding=encoding)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return tuple(lines)

    def collection(self, uri: str | None, *, base: str | None) -> tuple[XdmNode, ...]:
        """Return one application-supplied collection, cached per invocation.

        ``uri`` is ``None`` for the default collection.  A missing resolver and
        an empty named collection are both ``FODC0002``; an empty default
        collection is a valid empty sequence.  The resolver is application
        data, not a byte-resource policy, so it never reads a file or network.
        """
        target = None if uri is None else resolve_document_uri(uri, base)
        cached = self._collections.get(target)
        if cached is not None:
            return cached
        resolver = self.collection_resolver
        if resolver is None:
            raise XsltDynamicError(
                "no default collection is available to this transformation"
                if target is None
                else f"the collection {target!r} is not available to this transformation",
                code="FODC0002",
            )
        items = tuple(resolver(target))
        if any(not isinstance(item, XdmNode) for item in items):
            raise TypeError("a collection resolver must return documents or nodes")
        if target is not None and not items:
            raise XsltDynamicError(
                f"the collection {target!r} is not available to this transformation",
                code="FODC0002",
            )
        self._collections[target] = items
        return items

    def collection_uris(self, uri: str | None, *, base: str | None) -> tuple[str, ...]:
        """Return the document URI of every item in one collection."""
        uris: list[str] = []
        for item in self.collection(uri, base=base):
            document_uri = item.owner_document_uri
            if document_uri is None:
                raise XsltDynamicError(
                    "a collection member has no document URI",
                    code="FODC0002",
                )
            uris.append(document_uri)
        return tuple(uris)

    def _policy_for(self, target: str, *, label: str) -> _DocumentPolicy | None:
        """Select the policy that owns one absolute target and scheme."""
        remote = urlsplit(target).scheme not in ("file", "urn")
        policy = self.remote_policy if remote else self.policy
        if remote and policy is None and self.policy is not None:
            raise ResourcePolicyError(f"remote stylesheet {label} access denied for {target!r}")
        return policy
