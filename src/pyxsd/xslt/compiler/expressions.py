"""Dedicated XSLT grammars for value templates and sequence types.

Attribute value templates, text value templates, and sequence types are not
XPath expressions.  This module keeps their grammar separate from the Task 11
expression engine:

* a value template is decomposed into literal text and expression parts, with
  doubled-brace escaping, nested braces, XPath strings, and XPath comments
  understood by the template scanner;
* every expression part then compiles through
  :func:`pyxsd.xpath.engine.XPathEngine` against the element's immutable
  :class:`~pyxsd.xslt.compiler.context.StaticContext`; and
* a sequence type is parsed by its own recursive-descent grammar, including
  item/node/element/attribute/schema/document/function/map/array tests and
  occurrence indicators, with version-specific grammar gates.

No pattern is compiled here, and no sequence type is obtained by evaluating an
arbitrary XPath expression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, NoReturn, TypeAlias

from pyxsd.locations import IssueLocation
from pyxsd.xdm.annotations import builtin_type_classes
from pyxsd.xpath.engine import CompiledExpression, XPathEngine
from pyxsd.xslt.errors import XsltStaticError

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for type checking
    from pyxsd.xslt.compiler.context import StaticContext

XSD_NS = "http://www.w3.org/2001/XMLSchema"

__all__ = [
    "CompiledSequenceType",
    "CompiledValueTemplate",
    "ItemType",
    "Occurrence",
    "TemplatePart",
    "compile_avt",
    "compile_sequence_type",
    "compile_tvt",
    "compile_xpath",
]

Occurrence: TypeAlias = Literal["", "?", "*", "+"]
ItemKind: TypeAlias = Literal[
    "item",
    "atomic",
    "node",
    "text",
    "comment",
    "namespace-node",
    "processing-instruction",
    "element",
    "attribute",
    "schema-element",
    "schema-attribute",
    "document-node",
    "function",
    "map",
    "array",
]

_XPATH_WHITESPACE = " \t\r\n"
_OCCURRENCES = frozenset({"?", "*", "+"})
_KIND_TESTS = frozenset(
    {
        "array",
        "attribute",
        "comment",
        "document-node",
        "element",
        "function",
        "map",
        "namespace-node",
        "node",
        "processing-instruction",
        "schema-attribute",
        "schema-element",
        "text",
    }
)
_NCNAME = re.compile(
    r"[^\W\d][\w.\-\u00B7\u0300-\u036F\u203F-\u2040]*",
    re.UNICODE,
)
_NAME_START = re.compile(
    r"[^\W\d]",
    re.UNICODE,
)
_ERROR_CODE = re.compile(r"\[(?:err:)?([A-Z]{4}[0-9]{4})\]")

_ATOMIC_BUILTINS = frozenset(builtin_type_classes()) - {
    "anyType",
    "anySimpleType",
    "untyped",
}
_SIMPLE_BUILTINS = frozenset(builtin_type_classes()) - {"anyType", "untyped"}
_UNIFIED_ATOMIC = frozenset(set(_ATOMIC_BUILTINS) | {"untypedAtomic"})
_UNIFIED_SIMPLE = frozenset(set(_SIMPLE_BUILTINS) | {"untypedAtomic"})
_ELEMENT_BUILTINS = frozenset(set(_SIMPLE_BUILTINS) | {"anyType", "untyped", "untypedAtomic"})


def _error_code(exc: BaseException, default: str = "XPST0003") -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code.rsplit(":", 1)[-1]
    match = _ERROR_CODE.search(str(exc))
    return default if match is None else match.group(1)


def _static_error(
    context: StaticContext,
    message: str,
    code: str,
    *,
    location: IssueLocation | None = None,
    cause: BaseException | None = None,
) -> XsltStaticError:
    return XsltStaticError(
        message,
        code=code,
        uri=context.uri,
        location=location or context.location,
        cause=cause,
    )


# ---------------------------------------------------------------------------
# Dedicated sequence-type grammar


@dataclass(frozen=True, slots=True)
class ItemType:
    """One sequence-type item test, structurally compiled."""

    kind: ItemKind
    name: str | None = None
    lexical: str | None = None
    namespace_uri: str | None = None
    type_name: str | None = None
    type_lexical: str | None = None
    nillable: bool = False
    wildcard: bool = False
    target: str | None = None
    target_is_name: bool = False
    child: ItemType | None = None
    parameters: tuple[CompiledSequenceType, ...] = ()
    return_type: CompiledSequenceType | None = None
    key_type: ItemType | None = None
    value_type: CompiledSequenceType | None = None
    member_type: CompiledSequenceType | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KIND_TESTS and self.kind not in {"item", "atomic"}:
            raise ValueError(f"unknown item type kind {self.kind!r}")
        if self.kind == "atomic" and (self.name is None or self.lexical is None):
            raise ValueError("an atomic item type requires its expanded and lexical names")
        if self.kind in ("schema-element", "schema-attribute") and self.name is None:
            raise ValueError(f"a {self.kind} test requires a declaration name")
        if self.kind == "document-node" and self.child is None and self.wildcard:
            raise ValueError("document-node() has no wildcard form")
        if self.kind == "function" and not self.wildcard and self.return_type is None:
            raise ValueError("a typed function test requires a return type")
        if (
            self.kind == "map"
            and not self.wildcard
            and (self.key_type is None or self.value_type is None)
        ):
            raise ValueError("a typed map test requires key and value types")
        if self.kind == "array" and not self.wildcard and self.member_type is None:
            raise ValueError("a typed array test requires a member type")

    def __str__(self) -> str:
        kind = self.kind
        if kind == "item":
            return "item()"
        if kind == "atomic":
            return self.lexical or self.name or ""
        if kind in ("node", "text", "comment", "namespace-node"):
            return f"{kind}()"
        if kind == "processing-instruction":
            if self.target is None:
                return "processing-instruction()"
            target = self.target if self.target_is_name else _quote_literal(self.target)
            return f"processing-instruction({target})"
        if kind in ("element", "attribute"):
            if self.wildcard:
                name = self.lexical or "*"
            elif self.name is not None:
                name = self.lexical or self.name
            else:
                name = ""
            if self.type_name is not None:
                type_name = self.type_lexical or self.type_name
                suffix = ", " + type_name + ("?" if self.nillable else "")
            else:
                suffix = ""
                if self.nillable:  # pragma: no cover - grammar requires a type
                    suffix = "?"
            return f"{kind}({name}{suffix})"
        if kind in ("schema-element", "schema-attribute"):
            return f"{kind}({self.lexical or self.name})"
        if kind == "document-node":
            if self.child is None:
                return "document-node()"
            return f"document-node({self.child})"
        if kind == "function":
            if self.wildcard:
                return "function(*)"
            parameters = ", ".join(str(parameter) for parameter in self.parameters)
            return f"function({parameters}) as {self.return_type}"
        if kind == "map":
            if self.wildcard:
                return "map(*)"
            return f"map({self.key_type}, {self.value_type})"
        if kind == "array":
            if self.wildcard:
                return "array(*)"
            return f"array({self.member_type})"
        raise ValueError(f"unknown item type kind {kind!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class CompiledSequenceType:
    """One compiled sequence type with its occurrence indicator."""

    item_type: ItemType | None = None
    occurrence: Occurrence = ""
    source: str = ""

    def __post_init__(self) -> None:
        if self.occurrence not in {"", "?", "*", "+"}:
            raise ValueError(f"invalid occurrence indicator {self.occurrence!r}")
        if self.item_type is None and self.occurrence:
            raise ValueError("empty-sequence() cannot have an occurrence indicator")
        if self.item_type is not None and not isinstance(self.item_type, ItemType):
            raise TypeError("item_type must be an ItemType or None")
        if not isinstance(self.source, str):
            raise TypeError("source must be a string")

    def __str__(self) -> str:
        if self.item_type is None:
            return "empty-sequence()"
        return f"{self.item_type}{self.occurrence}"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: Literal["name", "qname", "wildcard", "string", "punct", "eof"]
    value: str
    offset: int


class _SequenceTypeCompiler:
    """Recursive-descent compiler for the XPath 2.0/3.1 SequenceType grammar."""

    def __init__(
        self,
        source: str,
        context: StaticContext,
        location: IssueLocation | None,
    ) -> None:
        self.source = source
        self.context = context
        self.location = location
        self.tokens = self._tokenize()
        self.index = 0

    # -- lexing ----------------------------------------------------------------

    def _tokenize(self) -> tuple[_Token, ...]:
        source = self.source
        tokens: list[_Token] = []
        index = 0
        length = len(source)
        while index < length:
            character = source[index]
            if character in _XPATH_WHITESPACE:
                index += 1
                continue
            if source.startswith("(:", index):
                index = self._skip_comment(index)
                continue
            if character == "*":
                if index + 1 < length and source[index + 1] == ":":
                    local = _NCNAME.match(source, index + 2)
                    if local is not None:
                        tokens.append(_Token("wildcard", source[index : local.end()], index))
                        index = local.end()
                        continue
                tokens.append(_Token("punct", "*", index))
                index += 1
                continue
            if character in "(),?+":
                tokens.append(_Token("punct", character, index))
                index += 1
                continue
            if character in ("'", '"'):
                token, index = self._scan_string(index)
                tokens.append(token)
                continue
            if source.startswith("Q{", index):
                token, index = self._scan_eqname(index)
                tokens.append(token)
                continue
            match = _NCNAME.match(source, index)
            if match is not None:
                end = match.end()
                if end < length and source[end] == ":":
                    if end + 1 < length and source[end + 1] == "*":
                        tokens.append(_Token("wildcard", source[index : end + 2], index))
                        index = end + 2
                        continue
                    if _NAME_START.match(source, end + 1):
                        local = _NCNAME.match(source, end + 1)
                        assert local is not None
                        end = local.end()
                        tokens.append(_Token("qname", source[index:end], index))
                        index = end
                        continue
                tokens.append(_Token("name", source[index:end], index))
                index = end
                continue
            self._fail("XPST0003", f"unexpected character {character!r} at offset {index}")
        tokens.append(_Token("eof", "", length))
        return tuple(tokens)

    def _skip_comment(self, start: int) -> int:
        depth = 0
        index = start
        while index < len(self.source):
            if self.source.startswith("(:", index):
                depth += 1
                index += 2
                continue
            if self.source.startswith(":)", index):
                depth -= 1
                index += 2
                if depth == 0:
                    return index
                continue
            index += 1
        self._fail("XPST0003", "unterminated XPath comment in sequence type")
        raise AssertionError("unreachable")  # pragma: no cover

    def _scan_string(self, start: int) -> tuple[_Token, int]:
        quote = self.source[start]
        index = start + 1
        value: list[str] = []
        while index < len(self.source):
            character = self.source[index]
            if character == quote:
                if index + 1 < len(self.source) and self.source[index + 1] == quote:
                    value.append(quote)
                    index += 2
                    continue
                return _Token("string", "".join(value), start), index + 1
            value.append(character)
            index += 1
        self._fail("XPST0003", "unterminated string literal in sequence type")
        raise AssertionError("unreachable")  # pragma: no cover

    def _scan_eqname(self, start: int) -> tuple[_Token, int]:
        close = self.source.find("}", start + 2)
        if close < 0:
            self._fail("XPST0003", "unterminated EQName in sequence type")
        if close + 1 < len(self.source) and self.source[close + 1] == "*":
            return _Token("wildcard", self.source[start : close + 2], start), close + 2
        local = _NCNAME.match(self.source, close + 1)
        if local is None:
            self._fail("XPST0003", "an EQName requires a local name")
            raise AssertionError("unreachable")  # pragma: no cover
        return _Token("qname", self.source[start : local.end()], start), local.end()

    # -- token helpers ---------------------------------------------------------

    def _peek(self, offset: int = 0) -> _Token:
        position = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[position]

    def _next(self) -> _Token:
        token = self._peek()
        self.index = min(self.index + 1, len(self.tokens) - 1)
        return token

    def _is_punct(self, value: str, offset: int = 0) -> bool:
        token = self._peek(offset)
        return token.kind == "punct" and token.value == value

    def _is_name(self, value: str, offset: int = 0) -> bool:
        token = self._peek(offset)
        return token.kind == "name" and token.value == value

    def _expect_punct(self, value: str) -> None:
        token = self._peek()
        if token.kind != "punct" or token.value != value:
            self._fail(
                "XPST0003",
                f"expected {value!r} at offset {token.offset}, found {token.value!r}",
            )
        self._next()

    def _expect_name(self, value: str) -> None:
        token = self._peek()
        if token.kind != "name" or token.value != value:
            self._fail(
                "XPST0003",
                f"expected {value!r} at offset {token.offset}, found {token.value!r}",
            )
        self._next()

    def _fail(self, code: str, message: str) -> NoReturn:
        raise _static_error(
            self.context,
            f"invalid sequence type {self.source!r}: {message}",
            code,
            location=self.location,
        )

    # -- grammar ---------------------------------------------------------------

    def compile(self) -> CompiledSequenceType:
        if not self.source.strip():
            self._fail("XPST0003", "a sequence type must not be empty")
        compiled = self._sequence_type()
        final = self._peek()
        if final.kind != "eof":
            self._fail(
                "XPST0003",
                f"unexpected token {final.value!r} at offset {final.offset}",
            )
        return replace(compiled, source=self.source)

    def _sequence_type(self) -> CompiledSequenceType:
        if self._is_name("empty-sequence") and self._is_punct("(", 1):
            self._next()
            self._expect_punct("(")
            self._expect_punct(")")
            if self._peek().kind == "punct" and self._peek().value in _OCCURRENCES:
                self._fail(
                    "XPST0003",
                    "empty-sequence() cannot have an occurrence indicator",
                )
            return CompiledSequenceType(None)
        item = self._item_type()
        occurrence: Occurrence = ""
        token = self._peek()
        if token.kind == "punct" and token.value in _OCCURRENCES:
            occurrence = token.value  # type: ignore[assignment]
            self._next()
        return CompiledSequenceType(item, occurrence)

    def _item_type(self) -> ItemType:
        token = self._peek()
        if token.kind == "punct" and token.value == "(":
            self._require_xpath("3.0", "parenthesized item types")
            self._next()
            item = self._item_type()
            self._expect_punct(")")
            return item
        if token.kind not in ("name", "qname"):
            self._fail(
                "XPST0003",
                f"expected an item type at offset {token.offset}, found {token.value!r}",
            )
        if token.kind == "qname":
            return self._atomic_type()
        name = token.value
        if name == "empty-sequence" and self._is_punct("(", 1):
            self._fail("XPST0003", "empty-sequence() is a sequence type, not an item type")
        if name == "item" and self._is_punct("(", 1):
            self._next()
            self._expect_punct("(")
            self._expect_punct(")")
            return ItemType("item")
        if name in _KIND_TESTS and self._is_punct("(", 1):
            return self._kind_test(name)
        return self._atomic_type()

    def _kind_test(self, name: str) -> ItemType:
        if name in ("node", "text", "comment"):
            self._next()
            self._expect_punct("(")
            self._expect_punct(")")
            return ItemType(name)  # type: ignore[arg-type]
        if name == "namespace-node":
            self._require_xpath("3.0", "namespace-node()")
            self._next()
            self._expect_punct("(")
            self._expect_punct(")")
            return ItemType("namespace-node")
        if name == "processing-instruction":
            return self._processing_instruction()
        if name == "element":
            return self._element_test()
        if name == "attribute":
            return self._attribute_test()
        if name == "schema-element":
            return self._schema_test("schema-element")
        if name == "schema-attribute":
            return self._schema_test("schema-attribute")
        if name == "document-node":
            return self._document_test()
        if name == "function":
            return self._function_test()
        if name == "map":
            return self._map_test()
        if name == "array":
            return self._array_test()
        raise AssertionError(f"unhandled kind test {name!r}")  # pragma: no cover

    def _processing_instruction(self) -> ItemType:
        self._next()
        self._expect_punct("(")
        target: str | None = None
        target_is_name = False
        token = self._peek()
        if token.kind == "name":
            self._next()
            target = token.value
            target_is_name = True
        elif token.kind == "string":
            self._next()
            if _NCNAME.fullmatch(token.value) is None:
                self._fail("XPST0003", "a processing-instruction target must be an NCName")
            target = token.value
        elif not (token.kind == "punct" and token.value == ")"):
            self._fail("XPST0003", "invalid processing-instruction target")
        self._expect_punct(")")
        return ItemType(
            "processing-instruction",
            target=target,
            target_is_name=target_is_name,
        )

    def _element_test(self) -> ItemType:
        self._next()
        self._expect_punct("(")
        if self._is_punct(")"):
            self._next()
            return ItemType("element")
        name, lexical, wildcard, namespace_uri = self._name_or_wildcard(element=True)
        type_name: str | None = None
        type_lexical: str | None = None
        nillable = False
        if self._is_punct(","):
            self._next()
            type_name, type_lexical = self._type_name()
            if self._is_punct("?"):
                self._next()
                nillable = True
        self._expect_punct(")")
        if type_name is not None:
            self._validate_type(type_name, attribute=False)
        return ItemType(
            "element",
            name=name,
            lexical=lexical,
            namespace_uri=namespace_uri,
            type_name=type_name,
            type_lexical=type_lexical,
            nillable=nillable,
            wildcard=wildcard,
        )

    def _attribute_test(self) -> ItemType:
        self._next()
        self._expect_punct("(")
        if self._is_punct(")"):
            self._next()
            return ItemType("attribute")
        name, lexical, wildcard, namespace_uri = self._name_or_wildcard(element=False)
        type_name: str | None = None
        type_lexical: str | None = None
        if self._is_punct(","):
            self._next()
            type_name, type_lexical = self._type_name()
        self._expect_punct(")")
        if type_name is not None:
            self._validate_type(type_name, attribute=True)
        return ItemType(
            "attribute",
            name=name,
            lexical=lexical,
            namespace_uri=namespace_uri,
            type_name=type_name,
            type_lexical=type_lexical,
            wildcard=wildcard,
        )

    def _schema_test(self, kind: Literal["schema-element", "schema-attribute"]) -> ItemType:
        self._next()
        self._expect_punct("(")
        token = self._peek()
        if token.kind not in ("name", "qname"):
            self._fail("XPST0003", f"{kind} requires a declaration name")
        self._next()
        name, lexical = self._expand_qname(
            token.value,
            kind="element" if kind == "schema-element" else "attribute",
        )
        self._expect_punct(")")
        pool = self.context.schema
        declaration = None
        if pool is not None:
            declaration = (
                pool.get_element(name) if kind == "schema-element" else pool.get_attribute(name)
            )
        if declaration is None:
            self._fail(
                "XPST0008",
                f"no {kind} declaration for {lexical!r} is in the in-scope schema definitions",
            )
        return ItemType(kind, name=name, lexical=lexical)

    def _document_test(self) -> ItemType:
        self._next()
        self._expect_punct("(")
        child: ItemType | None = None
        if self._is_punct(")"):
            self._next()
            return ItemType("document-node")
        if self._peek().kind == "name" and self._peek().value in (
            "element",
            "schema-element",
        ):
            child = self._kind_test(self._peek().value)
        else:
            self._fail(
                "XPST0003",
                "document-node() accepts only an element or schema-element test",
            )
        self._expect_punct(")")
        return ItemType("document-node", child=child)

    def _function_test(self) -> ItemType:
        self._require_xpath("3.0", "function tests")
        self._next()
        self._expect_punct("(")
        if self._is_punct("*"):
            self._next()
            self._expect_punct(")")
            return ItemType("function", wildcard=True)
        parameters: list[CompiledSequenceType] = []
        if not self._is_punct(")"):
            parameters.append(self._sequence_type())
            while self._is_punct(","):
                self._next()
                parameters.append(self._sequence_type())
        self._expect_punct(")")
        self._expect_name("as")
        return_type = self._sequence_type()
        return ItemType(
            "function",
            parameters=tuple(parameters),
            return_type=return_type,
        )

    def _map_test(self) -> ItemType:
        self._require_xpath("3.1", "map tests")
        self._next()
        self._expect_punct("(")
        if self._is_punct("*"):
            self._next()
            self._expect_punct(")")
            return ItemType("map", wildcard=True)
        token = self._peek()
        if token.kind not in ("name", "qname"):
            self._fail("XPST0003", "a map key type must be an atomic type name")
        key_type = self._atomic_type()
        self._expect_punct(",")
        value_type = self._sequence_type()
        self._expect_punct(")")
        return ItemType("map", key_type=key_type, value_type=value_type)

    def _array_test(self) -> ItemType:
        self._require_xpath("3.1", "array tests")
        self._next()
        self._expect_punct("(")
        if self._is_punct("*"):
            self._next()
            self._expect_punct(")")
            return ItemType("array", wildcard=True)
        member_type = self._sequence_type()
        self._expect_punct(")")
        return ItemType("array", member_type=member_type)

    def _atomic_type(self) -> ItemType:
        token = self._peek()
        self._next()
        name, lexical = self._expand_qname(token.value, kind="type")
        self._validate_type(name, atomic=True)
        return ItemType("atomic", name=name, lexical=lexical)

    def _name_or_wildcard(
        self, *, element: bool
    ) -> tuple[str | None, str | None, bool, str | None]:
        if self._is_punct("*"):
            self._next()
            return None, None, True, None
        token = self._peek()
        if token.kind == "wildcard":
            self._next()
            name, namespace_uri = self._expand_wildcard(token.value)
            return name, token.value, True, namespace_uri
        if token.kind not in ("name", "qname"):
            self._fail(
                "XPST0003",
                f"expected a {'element' if element else 'attribute'} name or '*'",
            )
        self._next()
        name, lexical = self._expand_qname(token.value, kind="element" if element else "attribute")
        return name, lexical, False, None

    def _expand_wildcard(self, raw: str) -> tuple[str | None, str | None]:
        if raw.startswith("Q{"):
            self._require_xpath("3.0", "EQNames")
            close = raw.find("}")
            eqname_uri = raw[2:close]
            return None, eqname_uri
        if raw.startswith("*:"):
            return raw[2:], None
        prefix, _, _ = raw.partition(":")
        uri = self.context.namespaces.get(prefix)
        if uri is None:
            self._fail(
                "XPST0081",
                f"the namespace prefix {prefix!r} is not bound in the static context",
            )
        return None, uri

    def _type_name(self) -> tuple[str, str]:
        token = self._peek()
        if token.kind not in ("name", "qname"):
            self._fail("XPST0003", f"expected a type name at offset {token.offset}")
        self._next()
        return self._expand_qname(token.value, kind="type")

    # -- names, versions, and schema checks ------------------------------------

    def _expand_qname(self, raw: str, *, kind: str) -> tuple[str, str]:
        if raw.startswith("Q{"):
            self._require_xpath("3.0", "EQNames")
            close = raw.find("}")
            eqname_uri = raw[2:close]
            local = raw[close + 1 :]
            expanded = local if not eqname_uri else f"{{{eqname_uri}}}{local}"
            return expanded, raw
        prefix, separator, local = raw.partition(":")
        if separator:
            uri = self.context.namespaces.get(prefix)
            if uri is None:
                self._fail(
                    "XPST0081",
                    f"the namespace prefix {prefix!r} is not bound in the static context",
                )
            return f"{{{uri}}}{local}", raw
        if kind in ("element", "type") and self.context.xpath_default_namespace:
            return f"{{{self.context.xpath_default_namespace}}}{raw}", raw
        return raw, raw

    def _validate_type(self, name: str, *, atomic: bool = False, attribute: bool = False) -> None:
        pool = self.context.schema
        namespace, _, local_name = (
            name[1:].partition("}") if name.startswith("{") else ("", "", name)
        )
        if pool is not None:
            adapter = pool.get_type(name)
            if adapter is None:
                if namespace == XSD_NS and local_name == "untypedAtomic":
                    return
                if namespace == XSD_NS and local_name == "untyped" and not attribute:
                    if atomic:
                        self._fail(
                            "XPST0051",
                            f"type {name!r} is not an atomic type or a union of atomic types",
                        )
                    return
                self._fail("XPST0051", f"type {name!r} is not in the in-scope schema types")
            if atomic and (
                local_name in {"anyType", "anySimpleType", "untyped"} or not adapter.is_atomic()
            ):
                self._fail(
                    "XPST0051",
                    f"type {name!r} is not an atomic type or a union of atomic types",
                )
            if attribute and not adapter.is_simple():
                self._fail("XPST0051", f"attribute type {name!r} is not a simple type")
            return

        if namespace != XSD_NS:
            self._fail("XPST0051", f"type {name!r} is not in the in-scope schema types")
        if atomic:
            if local_name not in _UNIFIED_ATOMIC:
                self._fail(
                    "XPST0051",
                    f"type {name!r} is not an atomic type or a union of atomic types",
                )
            return
        if attribute:
            if local_name not in _UNIFIED_SIMPLE:
                self._fail("XPST0051", f"attribute type {name!r} is not a simple type")
            return
        if local_name not in _ELEMENT_BUILTINS:  # pragma: no branch
            self._fail("XPST0051", f"type {name!r} is not in the in-scope schema types")

    def _require_xpath(self, minimum: str, feature: str) -> None:
        version = self.context.xpath_version
        if (minimum == "3.1" and version != "3.1") or (minimum == "3.0" and version == "2.0"):
            self._fail(
                "XPST0003",
                f"{feature} require XPath {minimum}, but this static context uses XPath {version}",
            )


def compile_sequence_type(
    source: str,
    context: StaticContext,
    *,
    location: IssueLocation | None = None,
) -> CompiledSequenceType:
    """Compile one XPath SequenceType in *context* without wrapping it in XPath."""
    from pyxsd.xslt.compiler.context import StaticContext

    if not isinstance(context, StaticContext):
        raise TypeError("context must be a StaticContext")
    if not isinstance(source, str):
        raise TypeError("a sequence type source must be a string")
    return _SequenceTypeCompiler(source, context, location).compile()


# ---------------------------------------------------------------------------
# Attribute and text value templates


@dataclass(frozen=True, slots=True)
class TemplatePart:
    """One literal-text or compiled-expression part of a value template."""

    kind: Literal["text", "expression"]
    text: str
    expression: CompiledExpression | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("text", "expression"):
            raise ValueError(f"unknown template part kind {self.kind!r}")
        if not isinstance(self.text, str):
            raise TypeError("template part text must be a string")
        if self.kind == "text":
            if self.expression is not None:
                raise ValueError("a text part cannot carry a compiled expression")
        else:
            if not isinstance(self.expression, CompiledExpression):
                raise ValueError("an expression part requires a compiled expression")


@dataclass(frozen=True, slots=True)
class CompiledValueTemplate:
    """A compiled AVT or TVT, preserving literal text and expression parts."""

    source: str
    parts: tuple[TemplatePart, ...]
    expand_text: bool = False
    text_value_template: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("template source must be a string")
        if not isinstance(self.parts, tuple) or not all(
            isinstance(part, TemplatePart) for part in self.parts
        ):
            raise TypeError("template parts must be a tuple of TemplatePart values")
        if not isinstance(self.expand_text, bool):
            raise ValueError("expand_text must be a boolean")
        if not isinstance(self.text_value_template, bool):
            raise ValueError("text_value_template must be a boolean")

    @property
    def expressions(self) -> tuple[CompiledExpression, ...]:
        return tuple(
            part.expression
            for part in self.parts
            if part.kind == "expression" and part.expression is not None
        )

    def with_expand_text(self, value: bool) -> CompiledValueTemplate:
        """Return a copy with the recorded flag changed; parts are not re-parsed."""
        if not isinstance(value, bool):
            raise ValueError("expand_text must be a boolean")
        return replace(self, expand_text=value)

    def __str__(self) -> str:
        return self.source


def compile_xpath(
    source: str,
    context: StaticContext,
    *,
    location: IssueLocation | None = None,
) -> CompiledExpression:
    """Compile one XPath expression through the Task 11 versioned engine."""
    from pyxsd.xslt.compiler.context import StaticContext

    if not isinstance(context, StaticContext):
        raise TypeError("context must be a StaticContext")
    if not isinstance(source, str):
        raise TypeError("an XPath expression source must be a string")
    if not source.strip():
        raise _static_error(
            context,
            "an XPath expression must not be empty",
            "XPST0003",
            location=location,
        )
    engine = XPathEngine(context.xpath_version)
    try:
        return engine.compile(source, context.xpath_static_context())
    except Exception as exc:
        raise _static_error(
            context,
            f"invalid XPath expression {source!r}: {exc}",
            _error_code(exc),
            location=location,
            cause=exc,
        ) from exc


def compile_avt(
    source: str,
    context: StaticContext,
    *,
    location: IssueLocation | None = None,
) -> CompiledValueTemplate:
    """Compile an attribute value template, including escaped braces."""
    if not isinstance(source, str):
        raise TypeError("an attribute value template source must be a string")
    parts = _template_parts(
        source,
        context,
        expand=True,
        location=location,
    )
    return CompiledValueTemplate(
        source=source,
        parts=parts,
        expand_text=False,
        text_value_template=False,
    )


def compile_tvt(
    source: str,
    context: StaticContext,
    *,
    expand_text: bool | None = None,
    location: IssueLocation | None = None,
) -> CompiledValueTemplate:
    """Compile text as a TVT when ``expand-text`` is effective.

    Text content of ``xsl:text`` follows the same effective ``expand-text``
    rule as any other text node; the default context value therefore applies.
    """
    if not isinstance(source, str):
        raise TypeError("a text value template source must be a string")
    if expand_text is None:
        expand = context.expand_text
    elif isinstance(expand_text, bool):
        expand = expand_text
    else:
        raise ValueError("expand_text must be a boolean or None")
    parts = _template_parts(
        source,
        context,
        expand=expand,
        location=location,
    )
    return CompiledValueTemplate(
        source=source,
        parts=parts,
        expand_text=expand,
        text_value_template=True,
    )


def _template_parts(
    source: str,
    context: StaticContext,
    *,
    expand: bool,
    location: IssueLocation | None,
) -> tuple[TemplatePart, ...]:
    if not expand:
        if not source:
            return ()
        return (TemplatePart(kind="text", text=source),)
    parts: list[TemplatePart] = []
    pending: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character == "{":
            if index + 1 < len(source) and source[index + 1] == "{":
                pending.append("{")
                index += 2
                continue
            _flush_text(parts, pending)
            expression, index = _scan_expression(source, index + 1, context, location)
            if _has_expression_content(expression):
                compiled = compile_xpath(expression, context, location=location)
                parts.append(TemplatePart(kind="expression", text=expression, expression=compiled))
            continue
        if character == "}":
            if index + 1 < len(source) and source[index + 1] == "}":
                pending.append("}")
                index += 2
                continue
            raise _static_error(
                context,
                f"unescaped right curly bracket in value template {source!r}",
                "XTSE0370",
                location=location,
            )
        pending.append(character)
        index += 1
    _flush_text(parts, pending)
    return tuple(parts)


def _flush_text(parts: list[TemplatePart], pending: list[str]) -> None:
    if not pending:
        return
    text = "".join(pending)
    pending.clear()
    if parts and parts[-1].kind == "text":
        parts[-1] = TemplatePart(kind="text", text=parts[-1].text + text)
    else:
        parts.append(TemplatePart(kind="text", text=text))


def _has_expression_content(source: str) -> bool:
    """Whether a value-template expression contains anything but comments."""
    index = 0
    length = len(source)
    while index < length:
        if source[index] in _XPATH_WHITESPACE:
            index += 1
            continue
        if source.startswith("(:", index):
            depth = 1
            index += 2
            while index < length and depth:
                if source.startswith("(:", index):
                    depth += 1
                    index += 2
                elif source.startswith(":)", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        return True
    return False


def _scan_expression(
    source: str,
    start: int,
    context: StaticContext,
    location: IssueLocation | None,
) -> tuple[str, int]:
    index = start
    depth = 0
    quote: str | None = None
    comment_depth = 0
    while index < len(source):
        character = source[index]
        if comment_depth:
            if source.startswith("(:", index):
                comment_depth += 1
                index += 2
                continue
            if source.startswith(":)", index):
                comment_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if quote is not None:
            if character == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if character in ("'", '"'):
            quote = character
        elif character == "(" and index + 1 < len(source) and source[index + 1] == ":":
            comment_depth = 1
            index += 2
            continue
        elif character == "{":
            depth += 1
        elif character == "}":
            if depth == 0:
                return source[start:index], index + 1
            depth -= 1
        index += 1
    raise _static_error(
        context,
        f"unescaped left curly bracket in value template {source!r}",
        "XTSE0350",
        location=location,
    )
