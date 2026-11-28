"""Adversarial RELAX NG interleaved derivatives stay iterative.

An interleave nests through its first operand; the open derivative used to
recurse once per level.  The unlimited path completes, and a memo budget trips
before Python's recursion limit.  The shared deep-shape builders live in
``relaxng_limits_support``.
"""

from __future__ import annotations

import pytest
from tests.relaxng_limits_support import (
    ADVERSARIAL_DEPTH,
    deep_first_operand,
    derivation_context,
    root_content,
)

import pyxsd.relaxng.validator as validator
from pyxsd.relaxng import RelaxNGLimitError, RelaxNGLimits


def test_deep_interleaved_open_derivatives_are_iterative() -> None:
    schema, _instance = deep_first_operand("interleave", ADVERSARIAL_DEPTH)
    content = root_content(schema)

    residual = validator.start_tag_open_derivative(content, "a0", derivation_context())

    assert type(residual).__name__ == "_After"
    with pytest.raises(RelaxNGLimitError) as caught:
        validator.start_tag_open_derivative(
            content,
            "a0",
            derivation_context(RelaxNGLimits(max_derivative_states=100_000, max_memo_entries=64)),
        )
    assert caught.value.budget == "max_memo_entries"
