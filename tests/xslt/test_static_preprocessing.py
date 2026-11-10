"""Normative XSLT static preprocessing.

Raw stylesheet trees stay source-faithful; the effective module applies
``use-when``, static parameters/variables, shadow attributes, simplified-
stylesheet normalization, and the separate stylesheet whitespace rules.
Literal result namespace/base-URI context is retained, and a shadow attribute
never survives as an ordinary AVT.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pyxsd.resource_policy import ResourceData, ResourcePolicyError
from pyxsd.xdm import XdmAtomic, XdmMap
from pyxsd.xdm.nodes import NodeKind
from pyxsd.xslt.compiler.modules import ModuleLoader
from pyxsd.xslt.compiler.static import StaticPreprocessor
from pyxsd.xslt.errors import XsltError, XsltStaticError

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
DOC_NS = "urn:example:doc"
FIXTURES = Path(__file__).parent / "fixtures"


def _stylesheet(body: str = "", *, attributes: str = "") -> str:
    return (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{DOC_NS}"{attributes}>{body}</xsl:stylesheet>'
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _effective(
    tmp_path: Path,
    xml: str,
    parameters: dict[str, Any] | None = None,
    *,
    name: str = "module.xsl",
    policy: Any = None,
):
    path = _write(tmp_path, name, xml)
    module = ModuleLoader(policy).load(path)
    return StaticPreprocessor().apply(module, parameters)


class _MappingPolicy:
    def __init__(self, modules: dict[str, str]) -> None:
        self.modules = modules

    def read(self, source: str, *, base: str | None, purpose: str) -> ResourceData:
        try:
            return ResourceData(source, self.modules[source].encode("utf-8"))
        except KeyError:
            raise ResourcePolicyError(f"denied {source}") from None

    def parse(self, data: ResourceData, context: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("the loader parses XDM itself")


def _texts(node: Any) -> list[str]:
    return [child.value for child in node.children if child.kind is NodeKind.TEXT]


def test_static_exclusion_and_shadow_attributes(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="production" static="yes" select="false()"/>'
        '<xsl:template name="debug-template" use-when="not($production)">'
        "<xsl:unknown-instruction/></xsl:template>"
        '<xsl:template name="production-template" use-when="$production" '
        '_priority="{1 + 1}"><out/></xsl:template>'
    )
    path = _write(tmp_path, "static.xsl", xml)
    module = ModuleLoader(None).load(path)

    effective = StaticPreprocessor().apply(module, {"production": True})

    assert effective.find("debug-template") is None
    assert effective.find("production-template") is not None
    assert effective.attribute("version") == "3.0"
    assert effective.source_location("production-template") is not None
    assert effective.find("production-template").attribute("priority") == "2"
    assert effective.find("production-template").attribute_node("priority").static is True
    assert effective.static_variables["production"] == (
        effective.static_variables["production"][0],
    )
    assert bool(effective.static_variables["production"][0].value) is True


def test_shadow_attributes_only_apply_to_xslt_elements(tmp_path: Path) -> None:
    xml = (
        f'<xsl:stylesheet xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}" '
        f'version="1.0" _version="{{\'3.0\'}}">'
        '<xsl:template name="t" _priority="{1 + 2}" priority="not-a-number">'
        '<out _tag="x"/></xsl:template></xsl:stylesheet>'
    )

    effective = _effective(tmp_path, xml)
    template = effective.find("t")
    literal = template.children[0]

    assert effective.attribute("version") == "3.0"
    assert effective.attribute_node("version").static is True
    assert effective.attribute("_version") is None
    assert template.attribute("priority") == "3"
    assert template.attribute("_priority") is None
    assert literal.name == "out"
    assert literal.attribute("_tag") == "x"
    assert literal.attribute("tag") is None


def test_shadow_attribute_is_not_later_an_ordinary_avt(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t" _select="{\'true()\'}"/>')

    effective = _effective(tmp_path, xml)
    template = effective.find("t")

    assert template.attribute("select") == "true()"
    assert template.attribute_node("select").static is True
    assert template.attribute("_select") is None


def test_shadow_attribute_value_template_escapes_and_errors(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t" _priority="{{literal}}"/>')
    assert _effective(tmp_path, xml).find("t").attribute("priority") == "{literal}"

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, _stylesheet('<xsl:template name="t" _priority="{" />'))
    assert caught.value.code == "XTSE0350"

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, _stylesheet('<xsl:template name="t" _priority="}"/>'))
    assert caught.value.code == "XTSE0370"


def test_use_when_excludes_invalid_content(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="skip" use-when="false()">'
        "<xsl:not-a-real-instruction/><xsl:template/></xsl:template>"
        '<xsl:template name="keep"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("skip") is None
    assert effective.find("keep") is not None


def test_root_use_when_drops_children_but_keeps_root_attributes(tmp_path: Path) -> None:
    xml = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xmlns:doc="{DOC_NS}" use-when="false()">'
        '<xsl:template name="hidden"/></xsl:stylesheet>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.root.name == f"{{{XSLT_NS}}}stylesheet"
    assert effective.attribute("version") == "3.0"
    assert effective.find("hidden") is None


def test_use_when_on_literal_result_element(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t">'
        '<out xsl:use-when="false()"/>'
        '<keep xsl:use-when="true()"/>'
        "</xsl:template>"
    )

    template = _effective(tmp_path, xml).find("t")

    assert [child.name for child in template.children if child.kind is NodeKind.ELEMENT] == ["keep"]


def test_conditional_include_skips_missing_resource(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:include href="missing.xsl" use-when="false()"/>')

    effective = _effective(tmp_path, xml)

    assert effective.includes == ()


def test_conditional_include_loads_and_exposes_graph(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="production" static="yes" select="false()"/>'
        '<xsl:include href="lib.xsl" use-when="$production"/>'
    )
    _write(
        tmp_path,
        "lib.xsl",
        _stylesheet('<xsl:template name="included-template"/>'),
    )

    effective = _effective(tmp_path, xml, {"production": True})

    assert len(effective.includes) == 1
    assert effective.find("included-template") is not None
    assert effective.includes[0].find("included-template") is not None


def test_included_static_variables_flow_in_document_order(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.xsl",
        _stylesheet(
            '<xsl:variable name="from-lib" static="yes" select="\'lib\'"/>'
            '<xsl:template name="lib-template" use-when="$prefix = \'root\'"/>'
        ),
    )
    xml = _stylesheet(
        '<xsl:variable name="prefix" static="yes" select="\'root\'"/>'
        '<xsl:include href="lib.xsl"/>'
        '<xsl:template name="after" use-when="$from-lib = \'lib\'"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("lib-template") is not None
    assert effective.find("after") is not None
    assert effective.static_variables["from-lib"][0].value == "lib"


def test_static_variable_forward_reference_preserves_xpath_code(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" use-when="$later"/>'
        '<xsl:variable name="later" static="yes" select="true()"/>'
    )

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XPST0008"
    assert caught.value.phase == "static"
    assert caught.value.cause is not None
    assert caught.value.location is not None


def test_static_variable_is_not_in_scope_within_its_own_declaration(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:variable name="self" static="yes" select="$self"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XPST0008"


def test_static_parameter_external_value_overrides_select(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="production" static="yes" select="false()"/>'
        '<xsl:template name="t" use-when="$production"/>'
    )

    effective = _effective(tmp_path, xml, {"production": True})

    assert effective.find("t") is not None
    assert bool(effective.static_variables["production"][0].value) is True


def test_static_variable_inconsistency_reports_xtse3450(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:variable name="v" static="yes" select="\'one\'"/>'
        '<xsl:variable name="v" static="yes" select="\'two\'"/>'
    )

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE3450"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('<xsl:param name="p" static="yes">content</xsl:param>', "XTSE0010"),
        (
            '<xsl:template name="t"><xsl:variable name="v" static="yes" '
            'select="1"/></xsl:template>',
            "XTSE0020",
        ),
        (
            '<xsl:variable name="v" static="yes" select="1" visibility="public"/>',
            "XTSE0020",
        ),
        (
            '<xsl:variable name="v" static="yes" select="1" visibility="final"/>',
            "XTSE0020",
        ),
        (
            '<xsl:param name="p" static="yes" select="1" visibility="private"/>',
            "XTSE0090",
        ),
        (
            '<xsl:param name="p" static="yes" select="1" visibility="invalid"/>',
            "XTSE0090",
        ),
        ('<xsl:param name="p" static="YES" select="1"/>', "XTSE0020"),
        ('<xsl:param name="p" static="yes" select="1" required="yes"/>', "XTSE0010"),
        ('<xsl:param name="p" static="yes" required="yes"/>', "XTDE0050"),
    ],
)
def test_invalid_static_declarations(tmp_path: Path, body: str, code: str) -> None:
    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, _stylesheet(body))

    assert caught.value.code == code


def test_stylesheet_whitespace_is_stripped_but_raw_tree_is_not(tmp_path: Path) -> None:
    xml = _stylesheet(
        "\n  <!--gone-->\n  <?gone data?>\n  "
        '<xsl:template name="t">\n    <out>text</out>\n  </xsl:template>\n'
    )
    path = _write(tmp_path, "module.xsl", xml)
    module = ModuleLoader().load(path)

    assert any(child.kind is NodeKind.COMMENT for child in module.root.children)
    effective = StaticPreprocessor().apply(module, None)
    template = effective.find("t")

    assert [child.kind for child in template.children] == [NodeKind.ELEMENT]
    assert [child.kind for child in effective.root.children] == [NodeKind.ELEMENT]
    out = template.children[0]
    assert [child.value for child in out.children if child.kind is NodeKind.TEXT] == ["text"]


def test_xml_space_preserve_and_default_override(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" xml:space="preserve">'
        "<preserved>  </preserved>"
        '<defaulted xml:space="default">  </defaulted>'
        "</xsl:template>"
    )

    template = _effective(tmp_path, xml).find("t")
    preserved, defaulted = [child for child in template.children if child.kind is NodeKind.ELEMENT]

    assert _texts(preserved) == ["  "]
    assert _texts(defaulted) == []


def test_xsl_text_preserves_whitespace_and_param_sort_catch_rules(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t">'
        '<xsl:text xml:space="default">  </xsl:text>'
        '   <xsl:param name="p"/>'
        '<xsl:try><xsl:sequence select="1"/>   <xsl:catch errors="*"/></xsl:try>'
        "</xsl:template>"
    )

    template = _effective(tmp_path, xml).find("t")
    elements = [child for child in template.children if child.kind is NodeKind.ELEMENT]

    assert _texts(elements[0]) == ["  "]
    assert [child.name.rsplit("}", 1)[-1] for child in elements] == [
        "text",
        "param",
        "try",
    ]
    assert _texts(elements[1]) == []
    try_element = elements[2]
    assert [child.name.rsplit("}", 1)[-1] for child in try_element.children] == [
        "sequence",
        "catch",
    ]
    assert all(child.kind is not NodeKind.TEXT for child in try_element.children)


def test_simplified_stylesheet_normalizes_with_namespace_and_base_context(
    tmp_path: Path,
) -> None:
    xml = (
        f'<doc:out xsl:version="3.0" xmlns:xsl="{XSLT_NS}" xmlns:doc="{DOC_NS}" '
        'xml:base="sub/">text<doc:item/></doc:out>'
    )
    path = _write(tmp_path, "simple.xsl", xml)
    effective = StaticPreprocessor().apply(ModuleLoader().load(path), None)

    assert effective.simplified is True
    assert effective.root.name == f"{{{XSLT_NS}}}stylesheet"
    assert effective.attribute("version") == "3.0"
    template = effective.root.children[0]
    assert template.name == f"{{{XSLT_NS}}}template"
    assert template.attribute("match") == "/"
    literal = template.children[0]
    assert literal.name == f"{{{DOC_NS}}}out"
    assert literal.namespace_uri == DOC_NS
    assert literal.in_scope_namespaces["doc"] == DOC_NS
    assert literal.base_uri == (tmp_path / "sub").resolve().as_uri() + "/"
    assert literal.source_location() is not None


def test_source_space_rules_are_collected_not_applied_to_stylesheet(
    tmp_path: Path,
) -> None:
    xml = _stylesheet(
        '<xsl:strip-space elements="doc:item"/>'
        '<xsl:preserve-space elements="doc:keep"/>'
        '<xsl:template name="t"> <out>  </out> </xsl:template>'
    )

    effective = _effective(tmp_path, xml)

    strip = effective.strip_space
    preserve = effective.preserve_space
    assert len(strip) == 1
    assert strip[0].elements == ("doc:item",)
    assert strip[0].namespaces["doc"] == DOC_NS
    assert strip[0].preserve is False
    assert preserve[0].elements == ("doc:keep",)
    assert preserve[0].preserve is True
    out = effective.find("t").children[0]
    assert _texts(out) == []


def test_literal_result_namespace_context_is_not_rewritten(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t">'
        '<doc:out xmlns:other="urn:other"><other:child/></doc:out>'
        "</xsl:template>"
    )

    literal = _effective(tmp_path, xml).find("t").children[0]
    child = literal.children[0]

    assert literal.namespace_uri == DOC_NS
    assert literal.in_scope_namespaces["other"] == "urn:other"
    assert child.namespace_uri == "urn:other"
    assert child.in_scope_namespaces == literal.in_scope_namespaces


def test_unavailable_static_resource_error_is_preserved(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:include href="missing.xsl" use-when="true()"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE0165"
    assert isinstance(caught.value.cause, FileNotFoundError)


def test_system_property_static_expression(tmp_path: Path) -> None:
    xml = _stylesheet(
        "<xsl:template name=\"t\" use-when=\"system-property('xsl:is-schema-aware') = 'yes'\"/>"
    )

    assert _effective(tmp_path, xml).find("t") is not None


def test_literal_result_use_when_is_not_unprefixed(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t"><out use-when="false()"/></xsl:template>')

    literal = _effective(tmp_path, xml).find("t").children[0]
    assert literal.name == "out"
    assert literal.attribute("use-when") == "false()"


def test_static_processing_does_not_load_inspection_schema(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t"/>')
    path = _write(tmp_path, "module.xsl", xml)
    module = ModuleLoader().load(path)

    effective = StaticPreprocessor().apply(module, None)

    assert effective.raw is module
    assert effective.document is module.document
    assert isinstance(StaticPreprocessor().apply(module, None), type(effective))


def test_not_all_static_errors_are_xslt_errors() -> None:
    assert issubclass(XsltStaticError, XsltError)


def _root(tmp_path: Path) -> Any:
    path = _write(tmp_path, "helpers.xsl", _stylesheet())
    return ModuleLoader().load(path).root


def test_static_helpers_cover_ebv_and_string_values(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import _ebv, _string_value

    node = _root(tmp_path)

    assert _ebv((), node) is False
    assert _ebv((node,), node) is True
    assert _ebv((XdmAtomic.boolean(True),), node) is True
    assert _ebv((XdmAtomic.boolean(False),), node) is False
    assert _ebv((XdmAtomic.integer(3),), node) is True
    assert _ebv((XdmAtomic.integer(0),), node) is False
    assert _ebv((XdmAtomic.double(float("nan")),), node) is False
    assert _ebv((XdmAtomic.string("x"),), node) is True
    assert _ebv((XdmAtomic.string(""),), node) is False
    assert _ebv((XdmAtomic.any_uri("x"),), node) is True
    assert _ebv((XdmAtomic.any_uri(""),), node) is False
    with pytest.raises(XsltStaticError) as caught:
        _ebv((XdmAtomic.integer(1), XdmAtomic.integer(2)), node)
    assert caught.value.code == "XPTY0004"
    with pytest.raises(XsltStaticError) as caught:
        _ebv((XdmMap.empty(),), node)
    assert caught.value.code == "XPTY0004"
    from pyxsd import xsd_data_types as _xsd

    with pytest.raises(XsltStaticError) as caught:
        _ebv((XdmAtomic.typed(_xsd.Date("2020-01-01"), _xsd.Date),), node)
    assert caught.value.code == "FORG0006"

    assert _string_value((XdmAtomic.integer(2), XdmAtomic.string("x")), node) == "2 x"
    assert _string_value((node,), node) == ""


def test_string_value_rejects_function_items(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import _string_value

    node = _root(tmp_path)
    with pytest.raises(XsltStaticError) as caught:
        _string_value((XdmMap.empty(),), node)
    assert caught.value.code == "XPTY0004"


def test_error_code_extraction_and_fallback() -> None:
    from pyxsd.xslt.compiler.static import _error_code

    assert _error_code(RuntimeError("[err:XPST0003] bad")) == "XPST0003"
    assert _error_code(RuntimeError("no code")) == "XTSE0010"


def test_static_variable_name_expansion_forms(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import _expand_name

    root = _root(tmp_path)

    assert _expand_name("plain", root.in_scope_namespaces, root) == "plain"
    assert _expand_name("Q{urn:x}v", root.in_scope_namespaces, root) == "{urn:x}v"
    assert _expand_name("{urn:x}v", root.in_scope_namespaces, root) == "{urn:x}v"
    assert _expand_name("doc:v", root.in_scope_namespaces, root) == f"{{{DOC_NS}}}v"
    with pytest.raises(XsltStaticError) as caught:
        _expand_name("", root.in_scope_namespaces, root)
    assert caught.value.code == "XTSE0010"
    with pytest.raises(XsltStaticError) as caught:
        _expand_name("missing:v", root.in_scope_namespaces, root)
    assert caught.value.code == "XTSE0280"


def test_namespaced_static_variables_and_lexical_visibility(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="doc:flag" static="yes" select="true()"/>'
        '<xsl:template name="t" use-when="$doc:flag"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None
    assert f"{{{DOC_NS}}}flag" in effective.static_variables


def test_namespaced_static_parameter_external_keys(tmp_path: Path) -> None:
    template = '<xsl:template name="t" use-when="$doc:flag"/>'
    for key in (f"{{{DOC_NS}}}flag", "doc:flag", "flag"):
        xml = _stylesheet('<xsl:param name="doc:flag" static="yes" select="false()"/>' + template)
        effective = _effective(tmp_path, xml, {key: True}, name=f"{key.replace(':', '_')}.xsl")
        assert effective.find("t") is not None


def test_static_variable_declaration_edge_cases(tmp_path: Path) -> None:
    same = _stylesheet(
        '<xsl:variable name="v" static="yes" select="1"/>'
        '<xsl:variable name="v" static="yes" select="1"/>'
    )
    effective = _effective(tmp_path, same, name="same.xsl")
    assert effective.static_variables["v"][0].lexical == "1"

    different_lengths = _stylesheet(
        '<xsl:variable name="v" static="yes" select="1"/>'
        '<xsl:variable name="v" static="yes" select="(1, 2)"/>'
    )
    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, different_lengths, name="lengths.xsl")
    assert caught.value.code == "XTSE3450"

    ordinary = _stylesheet('<xsl:variable name="v" select="1"/><xsl:template name="t"/>')
    assert _effective(tmp_path, ordinary, name="ordinary.xsl").find("t") is not None

    with pytest.raises(XsltStaticError) as caught:
        _effective(
            tmp_path,
            _stylesheet('<xsl:param static="yes" select="1"/>'),
            name="nameless.xsl",
        )
    assert caught.value.code == "XTSE0010"

    with pytest.raises(XsltStaticError) as caught:
        _effective(
            tmp_path,
            _stylesheet('<xsl:param name="p" static="yes" select="1" visibility="public"/>'),
            name="public-param.xsl",
        )
    assert caught.value.code == "XTSE0090"


def test_static_variable_without_bound_prefix(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:variable name="Q{urn:elsewhere}v" static="yes" select="1"/>')

    effective = _effective(tmp_path, xml)

    assert "{urn:elsewhere}v" in effective.static_variables


def test_duplicate_include_traversal_and_space_rules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.xsl",
        _stylesheet(
            '<xsl:strip-space elements=""/>'
            '<xsl:preserve-space elements="doc:keep"/>'
            '<xsl:template name="lib-template"/>'
        ),
    )
    xml = _stylesheet(
        '<xsl:include href="lib.xsl" use-when="true()"/>'
        '<xsl:include href="lib.xsl" use-when="true()"/>'
    )

    effective = _effective(tmp_path, xml)

    assert len(effective.includes) == 2
    assert effective.find("lib-template") is not None
    assert effective.find("absent") is None
    assert effective.strip_space == ()
    assert [rule.elements for rule in effective.preserve_space] == [
        ("doc:keep",),
        ("doc:keep",),
    ]


def test_conditional_module_cycle_reports_include_and_import_codes(tmp_path: Path) -> None:
    include = _write(
        tmp_path,
        "self.xsl",
        _stylesheet('<xsl:include href="self.xsl" use-when="true()"/>'),
    )
    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, include.read_text(), name="self-again.xsl")
    assert caught.value.code == "XTSE0180"

    import_path = _write(
        tmp_path,
        "self-import.xsl",
        _stylesheet('<xsl:import href="self-import.xsl" use-when="true()"/>'),
    )
    module = ModuleLoader().load(import_path)
    with pytest.raises(XsltStaticError) as caught:
        StaticPreprocessor().apply(module, None)
    assert caught.value.code == "XTSE0210"


def test_conditional_import_attaches_import_graph(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet('<xsl:param name="from-base" static="yes" select="1"/>'),
    )
    xml = _stylesheet(
        '<xsl:import href="base.xsl" use-when="true()"/>'
        '<xsl:template name="t" use-when="$from-base = 1"/>'
    )

    effective = _effective(tmp_path, xml)

    assert len(effective.imports) == 1
    assert effective.find("t") is not None


def test_include_without_href_reports_xtse0010(tmp_path: Path) -> None:
    with pytest.raises(XsltStaticError) as caught:
        _effective(
            tmp_path,
            _stylesheet("<xsl:include/>"),
            name="no-href.xsl",
        )

    assert caught.value.code == "XTSE0010"


def test_shadow_use_when_and_xpath_default_namespace(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="skip" _use-when="false()"/>'
        '<xsl:template name="keep" _use-when="true()" '
        "_xpath-default-namespace=\"{'urn:test'}\" "
        'xpath-default-namespace="urn:ignored"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("skip") is None
    kept = effective.find("keep")
    assert kept is not None
    assert kept.attribute("xpath-default-namespace") == "urn:test"
    assert kept.attribute_node("xpath-default-namespace").static is True


def test_namespaced_use_when_attribute_is_honoured(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t" xsl:use-when="false()"/>')

    assert _effective(tmp_path, xml).find("t") is None


def test_simplified_stylesheet_root_use_when_excludes_content(tmp_path: Path) -> None:
    xml = f'<out xsl:version="3.0" xmlns:xsl="{XSLT_NS}" xsl:use-when="false()"><child/></out>'
    effective = _effective(tmp_path, xml, name="simple-skip.xsl")

    assert effective.simplified is True
    assert effective.root.children[0].children == ()


def test_shadow_avt_empty_result_and_quotes(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" _priority="{()}" _select="{\'a\'\'b\'}" _mode="{map{\'k\': 1}?k}"/>'
    )

    template = _effective(tmp_path, xml).find("t")

    assert template.attribute("priority") == ""
    assert template.attribute("select") == "a'b"
    assert template.attribute("mode") == "1"


def test_effective_node_accessors_and_synthetic_nodes(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import EffectiveAttribute, EffectiveElement, EffectiveText

    plain = EffectiveAttribute("plain", "x")
    assert plain.local_name == "plain"
    assert plain.namespace_uri is None
    assert plain.prefix == ""
    assert plain.source_location() is None

    namespaced = EffectiveAttribute(f"{{{DOC_NS}}}kind", "x")
    assert namespaced.local_name == "kind"
    assert namespaced.namespace_uri == DOC_NS

    text = EffectiveText("value")
    assert text.kind is NodeKind.TEXT
    assert text.string_value == "value"
    assert text.source_location() is None

    synthetic = EffectiveElement(
        name=f"{{{XSLT_NS}}}stylesheet",
        attributes=(),
        children=(),
        in_scope_namespaces={"doc": DOC_NS},
    )
    assert synthetic.prefix == ""
    assert synthetic.source_location() is None
    assert synthetic.source_span() is None
    assert synthetic.in_scope_namespaces["doc"] == DOC_NS

    with_comment = _effective(
        tmp_path,
        _stylesheet("<!--c--><xsl:template name='t'><out>text</out></xsl:template>"),
    )
    template = with_comment.find("t")
    assert template.source_span() is not None
    assert template.children[0].local_name == "out"
    descendants = [
        node.local_name
        for node in with_comment.root.descendants
        if isinstance(node, EffectiveElement)
    ]
    assert descendants[:2] == ["template", "out"]


def test_effective_attribute_prefixed_lookup(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t"><out doc:kind="x"/></xsl:template>')

    literal = _effective(tmp_path, xml).find("t").children[0]

    assert literal.attribute("doc:kind") == "x"
    assert literal.attribute("missing:kind") is None
    assert literal.attribute_node(f"{{{DOC_NS}}}kind") is not None


def test_system_and_capability_static_functions(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" use-when="'
        "function-available('fn:true') and function-available('fn:concat', 2) "
        "and function-available('xs:string') and not(function-available('other:x')) "
        "and element-available('xsl:template') and not(element-available('template')) "
        "and type-available('xs:string') and not(type-available('doc:Thing')) "
        "and count(available-system-properties()) > 0 "
        "and system-property('xsl:unknown') = ''\"/>"
    )

    assert _effective(tmp_path, xml).find("t") is not None


def test_element_available_with_default_xslt_namespace(tmp_path: Path) -> None:
    xml = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        f'xpath-default-namespace="{XSLT_NS}" xmlns="urn:xml-default">'
        '<xsl:template name="t" use-when="element-available(\'template\')"/>'
        "</xsl:stylesheet>"
    )

    effective = _effective(tmp_path, xml, name="default-ns.xsl")

    assert effective.find("t") is not None


def test_lexical_name_without_a_bound_prefix() -> None:
    from pyxsd.xslt.compiler.static import _lexical_name

    assert _lexical_name("plain", {}) == "plain"
    assert _lexical_name("{urn:x}v", {"doc": DOC_NS}) == "{urn:x}v"
    assert _lexical_name(f"{{{DOC_NS}}}v", {"doc": DOC_NS}) == "doc:v"


def test_effective_module_properties_and_proxy_construction(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import EffectiveModule

    effective = _effective(tmp_path, _stylesheet('<xsl:template name="t"/>'))
    assert effective.identity == effective.source.identity
    assert effective.uri == effective.source.identity
    assert effective.version == "3.0"
    assert effective.document is effective.source.document
    assert effective.raw is effective.source

    rehydrated = EffectiveModule(
        source=effective.source,
        root=effective.root,
        includes=(),
        imports=(),
        static_variables={"v": ()},
        simplified=False,
    )
    assert rehydrated.static_variables["v"] == ()

    already_frozen = EffectiveModule(
        source=effective.source,
        root=effective.root,
        includes=(),
        imports=(),
        static_variables=rehydrated.static_variables,
        simplified=False,
    )
    assert already_frozen.static_variables is rehydrated.static_variables


def test_effective_find_descendant_and_cycle_guard(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.static import EffectiveModule

    effective = _effective(
        tmp_path,
        _stylesheet('<xsl:template name="t"><out>text</out></xsl:template>'),
    )
    assert effective.find("out") is not None
    assert effective.find("text") is None

    base = EffectiveModule(
        source=effective.source,
        root=effective.root,
        includes=(),
        imports=(),
        static_variables={},
        simplified=False,
    )
    cycle = EffectiveModule(
        source=effective.source,
        root=effective.root,
        includes=(base,),
        imports=(),
        static_variables={},
        simplified=False,
    )
    object.__setattr__(base, "includes", (cycle,))
    object.__setattr__(cycle, "includes", (base,))

    assert base.find("absent") is None
    assert base.strip_space == ()
    assert base.preserve_space == ()


def test_more_capability_function_name_forms(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" use-when="'
        "function-available('true') "
        "and function-available('Q{http://www.w3.org/2005/xpath-functions}true') "
        "and function-available('{http://www.w3.org/2005/xpath-functions}true') "
        "and function-available('math:pi') "
        "and not(function-available('map:unknown')) "
        "and not(function-available('xsl:unknown')) "
        "and element-available('Q{http://www.w3.org/1999/XSL/Transform}template') "
        "and element-available('{http://www.w3.org/1999/XSL/Transform}template') "
        "and not(type-available('other:Thing'))\"/>"
    )

    assert _effective(tmp_path, xml).find("t") is not None


def test_anonymous_module_static_preprocessing(tmp_path: Path) -> None:
    import io

    stream = io.StringIO(_stylesheet('<xsl:template name="t"/>'))
    effective = StaticPreprocessor().apply(ModuleLoader().load(stream), None)

    assert effective.identity is None
    assert effective.find("t") is not None


def test_unknown_xml_space_value_keeps_inherited_state(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" xml:space="preserve">'
        '<keep xml:space="invalid">  </keep>'
        "</xsl:template>"
    )

    template = _effective(tmp_path, xml).find("t")
    keep = template.children[0]

    assert _texts(keep) == ["  "]


def test_whitespace_after_catch_is_removed_even_when_preserved(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" xml:space="preserve">'
        '<xsl:try><xsl:sequence select="1"/><xsl:catch errors="*"/>   '
        "</xsl:try></xsl:template>"
    )

    try_element = _effective(tmp_path, xml).find("t").children[0]

    assert [child.name for child in try_element.children] == [
        f"{{{XSLT_NS}}}sequence",
        f"{{{XSLT_NS}}}catch",
    ]
    assert all(child.kind is not NodeKind.TEXT for child in try_element.children)


def test_error_string_formatting_variants() -> None:
    from pyxsd.locations import IssueLocation, PathStep

    plain = XsltError("plain")
    assert str(plain) == "plain"

    located = XsltError(
        "located",
        code="XTSE0001",
        location=IssueLocation(
            "file:///a.xsl",
            (PathStep(f"{{{XSLT_NS}}}stylesheet", 1),),
        ),
    )
    assert str(located) == (f"[XTSE0001] located at file:///a.xsl#{{{XSLT_NS}}}stylesheet[1]")

    no_source = XsltError(
        "located",
        uri="file:///fallback.xsl",
        location=IssueLocation(None, ()),
    )
    assert str(no_source) == "located at file:///fallback.xsl"

    no_where = XsltError(
        "located",
        location=IssueLocation(None, ()),
    )
    assert str(no_where) == "located"

    with_uri = XsltError("uri", uri="file:///a.xsl")
    assert str(with_uri) == "uri in file:///a.xsl"


def test_stylesheet_root_text_is_xtse0120(tmp_path: Path) -> None:
    with pytest.raises(XsltStaticError) as caught:
        _effective(
            tmp_path,
            _stylesheet('stray text<xsl:template name="t"/>'),
            name="root-text.xsl",
        )

    assert caught.value.code == "XTSE0120"


def test_type_available_uses_default_element_namespace(tmp_path: Path) -> None:
    xml = (
        f'<xsl:stylesheet version="3.0" xmlns:xsl="{XSLT_NS}" '
        'xmlns="urn:xml-default" xpath-default-namespace="http://www.w3.org/2001/XMLSchema">'
        '<xsl:template name="t" use-when="type-available(\'string\')"/>'
        "</xsl:stylesheet>"
    )

    assert _effective(tmp_path, xml, name="default-type-ns.xsl").find("t") is not None


def test_static_parameter_without_select_defaults_to_empty(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="p" static="yes"/><xsl:template name="t" use-when="$p = \'\'"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None
    assert effective.static_variables["p"][0].lexical == ""


def test_shadow_use_when_is_consumed_not_kept(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="keep" _use-when="true()"/>'
        '<xsl:template name="skip" _use-when="false()"/>'
    )

    effective = _effective(tmp_path, xml)
    kept = effective.find("keep")

    assert kept is not None
    assert kept.attribute("use-when") is None
    assert effective.find("skip") is None


def test_empty_static_avt_contributes_empty_string(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:variable name="N" static="yes" select="\'x\'"/>'
        '<xsl:template name="t" _select="${}{$N}"/>'
    )

    template = _effective(tmp_path, xml).find("t")

    assert template.attribute("select") == "$x"


def test_ancestor_xpath_default_namespace_is_inherited(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" use-when="\'abc\' instance of string"/>',
        attributes=' xpath-default-namespace="http://www.w3.org/2001/XMLSchema"',
    )

    assert _effective(tmp_path, xml).find("t") is not None


def test_root_xml_space_preserve_is_inherited(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t"><out>  </out></xsl:template>',
        attributes=' xml:space="preserve"',
    )

    out = _effective(tmp_path, xml).find("t").children[0]

    assert _texts(out) == ["  "]


def test_nbsp_is_not_xml_whitespace(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t"><out>&#160;</out></xsl:template>')
    out = _effective(tmp_path, xml).find("t").children[0]
    assert _texts(out) == ["\u00a0"]

    with pytest.raises(XsltStaticError) as caught:
        _effective(
            tmp_path,
            _stylesheet('\u00a0<xsl:template name="t"/>'),
            name="nbsp-root.xsl",
        )
    assert caught.value.code == "XTSE0120"


def test_import_precedence_ignores_later_lower_declaration(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet('<xsl:variable name="inc" static="yes" select="false()"/>'),
    )
    xml = _stylesheet(
        '<xsl:variable name="inc" static="yes" select="true()"/>'
        '<xsl:import href="base.xsl"/>'
        '<xsl:template name="t" use-when="$inc"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None


def test_import_precedence_higher_first_wins_even_when_inconsistent(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet('<xsl:variable name="p" static="yes" select="2"/>'),
    )
    xml = _stylesheet(
        '<xsl:param name="p" static="yes" select="1"/>'
        '<xsl:import href="base.xsl"/>'
        '<xsl:template name="t" use-when="$p = 1"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None
    assert effective.static_variables["p"][0].lexical == "1"


def test_import_precedence_later_higher_inconsistent_is_xtse3450(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet('<xsl:variable name="p" static="yes" select="1"/>'),
    )
    xml = _stylesheet(
        '<xsl:import href="base.xsl"/><xsl:variable name="p" static="yes" select="0"/>'
    )

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE3450"


def test_import_precedence_between_imports_inconsistent_is_xtse3450(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "a.xsl",
        _stylesheet('<xsl:variable name="flip" static="yes" select="true()"/>'),
    )
    _write(
        tmp_path,
        "b.xsl",
        _stylesheet('<xsl:variable name="flip" static="yes" select="false()"/>'),
    )
    xml = _stylesheet('<xsl:import href="a.xsl"/><xsl:import href="b.xsl"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE3450"


def test_import_precedence_consistent_imports_are_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet('<xsl:variable name="p" static="yes" select="1"/>'),
    )
    xml = _stylesheet(
        '<xsl:import href="base.xsl"/>'
        '<xsl:import href="base.xsl"/>'
        '<xsl:template name="t" use-when="$p = 1"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None


def test_external_parameter_does_not_override_static_variable(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:variable name="p" static="yes" select="\'declared\'"/>'
        '<xsl:template name="t" use-when="$p = \'declared\'"/>'
    )

    effective = _effective(tmp_path, xml, {"p": "external"})

    assert effective.find("t") is not None
    assert effective.static_variables["p"][0].lexical == "declared"


def test_function_available_checks_arity_and_added_functions(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t" use-when="'
        "function-available('fn:concat', 2) "
        "and function-available('fn:concat', 33) "
        "and function-available('fn:concat', 99) "
        "and not(function-available('fn:true', 1)) "
        "and function-available('fn:abs', 1) "
        "and function-available('fn:avg', 1) "
        "and function-available('fn:data', 1) "
        "and not(function-available('fn:data', 0)) "
        "and function-available('fn:static-base-uri') "
        "and function-available('fn:parse-xml') "
        "and function-available('fn:json-to-xml')"
        '"/>'
    )

    assert _effective(tmp_path, xml).find("t") is not None


def test_ebv_rejects_date_in_use_when(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t" use-when="xs:date(\'2020-01-01\')"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "FORG0006"


def test_space_rules_keep_module_local_namespace_context(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "base.xsl",
        _stylesheet(
            '<xsl:preserve-space elements="p:keep"/>',
            attributes=' xmlns:p="urn:imported"',
        ),
    )
    xml = _stylesheet(
        '<xsl:import href="base.xsl"/><xsl:strip-space elements="p:item"/>',
        attributes=' xmlns:p="urn:main"',
    )

    effective = _effective(tmp_path, xml)

    assert effective.strip_space[0].namespaces["p"] == "urn:main"
    assert effective.preserve_space[0].namespaces["p"] == "urn:imported"


def test_space_rule_freezes_plain_namespace_mapping() -> None:
    from pyxsd.xslt.compiler.static import SpaceRule

    rule = SpaceRule(
        elements=("p:item",),
        namespaces={"p": DOC_NS},
        preserve=False,
        module=None,
        base_uri=None,
    )

    assert rule.namespaces["p"] == DOC_NS
    from types import MappingProxyType

    rule = SpaceRule(
        elements=(),
        namespaces=MappingProxyType({}),
        preserve=True,
        module=None,
        base_uri=None,
    )
    assert rule.namespaces == {}


def test_non_global_invalid_static_flag_reports_xtse0020(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:template name="t"><xsl:param name="p" static="YES" select="1"/></xsl:template>'
    )

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE0020"


def test_non_global_static_no_is_accepted(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:template name="t"><xsl:param name="p" static="no"/></xsl:template>')

    assert _effective(tmp_path, xml).find("t") is not None


def test_required_on_static_variable_is_xtse0090(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:variable name="v" static="yes" select="1" required="yes"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE0090"


def test_invalid_required_value_is_xtse0020(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:param name="p" static="yes" required="maybe"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE0020"


def test_required_static_parameter_accepts_external_value(tmp_path: Path) -> None:
    xml = _stylesheet(
        '<xsl:param name="p" static="yes" required="yes"/>'
        '<xsl:template name="t" use-when="$p = \'supplied\'"/>'
    )

    effective = _effective(tmp_path, xml, {"p": "supplied"})

    assert effective.find("t") is not None


def test_static_variable_without_select_is_xtse0010(tmp_path: Path) -> None:
    xml = _stylesheet('<xsl:variable name="v" static="yes"/>')

    with pytest.raises(XsltStaticError) as caught:
        _effective(tmp_path, xml)

    assert caught.value.code == "XTSE0010"


def test_static_variable_select_uses_own_xpath_default_namespace(
    tmp_path: Path,
) -> None:
    xml = _stylesheet(
        '<xsl:variable name="v" static="yes" '
        f'xpath-default-namespace="{XSLT_NS}" '
        "select=\"element-available('template')\"/>"
        '<xsl:template name="t" use-when="$v"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None


def test_static_variable_select_uses_own_shadow_xpath_default_namespace(
    tmp_path: Path,
) -> None:
    xml = _stylesheet(
        '<xsl:variable name="v" static="yes" '
        f'_xpath-default-namespace="{{{XSLT_NS!r}}}" '
        "select=\"element-available('template')\"/>"
        '<xsl:template name="t" use-when="$v"/>'
    )

    effective = _effective(tmp_path, xml)

    assert effective.find("t") is not None
