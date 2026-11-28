"""Adversarial RELAX NG text and group derivatives never exhaust the stack.

Nesting through the first operand used to reach Python's recursion limit.  A
configured derivative budget trips first, and the unlimited path completes
iteratively.  The shared deep-shape builders live in
``relaxng_limits_support``.
"""

from __future__ import annotations

import pytest
from tests.relaxng_limits_support import (
    ADVERSARIAL_DEPTH,
    deep_first_operand,
    deep_text_schema,
    derivation_context,
    parse,
    root_content,
)

import pyxsd.relaxng.validator as validator
from pyxsd.relaxng import RelaxNGLimitError, RelaxNGLimits


def test_deep_text_derivatives_do_not_escape_as_recursion_errors() -> None:
    schema = deep_text_schema()

    document = parse(schema, "<r>text</r>")

    assert document.is_valid
    assert document.root._name_ == "r"


@pytest.mark.parametrize("budget", ["max_derivative_states", "max_memo_entries"])
def test_finite_budget_trips_before_the_python_recursion_limit(budget: str) -> None:
    schema = deep_text_schema()

    with pytest.raises(RelaxNGLimitError) as caught:
        parse(schema, "<r>text</r>", limits=RelaxNGLimits(**{budget: 64}))

    assert caught.value.budget == budget
    assert caught.value.configured == 64
    assert caught.value.observed > 64


def test_deep_group_open_derivatives_are_iterative() -> None:
    schema, _instance = deep_first_operand("group", ADVERSARIAL_DEPTH)
    content = root_content(schema)

    residual = validator.start_tag_open_derivative(content, "a0", derivation_context())

    assert type(residual).__name__ == "_After"
    with pytest.raises(RelaxNGLimitError) as caught:
        validator.start_tag_open_derivative(
            content,
            "a0",
            derivation_context(RelaxNGLimits(max_derivative_states=64)),
        )
    assert caught.value.budget == "max_derivative_states"
