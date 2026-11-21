"""Release gates and the actual embedded changelog extraction command."""

import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    # BaseLoader leaves the YAML 1.2 key "on" as text, unlike YAML 1.1's bool.
    return yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_release_runs_quality_workflows_before_build_and_publishes_before_github():
    jobs = _workflow("release.yml")["jobs"]
    expected = {
        "core": "ci.yml",
        "mcp": "mcp.yml",
        "integrations": "optional-integrations.yml",
        "docs": "docs.yml",
    }
    assert set(jobs["build"]["needs"]) == set(expected)
    for job, filename in expected.items():
        assert jobs[job]["uses"] == f"./.github/workflows/{filename}"
        assert "workflow_call" in _workflow(filename)["on"]
    assert jobs["publish"]["needs"] == "build"
    assert set(jobs["github-release"]["needs"]) == {"build", "publish"}
    steps = jobs["github-release"]["steps"]
    assert steps[-1]["with"]["body_path"] == "release-notes.md"
    assert "generate_release_notes" not in steps[-1]["with"]


def test_reusable_workflow_concurrency_does_not_cancel_the_release():
    workflows = ["release.yml", "ci.yml", "mcp.yml", "optional-integrations.yml", "docs.yml"]
    groups = [
        _workflow(name)["concurrency"]["group"]
        .replace("${{ github.workflow }}", "Release")
        .replace("${{ github.ref }}", "refs/tags/v1.3.0")
        .lower()
        for name in workflows
    ]
    assert len(groups) == len(set(groups))


def test_core_coverage_and_xsts_run_as_concurrent_version_matrices():
    jobs = _workflow("ci.yml")["jobs"]
    coverage = next(
        step for step in jobs["test"]["steps"] if step.get("name") == "Run tests with coverage"
    )
    assert "-n 4" in coverage["run"]
    assert "--dist loadfile" in coverage["run"]
    assert "--cov" in coverage["run"]
    assert "--cov-branch" in coverage["run"]
    assert coverage["env"]["PYXSD_RUN_RELAXNG"] == "1"

    xsts_job = jobs["xsts"]
    assert xsts_job["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
        "3.13",
        "3.14",
        "3.15",
    ]
    assert xsts_job["strategy"]["fail-fast"] == "false"
    assert "needs" not in xsts_job
    assert "Run the XSD Test Suite" not in {step.get("name") for step in jobs["test"]["steps"]}
    xsts = next(step for step in xsts_job["steps"] if step.get("name") == "Run the XSD Test Suite")
    assert xsts["env"]["PYXSD_RUN_XSTS"] == "1"
    assert xsts["env"]["PYXSD_XSTS_TIMEOUT"] == "180"
    assert "PYXSD_RUN_RELAXNG" not in xsts["env"]


def test_registry_publication_is_separate_from_github_and_uses_oidc():
    jobs = _workflow("release.yml")["jobs"]
    registry = jobs["mcp-registry"]
    assert registry["needs"] == "publish"
    assert "mcp-registry" not in jobs["github-release"]["needs"]
    assert registry["permissions"] == {"contents": "read", "id-token": "write"}
    commands = [step["run"] for step in registry["steps"] if "run" in step]
    assert commands.index("./mcp-publisher validate") < commands.index("./mcp-publisher publish")
    assert "./mcp-publisher login github-oidc" in commands
    assert "--registry registry.json" in commands[-1]


@pytest.mark.parametrize(
    ("changelog", "success"),
    [
        (
            "# Changelog\n\n## [Unreleased]\n\n## [1.3.0] - 2026-11-05\n\n### Fixed\n\n- A fix.\n\n## [1.2.1] - 2026-10-20\nOld.\n",
            True,
        ),
        ("# Changelog\n\n## [Unreleased]\n", False),
        ("## [Unreleased]\n\n## [1.3.0] - 2026-11-05\n\n", False),
        ("## [Unreleased]\n\n## [1.3.0] - 2026-11-05\nA\n\n## [1.3.0] - 2026-11-05\nB\n", False),
    ],
)
def test_release_extracts_only_the_matching_nonempty_dated_changelog(tmp_path, changelog, success):
    steps = _workflow("release.yml")["jobs"]["build"]["steps"]
    command = next(
        step["run"] for step in steps if step.get("name") == "Extract curated release notes"
    )
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-e", "-c", command],
        cwd=tmp_path,
        env=dict(os.environ, GITHUB_REF_NAME="v1.3.0"),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success, result.stderr
    if success:
        assert (tmp_path / "release-notes.md").read_text(
            encoding="utf-8"
        ) == "### Fixed\n\n- A fix.\n"
    else:
        assert not (tmp_path / "release-notes.md").exists()
