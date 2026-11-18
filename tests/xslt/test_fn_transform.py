"""Nested ``fn:transform`` invocations under the outer transformation's policy.

Every nested call runs on the outer processor's immutable policy snapshot with
the cumulative budget ledger shared across nesting depth.  Compiled artifacts
are cached by stylesheet identity; transformation results never are.
"""

from __future__ import annotations

import io
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from elementpath.datatypes import QName
from elementpath.exceptions import ElementPathZeroDivisionError
from tests.xslt.support import stylesheet

from pyxsd.resource_policy import ResourcePolicyError
from pyxsd.xdm import XdmArray, XdmAtomic, XdmMap, as_xdm
from pyxsd.xdm.nodes import XdmDocument, XdmNode
from pyxsd.xslt import ExecutionLimits, XsltDynamicError, XsltExtensions, XsltProcessor
from pyxsd.xslt.errors import XsltError
from pyxsd.xslt.processor import CompiledStylesheet, XsltInvocationError
from pyxsd.xslt.runtime.frames import PYXSD_FRAME_EXHAUSTED

APP = "urn:app"
XSLT_NS = "http://www.w3.org/1999/XSL/Transform"

OUTER_OPTIONS_TEMPLATE = (
    '<xsl:template name="main"><xsl:param name="options"/>'
    '<xsl:sequence select="transform($options)"/></xsl:template>'
)

NESTED_SHEET = (
    '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
    'version="3.0">'
    '<xsl:template name="xsl:initial-template"><out>nested</out></xsl:template>'
    "</xsl:stylesheet>"
)

HEAVY_SHEET = (
    '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
    'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">'
    '<xsl:template name="xsl:initial-template">'
    "<xsl:variable name='work' as='xs:integer*'>"
    "<xsl:for-each select='1 to 40'><xsl:sequence select='. * 2'/></xsl:for-each>"
    "</xsl:variable>"
    "<xsl:sequence select='count($work)'/>"
    "</xsl:template></xsl:stylesheet>"
)


def qname(uri: str, local: str) -> XdmAtomic:
    value = as_xdm(QName(uri, local))[0]
    assert isinstance(value, XdmAtomic)
    return value


def run_transform(
    options: dict,
    *,
    outer_body: str = OUTER_OPTIONS_TEMPLATE,
    namespaces: dict[str, str] | None = None,
    processor: XsltProcessor | None = None,
    extensions: XsltExtensions | None = None,
    limits: ExecutionLimits | None = None,
) -> XdmMap:
    """Evaluate ``transform($options)`` inside one outer stylesheet invocation.

    The option map travels as an ordinary template parameter, so the call runs
    through the real bridge and evaluator exactly as stylesheet code would.
    """
    source = stylesheet(outer_body, namespaces=namespaces or {"app": APP})
    compiled = (processor or XsltProcessor()).compile(
        io.BytesIO(source),
        extensions=extensions,
    )
    result = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={"options": XdmMap.from_mapping(options)},
        limits=limits,
    )
    principal = result.principal
    assert len(principal) == 1 and isinstance(principal[0], XdmMap)
    return principal[0]


def run_expression(body: str, *, namespaces: dict[str, str] | None = None) -> XdmMap:
    """Evaluate an outer stylesheet whose body builds and passes its own map."""
    source = stylesheet(
        f'<xsl:template name="main"><xsl:sequence select="{body}"/></xsl:template>',
        namespaces=namespaces or {"app": APP},
    )
    result = (
        XsltProcessor()
        .compile(io.BytesIO(source))
        .transform(
            initial_template="main",
            delivery="raw",
        )
    )
    principal = result.principal
    assert len(principal) == 1 and isinstance(principal[0], XdmMap)
    return principal[0]


def map_get(result: XdmMap, key: str):
    value = result.get(XdmAtomic.string(key))
    assert isinstance(value, tuple)
    return value


# ---------------------------------------------------------------------------
# The brief's headline shape: delivery, secondary keys, and pinned errors


def test_fn_transform_delivery_budgets_and_errors() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/secondary.xml">'
        "<secondary/></xsl:result-document><principal/></xsl:template>"
        "</xsl:stylesheet>"
    )
    options = {
        "stylesheet-text": nested,
        "delivery-format": "raw",
    }
    result = run_transform(options)
    principal = map_get(result, "output")
    assert len(principal) == 1 and isinstance(principal[0], XdmNode)
    assert principal[0].local_name == "principal"
    secondary = map_get(result, "https://example.test/secondary.xml")
    assert len(secondary) == 1 and isinstance(secondary[0], XdmNode)
    assert secondary[0].local_name == "secondary"

    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": nested, "stylesheet-location": "x.xsl"})
    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform({"stylesheet-text": nested, "xslt-version": 3.1})


def test_base_output_uri_keys_the_principal_by_its_absolute_uri() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="secondary.xml">'
        "<side/></xsl:result-document><principal/></xsl:template>"
        "</xsl:stylesheet>"
    )
    result = run_transform(
        {
            "stylesheet-text": nested,
            "base-output-uri": "https://example.test/results/main.xml",
            "delivery-format": "raw",
        }
    )
    principal = map_get(result, "https://example.test/results/main.xml")
    assert len(principal) == 1 and isinstance(principal[0], XdmNode)
    assert principal[0].local_name == "principal"
    secondary = map_get(result, "https://example.test/results/secondary.xml")
    assert len(secondary) == 1 and isinstance(secondary[0], XdmNode)
    assert secondary[0].local_name == "side"
    assert not result.contains(XdmAtomic.string("output"))


def test_a_result_document_at_the_base_output_uri_is_the_output_entry() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:result-document><at-base/></xsl:result-document>"
        "</xsl:template></xsl:stylesheet>"
    )
    base = "https://example.test/results/base.xml"

    result = run_transform(
        {
            "stylesheet-text": nested,
            "base-output-uri": base,
            "delivery-format": "serialized",
        }
    )

    assert len(result) == 1
    assert map_get(result, base)[0].value == "<at-base/>"


def test_two_result_documents_with_distinct_hrefs_do_not_collapse() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:output method="xml" omit-xml-declaration="yes"/>'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:result-document><at-base/></xsl:result-document>"
        '<xsl:result-document href="other.xml"><other/></xsl:result-document>'
        "</xsl:template></xsl:stylesheet>"
    )
    base = "https://example.test/results/base.xml"

    result = run_transform(
        {
            "stylesheet-text": nested,
            "base-output-uri": base,
            "delivery-format": "serialized",
        }
    )

    assert len(result) == 2
    assert map_get(result, base)[0].value == "<at-base/>"
    assert map_get(result, "https://example.test/results/other.xml")[0].value == "<other/>"


# ---------------------------------------------------------------------------
# Stylesheet and package sources


def test_delivery_document_and_serialized_formats() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0"><xsl:output method="xml"/>'
        '<xsl:template name="xsl:initial-template"><out/></xsl:template>'
        "</xsl:stylesheet>"
    )
    document = run_transform({"stylesheet-text": nested, "delivery-format": "document"})
    delivered = map_get(document, "output")[0]
    assert isinstance(delivered, XdmDocument)
    assert delivered.root_element.local_name == "out"

    serialized = run_transform({"stylesheet-text": nested, "delivery-format": "serialized"})
    text = map_get(serialized, "output")[0]
    assert isinstance(text, XdmAtomic)
    assert text.value == '<?xml version="1.0" encoding="UTF-8"?><out/>'

    overridden = run_transform(
        {
            "stylesheet-text": nested,
            "delivery-format": "serialized",
            "serialization-params": {"omit-xml-declaration": True},
        }
    )
    assert map_get(overridden, "output")[0].value == "<out/>"


def test_delivery_document_keeps_typed_result_for_the_caller() -> None:
    """The principal tree a nested transform delivers keeps construction
    annotations (bug 30211): delivery never re-validates the implicit tree."""
    inner = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:v="urn:example:validation" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'version="3.0">'
        "<xsl:import-schema><xs:schema "
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:v="urn:example:validation" targetNamespace="urn:example:validation" '
        'elementFormDefault="qualified">'
        '<xs:complexType name="Order"><xs:sequence>'
        '<xs:element name="line"><xs:complexType>'
        '<xs:attribute name="code" type="xs:int"/>'
        "</xs:complexType></xs:element>"
        "</xs:sequence></xs:complexType>"
        '<xs:element name="Order" type="v:Order"/>'
        "</xs:schema></xsl:import-schema>"
        '<xsl:template name="xsl:initial-template">'
        '<v:Order xsl:validation="strict"><v:line code="1"/></v:Order>'
        "</xsl:template></xsl:stylesheet>"
    )
    result = run_transform({"stylesheet-text": inner, "delivery-format": "document"})
    delivered = map_get(result, "output")[0]
    assert isinstance(delivered, XdmDocument)
    assert delivered.root_element.type_name == "{urn:example:validation}Order"


def test_stylesheet_location_text_and_node_sources(tmp_path: Path) -> None:
    location = tmp_path / "nested.xsl"
    location.write_text(NESTED_SHEET, encoding="utf-8")
    by_location = run_transform({"stylesheet-location": str(location)})
    assert map_get(by_location, "output")[0].root_element.local_name == "out"

    by_text = run_transform({"stylesheet-text": NESTED_SHEET})
    assert map_get(by_text, "output")[0].root_element.local_name == "out"

    by_node = run_transform({"stylesheet-node": XdmDocument.parse(NESTED_SHEET.encode())})
    assert map_get(by_node, "output")[0].root_element.local_name == "out"


def _package_source() -> str:
    return (
        '<xsl:package xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'package-version="1.0" version="3.0">'
        '<xsl:template name="xsl:initial-template" visibility="public">'
        "<packaged/></xsl:template>"
        "</xsl:package>"
    )


def test_package_sources(tmp_path: Path) -> None:
    by_text = run_transform({"package-text": _package_source()})
    assert map_get(by_text, "output")[0].root_element.local_name == "packaged"

    location = tmp_path / "nested-package.xsl"
    location.write_text(_package_source(), encoding="utf-8")
    by_location = run_transform({"package-location": str(location)})
    assert map_get(by_location, "output")[0].root_element.local_name == "packaged"

    by_node = run_transform({"package-node": XdmDocument.parse(_package_source().encode())})
    assert map_get(by_node, "output")[0].root_element.local_name == "packaged"


def test_package_name_resolves_through_the_inherited_resolver(tmp_path: Path) -> None:
    from pyxsd.xslt.compiler.packages import PackageResolver

    library = (
        '<xsl:package xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'name="https://example.test/lib.xsl" package-version="2.0" version="3.0">'
        '<xsl:template name="xsl:initial-template" visibility="public">'
        "<library/></xsl:template>"
        "</xsl:package>"
    )
    resolver = PackageResolver.from_sources(io.BytesIO(library.encode("utf-8")))
    processor = XsltProcessor(resolver=resolver)
    result = run_transform(
        {"package-name": "https://example.test/lib.xsl", "package-version": "2.0"},
        processor=processor,
    )
    assert map_get(result, "output")[0].root_element.local_name == "library"
    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform({"package-name": "https://example.test/absent.xsl"}, processor=processor)


# ---------------------------------------------------------------------------
# Parameters, entry points, and context


def test_static_stylesheet_and_global_parameters() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">'
        "<xsl:param name='static-label' static='yes' as='xs:string'/>"
        '<xsl:param name="global-label"/>'
        '<xsl:template name="xsl:initial-template">'
        "<out><xsl:value-of select='$static-label'/>-"
        "<xsl:value-of select='$global-label'/></out>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    result = run_transform(
        {
            "stylesheet-text": nested,
            "static-params": {"static-label": "static"},
            "stylesheet-params": {"global-label": "global"},
        }
    )
    text = map_get(result, "output")[0].string_value
    assert isinstance(text, str)
    assert "static-global" in text


def test_template_tunnel_and_function_entries() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:app="urn:app" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'version="3.0">'
        '<xsl:template name="entry">'
        "<xsl:param name='label'/><xsl:param name='tunnel-note' tunnel='yes'/>"
        "<entry><xsl:value-of select='$label'/>:"
        "<xsl:value-of select='$tunnel-note'/>"
        "<xsl:call-template name='inner'/></entry>"
        "</xsl:template>"
        '<xsl:template name="inner">'
        "<xsl:param name='tunnel-note' tunnel='yes'/>"
        "<inner><xsl:value-of select='$tunnel-note'/></inner>"
        "</xsl:template>"
        "<xsl:function name='app:answer' as='xs:integer' visibility='public'>"
        "<xsl:param name='base' as='xs:integer'/>"
        "<xsl:sequence select='$base + 40'/>"
        "</xsl:function>"
        "</xsl:stylesheet>"
    )
    by_template = run_transform(
        {
            "stylesheet-text": nested,
            "initial-template": "entry",
            "template-params": {"label": "entry"},
            "tunnel-params": {"tunnel-note": "tunneled"},
            "delivery-format": "serialized",
        }
    )
    text = map_get(by_template, "output")[0].value
    assert "<inner>tunneled</inner>" in text
    assert "entry:tunneled" in text

    by_function = run_transform(
        {
            "stylesheet-text": nested,
            "initial-function": qname(APP, "answer"),
            "function-params": XdmArray((XdmAtomic.integer(2),)),
            "delivery-format": "raw",
        }
    )
    assert map_get(by_function, "output") == (XdmAtomic.integer(42),)


def test_initial_mode_and_match_selection() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        "<xsl:template match='item' mode='list'><listed/></xsl:template>"
        "</xsl:stylesheet>"
    )
    source = XdmDocument.parse(b"<doc><item/><item/></doc>")
    by_selection = run_transform(
        {
            "stylesheet-text": nested,
            "initial-match-selection": source,
            "initial-mode": qname("", "list"),
            "delivery-format": "raw",
        }
    )
    nodes = map_get(by_selection, "output")
    assert len(nodes) == 2
    assert all(node.local_name == "listed" for node in nodes)


def test_global_context_item() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:global-context-item use="required"/>'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='local-name(/*)'/>"
        "</xsl:template>"
        "</xsl:stylesheet>"
    )
    source = XdmDocument.parse(b"<global-root/>")
    result = run_transform(
        {"stylesheet-text": nested, "global-context-item": source, "delivery-format": "raw"}
    )
    assert map_get(result, "output") == (XdmAtomic.string("global-root"),)


# ---------------------------------------------------------------------------
# Base URIs and version fallback


def test_base_uris(tmp_path: Path) -> None:
    (tmp_path / "sibling.xsl").write_text(NESTED_SHEET, encoding="utf-8")
    outer_path = tmp_path / "outer.xsl"
    outer_path.write_text(
        stylesheet(OUTER_OPTIONS_TEMPLATE, namespaces={"app": APP}).decode("utf-8"),
        encoding="utf-8",
    )
    compiled = XsltProcessor().compile(outer_path)
    result = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={
            "options": XdmMap.from_mapping({"stylesheet-location": "sibling.xsl"})
        },
    )
    nested_map = result.principal[0]
    assert isinstance(nested_map, XdmMap)
    assert nested_map.get(XdmAtomic.string("output"))[0].root_element.local_name == "out"

    based = run_transform(
        {
            "stylesheet-text": (
                '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
                'version="3.0">'
                '<xsl:template name="xsl:initial-template">'
                "<xsl:sequence select='static-base-uri()'/>"
                "</xsl:template></xsl:stylesheet>"
            ),
            "stylesheet-base-uri": "https://example.test/dynamic/main.xsl",
            "delivery-format": "raw",
        }
    )
    assert map_get(based, "output") == (XdmAtomic.string("https://example.test/dynamic/main.xsl"),)


def test_version_fallback() -> None:
    older = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="1.0">'
        '<xsl:template name="xsl:initial-template"><out>compat</out></xsl:template>'
        "</xsl:stylesheet>"
    )
    for requested in (1.0, 2.0):
        result = run_transform({"stylesheet-text": older, "xslt-version": requested})
        assert map_get(result, "output")[0].root_element.local_name == "out"

    for too_new in (3.1, 4.0):
        with pytest.raises(XsltDynamicError, match="FOXT0001"):
            run_transform({"stylesheet-text": NESTED_SHEET, "xslt-version": too_new})


# ---------------------------------------------------------------------------
# Delivery collisions, post-processing, and error preservation


def test_output_collisions_are_specific_nested_errors() -> None:
    colliding = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/out.xml">'
        "<second/></xsl:result-document><first/></xsl:template>"
        "</xsl:stylesheet>"
    )
    with pytest.raises(XsltDynamicError, match="XTDE1490"):
        run_transform(
            {"stylesheet-text": colliding, "base-output-uri": "https://example.test/out.xml"}
        )

    duplicate = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/twin.xml">'
        "<one/></xsl:result-document>"
        '<xsl:result-document href="https://example.test/twin.xml">'
        "<two/></xsl:result-document>"
        "</xsl:template></xsl:stylesheet>"
    )
    with pytest.raises(XsltDynamicError, match="XTDE1490"):
        run_transform({"stylesheet-text": duplicate})


def test_post_process_function_items() -> None:
    body = (
        "transform(map{"
        "'stylesheet-text': $sheet,"
        "'delivery-format': 'serialized',"
        "'post-process': function($key, $value) { $key || '=' || $value }"
        "})"
    )
    outer = stylesheet(
        '<xsl:template name="main"><xsl:param name="sheet"/>'
        f'<xsl:sequence select="{body}"/></xsl:template>',
        namespaces={"app": APP},
    )
    compiled = XsltProcessor().compile(io.BytesIO(outer))
    result = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={"sheet": XdmAtomic.string(NESTED_SHEET)},
    )
    principal = result.principal
    assert isinstance(principal[0], XdmMap)
    assert map_get(principal[0], "output") == (
        XdmAtomic.string('output=<?xml version="1.0" encoding="UTF-8"?><out>nested</out>'),
    )


def test_specific_errors_are_preserved_others_become_foxt0003() -> None:
    coded = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='1 div 0'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    with pytest.raises(XsltDynamicError) as coded_caught:
        run_transform({"stylesheet-text": coded})
    assert coded_caught.value.code == "FOAR0001"
    assert isinstance(coded_caught.value.cause, ElementPathZeroDivisionError)
    assert "FOAR0001" in str(coded_caught.value)

    with pytest.raises(XsltError) as static_caught:
        run_transform({"stylesheet-text": "<not-a-stylesheet/>"})
    assert static_caught.value.code is not None
    assert str(static_caught.value.code).startswith("XTSE")

    extensions = XsltExtensions().register_function(
        f"{{{APP}}}explode",
        0,
        lambda: (_ for _ in ()).throw(RuntimeError("exploded")),
    )
    crashing = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:app="urn:app" version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='app:explode()'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    with pytest.raises(XsltDynamicError, match="FOXT0003") as wrapped:
        run_transform({"stylesheet-text": crashing}, extensions=extensions)
    assert "exploded" in str(wrapped.value)


# ---------------------------------------------------------------------------
# Reentry, stable context values, caching, and budgets


def test_reentry_and_recursive_nesting() -> None:
    level_three = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='3'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    level_two = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:xs="http://www.w3.org/2001/XMLSchema" version="3.0">'
        "<xsl:param name='sheet'/>"
        '<xsl:template name="xsl:initial-template">'
        "<xsl:variable name='inner' select=\"transform(map{"
        "'stylesheet-text': $sheet, 'delivery-format': 'raw'})?output\"/>"
        "<xsl:sequence select='2 + xs:integer($inner)'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    outer_two_deep = stylesheet(
        '<xsl:template name="main"><xsl:param name="options"/>'
        "<xsl:sequence select='transform($options)?output + 1'/>"
        "</xsl:template>",
        namespaces={"app": APP},
    )
    compiled = XsltProcessor().compile(io.BytesIO(outer_two_deep))
    result = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={
            "options": XdmMap.from_mapping(
                {
                    "stylesheet-text": level_two,
                    "stylesheet-params": {"sheet": XdmAtomic.string(level_three)},
                    "delivery-format": "raw",
                }
            )
        },
    )
    assert result.principal == (XdmAtomic.integer(6),)

    recursive = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'xmlns:app="urn:app" xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='app:count-down(3)'/>"
        "</xsl:template>"
        "<xsl:function name='app:count-down' as='xs:integer'>"
        "<xsl:param name='n' as='xs:integer'/>"
        "<xsl:sequence select='if ($n le 0) then 0 else app:count-down($n - 1) + 1'/>"
        "</xsl:function></xsl:stylesheet>"
    )
    runner = stylesheet(
        '<xsl:template name="main"><xsl:param name="options"/>'
        "<xsl:sequence select='transform($options)?output'/>"
        "</xsl:template>",
        namespaces={"app": APP},
    )
    compiled = XsltProcessor().compile(io.BytesIO(runner))
    depth_runner = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={
            "options": XdmMap.from_mapping({"stylesheet-text": recursive, "delivery-format": "raw"})
        },
    )
    assert depth_runner.principal == (XdmAtomic.integer(3),)


def test_stable_outer_datetime_and_timezone() -> None:
    datetime_sheet = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='string(current-dateTime())'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    timezone_sheet = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select='string(implicit-timezone())'/>"
        "</xsl:template></xsl:stylesheet>"
    )
    outer = stylesheet(
        '<xsl:template name="main">'
        "<xsl:param name='options'/><xsl:param name='tz-options'/>"
        "<xsl:variable name='before' select='string(current-dateTime())'/>"
        "<xsl:variable name='outer-tz' select='string(implicit-timezone())'/>"
        "<xsl:variable name='nested-dt' select='transform($options)?output'/>"
        "<xsl:variable name='nested-tz' select='transform($tz-options)?output'/>"
        "<xsl:sequence select='($before = $nested-dt, $outer-tz = $nested-tz)'/>"
        "</xsl:template>",
        namespaces={"app": APP},
    )
    pinned = timedelta(hours=2)
    compiled = XsltProcessor(implicit_timezone=pinned).compile(io.BytesIO(outer))
    time.sleep(0.002)
    result = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={
            "options": XdmMap.from_mapping(
                {"stylesheet-text": datetime_sheet, "delivery-format": "raw"}
            ),
            "tz-options": XdmMap.from_mapping(
                {"stylesheet-text": timezone_sheet, "delivery-format": "raw"}
            ),
        },
    )
    values = result.principal
    assert values == (XdmAtomic.boolean(True), XdmAtomic.boolean(True))


def test_compiled_artifacts_are_cached_results_are_not(tmp_path: Path) -> None:
    import pyxsd.xslt.nested as nested_module

    location = tmp_path / "cached.xsl"
    location.write_text(NESTED_SHEET, encoding="utf-8")
    options = XdmMap.from_mapping({"stylesheet-location": str(location), "delivery-format": "raw"})
    outer = stylesheet(OUTER_OPTIONS_TEMPLATE, namespaces={"app": APP})
    compiled = XsltProcessor().compile(io.BytesIO(outer))
    cache = nested_module._COMPILED_CACHE
    before_keys = frozenset(cache)
    first = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={"options": options},
    )
    second = compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={"options": options},
    )

    new_keys = frozenset(cache) - before_keys
    assert len(new_keys) == 1
    entry = cache[next(iter(new_keys))]
    assert isinstance(entry.compiled, CompiledStylesheet)

    first_node = first.principal[0].get(XdmAtomic.string("output"))[0]
    second_node = second.principal[0].get(XdmAtomic.string("output"))[0]
    assert isinstance(first_node, XdmNode)
    assert first_node is not second_node

    compiled.transform(
        initial_template="main",
        delivery="raw",
        template_parameters={
            "options": XdmMap.from_mapping({"stylesheet-location": str(location), "cache": False})
        },
    )
    assert frozenset(cache) - before_keys == new_keys


class DenyAllReads:
    """A resource policy that denies every read with a tagged reason."""

    def read(self, source: str, *, base: str | None, purpose: str) -> object:
        raise ResourcePolicyError("resource read denied by policy")


class DenyRemoteReads:
    """A resource policy that refuses remote references and loads the rest.

    Stylesheet text sources arrive in memory, so the nested stylesheet itself
    compiles; any ``https:`` reference the stylesheet makes is refused.
    """

    def read(self, source: str, *, base: str | None, purpose: str) -> object:
        name = source if isinstance(source, str) else getattr(source, "name", None)
        if isinstance(name, str) and urlsplit(name).scheme in ("http", "https"):
            raise ResourcePolicyError("resource read denied by policy")
        from pyxsd.resource_policy import ResourceData
        from pyxsd.xdm.document import CaptureSource

        captured = CaptureSource.from_input(source)
        return ResourceData(
            source_id=captured.source_name or "<memory>",
            content=captured.data,
        )


class DenyOutputs:
    """An output policy that denies every destination."""

    def authorize(self, uri: str, *, principal: bool) -> None:
        raise ResourcePolicyError(f"output denied: {uri}")


def test_denied_resources_schemas_and_outputs() -> None:
    reading = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        "<xsl:sequence select=\"doc('https://example.test/data.xml')\"/>"
        "</xsl:template></xsl:stylesheet>"
    )
    watched = XsltProcessor(
        resource_policy=DenyRemoteReads(), remote_resource_policy=DenyAllReads()
    )
    with pytest.raises(ResourcePolicyError, match="resource read denied"):
        run_transform({"stylesheet-text": reading}, processor=watched)

    importing = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:import-schema namespace="urn:typed" '
        'schema-location="https://example.test/typed.xsd"/>'
        '<xsl:template name="xsl:initial-template"><out/></xsl:template>'
        "</xsl:stylesheet>"
    )
    with pytest.raises(XsltError) as schema_denied:
        run_transform({"stylesheet-text": importing}, processor=watched)
    assert schema_denied.value.code == "XTSE1650"
    assert isinstance(schema_denied.value.cause, ResourcePolicyError)

    writing = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/denied.xml">'
        "<x/></xsl:result-document>"
        "</xsl:template></xsl:stylesheet>"
    )
    guarded = XsltProcessor(output_policy=DenyOutputs())
    with pytest.raises(ResourcePolicyError, match="output denied"):
        run_transform({"stylesheet-text": writing}, processor=guarded)


#: Calibrated so one nested run of ``HEAVY_SHEET`` fits while the outer
#: template's own dispatch plus a second identical nested run exceeds the
#: same ledger (the usable window is 128..253; the midpoint is chosen).
CUMULATIVE_STEP_LIMIT = 190


def _twice_stylesheet() -> str:
    return stylesheet(
        '<xsl:template name="main"><xsl:param name="options"/>'
        "<xsl:sequence select='transform($options)?output, "
        "transform($options)?output'/>"
        "</xsl:template>",
        namespaces={"app": APP},
    )


def test_cumulative_limits_are_shared_and_never_reset() -> None:
    options = XdmMap.from_mapping({"stylesheet-text": HEAVY_SHEET, "delivery-format": "raw"})
    single = stylesheet(
        '<xsl:template name="main"><xsl:param name="options"/>'
        "<xsl:sequence select='transform($options)?output'/>"
        "</xsl:template>",
        namespaces={"app": APP},
    )
    fits = (
        XsltProcessor()
        .compile(io.BytesIO(single))
        .transform(
            initial_template="main",
            delivery="raw",
            template_parameters={"options": options},
            limits=ExecutionLimits(max_steps=CUMULATIVE_STEP_LIMIT),
        )
    )
    assert fits.principal == (XdmAtomic.integer(40),)

    compiled = XsltProcessor().compile(io.BytesIO(_twice_stylesheet()))
    with pytest.raises(XsltError) as exhausted:
        compiled.transform(
            initial_template="main",
            delivery="raw",
            template_parameters={"options": options},
            limits=ExecutionLimits(max_steps=CUMULATIVE_STEP_LIMIT),
        )
    assert exhausted.value.code == PYXSD_FRAME_EXHAUSTED


# ---------------------------------------------------------------------------
# Option validation


def test_invalid_option_shapes() -> None:
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "package-text": _package_source()})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "delivery-format": "wrapped"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "post-process": "not-a-function"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "stylesheet-params": "not-a-map"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "function-params": "not-an-array"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "base-output-uri": "relative/no-base"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "enable-assertions": "maybe"})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "vendor-options": 5})


def test_invocation_validation_is_shared_not_forked() -> None:
    """Function entry plus template params flows through the Task 16 validator."""
    with pytest.raises(XsltDynamicError, match="FOXT0002") as conflicting:
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "initial-function": qname(APP, "answer"),
                "function-params": XdmArray((XdmAtomic.integer(1),)),
                "template-params": {"label": "entry"},
            }
        )
    assert isinstance(conflicting.value.__cause__, XsltInvocationError)

    with pytest.raises(XsltDynamicError, match="FOXT0002") as orphaned:
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "function-params": XdmArray((XdmAtomic.integer(1),)),
            }
        )
    assert "initial_function" in str(orphaned.value.__cause__)


def test_unsupported_requests_report_foxt0001() -> None:
    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform({"stylesheet-text": NESTED_SHEET, "enable-assertions": False})
    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "requested-properties": {"xsl:product-name": "someone-else"},
            }
        )


def test_unlisted_keys_and_supported_toggles_are_ignored() -> None:
    result = run_transform(
        {
            "stylesheet-text": NESTED_SHEET,
            "unknown-future-option": True,
            "enable-messages": True,
            "enable-trace": True,
            "vendor-options": {"urn:vendor:option": 1},
            "requested-properties": {"xsl:product-name": "pyxsd"},
            "cache": True,
        }
    )
    assert map_get(result, "output")[0].root_element.local_name == "out"


def test_transform_outside_a_transformation_reports_foxt0001() -> None:
    from pyxsd.xslt.nested import invoke_nested_transform

    with pytest.raises(XsltDynamicError) as unavailable:
        invoke_nested_transform(object(), XdmMap.empty(), base_uri=None)
    assert unavailable.value.code == "FOXT0001"


def test_options_must_be_a_single_map() -> None:
    from pyxsd.xslt.nested import invoke_nested_transform

    with pytest.raises(XsltDynamicError) as not_a_map:
        invoke_nested_transform(object(), "options", base_uri=None)  # type: ignore[arg-type]
    assert not_a_map.value.code == "FOXT0002"


def test_package_version_requires_name() -> None:
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "package-version": "2.0"})


def test_package_name_without_a_resolver_reports_foxt0001() -> None:
    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform({"package-name": "https://example.test/lib.xsl"})


def test_source_node_drives_apply_templates_invocation() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        "<xsl:template match='leaf'><picked/></xsl:template>"
        "</xsl:stylesheet>"
    )
    source = XdmDocument.parse(b"<doc><branch><leaf/><leaf/></branch></doc>")
    result = run_transform(
        {"stylesheet-text": nested, "source-node": source, "delivery-format": "raw"}
    )
    picked = result.get(XdmAtomic.string("output"))
    assert len(picked) == 2
    assert all(node.local_name == "picked" for node in picked)


def test_option_value_shapes() -> None:
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": XdmDocument.parse(NESTED_SHEET.encode())})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "delivery-format": XdmAtomic.string("raw"),
                "stylesheet-location": ("a", "b"),
            }
        )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-node": XdmAtomic.string("not-a-node")})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "initial-template": XdmAtomic.integer(1)})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform(
            {"stylesheet-text": NESTED_SHEET, "xslt-version": XdmAtomic.string("not-a-decimal")}
        )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "xslt-version": float("inf")})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "xslt-version": XdmDocument.parse(b"<d/>")})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "source-node": XdmAtomic.string("x")})
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform(
            {"stylesheet-text": NESTED_SHEET, "initial-template": XdmDocument.parse(b"<d/>")}
        )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform({"stylesheet-text": NESTED_SHEET, "serialization-params": "not-a-map"})


def test_transform_empty_argument_reports_foxt0002() -> None:
    outer = stylesheet(
        '<xsl:template name="main"><xsl:sequence select="transform(())"/></xsl:template>',
        namespaces={"app": APP},
    )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        XsltProcessor().compile(io.BytesIO(outer)).transform(
            initial_template="main", delivery="raw"
        )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "delivery-format": ("raw", "document"),
            }
        )


def test_requested_properties_validation() -> None:
    matched = run_transform(
        {
            "stylesheet-text": NESTED_SHEET,
            "requested-properties": {
                qname(XSLT_NS, "supports-streaming"): False,
                "xsl:product-name": "pyxsd",
                "xsl:version": "1.0",
                "urn:other:name": "ignored",
                "product-name": "ignored",
                "other:product-name": "ignored",
                "xsl:no-such-property": "ignored",
            },
        }
    )
    assert map_get(matched, "output")[0].root_element.local_name == "out"

    with pytest.raises(XsltDynamicError, match="FOXT0001"):
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "requested-properties": {qname(XSLT_NS, "product-name"): "someone-else"},
            }
        )
    with pytest.raises(XsltDynamicError, match="FOXT0002"):
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "requested-properties": {"xsl:product-name": ("a", "b")},
            }
        )


def test_post_process_arity_is_validated() -> None:
    from pyxsd.xdm import XdmFunction

    def one_argument(key):
        return key

    with pytest.raises(XsltDynamicError, match="FOXT0002") as wrong_arity:
        run_transform(
            {
                "stylesheet-text": NESTED_SHEET,
                "delivery-format": "raw",
                "post-process": XdmFunction(one_argument, arity=1),
            }
        )
    assert "post-process" in str(wrong_arity.value)


def test_secondary_results_in_every_delivery_format() -> None:
    nested = (
        '<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" '
        'version="3.0">'
        '<xsl:template name="xsl:initial-template">'
        '<xsl:result-document href="https://example.test/plain.xml">'
        "<plain/></xsl:result-document>"
        '<xsl:result-document href="https://example.test/rawtree.xml" build-tree="no">'
        "<rawtree/></xsl:result-document>"
        "<principal/></xsl:template>"
        "</xsl:stylesheet>"
    )
    serialized = run_transform({"stylesheet-text": nested, "delivery-format": "serialized"})
    assert (
        map_get(serialized, "https://example.test/plain.xml")[0].value
        == '<?xml version="1.0" encoding="UTF-8"?><plain/>'
    )

    document = run_transform({"stylesheet-text": nested, "delivery-format": "document"})
    plain = map_get(document, "https://example.test/plain.xml")[0]
    assert isinstance(plain, XdmDocument)
    assert plain.root_element.local_name == "plain"
    rawtree = map_get(document, "https://example.test/rawtree.xml")[0]
    assert isinstance(rawtree, XdmDocument)
    assert rawtree.root_element.local_name == "rawtree"


def test_delivered_entries_guard_and_inheritance_validation() -> None:
    from pyxsd.xslt.policy import NestedTransformationPolicy
    from pyxsd.xslt.results import delivered_entries
    from pyxsd.xslt.runtime.transformation import Transformation

    with pytest.raises(ValueError, match="delivery"):
        delivered_entries(Transformation(), delivery="stylesheet")

    with pytest.raises(TypeError, match="limits"):
        NestedTransformationPolicy(
            processor=None,
            limits=object(),
            budget=object(),
            current_datetime=None,
            implicit_timezone=None,
        )
    from pyxsd.xslt.runtime.frames import ExecutionLimits

    limits = ExecutionLimits()
    with pytest.raises(TypeError, match="budget"):
        NestedTransformationPolicy(
            processor=None,
            limits=limits,
            budget=object(),
            current_datetime=None,
            implicit_timezone=None,
        )
    with pytest.raises(TypeError, match="shared"):
        Transformation(shared=object())
