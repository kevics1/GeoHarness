"""Publication retries exercise real Windows sharing failures without recomputing GIS."""

from __future__ import annotations

import ctypes
import errno
import json
import os
from ctypes import wintypes
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import geopandas as gpd
import pytest
from shapely.geometry import Point

from geoharness.analysis import vector
from geoharness.config.settings import PublicationConfig
from geoharness.files import publication
from geoharness.files.publication import DirectoryPublicationError, publish_directory
from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource


def _hold_file(path: Path):
    """Hold a child file without FILE_SHARE_DELETE, as a reader may do on Windows."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 1 | 2, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    closed = False

    def release():
        nonlocal closed
        if not closed:
            kernel.CloseHandle(handle)
            closed = True

    return release


def _stage(tmp_path: Path):
    source = tmp_path / ".staging-result"
    source.mkdir()
    (source / "result.csv").write_text("value\n47\n", encoding="utf-8")
    return source, tmp_path / "published-result"


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file sharing semantics")
def test_publish_retries_real_child_file_lock_until_reader_releases(tmp_path, monkeypatch):
    source, target = _stage(tmp_path)
    release = _hold_file(source / "result.csv")
    sleeps = Mock(side_effect=lambda _: release())
    monkeypatch.setattr(publication.time, "sleep", sleeps)
    try:
        diagnostics = publish_directory(
            source, target, settings=PublicationConfig(max_attempts=3, retry_delay_seconds=0.01)
        )
    finally:
        release()
    assert (target / "result.csv").read_text(encoding="utf-8") == "value\n47\n"
    assert not source.exists()
    assert diagnostics["status"] == "published"
    assert diagnostics["attempts"] == 2
    assert diagnostics["errors"][0]["winerror"] in (5, 32)
    sleeps.assert_called_once_with(0.01)


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file sharing semantics")
def test_persistent_lock_fails_with_bounded_diagnostics_and_preserves_stage(tmp_path, monkeypatch):
    source, target = _stage(tmp_path)
    release = _hold_file(source / "result.csv")
    sleeps = Mock()
    monkeypatch.setattr(publication.time, "sleep", sleeps)
    try:
        with pytest.raises(DirectoryPublicationError) as raised:
            publish_directory(
                source, target,
                settings=PublicationConfig(max_attempts=3, retry_delay_seconds=0.01),
            )
    finally:
        release()
    assert raised.value.diagnostics["status"] == "failed"
    assert raised.value.diagnostics["attempts"] == 3
    assert len(raised.value.diagnostics["errors"]) == 3
    assert sleeps.call_count == 2
    assert (source / "result.csv").exists()
    assert not target.exists()
    assert str(source) in str(raised.value)


def test_non_transient_failure_is_not_retried(tmp_path, monkeypatch):
    source, target = _stage(tmp_path)
    sleeps = Mock()
    monkeypatch.setattr(publication.time, "sleep", sleeps)
    rename = Mock(side_effect=OSError(errno.ENOSPC, "Disk full"))
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(DirectoryPublicationError) as raised:
        publish_directory(source, target)
    rename.assert_called_once_with(target)
    sleeps.assert_not_called()
    assert raised.value.diagnostics["attempts"] == 1
    assert raised.value.diagnostics["errors"][0]["errno"] == errno.ENOSPC
    assert (source / "result.csv").exists()


def test_existing_destination_is_never_deleted_or_overwritten(tmp_path):
    source, target = _stage(tmp_path)
    target.mkdir()
    (target / "existing.csv").write_text("keep", encoding="utf-8")
    with pytest.raises(DirectoryPublicationError) as raised:
        publish_directory(source, target)
    assert raised.value.diagnostics["attempts"] == 0
    assert (source / "result.csv").exists()
    assert (target / "existing.csv").read_text(encoding="utf-8") == "keep"


def _analysis_args():
    frame = gpd.GeoDataFrame(
        {"asset": ["known"]}, geometry=[Point(500000, 3400000)], crs="EPSG:32650"
    )
    return GeoAnalysisInput(
        action="buffer", source=SpatialSource(geojson=frame.to_json(), crs="EPSG:32650"),
        distance_m=5,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file sharing semantics")
def test_vector_result_is_computed_once_when_publication_retries(tmp_path, monkeypatch):
    original_rename = Path.rename
    operation = Mock(wraps=vector._run_operation)
    monkeypatch.setattr(vector, "_run_operation", operation)
    releases = []

    def lock_first_rename(source, target):
        if not releases:
            releases.append(_hold_file(source / "result.csv"))
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", lock_first_rename)
    monkeypatch.setattr(publication.time, "sleep", lambda _: releases[0]())
    try:
        manifest = vector.analyze_vectors(
            _analysis_args(), cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
            publication_settings=PublicationConfig(max_attempts=3, retry_delay_seconds=0.01),
        )
    finally:
        for release in releases:
            release()
    operation.assert_called_once()
    assert manifest["statistics"]["feature_count"] == 1
    assert manifest["publication"]["attempts"] == 2
    saved = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    assert saved["statistics"] == manifest["statistics"]
    assert not list((tmp_path / "analysis").glob(".staging-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows child-file sharing semantics")
def test_vector_preserves_computed_artifacts_when_publication_is_blocked(tmp_path, monkeypatch):
    original_rename = Path.rename
    operation = Mock(wraps=vector._run_operation)
    monkeypatch.setattr(vector, "_run_operation", operation)
    releases = []

    def lock_first_rename(source, target):
        if not releases:
            releases.append(_hold_file(source / "result.csv"))
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", lock_first_rename)
    monkeypatch.setattr(publication.time, "sleep", Mock())
    try:
        with pytest.raises(DirectoryPublicationError) as raised:
            vector.analyze_vectors(
                _analysis_args(), cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
                publication_settings=PublicationConfig(max_attempts=2, retry_delay_seconds=0.01),
            )
    finally:
        for release in releases:
            release()
    operation.assert_called_once()
    stage = Path(raised.value.diagnostics["source"])
    assert (stage / "result.geojson").exists()
    assert (stage / "result.csv").exists()
    assert (stage / "manifest.json").exists()
    failure = json.loads((stage / "publication.json").read_text(encoding="utf-8"))
    assert failure["status"] == "failed"
    assert failure["attempts"] == 2
    assert not Path(raised.value.diagnostics["destination"]).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows publication retry semantics")
@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
def test_vector_retains_complete_stage_when_retry_wait_is_interrupted(
    tmp_path, monkeypatch, interruption
):
    args = _analysis_args()
    clock = [0.0]
    cancelled = Event()
    operation = Mock(wraps=vector._run_operation)
    monkeypatch.setattr(vector, "_run_operation", operation)
    monkeypatch.setattr(vector.time, "monotonic", lambda: clock[0])
    locked = PermissionError(errno.EACCES, "Access denied")
    locked.winerror = 5
    rename = Mock(side_effect=locked)
    monkeypatch.setattr(Path, "rename", rename)

    def interrupt_wait(_):
        if interruption == "deadline":
            clock[0] = 2.0
        else:
            cancelled.set()

    monkeypatch.setattr(publication.time, "sleep", interrupt_wait)
    with pytest.raises(TimeoutError) as raised:
        vector.analyze_vectors(
            args, cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
            timeout_seconds=1, cancelled=cancelled,
            publication_settings=PublicationConfig(max_attempts=3, retry_delay_seconds=0.01),
        )
    operation.assert_called_once()
    rename.assert_called_once()
    assert type(raised.value) is TimeoutError
    diagnostics = raised.value.diagnostics
    assert diagnostics["status"] == "interrupted"
    assert diagnostics["attempts"] == 1
    assert diagnostics["errors"][0]["winerror"] == 5
    _assert_interrupted_stage(diagnostics)


def test_vector_retains_complete_stage_when_deadline_expires_before_first_publication(
    tmp_path, monkeypatch
):
    args = _analysis_args()
    clock = [0.0]
    operation = Mock(wraps=vector._run_operation)
    monkeypatch.setattr(vector, "_run_operation", operation)
    monkeypatch.setattr(vector.time, "monotonic", lambda: clock[0])
    original_write = Path.write_text

    def expire_after_manifest(path, *args, **kwargs):
        result = original_write(path, *args, **kwargs)
        if path.name == "manifest.json":
            clock[0] = 2.0
        return result

    monkeypatch.setattr(Path, "write_text", expire_after_manifest)
    rename = Mock()
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(TimeoutError) as raised:
        vector.analyze_vectors(
            args, cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
            timeout_seconds=1,
        )
    operation.assert_called_once()
    rename.assert_not_called()
    assert type(raised.value) is TimeoutError
    diagnostics = raised.value.diagnostics
    assert diagnostics["status"] == "interrupted"
    assert diagnostics["attempts"] == 0
    assert diagnostics["errors"] == []
    _assert_interrupted_stage(diagnostics)


def _assert_interrupted_stage(diagnostics):
    stage = Path(diagnostics["source"])
    assert (stage / "result.geojson").exists()
    assert (stage / "result.csv").exists()
    assert (stage / "manifest.json").exists()
    assert not Path(diagnostics["destination"]).exists()
    saved = json.loads((stage / "publication.json").read_text(encoding="utf-8"))
    assert saved == diagnostics
    assert saved["interruption"]["type"] == "TimeoutError"


def test_vector_cleans_incomplete_stage_on_export_cancellation(tmp_path, monkeypatch):
    args = _analysis_args()
    cancelled = Event()
    original_write = Path.write_text

    def cancel_during_export(path, *args, **kwargs):
        result = original_write(path, *args, **kwargs)
        if path.name == "result.geojson":
            cancelled.set()
            raise TimeoutError("Export cancelled before CSV and manifest")
        return result

    monkeypatch.setattr(Path, "write_text", cancel_during_export)
    with pytest.raises(TimeoutError) as raised:
        vector.analyze_vectors(
            args, cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
            cancelled=cancelled,
        )
    assert not hasattr(raised.value, "diagnostics")
    assert not list((tmp_path / "analysis").iterdir())
