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

import io
import os

import pytest

from conformance_runner import _errors, _materialize, case_mode, load_cases
from pyxsd.exceptions import PyXSDError
from pyxsd.parser import PyXSD

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
    "composition/include-missing": (
        "xmlschema downgrades a missing include to a warning; pyxsd reports "
        "the schema-compose error"
    ),
    "composition/import-namespace-mismatch-invalid": (
        "xmlschema accepts an import whose namespace does not match the "
        "imported schema's target namespace; pyxsd enforces src-import and "
        "reports the schema-compose error"
    ),
    "legality/annotation-invalid-xml-lang": (
        "xmlschema does not validate the xml:lang lexical space of "
        "xs:documentation; the W3C annotF001 case expects invalid"
    ),
    "structure/emptiable-choice-required-ref-valid": (
        "xmlschema's meta-schema rejects occurrence attributes on a model "
        "group inside a named xs:group; the schema-for-schemas allows them "
        "and the MS particlesHa valid control relies on it"
    ),
}


def _pyxsd_verdict(case, directory):
    """Returns ``(schema_valid, instance_valid | None)`` for pyxsd."""
    has_instance = "instance" in case
    instance_input: str | io.StringIO = (
        str(directory / "instance.xml") if has_instance else io.StringIO("<x/>")
    )
    try:
        parser = PyXSD(
            instance_input,
            str(directory / "schema.xsd"),
            xmlFileOutput=False,
            transformOutputName=None,
            mode=case_mode(case),
        )
    except PyXSDError:
        # pyxsd reports an unresolvable schema as a verdict, not a crash.
        return False, None
    except Exception as exc:
        # An internal crash is never agreement with the oracle.
        pytest.fail(f"pyxsd raised {type(exc).__name__} for {case['id']}: {exc}")
    schema_ok = not _errors(parser.report.for_phase("schema"))
    if not has_instance:
        return schema_ok, None
    instance_ok = not _errors(parser.report.for_phase("instance"))
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
