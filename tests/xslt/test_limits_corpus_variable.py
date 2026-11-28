"""A pathological XSLT corpus case has a controlled, coded verdict.

The probe runs one catalog case in a child process under a five-second budget
and accepts either the catalog assertion or the documented timeout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.xslt.support import run_probe

_CORPUS = Path(__file__).with_name("corpus") / "catalog.xml"


@pytest.mark.skipif(not _CORPUS.is_file(), reason="XSLT corpus not checked out")
@pytest.mark.parametrize("name", ["variable/variable-0108"])
def test_pathological_corpus_case_has_a_controlled_verdict(name):
    run = run_probe(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from tests.xslt.catalog import load_catalog
from tests.xslt.suite import SUITE_PROFILES, run_parallel

case = load_catalog(sys.argv[1]).cases[sys.argv[2]]
(result,) = run_parallel((case,), SUITE_PROFILES["xslt30"], jobs=1, timeout=5)
print(json.dumps([result.name, result.outcome.value, result.message]))
""",
            str(_CORPUS),
            name,
        ],
        cwd=Path(__file__).resolve().parents[2],
        timeout=40,
    )
    assert "Traceback (most recent call last)" not in run.stderr
    actual_name, verdict, detail = json.loads(run.stdout)
    assert actual_name == name
    if name == "catalog/catalog-003" and verdict == "pass":
        return  # The corpus assertion already checked the result.
    assert verdict == "timeout"
    assert detail == "the case exceeded its 5 s budget"
