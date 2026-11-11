"""Grounded XSLT document resources with explicit authorization.

A document is available to an instruction only when the invocation already
holds it or an application-supplied resource policy admits a read.  There is no
implicit filesystem or network retrieval, no streaming cursor, and no
bounded-memory claim.  One invocation owns one resolver, so cached documents
and canonical URI identities never leak between transformations.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourceData
from pyxsd.xdm.document import DocumentValidationError, parse_typed_document
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt.errors import XsltDynamicError

__all__ = ["DocumentResources", "resolve_document_uri"]

#: Resource-policy purpose for an XSLT source document.  Source documents are
#: XML instances, so the core policy vocabulary already covers them.
DOCUMENT_PURPOSE = "instance"
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
    if base is None:
        raise XsltDynamicError(
            f"the relative document URI {uri!r} has no base URI",
            code="XTDE1162",
        )
    return urljoin(base, value)


class DocumentResources:
    """Resolve grounded documents for one transformation invocation.

    *documents* is the invocation's shared document registry.  A document that
    is already present is returned unchanged; a policy read is cached under the
    same canonical URI so later ``document()`` or ``xsl:source-document`` calls
    observe the same grounded nodes.
    """

    def __init__(
        self,
        documents: MutableMapping[str, XdmDocument],
        *,
        policy: object = None,
        schema_pool: object = None,
        validation: str = "strip",
        validation_type: str | None = None,
        input_type_annotations: str = "unspecified",
    ) -> None:
        if not isinstance(documents, MutableMapping):
            raise TypeError("document resources require a mutable document mapping")
        if policy is not None and not callable(getattr(policy, "read", None)):
            raise TypeError("a document resource policy must provide read()")
        if validation not in _VALIDATION_MODES:
            raise ValueError(f"invalid validation mode {validation!r}")
        if validation_type is not None and not isinstance(validation_type, str):
            raise TypeError("validation_type must be a string or None")
        if input_type_annotations not in _INPUT_TYPE_ANNOTATIONS:
            raise ValueError(f"invalid input-type-annotations value {input_type_annotations!r}")
        self.documents = documents
        self.policy = cast(_DocumentPolicy | None, policy)
        self.schema_pool = schema_pool
        self.validation = validation
        self.validation_type = validation_type
        self.input_type_annotations = input_type_annotations
        self._variants: dict[tuple[str, str, str | None, int, str], XdmDocument] = {}

    def resolve(
        self,
        uri: str,
        *,
        base: str | None,
        validation: str | None = None,
        type_name: str | None = None,
        schema_pool: object = _UNSET,
        input_type_annotations: str | object = _UNSET,
    ) -> XdmDocument:
        """Return the grounded document identified by *uri*.

        A missing document without an authorizing policy is ``FODC0002``.  A
        policy denial propagates as :class:`ResourcePolicyError`; an ordinary
        read failure or malformed XML becomes ``FODC0002`` with the original
        exception preserved as the cause.  Schema-aware validation uses the
        requested pool and validation mode; a cached document is reused only
        for the same validation, type, pool, and annotation mode.
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
        key = (target, mode, requested_type, id(pool), annotations)
        cached = self._variants.get(key)
        if cached is not None:
            return cached
        existing = self.documents.get(target)
        if existing is not None and not isinstance(existing, XdmDocument):  # pragma: no cover
            raise TypeError("a document registry entry must be an XdmDocument")
        if self.policy is None:
            if existing is not None:
                return existing
            raise XsltDynamicError(
                f"the document {uri!r} is not available to this transformation",
                code="FODC0002",
            )
        try:
            data = self.policy.read(target, base=None, purpose=DOCUMENT_PURPOSE)
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
        self._variants[key] = document
        if existing is None:
            self.documents[target] = document
        return document
