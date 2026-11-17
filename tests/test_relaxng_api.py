"""Public RELAX NG support contracts: exceptions, budgets, and origins.

These tests pin the value semantics that every later compiler stage shares.
They deliberately avoid calling a compiler: the initial package defines the
contracts, not placeholder results.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from io import StringIO

import pytest

import pyxsd
import pyxsd.relaxng as relaxng
from pyxsd.exceptions import PyXSDError
from pyxsd.locations import IssueLocation, PathStep
from pyxsd.relaxng.annotations import NodeAnnotations, ScalarAnnotation
from pyxsd.relaxng.errors import (
    AmbiguousValueError,
    RelaxNGLimitError,
    StaleInterpretationError,
)
from pyxsd.relaxng.limits import BudgetCounter, RelaxNGLimits
from pyxsd.relaxng.origins import OriginTable, SourceOrigin
from pyxsd.relaxng.schema import RelaxNGSchema
from pyxsd.relaxng.values import AlternativeValue, ListInterpretation

LIMIT_FIELDS = tuple(field.name for field in fields(RelaxNGLimits))

LOCATION = IssueLocation(
    "schema.rng",
    (PathStep("{http://relaxng.org/ns/structure/1.0}element", 1), PathStep("name", 2)),
    "name",
)
OTHER_LOCATION = IssueLocation("included.rng", (PathStep("define", 1),))


def test_the_package_exposes_only_the_settled_public_contracts():
    assert set(relaxng.__all__) == {
        "AlternativeValue",
        "AmbiguousValueError",
        "ListInterpretation",
        "NodeAnnotations",
        "RelaxNGDocument",
        "RelaxNGElement",
        "RelaxNGLimitError",
        "RelaxNGLimits",
        "RelaxNGSchema",
        "ScalarAnnotation",
        "StaleInterpretationError",
        "compile",
    }
    assert relaxng.RelaxNGLimitError is RelaxNGLimitError
    assert relaxng.AmbiguousValueError is AmbiguousValueError
    assert relaxng.StaleInterpretationError is StaleInterpretationError
    assert relaxng.RelaxNGLimits is RelaxNGLimits
    assert relaxng.RelaxNGSchema is RelaxNGSchema
    assert relaxng.AlternativeValue is AlternativeValue
    assert relaxng.ListInterpretation is ListInterpretation
    assert relaxng.NodeAnnotations is NodeAnnotations
    assert relaxng.ScalarAnnotation is ScalarAnnotation


def test_the_package_exposes_the_real_compiler_entry_point():
    assert relaxng.compile.__func__ is RelaxNGSchema.compile.__func__
    schema = relaxng.compile(
        StringIO(
            '<grammar xmlns="http://relaxng.org/ns/structure/1.0">'
            '<start><element name="root"><empty/></element></start>'
            "</grammar>"
        )
    )
    assert isinstance(schema, RelaxNGSchema)
    assert not schema.report.has_errors


def test_relaxng_contracts_are_not_reexported_at_the_top_level():
    for name in relaxng.__all__:
        if name == "compile":
            # pyxsd.compile is the XSD compiler, never the RELAX NG one.
            assert pyxsd.compile is not relaxng.compile
            continue
        assert name not in pyxsd.__all__
        assert not hasattr(pyxsd, name)


def test_importing_relaxng_does_not_pull_xsd_machinery():
    script = (
        "import sys\n"
        "import pyxsd.relaxng\n"
        "pulled = {'pyxsd.element_representatives', 'pyxsd.content_model',"
        " 'pyxsd.instance_binding'}\n"
        "assert not pulled & set(sys.modules), sorted(pulled & set(sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "exception",
    [RelaxNGLimitError, AmbiguousValueError, StaleInterpretationError],
)
def test_every_public_exception_shares_the_pyxsd_root(exception):
    assert issubclass(exception, PyXSDError)


def test_limit_error_reports_stage_budget_configured_and_observed():
    error = RelaxNGLimitError("compile", "max_pattern_nodes", 2, 3)

    assert (error.stage, error.budget, error.configured, error.observed) == (
        "compile",
        "max_pattern_nodes",
        2,
        3,
    )
    message = str(error)
    for part in ("compile", "max_pattern_nodes", "2", "3"):
        assert part in message


@pytest.mark.parametrize("attribute", ["stage", "budget", "configured", "observed"])
def test_limit_error_attributes_are_read_only(attribute):
    error = RelaxNGLimitError("compile", "max_pattern_nodes", 2, 3)

    with pytest.raises(AttributeError):
        setattr(error, attribute, "changed")


def test_limit_error_survives_pickling_with_its_typed_attributes():
    error = RelaxNGLimitError("validate", "max_diagnostics", 5, 6)

    restored = pickle.loads(pickle.dumps(error))

    assert (restored.stage, restored.budget, restored.configured, restored.observed) == (
        "validate",
        "max_diagnostics",
        5,
        6,
    )
    assert str(restored) == str(error)


def test_limits_default_to_unlimited():
    limits = RelaxNGLimits()

    assert all(getattr(limits, name) is None for name in LIMIT_FIELDS)

    counter = BudgetCounter("compile", limits)
    for _ in range(100):
        counter.consume("max_pattern_nodes", 1000)

    missing = BudgetCounter("compile", None)
    missing.consume("max_derivative_states", 10_000)


@pytest.mark.parametrize("name", LIMIT_FIELDS)
@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "2"])
def test_each_budget_rejects_values_outside_non_negative_integers(name, bad):
    with pytest.raises(ValueError, match=name):
        RelaxNGLimits(**{name: bad})


@pytest.mark.parametrize("name", LIMIT_FIELDS)
def test_zero_is_accepted_and_exhausts_on_the_first_unit(name):
    limits = RelaxNGLimits(**{name: 0})

    assert getattr(limits, name) == 0
    with pytest.raises(RelaxNGLimitError) as excinfo:
        BudgetCounter("compile", limits).consume(name)

    assert (excinfo.value.stage, excinfo.value.budget, excinfo.value.observed) == (
        "compile",
        name,
        1,
    )


def test_exceeding_max_pattern_nodes_reports_the_third_observation():
    counter = BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=2))

    counter.consume("max_pattern_nodes")
    counter.consume("max_pattern_nodes")
    with pytest.raises(RelaxNGLimitError) as excinfo:
        counter.consume("max_pattern_nodes")

    error = excinfo.value
    assert (error.stage, error.budget, error.configured, error.observed) == (
        "compile",
        "max_pattern_nodes",
        2,
        3,
    )
    assert "compile" in str(error)
    assert "max_pattern_nodes" in str(error)
    assert "observed 3" in str(error)


def test_one_consumption_past_the_budget_reports_the_total():
    counter = BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=2))

    with pytest.raises(RelaxNGLimitError) as excinfo:
        counter.consume("max_pattern_nodes", 3)

    assert excinfo.value.observed == 3


def test_budgets_are_counted_independently_and_inclusively():
    counter = BudgetCounter("validate", RelaxNGLimits(max_pattern_nodes=1, max_diagnostics=2))

    counter.consume("max_pattern_nodes")
    counter.consume("max_diagnostics", 2)

    with pytest.raises(RelaxNGLimitError) as excinfo:
        counter.consume("max_diagnostics")

    assert excinfo.value.stage == "validate"
    assert excinfo.value.budget == "max_diagnostics"
    assert excinfo.value.observed == 3


def test_consume_accepts_zero_as_a_no_op():
    counter = BudgetCounter("compile", RelaxNGLimits(max_pattern_nodes=0))

    counter.consume("max_pattern_nodes", 0)

    with pytest.raises(RelaxNGLimitError):
        counter.consume("max_pattern_nodes")


@pytest.mark.parametrize("amount", [True, False, -1, 1.0, "1"])
def test_consume_rejects_invalid_amounts(amount):
    counter = BudgetCounter("compile", RelaxNGLimits())

    with pytest.raises(ValueError, match="amount"):
        counter.consume("max_pattern_nodes", amount)


def test_consume_rejects_an_unknown_budget_name():
    counter = BudgetCounter("compile", RelaxNGLimits())

    with pytest.raises(ValueError, match="unknown"):
        counter.consume("max_nodes")


def test_source_origin_retains_the_exact_issue_location():
    origin = SourceOrigin("schema.rng", LOCATION)

    assert origin.source_id == "schema.rng"
    assert origin.location is LOCATION
    assert SourceOrigin("included.rng", None).location is None


def test_source_origin_is_frozen_and_hashable():
    origin = SourceOrigin("schema.rng", LOCATION)

    with pytest.raises(FrozenInstanceError):
        origin.source_id = "other.rng"

    assert {origin, SourceOrigin("schema.rng", LOCATION)} == {origin}


def test_origin_table_is_a_defensive_immutable_set():
    origins = [SourceOrigin("schema.rng", LOCATION), SourceOrigin("included.rng", None)]
    table = OriginTable(origins)
    origins.append(SourceOrigin("late.rng", OTHER_LOCATION))

    assert len(table) == 2
    assert set(table) == {
        SourceOrigin("schema.rng", LOCATION),
        SourceOrigin("included.rng", None),
    }
    assert SourceOrigin("schema.rng", LOCATION) in table
    assert SourceOrigin("late.rng", OTHER_LOCATION) not in table
    assert next(origin for origin in table if origin.source_id == "schema.rng").location is LOCATION

    with pytest.raises(FrozenInstanceError):
        table.origins = frozenset()


def test_origin_table_union_returns_a_new_table():
    left = OriginTable([SourceOrigin("schema.rng", LOCATION)])
    right = OriginTable(
        [SourceOrigin("schema.rng", LOCATION), SourceOrigin("included.rng", OTHER_LOCATION)]
    )

    combined = left.union(right)

    assert set(combined) == {
        SourceOrigin("schema.rng", LOCATION),
        SourceOrigin("included.rng", OTHER_LOCATION),
    }
    assert set(left) == {SourceOrigin("schema.rng", LOCATION)}
    assert len(right) == 2
    assert combined == OriginTable(combined)
    assert hash(combined) == hash(OriginTable(set(combined)))
    assert not OriginTable()
