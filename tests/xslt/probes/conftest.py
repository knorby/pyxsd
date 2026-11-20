"""Fixtures shared by the XPath substrate probes."""

from __future__ import annotations

import elementpath
import pytest

from pyxsd.xpath.capabilities import CapabilityOutcome, run_xpath_capabilities


@pytest.fixture(scope="session")
def installed_elementpath_version() -> str:
    """The elementpath release the probe and QT3 reports describe."""
    return elementpath.__version__


@pytest.fixture(scope="session")
def xpath31_outcomes() -> dict[str, CapabilityOutcome]:
    """The XPath 3.1 capability matrix, keyed by capability name."""
    return {outcome.name: outcome for outcome in run_xpath_capabilities("3.1")}
