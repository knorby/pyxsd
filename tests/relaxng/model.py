"""Corpus case model and outcome taxonomy for RELAX NG conformance.

The pinned catalogs are inline XML manifests: each ``testCase`` carries a
correct or incorrect schema and zero or more valid or invalid instances.
:class:`CorpusCase` is the harness-level view of one assertion, with paths to
the materialized schema and instance documents.

An outcome deliberately separates a wrong verdict from an inability to reach
one.  A matching verdict is the only thing that satisfies an expectation;
unknown datatypes, compiler crashes, timeouts, and exhausted budgets are
adapter gaps, never accidental rejections.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CaseKind = Literal[
    "correct-schema",
    "incorrect-schema",
    "valid-instance",
    "invalid-instance",
]

CASE_KINDS: tuple[CaseKind, ...] = (
    "correct-schema",
    "incorrect-schema",
    "valid-instance",
    "invalid-instance",
)
SCHEMA_KINDS = frozenset({"correct-schema", "incorrect-schema"})
INSTANCE_KINDS = frozenset({"valid-instance", "invalid-instance"})

PROFILE_CORE = "core"
PROFILE_XSD_DATATYPE = "xsd-datatype"
PROFILE_COMPACT_SYNTAX = "compact-syntax"
PROFILE_DTD_COMPATIBILITY = "dtd-compatibility"

PROFILES = frozenset(
    {
        PROFILE_CORE,
        PROFILE_XSD_DATATYPE,
        PROFILE_COMPACT_SYNTAX,
        PROFILE_DTD_COMPATIBILITY,
    }
)
REQUIRED_PROFILES = frozenset({PROFILE_CORE, PROFILE_XSD_DATATYPE})
EXCLUDED_PROFILES = frozenset({PROFILE_COMPACT_SYNTAX, PROFILE_DTD_COMPATIBILITY})

#: Set to ``1`` to execute the pinned corpus.  Synthetic catalog tests always run.
RUN_FULL_CORPUS_ENV = "PYXSD_RUN_RELAXNG"


@dataclass(frozen=True)
class CorpusCase:
    """One auditable assertion from a pinned catalog.

    ``schema`` is always a materialized schema document.  ``instance`` is set
    exactly for instance assertions; schema assertions carry ``None``.
    """

    id: str
    kind: CaseKind
    schema: Path
    instance: Path | None
    profile: str

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("case id must be non-blank")
        if self.kind not in CASE_KINDS:
            raise ValueError(f"unknown case kind {self.kind!r}")
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}")
        if self.kind in INSTANCE_KINDS and self.instance is None:
            raise ValueError(f"{self.kind} case requires an instance path")
        if self.kind in SCHEMA_KINDS and self.instance is not None:
            raise ValueError(f"{self.kind} case must not carry an instance path")

    @property
    def is_required(self) -> bool:
        """Whether the declared profile makes this case release-required."""
        return self.profile in REQUIRED_PROFILES

    @property
    def is_excluded(self) -> bool:
        """Whether the declared profile excludes this case from the release."""
        return self.profile in EXCLUDED_PROFILES


class Outcome(enum.StrEnum):
    """The settled classification of one tested case."""

    #: The engine verdict matched the catalog expectation.
    PASS = "pass"
    #: The engine reached a verdict that contradicted the expectation.
    FAIL = "fail"
    #: No verdict was reached: unsupported behavior, crash, timeout, or a limit.
    ADAPTER_GAP = "adapter-gap"
    #: The case depends on a feature outside the declared profile.
    PROFILE_EXCLUDED = "profile-excluded"


def satisfies_expected(case: CorpusCase, outcome: Outcome) -> bool:
    """Whether *outcome* satisfies the catalog expectation for *case*.

    Only :attr:`Outcome.PASS` is a matching verdict.  In particular, an
    adapter gap is never a correct rejection of an incorrect schema or an
    invalid instance.
    """
    return outcome is Outcome.PASS


def corpus_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether full pinned-corpus execution was explicitly requested."""
    source = os.environ if environ is None else environ
    return source.get(RUN_FULL_CORPUS_ENV) == "1"
