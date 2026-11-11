"""Grounded XSLT document resources with explicit authorization.

A document is available to an instruction only when the invocation already
holds it or an application-supplied resource policy admits a read.  There is no
implicit filesystem or network retrieval, no streaming cursor, and no
bounded-memory claim.  One invocation owns one resolver, so cached documents
and canonical URI identities never leak between transformations.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit

from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourceData
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.runtime.instructions import construct_grounded_document

__all__ = ["DocumentResources", "resolve_document_uri"]

#: Resource-policy purpose for an XSLT source document.  Source documents are
#: XML instances, so the core policy vocabulary already covers them.
DOCUMENT_PURPOSE = "instance"


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
    ) -> None:
        if not isinstance(documents, MutableMapping):
            raise TypeError("document resources require a mutable document mapping")
        if policy is not None and not callable(getattr(policy, "read", None)):
            raise TypeError("a document resource policy must provide read()")
        self.documents = documents
        self.policy = cast(_DocumentPolicy | None, policy)
        self.schema_pool = schema_pool

    def resolve(self, uri: str, *, base: str | None) -> XdmDocument:
        """Return the grounded document identified by *uri*.

        A missing document without an authorizing policy is ``FODC0002``.  A
        policy denial propagates as :class:`ResourcePolicyError`; an ordinary
        read failure or malformed XML becomes ``FODC0002`` with the original
        exception preserved as the cause.
        """
        target = resolve_document_uri(uri, base)
        cached = self.documents.get(target)
        if cached is not None:
            if not isinstance(cached, XdmDocument):  # pragma: no cover - registry invariant
                raise TypeError("a document registry entry must be an XdmDocument")
            return cached
        if self.policy is None:
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
            document = construct_grounded_document(
                data.content,
                uri=target,
                source_name=data.source_id or target,
                schema_pool=self.schema_pool,
            )
        except PyXSDError as exc:
            raise XsltDynamicError(
                f"the document {uri!r} is not well-formed XML",
                code="FODC0002",
                cause=exc,
            ) from exc
        self.documents[target] = document
        return document
