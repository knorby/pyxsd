"""Python customization rules compiled into synthetic XSLT layers.

A :class:`~pyxsd.xslt.python_rules.PythonRules` value registers named-component
overrides and named-mode template rules.  The processor validates every binding
against the unmodified compiled artifact, builds a synthetic layer from the
retained module graph, and never mutates the artifact it was given.  Callbacks
see a restricted context: a named-component override may call the original,
while a mode rule may continue with ``next-match`` or ``apply-imports``.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic
from pyxsd.xdm.nodes import XdmDocument
from pyxsd.xslt import (
    PythonRuleContext,
    PythonRules,
    XsltDynamicError,
    XsltExtensions,
    XsltProcessor,
)
from pyxsd.xslt.compiler.packages import PackageResolver
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
APP = "urn:example:app"
DOC = "urn:example:doc"
BASE = "urn:example:base"
PRINCIPAL = "urn:example:principal"


def app(local: str) -> str:
    return f"{{{APP}}}{local}"


ITEM = XdmDocument.parse(b'<doc:item xmlns:doc="urn:example:doc">item</doc:item>').root_element


def doc(local: str) -> str:
    return f"{{{DOC}}}{local}"


def package(body: str, *, name: str = BASE, version: str = "1.0.0") -> bytes:
    return (
        f'<xsl:package name="{name}" package-version="{version}" version="3.0" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:app="{APP}" xmlns:doc="{DOC}" '
        f'xmlns:xs="{XSD_NS}">{body}</xsl:package>'
    ).encode()


def stylesheet(body: str, *, version: str = "3.0") -> bytes:
    return (
        f'<xsl:stylesheet version="{version}" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:app="{APP}" xmlns:doc="{DOC}" xmlns:xs="{XSD_NS}">{body}</xsl:stylesheet>'
    ).encode()


def compile_rules(
    source: bytes,
    rules: PythonRules,
    *,
    resolver: PackageResolver | None = None,
) -> Any:
    extensions = XsltExtensions().with_python_rules(rules)
    return XsltProcessor(resolver=resolver).compile(
        io.BytesIO(source),
        extensions=extensions,
    )


def code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


BASE_BODY = """
<xsl:mode name="app:process" visibility="public"/>
<xsl:template name="app:target" visibility="public"><original/></xsl:template>
<xsl:function name="app:double" visibility="public" as="xs:integer">
  <xsl:param name="value" as="xs:integer"/>
  <xsl:sequence select="$value * 2"/>
</xsl:function>
<xsl:template match="doc:item" mode="app:process"><base-rule/></xsl:template>
"""


def base_rules(
    *,
    template_callback: Any = None,
    function_callback: Any = None,
    mode_callback: Any = None,
) -> PythonRules:
    rules = PythonRules.empty()
    if template_callback is not None:
        rules = rules.override_template("app:target", template_callback)
    if function_callback is not None:
        rules = rules.override_function("app:double", 1, function_callback)
    if mode_callback is not None:
        rules = rules.add_mode_rule("app:process", "doc:item", mode_callback)
    return rules


def test_named_template_override_invokes_the_saved_original() -> None:
    def wrapped(context: PythonRuleContext) -> Any:
        assert context.context_item is None
        return context.invoke_original()

    compiled = compile_rules(package(BASE_BODY), base_rules(template_callback=wrapped))
    result = compiled.transform(initial_template="Q{urn:example:app}target")
    assert "<original" in result.serialize()


def test_public_function_override_and_invoke_original() -> None:
    def python_double(context: PythonRuleContext) -> Any:
        assert list(context.parameters) == ["value"]
        return context.invoke_original(42)

    compiled = compile_rules(package(BASE_BODY), base_rules(function_callback=python_double))
    result = compiled.transform(
        initial_function="Q{urn:example:app}double",
        initial_function_arguments=(21,),
        delivery="raw",
    )
    assert result.principal == (XdmAtomic.integer(84),)


def test_public_mode_rule_next_match_and_apply_imports() -> None:
    def python_rule(context: PythonRuleContext) -> Any:
        assert context.context_item is not None
        return context.next_match()

    compiled = compile_rules(package(BASE_BODY), base_rules(mode_callback=python_rule))
    result = compiled.transform(
        initial_match_selection=(ITEM,),
        initial_mode="Q{urn:example:app}process",
    )
    assert "<base-rule" in result.serialize()

    def imported(context: PythonRuleContext) -> Any:
        return context.apply_imports()

    compiled = compile_rules(package(BASE_BODY), base_rules(mode_callback=imported))
    result = compiled.transform(
        initial_match_selection=(ITEM,),
        initial_mode="Q{urn:example:app}process",
    )
    assert "<base-rule" in result.serialize()


def test_mode_rule_result_crosses_the_xdm_boundary() -> None:
    def python_rule(context: PythonRuleContext) -> Any:
        del context
        return 7

    compiled = compile_rules(package(BASE_BODY), base_rules(mode_callback=python_rule))
    result = compiled.transform(
        initial_match_selection=(ITEM,),
        initial_mode="Q{urn:example:app}process",
    )
    assert result.serialize().endswith("7")


def test_abstract_accepted_component_is_completed_but_has_no_original() -> None:
    abstract_base = package(
        '<xsl:template name="app:target" visibility="abstract"/>',
    )
    principal = package(
        """
        <xsl:use-package name="urn:example:base">
          <xsl:accept component="template" names="app:target" visibility="abstract"/>
        </xsl:use-package>
        <xsl:template name="app:start" visibility="public"/>
        """,
        name=PRINCIPAL,
    )
    resolver = PackageResolver.from_sources(abstract_base)

    def completed(context: PythonRuleContext) -> Any:
        del context
        return "completed"

    compiled = compile_rules(
        principal,
        base_rules(template_callback=completed),
        resolver=resolver,
    )
    result = compiled.transform(initial_template="Q{urn:example:app}target")
    assert result.serialize().endswith("completed")

    def original(context: PythonRuleContext) -> Any:
        return context.invoke_original()

    compiled = compile_rules(
        principal,
        base_rules(template_callback=original),
        resolver=PackageResolver.from_sources(abstract_base),
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert code(caught.value) == "XTDE0040"


def test_private_and_final_targets_are_rejected() -> None:
    private_template = package('<xsl:template name="app:target"><x/></xsl:template>')
    final_template = package(
        '<xsl:template name="app:target" visibility="final"><x/></xsl:template>'
    )
    private_function = package("""
        <xsl:function name="app:double" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
    """)
    final_function = package("""
        <xsl:function name="app:double" visibility="final" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
    """)

    for source, rules in (
        (private_template, base_rules(template_callback=lambda context: ())),
        (final_template, base_rules(template_callback=lambda context: ())),
        (
            private_function,
            base_rules(function_callback=lambda context: 0),
        ),
        (
            final_function,
            base_rules(function_callback=lambda context: 0),
        ),
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(source, rules)
        assert code(caught.value) == "XTSE3050"


def test_unknown_targets_and_arity_mismatch_are_static_errors() -> None:
    source = package(BASE_BODY)
    for rules in (
        PythonRules.empty().override_template("app:missing", lambda context: ()),
        PythonRules.empty().override_function("app:missing", 0, lambda context: ()),
        PythonRules.empty().override_function("app:double", 2, lambda context: ()),
    ):
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(source, rules)
        assert code(caught.value) == "XTSE0010"


def test_private_final_and_unnamed_modes_are_rejected() -> None:
    for body in (
        '<xsl:mode name="app:process" visibility="private"/>',
        '<xsl:mode name="app:process" visibility="final"/>',
    ):
        rules = PythonRules.empty().add_mode_rule("app:process", "doc:item", lambda context: ())
        with pytest.raises(XsltStaticError) as caught:
            compile_rules(package(body), rules)
        assert code(caught.value) == "XTSE3050"

    unnamed = PythonRules.empty().add_mode_rule(None, "doc:item", lambda context: ())
    with pytest.raises(XsltStaticError) as caught:
        compile_rules(package(BASE_BODY), unnamed)
    assert code(caught.value) == "XTSE3050"


def test_dual_name_and_match_target_overrides_only_the_named_component() -> None:
    body = """
    <xsl:mode name="app:process" visibility="public"/>
    <xsl:template name="app:both" match="doc:item" mode="app:process" visibility="public"><both/></xsl:template>
    """

    def named(context: PythonRuleContext) -> Any:
        del context
        return "named"

    compiled = compile_rules(
        package(body),
        PythonRules.empty().override_template("app:both", named),
    )
    named_result = compiled.transform(initial_template="Q{urn:example:app}both")
    assert named_result.serialize().endswith("named")

    rule_result = compiled.transform(
        initial_match_selection=(ITEM,),
        initial_mode="Q{urn:example:app}process",
    )
    assert "<both" in rule_result.serialize()


def test_native_1_0_and_2_0_stylesheets_use_an_import_layer() -> None:
    for version in ("1.0", "2.0"):
        source = stylesheet(
            '<xsl:template name="app:target"><original/></xsl:template>',
            version=version,
        )

        # invoke_original delegates to the pre-customization target component;
        # it is not xsl:original's import-stack continuation.
        def original(context: PythonRuleContext) -> Any:
            return context.invoke_original()

        compiled = compile_rules(source, base_rules(template_callback=original))
        assert compiled.package.used_packages == ()
        assert (
            "<original"
            in compiled.transform(initial_template="Q{urn:example:app}target").serialize()
        )


def test_wrong_role_delegation_is_a_dynamic_error() -> None:
    def bad_template(context: PythonRuleContext) -> Any:
        return context.next_match()

    compiled = compile_rules(package(BASE_BODY), base_rules(template_callback=bad_template))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert code(caught.value) == "XTDE0560"

    def bad_rule(context: PythonRuleContext) -> Any:
        return context.invoke_original()

    compiled = compile_rules(package(BASE_BODY), base_rules(mode_callback=bad_rule))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            initial_match_selection=(ITEM,),
            initial_mode="Q{urn:example:app}process",
        )
    assert code(caught.value) == "XTDE0560"


def test_callback_none_and_exception_conversion() -> None:
    def none_result(context: PythonRuleContext) -> Any:
        del context

    compiled = compile_rules(package(BASE_BODY), base_rules(template_callback=none_result))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert code(caught.value) == "XPTY0004"

    failure = ValueError("callback failed")

    def raises(context: PythonRuleContext) -> Any:
        del context
        raise failure

    compiled = compile_rules(package(BASE_BODY), base_rules(template_callback=raises))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert caught.value.code is None
    assert caught.value.allow_catch is False
    assert caught.value.cause is failure


def test_late_registration_does_not_change_a_compiled_artifact() -> None:
    def first(context: PythonRuleContext) -> Any:
        del context
        return "first"

    rules = PythonRules.empty().override_template("app:target", first)
    compiled = compile_rules(package(BASE_BODY), rules)
    later = rules.override_function("app:double", 1, lambda context: 99)

    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("first")
    )
    assert compiled.extensions.python_rules is not later
    assert later is not rules

    other = compile_rules(package(BASE_BODY), later)
    assert other.transform(
        initial_function="Q{urn:example:app}double",
        initial_function_arguments=(2,),
        delivery="raw",
    ).principal == (XdmAtomic.integer(99),)


def test_python_rules_registry_is_immutable_and_validates_input() -> None:
    rules = PythonRules.empty()
    assert len(rules) == 0
    assert not rules
    updated = rules.override_template("app:target", lambda context: ())
    assert len(updated) == 1
    assert rules is not updated
    assert len(rules) == 0

    with pytest.raises(ValueError):
        rules.override_template("", lambda context: ())
    with pytest.raises(TypeError):
        rules.override_template("app:t", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rules.override_function("app:f", -1, lambda context: ())
    with pytest.raises(ValueError):
        rules.add_mode_rule("app:m", "", lambda context: ())
    with pytest.raises(ValueError):
        rules.add_mode_rule("app:m", "doc:item", lambda context: (), priority="nope")

    extensions = XsltExtensions().with_python_rules(updated)
    assert extensions.python_rules is updated
    resolved = extensions.resolve({"app": APP})
    assert resolved.python_rules is updated


def test_rule_names_accept_eqnames_and_unprefixed_names() -> None:
    rules = (
        PythonRules.empty()
        .override_template("Q{urn:example:app}target", lambda context: ())
        .override_template("local", lambda context: ())
    )
    resolved = rules.resolve({"app": APP})
    assert [entry.name for entry in resolved.entries] == [app("target"), "local"]

    for name in ("1bad", "a:b:c"):
        with pytest.raises(ValueError):
            PythonRules.empty().override_template(name, lambda context: ())
    with pytest.raises(TypeError):
        PythonRules.empty().add_mode_rule(5, "doc:item", lambda context: ())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PythonRules.empty().add_mode_rule("app:m", "doc:item", lambda context: (), priority=True)
    with pytest.raises(ValueError):
        PythonRules.empty().add_mode_rule(
            "app:m", "doc:item", lambda context: (), priority="Infinity"
        )


def test_rule_context_validates_its_restricted_operations() -> None:
    with pytest.raises(TypeError):
        PythonRuleContext(context_item=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PythonRuleContext(parameters=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PythonRuleContext(_next_match=object())  # type: ignore[arg-type]

    context = PythonRuleContext()
    for operation in (context.invoke_original, context.next_match, context.apply_imports):
        with pytest.raises(XsltDynamicError) as caught:
            operation()
        assert code(caught.value) == "XTDE0560"


def test_static_context_and_extensions_validate_python_state() -> None:
    from pyxsd.xslt.compiler.context import StaticContext

    for count in (True, -1):
        with pytest.raises(TypeError):
            StaticContext(python_rule_count=count)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        StaticContext(python_layer=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        XsltExtensions(_python_rules=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        XsltExtensions().with_python_rules(object())  # type: ignore[arg-type]


def test_package_aliases_resolve_a_nameless_descriptor() -> None:
    from pyxsd.xslt.compiler.packages import PackageResolver

    descriptor = PackageResolver.from_sources(
        stylesheet('<xsl:template name="app:t"/>')
    ).descriptors[0]
    resolver = PackageResolver()
    resolver.register(descriptor)
    resolver.register_alias("urn:example:alias", descriptor)
    assert resolver.resolve("urn:example:alias") is descriptor
    resolver.register_alias("urn:example:alias", descriptor)

    with pytest.raises(ValueError):
        resolver.register_alias("relative", descriptor)
    with pytest.raises(TypeError):
        resolver.register_alias("urn:example:other", object())  # type: ignore[arg-type]
    other = PackageResolver.from_sources(package('<xsl:template name="app:o"/>')).descriptors[0]
    with pytest.raises(ValueError):
        resolver.register_alias("urn:example:alias", other)


def test_target_parameter_signatures_are_mirrored() -> None:
    source = package(
        """
        <xsl:template name="app:target" visibility="public" as="xs:string">
          <xsl:context-item use="absent"/>
          <xsl:param name="value" as="xs:string" required="yes"/>
          <xsl:param name="tunneled" as="xs:string" tunnel="yes"/>
          <xsl:sequence select="$value"/>
        </xsl:template>
        """
    )
    rules = PythonRules.empty().override_template(
        "app:target",
        lambda context: context.invoke_original("from-python"),
    )
    compiled = compile_rules(source, rules)
    assert (
        compiled.transform(
            initial_template="Q{urn:example:app}target",
            template_parameters={"value": "x"},
            tunnel_parameters={"tunneled": "y"},
        )
        .serialize()
        .endswith("from-python")
    )


def test_function_flags_and_mode_priority_are_mirrored() -> None:
    source = package(
        """
        <xsl:mode name="app:process" visibility="public"/>
        <xsl:function name="app:f" visibility="public" new-each-time="maybe"
                      identity-sensitive="no" as="xs:string">
          <xsl:param name="value" as="xs:string"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
        """
    )
    rules = (
        PythonRules.empty()
        .override_function("app:f", 1, lambda context: "f")
        .add_mode_rule("app:process", "doc:item", lambda context: (), priority=2)
    )
    compiled = compile_rules(source, rules)
    assert (
        compiled.transform(
            initial_function="Q{urn:example:app}f",
            initial_function_arguments=("x",),
            delivery="raw",
        )
        .principal[0]
        .value
        == "f"
    )
    component = compiled.package.function(app("f"), 1)
    assert component is not None
    assert component.declaration.attribute("new-each-time") == "maybe"
    assert component.declaration.attribute("identity-sensitive") == "no"
    mode = compiled.package.mode(app("process"))
    assert mode is not None
    override = next(
        rule
        for layer in mode.rule_layers
        for rule in layer.rules
        if rule.owner == compiled.package.name
    )
    assert override.priority == Decimal(2)
    assert override.pattern == "doc:item"


def test_synthetic_layer_requires_bound_rules() -> None:
    from pyxsd.xslt.compiler.modules import ModuleLoader
    from pyxsd.xslt.compiler.static import StaticPreprocessor
    from pyxsd.xslt.python_rules import build_python_layer

    effective = StaticPreprocessor().apply(
        ModuleLoader().load(io.BytesIO(stylesheet('<xsl:template name="app:t"/>')))
    )
    rules = PythonRules.empty().override_template("app:t", lambda context: ())
    with pytest.raises(ValueError):
        build_python_layer(rules, effective)


def test_unbound_rule_indices_are_rejected_at_runtime() -> None:
    from pyxsd.xslt.python_rules import _bound_at, execute_python_rule

    class Stylesheet:
        extensions = XsltExtensions().with_python_rules(
            PythonRules.empty().override_template("app:t", lambda context: ())
        )

    class Invocation:
        stylesheet = Stylesheet()

    with pytest.raises(XsltDynamicError) as caught:
        execute_python_rule(0, Invocation(), None)
    assert caught.value.code is None

    rules = PythonRules.empty()
    assert _bound_at(rules, True) is None  # type: ignore[arg-type]
    assert _bound_at(rules, -1) is None
    assert _bound_at(rules, 0) is None


def test_unusable_callback_results_are_type_errors() -> None:
    def unusable(context: PythonRuleContext) -> Any:
        del context
        return object()

    compiled = compile_rules(package(BASE_BODY), base_rules(template_callback=unusable))
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert code(caught.value) == "XPTY0004"


def test_invoke_original_rejects_wrong_argument_counts() -> None:
    def too_many_function_arguments(context: PythonRuleContext) -> Any:
        return context.invoke_original(1, 2)

    compiled = compile_rules(
        package(BASE_BODY),
        base_rules(function_callback=too_many_function_arguments),
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            initial_function="Q{urn:example:app}double",
            initial_function_arguments=(1,),
        )
    assert code(caught.value) == "XPTY0004"

    source = package('<xsl:template name="app:target" visibility="public"/>')
    rules = PythonRules.empty().override_template(
        "app:target",
        lambda context: context.invoke_original("unexpected"),
    )
    compiled = compile_rules(source, rules)
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}target")
    assert code(caught.value) == "XPTY0004"


def test_saved_components_resolve_references_inside_original_templates() -> None:
    source = stylesheet(
        """
        <xsl:template name="app:inner"><inner/></xsl:template>
        <xsl:template name="app:outer"><xsl:call-template name="app:inner"/></xsl:template>
        """
    )
    rules = (
        PythonRules.empty()
        .override_template("app:inner", lambda context: "python-inner")
        .override_template("app:outer", lambda context: context.invoke_original())
    )
    compiled = compile_rules(source, rules)
    assert "<inner" in compiled.transform(initial_template="Q{urn:example:app}outer").serialize()


def test_function_original_recursion_uses_the_saved_plan() -> None:
    source = stylesheet(
        """
        <xsl:function name="app:fact" visibility="public" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="if ($value = 0) then 1 else $value * app:fact($value - 1)"/>
        </xsl:function>
        """
    )
    rules = PythonRules.empty().override_function(
        "app:fact",
        1,
        lambda context: context.invoke_original(),
    )
    compiled = compile_rules(source, rules)
    assert compiled.transform(
        initial_function="Q{urn:example:app}fact",
        initial_function_arguments=(3,),
        delivery="raw",
    ).principal == (XdmAtomic.integer(6),)


def test_expression_inside_an_original_template_keeps_the_original_catalog() -> None:
    source = package(
        """
        <xsl:template name="app:target" visibility="public">
          <xsl:sequence select="'original'"/>
        </xsl:template>
        """
    )
    rules = PythonRules.empty().override_template(
        "app:target",
        lambda context: context.invoke_original(),
    )
    compiled = compile_rules(source, rules)
    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("original")
    )


def test_ordinary_stylesheet_can_accept_and_customize_a_package_component() -> None:
    library = package(
        '<xsl:template name="app:target" visibility="public"><library/></xsl:template>'
    )
    principal = stylesheet(
        """
        <xsl:use-package name="urn:example:base">
          <xsl:accept component="template" names="app:target" visibility="public"/>
        </xsl:use-package>
        <xsl:template name="app:start" visibility="public">
          <xsl:call-template name="app:target"/>
        </xsl:template>
        """
    )
    rules = PythonRules.empty().override_template("app:target", lambda context: "python")
    compiled = compile_rules(
        principal,
        rules,
        resolver=PackageResolver.from_sources(library),
    )
    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("python")
    )


def test_root_declaration_attributes_are_copied_to_the_layer() -> None:
    source = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:app="{APP}" '
        'xpath-default-namespace="urn:example:doc" default-validation="strip" '
        'input-type-annotations="strip">'
        '<xsl:template name="app:target"><original/></xsl:template>'
        "</xsl:stylesheet>"
    ).encode()
    compiled = compile_rules(
        source,
        PythonRules.empty().override_template("app:target", lambda context: "copied"),
    )
    assert (
        compiled.transform(initial_template="Q{urn:example:app}target")
        .serialize()
        .endswith("copied")
    )
    layer_root = compiled.package.modules[0].root
    assert layer_root.attribute("xpath-default-namespace") == "urn:example:doc"
    assert layer_root.attribute("default-validation") == "strip"
    assert compiled.package.default_validation == "strip"
    assert compiled.package.input_type_annotations == "strip"


def test_evaluation_context_validates_original_component_maps() -> None:
    from pyxsd.xslt.runtime.context import EvaluationContext

    with pytest.raises(TypeError):
        EvaluationContext(original_components=[])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EvaluationContext(original_plans=[])  # type: ignore[arg-type]


def test_optional_parameter_defaults_survive_override() -> None:
    source = package(
        """
        <xsl:template name="app:typed" visibility="public">
          <xsl:param name="label" as="xs:string" select="'default-label'"/>
          <xsl:sequence select="$label"/>
        </xsl:template>
        <xsl:template name="app:untyped" visibility="public">
          <xsl:param name="label" select="'default-untyped'"/>
          <xsl:sequence select="$label"/>
        </xsl:template>
        """
    )
    rules = (
        PythonRules.empty()
        .override_template("app:typed", lambda context: context.parameters["label"])
        .override_template("app:untyped", lambda context: context.parameters["label"])
    )
    compiled = compile_rules(source, rules)
    assert (
        compiled.transform(initial_template="Q{urn:example:app}typed")
        .serialize()
        .endswith("default-label")
    )
    assert (
        compiled.transform(initial_template="Q{urn:example:app}untyped")
        .serialize()
        .endswith("default-untyped")
    )


def test_nested_original_body_cannot_execute_an_abstract_component() -> None:
    library = package(
        """
        <xsl:template name="app:a" visibility="abstract"/>
        <xsl:template name="app:b" visibility="public">
          <xsl:call-template name="app:a"/>
        </xsl:template>
        """
    )
    principal = package(
        """
        <xsl:use-package name="urn:example:base">
          <xsl:accept component="template" names="app:a" visibility="abstract"/>
          <xsl:accept component="template" names="app:b" visibility="public"/>
        </xsl:use-package>
        """,
        name=PRINCIPAL,
    )
    rules = (
        PythonRules.empty()
        .override_template("app:a", lambda context: "completed")
        .override_template("app:b", lambda context: context.invoke_original())
    )
    compiled = compile_rules(
        principal,
        rules,
        resolver=PackageResolver.from_sources(library),
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(initial_template="Q{urn:example:app}b")
    assert code(caught.value) == "XTDE0040"


def test_public_mode_rule_on_an_ordinary_stylesheet() -> None:
    source = stylesheet(
        """
        <xsl:mode name="app:process" visibility="public"/>
        <xsl:template match="doc:item" mode="app:process"><base-rule/></xsl:template>
        """
    )
    rules = PythonRules.empty().add_mode_rule(
        "app:process",
        "doc:item",
        lambda context: context.next_match(),
    )
    compiled = compile_rules(source, rules)
    result = compiled.transform(
        initial_match_selection=(ITEM,),
        initial_mode="Q{urn:example:app}process",
    )
    assert "<base-rule" in result.serialize()


def test_wrapper_return_type_is_validated() -> None:
    source = package(
        """
        <xsl:function name="app:f" visibility="public" as="xs:integer">
          <xsl:param name="value" as="xs:integer"/>
          <xsl:sequence select="$value"/>
        </xsl:function>
        """
    )
    rules = PythonRules.empty().override_function("app:f", 1, lambda context: "not-an-integer")
    compiled = compile_rules(source, rules)
    with pytest.raises(XsltError) as caught:
        compiled.transform(
            initial_function="Q{urn:example:app}f",
            initial_function_arguments=(1,),
        )
    assert code(caught.value) == "XTTE0780"


def test_nested_original_function_cannot_execute_an_abstract_component() -> None:
    library = package(
        """
        <xsl:function name="app:a" visibility="abstract" as="xs:string">
          <xsl:param name="value" as="xs:string"/>
        </xsl:function>
        <xsl:function name="app:b" visibility="public" as="xs:string">
          <xsl:param name="value" as="xs:string"/>
          <xsl:sequence select="app:a($value)"/>
        </xsl:function>
        """
    )
    principal = package(
        """
        <xsl:use-package name="urn:example:base">
          <xsl:accept component="function" names="app:a#1" visibility="abstract"/>
          <xsl:accept component="function" names="app:b#1" visibility="public"/>
        </xsl:use-package>
        """,
        name=PRINCIPAL,
    )
    rules = (
        PythonRules.empty()
        .override_function("app:a", 1, lambda context: "completed")
        .override_function("app:b", 1, lambda context: context.invoke_original())
    )
    compiled = compile_rules(
        principal,
        rules,
        resolver=PackageResolver.from_sources(library),
    )
    with pytest.raises(XsltDynamicError) as caught:
        compiled.transform(
            initial_function="Q{urn:example:app}b",
            initial_function_arguments=("x",),
        )
    assert code(caught.value) == "XTDE0040"
