"""Deliver readable, editable artifacts into the current workspace."""

from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import DeliveryConfig, GeoConfig, MapReviewConfig
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool


@pytest.mark.asyncio
async def test_native_map_has_editable_project_and_default_workspace_output(tmp_path):
    config = GeoConfig(map_review=MapReviewConfig(enabled=False))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    data = gpd.GeoDataFrame({"value": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857)
    mapped = await GeoCartographyTool().execute(
        GeoCartographyInput(
            geojson=data.to_json(),
            field_name="value",
            output_format="png",
            title="可编辑地图",
        ),
        context,
    )
    assert not mapped.is_error, mapped.output
    assert Path(mapped.metadata["output_path"]).parent == tmp_path / "exports"
    assert Path(mapped.metadata["qgis_project"]).is_file()
    delivered = await GeoDeliverTool().execute(
        GeoDeliverInput(
            files=[mapped.metadata["output_path"], mapped.metadata["review_path"]],
            project_dir=mapped.metadata["project_dir"],
            name="用户交付",
        ),
        context,
    )
    assert not delivered.is_error, delivered.output
    folder = Path(delivered.metadata["delivery_dir"])
    assert folder.parent == tmp_path / "exports"
    assert (folder / "project" / "map.qgs").is_file()
    assert (folder / "project" / "data" / "layer_1.gpkg").is_file()
    # The original and any user edits to a delivered project are preserved.
    edited = folder / "project" / "README.md"
    edited.write_text("User modified this", encoding="utf-8")
    again = await GeoDeliverTool().execute(
        GeoDeliverInput(
            files=[mapped.metadata["output_path"]],
            name="用户交付",
        ),
        context,
    )
    assert not again.is_error
    assert Path(again.metadata["delivery_dir"]) != folder
    assert edited.read_text(encoding="utf-8") == "User modified this"


@pytest.mark.asyncio
async def test_script_map_can_export_project_without_rendering_again(tmp_path, monkeypatch):
    from PIL import Image

    data = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)
    image = tmp_path / "custom.png"
    Image.new("RGB", (20, 20), "white").save(image)
    original = image.read_bytes()
    tool = GeoCartographyTool()
    monkeypatch.setattr(tool, "_render", lambda *args: pytest.fail("Project must not render again"))
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await tool.execute(
        GeoCartographyInput(
            action="project",
            geojson=data.to_json(),
            output_path=str(image),
        ),
        ctx,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["qgis_project"]).is_file()
    assert image.read_bytes() == original


@pytest.mark.asyncio
async def test_delivery_rejects_external_destination_and_credentials(tmp_path):
    file = tmp_path / ".env"
    file.write_text("Never deliver credentials", encoding="utf-8")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(GeoDeliverInput(files=[str(file)]), context)
    assert result.is_error and not (tmp_path / "exports").exists()
    context.metadata["geoharness_config"] = GeoConfig(
        delivery=DeliveryConfig(outputs_dir=str(tmp_path.parent / "outside-delivery")),
    )
    result = await GeoDeliverTool().execute(GeoDeliverInput(files=[str(file)]), context)
    assert result.is_error and "subfolder" in result.output


@pytest.mark.asyncio
async def test_project_source_names_resolve_against_workspace_not_process_cwd(
    tmp_path, monkeypatch
):
    from PIL import Image

    gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857).to_file(tmp_path / "assets.gpkg")
    Image.new("RGB", (20, 20), "white").save(tmp_path / "map.png")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="project",
            source_name="assets.gpkg",
            output_path="map.png",
        ),
        ctx,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["qgis_project"]).is_file()


@pytest.mark.asyncio
async def test_delivery_filename_collisions_do_not_overwrite_selected_artifacts(tmp_path):
    paths = []
    for index, name in enumerate(("map-2.png", "map.png", "map.png", "DELIVERY.JSON", "PROJECT")):
        directory = tmp_path / str(index)
        directory.mkdir()
        path = directory / name
        path.write_text(str(index), encoding="utf-8")
        paths.append(str(path))
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(GeoDeliverInput(files=paths), ctx)
    assert not result.is_error, result.output
    copied = result.metadata["files"]
    assert len(set(copied)) == 5
    assert [Path(path).read_text(encoding="utf-8") for path in copied] == ["0", "1", "2", "3", "4"]
