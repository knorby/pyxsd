"""Effective-version, compatibility, and recovery policy behavior.

The XSLT 3.0 processor keeps its own identity when an element declares an
earlier effective version: ``version="1.0"`` enables only the specified 1.0
compatibility conversions, ``version="2.0"`` is processed as 3.0, and neither
selects a native language profile.  These tests are corpus-independent and
assert both the profile decisions and the behavior of the affected
instructions.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from pyxsd.xdm import XdmAtomic, XdmFunction
from pyxsd.xdm.nodes import NodeKind, XdmDocument
from pyxsd.xslt import XsltProcessor
from pyxsd.xslt.compiler.context import ProfileOptions, StaticContext
from pyxsd.xslt.errors import XsltDynamicError
from pyxsd.xslt.profiles import (
    DOCUMENT_FRAGMENT_IDENTIFIER,
    DOCUMENT_RETRIEVAL,
    LanguageProfile,
    RecoveryPolicy,
    RecoveryRule,
    system_property_for,
)
from pyxsd.xslt.runtime.functions import XsltContextBridge
from pyxsd.xslt.runtime.transformation import Transformation

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"
MY_NS = "urn:my"


def stylesheet(body: str, *, version: str = "3.0", attributes: str = "") -> bytes:
    return (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:xs="{XSD_NS}" '
        f'xmlns:my="{MY_NS}" version="{version}"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def transform(
    body: str,
    *,
    version: str = "3.0",
    attributes: str = "",
    source: bytes = b"<doc/>",
    initial_template: str = "main",
    delivery: str = "raw",
) -> Any:
    compiled = XsltProcessor().compile(
        io.BytesIO(stylesheet(body, version=version, attributes=attributes))
    )
    return compiled.transform(
        source=source,
        initial_template=initial_template,
        delivery=delivery,
    )


def element_text(node: Any) -> str:
    return "".join(child.string_value for child in node.children if child.kind is NodeKind.TEXT)


def attribute_value(element: Any, name: str) -> str:
    attribute = element.attribute(name)
    assert attribute is not None
    return attribute.string_value


def principal_element(body: str, *, version: str = "3.0") -> Any:
    result = transform(body, version=version, delivery="document")
    tree = result.principal_tree
    return tree.children[0]


# ---------------------------------------------------------------------------
# Effective version and processor identity


def test_effective_versions_keep_the_processor_identity() -> None:
    assert system_property_for(version="1.0", processor="3.0") == "3.0"
    assert system_property_for(version="2.0", processor="3.0") == "3.0"
    assert system_property_for(version="3.0", processor="3.0") == "3.0"
    assert system_property_for(version="1.0", processor="2.0") == "2.0"

    for version in ("1.0", "2.0", "3.0"):
        result = transform(
            '<xsl:template name="main">'
            "<xsl:sequence select=\"system-property('xsl:version')\"/>"
            "</xsl:template>",
            version=version,
        )
        assert result.principal == (XdmAtomic.string("3.0"),)


def test_profile_effective_versions_and_native_flags() -> None:
    one = LanguageProfile.for_processor("3.0", "1.0")
    assert one.processor_version == "3.0"
    assert one.effective_version == "1.0"
    assert one.compatibility_mode is True
    assert one.native is False
    assert one.xpath_version == "3.1"
    assert one.system_property("xsl:version") == "3.0"

    two = LanguageProfile.for_processor("3.0", "2.0")
    assert two.compatibility_mode is False
    assert two.native is False
    assert two.system_property("xsl:version") == "3.0"

    native_two = LanguageProfile.for_processor("2.0", "2.0")
    assert native_two.native is True
    assert native_two.compatibility_mode is False
    assert native_two.xpath_version == "2.0"
    assert native_two.system_property("xsl:version") == "2.0"

    native_one = LanguageProfile.for_processor("1.0", "1.0")
    assert native_one.native is True
    assert native_one.compatibility_mode is True
    assert native_one.xpath_version == "1.0"


def test_profile_system_properties() -> None:
    profile = LanguageProfile.for_processor("3.0", "1.0")
    assert profile.system_property(f"Q{{{XSLT_NS}}}version") == "3.0"
    assert profile.system_property(f"{{{XSLT_NS}}}version") == "3.0"
    assert profile.system_property("xsl:xpath-version") == "3.1"
    assert profile.system_property("xsl:is-schema-aware") == "yes"
    assert profile.system_property("xsl:supports-backwards-compatibility") == "yes"
    assert profile.system_property("") == ""
    assert profile.system_property(1) == ""  # type: ignore[arg-type]
    assert profile.system_property("version") == ""
    assert profile.system_property("xsl:unknown") == ""
    assert profile.system_property("Q{urn:other}version") == ""
    assert profile.system_property("Q{broken") == ""
    assert profile.system_property("{broken") == ""
    assert profile.system_property("other:version") == ""

    not_aware = LanguageProfile(schema_aware=False)
    assert not_aware.system_property("xsl:is-schema-aware") == "no"


def test_profile_validation_and_option_conversion() -> None:
    class SimpleOptions:
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

    with pytest.raises(ValueError):
        LanguageProfile(processor_version="4.0")
    with pytest.raises(ValueError):
        LanguageProfile(effective_version="not-a-number")
    with pytest.raises(ValueError):
        LanguageProfile(effective_version="-1")
    with pytest.raises(ValueError):
        LanguageProfile(effective_version="nan")
    with pytest.raises(TypeError):
        LanguageProfile(processor_version=3)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LanguageProfile(schema_aware="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LanguageProfile.for_processor("3.0", 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        LanguageProfile.from_options(SimpleOptions(processor_profile=3))
    with pytest.raises(TypeError):
        LanguageProfile.from_options(SimpleOptions(schema_aware="yes"))

    options = ProfileOptions(processor_profile="xslt-2.0", schema_aware=False)
    converted = LanguageProfile.from_options(options, "1.0")
    assert converted.processor_version == "2.0"
    assert converted.native is True
    assert converted.schema_aware is False

    default = LanguageProfile.from_options(None)
    assert default.processor_version == "3.0"
    assert default.effective_version == "3.0"


def test_static_context_uses_profile_compatibility_rules() -> None:
    assert StaticContext(version="1.0").compatibility_mode is True
    assert StaticContext(version="2.0").compatibility_mode is False
    assert StaticContext(version="1.5").compatibility_mode is False
    assert (
        StaticContext(
            version="3.0",
            profile=ProfileOptions(compatibility_mode=True),
        ).compatibility_mode
        is True
    )


# ---------------------------------------------------------------------------
# 1.0 compatibility conversions


def test_profile_string_conversion_rules() -> None:
    compatible = LanguageProfile.for_processor("3.0", "1.0")
    normal = LanguageProfile.for_processor("3.0", "3.0")
    document = XdmDocument.parse(b"<doc><a>one</a><a>two</a></doc>")
    first_child = document.root_element.children[0]

    assert (
        compatible.string_sequence((XdmAtomic.string("first"), XdmAtomic.string("second")))
        == "first"
    )
    assert compatible.string_sequence((first_child, XdmAtomic.string("second"))) == "one"
    assert compatible.string_sequence(()) == ""
    assert compatible.string_sequence((XdmAtomic.string("only"),)) == "only"

    assert normal.string_sequence((XdmAtomic.string("only"),)) == "only"
    assert normal.string_sequence((first_child,)) == "one"
    assert normal.string_sequence(()) == ""
    with pytest.raises(XsltDynamicError) as caught:
        normal.string_sequence((XdmAtomic.string("first"), XdmAtomic.string("second")))
    assert caught.value.code == "XPTY0004"

    function = XdmFunction(lambda *arguments: (), arity=0)
    for profile in (compatible, normal):
        with pytest.raises(XsltDynamicError) as caught:
            profile.string_sequence((function,))
        assert caught.value.code == "FOTY0013"


def test_value_of_uses_first_item_only_without_separator() -> None:
    one = principal_element(
        '<xsl:template name="main"><out><xsl:value-of select="(1, 2, 3)"/></out></xsl:template>',
        version="1.0",
    )
    assert element_text(one) == "1"

    three = principal_element(
        '<xsl:template name="main"><out><xsl:value-of select="(1, 2, 3)"/></out></xsl:template>',
        version="3.0",
    )
    assert element_text(three) == "1 2 3"

    node_sequence = transform(
        '<xsl:template name="main"><out><xsl:value-of select="/doc/a"/></out></xsl:template>',
        version="1.0",
        source=b"<doc><a>one</a><a>two</a></doc>",
        delivery="document",
    ).principal_tree.children[0]
    assert element_text(node_sequence) == "one"
    node_sequence_three = transform(
        '<xsl:template name="main"><out><xsl:value-of select="/doc/a"/></out></xsl:template>',
        source=b"<doc><a>one</a><a>two</a></doc>",
        delivery="document",
    ).principal_tree.children[0]
    assert element_text(node_sequence_three) == "one two"


def test_value_of_explicit_separator_and_constructor_content_are_not_truncated() -> None:
    separated = principal_element(
        '<xsl:template name="main"><out><xsl:value-of select="(1, 2, 3)" '
        'separator="|"/></out></xsl:template>',
        version="1.0",
    )
    assert element_text(separated) == "1|2|3"

    constructed = principal_element(
        '<xsl:template name="main"><out><xsl:value-of>'
        '<xsl:sequence select="(1, 2, 3)"/>'
        "</xsl:value-of></out></xsl:template>",
        version="1.0",
    )
    assert element_text(constructed) == "123"


def test_attribute_value_templates_use_first_atomized_item_in_1_0_mode() -> None:
    one = principal_element(
        '<xsl:template name="main"><out value="{(1, 2, 3)}"/></xsl:template>',
        version="1.0",
    )
    assert attribute_value(one, "value") == "1"

    three = principal_element(
        '<xsl:template name="main"><out value="{(1, 2, 3)}"/></xsl:template>',
        version="3.0",
    )
    assert attribute_value(three, "value") == "1 2 3"

    two = principal_element(
        '<xsl:template name="main"><out value="{(1, 2, 3)}"/></xsl:template>',
        version="2.0",
    )
    assert attribute_value(two, "value") == "1 2 3"


def test_2_0_effective_version_uses_normal_3_0_conversions() -> None:
    value = principal_element(
        '<xsl:template name="main"><out><xsl:value-of select="(1, 2, 3)"/></out></xsl:template>',
        version="2.0",
    )
    assert element_text(value) == "1 2 3"

    attribute = principal_element(
        '<xsl:template name="main"><out value="{(1, 2, 3)}"/></xsl:template>',
        version="2.0",
    )
    assert attribute_value(attribute, "value") == "1 2 3"


def test_1_0_compatibility_does_not_leak_native_profile_features() -> None:
    result = transform(
        """
        <xsl:function name="my:f">
          <xsl:sequence select="'function-ok'"/>
        </xsl:function>
        <xsl:template name="main">
          <xsl:variable name="tree"><root><child>text</child></root></xsl:variable>
          <xsl:sequence select="count($tree/root/child)"/>
          <xsl:sequence select="my:f()"/>
        </xsl:template>
        """,
        version="1.0",
    )
    assert result.principal == (
        XdmAtomic.integer(1),
        XdmAtomic.string("function-ok"),
    )


# ---------------------------------------------------------------------------
# Rule-specific recovery


def test_recovery_is_rule_specific_and_profile_bounded() -> None:
    three = LanguageProfile.for_processor("3.0", "2.0")
    assert three.recovery_rule_for("document-retrieval") is DOCUMENT_RETRIEVAL
    assert three.recovery_rule_for("FODC0002") is DOCUMENT_RETRIEVAL
    assert three.recovery_rule_for("XTDE1160") is DOCUMENT_FRAGMENT_IDENTIFIER
    assert three.recovery_rule_for("XTRE1500") is None
    assert three.recovery_rule_for("XTDE1500") is None
    assert three.recovery_rule_for("not-a-rule") is None
    assert three.permits_recovery(DOCUMENT_RETRIEVAL) is True

    legacy = RecoveryRule("legacy-2.0", frozenset({"XTRE1500"}), "native 2.0 only")
    assert three.permits_recovery(legacy) is False

    native_two = LanguageProfile.for_processor("2.0", "2.0")
    assert native_two.recovery_rule_for("XTRE1500") is not None
    assert native_two.recovery_rule_for("document-retrieval") is None

    native_one = LanguageProfile.for_processor("1.0", "1.0")
    assert native_one.recovery_rule_for("XTRE1500") is None


def test_strict_recovery_turns_only_permitted_rules_into_errors() -> None:
    profile = LanguageProfile.for_processor("3.0", "3.0")
    policy = RecoveryPolicy(rules=frozenset({DOCUMENT_RETRIEVAL.name}))
    assert policy.allows(DOCUMENT_RETRIEVAL, profile) is True
    assert policy.allows(DOCUMENT_FRAGMENT_IDENTIFIER, profile) is False
    assert (
        policy.allows(
            RecoveryRule("legacy", frozenset({"XTRE1500"}), "native 2.0 only"),
            profile,
        )
        is False
    )

    strict = RecoveryPolicy(
        rules=frozenset({DOCUMENT_RETRIEVAL.name, DOCUMENT_FRAGMENT_IDENTIFIER.name}),
        strict=True,
    )
    assert strict.allows(DOCUMENT_RETRIEVAL, profile) is False
    assert strict.allows(DOCUMENT_FRAGMENT_IDENTIFIER, profile) is False


def test_recovery_rule_and_policy_validation() -> None:
    with pytest.raises(ValueError):
        RecoveryRule("", frozenset(), "empty name")
    with pytest.raises(TypeError):
        RecoveryRule("bad", frozenset({1}), "non-string code")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecoveryRule("bad", frozenset(), 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecoveryPolicy(rules=frozenset({"ok", 1}))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecoveryPolicy(rules="document-retrieval")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecoveryPolicy(rules=3)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RecoveryPolicy(strict="yes")  # type: ignore[arg-type]

    policy = RecoveryPolicy()
    profile = LanguageProfile()
    with pytest.raises(TypeError):
        policy.allows("rule", profile)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        policy.allows(DOCUMENT_RETRIEVAL, "profile")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        profile.permits_recovery("document-retrieval")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        profile.recovery_rule_for(3)  # type: ignore[arg-type]


def test_document_retrieval_recovery_requires_an_explicit_policy() -> None:
    default = XsltContextBridge(Transformation())
    with pytest.raises(XsltDynamicError) as caught:
        default.document("urn:missing")
    assert caught.value.code == "FODC0002"

    recovering = XsltContextBridge(
        Transformation(
            recovery_policy=RecoveryPolicy(
                rules=frozenset({DOCUMENT_RETRIEVAL.name}),
            )
        )
    )
    assert recovering.document("urn:missing") == ()

    node_argument = XdmDocument.parse(b"<doc>urn:also-missing</doc>").root_element.children[0]
    assert recovering.document((node_argument,)) == ()

    strict = XsltContextBridge(
        Transformation(
            recovery_policy=RecoveryPolicy(
                rules=frozenset({DOCUMENT_RETRIEVAL.name}),
                strict=True,
            )
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        strict.document("urn:missing")
    assert caught.value.code == "FODC0002"


def test_document_fragment_recovery_ignores_the_fragment() -> None:
    document = XdmDocument.parse(b"<doc/>")
    transformation = Transformation(
        recovery_policy=RecoveryPolicy(
            rules=frozenset({DOCUMENT_FRAGMENT_IDENTIFIER.name}),
        )
    )
    transformation.documents["urn:doc"] = document
    bridge = XsltContextBridge(transformation)

    assert bridge.document("urn:doc#fragment") == (document,)

    strict = XsltContextBridge(
        Transformation(
            recovery_policy=RecoveryPolicy(
                rules=frozenset({DOCUMENT_FRAGMENT_IDENTIFIER.name}),
                strict=True,
            )
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        strict.document("urn:doc#fragment")
    assert caught.value.code == "XTDE1160"


def test_processor_recovery_policy_reaches_document_loading() -> None:
    body = """
        <xsl:template name="main">
          <xsl:sequence select="count(document('urn:missing'))"/>
        </xsl:template>
        """
    recovering = XsltProcessor(
        recovery_policy=RecoveryPolicy(
            rules=frozenset({DOCUMENT_RETRIEVAL.name}),
        )
    )
    recovered = recovering.compile(io.BytesIO(stylesheet(body))).transform(initial_template="main")
    assert recovered.principal == (XdmAtomic.integer(0),)

    strict = XsltProcessor(
        recovery_policy=RecoveryPolicy(
            rules=frozenset({DOCUMENT_RETRIEVAL.name}),
            strict=True,
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        strict.compile(io.BytesIO(stylesheet(body))).transform(initial_template="main")
    assert caught.value.code == "FODC0002"

    with pytest.raises(TypeError):
        XsltProcessor(recovery_policy="recover")  # type: ignore[arg-type]


def test_unlisted_document_errors_are_not_recovered() -> None:
    bridge = XsltContextBridge(
        Transformation(
            recovery_policy=RecoveryPolicy(
                rules=frozenset(
                    {
                        DOCUMENT_RETRIEVAL.name,
                        DOCUMENT_FRAGMENT_IDENTIFIER.name,
                    }
                ),
            )
        )
    )
    with pytest.raises(XsltDynamicError) as caught:
        bridge.document("")
    assert caught.value.code == "XTDE1162"


def test_transformation_profile_and_recovery_policy_are_independent_of_frames() -> None:
    transformation = Transformation(
        recovery_policy=RecoveryPolicy(
            rules=frozenset({DOCUMENT_RETRIEVAL.name}),
        )
    )
    assert transformation.profile_for("1.0").compatibility_mode is True
    assert transformation.profile_for("2.0").compatibility_mode is False
    assert transformation.recover(
        XsltDynamicError("missing", code="FODC0002"),
        DOCUMENT_RETRIEVAL,
    )
