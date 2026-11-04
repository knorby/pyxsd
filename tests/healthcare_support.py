"""Download-dependent schema fixtures shared by healthcare example tests."""

from pathlib import Path

import pytest


def document_path(name):
    """Use unchanged offline test fixtures, never a developer's downloaded corpus."""
    filenames = {
        "discharge": "hl7-discharge-summary.xml",
        "ccd-with-issues": "hl7-ccd-with-issues.xml",
    }
    return Path(__file__).parent / "fixtures" / "healthcare" / filenames[name]


@pytest.fixture(scope="module")
def cda():
    from examples.healthcare.common.schemas import compile_cda

    try:
        return compile_cda()
    except FileNotFoundError:
        pytest.skip("fetch healthcare schemas with examples/healthcare/download_sources.py")


@pytest.fixture(scope="module")
def fhir():
    from examples.healthcare.common.schemas import compile_fhir

    try:
        return compile_fhir()
    except FileNotFoundError:
        pytest.skip("fetch healthcare schemas with examples/healthcare/download_sources.py")
