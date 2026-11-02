"""Download-dependent schema fixtures shared by healthcare example tests."""

import pytest


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
