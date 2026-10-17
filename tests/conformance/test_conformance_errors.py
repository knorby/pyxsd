"""Per-error-code conformance pins for the real-schema corpus.

Each schema's compilation outcome is pinned as an exact tally of report
error codes (plus its class count). The pins describe the *current*
outcome and pass by design; their gate value is in the parse-correctness
fixes that follow: each fix moves exactly the lines it resolves, in the
same commit, and nothing else may move.

Downloaded corpora (``fhir-single``, ``cda-r2``) skip when absent — run
``python -m tests.conformance.fetch`` to populate the cache.
"""

import pytest
from tests.conformance.fetch import SCHEMAS, entry_point
from tests.conformance.run import EXPECTED, errors_by_code

from pyxsd import Schema


def _available(name: str) -> bool:
    try:
        return entry_point(name).is_file()
    except Exception:
        return False


@pytest.mark.parametrize("name", list(SCHEMAS))
def test_conformance_errors_match_pinned_tally(name):
    if not _available(name):
        pytest.skip(
            f"conformance schema '{name}' not downloaded; run python -m tests.conformance.fetch"
        )
    schema = Schema.compile(str(entry_point(name)))
    expected = EXPECTED[name]
    assert errors_by_code(name, schema.report) == expected["errors_by_code"], "error tally drifted"
    assert len(schema.classes) == expected["classes"], "class count drifted"
