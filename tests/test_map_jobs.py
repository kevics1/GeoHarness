"""Server-owned map identities and hash-bound, persistent stage evidence."""

import hashlib
import json
import shutil
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from geoharness.cartography.jobs import (
    begin_map_request,
    ensure_map_job,
    map_job_receipt,
    record_map_delivery,
    record_map_review,
    record_map_version,
    saved_project_job,
)
from geoharness.config.settings import CartographyConfig, GeoConfig


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight():
    return {
        "status": "passed",
        "scope": "map_data_preflight",
        "display_crs": "EPSG:4326",
        "map_extent": [0, 0, 1, 1],
        "layers": [{"feature_count": 1}],
        "checks": {"geometry_valid_finite_non_empty": "passed"},
    }


def native_layout(image):
    return {
        "scope": "layout_geometry",
        "status": "passed",
        "image_sha256": digest(image),
        **{
            key: []
            for key in (
                "panel_overlaps",
                "label_overlaps",
                "clipped_labels",
                "overflow_text",
                "figure_text_overlaps",
                "clipped_figure_text",
                "text_panel_overlaps",
                "panels_outside_canvas",
            )
        },
    }


def package(root, image):
    root.mkdir()
    (root / "map.qgz").write_bytes(b"saved project")
    (root / "data.gpkg").write_bytes(b"data snapshot")
    preview = root / "preview.png"
    preview.write_bytes(image.read_bytes())
    layout = {"bounds_check": {"checked": True, "valid": True, "problems": []}}
    (root / "project.json").write_text(
        json.dumps(
            {
                "backend": "qgis",
                "project_file": "map.qgz",
                "preview": "preview.png",
                "preview_sha256": digest(image),
                "layout": layout,
                "files": ["map.qgz", "data.gpkg", "preview.png", "project.json"],
            }
        ),
        encoding="utf-8",
    )
    return layout


def record(job_id, metadata, cwd, image, **kwargs):
    return record_map_version(
        job_id,
        metadata,
        cwd=cwd,
        config=GeoConfig(),
        image_path=image,
        preflight=kwargs.get("preflight"),
        layout=kwargs.get("layout"),
        project_dir=kwargs.get("project_dir"),
        analysis=kwargs.get("analysis"),
    )


async def test_delivery_preserves_selection_even_when_history_has_better_candidate(tmp_path):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="fire")
    good, bad = tmp_path / "v2.png", tmp_path / "v3.png"
    good.write_bytes(b"good layout")
    bad.write_bytes(b"clipped footer")
    project_root = tmp_path / "good-project"
    layout = package(project_root, good)
    manifest = json.loads((project_root / "project.json").read_text())
    manifest.update(backend="qgis", project_fidelity="saved_layout")
    (project_root / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    record(job_id, metadata, tmp_path, good, preflight=preflight(), layout=layout,
           project_dir=project_root)
    failed = native_layout(bad)
    failed.update(status="issues", clipped_figure_text=[{"text": "overflow"}])
    record(job_id, metadata, tmp_path, bad, preflight=preflight(), layout=failed)
    context = ToolExecutionContext(cwd=tmp_path, metadata={**metadata, "geoharness_config": config})
    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config, map_id=job_id)
    assert receipt["latest_version"] == 2
    assert receipt["delivery_candidate"]["version"] == 1
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        map_id=job_id, version_policy="prefer_checked", name="checked"), context)
    assert delivered.is_error and 'missing_project' in delivered.output
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(good), name='explicit-history'), context)
    assert not delivered.is_error, delivered.output
    result = delivered.metadata
    assert result["selection"]["selected_version"] == 1
    assert result["selection"]["changed"] is False
    assert result["selection"]["requested_version"] == 1
    assert Path(result["primary_map"]).read_bytes() == good.read_bytes()
    assert result["project_fidelity"] == "saved_layout"
    assert Path(result["qgis_project"]).is_file()
    assert result["quality_status"] == "draft"  # No visual review was performed.

    # A later requested version must not be silently replaced under prefer_checked.
    latest = tmp_path / "latest.png"
    latest.write_bytes(b"newest requested map")
    latest_project = tmp_path / "latest-project"
    latest_layout = package(latest_project, latest)
    latest_manifest = json.loads((latest_project / "project.json").read_text())
    latest_manifest.update(backend="qgis", project_fidelity="saved_layout")
    (latest_project / "project.json").write_text(json.dumps(latest_manifest), encoding="utf-8")
    record(job_id, metadata, tmp_path, latest, preflight=preflight(),
           layout=latest_layout, project_dir=latest_project)
    pinned = await GeoDeliverTool().execute(GeoDeliverInput(
        primary_map=str(latest), project_dir=str(latest_project), map_id=job_id,
        version_policy="prefer_checked", name="pinned-latest"), context)
    assert not pinned.is_error, pinned.output
    assert Path(pinned.metadata["primary_map"]).read_bytes() == latest.read_bytes()
    assert pinned.metadata["selection"]["selected_version"] == pinned.metadata[
        "map_receipt"]["selected_version"]
    assert pinned.metadata["selection"]["changed"] is False
    assert pinned.metadata["selection"].get("explicitly_pinned") is True

    good.write_bytes(b"changed after check")
    stale = map_job_receipt(context.metadata, cwd=tmp_path, config=config, map_id=job_id)
    assert stale["delivery_candidate"]['version'] == 3
    refused = await GeoDeliverTool().execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(good), name="blocked"), context)
    assert refused.is_error


def test_candidate_selection_prefers_visual_pass_over_unreviewed_version(tmp_path):
    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    for index in range(2):
        image = tmp_path / f"v{index}.png"
        image.write_bytes(f"image-{index}".encode())
        record(job_id, metadata, tmp_path, image,
               preflight=preflight(), layout=native_layout(image))
        if index == 0:
            record_map_review(job_id, metadata, cwd=tmp_path, config=config, image_path=image,
                review={"status": "passed", "image_sha256": digest(image), "issues": [],
                        "layout_patch": {}})
    receipt = map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=job_id)
    assert receipt["delivery_candidate"]["version"] == 1
    assert receipt["delivery_candidate"]["quality_status"] == "passed"


async def test_delivery_by_id_and_explicit_draft_preserve_selection_intent(tmp_path):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    good, bad, edited = [tmp_path / name for name in ("good.png", "bad.png", "edited.png")]
    for image in (good, bad, edited):
        image.write_bytes(image.name.encode())
    good_project = tmp_path / "good-project"
    good_layout = package(good_project, good)
    bad_project = tmp_path / "bad-project"
    bad_layout = package(bad_project, bad)
    bad_layout["status"] = "issues"
    bad_layout["clipped_figure_text"] = [{"text": "clipped"}]
    record(job_id, metadata, tmp_path, good, preflight=preflight(), layout=good_layout,
           project_dir=good_project)
    record(job_id, metadata, tmp_path, bad, preflight=preflight(), layout=bad_layout,
           project_dir=bad_project)
    context = ToolExecutionContext(cwd=tmp_path, metadata={**metadata, "geoharness_config": config})
    tool = GeoDeliverTool()
    by_id = await tool.execute(GeoDeliverInput(map_id=job_id), context)
    assert not by_id.is_error, by_id.output
    assert Path(by_id.metadata["primary_map"]).read_bytes() == bad.read_bytes()
    explicit = await tool.execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(bad), version_policy="selected"), context)
    assert not explicit.is_error, explicit.output
    assert Path(explicit.metadata["primary_map"]).read_bytes() == bad.read_bytes()
    assert explicit.metadata["quality_status"] == "draft"
    explicit_id = await tool.execute(GeoDeliverInput(
        map_id=job_id, version_policy="selected"), context)
    assert not explicit_id.is_error, explicit_id.output
    assert Path(explicit_id.metadata["primary_map"]).read_bytes() == bad.read_bytes()
    assert explicit_id.metadata["quality_status"] == "draft"
    assert explicit_id.metadata["selection"]["selected_version"] == 2
    # A bare PNG cannot be delivered as a map: it has no editable project.
    unregistered = await tool.execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(edited)), context)
    assert unregistered.is_error
    assert "missing_project" in unregistered.output
    assert unregistered.metadata.get("failure", {}).get("code") == "missing_project"
    # The same primary map with its actual project remains an explicit draft.
    edited_in_place = await tool.execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(bad), project_dir=str(bad_project),
        version_policy="selected"), context)
    assert not edited_in_place.is_error, edited_in_place.output
    assert Path(edited_in_place.metadata["primary_map"]).read_bytes() == bad.read_bytes()
    assert edited_in_place.metadata["quality_status"] == "draft"
    assert edited_in_place.metadata["selection"]["changed"] is False


def test_candidate_excludes_changed_bound_report(tmp_path):
    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    report = tmp_path / "layout.json"
    layout = native_layout(image)
    report.write_text(json.dumps(layout), encoding="utf-8")
    layout["report_path"] = str(report)
    record(job_id, metadata, tmp_path, image, layout=layout)
    assert map_job_receipt(metadata, cwd=tmp_path, config=config,
                           map_id=job_id)["delivery_candidate"] is not None
    report.write_text("{}", encoding="utf-8")
    assert map_job_receipt(metadata, cwd=tmp_path, config=config,
                           map_id=job_id)["delivery_candidate"] is None


@pytest.mark.parametrize("changed_during_copy", [False, True])
async def test_delivery_explicit_history_recovers_its_visual_report_after_restart(
    tmp_path, monkeypatch, changed_during_copy,
):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    good, bad = tmp_path / "checked.png", tmp_path / "new.png"
    good.write_bytes(b"checked")
    bad.write_bytes(b"new")
    good_project = tmp_path / "checked-project"
    package(good_project, good)
    good_layout = native_layout(good)
    bad_project = tmp_path / "new-project"
    package(bad_project, bad)
    bad_layout = native_layout(bad)
    bad_layout["status"] = "issues"
    bad_layout["clipped_figure_text"] = [{"text": "clipped"}]
    record(job_id, metadata, tmp_path, good, preflight=preflight(), layout=good_layout,
           project_dir=good_project)
    review = {"status": "passed", "summary": "Checked actual image", "issues": [],
              "layout_patch": {}, "image_sha256": digest(good)}
    review_path = tmp_path / "checked.review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    record_map_review(job_id, metadata, cwd=tmp_path, config=config, image_path=good,
                      review={**review, "review_path": str(review_path)})
    record(job_id, metadata, tmp_path, bad, preflight=preflight(), layout=bad_layout,
           project_dir=bad_project)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    if changed_during_copy:
        copy = shutil.copy2

        def mutate_then_restore(source, target):
            if Path(source) != review_path:
                return copy(source, target)
            original = review_path.read_bytes()
            try:
                review_path.write_text("{}", encoding="utf-8")
                return copy(source, target)
            finally:
                review_path.write_bytes(original)

        monkeypatch.setattr("geoharness.tools.geo_deliver.shutil.copy2", mutate_then_restore)
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        map_id=job_id, primary_map=str(good)), context)
    assert not result.is_error, result.output
    assert result.metadata["quality_status"] == ("draft" if changed_during_copy else "passed")
    assert result.metadata["snapshot_status"] == ("changed" if changed_during_copy else "passed")
    assert result.metadata["review_status"] == "passed"
    assert any(Path(path).name == review_path.name for path in result.metadata["files"])


def test_server_uuid_and_alias_rename_survive_restart(tmp_path):
    metadata = {"map_job_state": {}}
    shallow = dict(metadata)
    begin_map_request(metadata, cwd=tmp_path, config=GeoConfig())
    first = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig(), map_id="model-selected")
    second = ensure_map_job(shallow, cwd=tmp_path, config=GeoConfig(), map_id="replacement-v9")
    assert str(uuid.UUID(first)) == first == second
    assert metadata["map_job_state"] is shallow["map_job_state"]
    assert metadata["map_job_state"]["aliases"]["replacement-v9"] == first
    restarted = {}
    assert (
        ensure_map_job(restarted, cwd=tmp_path, config=GeoConfig(), map_id="model-selected")
        == first
    )
    begin_map_request(restarted, cwd=tmp_path, config=GeoConfig(), continuation=False)
    fresh = ensure_map_job(restarted, cwd=tmp_path, config=GeoConfig(), map_id="new-request")
    assert fresh != first
    begin_map_request({}, cwd=tmp_path, config=GeoConfig(), continuation=True)
    metadata2 = {}
    begin_map_request(metadata2, cwd=tmp_path, config=GeoConfig(), continuation=True)
    assert ensure_map_job(metadata2, cwd=tmp_path, config=GeoConfig(), map_id="continue") == fresh


def test_artifact_path_and_digest_resolve_same_job_without_new_records(tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"same pixels")
    metadata = {}
    job_id = ensure_map_job(
        metadata, cwd=tmp_path, config=GeoConfig(), map_id="first", artifact_path=image
    )
    renamed = tmp_path / "renamed.png"
    renamed.write_bytes(image.read_bytes())
    restarted = {}
    assert (
        ensure_map_job(restarted, cwd=tmp_path, config=GeoConfig(), artifact_path=renamed) == job_id
    )
    image.write_bytes(b"updated pixels")
    assert ensure_map_job({}, cwd=tmp_path, config=GeoConfig(), artifact_path=image) == job_id


def test_receipt_lookup_has_no_side_effect_or_active_fallback(tmp_path):
    metadata = {}
    config = GeoConfig()
    result = map_job_receipt(metadata, cwd=tmp_path, config=config, map_id="missing")
    assert result["job_id"] is None and result["quality_status"] == "draft"
    assert metadata == {} and not (tmp_path / config.cartography.jobs_dir).exists()
    ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="active")
    unregistered = tmp_path / "different.png"
    unregistered.write_bytes(b"other")
    result = map_job_receipt(metadata, cwd=tmp_path, config=config, artifact_path=unregistered)
    assert result["job_id"] is None


def test_package_project_path_and_saved_as_embedded_uuid_recover_job(tmp_path):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    metadata = {}
    config = GeoConfig()
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    root = tmp_path / "editable"
    layout = package(root, image)
    record(job_id, metadata, tmp_path, image, layout=layout, project_dir=root)
    assert ensure_map_job({}, cwd=tmp_path, config=config, artifact_path=root / "map.qgz") == job_id
    saved = tmp_path / "saved-as.qgz"
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr(
            "saved-as.qgs",
            f"<qgis><properties><geoharness>"
            f"<map_job_id>{job_id}</map_job_id>"
            "</geoharness></properties></qgis>",
        )
    assert saved_project_job({}, cwd=tmp_path, config=config, project_path=saved) == job_id
    saved.write_text(
        f"<qgis><properties><geoharness><map_job_id>{uuid.uuid4()}</map_job_id>"
        "</geoharness></properties></qgis>",
        encoding="utf-8",
    )
    assert saved_project_job({}, cwd=tmp_path, config=config, project_path=saved) is None


def test_package_allows_configured_external_artifact_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "artifacts"
    external.mkdir()
    image = external / "map.png"
    image.write_bytes(b"pixels")
    root = external / "editable"
    layout = package(root, image)
    config = replace(GeoConfig(), cartography=CartographyConfig(outputs_dir=str(external)))
    job_id = ensure_map_job({}, cwd=workspace, config=config)
    result = record_map_version(
        job_id,
        {},
        cwd=workspace,
        config=config,
        image_path=image,
        preflight=None,
        layout=layout,
        project_dir=root,
    )
    assert result["stages"]["packaged"]["status"] == "passed"


@pytest.mark.parametrize("configured", [".", "..", "../outside"])
def test_jobs_directory_requires_workspace_subdirectory(tmp_path, configured):
    config = replace(GeoConfig(), cartography=CartographyConfig(jobs_dir=configured))
    with pytest.raises(ValueError, match="subdirectory"):
        ensure_map_job({}, cwd=tmp_path, config=config)


def test_stage_claims_require_file_structured_data_and_hash_bound_layout(tmp_path):
    metadata = {}
    image = tmp_path / "map.png"
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    result = record(
        job_id,
        metadata,
        tmp_path,
        image,
        preflight={"status": "passed"},
        layout={"status": "passed"},
    )
    assert result["stages"]["rendered"]["status"] == "unverified"
    assert result["stages"]["data_checked"]["status"] == "unverified"
    assert result["stages"]["layout_checked"]["status"] == "unverified"
    image.write_bytes(b"image bytes")
    result = record(
        job_id, metadata, tmp_path, image, preflight=preflight(), layout=native_layout(image)
    )
    assert result["stages"]["rendered"]["status"] == "passed"
    assert result["stages"]["data_checked"]["status"] == "passed"
    assert result["stages"]["layout_checked"]["status"] == "passed"
    assert result["stages"]["visual_checked"]["status"] == "unverified"
    assert result["quality_status"] == "draft"
    assert result["analysis_correctness"] == "unverified"


def test_same_version_preserves_checks_none_and_new_pixels_invalidate_visual(tmp_path):
    metadata = {}
    image = tmp_path / "map.png"
    image.write_bytes(b"first pixels")
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(job_id, metadata, tmp_path, image, preflight=preflight(), layout=native_layout(image))
    first = record_map_review(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=image,
        review={"status": "passed", "image_sha256": digest(image)},
    )
    assert first["quality_status"] == "passed"
    second = record(job_id, metadata, tmp_path, image)
    assert second["current_version"] == 1 and second["quality_status"] == "passed"
    image.write_bytes(b"new pixels")
    stale = map_job_receipt(metadata, cwd=tmp_path, config=GeoConfig(), map_id=job_id)
    assert stale["status"] == "stale" and stale["stages"]["visual_checked"]["status"] == "stale"
    new = record(
        job_id, metadata, tmp_path, image, preflight=preflight(), layout=native_layout(image)
    )
    assert new["current_version"] == 2 and new["stages"]["visual_checked"]["status"] == "unverified"
    assert len(new["versions"]) == 2


def test_new_export_package_with_equal_pixels_preserves_old_verified_version(tmp_path):
    metadata = {}
    image = tmp_path / "map.png"
    image.write_bytes(b"same pixels")
    project = tmp_path / "project"
    layout = package(project, image)
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(job_id, metadata, tmp_path, image, preflight=preflight(), layout=layout,
           project_dir=project)
    record_map_review(job_id, metadata, cwd=tmp_path, config=GeoConfig(), image_path=image,
                      review={"status": "passed", "image_sha256": digest(image)})
    exported = tmp_path / "exported"
    shutil.copytree(project, exported)
    (exported / "data.gpkg").write_bytes(b"different package")
    preview = exported / "preview.png"
    result = record(job_id, metadata, tmp_path, preview, layout=layout, project_dir=exported)
    assert result["current_version"] == 2
    assert result["stages"]["data_checked"]["status"] == "unverified"
    old = map_job_receipt(metadata, cwd=tmp_path, config=GeoConfig(), artifact_path=image)
    assert old["current_version"] == 1 and old["quality_status"] == "passed"
    assert old["delivery_candidate"]["version"] == 1


@pytest.mark.parametrize(
    "extra,expected",
    [
        ({"issues": [{"severity": "warning"}]}, "issues"),
        ({"issues": [{"severity": "error"}]}, "issues"),
        ({"layout_patch": {"width": 12}}, "issues"),
        ({"diagnostics": {"finish_reason": "length"}}, "unverified"),
        ({"diagnostics": {"failure_stage": "schema"}}, "unverified"),
        ({"image_sha256": "0" * 64}, "unverified"),
        ({"status": "unverified"}, "unverified"),
    ],
)
def test_review_cannot_claim_pass_with_issues_or_incomplete_evidence(tmp_path, extra, expected):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    metadata = {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(job_id, metadata, tmp_path, image)
    result = record_map_review(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=image,
        review={"status": "passed", "image_sha256": digest(image), **extra},
        layout_status="passed",
    )
    assert result["stages"]["visual_checked"]["status"] == expected
    assert result["stages"]["layout_checked"]["status"] == "unverified"


def test_qgis_package_bounds_and_assets_rechecked_on_read(tmp_path):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    project = tmp_path / "project"
    layout = package(project, image)
    metadata = {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    result = record(
        job_id, metadata, tmp_path, image, preflight=preflight(), layout=layout, project_dir=project
    )
    assert result["stages"]["layout_checked"]["status"] == "passed"
    assert result["stages"]["packaged"]["status"] == "passed"
    assert result["project_dir"] == str(project)
    assert len(result["stages"]["packaged"]["files"]) == 4
    record(job_id, metadata, tmp_path, image)
    (project / "data.gpkg").write_bytes(b"edited data")
    result = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), map_id=job_id)
    assert result["stages"]["packaged"]["status"] == "stale"
    assert result["stages"]["layout_checked"]["status"] == "stale"


def test_modified_manifest_or_missing_image_is_stale_and_does_not_mutate_disk(tmp_path):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    project = tmp_path / "project"
    layout = package(project, image)
    metadata = {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig(), artifact_path=image)
    record(job_id, metadata, tmp_path, image, layout=layout, project_dir=project)
    receipt_file = tmp_path / GeoConfig().cartography.jobs_dir / job_id / "receipt.json"
    before = receipt_file.read_bytes()
    image.unlink()
    result = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), artifact_path=image)
    assert result["job_id"] == job_id and result["stages"]["rendered"]["status"] == "stale"
    assert receipt_file.read_bytes() == before


def test_package_path_traversal_is_rejected(tmp_path):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    project = tmp_path / "project"
    layout = package(project, image)
    manifest = json.loads((project / "project.json").read_text())
    manifest["files"].append("../map.png")
    (project / "project.json").write_text(json.dumps(manifest))
    metadata = {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    with pytest.raises(ValueError, match="escapes"):
        record(job_id, metadata, tmp_path, image, layout=layout, project_dir=project)


def test_delivery_only_records_existing_files_and_checks_copies_again(tmp_path):
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    metadata = {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(job_id, metadata, tmp_path, image)
    copy = tmp_path / "delivered.png"
    copy.write_bytes(image.read_bytes())
    missing = tmp_path / "missing.txt"
    result = record_map_delivery(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=image,
        delivery={"files": [str(copy), str(missing)], "primary_map": str(copy)},
    )
    assert result["stages"]["delivered"]["status"] == "unverified"
    assert [item["path"] for item in result["stages"]["delivered"]["files"]] == [str(copy)]
    copy.write_bytes(b"edited delivery")
    result = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), artifact_path=copy)
    assert result["stages"]["delivered"]["status"] == "stale"


def test_persisted_reports_are_bounded_redacted_and_do_not_include_source_objects(tmp_path):
    config = GeoConfig(api_key="test-secret-api-key")
    metadata = {}
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    record_map_version(
        job_id,
        metadata,
        cwd=tmp_path,
        config=config,
        image_path=image,
        preflight=None,
        layout=None,
        project_dir=None,
        analysis={
            "api_key": config.api_key,
            "source": "print('secret code')",
            "gdf": object(),
            "summary": config.api_key + "x" * 10000,
            "layers": list(range(10000)),
            "method": "count within buffer",
        },
    )
    raw = (tmp_path / config.cartography.jobs_dir / job_id / "receipt.json").read_text()
    assert config.api_key not in raw and "secret code" not in raw
    assert '"gdf"' not in raw and len(raw) < 14000
    stored = json.dumps(map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=job_id))
    assert config.api_key not in stored and "secret code" not in stored and '"gdf"' not in stored
    assert "count within buffer" in stored and "[redacted]" in stored


def test_parallel_aliases_create_one_job_and_valid_atomic_json(tmp_path):
    metadata = {"map_job_state": {}}
    begin_map_request(metadata, cwd=tmp_path, config=GeoConfig())
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(
            pool.map(
                lambda n: ensure_map_job(
                    dict(metadata), cwd=tmp_path, config=GeoConfig(), map_id=f"alias-{n}"
                ),
                range(16),
            )
        )
    assert len(set(ids)) == 1
    index = json.loads((tmp_path / GeoConfig().cartography.jobs_dir / "index.json").read_text())
    assert len(index["jobs"]) == 1 and len(index["aliases"]) == 16
    assert not list((tmp_path / GeoConfig().cartography.jobs_dir).rglob("*.tmp"))


def test_selected_old_image_receipt_does_not_take_latest_draft_stages(tmp_path):
    metadata = {}
    old = tmp_path / "old.png"
    old.write_bytes(b"old pixels")
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(job_id, metadata, tmp_path, old, preflight=preflight(), layout=native_layout(old))
    record_map_review(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=old,
        review={"status": "passed", "image_sha256": digest(old)},
    )
    new = tmp_path / "new.png"
    new.write_bytes(b"new pixels")
    record(job_id, metadata, tmp_path, new)
    selected = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), artifact_path=old)
    assert selected["quality_status"] == "passed"
    assert selected["current_version"] == selected["selected_version"] == 1
    assert selected["latest_version"] == 2
    assert selected["image_path"] == str(old)
    latest = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), map_id=job_id)
    assert latest["current_version"] == 2 and latest["quality_status"] == "draft"
    delivered = record_map_delivery(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=old,
        delivery={"files": [str(old)], "primary_map": str(old)},
    )
    assert delivered["current_version"] == 1 and delivered["quality_status"] == "passed"
    rereviewed = record_map_review(
        job_id,
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=old,
        review={"status": "issues", "image_sha256": digest(old)},
    )
    assert rereviewed["current_version"] == 1
    assert rereviewed["stages"]["visual_checked"]["status"] == "issues"
    assert rereviewed["latest_version"] == 2


def test_copied_identical_package_keeps_checks_but_content_change_invalidates_data(tmp_path):
    metadata = {}
    image = tmp_path / "map.png"
    image.write_bytes(b"pixels")
    original = tmp_path / "original"
    layout = package(original, image)
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=GeoConfig())
    record(
        job_id,
        metadata,
        tmp_path,
        image,
        preflight=preflight(),
        layout=layout,
        project_dir=original,
        analysis={"method": "buffer", "count": 1},
    )
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    result = record(job_id, metadata, tmp_path, image, layout=layout, project_dir=copied)
    assert result["current_version"] == 1
    assert result["stages"]["layout_checked"]["status"] == "passed"
    assert result["stages"]["data_checked"]["status"] == "passed"
    (copied / "data.gpkg").write_bytes(b"other data")
    stale = map_job_receipt({}, cwd=tmp_path, config=GeoConfig(), artifact_path=image)
    assert stale["stages"]["data_checked"]["status"] == "stale"
    assert stale["versions"][0]["analysis"]["status"] == "stale"
    result = record(job_id, metadata, tmp_path, image, layout=layout, project_dir=copied)
    assert result["stages"]["layout_checked"]["status"] == "unverified"
    assert result["stages"]["data_checked"]["status"] == "stale"
    refreshed = record(job_id, metadata, tmp_path, image, preflight=preflight())
    assert refreshed["stages"]["data_checked"]["status"] == "passed"
