"""Module-local XSLT static contexts.

A ``StaticContext`` is an immutable view of one element's static environment.
It carries the element's namespaces and defaults while preserving the enclosing
module's symbols and options, so an imported module never inherits the
principal module's namespace bindings or defaults.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from pyxsd.xdm.schema import SchemaPool
from pyxsd.xslt.compiler.context import (
    ProfileOptions,
    StaticContext,
    StaticFunction,
    StaticVariable,
)
from pyxsd.xslt.compiler.expressions import compile_avt
from pyxsd.xslt.compiler.modules import ModuleLoader
from pyxsd.xslt.compiler.static import (
    EffectiveAttribute,
    EffectiveElement,
    EffectiveModule,
    StaticPreprocessor,
)
from pyxsd.xslt.errors import XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
DOC_NS = "urn:example:doc"
OTHER_NS = "urn:example:other"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _module(tmp_path: Path, xml: str, *, name: str = "module.xsl") -> EffectiveModule:
    path = _write(tmp_path, name, xml)
    return StaticPreprocessor().apply(ModuleLoader().load(path))


def _stylesheet(body: str = "", *, attributes: str = "") -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{DOC_NS}"{attributes}>{body}</xsl:stylesheet>'
    )


def _element(root: EffectiveElement, local_name: str) -> EffectiveElement:
    for descendant in root.descendants:
        if isinstance(descendant, EffectiveElement) and descendant.local_name == local_name:
            return descendant
    raise AssertionError(f"no element {local_name!r}")


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


# ---------------------------------------------------------------------------
# Element defaults


def test_context_reads_element_defaults(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/"/>',
            attributes=(
                ' xpath-default-namespace="urn:example:elements"'
                ' default-collation="urn:example:collation"'
                ' expand-text="yes" default-validation="preserve"'
                ' input-type-annotations="preserve"'
            ),
        ),
    )

    context = StaticContext.for_element(module.root)

    assert context.version == "3.0"
    assert context.xpath_version == "3.1"
    assert context.namespaces["doc"] == DOC_NS
    assert context.namespaces["xs"] == XSD_NS
    assert context.namespaces["fn"] == "http://www.w3.org/2005/xpath-functions"
    assert context.namespaces["xml"] == "http://www.w3.org/XML/1998/namespace"
    assert context.xpath_default_namespace == "urn:example:elements"
    assert context.default_namespace == "urn:example:elements"
    assert context.base_uri == Path(module.source.source_id).resolve().as_uri()
    assert context.default_collation == "urn:example:collation"
    assert context.expand_text is True
    assert context.default_validation == "preserve"
    assert context.input_type_annotations == "preserve"
    assert context.uri == context.base_uri
    assert context.location is not None
    assert context.module == module.identity
    assert context.compatibility_mode is False
    assert context.profile.processor_profile == "xslt-3.0"
    assert context.profile.schema_aware is True
    assert context.profile.static_options == {}

    xpath = context.xpath_static_context()
    assert xpath.namespaces == context.namespaces
    assert xpath.default_namespace == "urn:example:elements"
    assert xpath.base_uri == context.base_uri
    assert xpath.default_collation == "urn:example:collation"
    assert xpath.variable_types == {}
    assert xpath.functions == {}


def test_context_version_scoping_and_validation(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/">'
            '<outer><inner xsl:version="2.0"><deep/></inner></outer>'
            "</xsl:template>"
        ),
    )
    outer = _element(module.root, "outer")
    inner = _element(module.root, "inner")
    deep = _element(module.root, "deep")

    assert StaticContext.for_element(module.root).version == "3.0"
    assert StaticContext.for_element(outer).version == "3.0"
    assert StaticContext.for_element(inner).version == "2.0"
    assert StaticContext.for_element(inner).xpath_version == "2.0"
    assert StaticContext.for_element(inner).compatibility_mode is False
    assert StaticContext.for_element(deep).version == "2.0"
    assert StaticContext.for_element(deep).xpath_version == "2.0"

    backwards = _module(
        tmp_path,
        f'<xsl:stylesheet version="1.0" xmlns:xsl="{XSLT_NS}"/>',
        name="backwards.xsl",
    )
    context = StaticContext.for_element(backwards.root)
    assert context.version == "1.0"
    assert context.xpath_version == "2.0"
    assert context.compatibility_mode is True

    invalid = _module(
        tmp_path,
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
        '<xsl:template match="/"><bad xsl:version="three"/></xsl:template>'
        "</xsl:stylesheet>",
        name="invalid-version.xsl",
    )
    with pytest.raises(XsltStaticError) as caught:
        StaticContext.for_element(_element(invalid.root, "bad"))
    assert _code(caught.value) == "XTSE0110"
    assert caught.value.location is not None


def test_context_expand_text_scoping_and_invalid_values(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/"><a xsl:expand-text="no">'
            '<b xsl:expand-text="1"/></a></xsl:template>',
            attributes=' expand-text="yes"',
        ),
    )
    assert StaticContext.for_element(module.root).expand_text is True
    assert StaticContext.for_element(_element(module.root, "a")).expand_text is False
    assert StaticContext.for_element(_element(module.root, "b")).expand_text is True

    invalid = _module(
        tmp_path,
        _stylesheet('<xsl:template match="/"><a xsl:expand-text="maybe"/></xsl:template>'),
        name="invalid-expand.xsl",
    )
    with pytest.raises(XsltStaticError) as caught:
        StaticContext.for_element(_element(invalid.root, "a"))
    assert _code(caught.value) == "XTSE0020"

    legacy = _module(
        tmp_path,
        f'<xsl:stylesheet version="2.0" xmlns:xsl="{XSLT_NS}" expand-text="yes">'
        '<xsl:template match="/"/></xsl:stylesheet>',
        name="legacy.xsl",
    )
    assert StaticContext.for_element(legacy.root).expand_text is False


def test_context_default_collation_and_validation_scoping(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/">'
            '<a xsl:default-collation="urn:a urn:b" xsl:default-validation="strip" '
            'xsl:input-type-annotations="strip"/>'
            "</xsl:template>",
            attributes=' default-collation="urn:root"',
        ),
    )
    root = StaticContext.for_element(module.root)
    child = StaticContext.for_element(_element(module.root, "a"))
    assert root.default_collation == "urn:root"
    assert child.default_collation == "urn:a"
    assert child.default_validation == "strip"
    assert child.input_type_annotations == "strip"

    for attribute, value, code in (
        ("xsl:default-validation", "strict", "XTSE0020"),
        ("xsl:default-validation", "lax", "XTSE0020"),
        ("xsl:default-validation", "sometimes", "XTSE0020"),
        ("xsl:input-type-annotations", "maybe", "XTSE0020"),
    ):
        xml = _stylesheet(f'<xsl:template match="/"><a {attribute}="{value}"/></xsl:template>')
        invalid = _module(tmp_path, xml, name=f"invalid-{attribute}.xsl")
        with pytest.raises(XsltStaticError) as caught:
            StaticContext.for_element(_element(invalid.root, "a"))
        assert _code(caught.value) == code


# ---------------------------------------------------------------------------
# Module-local symbols and contexts


def test_context_for_module_collects_variables_and_functions(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:variable name="v" select="1" as="xs:integer"/>'
            '<xsl:param name="p" select="\'x\'" as="xs:string"/>'
            '<xsl:function name="doc:f" as="xs:integer" visibility="public">'
            '<xsl:param name="a" as="xs:integer"/>'
            '<xsl:param name="b" as="xs:string?"/>'
            '<xsl:sequence select="$a"/>'
            "</xsl:function>"
            '<xsl:function name="doc:f" visibility="private">'
            '<xsl:sequence select="2"/>'
            "</xsl:function>"
        ),
    )

    context = StaticContext.for_module(module)

    variable = context.variable("v")
    assert variable is not None
    assert variable.kind == "variable"
    assert variable.static is False
    assert str(variable.sequence_type) == "xs:integer"
    param = context.variable("p")
    assert param is not None
    assert param.kind == "param"
    assert str(param.sequence_type) == "xs:string"

    public = context.function(f"{{{DOC_NS}}}f", 2)
    assert public is not None
    assert public.visibility == "public"
    assert public.arity == 2
    assert tuple(str(parameter) for parameter in public.parameters) == (
        "xs:integer",
        "xs:string?",
    )
    assert public.return_type is not None
    assert str(public.return_type) == "xs:integer"
    private = context.function(f"{{{DOC_NS}}}f", 0)
    assert private is not None
    assert private.visibility == "private"
    assert private.parameters == ()
    assert private.return_type is not None
    assert str(private.return_type) == "item()*"
    assert context.function(f"{{{DOC_NS}}}f", 1) is None
    assert context.function_arities(f"{{{DOC_NS}}}f") == (0, 2)

    xpath = context.xpath_static_context()
    assert xpath.functions[f"{{{DOC_NS}}}f"] == (0, 2)
    assert xpath.variable_types == {"v": "xs:integer", "p": "xs:string"}


def test_imported_module_keeps_its_own_context(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "imported.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{OTHER_NS}" xpath-default-namespace="urn:imported" '
        f'default-collation="urn:imported:collation" expand-text="yes">'
        '<xsl:variable name="imported_public" select="1"/>'
        '<xsl:variable name="imported_private" select="2" visibility="private"/>'
        '<xsl:function name="doc:imported" visibility="public">'
        '<xsl:sequence select="1"/>'
        "</xsl:function>"
        '<xsl:function name="doc:hidden" visibility="private">'
        '<xsl:sequence select="2"/>'
        "</xsl:function>"
        "</xsl:stylesheet>",
    )
    _write(
        tmp_path,
        "included.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{OTHER_NS}">'
        '<xsl:variable name="included_private" select="1" visibility="private"/>'
        '<xsl:function name="doc:included" visibility="private">'
        '<xsl:sequence select="3"/>'
        "</xsl:function>"
        "</xsl:stylesheet>",
    )
    principal = _module(
        tmp_path,
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{DOC_NS}" xmlns:other="{OTHER_NS}" '
        f'xpath-default-namespace="urn:principal">'
        '<xsl:import href="imported.xsl"/>'
        '<xsl:include href="included.xsl"/>'
        '<xsl:variable name="principal" select="1"/>'
        "</xsl:stylesheet>",
    )

    context = StaticContext.for_module(principal)
    imported = StaticContext.for_module(principal.imports[0])

    assert context.namespaces["doc"] == DOC_NS
    assert imported.namespaces["doc"] == OTHER_NS
    assert context.xpath_default_namespace == "urn:principal"
    assert imported.xpath_default_namespace == "urn:imported"
    assert imported.expand_text is True
    assert context.expand_text is False
    assert imported.default_collation == "urn:imported:collation"

    assert context.variable("principal") is not None
    assert context.variable("included_private") is not None
    assert context.variable("imported_public") is not None
    assert context.variable("imported_private") is not None
    assert context.function(f"{{{OTHER_NS}}}included", 0) is not None
    assert context.function(f"{{{OTHER_NS}}}imported", 0) is not None
    assert context.function(f"{{{OTHER_NS}}}hidden", 0) is not None
    private_call = compile_avt("{other:hidden()}", context)
    assert private_call.parts[0].kind == "expression"

    assert imported.variable("imported_private") is not None
    assert imported.variable("principal") is None
    assert imported.function(f"{{{OTHER_NS}}}hidden", 0) is not None
    assert imported.function(f"{{{OTHER_NS}}}included", 0) is None


def test_package_components_default_to_private(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "plain.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{OTHER_NS}">'
        '<xsl:function name="doc:plain"><xsl:sequence select="3"/></xsl:function>'
        '<xsl:variable name="plain_variable" select="3"/>'
        "</xsl:stylesheet>",
    )
    _write(
        tmp_path,
        "library.xsl",
        f'<xsl:package version="3.0" name="urn:library" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:doc="{OTHER_NS}">'
        '<xsl:include href="plain.xsl"/>'
        '<xsl:function name="doc:internal">'
        '<xsl:sequence select="1"/>'
        "</xsl:function>"
        '<xsl:function name="doc:public" visibility="public">'
        '<xsl:sequence select="2"/>'
        "</xsl:function>"
        '<xsl:variable name="private_variable" select="1"/>'
        '<xsl:param name="public_param" select="1"/>'
        '<xsl:param name="static_param" select="1" static="yes"/>'
        "</xsl:package>",
    )
    principal = _module(
        tmp_path,
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
        '<xsl:import href="library.xsl"/>'
        "</xsl:stylesheet>",
    )

    context = StaticContext.for_module(principal)
    library = StaticContext.for_module(principal.imports[0])

    internal = context.function(f"{{{OTHER_NS}}}internal", 0)
    assert internal is not None
    assert internal.visibility == "private"
    assert context.function(f"{{{OTHER_NS}}}public", 0) is not None
    plain = context.function(f"{{{OTHER_NS}}}plain", 0)
    assert plain is not None
    assert plain.visibility == "private"
    assert context.variable("private_variable") is not None
    assert context.variable("private_variable").visibility == "private"
    assert context.variable("plain_variable") is not None
    assert context.variable("plain_variable").visibility == "private"
    assert context.variable("public_param") is not None
    assert context.variable("public_param").visibility == "public"
    assert context.variable("static_param") is not None
    assert context.variable("static_param").visibility == "private"
    assert library.function(f"{{{OTHER_NS}}}internal", 0) is not None


def test_context_for_element_inherits_parent_symbols(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/"/><xsl:variable name="v" select="1"/>',
            attributes=' default-collation="urn:root:collation"',
        ),
    )
    root_context = StaticContext.for_module(module)
    template = _element(module.root, "template")
    child_context = StaticContext.for_element(template, parent=root_context)
    assert child_context.variable("v") is not None
    assert child_context.default_collation == "urn:root:collation"

    extra_variable = StaticVariable(name="extra")
    extra_function = StaticFunction(name=f"{{{DOC_NS}}}extra", arity=1)
    overridden = StaticContext.for_element(
        template,
        parent=root_context,
        variables={extra_variable.name: extra_variable},
        functions={(extra_function.name, extra_function.arity): extra_function},
    )
    assert overridden.variable("v") is None
    assert overridden.variable("extra") is not None
    assert overridden.function(f"{{{DOC_NS}}}extra", 1) is not None
    assert root_context.variable("extra") is None
    assert child_context.for_child(template).variable("v") is not None

    with pytest.raises(TypeError):
        StaticContext.for_element(template, module=module, parent=root_context)


def test_context_with_methods_are_immutable(tmp_path: Path) -> None:
    module = _module(tmp_path, _stylesheet())
    context = StaticContext.for_module(module)
    location = context.location

    changed = (
        context.with_expand_text(True)
        .with_version("2.0")
        .with_namespace("other", OTHER_NS)
        .with_namespaces({"only": OTHER_NS, "other": OTHER_NS})
        .with_xpath_default_namespace("urn:changed")
        .with_base_uri("urn:base")
        .with_default_collation("urn:collation")
        .with_document_types({"urn:doc": "document-node()"})
        .with_collection_types({"urn:collection": "item()*"})
        .with_default_collection_type("item()*")
        .with_uri("urn:stylesheet")
        .with_location(None)
        .with_static_option("mode", "strict")
    )
    assert context.expand_text is False
    assert context.version == "3.0"
    assert "other" not in context.namespaces
    assert changed.expand_text is True
    assert changed.version == "2.0"
    assert changed.namespaces["other"] == OTHER_NS
    assert changed.namespaces["only"] == OTHER_NS
    assert changed.namespaces["xs"] == XSD_NS
    assert changed.xpath_default_namespace == "urn:changed"
    assert changed.base_uri == "urn:base"
    assert changed.default_collation == "urn:collation"
    assert changed.document_type("urn:doc") == "document-node()"
    assert changed.collection_type("urn:collection") == "item()*"
    assert changed.default_collection_type == "item()*"
    assert changed.document_type("missing") is None
    assert changed.collection_type("missing") is None
    assert changed.uri == "urn:stylesheet"
    assert changed.location is None
    assert location is not None
    assert changed.static_option("mode") == "strict"
    assert changed.static_option("missing", "fallback") == "fallback"

    with pytest.raises(FrozenInstanceError):
        context.version = "4.0"  # type: ignore[misc]

    profile = ProfileOptions(processor_profile="xslt-2.0", schema_aware=False)
    profiled = context.with_profile(profile)
    assert profiled.profile is profile
    assert profiled.xpath_version == "3.1"

    schema = SchemaPool()
    with_schema = context.with_schema(schema)
    assert with_schema.schema is schema
    assert with_schema.xpath_static_context().schema is schema

    variable = StaticVariable(name="v")
    function = StaticFunction(name=f"{{{DOC_NS}}}f", arity=1)
    symbols = context.with_variable(variable).with_function(function)
    assert symbols.variable("v") is variable
    assert symbols.function(f"{{{DOC_NS}}}f", 1) is function
    replaced = symbols.with_variables({}).with_functions({})
    assert replaced.variable("v") is None
    assert replaced.function(f"{{{DOC_NS}}}f", 1) is None


def test_context_rejects_invalid_construction() -> None:
    with pytest.raises(XsltStaticError) as caught:
        StaticContext(version="three")
    assert _code(caught.value) == "XTSE0110"

    with pytest.raises(TypeError):
        StaticContext(expand_text=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(namespaces={"p": 1})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        StaticContext(variables={"other": StaticVariable(name="v")})
    with pytest.raises(ValueError):
        StaticContext(functions={("name", 1): StaticFunction(name="other", arity=1)})
    with pytest.raises(TypeError):
        StaticContext(profile="profile")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(schema="schema")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ProfileOptions(processor_profile="unknown")
    with pytest.raises(ValueError):
        StaticVariable(name="")
    with pytest.raises(ValueError):
        StaticFunction(name="", arity=1)
    with pytest.raises(ValueError):
        StaticFunction(name="name", arity=-1)


def test_context_symbol_helpers_cover_variadic_arities() -> None:
    context = StaticContext(namespaces={"t": "urn:test"})
    function = StaticFunction(name="{urn:test}f", arity=(1, 3))
    context = context.with_function(function)
    assert context.function("{urn:test}f", 1) is function
    assert context.function("{urn:test}f", 3) is function
    assert context.function("{urn:test}f", 4) is None
    assert context.function_arities("{urn:test}f") == (1, 3)
    assert context.function_arities("missing") == ()
    assert context.xpath_static_context().functions["{urn:test}f"] == (1, 3)

    overlapping = context.with_function(StaticFunction(name="{urn:test}g", arity=0)).with_function(
        StaticFunction(name="{urn:test}g", arity=2)
    )
    assert overlapping.xpath_static_context().functions["{urn:test}g"] == (0, 2)

    variable = StaticVariable(name="v", sequence_type=None)
    context = context.with_variable(variable)
    assert context.xpath_static_context().variable_types == {}
    assert variable.sequence_type is None


def test_context_profile_compatibility_mode_can_be_forced() -> None:
    profile = ProfileOptions(compatibility_mode=True)
    context = StaticContext(version="3.0", profile=profile)
    assert context.compatibility_mode is True
    assert context.profile.static_options == {}
    options: Any = {"a": 1}
    frozen = ProfileOptions(static_options=options)
    options["a"] = 2
    assert frozen.static_options == {"a": 1}


# ---------------------------------------------------------------------------
# Validation, source-node contexts, and precedence


def test_context_validation_paths() -> None:
    with pytest.raises(TypeError):
        StaticContext(namespaces=[])  # type: ignore[arg-type]
    for version in ("", "   ", "NaN", "-1"):
        with pytest.raises(XsltStaticError) as caught:
            StaticContext(version=version)
        assert _code(caught.value) == "XTSE0110"
    with pytest.raises(TypeError):
        StaticContext(schema="schema")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(profile="profile")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(expand_text=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(variables={"v": "variable"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        StaticContext(functions={("f", 1): "function"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        StaticContext(functions={"f": StaticFunction(name="f", arity=1)})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        StaticContext(functions={("f", 1): StaticFunction(name="g", arity=1)})
    with pytest.raises(TypeError):
        StaticContext(default_collation=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(location="location")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(document_types={1: "document-node()"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        StaticContext(collection_types={"urn:c": 1})  # type: ignore[dict-item]
    with pytest.raises(XsltStaticError) as caught:
        StaticContext(default_validation="sometimes")
    assert _code(caught.value) == "XTSE0020"
    for invalid_validation in ("strict", "lax"):
        with pytest.raises(XsltStaticError) as caught:
            StaticContext(default_validation=invalid_validation)
        assert _code(caught.value) == "XTSE0020"
    with pytest.raises(XsltStaticError) as caught:
        StaticContext(input_type_annotations="sometimes")
    assert _code(caught.value) == "XTSE0020"
    with pytest.raises(TypeError):
        StaticContext().with_expand_text(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext().with_namespace(1, "urn:p")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext().with_namespace("p", 1)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        ProfileOptions(schema_aware=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProfileOptions(streaming=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProfileOptions(compatibility_mode="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProfileOptions(static_options={1: 2})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        ProfileOptions().with_option("", 1)

    with pytest.raises(ValueError):
        StaticVariable(name="v", kind="other")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticVariable(name="v", static=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        StaticVariable(name="v", visibility="other")
    with pytest.raises(TypeError):
        StaticVariable(name="v", sequence_type="xs:string")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticVariable(name="v", declared_type=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticVariable(name="v", precedence=("x",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticVariable(name="v", module=1)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        StaticFunction(name="f", arity=True)  # type: ignore[arg-type]
    for arity in (-1, "one", (1,), (2, 1), (True, 2)):
        with pytest.raises(ValueError):
            StaticFunction(name="f", arity=arity)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        StaticFunction(name="f", arity=1, visibility="other")
    with pytest.raises(TypeError):
        StaticFunction(name="f", arity=1, parameters=("xs:string",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticFunction(name="f", arity=1, return_type="xs:string")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticFunction(name="f", arity=1, precedence=("x",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticFunction(name="f", arity=1, module=1)  # type: ignore[arg-type]
    assert StaticFunction(name="f", arity=(1, 3)).matches(2) is True
    assert StaticFunction(name="f", arity=(1, 3)).matches(4) is False

    with pytest.raises(TypeError):
        StaticContext.for_module("module")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext.for_element("element")  # type: ignore[arg-type]


def test_context_source_nodes_synthetic_fallbacks(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet('<xsl:template match="/"><a expand-text="no"/></xsl:template>'),
    )
    raw_root = module.source.root
    raw_template = raw_root.children[0]

    assert StaticContext.for_element(raw_root).version == "3.0"
    raw_context = StaticContext.for_element(raw_template)
    assert raw_context.namespaces["xsl"] == XSLT_NS
    assert raw_context.base_uri == Path(module.source.source_id).resolve().as_uri()
    assert raw_context.expand_text is False
    with pytest.raises(TypeError):
        StaticContext.for_element(module.source.document)
    with pytest.raises(TypeError):
        StaticContext.for_element(object())  # type: ignore[arg-type]

    synthetic = EffectiveElement(
        name=f"{{{XSLT_NS}}}stylesheet",
        attributes=(EffectiveAttribute("version", "3.0"),),
        children=(),
        raw=None,
        base_uri=None,
        in_scope_namespaces={},
    )
    base = StaticContext.for_element(synthetic)
    assert base.version == "3.0"
    assert base.uri is None
    assert base.module is None
    assert base.location is None
    assert base.namespaces["xs"] == XSD_NS

    fallback = StaticContext(
        namespaces={"p": "urn:p"},
        xpath_default_namespace="urn:default",
        base_uri="urn:base",
        default_collation="urn:collation",
        default_validation="strip",
        input_type_annotations="strip",
        uri="urn:fallback",
        module="urn:module",
    )
    inherited = StaticContext.for_element(synthetic, parent=fallback)
    assert inherited.namespaces["p"] == "urn:p"
    assert inherited.xpath_default_namespace == "urn:default"
    assert inherited.base_uri == "urn:base"
    assert inherited.default_collation == "urn:collation"
    assert inherited.default_validation == "strip"
    assert inherited.input_type_annotations == "strip"
    assert inherited.uri == "urn:fallback"
    assert inherited.module == "urn:module"

    schema = SchemaPool()
    profile = ProfileOptions(processor_profile="xslt-2.0")
    configured = StaticContext.for_element(
        synthetic,
        parent=fallback,
        schema=schema,
        profile=profile,
        location=module.root.source_location(),
    )
    assert configured.schema is schema
    assert configured.profile is profile
    assert configured.location == module.root.source_location()
    assert StaticContext.for_module(module, schema=schema, profile=profile).schema is schema


def test_context_module_symbol_precedence_and_declaration_edges(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "first.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}">'
        '<xsl:variable name="shared" select="1" as="xs:string"/>'
        '<xsl:variable name="duplicate" select="1" as="xs:string"/>'
        '<xsl:function name="doc:shared" as="xs:string"><xsl:sequence select="1"/></xsl:function>'
        "</xsl:stylesheet>",
    )
    _write(
        tmp_path,
        "second.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}">'
        '<xsl:variable name="duplicate" select="2" as="xs:integer"/>'
        '<xsl:variable name="quote" select="2" static="yes"/>'
        '<xsl:function name="doc:shared" as="xs:integer"><xsl:sequence select="2"/></xsl:function>'
        "</xsl:stylesheet>",
    )
    _write(
        tmp_path,
        "third.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}">'
        '<xsl:variable name="imported" select="3" as="xs:string"/>'
        '<xsl:variable name="shared" select="3" as="xs:string"/>'
        '<xsl:function name="doc:hidden" visibility="private">'
        '<xsl:sequence select="3"/>'
        "</xsl:function>"
        "</xsl:stylesheet>",
    )
    _write(
        tmp_path,
        "fourth.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}">'
        '<xsl:variable name="imported" select="4" as="xs:integer"/>'
        "</xsl:stylesheet>",
    )
    module = _module(
        tmp_path,
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}">'
        '<xsl:import href="third.xsl"/>'
        '<xsl:import href="fourth.xsl"/>'
        '<xsl:include href="first.xsl"/>'
        '<xsl:include href="second.xsl"/>'
        '<xsl:variable name="shared" select="0" as="xs:boolean"/>'
        '<xsl:variable name="quote" select="1"/>'
        "</xsl:stylesheet>",
    )

    context = StaticContext.for_module(module)
    assert context.variable("duplicate") is not None
    assert str(context.variable("duplicate").sequence_type) == "xs:string"
    assert context.function(f"{{{DOC_NS}}}shared", 0) is not None
    assert str(context.function(f"{{{DOC_NS}}}shared", 0).return_type) == "xs:string"
    assert context.variable("imported") is not None
    assert str(context.variable("imported").sequence_type) == "xs:integer"
    assert str(context.variable("shared").sequence_type) == "xs:boolean"
    hidden = context.function(f"{{{DOC_NS}}}hidden", 0)
    assert hidden is not None
    assert hidden.visibility == "private"


def test_context_declarations_with_names_edge_cases(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:variable name="Q{urn:q}v1" select="1"/>'
            '<xsl:variable name="{urn:q}v2" select="2"/>'
            '<xsl:variable name="Q{}v3" select="3"/>'
            '<xsl:variable name="plain" select="4"/>'
            '<xsl:variable select="5"/>'
            '<xsl:function><xsl:sequence select="6"/></xsl:function>'
            '<xsl:function name=" "><xsl:sequence select="7"/></xsl:function>'
        ),
    )
    with pytest.raises(XsltStaticError) as caught:
        StaticContext.for_module(module)
    assert _code(caught.value) == "XTSE0010"

    _write(
        tmp_path,
        "malformed-eqname.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
        '<xsl:variable name="Q{broken" select="1"/>'
        "</xsl:stylesheet>",
    )
    malformed = _module(tmp_path, (tmp_path / "malformed-eqname.xsl").read_text(), name="q.xsl")
    with pytest.raises(XsltStaticError) as caught:
        StaticContext.for_module(malformed)
    assert _code(caught.value) == "XTSE0010"

    _write(
        tmp_path,
        "valid-names.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:q="urn:q">'
        '<xsl:variable name="Q{urn:q}v1" select="1"/>'
        '<xsl:variable name="{urn:q}v2" select="2"/>'
        '<xsl:variable name="Q{}v3" select="3"/>'
        '<xsl:variable name="plain" select="4"/>'
        "</xsl:stylesheet>",
    )
    names = _module(tmp_path, (tmp_path / "valid-names.xsl").read_text(), name="n.xsl")
    names_context = StaticContext.for_module(names)
    assert set(names_context.variables) == {
        "{urn:q}v1",
        "{urn:q}v2",
        "v3",
        "plain",
    }

    _write(
        tmp_path,
        "unknown-prefix.xsl",
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
        '<xsl:variable name="missing:name" select="1"/>'
        "</xsl:stylesheet>",
    )
    unknown = _module(tmp_path, (tmp_path / "unknown-prefix.xsl").read_text(), name="u.xsl")
    with pytest.raises(XsltStaticError) as caught:
        StaticContext.for_module(unknown)
    assert _code(caught.value) == "XTSE0280"

    for index, visibility in enumerate(("sometimes", "")):
        name = f"invalid-visibility-{index}.xsl"
        _write(
            tmp_path,
            name,
            f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}">'
            f'<xsl:function name="f" visibility="{visibility}">'
            '<xsl:sequence select="1"/>'
            "</xsl:function>"
            "</xsl:stylesheet>",
        )
        invalid = _module(tmp_path, (tmp_path / name).read_text(), name=f"v{index}.xsl")
        with pytest.raises(XsltStaticError) as caught:
            StaticContext.for_module(invalid)
        assert _code(caught.value) == "XTSE0020"


def test_context_expand_text_no_and_empty_defaults(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/"><a xsl:expand-text="no" xsl:default-collation="  "/></xsl:template>',
            attributes=' expand-text="yes" default-collation="urn:root"',
        ),
    )
    assert StaticContext.for_element(_element(module.root, "a")).expand_text is False
    assert StaticContext.for_element(_element(module.root, "a")).default_collation is None

    context = StaticContext.for_module(module)
    changed = context.with_default_validation("preserve").with_input_type_annotations("preserve")
    assert changed.default_validation == "preserve"
    assert changed.input_type_annotations == "preserve"


def test_literal_result_standard_spellings_are_ordinary_attributes(tmp_path: Path) -> None:
    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:template match="/"><out version="9.9" '
            'xpath-default-namespace="urn:wrong" expand-text="maybe" '
            'default-collation="urn:wrong"/></xsl:template>'
        ),
    )
    out = _element(module.root, "out")
    context = StaticContext.for_element(out)
    assert context.version == "3.0"
    assert context.xpath_version == "3.1"
    assert context.xpath_default_namespace is None
    assert context.expand_text is False
    assert context.default_collation is None


def test_context_symbol_helpers_and_unqualified_function_skip() -> None:
    context = StaticContext()
    context = context.with_function(StaticFunction(name="f", arity=1))
    assert context.xpath_static_context().functions == {}
    variadic = StaticContext(namespaces={"t": "urn:test"}).with_function(
        StaticFunction(name="{urn:test}variadic", arity=(1, None))
    )
    assert variadic.xpath_static_context().functions["{urn:test}variadic"] == (1, None)
    assert variadic.function_arities("{urn:test}variadic") == (1, None)
    exact = StaticContext(namespaces={"t": "urn:test"}).with_function(
        StaticFunction(name="{urn:test}exact", arity=2)
    )
    assert exact.function_arities("{urn:test}exact") == (2, 2)
    prefixed = StaticContext(namespaces={"t": "urn:test"}).with_function(
        StaticFunction(name="t:prefixed", arity=1)
    )
    assert prefixed.xpath_static_context().functions["t:prefixed"] == 1


def test_context_for_element_module_path_and_cyclic_graph(tmp_path: Path) -> None:
    module = _module(tmp_path, _stylesheet('<xsl:variable name="v" select="1"/>'))
    with pytest.raises(TypeError):
        StaticContext.for_element(module.root, module="module")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext.for_element(module.root, parent="parent")  # type: ignore[arg-type]
    context = StaticContext.for_element(module.root, module=module)
    assert context.variable("v") is not None

    cyclic = replace(module)
    object.__setattr__(cyclic, "includes", (cyclic,))
    cyclic_context = StaticContext.for_module(cyclic)
    assert cyclic_context.variable("v") is not None


def test_unprefixed_function_namespace_is_not_registered_lexically(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.expressions import compile_xpath

    module = _module(
        tmp_path,
        _stylesheet(
            '<xsl:function name="Q{urn:no-prefix}f"><xsl:sequence select="1"/></xsl:function>'
        ),
    )
    context = StaticContext.for_module(module)
    assert context.function("{urn:no-prefix}f", 0) is not None
    assert context.xpath_static_context().functions == {}
    assert compile_xpath("1 + 1", context).source == "1 + 1"
