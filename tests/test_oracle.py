"""Cross-check of pyxsd's verdicts against an independent oracle.

This module is opt-in: it is skipped unless the environment variable
``PYXSD_RUN_ORACLE=1`` is set. When it *is* requested, a missing
``xmlschema`` installation fails the run rather than silently skipping
the check. Run it after ``uv sync --group dev``::

    PYXSD_RUN_ORACLE=1 uv run pytest tests/test_oracle.py -q

``xmlschema`` is a well-tested independent XSD 1.0 implementation. Where
the two disagree, the disagreement is printed with the case id and
direction so the result can be triaged. Internal crashes on either side
are failures: "both crashed" is not agreement. Each case runs under the
binding policy named by its manifest ``mode`` (``legacy`` by default), so
the ``namespaced`` cases exercise namespace-aware matching alongside the
oracle. The one remaining expected difference is a schema-composition
warning/error mismatch; pyxsd's other documented limitations (identity
XPath predicates, user facets, remote schemas) are not exercised by the
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
    reason="set PYXSD_RUN_ORACLE=1 to run the non-gating xmlschema oracle",
)

if _ORACLE_REQUESTED:
    try:
        import xmlschema
    except ImportError as exc:  # pragma: no cover - exercised only without the dev extra
        raise RuntimeError(
            "PYXSD_RUN_ORACLE=1 but the xmlschema oracle is not installed; "
            "install the dev dependency group (uv sync --group dev)"
        ) from exc
else:  # pragma: no cover - the skip above already short-circuits
    xmlschema = None  # type: ignore[assignment]

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
    """Returns ``(schema_valid, instance_valid | None)`` for xmlschema."""
    try:
        schema = xmlschema.XMLSchema(str(directory / "schema.xsd"))
    except Exception:
        return False, None
    if "instance" not in case:
        return True, None
    try:
        return True, bool(schema.is_valid(str(directory / "instance.xml")))
    except Exception:
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
