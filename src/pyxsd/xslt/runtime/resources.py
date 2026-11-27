"""Grounded XSLT document resources with a local-only default and explicit authorization.

A document is available to an instruction only when the invocation already
holds it or a resource policy admits a read.  There is no implicit network
retrieval, no streaming cursor, and no bounded-memory claim.  When no
application resource policy is configured, a canonical local ``file`` URI that
the invocation does not already hold is read by a local-only default; ``urn:``
identifiers still need an application policy, and remote URI schemes always
require a separate application policy whose trusted implementation is
responsible for origins, redirects, timeouts and budgets.  One invocation owns
one resolver, so cached documents and canonical URI identities never leak
between transformations.
"""

from __future__ import annotations

import codecs
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

from pyxsd.exceptions import PyXSDError
from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm.document import DocumentValidationError, parse_typed_document
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xslt.errors import XsltDynamicError, is_budget_alarm
from pyxsd.xslt.runtime.frames import ExecutionBudget, resource_error
from pyxsd.xslt.runtime.whitespace import (
    apply_source_space_rules,
    apply_source_space_rules_to_members,
)
from pyxsd.xslt.serialization.json_lexical import preescape_non_xml_characters

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


def _canonical_file_path(target: str) -> Path | None:
    """Return the filesystem path of one canonical absolute ``file`` URI.

    Only the canonical spelling names a local file: the ``file`` scheme, no
    authority, no query, and an absolute path.  Anything else returns ``None``
    so the caller refuses it instead of guessing a path from an authored URI.
    """
    parts = urlsplit(target)
    if parts.scheme != "file" or parts.netloc or parts.query or not parts.path.startswith("/"):
        return None
    return Path(url2pathname(parts.path))


class _LocalFilePolicy:
    """Read one canonical local ``file`` URI when no application policy exists.

    The bytes keep the ordinary policy shape, so document parsing, text
    decoding, cache keys, error mapping, and the document budget all run
    exactly as they do for an application-supplied read.
    """

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        """Read the local file named by a canonical absolute ``file`` URI."""
        path = _canonical_file_path(source)
        if path is None:
            raise ResourcePolicyError(
                f"the local {purpose} resource {source!r} is not a canonical file URI"
            )
        return ResourceData(source, path.read_bytes())


_LOCAL_FILE_POLICY = _LocalFilePolicy()

#: Byte order marks that select one JSON octet encoding.
_JSON_BOMS = (
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
)


def _json_encoding(content: bytes) -> str:
    """Select UTF-8, UTF-16, or UTF-32 from one JSON resource's initial octets.

    RFC 4627's BOM patterns take precedence; a UTF-32 little-endian mark starts
    with the UTF-16 little-endian mark, so the four-octet forms are checked
    first.  Without a mark the null-byte pattern of the first four octets
    distinguishes the remaining forms.
    """
    for bom, encoding in _JSON_BOMS:
        if content.startswith(bom):
            return encoding
    sample = content[:4]
    if len(sample) >= 4 and sample[0] == 0 and sample[1] == 0:
        return "utf-32-be"
    if len(sample) >= 4 and sample[2] == 0 and sample[3] == 0:
        return "utf-32-le"
    if len(sample) >= 2 and sample[0] == 0:
        return "utf-16-be"
    if len(sample) >= 2 and sample[1] == 0:
        return "utf-16-le"
    return "utf-8"


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
        budget: ExecutionBudget | None = None,
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
        if budget is not None and not isinstance(budget, ExecutionBudget):
            raise TypeError("a resource budget must be an ExecutionBudget value")
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
        self.budget = budget
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
        self._bytes: dict[str, bytes] = {}
        self._collections: dict[tuple[str | None, str | None], tuple[XdmNode, ...]] = {}
        #: The space-filtered copy of each collection member document, per
        #: package: one package's declarations produce one copy per source.
        self._collection_spaces: dict[str | None, dict[XdmDocument, XdmDocument]] = {}

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

        A missing document without an authorizing policy is ``FODC0002``; a
        canonical local ``file`` URI is instead read by the local-only default
        when no application policy is configured.  A policy denial propagates
        as :class:`ResourcePolicyError`; an ordinary read failure or malformed
        XML becomes ``FODC0002`` with the original exception preserved as the
        cause, but an execution-budget alarm is re-raised instead of converted.
        ``TimeoutError`` is one class, so a policy's own timeout and the
        caller's alarm are indistinguishable here; once a budget is set the
        alarm is by far the likelier origin.  Schema-aware validation uses the
        requested pool and validation
        mode; a cached document is reused only for the same validation, type,
        pool, annotation mode, and package.  The package's space declarations
        are applied to the captured document; a caller-owned registry entry is
        returned unchanged when no rule removes anything.
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
            policy = self._policy_for(target, label="document", held=existing is not None)
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
                    if is_budget_alarm(exc):
                        raise
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
                if self.budget is not None and not self.budget.take_document():
                    raise resource_error("the transformation exceeded its document budget")
                if existing is None:
                    self.documents[target] = document
            self._sources[source_key] = document
        filtered = apply_source_space_rules(document, rules=rules)
        self._variants[key] = filtered
        return filtered

    def document_variants(self, package: str | None) -> dict[str, XdmDocument]:
        """The package-filtered variants of documents already resolved.

        A pattern's ``doc()`` must match the same node the invoking package's
        ``doc()`` delivered; strip/preserve-space rules make that a different
        tree from the shared unfiltered registry entry.
        """
        return {key[0]: document for key, document in self._variants.items() if key[-1] == package}

    def read_text(
        self,
        uri: str,
        *,
        base: str | None,
        encoding: str | None = None,
    ) -> str:
        """Return one authorized text resource with normalized newlines.

        The URI identifies the resource; the effective policy is the same one
        :meth:`resolve` uses for source documents, including the local-only
        default for a canonical ``file`` URI.  A missing authorization is
        ``FOUT1170``, an unsupported encoding is ``FOUT1190``, and a policy
        denial keeps its own :class:`ResourcePolicyError` identity.  As in
        :meth:`resolve`, an execution-budget alarm is re-raised instead of
        becoming ``FOUT1170``: a policy's own ``TimeoutError`` and the caller's
        alarm share one class, and the alarm is the likelier origin once a
        budget is set.  The cache belongs to this invocation and is keyed by
        target and encoding.
        """
        target = resolve_document_uri(uri, base)
        key = (target, encoding)
        if key in self._texts:
            return self._texts[key]
        content = self.read_bytes(uri, base=base)
        name = "utf-8" if encoding is None else encoding
        try:
            text = content.decode(name)
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

    def read_json_text(self, uri: str, *, base: str | None) -> str:
        """Return one authorized JSON resource as parseable text.

        RFC 7159 requires UTF-8, UTF-16, and UTF-32, distinguished by their
        initial octets; a byte order mark selects the encoding and is otherwise
        left for :func:`parse_json_text` to ignore.  Codepoints XML cannot hold
        are expressed as JSON escapes so the grammar stays parseable; the
        parser turns them back into the fallback-eligible characters.  The
        octet read is shared with :meth:`read_text` through
        :meth:`read_bytes`, so a later ``unparsed-text`` call for the same
        canonical URI observes one policy read.
        """
        target = resolve_document_uri(uri, base)
        content = self.read_bytes(uri, base=base)
        encoding = _json_encoding(content)
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError as exc:
            raise XsltDynamicError(
                f"the text resource {uri!r} could not be decoded as {encoding}",
                code="FOUT1170",
                cause=exc,
            ) from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if encoding == "utf-8":
            # The same octets supply the plain text read; a UTF-16 or UTF-32
            # resource must not be served to unparsed-text under another
            # encoding's cache key.
            self._texts[(target, None)] = text
        return preescape_non_xml_characters(text)

    def read_bytes(self, uri: str, *, base: str | None) -> bytes:
        """Return the authorized octets of one text resource.

        This is the one policy read behind both :meth:`read_text` and
        :meth:`read_json_text`, so the canonical URI identity is preserved
        across encodings and functions.
        """
        target = resolve_document_uri(uri, base)
        cached = self._bytes.get(target)
        if cached is not None:
            return cached
        policy = self._policy_for(target, label="text")
        if policy is None:
            raise XsltDynamicError(
                f"the text resource {uri!r} is not available to this transformation",
                code="FOUT1170",
            )
        try:
            data = policy.read(target, base=None, purpose=TEXT_PURPOSE)
        except OSError as exc:
            if is_budget_alarm(exc):
                raise
            raise XsltDynamicError(
                f"the text resource {uri!r} could not be read",
                code="FOUT1170",
                cause=exc,
            ) from exc
        if not isinstance(data, ResourceData):
            raise TypeError("a resource policy read must return ResourceData")
        content = bytes(data.content)
        self._bytes[target] = content
        return content

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
        if lines[-1] == "":
            lines.pop()
        return tuple(lines)

    def collection(
        self,
        uri: str | None,
        *,
        base: str | None,
        package: str | None = None,
    ) -> tuple[XdmNode, ...]:
        """Return one application-supplied collection, cached per invocation.

        ``uri`` is ``None`` for the default collection.  A missing resolver and
        an empty named collection are both ``FODC0002``; an empty default
        collection is a valid empty sequence.  The resolver is application
        data, not a byte-resource policy, so it never reads a file or network.

        XSLT 3.0 section 4.4 strips the whitespace of every document that
        supplies a member, using the declarations of the package that called
        ``fn:collection``.  The filtered copy belongs to this invocation, so
        two collections that serve the same member observe one tree.
        """
        target = None if uri is None else resolve_document_uri(uri, base)
        key = (target, package)
        cached = self._collections.get(key)
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
        rules = self.space_rules.get(package, ())
        if rules:
            items = apply_source_space_rules_to_members(
                items,
                rules=rules,
                filtered=self._collection_spaces.setdefault(package, {}),
            )
        self._collections[key] = items
        return items

    def collection_uris(
        self,
        uri: str | None,
        *,
        base: str | None,
        package: str | None = None,
    ) -> tuple[str, ...]:
        """Return the document URI of every item in one collection."""
        uris: list[str] = []
        for item in self.collection(uri, base=base, package=package):
            document_uri = item.owner_document_uri
            if document_uri is None:
                raise XsltDynamicError(
                    "a collection member has no document URI",
                    code="FODC0002",
                )
            uris.append(document_uri)
        return tuple(uris)

    def _policy_for(self, target: str, *, label: str, held: bool = False) -> _DocumentPolicy | None:
        """Select the policy that owns one absolute target and scheme.

        A canonical local ``file`` URI falls back to the local-only default when
        no application policy is configured and the invocation does not already
        hold that document; *held* keeps an invocation-owned document from being
        reread or charged to the document budget.  ``urn:`` identifiers and
        remote schemes keep their explicit-policy behavior, and a local denial
        from that default stays a :class:`ResourcePolicyError`.
        """
        scheme = urlsplit(target).scheme
        remote = scheme not in ("file", "urn")
        policy = self.remote_policy if remote else self.policy
        if remote and policy is None and self.policy is not None:
            raise ResourcePolicyError(f"remote stylesheet {label} access denied for {target!r}")
        if policy is None and scheme == "file" and not held:
            return _LOCAL_FILE_POLICY
        return policy
