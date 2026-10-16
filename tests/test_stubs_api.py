"""The public stub API: ``Schema.stub()`` and the ``pyxsd.stubs`` module.

``Schema.stub()`` is the ergonomic entry point to the stub renderer: a
thin delegate to :func:`pyxsd.stubs.emit_stub` on the class a caller
already holds after a compile. The module must also import cleanly on
its own: the element-representative tag modules form an import cycle
that only resolves in one order, and a caller who imports
``pyxsd.stubs`` before ever compiling a schema must not trip it.
"""

import io
import subprocess
import sys

import pytest

import pyxsd

TRIVIAL = (
    "<xs:schema xmlns:xs='http://www.w3.org/2001/XMLSchema'>"
    "<xs:element name='a' type='xs:string'/></xs:schema>"
)


def test_schema_stub_method():
    schema = pyxsd.compile(io.StringIO(TRIVIAL))
    text = schema.stub()
    assert "SchemaBase" in text
    compact = schema.stub(compact=True)
    assert len(compact) < len(text)


def test_schema_stub_delegates_to_emit_stub():
    from pyxsd.stubs import emit_stub

    schema = pyxsd.compile(io.StringIO(TRIVIAL))
    assert schema.stub() == emit_stub(schema)
    assert schema.stub(compact=True) == emit_stub(schema, compact=True)


def test_schema_stub_compact_is_keyword_only():
    schema = pyxsd.compile(io.StringIO(TRIVIAL))
    with pytest.raises(TypeError):
        schema.stub(True)  # type: ignore[call-arg]


def test_emit_stub_importable():
    from pyxsd.stubs import emit_stub

    assert callable(emit_stub)


def test_stubs_module_imports_cold():
    """``import pyxsd.stubs`` must work before any schema was compiled.

    The element-representative tag modules and ``schema_base`` import
    each other; the cycle only resolves when the package is entered
    through ``element_representative``, the way ``pyxsd.schema``'s own
    imports do it. A run that imports ``pyxsd.stubs`` first must not
    enter the cycle from a tag module (a subprocess, because
    ``tests/conftest.py`` has already loaded the stack in-process).
    """
    script = (
        "import io\n"
        "from pyxsd.stubs import emit_stub\n"
        "import pyxsd\n"
        f"schema = pyxsd.compile(io.StringIO({TRIVIAL!r}))\n"
        "assert 'SchemaBase' in emit_stub(schema)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
