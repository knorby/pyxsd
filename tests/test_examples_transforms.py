"""Tests for the example transform library and file-based transform loading."""

import importlib
import sys
from pathlib import Path

import pytest

from pyxsd.parser import PyXSD, _loadModuleFromFile
from pyxsd.transforms import Displayer, Transform

EXAMPLES_TRANSFORMS = Path(__file__).parent.parent / "examples" / "transforms"

MOVED_MODULES = [
    "atom",
    "vector",
    "bravais_lattice",
    "cell_sizer",
    "coord_viewer",
    "expand_cell",
    "format_for_visit",
    "sphere_cutter",
]


class TestPackageSurface:
    """The transform framework is importable from the package root."""

    def test_reexport(self):
        assert issubclass(Displayer, Transform)

    def test_import_shortcut(self):
        mod = importlib.import_module("pyxsd.transforms")
        assert mod.Transform is Transform
        assert mod.Displayer is Displayer

    @pytest.mark.parametrize("module", MOVED_MODULES)
    def test_crystallography_modules_left_the_package(self, module):
        """Breaking change: the sci transforms are no longer shipped."""
        with pytest.raises(ImportError):
            importlib.import_module(f"pyxsd.transforms.{module}")


class TestExampleLibraryFiles:
    """The moved library files exist and are loadable standalone."""

    @pytest.mark.parametrize("module", MOVED_MODULES)
    def test_file_exists(self, module):
        assert (EXAMPLES_TRANSFORMS / f"{module}.py").is_file()

    def test_load_cell_sizer_from_file(self):
        mod = _loadModuleFromFile("cell_sizer", EXAMPLES_TRANSFORMS / "cell_sizer.py")
        assert mod is not None
        assert issubclass(mod.CellSizer, Transform)

    def test_load_expand_cell_resolves_sibling_imports(self):
        """ExpandCell imports CellSizer by plain sibling name."""
        mod = _loadModuleFromFile("expand_cell", EXAMPLES_TRANSFORMS / "expand_cell.py")
        assert mod is not None
        assert issubclass(mod.ExpandCell, mod.CellSizer)
        assert issubclass(mod.ExpandCell, Transform)

    def test_load_coord_viewer_mixes_in_displayer(self):
        mod = _loadModuleFromFile("coord_viewer", EXAMPLES_TRANSFORMS / "coord_viewer.py")
        assert mod is not None
        assert issubclass(mod.CoordViewer, Transform)
        assert issubclass(mod.CoordViewer, Displayer)


class TestSiblingPathScoping:
    """Loading a file module adds its directory to sys.path only transiently."""

    def test_sys_path_restored(self):
        before = list(sys.path)
        marker = EXAMPLES_TRANSFORMS / "cell_sizer.py"
        _loadModuleFromFile("cell_sizer", marker)
        assert sys.path == before

    def test_sibling_directory_not_on_path_after_load(self):
        marker_dir = str(EXAMPLES_TRANSFORMS)
        _loadModuleFromFile("vector", EXAMPLES_TRANSFORMS / "vector.py")
        assert marker_dir not in sys.path


class TestSearchPathResolution:
    """getTransformModuleAndLoad finds example transforms via the CWD."""

    def test_resolved_from_examples_directory(self, monkeypatch):
        monkeypatch.chdir(EXAMPLES_TRANSFORMS)
        parser = PyXSD.__new__(PyXSD)
        parser.xmlPath = Path.cwd()
        module = parser.getTransformModuleAndLoad("ExpandCell")
        assert issubclass(module.ExpandCell, Transform)

    def test_not_found_from_repo_root(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        parser = PyXSD.__new__(PyXSD)
        parser.xmlPath = tmp_path
        with pytest.raises(ImportError, match="ExpandCell"):
            parser.getTransformModuleAndLoad("ExpandCell")
