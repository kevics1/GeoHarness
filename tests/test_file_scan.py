"""Regression tests for the workspace file-scan performance fix.

Previously a recursive scan walked `.venv` (tens of thousands of files) on
EVERY catalog call, making discovery take ~5.5s per call and compounding when
several tool calls ran in sequence. The scanner now prunes noise directories,
bounds depth, and caches results briefly.
"""

from __future__ import annotations

import time
from pathlib import Path

from geoharness.data.file_loader import _PRUNE_DIRS, FileLoader


def _geojson(path: Path, name: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature",'
        '"properties":{"n":1},"geometry":{"type":"Point","coordinates":[0,0]}}]}',
        encoding="utf-8",
    )
    return path


class TestScanPruning:
    def test_prunes_venv_directory(self, tmp_path: Path) -> None:
        """.venv contents must never be scanned."""
        _geojson(tmp_path / "数据" / "roads.geojson")
        # Decoy spatial file buried inside .venv
        _geojson(tmp_path / ".venv" / "lib" / "decoy.geojson")

        loader = FileLoader(file_dir=str(tmp_path))
        names = {p.name for p in loader._scan_files()}
        assert names == {"roads.geojson"}, names

    def test_prunes_git_and_caches(self, tmp_path: Path) -> None:
        _geojson(tmp_path / ".git" / "objects" / "blob.geojson")
        _geojson(tmp_path / "__pycache__" / "c.geojson")
        _geojson(tmp_path / "data" / "real.geojson")

        loader = FileLoader(file_dir=str(tmp_path))
        names = {p.name for p in loader._scan_files()}
        assert names == {"real.geojson"}, names

    def test_prune_set_covers_common_noise(self) -> None:
        for d in (".git", ".venv", "node_modules", "__pycache__", "site-packages"):
            assert d in _PRUNE_DIRS

    def test_deep_nesting_is_bounded(self, tmp_path: Path) -> None:
        """Paths deeper than max_depth are not scanned."""
        deep = tmp_path
        for i in range(10):
            deep = deep / f"lvl{i}"
        _geojson(deep / "too_deep.geojson")
        _geojson(tmp_path / "shallow.geojson")

        loader = FileLoader(file_dir=str(tmp_path), max_depth=3)
        names = {p.name for p in loader._scan_files()}
        assert "too_deep.geojson" not in names
        assert "shallow.geojson" in names

    def test_max_depth_reachable_within_limit(self, tmp_path: Path) -> None:
        _geojson(tmp_path / "a" / "b" / "ok.geojson")
        loader = FileLoader(file_dir=str(tmp_path), max_depth=4)
        assert {p.name for p in loader._scan_files()} == {"ok.geojson"}

    def test_still_finds_nested_user_data(self, tmp_path: Path) -> None:
        """Pruning must not break the normal workspace layout."""
        _geojson(tmp_path / "数据" / "受威胁居民点.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        assert loader._find_file("受威胁居民点") is not None


class TestScanCaching:
    def test_second_scan_is_cached(self, tmp_path: Path) -> None:
        _geojson(tmp_path / "a.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        first = loader._scan_files()
        assert loader._cache is not None
        second = loader._scan_files()
        assert first == second
        assert second is loader._cache

    def test_cache_is_cheap(self, tmp_path: Path) -> None:
        for i in range(30):
            _geojson(tmp_path / "d" / f"f{i}.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        loader._scan_files()  # warm
        t = time.time()
        loader._scan_files()
        assert time.time() - t < 0.05

    def test_missing_dir_is_empty_and_not_cached_crash(
        self, tmp_path: Path
    ) -> None:
        loader = FileLoader(file_dir=str(tmp_path / "nope"))
        assert loader._scan_files() == []

    def test_repeated_lookup_is_fast(self, tmp_path: Path) -> None:
        """Many lookups in a row must not each pay a full scan."""
        for i in range(10):
            _geojson(tmp_path / "数据" / f"layer{i}.geojson")
        loader = FileLoader(file_dir=str(tmp_path))
        loader._scan_files()

        t = time.time()
        for i in range(10):
            assert loader._find_file(f"layer{i}") is not None
        assert time.time() - t < 1.0


class TestScanPerformance:
    def test_scan_of_large_tree_is_fast(self, tmp_path: Path) -> None:
        """A tree with thousands of junk files must still scan quickly."""
        junk = tmp_path / ".venv" / "lib" / "site-packages" / "pkg"
        junk.mkdir(parents=True)
        for i in range(3000):
            (junk / f"mod{i}.py").write_text("x = 1\n", encoding="utf-8")
        _geojson(tmp_path / "数据" / "real.geojson")

        loader = FileLoader(file_dir=str(tmp_path))
        t = time.time()
        names = {p.name for p in loader._scan_files()}
        elapsed = time.time() - t
        assert names == {"real.geojson"}
        assert elapsed < 2.0, f"scan took {elapsed:.2f}s"
