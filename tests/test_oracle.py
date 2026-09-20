"""Cross-check of pyxsd's verdicts against an independent oracle.

This module is opt-in locally: it is skipped unless the environment
variable ``PYXSD_RUN_ORACLE=1`` is set. The CI workflow runs it as a
required job with that variable set, and a missing ``xmlschema``
installation then fails the run rather than silently skipping the
check. Run it locally after ``uv sync --group dev``::

    PYXSD_RUN_ORACLE=1 uv run pytest tests/test_oracle.py -q

``xmlschema`` is a well-tested independent implementation. Where
the two disagree, the disagreement is printed with the case id and
direction so the result can be triaged. Internal crashes on either side
are failures: "both crashed" is not agreement. Only xmlschema's own
parse/namespace/validation errors are treated as verdicts; any other
exception fails the test. Each case runs under the binding policy named
by its manifest ``mode`` (``legacy`` by default), so the ``namespaced``
cases exercise namespace-aware matching alongside the oracle.

Cases flagged ``xsd11 = true`` in the manifest are XSD 1.1-only and are
compared against ``xmlschema.XMLSchema11``; the default oracle parser is
XSD 1.0 and rejects 1.1 constructs outright. A short list of intentional
per-case differences is kept in ``_DOCUMENTED_DIVERGENCES``, each with
its reason. pyxsd's other documented limitations (identity XPath
predicates, user facets, remote schemas) are not exercised by the
corpus.
"""

from __future__ import annotations

import os

import pytest

from conformance_runner import _errors, _materialize, case_mode, load_cases
from pyxsd.exceptions import PyXSDError
from pyxsd.schema import Schema

_ORACLE_REQUESTED = os.environ.get("PYXSD_RUN_ORACLE") == "1"

pytestmark = pytest.mark.skipif(
    not _ORACLE_REQUESTED,
    reason="set PYXSD_RUN_ORACLE=1 to run the xmlschema oracle (required in CI)",
)

if _ORACLE_REQUESTED:
    try:
        import xmlschema
    except ImportError as exc:  # pragma: no cover - exercised only without the dev extra
        raise RuntimeError(
            "PYXSD_RUN_ORACLE=1 but the xmlschema oracle is not installed; "
            "install the dev dependency group (uv sync --group dev)"
        ) from exc
    # The base of the errors xmlschema raises for its own parse,
    # validation, and lookup verdicts. Anything else is an internal
    # crash and must propagate.
    from xmlschema.exceptions import XMLSchemaException

    _ORACLE_ERRORS = (XMLSchemaException,)
else:  # pragma: no cover - the skip above already short-circuits
    xmlschema = None  # type: ignore[assignment]
    _ORACLE_ERRORS = ()

CASES = load_cases()

# Cases where pyxsd intentionally differs from the oracle. Listed per case
# id with the reason so an *unexpected* new disagreement still surfaces.
# Namespace, wildcard, and QName cases were previously listed here; they now
# run in the manifest's ``namespaced`` mode and agree with the oracle.
_DOCUMENTED_DIVERGENCES = {
    "assertions/assert-out-of-subset-invalid": (
        "pyxsd's assertion subset omits the document/collection accessors and "
        "rejects fn:doc at schema phase; XSD 1.1 eliminated the assertion "
        "XPath subset and evaluates fn:doc against an empty available-documents "
        "set (3.13.4.2), so xmlschema accepts the schema and can only fail at "
        "validation"
    ),
    "composition/include-missing": (
        "xmlschema downgrades a missing include to a warning; pyxsd reports "
        "the schema-compose error"
    ),
    "composition/import-namespace-mismatch-invalid": (
        "xmlschema accepts an import whose namespace does not match the "
        "imported schema's target namespace; pyxsd enforces src-import and "
        "reports the schema-compose error"
    ),
    "cta/alternative-out-of-subset-invalid": (
        "child::x is valid XPath 2.0 but outside the conditional-type-"
        "assignment required subset of XSD 1.1 3.12.6, which processors may "
        "but need not accept; pyxsd implements only the required subset and "
        "reports alternative-invalid, while xmlschema accepts it"
    ),
    "cta/instance-inheritable-attribute": (
        "XSD 1.1 3.3.5.6 inherited attributes (copied into the type-alternative "
        "context by 3.12.4) put the ancestor's kind='a' in scope: pyxsd selects "
        "ChapA and rejects <b>, while xmlschema does not inherit the attribute "
        "and matches no alternative"
    ),
    "cta/instance-xsi-type-overrides-alternative": (
        "XSD 1.1 3.3.4.1 lets an instance-specified xsi:type override "
        "conditional type assignment; pyxsd validates against the xsi:type, "
        "while xmlschema applies the alternative and rejects the content"
    ),
    "datatypes/empty-union-has-empty-value-space": (
        "XSD 1.1 permits a union with no member types (its value space is "
        "empty; bug 4912) and pyxsd enforces that empty value space; xmlschema "
        "rejects the schema with 'missing xs:union type declarations'"
    ),
    "legality/annotation-invalid-xml-lang": (
        "xmlschema does not validate the xml:lang lexical space of "
        "xs:documentation; the W3C annotF001 case expects invalid"
    ),
    "override/missing-component-not-added-invalid": (
        "both engines agree XSD 1.1 4.2.5 ignores an xs:override declaration "
        "that matches nothing (so 'ghost' is not added); pyxsd reports the "
        "dangling type reference as an instance-phase unknown-type, while "
        "xmlschema rejects the schema at parse"
    ),
    "structure/emptiable-choice-required-ref-valid": (
        "xmlschema's meta-schema rejects occurrence attributes on a model "
        "group inside a named xs:group; the schema-for-schemas allows them "
        "and the MS particlesHa valid control relies on it"
    ),
    "xsd11/pattern-name-char-complement-excludes-astral": (
        "the \\C complement is built from the XML 1.1 NameChar set, which "
        "includes astral characters [#x10000-#xEFFFF]; pyxsd follows it and "
        "rejects U+12000, while xmlschema accepts the astral value"
    ),
    "regex/word-class-includes-marks-valid": (
        "XSD 1.1 G.4.2 defines \\w as every character except the P/Z/C "
        "categories, so a combining mark is a word character; xmlschema uses "
        "Python's \\w, which excludes marks and rejects the value"
    ),
    "regex/word-class-excludes-underscore-invalid": (
        "XSD 1.1 G.4.2 defines \\w as every character except the P/Z/C "
        "categories, so underscore (Pc) is not a word character; xmlschema "
        "uses Python's \\w, which includes underscore and accepts the value"
    ),
    "regex/space-class-is-xsd-four-invalid": (
        "XSD 1.1 G.4.2 defines \\s as exactly [#x20\\t\\n\\r]; xmlschema uses "
        "Python's \\s, which also matches no-break space and accepts the value"
    ),
}


def _pyxsd_verdict(case, directory):
    """Returns ``(schema_valid, instance_valid | None)`` for pyxsd."""
    has_instance = "instance" in case
    try:
        schema = Schema.compile(str(directory / "schema.xsd"), mode=case_mode(case))
    except PyXSDError:
        # pyxsd reports an unresolvable schema as a verdict, not a crash.
        return False, None
    except Exception as exc:
        # An internal crash is never agreement with the oracle.
        pytest.fail(f"pyxsd raised {type(exc).__name__} for {case['id']}: {exc}")
    schema_ok = not _errors(schema.report)
    if not has_instance:
        return schema_ok, None
    doc = schema.parse(str(directory / "instance.xml"))
    instance_ok = not _errors(doc.report.for_phase("instance"))
    return schema_ok, instance_ok


def _oracle_verdict(case, directory):
    """Returns ``(schema_valid, instance_valid | None)`` for xmlschema.

    Only xmlschema's own parse/validation errors become verdicts; any
    other exception propagates so an oracle crash cannot masquerade as
    agreement with pyxsd.
    """
    # XSD 1.1-only cases (``xsd11 = true`` in the manifest) are compared
    # against the 1.1 parser; the default oracle parser is XSD 1.0 and
    # rejects 1.1 constructs outright.
    oracle_cls = xmlschema.XMLSchema11 if case.get("xsd11") else xmlschema.XMLSchema
    try:
        schema = oracle_cls(str(directory / "schema.xsd"))
    except _ORACLE_ERRORS:
        return False, None
    if "instance" not in case:
        return True, None
    try:
        return True, bool(schema.is_valid(str(directory / "instance.xml")))
    except _ORACLE_ERRORS:
        # xmlschema raises on some instance errors (for example an
        # unresolvable xsi:type) instead of returning False.
        return True, False


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_oracle_agrees(case, tmp_path):
    if case["id"] in _DOCUMENTED_DIVERGENCES:
        pytest.skip(_DOCUMENTED_DIVERGENCES[case["id"]])
    directory = _materialize(case, tmp_path)
    pyxsd_schema, pyxsd_instance = _pyxsd_verdict(case, directory)
    oracle_schema, oracle_instance = _oracle_verdict(case, directory)

    if pyxsd_schema != oracle_schema:
        pytest.fail(
            f"schema verdict differs for {case['id']}: "
            f"pyxsd={pyxsd_schema}, xmlschema={oracle_schema}"
        )
    if (
        pyxsd_instance is not None
        and oracle_instance is not None
        and pyxsd_instance != oracle_instance
    ):
        pytest.fail(
            f"instance verdict differs for {case['id']}: "
            f"pyxsd={pyxsd_instance}, xmlschema={oracle_instance}"
        )


def test_oracle_harness_fails_on_an_oracle_crash(tmp_path, monkeypatch):
    """An unexpected xmlschema exception must fail, not become a verdict."""
    case = CASES[0]
    directory = _materialize(case, tmp_path)

    def exploding(*args, **kwargs):
        raise RuntimeError("oracle internal crash")

    monkeypatch.setattr(xmlschema, "XMLSchema", exploding)
    with pytest.raises(RuntimeError, match="oracle internal crash"):
        _oracle_verdict(case, directory)


def test_oracle_harness_fails_on_a_validation_crash(tmp_path, monkeypatch):
    """An unexpected exception from ``is_valid`` must fail the harness."""
    case = next(case for case in CASES if "instance" in case)
    directory = _materialize(case, tmp_path)

    class BrokenSchema:
        def is_valid(self, source):
            raise RuntimeError("validation internal crash")

    monkeypatch.setattr(xmlschema, "XMLSchema", lambda source: BrokenSchema())
    with pytest.raises(RuntimeError, match="validation internal crash"):
        _oracle_verdict(case, directory)
