"""PROJ repair follows the selected wheel's database and stays quiet on success."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from geoharness import _proj_fix


def _database(directory: Path, minor: int) -> str:
    directory.mkdir()
    with sqlite3.connect(directory / "proj.db") as connection:
        connection.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
        connection.execute(
            "INSERT INTO metadata VALUES ('DATABASE.LAYOUT.VERSION.MINOR', ?)", (str(minor),)
        )
    return str(directory)


@pytest.fixture(autouse=True)
def reset_proj_repair(monkeypatch):
    monkeypatch.setattr(_proj_fix, "_fixed", False)
    monkeypatch.delenv("PROJ_LIB", raising=False)
    monkeypatch.delenv("PROJ_DATA", raising=False)


def test_repaired_inherited_postgis_path_is_not_a_warning(tmp_path, monkeypatch, caplog):
    stale = _database(tmp_path / "postgis", 2)
    bundled = _database(tmp_path / "rasterio", 6)
    monkeypatch.setenv("PROJ_LIB", stale)
    monkeypatch.setattr(_proj_fix, "_rasterio_proj_dir", lambda: bundled)
    with caplog.at_level(logging.DEBUG):
        _proj_fix.ensure_proj_data()
    assert _proj_fix.os.environ["PROJ_LIB"] == bundled
    assert _proj_fix.os.environ["PROJ_DATA"] == bundled
    assert all(record.levelno < logging.WARNING for record in caplog.records)


def test_layout_requirement_comes_from_this_wheel(tmp_path, monkeypatch):
    stale = _database(tmp_path / "external", 2)
    bundled = _database(tmp_path / "rasterio", 4)
    monkeypatch.setenv("PROJ_LIB", stale)
    monkeypatch.setattr(_proj_fix, "_rasterio_proj_dir", lambda: bundled)
    _proj_fix.ensure_proj_data()
    assert _proj_fix.os.environ["PROJ_DATA"] == bundled


def test_proj_data_precedence_does_not_replace_valid_custom_path(tmp_path, monkeypatch):
    stale = _database(tmp_path / "postgis", 2)
    current = _database(tmp_path / "custom", 6)
    bundled = _database(tmp_path / "rasterio", 6)
    monkeypatch.setenv("PROJ_LIB", stale)
    monkeypatch.setenv("PROJ_DATA", current)
    monkeypatch.setattr(_proj_fix, "_rasterio_proj_dir", lambda: bundled)
    _proj_fix.ensure_proj_data()
    assert _proj_fix.os.environ["PROJ_DATA"] == current
    assert _proj_fix.os.environ["PROJ_LIB"] == stale


def test_stale_proj_data_is_repaired_even_when_proj_lib_is_valid(tmp_path, monkeypatch):
    stale = _database(tmp_path / "postgis", 2)
    current = _database(tmp_path / "custom", 6)
    bundled = _database(tmp_path / "rasterio", 6)
    monkeypatch.setenv("PROJ_DATA", stale)
    monkeypatch.setenv("PROJ_LIB", current)
    monkeypatch.setattr(_proj_fix, "_rasterio_proj_dir", lambda: bundled)
    _proj_fix.ensure_proj_data()
    assert _proj_fix.os.environ["PROJ_DATA"] == bundled


def test_missing_rasterio_bundle_does_not_claim_repair(monkeypatch, caplog):
    monkeypatch.setenv("PROJ_LIB", "missing-proj-directory")
    monkeypatch.setattr(_proj_fix, "_rasterio_proj_dir", lambda: None)
    _proj_fix.ensure_proj_data()
    assert _proj_fix.os.environ["PROJ_LIB"] == "missing-proj-directory"
    assert "using rasterio" not in caplog.text


def test_reading_bad_database_does_not_create_or_mutate_files(tmp_path):
    assert _proj_fix._proj_db_layout_minor(str(tmp_path)) is None
    assert list(tmp_path.iterdir()) == []
