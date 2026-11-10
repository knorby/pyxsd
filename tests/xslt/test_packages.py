"""XSLT 3.0 package composition, component visibility, and normalized instructions.

Packages are a separate graph from include/import: an implicit stylesheet is one
package, and only ``xsl:use-package`` crosses a package boundary.  These tests
exercise version selection, expose/accept, abstract/final/private/public
components, named-component overrides with ``xsl:original``, mode rule layers,
local constructs, duplicate handling, and forward references without consulting
the advisory XSD inspection report.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from pyxsd.locations import IssueLocation
from pyxsd.xslt.compiler.modules import ModuleLoader
from pyxsd.xslt.compiler.packages import (
    Component,
    Mode,
    PackageResolver,
    PackageVersion,
    compile_package_set,
)
from pyxsd.xslt.compiler.static import StaticPreprocessor
from pyxsd.xslt.errors import XsltStaticError
from pyxsd.xslt.inspection import inspect_stylesheet

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
APP_NS = "urn:example:app"
DOC_NS = "urn:example:doc"
BASE = "urn:example:base"
USING = "urn:example:using"
OTHER = "urn:example:other"


def _package(
    name: str | None,
    body: str = "",
    *,
    version: str = "3.0",
    package_version: str | None = None,
    attributes: str = "",
) -> str:
    name_attr = "" if name is None else f' name="{name}"'
    version_attr = "" if package_version is None else f' package-version="{package_version}"'
    return (
        f'<xsl:package{name_attr}{version_attr} version="{version}" '
        f'xmlns:xsl="{XSLT_NS}" xmlns:app="{APP_NS}" xmlns:doc="{DOC_NS}" '
        f'xmlns:other="{OTHER}" xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        f"{attributes}>{body}</xsl:package>"
    )


def _stylesheet(body: str = "", *, attributes: str = "") -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:app="{APP_NS}" xmlns:doc="{DOC_NS}" xmlns:other="{OTHER}" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema"'
        f"{attributes}>{body}</xsl:stylesheet>"
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _effective(tmp_path: Path, name: str, text: str) -> Any:
    path = _write(tmp_path, name, text)
    return StaticPreprocessor().apply(ModuleLoader().load(path))


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


def _raise_code(callable_: Any) -> str | None:
    with pytest.raises(XsltStaticError) as caught:
        callable_()
    return _code(caught.value)


BASE_PUBLIC = _package(
    BASE,
    """
    <xsl:mode name="app:process" visibility="public"/>
    <xsl:variable name="app:greeting" select="'hello'" visibility="public"/>
    <xsl:attribute-set name="app:attrs" visibility="public">
      <xsl:attribute name="base" select="'yes'"/>
    </xsl:attribute-set>
    <xsl:template name="app:start" visibility="public"><base/></xsl:template>
    <xsl:template match="doc:item" mode="app:process"><base-rule/></xsl:template>
    """,
    package_version="1.0.0",
)

USING_OVERRIDE = _package(
    USING,
    """
    <xsl:use-package name="urn:example:base" package-version="1.0.0">
      <xsl:override>
        <xsl:template name="app:start" visibility="public">
          <using><xsl:call-template name="xsl:original"/></using>
        </xsl:template>
        <xsl:template match="doc:item" mode="app:process"><using-rule/></xsl:template>
      </xsl:override>
    </xsl:use-package>
    <xsl:template name="app:main" visibility="public">
      <xsl:call-template name="app:start"/>
      <xsl:apply-templates select="doc:item" mode="app:process"/>
    </xsl:template>
    """,
    package_version="1.0.0",
)


# ---------------------------------------------------------------------------
# Package visibility, modes, and xsl:original


def test_package_visibility() -> None:
    compiled = compile_package_set(BASE_PUBLIC, USING_OVERRIDE)

    start = compiled.named_template("app:start")
    assert start.body.owner == USING
    assert compiled.original_target("app:start").owner == BASE

    mode = compiled.mode("app:process")
    assert mode.rule_layers[0].owner == USING
    assert mode.rule_layers[-1].owner == BASE
    assert [rule.pattern for layer in mode.rule_layers for rule in layer.rules] == [
        "doc:item",
        "doc:item",
    ]

    original_call = start.body.children[0].children[0]
    assert original_call.reference is not None
    assert original_call.reference.original is True
    assert original_call.reference.owner == BASE


def test_private_override_target_is_rejected() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:start" visibility="private"><base/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(base, USING_OVERRIDE)) == "XTSE3060"


def test_final_override_target_is_rejected() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:start" visibility="final"><base/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(base, USING_OVERRIDE)) == "XTSE3060"


def test_implicit_package_is_one_package_and_public_by_default() -> None:
    principal = _stylesheet(
        '<xsl:template name="app:start"><out/></xsl:template><xsl:mode name="app:mode"/>'
    )
    compiled = compile_package_set(principal)

    assert compiled.name is None
    assert compiled.kind == "implicit"
    assert compiled.named_template("app:start").visibility == "public"
    assert compiled.mode("app:mode").visibility == "public"


# ---------------------------------------------------------------------------
# Version selection


def _versions(*versions: str) -> list[str]:
    return [
        _package(
            "urn:example:versioned",
            f'<xsl:template name="app:marker" visibility="public"><v version="{v}"/></xsl:template>',
            package_version=v,
        )
        for v in versions
    ]


def _using_version(selector: str, body: str = "") -> str:
    return _package(
        USING,
        f"""
        <xsl:use-package name="urn:example:versioned" package-version="{selector}">
          {body}
        </xsl:use-package>
        <xsl:template name="app:main" visibility="public"/>
        """,
        package_version="1.0.0",
    )


def test_version_selection_exact_and_ranges() -> None:
    versions = _versions("1.0.0", "2.0.0", "2.0.0-alpha", "2.5.0")

    compiled = compile_package_set(*versions, _using_version("2.0.0"))
    assert compiled.used_packages[0].version == "2.0.0"

    compiled = compile_package_set(*versions, _using_version("2.0.0-alpha"))
    assert compiled.used_packages[0].version == "2.0.0-alpha"

    compiled = compile_package_set(*versions, _using_version("2.0+"))
    assert compiled.used_packages[0].version == "2.5.0"

    compiled = compile_package_set(*versions, _using_version("2.*"))
    assert compiled.used_packages[0].version == "2.5.0"

    compiled = compile_package_set(*versions, _using_version("1.0 to 2.0.*"))
    assert compiled.used_packages[0].version == "2.0.0"

    compiled = compile_package_set(*versions, _using_version("*"))
    assert compiled.used_packages[0].version == "2.5.0"


def test_version_ranges_choose_the_highest_matching_version_by_default() -> None:
    versions = _versions("1.0", "1.5", "1.9", "2.1")
    compiled = compile_package_set(*versions, _using_version("to 2.0"))
    assert compiled.used_packages[0].version == "1.9"


def test_version_ordering_and_range_parsing() -> None:
    assert PackageVersion.parse("1.0") == PackageVersion.parse("1")
    assert PackageVersion.parse("1.0.2") > PackageVersion.parse("1")
    assert PackageVersion.parse("2.0") > PackageVersion.parse("2.0-rc1")
    assert PackageVersion.parse("1.0.3-rc1") < PackageVersion.parse("1.0.3")
    assert PackageVersion.parse("1.0.10") > PackageVersion.parse("1.0.3")

    ordered = ["0-rc1", "0-rc2", "0", "1", "1.0.2", "1.0.3-rc1", "1.0.3", "1.0.3.2", "1.0.10"]
    parsed = [PackageVersion.parse(v) for v in ordered]
    assert parsed == sorted(parsed)
    assert len({PackageVersion.parse(v) for v in ("1", "1.0", "1.0.0")}) == 1


def test_invalid_or_missing_package_version_is_a_static_error() -> None:
    base = _versions("1.0.0")
    for selector in ("TotallyInvalid", "-3.6", "-alpha", "2.0.0-alpha:beta"):
        assert (
            _raise_code(
                lambda selector=selector: compile_package_set(*base, _using_version(selector))
            )
            == "XTSE3000"
        )

    missing = _package(USING, '<xsl:use-package name="urn:missing"/>')
    assert _raise_code(lambda: compile_package_set(missing)) == "XTSE3000"


def test_package_use_cycles_are_rejected() -> None:
    first = _package(BASE, '<xsl:use-package name="urn:example:using"/>')
    second = _package(USING, '<xsl:use-package name="urn:example:base"/>')
    assert _raise_code(lambda: compile_package_set(first, second)) == "XTSE3000"


# ---------------------------------------------------------------------------
# xsl:expose


def test_expose_sets_visibility_and_reduces_it() -> None:
    base = _package(
        BASE,
        """
        <xsl:template name="app:a"><a/></xsl:template>
        <xsl:template name="app:b" visibility="public"><b/></xsl:template>
        <xsl:template name="app:c" visibility="public"><c/></xsl:template>
        <xsl:function name="app:f" visibility="public"><xsl:sequence select="1"/></xsl:function>
        <xsl:expose component="template" names="app:a" visibility="public"/>
        <xsl:expose component="template" names="app:b" visibility="final"/>
        <xsl:expose component="template" names="app:c" visibility="private"/>
        <xsl:expose component="function" names="app:f#0" visibility="public"/>
        """,
    )
    using = _package(
        USING,
        """
        <xsl:use-package name="urn:example:base"/>
        <xsl:template name="app:main" visibility="public">
          <xsl:call-template name="app:a"/>
          <xsl:call-template name="app:b"/>
        </xsl:template>
        """,
    )
    compiled = compile_package_set(base, using)
    assert compiled.named_template("app:a").visibility == "public"
    assert compiled.named_template("app:b").visibility == "final"
    # Private components are not acquired by the using package.
    assert compiled.named_template("app:c") is None
    assert compiled.function("app:f", 0).visibility == "public"


def test_expose_wildcards_and_specificity() -> None:
    base = _package(
        BASE,
        """
        <xsl:template name="app:a"><a/></xsl:template>
        <xsl:template name="other:a"><a/></xsl:template>
        <xsl:template name="app:b"><b/></xsl:template>
        <xsl:expose component="template" names="*" visibility="private"/>
        <xsl:expose component="template" names="app:*" visibility="final"/>
        <xsl:expose component="template" names="app:a" visibility="public"/>
        """,
    )
    compiled = compile_package_set(base)
    assert compiled.named_template("app:a").visibility == "public"
    assert compiled.named_template("app:b").visibility == "final"
    assert compiled.named_template("other:a").visibility == "private"


def test_expose_consistency_and_unknown_names_are_errors() -> None:
    for visibility, code in (
        ("public", "XTSE3010"),
        ("final", "XTSE3010"),
    ):
        base = _package(
            BASE,
            '<xsl:template name="app:a" visibility="private"/>'
            f'<xsl:expose component="template" names="app:a" visibility="{visibility}"/>',
        )
        assert _raise_code(lambda base=base: compile_package_set(base)) == code

    unmatched = _package(
        BASE,
        '<xsl:template name="app:a"/>'
        '<xsl:expose component="template" names="app:missing" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(unmatched)) == "XTSE3020"

    wrong_kind = _package(
        BASE,
        '<xsl:template name="app:a"/>'
        '<xsl:expose component="variable" names="app:a" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(wrong_kind)) == "XTSE3020"


def test_abstract_expose_requires_explicit_abstract_declaration() -> None:
    implicit = _package(
        BASE,
        '<xsl:function name="app:f"/>'
        '<xsl:expose component="function" names="app:f#0" visibility="abstract"/>',
    )
    assert _raise_code(lambda: compile_package_set(implicit)) == "XTSE3025"

    wildcard = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"/>'
        '<xsl:expose component="function" names="app:*" visibility="abstract"/>',
    )
    assert _raise_code(lambda: compile_package_set(wildcard)) == "XTSE3025"

    mode = _package(
        BASE,
        '<xsl:mode name="app:m"/>'
        '<xsl:expose component="mode" names="app:m" visibility="abstract"/>',
    )
    assert _raise_code(lambda: compile_package_set(mode)) == "XTSE3025"

    declared = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"/>'
        '<xsl:expose component="function" names="app:f#0" visibility="abstract"/>',
    )
    # A library package may keep the abstract component; the top-level check is
    # exercised separately below.
    resolver = PackageResolver.from_sources(declared)
    library = resolver.compile_library("urn:example:base")
    assert library.function("app:f", 0).visibility == "abstract"


def test_expose_syntax_errors() -> None:
    bad_component = _package(
        BASE,
        '<xsl:mode name="app:m"/>'
        '<xsl:expose component="mode" names="#unnamed" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(bad_component)) == "XTSE0020"

    bad_visibility = _package(
        BASE,
        '<xsl:template name="app:a"/>'
        '<xsl:expose component="template" names="app:a" visibility="hidden"/>',
    )
    assert _raise_code(lambda: compile_package_set(bad_visibility)) == "XTSE0020"

    unbound_prefix = _package(
        BASE,
        '<xsl:variable name="app:v" select="1"/>'
        '<xsl:expose component="variable" names="missing:*" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(unbound_prefix)) == "XTSE0020"

    function_without_arity = _package(
        BASE,
        '<xsl:function name="app:f"/>'
        '<xsl:expose component="function" names="app:f" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(function_without_arity)) == "XTSE3020"

    template_with_arity = _package(
        BASE,
        '<xsl:template name="app:t"/>'
        '<xsl:expose component="template" names="app:t#0" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(template_with_arity)) == "XTSE3020"


# ---------------------------------------------------------------------------
# xsl:accept


def test_accept_visibility_and_hidden_components() -> None:
    base = _package(
        BASE,
        """
        <xsl:template name="app:a" visibility="public"><a/></xsl:template>
        <xsl:template name="app:b" visibility="public"><b/></xsl:template>
        <xsl:template name="app:c" visibility="public"><c/></xsl:template>
        """,
    )
    using = _package(
        USING,
        """
        <xsl:use-package name="urn:example:base">
          <xsl:accept component="template" names="app:a" visibility="private"/>
          <xsl:accept component="template" names="app:b" visibility="hidden"/>
          <xsl:accept component="template" names="app:c" visibility="final"/>
        </xsl:use-package>
        <xsl:template name="app:main" visibility="public"/>
        """,
    )
    compiled = compile_package_set(base, using)
    assert compiled.named_template("app:a").visibility == "private"
    assert compiled.named_template("app:b") is None
    assert compiled.named_template("app:c").visibility == "final"


def test_accept_incompatible_visibility_is_an_error_but_wildcards_fall_back() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:a" visibility="final"><a/></xsl:template>',
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:a" visibility="public"/>'
        "</xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3040"

    wildcard = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:*" visibility="public"/>'
        "</xsl:use-package>",
    )
    compiled = compile_package_set(base, wildcard)
    assert compiled.named_template("app:a").visibility == "final"


def test_accept_unknown_name_is_an_error() -> None:
    base = _package(BASE, '<xsl:template name="app:a" visibility="public"/>')
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:missing" visibility="private"/>'
        "</xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3030"


def test_accept_absent_abstract_component() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"/>',
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="function" names="app:f#0" visibility="absent"/>'
        "</xsl:use-package>"
        '<xsl:template name="app:main" visibility="public"/>',
    )
    compiled = compile_package_set(base, using)
    component = compiled.function("app:f", 0)
    assert component is not None
    assert component.visibility == "absent"


def test_two_used_packages_with_conflicting_components_are_an_error() -> None:
    first = _package(
        BASE,
        '<xsl:function name="app:f" visibility="public"><xsl:sequence select="1"/></xsl:function>',
    )
    second = _package(
        OTHER,
        '<xsl:function name="app:f" visibility="public"><xsl:sequence select="2"/></xsl:function>',
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/><xsl:use-package name="urn:example:other"/>',
    )
    assert _raise_code(lambda: compile_package_set(first, second, using)) == "XTSE3050"

    accepted = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>'
        '<xsl:use-package name="urn:example:other">'
        '<xsl:accept component="function" names="app:f#0" visibility="hidden"/>'
        "</xsl:use-package>",
    )
    compiled = compile_package_set(first, second, accepted)
    assert compiled.function("app:f", 0) is not None
    assert compiled.function("app:f", 0).owner == BASE


def test_component_star_expose_and_accept_require_wildcards() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:a" visibility="public"/>',
    )
    expose = _package(
        BASE,
        '<xsl:template name="app:a"/><xsl:expose component="*" names="app:a" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(expose)) == "XTSE3022"

    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="*" names="app:a" visibility="public"/>'
        "</xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3032"


# ---------------------------------------------------------------------------
# xsl:override and signatures


def _override_package(name: str, body: str, selector: str = "1.0.0") -> str:
    return _package(name, body, package_version="1.0.0")


def test_named_component_overrides_and_original_bindings() -> None:
    base = _package(
        BASE,
        """
        <xsl:template name="app:t" visibility="public"><base/></xsl:template>
        <xsl:function name="app:f" as="xs:integer" visibility="public">
          <xsl:param name="x" as="xs:integer"/>
          <xsl:sequence select="$x"/>
        </xsl:function>
        <xsl:variable name="app:v" select="1" visibility="public"/>
        <xsl:attribute-set name="app:a" visibility="public">
          <xsl:attribute name="base" select="'yes'"/>
        </xsl:attribute-set>
        """,
        package_version="1.0.0",
    )
    using = _package(
        USING,
        """
        <xsl:use-package name="urn:example:base">
          <xsl:override>
            <xsl:template name="app:t" visibility="public">
              <xsl:call-template name="xsl:original"/>
            </xsl:template>
            <xsl:function name="app:f" as="xs:integer" visibility="public">
              <xsl:param name="x" as="xs:integer"/>
              <xsl:sequence select="xsl:original($x) + 1"/>
            </xsl:function>
            <xsl:variable name="app:v" select="$xsl:original + 1" visibility="public"/>
            <xsl:attribute-set name="app:a" visibility="public">
              <xsl:attribute name="using" select="'yes'"/>
            </xsl:attribute-set>
          </xsl:override>
        </xsl:use-package>
        <xsl:template name="app:main" visibility="public">
          <xsl:call-template name="app:t"/>
          <xsl:value-of select="app:f(1)"/>
          <xsl:value-of select="$app:v"/>
          <copy xsl:use-attribute-sets="app:a"/>
        </xsl:template>
        """,
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)

    template = compiled.named_template("app:t")
    assert template is not None
    assert template.owner == USING
    assert template.body.owner == USING
    assert compiled.original_target("app:t").owner == BASE
    assert compiled.original_target("app:f", kind="function", arity=1).owner == BASE
    assert compiled.original_target("app:v", kind="variable").owner == BASE
    assert compiled.original_target("app:a", kind="attribute-set").owner == BASE

    function = compiled.function("app:f", 1)
    assert function is not None
    sequence = next(child for child in function.body.children if child.local_name == "sequence")
    assert any(ref.original for ref in sequence.references)
    variable = compiled.variable("app:v")
    assert variable is not None
    assert any(ref.original for ref in variable.declaration.references)
    attribute_set = compiled.attribute_set("app:a")
    assert attribute_set is not None
    assert attribute_set.declaration.attribute("name") == "app:a"


def test_override_of_unknown_component_is_an_error() -> None:
    base = _package(BASE, '<xsl:template name="app:t" visibility="public"/>')
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:missing" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3058"


def test_override_duplicate_declarations_are_errors() -> None:
    base = _package(BASE, '<xsl:template name="app:t" visibility="public"/>')
    duplicate = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" visibility="public"/>'
        '<xsl:template name="app:t" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, duplicate)) == "XTSE3055"

    local = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" visibility="public"/>'
        "</xsl:override></xsl:use-package>"
        '<xsl:template name="app:t" visibility="public"/>',
    )
    assert _raise_code(lambda: compile_package_set(base, local)) == "XTSE3055"


def test_override_signature_compatibility() -> None:
    base = _package(
        BASE,
        """
        <xsl:function name="app:f" as="xs:integer" visibility="public">
          <xsl:param name="x" as="xs:integer"/>
          <xsl:sequence select="$x"/>
        </xsl:function>
        <xsl:variable name="app:v" as="xs:integer" select="1" visibility="public"/>
        <xsl:template name="app:t" as="xs:string" visibility="public">
          <xsl:param name="x" as="xs:string"/>
          <xsl:context-item use="optional" as="item()"/>
        </xsl:template>
        """,
        package_version="1.0.0",
    )

    bad_function = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:function name="app:f" as="xs:string" visibility="public">'
        '<xsl:param name="x" as="xs:string"/><xsl:sequence select="$x"/>'
        "</xsl:function></xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, bad_function)) == "XTSE3070"

    bad_variable = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:variable name="app:v" as="xs:string" select="\'x\'" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, bad_variable)) == "XTSE3070"

    bad_template = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" as="xs:string" visibility="public">'
        '<xsl:param name="x" as="xs:integer"/>'
        "</xsl:template></xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, bad_template)) == "XTSE3070"

    extra_required = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" as="xs:string" visibility="public">'
        '<xsl:param name="x" as="xs:string"/>'
        '<xsl:param name="extra" as="xs:string" required="yes"/>'
        "</xsl:template></xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, extra_required)) == "XTSE3070"

    bad_context = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" as="xs:string" visibility="public">'
        '<xsl:param name="x" as="xs:string"/>'
        '<xsl:context-item use="absent"/>'
        "</xsl:template></xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, bad_context)) == "XTSE3070"

    bad_attribute_set = _package(
        BASE,
        '<xsl:attribute-set name="app:a" visibility="public" streamable="yes"/>',
        package_version="1.0.0",
    )
    bad_attribute_override = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:attribute-set name="app:a" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert (
        _raise_code(lambda: compile_package_set(bad_attribute_set, bad_attribute_override))
        == "XTSE3070"
    )


def test_required_parameter_may_not_be_replaced_by_optional_parameter() -> None:
    base = _package(
        BASE,
        """
        <xsl:param name="app:p" required="yes" visibility="public"/>
        <xsl:variable name="app:v" visibility="public"/>
        """,
        package_version="1.0.0",
    )
    bad = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:param name="app:p" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, bad)) == "XTSE3070"

    good = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:variable name="app:p" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert compile_package_set(base, good).variable("app:p") is not None


def test_override_children_are_restricted() -> None:
    base = _package(BASE, '<xsl:template name="app:t" visibility="public"/>')
    key = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:key name="k" match="*" use="."/>'
        "</xsl:override></xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, key)) == "XTSE0010"

    mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:mode name="app:m"/>'
        "</xsl:override></xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, mode)) == "XTSE0010"


def test_xsl_original_is_illegal_outside_an_override() -> None:
    illegal = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:call-template name="xsl:original"/>'
        "</xsl:template>",
    )
    assert _raise_code(lambda: compile_package_set(illegal)) == "XTSE0650"

    illegal_variable = _package(
        BASE,
        '<xsl:variable name="app:v" select="$xsl:original"/>',
    )
    assert _raise_code(lambda: compile_package_set(illegal_variable)) == "XPST0008"


def test_xsl_original_is_unavailable_for_abstract_targets() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:t" visibility="abstract"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:call-template name="xsl:original"/>'
        "</xsl:template></xsl:override></xsl:use-package>"
        '<xsl:template name="app:main" visibility="public"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE0650"


# ---------------------------------------------------------------------------
# Abstract completion


def test_abstract_components_are_completed_by_override() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"><xsl:param name="x"/></xsl:function>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:function name="app:f" visibility="public">'
        '<xsl:param name="x"/><xsl:sequence select="$x"/>'
        "</xsl:function></xsl:override></xsl:use-package>"
        '<xsl:template name="app:main" visibility="public"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert compiled.function("app:f", 1).visibility == "public"
    assert compiled.original_target("app:f", kind="function", arity=1) is None


def test_unresolved_abstract_component_in_top_level_package_is_an_error() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>'
        '<xsl:template name="app:main" visibility="public"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3080"


def test_hidden_abstract_component_allows_an_executable_package() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="abstract"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="function" names="app:f#0" visibility="hidden"/>'
        "</xsl:use-package>"
        '<xsl:template name="app:main" visibility="public"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert compiled.function("app:f", 0) is None


# ---------------------------------------------------------------------------
# Modes


def test_mode_rule_layers_and_special_modes() -> None:
    base = _package(
        BASE,
        """
        <xsl:mode name="app:public" visibility="public"/>
        <xsl:mode name="app:final" visibility="final"/>
        <xsl:mode name="app:private" visibility="private"/>
        <xsl:template match="doc:a" mode="app:public"/>
        <xsl:template match="doc:a" mode="app:final"/>
        <xsl:template match="doc:a" mode="app:private"/>
        """,
        package_version="1.0.0",
    )

    def override(mode: str) -> str:
        return _package(
            USING,
            f'<xsl:use-package name="urn:example:base"><xsl:override>'
            f'<xsl:template match="doc:a" mode="{mode}"/>'
            "</xsl:override></xsl:use-package>",
            package_version="1.0.0",
        )

    compiled = compile_package_set(base, override("app:public"))
    assert compiled.mode("app:public").rule_layers[0].owner == USING
    assert compiled.mode("app:public").rule_layers[1].owner == BASE

    assert _raise_code(lambda: compile_package_set(base, override("app:final"))) == "XTSE3060"
    assert _raise_code(lambda: compile_package_set(base, override("app:private"))) == "XTSE3060"
    assert _raise_code(lambda: compile_package_set(base, override("#unnamed"))) == "XTSE3440"


def test_mode_override_without_explicit_mode_uses_default_mode() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/><xsl:template match="doc:a" mode="app:m"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:override default-mode="app:m">'
        '<xsl:template match="doc:a"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert compiled.mode("app:m").rule_layers[0].owner == USING


def test_rule_added_to_used_mode_outside_override_is_an_error() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/><xsl:template match="doc:a" mode="app:m"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3050"


def test_mode_redeclaration_and_unnamed_rules_are_errors() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )
    redeclared = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/><xsl:mode name="app:m" visibility="private"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, redeclared)) == "XTSE3050"

    unnamed = _package(
        BASE,
        '<xsl:mode/><xsl:template match="doc:a"/>',
    )
    assert compile_package_set(unnamed).mode(None).rule_layers[0].owner == BASE

    bad_name = _package(BASE, '<xsl:mode name="#unnamed"/>')
    assert _raise_code(lambda: compile_package_set(bad_name)) == "XTSE0020"

    visibility = _package(BASE, '<xsl:mode visibility="abstract"/>')
    assert _raise_code(lambda: compile_package_set(visibility)) == "XTSE0020"


def test_using_package_owns_its_unnamed_mode() -> None:
    base = _package(
        BASE,
        '<xsl:mode/><xsl:template match="doc:a"><base/></xsl:template>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>'
        '<xsl:template match="doc:b"><using/></xsl:template>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert compiled.mode(None).rule_layers[0].owner == USING
    assert all(layer.owner == USING for layer in compiled.mode(None).rule_layers)


# ---------------------------------------------------------------------------
# Local constructs: keys, outputs, decimal formats, character maps


def test_keys_are_local_to_their_package() -> None:
    first = _package(
        BASE,
        '<xsl:key name="app:k" match="doc:a" use="@id"/>',
        package_version="1.0.0",
    )
    second = _package(
        OTHER,
        '<xsl:key name="app:k" match="doc:b" use="@id"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>'
        '<xsl:use-package name="urn:example:other"/>'
        '<xsl:key name="app:k" match="doc:c" use="@id"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(first, second, using)
    key = compiled.key("app:k")
    assert key is not None
    assert key.owner == USING
    assert key.declarations[0].attribute("match") == "doc:c"
    assert len(compiled.keys) == 1


def test_outputs_decimal_formats_and_character_maps_are_package_local() -> None:
    first = _package(
        BASE,
        '<xsl:output method="xml" indent="yes"/>'
        '<xsl:decimal-format name="app:d" decimal-separator=","/>'
        '<xsl:character-map name="app:c" use-character-maps=""/>',
        package_version="1.0.0",
    )
    second = _package(
        OTHER,
        '<xsl:output method="text"/>'
        '<xsl:decimal-format name="app:d" decimal-separator="."/>'
        '<xsl:character-map name="app:c"><xsl:output-character character="a" string="b"/>'
        "</xsl:character-map>",
        package_version="1.0.0",
    )
    using = _package(USING, '<xsl:use-package name="urn:example:base"/>')
    compiled = compile_package_set(first, second, using, principal=second)
    assert compiled.outputs[0].attribute("method") == "text"
    assert compiled.decimal_format("app:d").attribute("decimal-separator") == "."
    assert compiled.character_map("app:c").name == f"{{{APP_NS}}}c"


def test_conflicting_local_constructs_are_errors() -> None:
    output = _package(
        BASE,
        '<xsl:output method="xml"/><xsl:output method="text"/>',
    )
    assert _raise_code(lambda: compile_package_set(output)) == "XTSE1560"

    decimal = _package(
        BASE,
        '<xsl:decimal-format name="app:d" decimal-separator=","/>'
        '<xsl:decimal-format name="app:d" decimal-separator="."/>',
    )
    assert _raise_code(lambda: compile_package_set(decimal)) == "XTSE1290"

    character_map = _package(
        BASE,
        '<xsl:character-map name="app:c"/><xsl:character-map name="app:c"/>',
    )
    assert _raise_code(lambda: compile_package_set(character_map)) == "XTSE1580"


# ---------------------------------------------------------------------------
# Duplicates, precedence, and forward references


def test_duplicate_component_declarations_at_same_precedence_are_errors() -> None:
    cases = {
        "XTSE0660": '<xsl:template name="app:x"/>',
        "XTSE0770": '<xsl:function name="app:x"/>',
        "XTSE0630": '<xsl:variable name="app:x" select="1"/>',
        "XTSE3350": '<xsl:accumulator name="app:x" initial-value="0"/>',
    }
    for code, declaration in cases.items():
        package = _package(BASE, declaration + declaration)
        assert _raise_code(lambda package=package: compile_package_set(package)) == code


def test_import_precedence_still_overrides_within_a_package(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "imported.xsl",
        _stylesheet('<xsl:template name="app:x" visibility="public"><imported/></xsl:template>'),
    )
    principal = _package(
        BASE,
        f'<xsl:import href="{imported.name}"/>'
        '<xsl:template name="app:x" visibility="public"><principal/></xsl:template>',
    )
    path = _write(tmp_path, "principal.xsl", principal)
    compiled = compile_package_set(path)
    declaration = compiled.named_template("app:x").declaration
    assert declaration.children[0].local_name == "principal"


def test_attribute_sets_and_keys_combine_within_a_package() -> None:
    package = _package(
        BASE,
        '<xsl:attribute-set name="app:a"><xsl:attribute name="first" select="1"/></xsl:attribute-set>'
        '<xsl:attribute-set name="app:a"><xsl:attribute name="second" select="2"/></xsl:attribute-set>'
        '<xsl:key name="app:k" match="doc:a" use="@id"/>'
        '<xsl:key name="app:k" match="doc:b" use="@id"/>',
    )
    compiled = compile_package_set(package)
    attribute_set = compiled.attribute_set("app:a")
    assert attribute_set is not None
    assert len(attribute_set.declarations) == 2
    assert len(compiled.key("app:k").declarations) == 2


def test_forward_references_bind_after_the_complete_graph_is_built() -> None:
    package = _package(
        BASE,
        '<xsl:template name="app:first" visibility="public">'
        '<xsl:call-template name="app:later"/>'
        '<copy xsl:use-attribute-sets="app:later-attrs"/>'
        "</xsl:template>"
        '<xsl:attribute-set name="app:later-attrs"/>'
        '<xsl:template name="app:later" visibility="public"/>',
    )
    compiled = compile_package_set(package)
    first = compiled.named_template("app:first")
    assert first.body.children[0].reference is not None
    assert first.body.children[0].reference.name == f"{{{APP_NS}}}later"
    assert first.body.children[1].references[0].name == f"{{{APP_NS}}}later-attrs"


def test_missing_component_references_are_errors() -> None:
    missing_template = _package(
        BASE,
        '<xsl:template name="app:t"><xsl:call-template name="app:missing"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(missing_template)) == "XTSE0650"

    missing_attribute_set = _package(
        BASE,
        '<xsl:template name="app:t"><copy xsl:use-attribute-sets="app:missing"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(missing_attribute_set)) == "XTSE0710"


# ---------------------------------------------------------------------------
# Instructions, package manifests, and graph boundaries


def test_instruction_nodes_are_immutable_and_carry_provenance() -> None:
    package = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public" as="item()*">'
        '<literal><xsl:value-of select="1"/><xsl:text>text</xsl:text></literal>'
        "</xsl:template>",
    )
    compiled = compile_package_set(package)
    body = compiled.named_template("app:t").body
    literal = body.children[0]
    assert literal.literal is True
    assert literal.local_name == "literal"
    assert literal.owner == BASE
    assert literal.children[0].is_xslt is True
    assert literal.children[0].attributes[0].local_name == "select"
    text = literal.children[1].children[0]
    assert text.value == "text"
    assert text.owner == BASE
    with pytest.raises(FrozenInstanceError):
        literal.children = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        body.owner = USING  # type: ignore[misc]

    declaration = compiled.named_template("app:t").declaration
    assert declaration.attribute("visibility") == "public"
    assert declaration.location is not None


def test_next_match_and_apply_imports_remain_distinct_instructions() -> None:
    package = _package(
        BASE,
        '<xsl:template match="doc:a" mode="app:m">'
        "<xsl:next-match/><xsl:apply-imports/>"
        "</xsl:template>"
        '<xsl:mode name="app:m"/>',
    )
    compiled = compile_package_set(package)
    body = compiled.mode("app:m").rule_layers[0].rules[0].body
    assert [child.local_name for child in body.children] == ["next-match", "apply-imports"]


def test_use_package_through_include_is_allowed_but_import_is_not(tmp_path: Path) -> None:
    base_path = _write(
        tmp_path,
        "base.xsl",
        _package(BASE, '<xsl:template name="app:b" visibility="public"/>', package_version="1.0.0"),
    )
    included = _write(
        tmp_path,
        "included.xsl",
        _stylesheet('<xsl:use-package name="urn:example:base"/>'),
    )
    principal = _package(USING, f'<xsl:include href="{included.name}"/>', package_version="1.0.0")
    principal_path = _write(tmp_path, "principal.xsl", principal)
    compiled = compile_package_set(base_path, principal_path)
    assert compiled.named_template("app:b") is not None

    imported = _write(
        tmp_path,
        "imported.xsl",
        _stylesheet('<xsl:use-package name="urn:example:base"/>'),
    )
    bad_principal = _package(
        USING, f'<xsl:import href="{imported.name}"/>', package_version="1.0.0"
    )
    bad_path = _write(tmp_path, "bad-principal.xsl", bad_principal)
    assert _raise_code(lambda: compile_package_set(base_path, bad_path)) == "XTSE3008"


def test_package_manifest_validation() -> None:
    missing_name = _package(
        BASE,
        '<xsl:use-package package-version="1.0"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(missing_name)) == "XTSE0010"

    misplaced = _package(
        BASE,
        '<xsl:template name="app:t"><xsl:expose component="template" names="app:t"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(misplaced)) == "XTSE0010"

    bad_visibility = _package(BASE, '<xsl:template name="app:t" visibility="hidden"/>')
    assert _raise_code(lambda: compile_package_set(bad_visibility)) == "XTSE0020"

    no_name_no_match = _package(BASE, "<xsl:template/>")
    assert _raise_code(lambda: compile_package_set(no_name_no_match)) == "XTSE0500"

    mode_attrs = _package(BASE, '<xsl:template name="app:t" mode="app:m"/>')
    assert _raise_code(lambda: compile_package_set(mode_attrs)) == "XTSE0500"


def test_inspection_does_not_change_package_acceptance_or_errors(tmp_path: Path) -> None:
    source = _write(tmp_path, "package.xsl", USING_OVERRIDE)
    before = compile_package_set(BASE_PUBLIC, source)
    inspect_stylesheet(source)
    after = compile_package_set(BASE_PUBLIC, source)
    assert (
        before.named_template("app:start").body.owner
        == after.named_template("app:start").body.owner
    )

    bad: list[str] = []

    def capture() -> None:
        try:
            compile_package_set(BASE_PRIVATE_SOURCE, USING_OVERRIDE)
        except XsltStaticError as exc:
            bad.append(exc.code or "")

    capture()
    inspect_stylesheet(_write(tmp_path, "private.xsl", BASE_PRIVATE_SOURCE))
    capture()
    assert bad == ["XTSE3060", "XTSE3060"]


BASE_PRIVATE_SOURCE = _package(
    BASE,
    '<xsl:template name="app:start" visibility="private"/>',
    package_version="1.0.0",
)


# ---------------------------------------------------------------------------
# Resolver API and unresolvable package names


def test_package_resolver_only_resolves_explicitly_registered_packages() -> None:
    resolver = PackageResolver.from_sources(BASE_PUBLIC)
    assert len(resolver) == 1
    descriptor = resolver.resolve("urn:example:base", "1.0.0")
    assert descriptor.name == BASE
    with pytest.raises(XsltStaticError) as caught:
        resolver.resolve("https://example.invalid/unregistered", "*")
    assert caught.value.code == "XTSE3000"
    assert not resolver.can_resolve("https://example.invalid/unregistered", "*")


def test_component_and_mode_lookup_accept_eqnames() -> None:
    compiled = compile_package_set(BASE_PUBLIC)
    assert compiled.named_template(f"Q{{{APP_NS}}}start") is not None
    assert compiled.mode(f"Q{{{APP_NS}}}process") is not None
    assert compiled.component("template", f"{{{APP_NS}}}start") is not None
    with pytest.raises(XsltStaticError) as caught:
        compiled.named_template("missing:start")
    assert caught.value.code == "XTSE0280"


def test_compiled_package_is_immutable() -> None:
    compiled = compile_package_set(BASE_PUBLIC)
    with pytest.raises(FrozenInstanceError):
        compiled.name = USING  # type: ignore[misc]
    component: Component = compiled.named_template("app:start")
    with pytest.raises(FrozenInstanceError):
        component.visibility = "final"  # type: ignore[misc]
    mode: Mode = compiled.mode("app:process")
    with pytest.raises(FrozenInstanceError):
        mode.visibility = "final"  # type: ignore[misc]


def test_provenance_includes_source_location_and_module() -> None:
    compiled = compile_package_set(BASE_PUBLIC)
    component = compiled.named_template("app:start")
    assert isinstance(component.location, IssueLocation)
    assert component.body.location is not None
    assert component.precedence == ()


# ---------------------------------------------------------------------------
# Constructor validation and low-level value coverage


def test_attribute_and_reference_helpers() -> None:
    from pyxsd.xslt.compiler.instructions import Attribute, ComponentReference, TextNode

    assert Attribute("{urn:x}a", "v", static=True).local_name == "a"
    assert Attribute("{urn:x}a", "v").namespace_uri == "urn:x"
    assert Attribute("plain", "v").namespace_uri is None
    reference = ComponentReference("template", "f", owner=BASE, visibility="public")
    assert reference.kind == "template"
    assert reference.owner == BASE
    assert reference.original is False
    assert TextNode("x").kind.value == "text"


def test_instruction_node_helpers() -> None:
    from pyxsd.xslt.compiler.instructions import (
        Attribute,
        ComponentReference,
        Instruction,
        SequenceConstructor,
        TextNode,
    )

    text = TextNode("value")
    reference = ComponentReference("template", "{urn:x}t")
    node = Instruction(
        "{urn:x}literal",
        attributes=(Attribute("select", "1"), Attribute("{urn:x}a", "2")),
        children=(text,),
        namespaces={"x": "urn:x"},
        reference=reference,
        references=(reference,),
    )
    assert node.local_name == "literal"
    assert node.namespace_uri == "urn:x"
    assert node.is_xslt is False
    assert node.attribute("x:a") == "2"
    assert node.attribute("{urn:x}a") == "2"
    assert node.attribute("missing:a") is None
    assert node.attribute("missing") is None
    assert node.attribute_node("select") is not None
    assert node.elements == ()
    assert node.with_reference(None).reference is None
    assert node.with_references(()).references == ()
    assert node.with_children((node,)).children == (node,)

    xslt = Instruction(f"{{{XSLT_NS}}}value-of", children=(node,))
    assert xslt.is_xslt is True
    assert xslt.elements == (node,)

    constructor = SequenceConstructor((text, node), owner=USING)
    assert constructor.elements == (node,)
    assert list(constructor) == [text, node]
    assert len(constructor) == 2
    assert constructor.with_children((text,)).children == (text,)
    assert SequenceConstructor().children == ()


def test_mode_and_component_values() -> None:
    from pyxsd.xslt.compiler.packages import (
        ContextItemSignature,
        ModeRule,
        ModeRuleLayer,
        PackageConstruct,
        ParameterSignature,
        Signature,
        UsedPackage,
    )

    compiled = compile_package_set(BASE_PUBLIC)
    context = compiled.named_template("app:start").body
    declaration = compiled.named_template("app:start").declaration
    sequence_type = compiled.variable("app:greeting").signature.sequence_type
    assert sequence_type is not None

    parameter = ParameterSignature("p", sequence_type)
    assert parameter.name == "p"
    context_item = ContextItemSignature("optional", sequence_type)
    assert context_item.use == "optional"
    signature = Signature("template", parameters=(parameter,), context_item=context_item)
    assert signature.flags == {}
    assert Signature("variable", variable_kind="param").variable_kind == "param"

    component = Component("template", context, declaration, name="t")
    assert component.hidden is False
    assert component.abstract is False
    rule = ModeRule("x", context, declaration)
    assert ModeRuleLayer(USING, USING, (rule,)).rules == (rule,)
    mode = Mode(
        "m",
        "private",
        USING,
        USING,
        rule_layers=(ModeRuleLayer(USING, USING, (rule,)),),
    )
    assert mode.rules == (rule,)
    construct = PackageConstruct("output", None, {"method": "xml"}, (), USING)
    assert construct.attribute("method") == "xml"
    assert construct.attribute("missing") is None
    assert UsedPackage("p", "1", "*").version == "1"


def test_compiled_package_introspection_paths() -> None:
    from pyxsd.xslt.compiler.packages import PackageConstruct

    compiled = compile_package_set(BASE_PUBLIC)
    assert compiled.accumulator("app:none") is None
    assert compiled.output("app:none") is None
    assert compiled.output(None) is None
    assert compiled.decimal_format("app:none") is None
    assert compiled.decimal_format(None) is None
    assert compiled.character_map("app:none") is None
    assert compiled.key("app:none") is None
    assert compiled.mode("#unnamed") is None
    assert PackageConstruct("output", None, {}, (), USING)


def test_package_version_dunder_and_range_edges() -> None:
    version = PackageVersion.parse("1.0")
    assert str(version) == "1.0"
    for other in (1, "1", None):
        assert version.__lt__(other) is NotImplemented
        assert version.__le__(other) is NotImplemented
        assert version.__gt__(other) is NotImplemented
        assert version.__ge__(other) is NotImplemented
        assert version.__eq__(other) is NotImplemented
    with pytest.raises(XsltStaticError):
        PackageVersion.parse("")
    with pytest.raises(XsltStaticError):
        PackageVersion.parse("Q{not-a-version}")

    base = _versions("1.0.0", "2.0.0")
    for selector in ("1.0,,2.0", "2.0.0 to 1.0.0", "to bad-version"):
        assert (
            _raise_code(
                lambda selector=selector: compile_package_set(*base, _using_version(selector))
            )
            == "XTSE3000"
        )


def test_loaders_and_resolver_api_paths(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.packages import _load_sources

    effective = _effective(tmp_path, "one.xsl", BASE_PUBLIC)
    compiled = compile_package_set(effective)
    assert compiled.name == BASE

    source = ModuleLoader().load(_write(tmp_path, "two.xsl", BASE_PUBLIC))
    compiled = compile_package_set(source)
    assert compiled.name == BASE

    compiled = compile_package_set(BASE_PUBLIC.encode("utf-8"))
    assert compiled.name == BASE

    resolver = PackageResolver.from_sources(effective)
    descriptor = resolver.descriptors[0]
    assert list(resolver) == [descriptor]
    assert resolver.compile(descriptor).name == BASE
    assert resolver.can_resolve(BASE, "1") is True
    with pytest.raises(XsltStaticError) as caught:
        resolver.resolve("")
    assert caught.value.code == "XTSE0010"
    with pytest.raises(XsltStaticError) as caught:
        resolver.resolve(BASE, "2.0")
    assert caught.value.code == "XTSE3000"
    with pytest.raises(ValueError):
        resolver.register(effective)

    assert _load_sources((), static_parameters=None) == ()
    with pytest.raises(ValueError):
        compile_package_set()

    extra = _write(tmp_path, "extra.xsl", BASE_PUBLIC)
    compiled = compile_package_set(extra, principal=extra)
    assert compiled.name == BASE


def test_normalize_sequence_view(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.instructions import normalize_sequence

    effective = _effective(
        tmp_path,
        "sequence.xsl",
        _package(
            BASE,
            '<xsl:template name="app:t" visibility="public"><xsl:text>hello</xsl:text></xsl:template>',
        ),
    )
    template = effective.find("app:t")
    constructor = normalize_sequence(template, owner=BASE, module=effective.identity)
    assert constructor.owner == BASE
    assert constructor.children[0].local_name == "text"
    assert constructor.children[0].children[0].value == "hello"
    assert constructor.elements[0].local_name == "text"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('<xsl:template name=" "/>', "XTSE0010"),
        ('<xsl:template name="app:t" visibility="hidden"/>', "XTSE0020"),
        ('<xsl:template name="app:t" priority="1"><xsl:value-of/></xsl:template>', "XTSE0500"),
        ('<xsl:template match="doc:a" visibility="public"/>', "XTSE0500"),
        ("<xsl:template/>", "XTSE0500"),
        ("<xsl:function/>", "XTSE0010"),
        ('<xsl:function name="f"/>', "XTSE0740"),
        ("<xsl:variable/>", "XTSE0010"),
        ("<xsl:attribute-set/>", "XTSE0010"),
        ("<xsl:accumulator/>", "XTSE0010"),
        ("<xsl:key/>", "XTSE0010"),
        ('<xsl:key name="app:k" visibility="public"/>', "XTSE0020"),
        ('<xsl:template match="doc:a" priority="not-a-number"/>', "XTSE0530"),
        ('<xsl:template match="doc:a" priority="NaN"/>', "XTSE0530"),
        ('<xsl:template match="doc:a"><xsl:unknown/></xsl:template>', None),
    ],
)
def test_declaration_error_paths(body: str, code: str | None) -> None:
    package = _package(BASE, body)
    if code is None:
        compiled = compile_package_set(package)
        assert compiled.mode(None) is not None
    else:
        assert _raise_code(lambda: compile_package_set(package)) == code


def test_named_template_with_match_declares_both_component_and_rule() -> None:
    package = _package(
        BASE,
        '<xsl:mode name="app:m"/>'
        '<xsl:template name="app:t" match="doc:a" mode="app:m" priority="2">'
        "<out/></xsl:template>",
    )
    compiled = compile_package_set(package)
    assert compiled.named_template("app:t") is not None
    rule = compiled.mode("app:m").rule_layers[0].rules[0]
    assert rule.pattern == "doc:a"
    assert rule.priority == 2
    assert rule.name == f"{{{APP_NS}}}t"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('<xsl:use-package name="urn:example:base"><unknown/></xsl:use-package>', "XTSE0010"),
        (
            '<xsl:use-package name="urn:example:base">text</xsl:use-package>',
            "XTSE0010",
        ),
        (
            '<xsl:use-package name="urn:example:base"><xsl:override>text</xsl:override>'
            "</xsl:use-package>",
            "XTSE0010",
        ),
        (
            '<xsl:use-package name="urn:example:base"><xsl:override>'
            '<xsl:key name="app:k" match="*" use="."/>'
            "</xsl:override></xsl:use-package>",
            "XTSE0010",
        ),
    ],
)
def test_use_package_content_errors(body: str, code: str) -> None:
    package = _package(USING, body)
    assert _raise_code(lambda: compile_package_set(package)) == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('<xsl:template match="doc:a" mode="#current"/>', "XTSE0550"),
        ('<xsl:mode name="app:m"/><xsl:template match="doc:a" mode="app:m app:m"/>', "XTSE0550"),
        ('<xsl:template match="doc:a" mode="#all"/>', None),
    ],
)
def test_mode_token_errors(body: str, code: str | None) -> None:
    package = _package(BASE, body)
    if code is None:
        assert compile_package_set(package).mode(None) is not None
    else:
        assert _raise_code(lambda: compile_package_set(package)) == code


def test_override_mode_token_errors() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )

    def override(mode: str, extra: str = "") -> str:
        return _package(
            USING,
            f'<xsl:use-package name="urn:example:base"><xsl:override {extra}>'
            f'<xsl:template match="doc:a" mode="{mode}"/></xsl:override></xsl:use-package>',
            package_version="1.0.0",
        )

    assert _raise_code(lambda: compile_package_set(base, override("#current"))) == "XTSE0550"
    assert _raise_code(lambda: compile_package_set(base, override("#unnamed"))) == "XTSE3440"
    assert _raise_code(lambda: compile_package_set(base, override("#all"))) == "XTSE3440"
    assert _raise_code(lambda: compile_package_set(base, override(""))) == "XTSE3440"
    unnamed = _package(BASE, "<xsl:mode/>", package_version="1.0.0")
    assert (
        _raise_code(
            lambda: compile_package_set(
                unnamed,
                override("#default"),
            )
        )
        == "XTSE3440"
    )
    assert (
        _raise_code(
            lambda: compile_package_set(
                base,
                override("#default", 'default-mode="#default"'),
            )
        )
        == "XTSE0020"
    )


def test_parameter_and_context_item_errors() -> None:
    no_name = _package(
        BASE,
        '<xsl:function name="app:f"><xsl:param/></xsl:function>',
    )
    assert _raise_code(lambda: compile_package_set(no_name)) == "XTSE0010"

    duplicate = _package(
        BASE,
        '<xsl:function name="app:f"><xsl:param name="p"/><xsl:param name="p"/></xsl:function>',
    )
    assert _raise_code(lambda: compile_package_set(duplicate)) == "XTSE0580"

    bad_required = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:param name="p" required="maybe"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(bad_required)) == "XTSE0020"

    bad_tunnel = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:param name="p" tunnel="maybe"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(bad_tunnel)) == "XTSE0020"

    bad_use = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:context-item use="sometimes"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(bad_use)) == "XTSE0020"

    absent_as = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public">'
        '<xsl:context-item use="absent" as="item()"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(absent_as)) == "XTSE3088"


def test_declared_modes_and_package_names() -> None:
    relative = _package("relative-name", "")
    assert _raise_code(lambda: compile_package_set(relative)) == "XTSE0020"

    bad_version = _package(BASE, "", package_version="not-valid")
    assert _raise_code(lambda: compile_package_set(bad_version)) == "XTSE0020"

    bad_modes = _package(BASE, "", attributes=' declared-modes="maybe"')
    assert _raise_code(lambda: compile_package_set(bad_modes)) == "XTSE0020"

    implicit = _stylesheet("", attributes=' xml:space="preserve"')
    # The implicit stylesheet root does not accept declared-modes; it is just an
    # ordinary attribute and must not be consulted.
    assert compile_package_set(implicit).name is None


def test_expose_selector_syntax_edges() -> None:
    cases = {
        'component="nope" names="*"': "XTSE0020",
        'component="template"': "XTSE0010",
        'component="template" names="Q{urn:x"': "XTSE3020",
        'component="function" names="app:f#x"': "XTSE3020",
        'component="function" names="app:f#-1"': "XTSE3020",
        'component="template" names="#unnamed"': "XTSE0020",
        'component="template" names="*:t"': None,
        'component="template" names="Q{urn:example:app}*"': None,
        'component="template" names="Q{urn:example:app}t"': None,
    }
    for selector, code in cases.items():
        package = _package(
            BASE,
            f'<xsl:template name="app:t"/><xsl:expose {selector} visibility="public"/>',
        )
        if code is None:
            compile_package_set(package)
        else:
            assert _raise_code(lambda package=package: compile_package_set(package)) == code


def test_accept_and_expose_selector_edges() -> None:
    base = _package(
        BASE,
        """
        <xsl:mode name="app:m" visibility="public"/>
        <xsl:template name="app:t" visibility="public"/>
        <xsl:template name="app:u" visibility="public"/>
        """,
    )
    invalid_mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="public"/>'
        "</xsl:use-package>",
    )
    assert compile_package_set(base, invalid_mode).mode("app:m").visibility == "public"

    reduce_mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="private"/>'
        "</xsl:use-package>",
    )
    assert compile_package_set(base, reduce_mode).mode("app:m").visibility == "private"

    hidden_mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="hidden"/>'
        "</xsl:use-package>",
    )
    assert compile_package_set(base, hidden_mode).mode("app:m") is None

    incompatible_mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="absent"/>'
        "</xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, incompatible_mode)) == "XTSE3040"

    unknown_mode = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:missing" visibility="private"/>'
        "</xsl:use-package>",
    )
    assert _raise_code(lambda: compile_package_set(base, unknown_mode)) == "XTSE3030"

    wildcard_fallback = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:*" visibility="absent"/>'
        "</xsl:use-package>",
    )
    assert compile_package_set(base, wildcard_fallback).mode("app:m").visibility == "public"


def test_accept_override_conflict_and_local_accept_conflict() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public"/>',
        package_version="1.0.0",
    )
    both = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:t" visibility="private"/>'
        '<xsl:override><xsl:template name="app:t" visibility="public"/></xsl:override>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, both)) == "XTSE3051"

    local = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>'
        '<xsl:template name="app:t" visibility="public"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, local)) == "XTSE3050"

    hidden_reference = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:t" visibility="hidden"/>'
        "</xsl:use-package>"
        '<xsl:template name="app:main" visibility="public">'
        '<xsl:call-template name="app:t"/></xsl:template>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, hidden_reference)) == "XTSE0650"


def test_absent_reference_is_rejected() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:t" visibility="abstract"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:t" visibility="absent"/>'
        "</xsl:use-package>"
        '<xsl:template name="app:main" visibility="public">'
        '<xsl:call-template name="app:t"/></xsl:template>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE0650"


def test_two_used_packages_with_same_mode_conflict() -> None:
    first = _package(BASE, '<xsl:mode name="app:m" visibility="public"/>', package_version="1.0.0")
    second = _package(
        OTHER, '<xsl:mode name="app:m" visibility="public"/>', package_version="1.0.0"
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/><xsl:use-package name="urn:example:other"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(first, second, using)) == "XTSE3050"


def test_override_attribute_set_original_binding() -> None:
    base = _package(
        BASE,
        '<xsl:attribute-set name="app:a" visibility="public">'
        '<xsl:attribute name="base" select="1"/></xsl:attribute-set>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:attribute-set name="app:a" visibility="public" use-attribute-sets="xsl:original">'
        '<xsl:attribute name="using" select="2"/></xsl:attribute-set>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    component = compiled.attribute_set("app:a")
    assert component is not None
    reference = component.declaration.references[0]
    assert reference.original is True
    assert reference.kind == "attribute-set"


def test_original_scanner_handles_strings_comments_and_eqnames() -> None:
    base = _package(
        BASE,
        '<xsl:variable name="app:v" select="1" visibility="public"/>',
        package_version="1.0.0",
    )

    def override(select: str) -> str:
        return _package(
            USING,
            '<xsl:use-package name="urn:example:base"><xsl:override>'
            f'<xsl:variable name="app:v" visibility="public" select="{select}"/>'
            "</xsl:override></xsl:use-package>",
            package_version="1.0.0",
        )

    compiled = compile_package_set(
        base,
        override(
            "&quot;xsl:original&quot;, (: xsl:original :) "
            "$Q{http://www.w3.org/1999/XSL/Transform}original"
        ),
    )
    variable = compiled.variable("app:v")
    assert variable is not None
    # The string literal is ignored; the EQName reference is bound.
    assert len(variable.declaration.references) == 1

    for selector in (
        "$Q{http://www.w3.org/1999/XSL/Transform}original",
        "Q{http://www.w3.org/1999/XSL/Transform}original(1)",
    ):
        package = _package(BASE, f'<xsl:variable name="app:x" select="{selector}"/>')
        assert _raise_code(lambda package=package: compile_package_set(package)) in (
            "XPST0008",
            "XPST0017",
        )

    from pyxsd.xslt.compiler.packages import _scan_original_references

    namespaces = {"xsl": XSLT_NS}
    assert _scan_original_references("$Q{unterminated", namespaces) == []
    assert _scan_original_references("$-bad", namespaces) == []
    assert _scan_original_references("$", namespaces) == []
    assert _scan_original_references("$xsl:original", namespaces)
    assert _scan_original_references("(: nested (: x :) $xsl:original :)", namespaces) == []
    assert _scan_original_references("'$xsl:original'", namespaces) == []
    assert _scan_original_references("'it''s $xsl:original'", namespaces) == []
    assert _scan_original_references("$xsl:other", namespaces) == []


def test_transitive_rebinding_of_overridden_references() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:a" visibility="public">'
        '<xsl:call-template name="app:b"/></xsl:template>'
        '<xsl:template name="app:b" visibility="public"><base/></xsl:template>',
        package_version="1.0.0",
    )
    middle = _package(
        OTHER,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:b" visibility="public"><middle/></xsl:template>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    top = _package(
        USING,
        '<xsl:use-package name="urn:example:other"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, middle, top)
    template = compiled.named_template("app:a")
    reference = template.body.children[0].reference
    assert reference is not None
    assert reference.owner == OTHER


def test_transitive_hidden_mode_and_component_branches() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/><xsl:template match="doc:a" mode="app:m"/>',
        package_version="1.0.0",
    )
    middle = _package(
        OTHER,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="hidden"/>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    top = _package(
        USING,
        '<xsl:use-package name="urn:example:other">'
        '<xsl:override><xsl:template match="doc:a" mode="app:m"/></xsl:override>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, middle, top)) == "XTSE3060"


def test_diamond_package_is_cached() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:left" visibility="public"/>'
        '<xsl:template name="app:right" visibility="public"/>',
        package_version="1.0.0",
    )
    left = _package(
        OTHER,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:right" visibility="hidden"/>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    right = _package(
        "urn:example:right",
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="template" names="app:left" visibility="hidden"/>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    top = _package(
        USING,
        '<xsl:use-package name="urn:example:other"/><xsl:use-package name="urn:example:right"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, left, right, top)
    assert len(compiled.used_packages) == 2


def test_two_imports_use_later_import_precedence(tmp_path: Path) -> None:
    first = _write(
        tmp_path,
        "first.xsl",
        _stylesheet('<xsl:template name="app:x" visibility="public"><first/></xsl:template>'),
    )
    second = _write(
        tmp_path,
        "second.xsl",
        _stylesheet('<xsl:template name="app:x" visibility="public"><second/></xsl:template>'),
    )
    principal = _package(
        BASE,
        f'<xsl:import href="{first.name}"/><xsl:import href="{second.name}"/>',
    )
    path = _write(tmp_path, "principal.xsl", principal)
    compiled = compile_package_set(path)
    assert compiled.named_template("app:x").declaration.children[0].local_name == "second"


def test_conflicting_attr_set_visibility_and_mode_properties() -> None:
    attr_sets = _package(
        BASE,
        '<xsl:attribute-set name="app:a" visibility="public"/>'
        '<xsl:attribute-set name="app:a" visibility="private"/>',
    )
    assert _raise_code(lambda: compile_package_set(attr_sets)) == "XTSE0020"

    modes = _package(
        BASE,
        '<xsl:mode name="app:m" on-no-match="deep-copy"/>'
        '<xsl:mode name="app:m" on-no-match="fail"/>',
    )
    assert _raise_code(lambda: compile_package_set(modes)) == "XTSE0545"


def test_expose_all_wildcard_and_accept_mode_visibility_edges() -> None:
    wildcard = _package(
        BASE,
        '<xsl:function name="app:f"/>'
        '<xsl:expose component="function" names="*" visibility="abstract"/>',
    )
    assert _raise_code(lambda: compile_package_set(wildcard)) == "XTSE3025"

    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>'
        '<xsl:function name="app:f" visibility="public"/>',
    )
    mode_wild = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="*" visibility="hidden"/>'
        "</xsl:use-package>",
    )
    compiled = compile_package_set(base, mode_wild)
    assert compiled.mode("app:m") is None


def test_override_mode_not_available_after_hidden_accept() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="hidden"/>'
        '<xsl:override><xsl:template match="doc:a" mode="app:m"/></xsl:override>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE3060"


def test_accumulator_component_and_empty_sequence_type() -> None:
    package = _package(
        BASE,
        '<xsl:accumulator name="app:a" as="empty-sequence()" initial-value="()"/>',
    )
    compiled = compile_package_set(package)
    accumulator = compiled.accumulator("app:a")
    assert accumulator is not None
    assert accumulator.signature.sequence_type.item_type is None


def test_function_signature_flag_incompatibilities() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="public" new-each-time="yes" '
        'identity-sensitive="no" override-extension-functions="no" streamable="yes" '
        'posture="striding" sweep="yes"><xsl:sequence select="1"/></xsl:function>',
        package_version="1.0.0",
    )

    def override(attributes: str) -> str:
        return _package(
            USING,
            '<xsl:use-package name="urn:example:base"><xsl:override>'
            f'<xsl:function name="app:f" visibility="public" {attributes}>'
            '<xsl:sequence select="1"/></xsl:function>'
            "</xsl:override></xsl:use-package>",
            package_version="1.0.0",
        )

    for attributes in (
        'new-each-time="no"',
        'identity-sensitive="yes"',
        'override-extension-functions="yes"',
        'streamable="no"',
        'streamable="yes" posture="sweeping" sweep="yes"',
        'streamable="yes" posture="striding" sweep="no"',
    ):
        assert (
            _raise_code(
                lambda attributes=attributes: compile_package_set(base, override(attributes))
            )
            == "XTSE3070"
        )

    compatible = override(
        'new-each-time="yes" identity-sensitive="no" '
        'override-extension-functions="no" streamable="yes" posture="striding" sweep="yes"'
    )
    assert compile_package_set(base, compatible).function("app:f", 0) is not None


def test_template_signature_more_parameter_mismatches() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:t" as="xs:string" visibility="public">'
        '<xsl:param name="a" as="xs:string"/>'
        '<xsl:param name="b" as="xs:string" tunnel="yes"/>'
        "</xsl:template>",
        package_version="1.0.0",
    )

    def override(parameters: str, result: str = "xs:string") -> str:
        return _package(
            USING,
            '<xsl:use-package name="urn:example:base"><xsl:override>'
            f'<xsl:template name="app:t" as="{result}" visibility="public">'
            f"{parameters}</xsl:template>"
            "</xsl:override></xsl:use-package>",
            package_version="1.0.0",
        )

    assert (
        _raise_code(
            lambda: compile_package_set(base, override('<xsl:param name="a" as="xs:string"/>'))
        )
        == "XTSE3070"
    )
    assert (
        _raise_code(
            lambda: compile_package_set(
                base,
                override(
                    '<xsl:param name="a" as="xs:string"/><xsl:param name="b" as="xs:string"/>'
                ),
            )
        )
        == "XTSE3070"
    )
    assert (
        _raise_code(
            lambda: compile_package_set(
                base,
                override(
                    '<xsl:param name="a" as="xs:string"/>'
                    '<xsl:param name="b" as="xs:string" tunnel="yes"/>',
                    result="xs:integer",
                ),
            )
        )
        == "XTSE3070"
    )


def test_internal_name_helpers_and_output_lookup() -> None:
    from pyxsd.xslt.compiler.packages import _expand_name, _namespace_of

    assert _namespace_of("plain") is None
    assert _namespace_of("{urn:x}plain") == "urn:x"
    with pytest.raises(XsltStaticError):
        _expand_name(" ", {})
    with pytest.raises(XsltStaticError):
        _expand_name("Q{unterminated", {})

    output_package = _package(BASE, '<xsl:output method="xml"/>')
    compiled = compile_package_set(output_package)
    assert compiled.output(None) is not None
    assert compiled.output(None).attribute("method") == "xml"


def test_declared_modes_no_and_principal_outside_sources(tmp_path: Path) -> None:
    base = _package(BASE, '<xsl:template name="app:t" visibility="public"/>')
    explicit_no = _package(
        BASE,
        '<xsl:template match="doc:a"/>',
        attributes=' declared-modes="no"',
    )
    assert compile_package_set(explicit_no).mode(None) is not None

    base_path = _write(tmp_path, "base.xsl", base)
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base_path, principal=using)
    assert compiled.name == USING
    assert compiled.named_template("app:t") is not None


def test_expose_on_unnamed_mode_is_ignored() -> None:
    package = _package(
        BASE,
        '<xsl:mode/><xsl:template match="doc:a"/>'
        '<xsl:expose component="mode" names="*" visibility="public"/>',
    )
    compiled = compile_package_set(package)
    assert compiled.mode(None).visibility == "private"


def test_manifest_accept_override_placement_errors() -> None:
    accept = _package(
        BASE,
        '<xsl:template name="app:t"><xsl:accept component="template" names="*"/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(accept)) == "XTSE0010"

    override = _package(
        BASE,
        '<xsl:template name="app:t"><xsl:override/></xsl:template>',
    )
    assert _raise_code(lambda: compile_package_set(override)) == "XTSE0010"


def test_expose_reached_through_import_is_rejected(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "imported-expose.xsl",
        _stylesheet('<xsl:expose component="template" names="*" visibility="public"/>'),
    )
    principal = _package(BASE, f'<xsl:import href="{imported.name}"/>')
    path = _write(tmp_path, "principal-expose.xsl", principal)
    assert _raise_code(lambda: compile_package_set(path)) == "XTSE0010"


def test_signature_compatibility_flag_matrix() -> None:
    from pyxsd.xslt.compiler.packages import (
        ParameterSignature,
        Signature,
        _signatures_compatible,
        _type_key,
    )

    compiled = compile_package_set(BASE_PUBLIC)
    sequence_type = compiled.variable("app:greeting").signature.sequence_type
    assert sequence_type is not None
    assert _type_key(None) is None
    empty_package = _package(
        BASE,
        '<xsl:accumulator name="app:a" as="empty-sequence()" initial-value="()"/>',
    )
    empty = compile_package_set(empty_package).accumulator("app:a").signature.sequence_type
    assert _type_key(empty) == ("empty", "")

    base = Signature(
        "function",
        return_type=sequence_type,
        parameters=(ParameterSignature("p", sequence_type),),
    )
    # Parameter name/type mismatch.
    other_type = (
        compile_package_set(
            _package(BASE, '<xsl:variable name="app:v" as="xs:integer" select="1"/>')
        )
        .variable("app:v")
        .signature.sequence_type
    )
    assert other_type is not None
    assert not _signatures_compatible(
        base,
        Signature(
            "function",
            return_type=sequence_type,
            parameters=(ParameterSignature("p", other_type),),
        ),
    )
    # Each restrictive flag is checked independently.
    for base_flags, override_flags in (
        ({"identity-sensitive": "no"}, {"identity-sensitive": "yes"}),
        ({"override-extension-functions": "no"}, {"override-extension-functions": "yes"}),
        ({"streamable": "yes"}, {"streamable": "no"}),
        (
            {"streamable": "yes", "posture": "striding", "sweep": "yes"},
            {"streamable": "yes", "posture": "sweeping", "sweep": "yes"},
        ),
        (
            {"streamable": "yes", "posture": "striding", "sweep": "yes"},
            {"streamable": "yes", "posture": "striding", "sweep": "no"},
        ),
    ):
        assert not _signatures_compatible(
            Signature(
                "function",
                return_type=sequence_type,
                parameters=(ParameterSignature("p", sequence_type),),
                flags=base_flags,
            ),
            Signature(
                "function",
                return_type=sequence_type,
                parameters=(ParameterSignature("p", sequence_type),),
                flags=override_flags,
            ),
        )


def test_named_template_and_mode_rule_override_together() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>'
        '<xsl:template name="app:t" match="doc:a" mode="app:m" visibility="public"><base/>'
        "</xsl:template>",
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:t" match="doc:a" mode="app:m" visibility="public">'
        "<using/></xsl:template>"
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert compiled.named_template("app:t").body.owner == USING
    assert compiled.mode("app:m").rule_layers[0].owner == USING


def test_override_unknown_mode_and_private_accept_mode() -> None:
    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )
    unknown = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template match="doc:a" mode="app:unknown"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, unknown)) == "XTSE3060"

    private = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="mode" names="app:m" visibility="private"/>'
        '<xsl:override><xsl:template match="doc:a" mode="app:m"/></xsl:override>'
        "</xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, private)) == "XTSE3060"


def test_attribute_set_original_unavailable_for_abstract_target() -> None:
    base = _package(
        BASE,
        '<xsl:attribute-set name="app:a" visibility="abstract"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:attribute-set name="app:a" visibility="public" use-attribute-sets="xsl:original"/>'
        "</xsl:override></xsl:use-package>"
        '<xsl:template name="app:main" visibility="public"/>',
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE0710"


def test_rebinding_text_private_and_original_references() -> None:
    base = _package(
        BASE,
        '<xsl:template name="app:a" visibility="public">'
        "<xsl:text>text</xsl:text>"
        '<xsl:call-template name="app:private"/>'
        "</xsl:template>"
        '<xsl:template name="app:private" visibility="private"/>'
        '<xsl:template name="app:b" visibility="public"><base/></xsl:template>',
        package_version="1.0.0",
    )
    middle = _package(
        OTHER,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template name="app:b" visibility="public">'
        '<xsl:call-template name="xsl:original"/></xsl:template>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    top = _package(
        USING,
        '<xsl:use-package name="urn:example:other"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, middle, top)
    a = compiled.named_template("app:a")
    assert a.body.children[0].children[0].value == "text"
    assert a.body.children[1].reference.visibility == "private"
    b = compiled.named_template("app:b")
    assert b.body.children[0].reference.original is True


def test_multiple_local_constructs_and_lookup_order() -> None:
    package = _package(
        BASE,
        '<xsl:output name="app:a" method="xml"/>'
        '<xsl:output name="app:b" method="text"/>'
        '<xsl:decimal-format name="app:a" decimal-separator=","/>'
        '<xsl:decimal-format name="app:b" decimal-separator="."/>'
        '<xsl:character-map name="app:a"/>'
        '<xsl:character-map name="app:b"/>',
    )
    compiled = compile_package_set(package)
    assert compiled.output("app:b").attribute("method") == "text"
    assert compiled.decimal_format("app:b").attribute("decimal-separator") == "."
    assert compiled.character_map("app:b").name == f"{{{APP_NS}}}b"


def test_unnamed_explicit_package_and_provided_resolver() -> None:
    unnamed = _package(
        None,
        '<xsl:template name="app:t" visibility="public"/>',
    )
    resolver = PackageResolver()
    compiled = compile_package_set(unnamed, resolver=resolver)
    assert compiled.name is None
    assert compiled.named_template("app:t") is not None
    assert len(resolver) == 1


def test_hidden_then_public_accept_conflict() -> None:
    base = _package(
        BASE,
        '<xsl:function name="app:f" visibility="public"><xsl:sequence select="1"/></xsl:function>',
        package_version="1.0.0",
    )
    other = _package(
        OTHER,
        '<xsl:function name="app:f" visibility="public"><xsl:sequence select="2"/></xsl:function>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base">'
        '<xsl:accept component="function" names="app:f#0" visibility="hidden"/>'
        "</xsl:use-package>"
        '<xsl:use-package name="urn:example:other"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, other, using)
    assert compiled.function("app:f", 0).owner == OTHER


def test_multiple_explicit_expose_rules_last_wins() -> None:
    package = _package(
        BASE,
        '<xsl:template name="app:t"/>'
        '<xsl:expose component="template" names="app:t" visibility="final"/>'
        '<xsl:expose component="template" names="app:t" visibility="private"/>',
    )
    compiled = compile_package_set(package)
    assert compiled.named_template("app:t").visibility == "private"


def test_call_template_without_name_is_ignored_by_binding() -> None:
    package = _package(
        BASE,
        '<xsl:template name="app:t" visibility="public"><xsl:call-template/></xsl:template>',
    )
    compiled = compile_package_set(package)
    assert compiled.named_template("app:t").body.children[0].reference is None


def test_mode_declaration_in_included_module(tmp_path: Path) -> None:
    included = _write(
        tmp_path,
        "included-mode.xsl",
        _stylesheet('<xsl:mode name="app:m" visibility="public"/>'),
    )
    principal = _package(BASE, f'<xsl:include href="{included.name}"/>')
    path = _write(tmp_path, "principal-mode.xsl", principal)
    compiled = compile_package_set(path)
    assert compiled.mode("app:m") is not None


def test_remaining_expose_and_scanner_branches() -> None:
    package = _package(
        BASE,
        '<xsl:template name="app:t"/>'
        '<xsl:expose component="template" names="app:*" visibility="final"/>'
        '<xsl:expose component="template" names="*:t" visibility="private"/>'
        '<xsl:expose component="template" names="*" visibility="final"/>'
        '<xsl:expose component="template" names="*" visibility="private"/>',
    )
    compiled = compile_package_set(package)
    assert compiled.named_template("app:t").visibility == "private"

    from pyxsd.xslt.compiler.packages import _scan_original_references

    namespaces = {"xsl": XSLT_NS}
    assert _scan_original_references("xsl:original", namespaces) == []
    assert _scan_original_references("$xsl:", namespaces) == []


def test_importing_a_package_root_is_rejected(tmp_path: Path) -> None:
    imported = _write(
        tmp_path,
        "imported-package.xsl",
        _package(BASE, '<xsl:template name="app:t" visibility="public"/>'),
    )
    principal = _package(BASE, f'<xsl:import href="{imported.name}"/>')
    path = _write(tmp_path, "principal-import-package.xsl", principal)
    assert _raise_code(lambda: compile_package_set(path)) == "XTSE0165"


def test_dependency_packages_retain_local_constructs() -> None:
    base = _package(
        BASE,
        '<xsl:key name="app:k" match="doc:a" use="@id"/><xsl:output method="xml"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"/>',
        package_version="1.0.0",
    )
    compiled = compile_package_set(base, using)
    assert len(compiled.dependencies) == 1
    dependency = compiled.dependencies[0]
    assert dependency.name == BASE
    assert dependency.key("app:k") is not None
    assert dependency.output(None).attribute("method") == "xml"


def test_package_manifest_rejects_nested_stylesheet_and_rule_visibility() -> None:
    nested = _package(BASE, "<xsl:stylesheet/>")
    assert _raise_code(lambda: compile_package_set(nested)) == "XTSE0010"

    base = _package(
        BASE,
        '<xsl:mode name="app:m" visibility="public"/>',
        package_version="1.0.0",
    )
    using = _package(
        USING,
        '<xsl:use-package name="urn:example:base"><xsl:override>'
        '<xsl:template match="doc:a" mode="app:m" visibility="public"/>'
        "</xsl:override></xsl:use-package>",
        package_version="1.0.0",
    )
    assert _raise_code(lambda: compile_package_set(base, using)) == "XTSE0500"
