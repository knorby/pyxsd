"""Tests for the large-schema conformance corpus fetcher.

The corpus itself is downloaded on demand into a gitignored cache; these
tests cover the manifest's shape, the in-repo (``local``) entries, and the
cache directory's gitignore status, all without network access.
"""

from tests.conformance.fetch import SCHEMAS, cache_dir, entry_point


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


def test_interrupted_extraction_leaves_no_trusted_cache(tmp_path, monkeypatch):
    import zipfile

    import pytest
    from tests.conformance import fetch as fetch_mod

    archive = tmp_path / "bench.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("bench/entry.xsd", "<xs:schema/>")
        zipped.writestr("bench/extra.xsd", "<xs:schema/>")

    def partial_then_die(self, path=None, members=None, pwd=None):
        # Extract the first member, then simulate an interruption
        # (disk full, Ctrl-C) partway through the archive.
        self.extract(self.namelist()[0], path=path, pwd=pwd)
        raise KeyboardInterrupt

    monkeypatch.setattr(zipfile.ZipFile, "extractall", partial_then_die)
    target = tmp_path / "bench"
    with pytest.raises(KeyboardInterrupt):
        fetch_mod._extract(archive, target)
    # The final directory must not exist: the skip-if-present logic
    # must never trust a partially extracted corpus, and the staging
    # directory must not linger either.
    assert not target.exists()
    assert not target.with_name("bench.tmp").exists()
