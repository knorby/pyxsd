"""Shared fixtures and helpers for the XSLT conformance-gate contract tests.

The contract, the checked-in baseline reproduction pins, and the parallel
worker probes share these synthetic repositories, baseline writers, and
deadline payloads.  Test modules import the names they use explicitly;
nothing here is a test.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from tests import report_xslt
from tests.xslt import baseline as baseline_module
from tests.xslt import suite as suite_module
from tests.xslt.catalog import load_catalog
from tests.xslt.suite import _run_payload, execute_case

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "tests" / "xslt" / "corpus" / "catalog.xml"
GATE_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
</catalog>
"""

GATE_TEST_SET = """\
<?xml version="1.0" encoding="UTF-8"?>
<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="gate">
  <environment name="gate-env">
    <source role="."><content><![CDATA[<doc/>]]></content></source>
  </environment>
{environments}
{cases}
</test-set>
"""

TWO_FAMILY_CATALOG = """\
<?xml version="1.0" encoding="UTF-8"?>
<catalog xmlns="http://www.w3.org/2012/10/xslt-test-catalog">
  <test-set name="gate" file="gate/_gate-test-set.xml"/>
  <test-set name="other" file="other/_other-test-set.xml"/>
</catalog>
"""

OTHER_TEST_SET = """\
<?xml version="1.0" encoding="UTF-8"?>
<test-set xmlns="http://www.w3.org/2012/10/xslt-test-catalog" name="other">
  <environment name="other-env">
    <source role="."><content><![CDATA[<doc/>]]></content></source>
  </environment>
  <test-case name="other-001">
    <environment ref="other-env"/>
    <test><stylesheet file="other-001.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
</test-set>
"""

GATE_CASE = """\
  <test-case name="{name}">
    <environment ref="gate-env"/>
    <test><stylesheet file="{name}.xsl"/></test>
    <result><assert-xml><![CDATA[<out>right</out>]]></assert-xml></result>
  </test-case>
"""

SHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:template match="/"><out>wrong</out></xsl:template>
</xsl:stylesheet>
"""

RIGHT_SHEET = SHEET.replace("wrong", "right")


def fixture_repo(tmp_path: Path, names: tuple[str, ...] = ("case-001",)) -> Path:
    """A synthetic repo with one test set, its stylesheets, and no baseline."""
    root = tmp_path / "repo"
    corpus = root / "tests" / "xslt" / "corpus"
    gate = corpus / "gate"
    gate.mkdir(parents=True)
    (corpus / "catalog.xml").write_text(GATE_CATALOG, encoding="utf-8")
    (gate / "_gate-test-set.xml").write_text(
        GATE_TEST_SET.format(
            environments="",
            cases="".join(GATE_CASE.format(name=name) for name in names),
        ),
        encoding="utf-8",
    )
    for name in names:
        (gate / f"{name}.xsl").write_text(SHEET, encoding="utf-8")
    (root / "tests" / "xslt" / "baselines").mkdir(parents=True)
    return root


def two_family_repo(tmp_path: Path) -> Path:
    """A synthetic repo with ``gate/`` (two cases) and ``other/`` (one case)."""
    root = fixture_repo(tmp_path, names=("case-001", "case-002"))
    corpus = root / "tests" / "xslt" / "corpus"
    (corpus / "catalog.xml").write_text(TWO_FAMILY_CATALOG, encoding="utf-8")
    other = corpus / "other"
    other.mkdir()
    (other / "_other-test-set.xml").write_text(OTHER_TEST_SET, encoding="utf-8")
    (other / "other-001.xsl").write_text(SHEET, encoding="utf-8")
    return root


def baseline_file(root: Path, profile: str = "xslt30") -> Path:
    return root / "tests" / "xslt" / "baselines" / f"{profile}.toml"


def write_baseline(root: Path, profile: str, cases: dict[str, str], **meta: object) -> Path:
    path = baseline_file(root, profile)
    catalog = load_catalog(root / "tests" / "xslt" / "corpus" / "catalog.xml")
    recorded = baseline_module.Baseline(
        meta={
            "schema": baseline_module.BASELINE_SCHEMA,
            "corpus": catalog.corpus_revision,
            "profile": profile,
            "timeout_seconds": report_xslt.DEFAULT_TIMEOUT,
            **meta,
        },
        cases=cases,
    )
    baseline_module.write_baseline(path, recorded)
    return path


def deadline_case(case, profile):
    if case.local_name == "case-001":
        start = Barrier(2)

        def backtrack():
            try:
                Path(os.environ["PYXSD_TEST_XSLT_WORKER_PID"]).write_text(
                    str(os.getpid()), encoding="ascii"
                )
                start.wait()
                # The C matcher holds the GIL here. Its signal checks cannot
                # dispatch the Python alarm handler from this non-main thread.
                re.fullmatch(r"(a+)+", "a" * 80 + "!")
                raise AssertionError("the backtracking probe completed")
            except BaseException:
                start.abort()
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            searching = executor.submit(backtrack)
            start.wait()
            return searching.result()
    return execute_case(case, profile)


def deadline_worker(payload):
    with patch.object(suite_module, "execute_case", deadline_case):
        return _run_payload(payload)


STUCK_SIBLING = None


def receive_stuck_sibling(event):
    """Pool initializer: hand every worker the event its stuck sibling sets."""
    global STUCK_SIBLING
    STUCK_SIBLING = event


def unpicklable_payload(payload):
    """One case returns an unpicklable result; its sibling hangs in the C matcher."""
    _profile_name, _unicode_version, case, _timeout = payload
    if case.local_name == "case-002":
        STUCK_SIBLING.set()
        # The C matcher holds the GIL here; the Python alarm cannot run.
        re.fullmatch(r"(a+)+", "a" * 80 + "!")
        raise AssertionError("the backtracking probe completed")
    STUCK_SIBLING.wait()
    return case.name, lambda: None


def clear_ambient_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every fixture's budget hermetic: the test sets it, never the shell."""
    monkeypatch.delenv("PYXSD_XSLT_TIMEOUT", raising=False)
