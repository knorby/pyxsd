"""Overlay signature resolution and corpus-wide identifier resolution.

``inspect.signature`` formats annotations through their defining modules
(``decimal.Decimal``, ``List[str]``, a quoted forward reference), so
text copied into the emitted document verbatim can name identifiers the
document binds nowhere — mypy's ``name is not defined``. The renderer
resolves what the document can bind (Python builtins, rendered class
names, typing generics, ``datetime.``/``decimal.`` qualifiers) and
degrades the whole signature to ``(self) -> Any`` with a comment naming
the first identifier it cannot. The AST guard at the end generalizes
the contract: every identifier reference in an emitted full stub of the
corpus resolves to a rendered class name, an emitted import binding, a
typing import, a Python builtin, or ``Any``.
"""

from __future__ import annotations

import ast
import builtins
import io
import sys
from pathlib import Path

import pytest

from conftest import ALL_FIXTURES, run_parser
from pyxsd.schema import Schema
from pyxsd.stubs import _validated_signature, emit_stub

ITEM = """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:complexType name="Item"><xs:sequence>
    <xs:element name="quantity" type="xs:int"/>
  </xs:sequence></xs:complexType>
  <xs:element name="orders" type="Item"/>
</xs:schema>"""


def overlay_item(methods: str, preamble: str = "") -> str:
    """Returns an overlay module extending the ``Item`` type with *methods*.

    *methods* is class-body text without indentation; *preamble* is the
    module-level text (imports, helper classes) placed before the class.
    """
    body = "\n".join(f"    {line}" if line else "" for line in methods.splitlines())
    header = "from pyxsd.schema_base import SchemaBase"
    if preamble:
        header = f"{preamble.rstrip()}\n\n{header}"
    return f"{header}\n\nclass Item(SchemaBase):\n    name = 'Item'\n\n{body}\n"


def overlay_schema(tmp_path: Path, module_source: str) -> Schema:
    """Compiles ``ITEM`` with *module_source* loaded as the overlay file."""
    overlay = tmp_path / "overlay.py"
    overlay.write_text(module_source)
    return Schema.compile(io.StringIO(ITEM), overlay=overlay)


# ---------------------------------------------------------------------------
# Signature resolution: what the emitted document can bind
# ---------------------------------------------------------------------------


def test_plain_builtin_annotations_pass_through_unchanged(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def double(self, count: int) -> str:\n"
            "    return 'x' * count\n"
            "\n"
            "def count_of(self) -> int:\n"
            "    return 1"
        ),
    )
    text = emit_stub(schema)
    assert "    def double(self, count: int) -> str: ..." in text
    assert "    def count_of(self) -> int: ..." in text
    assert "unresolvable" not in text


def test_decimal_qualifier_is_kept_and_imported(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "@property\ndef price(self) -> decimal.Decimal:\n    return decimal.Decimal('1')",
            preamble="import decimal",
        ),
    )
    text = emit_stub(schema)
    assert "import decimal" in text.splitlines()
    assert "    @property\n    def price(self) -> decimal.Decimal: ..." in text
    assert "unresolvable" not in text


def test_bare_typing_generic_is_kept_and_imported(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def tags(self) -> List[str]:\n"
            "    return []\n"
            "\n"
            "def total(self, count: int) -> Optional[int]:\n"
            "    return None",
            preamble="from typing import List, Optional",
        ),
    )
    text = emit_stub(schema)
    # ``inspect.signature`` renders ``Optional[int]`` in its PEP 604
    # spelling ``int | None`` on Python 3.14+, so the emitted signature
    # and the scan-driven ``from typing import`` line follow the running
    # interpreter rather than one fixed spelling.
    if sys.version_info >= (3, 14):
        expected_return = "int | None"
        expected_imports = "from typing import Any, List"
    else:
        expected_return = "Optional[int]"
        expected_imports = "from typing import Any, List, Optional"
    assert expected_imports in text
    assert "    def tags(self) -> List[str]: ..." in text
    assert f"    def total(self, count: int) -> {expected_return}: ..." in text


def test_typing_qualified_spelling_is_rewritten_to_the_typing_import(tmp_path):
    # ``inspect.signature`` normally strips a ``typing.`` qualifier, so an
    # annotation object whose repr keeps it simulates the spelling the
    # contract names: the emitted document binds ``List``, not
    # ``typing.List``, so the qualifier is rewritten.
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def spelled(self) -> Spelled():\n    return Spelled()",
            preamble=("class Spelled:\n    def __repr__(self):\n        return 'typing.List[str]'"),
        ),
    )
    text = emit_stub(schema)
    assert "from typing import Any, List" in text
    assert "    def spelled(self) -> List[str]: ..." in text
    assert "typing.List" not in text


def test_literal_string_arguments_are_values_not_forward_refs(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def mode(self) -> Literal['fast', 'slow']:\n    return 'fast'",
            preamble="from typing import Literal",
        ),
    )
    text = emit_stub(schema)
    assert "from typing import Any, Literal" in text
    assert "    def mode(self) -> Literal['fast', 'slow']: ..." in text


def test_unknown_module_qualifier_degrades_the_signature(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def series(self) -> Series:\n    return Series()",
            preamble="class Series: ...\nSeries.__module__ = 'pandas'",
        ),
    )
    text = emit_stub(schema)
    assert (
        "    def series(self) -> Any: ...  # unresolvable identifier 'pandas.Series' "
        "in signature; annotated Any" in text
    )
    assert "def series(self) -> Series" not in text


def test_unknown_bare_name_degrades_the_signature(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def widget(self) -> Bare():\n    return Bare()",
            preamble=("class Bare:\n    def __repr__(self):\n        return 'Widget'"),
        ),
    )
    text = emit_stub(schema)
    assert (
        "    def widget(self) -> Any: ...  # unresolvable identifier 'Widget' "
        "in signature; annotated Any" in text
    )


def test_quoted_forward_reference_degrades_the_signature(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item("def forward(self) -> 'Item':\n    return self"),
    )
    text = emit_stub(schema)
    assert (
        "    def forward(self) -> Any: ...  # unresolvable identifier 'Item' "
        "in signature; annotated Any" in text
    )


def test_rendered_class_name_resolves_bare(tmp_path):
    # A bare name that is a rendered class of the document resolves: the
    # class block is a module-level binding of the emitted document.
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def named(self) -> Named():\n    return Named()",
            preamble=("class Named:\n    def __repr__(self):\n        return 'Item'"),
        ),
    )
    text = emit_stub(schema)
    assert "    def named(self) -> Item: ..." in text


def test_property_with_unresolvable_annotation_stays_a_property(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item("@property\ndef label(self) -> 'Item':\n    return self"),
    )
    text = emit_stub(schema)
    assert "    @property\n    def label(self) -> Any: ..." in text
    assert "unresolvable identifier 'Item'" in text


def test_validated_signature_unit_shapes():
    names = frozenset({"Item"})
    assert _validated_signature("(self) -> typing.List[str]", names) == (
        "(self) -> List[str]",
        None,
    )
    assert _validated_signature("(self) -> Literal['a', 'b']", names) == (
        "(self) -> Literal['a', 'b']",
        None,
    )
    assert _validated_signature("(self) -> Optional['Item']", names)[1] is not None
    assert _validated_signature("(self) -> datetime.no_such_thing", names)[1] is not None
    assert _validated_signature("(self, x: int) -> decimal.Decimal", names) == (
        "(self, x: int) -> decimal.Decimal",
        None,
    )


def test_non_expression_default_degrades_the_signature(tmp_path):
    # A default value's repr is not always a Python expression
    # (``<object object at 0x...>``), which would emit a syntax error;
    # the whole signature degrades instead.
    import ast

    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "def describe(self, x: int = sentinel) -> str:\n    return 'x'",
            preamble="sentinel = object()",
        ),
    )
    text = emit_stub(schema)
    ast.parse(text)
    assert (
        "    def describe(self) -> Any: ...  # signature could not be parsed; annotated Any" in text
    )


def test_root_named_datetime_defers_to_the_qualifier_import(tmp_path):
    # Without the qualifier the alias binds ``datetime`` itself; with an
    # overlay signature using ``datetime.`` the emitted ``import
    # datetime`` makes the alias a duplicate definition, so it becomes a
    # comment.
    xsd = ITEM.replace(
        '<xs:element name="orders" type="Item"/>',
        '<xs:element name="datetime" type="Item"/>',
    )
    plain_text = emit_stub(Schema.compile(io.StringIO(xsd)))
    assert "import datetime" not in plain_text.splitlines()
    assert "datetime: type[Item]" in plain_text

    overlay = tmp_path / "overlay.py"
    overlay.write_text(
        overlay_item(
            "def stamp(self) -> datetime.datetime:\n    return datetime.datetime.now()",
            preamble="import datetime",
        )
    )
    text = emit_stub(Schema.compile(io.StringIO(xsd), overlay=overlay))
    assert "import datetime" in text.splitlines()
    assert "datetime: type[Item]" not in text
    assert "# root element 'datetime': name already used by an import" in text
    assert "    def stamp(self) -> datetime.datetime: ..." in text


# ---------------------------------------------------------------------------
# The generalized guard: every identifier reference resolves
# ---------------------------------------------------------------------------


def _unresolvable_references(text: str) -> set[str]:
    """Returns the identifier references *text* uses without a binding.

    The module is parsed and every ``Name`` load is collected; the
    module-level bindings (imports, class names, assigned names) and the
    Python builtins are subtracted. The remainder is what mypy would
    report as ``name is not defined``.
    """
    module = ast.parse(text)
    bound: set[str] = set()
    referenced: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Name):
            (referenced if isinstance(node.ctx, ast.Load) else bound).add(node.id)
        elif isinstance(node, ast.Import):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
    return {name for name in referenced if name not in bound and not hasattr(builtins, name)}


@pytest.mark.parametrize("fixture", ALL_FIXTURES)
def test_corpus_full_stub_identifiers_all_resolve(fixture):
    document = run_parser(fixture)
    assert _unresolvable_references(emit_stub(document.schema)) == set()


def test_overlay_and_collision_stubs_identifiers_all_resolve(tmp_path):
    schema = overlay_schema(
        tmp_path,
        overlay_item(
            "@property\n"
            "def price(self) -> decimal.Decimal:\n"
            "    return decimal.Decimal('1')\n"
            "\n"
            "def tags(self) -> List[str]:\n"
            "    return []\n"
            "\n"
            "def total(self, count: int) -> Optional[int]:\n"
            "    return None\n"
            "\n"
            "def series(self) -> Series:\n"
            "    return Series()\n"
            "\n"
            "def widget(self) -> Bare():\n"
            "    return Bare()\n"
            "\n"
            "def spelled(self) -> Spelled():\n"
            "    return Spelled()\n"
            "\n"
            "def forward(self) -> 'Item':\n"
            "    return self\n"
            "\n"
            "def plain(self) -> str:\n"
            "    return 'x'",
            preamble=(
                "import decimal\n"
                "from typing import List, Optional\n"
                "\n"
                "class Series: ...\n"
                "Series.__module__ = 'pandas'\n"
                "\n"
                "class Bare:\n"
                "    def __repr__(self):\n"
                "        return 'Widget'\n"
                "\n"
                "class Spelled:\n"
                "    def __repr__(self):\n"
                "        return 'typing.List[str]'"
            ),
        ),
    )
    assert _unresolvable_references(emit_stub(schema)) == set()

    collisions = [
        # A root aliasing to a rendered class name.
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:complexType name="Item"><xs:sequence>
            <xs:element name="n" type="xs:int"/></xs:sequence></xs:complexType>
          <xs:element name="Item" type="Item"/>
        </xs:schema>""",
        # A root aliasing to a datatype import.
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:element name="Int" type="xs:int"/>
        </xs:schema>""",
        # Roots aliasing to the unconditional typing/schema-base imports.
        """<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
          <xs:complexType name="a"><xs:sequence>
            <xs:element name="x" type="xs:int"/></xs:sequence></xs:complexType>
          <xs:element name="Any" type="a"/>
          <xs:element name="SchemaBase" type="a"/>
          <xs:element name="List" type="a"/>
          <xs:element name="plain" type="a"/>
        </xs:schema>""",
    ]
    for xsd in collisions:
        text = emit_stub(Schema.compile(io.StringIO(xsd)))
        assert _unresolvable_references(text) == set()
