"""Native QGIS packages use the same evidence and workspace delivery rules."""

import hashlib
import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool


def qgis_package(workspace):
    directory = workspace / ".geoharness" / "qgis" / "run-123" / "project"
    (directory / "data").mkdir(parents=True)
    files = {"map.qgz": b"saved project", "preview.png": b"real exported pixels",
             "data/points.gpkg": b"data snapshot", "README.md": b"instructions"}
    for name, contents in files.items():
        (directory / name).write_bytes(contents)
    manifest = {
        "backend": "qgis", "project_file": "map.qgz", "preview": "preview.png",
        "preview_sha256": hashlib.sha256(files["preview.png"]).hexdigest(),
        "files": ["project.json", *files], "layout_name": "Map",
    }
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory, manifest


@pytest.mark.asyncio
async def test_deliver_qgz_and_exact_native_preview(tmp_path):
    directory, manifest = qgis_package(tmp_path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(project_dir=str(directory), primary_map=str(directory / "preview.png")),
        context,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["qgis_project"]).suffix == ".qgz"
    assert Path(result.metadata["qgis_project"]).read_bytes() == b"saved project"
    assert result.metadata["primary_map_sha256"] == manifest["preview_sha256"]
    assert result.metadata["quality_status"] == "draft"
    output = Path(result.metadata["delivery_dir"])
    assert (output / "project/data/points.gpkg").read_bytes() == b"data snapshot"


@pytest.mark.asyncio
async def test_qgis_package_cannot_copy_files_outside_it(tmp_path):
    directory, manifest = qgis_package(tmp_path)
    (directory.parent / "unrelated.txt").write_text("keep private", encoding="utf-8")
    manifest["files"].append("../unrelated.txt")
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(project_dir=str(directory)),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}),
    )
    assert result.is_error and "inside its folder" in result.output
    assert not (tmp_path / "exports").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["round_trip", "absolute", "preview_round_trip"])
async def test_qgis_package_rejects_reentered_source_path_before_copy(tmp_path, reference):
    workspace = tmp_path / "workspace"
    directory, manifest = qgis_package(workspace)
    (directory / "other.txt").write_text("source", encoding="utf-8")
    sentinel = tmp_path / ".geoharness/qgis/run-123/project/other.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("unchanged", encoding="utf-8")
    if reference == "absolute":
        manifest["files"].append(str(directory / "other.txt"))
    elif reference == "preview_round_trip":
        manifest["preview"] = "../../../../.geoharness/qgis/run-123/project/preview.png"
    else:
        manifest["files"].append("../../../../.geoharness/qgis/run-123/project/other.txt")
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(project_dir=str(directory)),
        ToolExecutionContext(cwd=workspace, metadata={"geoharness_config": GeoConfig()}),
    )
    assert result.is_error and "inside its folder" in result.output
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not (workspace / "exports").exists()


@pytest.mark.asyncio
async def test_changed_qgis_preview_requires_new_snapshot(tmp_path):
    directory, _ = qgis_package(tmp_path)
    (directory / "preview.png").write_bytes(b"edited image")
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(project_dir=str(directory)),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}),
    )
    assert result.is_error and "SHA256" in result.output


@pytest.mark.asyncio
async def test_qgis_package_can_deliver_only_a_matching_recorded_review(tmp_path):
    directory, manifest = qgis_package(tmp_path)
    report = tmp_path / "review.json"
    report.write_text(json.dumps({
        "status": "passed", "summary": "Verified", "issues": [], "layout_patch": {},
        "image_sha256": manifest["preview_sha256"],
    }), encoding="utf-8")
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(project_dir=str(directory), review_path=str(report)),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}),
    )
    assert not result.is_error and result.metadata["quality_status"] == "passed"
