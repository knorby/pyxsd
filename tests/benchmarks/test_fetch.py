"""Tests for the large-schema benchmark corpus fetcher.

The corpus itself is downloaded on demand into a gitignored cache; these
tests cover the manifest's shape, the in-repo (``local``) entries, and the
cache directory's gitignore status, all without network access.
"""

from tests.benchmarks.fetch import SCHEMAS, cache_dir, entry_point


def test_manifest_entries_are_well_formed():
    for _name, entry in SCHEMAS.items():
        assert set(entry) >= {"url", "kind", "entry"}
        assert entry["kind"] in ("zip", "local")
        assert entry["entry"].endswith(".xsd")
        if entry["kind"] == "zip":
            assert entry["url"].startswith("https://")
        else:
            assert entry["url"] is None


def test_manifest_covers_the_planned_corpora():
    assert set(SCHEMAS) >= {
        "fhir-single",
        "cda-r2",
        "ooxml-wml",
        "ooxml-sml",
        "musicxml",
    }


def test_local_entries_resolve_to_existing_files():
    for name in ("ooxml-wml", "ooxml-sml", "musicxml"):
        assert entry_point(name).is_file(), name


def test_zip_entry_points_live_under_the_cache_dir():
    for name, entry in SCHEMAS.items():
        if entry["kind"] == "zip":
            assert entry_point(name).is_relative_to(cache_dir()), name


def test_cache_dir_is_gitignored():
    import subprocess

    # Create the directory first: the .gitignore pattern carries a
    # trailing slash (directories only), which git will not match
    # against a path that does not exist yet.
    cache_dir().mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "check-ignore", str(cache_dir())], capture_output=True)
    assert result.returncode == 0, f"cache dir not ignored: {cache_dir()}"


def test_entry_point_rejects_unknown_names():
    import pytest

    with pytest.raises(KeyError):
        entry_point("not-a-benchmark")
