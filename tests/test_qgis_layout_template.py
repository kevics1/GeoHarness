"""Validate the fixed template and its real native QGIS save/export round trip."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from geoharness.config.settings import QgisConfig
from geoharness.qgis.discovery import child_environment, discover_qgis
from geoharness.qgis.project_worker import _layout_regions, _rectangle_problems
from geoharness.qgis.runner import execute_qgis
from geoharness.tools.geo_qgis_project import (
    GeoQgisProjectInput,
    QgisProjectLayout,
    QgisVectorStyle,
)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"detail_extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}},
        {"legend": False, "detail_extent": {"xmin": 0, "ymin": 0, "xmax": 1, "ymax": 1}},
        {"legend": False},
    ],
)
def test_template_regions_have_positive_area_and_do_not_overlap(spec):
    spec = QgisProjectLayout.model_validate(spec).model_dump(exclude_none=True)
    regions = _layout_regions(spec, "地理分析成果")
    assert regions["footer"][1] > regions["main_map"][1] + regions["main_map"][3]
    assert not _rectangle_problems(regions, spec["width_mm"], spec["height_mm"])


@pytest.mark.parametrize(
    "update",
    [
        {"detail_extent": {"xmin": 1, "ymin": 0, "xmax": 0, "ymax": 1}},
        {"detail_extent": {"xmin": 0, "ymin": 0, "xmax": float("inf"), "ymax": 1}},
        {"width_mm": 100, "sidebar_width_mm": 60},
        {"height_mm": 80, "footer_height_mm": 35},
    ],
)
def test_invalid_detail_extents_and_insufficient_regions_fail_before_render(update):
    with pytest.raises(ValidationError):
        QgisProjectLayout.model_validate(update)


@pytest.mark.parametrize(
    "ranges",
    [
        [{"lower": 2, "upper": 1, "color": "red"}],
        [{"lower": 0, "upper": float("nan"), "color": "red"}],
        [{"lower": 0, "upper": 2, "color": "red"},
         {"lower": 1, "upper": 3, "color": "blue"}],
    ],
)
def test_invalid_graduated_ranges_are_rejected(ranges):
    with pytest.raises(ValidationError):
        QgisVectorStyle(renderer="graduated", field="value", ranges=ranges)


def test_adjacent_graduated_ranges_keep_explicit_colors_and_sizes():
    style = QgisVectorStyle(
        renderer="graduated", field="value", width_mm=0.6,
        ranges=[{"lower": 0, "upper": 2, "color": "red"},
                {"lower": 2, "upper": 3, "color": "blue", "size_mm": 4}],
    )
    assert style.ranges[1].size_mm == 4


def test_rectangle_validation_identifies_page_overflow_and_item_collisions():
    problems = _rectangle_problems(
        {"main_map": [10, 10, 80, 60], "legend": [70, 20, 40, 30]}, 100, 80,
    )
    assert "legend: rectangle exceeds page bounds" in problems
    assert "main_map/legend: rectangles overlap" in problems


async def test_real_footer_wrap_uses_font_metrics_before_export(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point

    settings = QgisConfig()
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"Independent QGIS installation required: {exc}")
    source = tmp_path / "source.geojson"
    gpd.GeoDataFrame({"name": ["site"]}, geometry=[Point(101, 28)], crs=4326).to_file(source)
    text = ("统计（核验分析）：蔓延范围内受威胁居民点2处。应急避难场所23处，总容量36.9万人；"
            "最近者西昌学院南校区，距当前火场5.96km。计量CRS EPSG:32648。" * 3)
    result = await execute_qgis("project_preflight", {
        "layers": [{"file_path": str(source)}], "title": "底栏自动排版",
        "crs": "EPSG:32647", "map_extent": [800000, 3060000, 840000, 3100000],
        "layout": QgisProjectLayout(width_mm=420, height_mm=297, margin_mm=10,
            footer_text=text, footer_height_mm=10, body_font_size_pt=9).model_dump(),
    }, cwd=tmp_path, settings=settings, read_roots=[tmp_path])
    assert result["status"] == "succeeded", result.get("error_summary")
    layout = result["result"]["layout"]
    assert layout["bounds_check"]["valid"] is True
    assert "\n" in layout["footer_text"]
    assert layout["footer_text"].replace("\n", "") == text
    assert layout["regions"]["footer"][3] > 10
    assert not list(Path(result["run_dir"]).glob("*.png"))
    assert not list(Path(result["run_dir"]).glob("*.qgz"))


@pytest.fixture
def native_settings(tmp_path):
    settings = QgisConfig(outputs_dir=str(tmp_path / "native-outputs"))
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    return settings


def _sources(tmp_path):
    polygon = tmp_path / "areas.geojson"
    features = []
    for index in range(3):
        x = index * 2000
        features.append({
            "type": "Feature", "properties": {"value": index + 1, "name": f"区域{index + 1}"},
            "geometry": {"type": "Polygon", "coordinates": [[
                [x, 0], [x + 1800, 0], [x + 1800, 3500], [x, 3500], [x, 0],
            ]]},
        })
    polygon.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
        "features": features,
    }, ensure_ascii=False), encoding="utf-8")
    points = tmp_path / "places.geojson"
    points.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
        "features": [{
            "type": "Feature", "properties": {"kind": "station", "name": "观测点"},
            "geometry": {"type": "Point", "coordinates": [1000, 1800]},
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return [
        {"file_path": str(polygon), "name": "风险区域", "label_field": "name", "style": {
            "renderer": "graduated", "field": "value", "width_mm": 0.6,
            "ranges": [{"lower": 0, "upper": 2, "color": "#fee08b", "label": "低风险"},
                       {"lower": 2, "upper": 3, "color": "#d73027", "label": "高风险"}],
        }},
        {"file_path": str(points), "name": "观测站点", "label_field": "name", "style": {
            "renderer": "categorized", "field": "kind", "size_mm": 3,
            "categories": [{"value": "station", "color": "#2166ac", "label": "观测站",
                            "size_mm": 4.5, "width_mm": 0.5}],
        }},
    ]


async def test_native_template_create_png_reopen_and_export_preserve_layout(
    tmp_path: Path, native_settings
):
    request = GeoQgisProjectInput(
        action="create", layers=_sources(tmp_path), project_format="qgs", dpi=96,
        title="区域风险与观测站点", crs="EPSG:3857", map_extent=[-300, -300, 6100, 3900],
        layout={"detail_extent": {"xmin": 500, "ymin": 800, "xmax": 1800, "ymax": 2500},
                "footer_text": "数据说明：测试数据，仅用于验证版式。"},
    )
    created = await execute_qgis(
        "project_create", request.model_dump(exclude={"action"}, exclude_none=True),
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert created["status"] == "succeeded", created.get("error_summary")
    result = created["result"]
    layout = result["layout"]
    assert layout["template"] == "structured-v1"
    assert set(layout["regions"]) == {"main_map", "detail_map", "legend", "title", "footer"}
    assert set(layout["map_extents"]) == {"main_map", "detail_map"}
    assert layout["bounds_check"]["valid"] is True
    assert layout["font"]["loaded"] is True
    assert layout["font"]["family"]
    assert layout["scale_bars"][0]["units"] == "Kilometers"
    assert layout["scale_bars"][0]["units_per_segment"] > 0
    assert layout["bounds_check"]["items"]["scale_bar"][2] > 25
    for layer in result["layers"]:
        assert layer["label_style"]["font"]["loaded"] is True
        assert layer["label_style"]["buffer_enabled"] is True
        assert layer["label_style"]["overlap_handling"] == "PreventOverlap"
    assert result["layers"][0]["ranges"][1]["color"] == "#d73027"
    assert result["layers"][0]["ranges"][1]["width_mm"] == pytest.approx(0.6)
    assert result["layers"][1]["categories"][0]["size_mm"] == pytest.approx(4.5)
    preview = Path(result["preview_path"])
    assert hashlib.sha256(preview.read_bytes()).hexdigest() == result["image_sha256"]
    with Image.open(preview) as image:
        assert image.width > 1000 and image.height > 700
        assert len(image.convert("RGB").getcolors(image.width * image.height)) > 100
    inspected = await execute_qgis(
        "project_inspect", {"project_path": result["project_file"]},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert inspected["status"] == "succeeded", inspected.get("error_summary")
    assert inspected["result"]["layouts"][0]["map_extents"] == layout["map_extents"]
    assert inspected["result"]["layouts"][0]["bounds_check"]["valid"] is True
    exported = await execute_qgis(
        "project_export", {"project_path": result["project_file"], "dpi": 96},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert exported["status"] == "succeeded", exported.get("error_summary")
    assert exported["result"]["layout"]["map_extents"] == layout["map_extents"]
    assert exported["result"]["image_sha256"] == result["image_sha256"]

    # Native user editing may add labels or north arrows inside the map frame.
    # Those intentional overlays must survive export without template collisions.
    runtime = discover_qgis(native_settings, cwd=tmp_path)
    script = """
import os,sys
from qgis.core import (
    QgsApplication,QgsProject,QgsLayoutItemLabel,QgsLayoutPoint,QgsLayoutSize
)
QgsApplication.setPrefixPath(os.environ['QGIS_PREFIX_PATH'],True)
app=QgsApplication([],False);app.initQgis()
project=QgsProject.instance()
assert project.read(sys.argv[1])
layout=project.layoutManager().printLayouts()[0]
note=QgsLayoutItemLabel(layout);note.setId('user_note');note.setText('User annotation')
layout.addLayoutItem(note)
note.attemptMove(QgsLayoutPoint(20,50));note.attemptResize(QgsLayoutSize(50,12))
assert project.write()
legend=layout.itemById('legend')
legend.attemptMove(QgsLayoutPoint(70,55))
project.setFileName(sys.argv[2]);assert project.write()
project.clear();app.exitQgis()
"""
    moved_legend = Path(result["project_file"]).with_name("edited-overlap.qgs")
    edited = subprocess.run(
        [str(runtime.python_executable), "-c", script, result["project_file"], str(moved_legend)],
        env=child_environment(runtime, tmp_path / "native-edit"),
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert edited.returncode == 0, edited.stderr
    with_note = await execute_qgis(
        "project_export", {"project_path": result["project_file"], "dpi": 96},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert with_note["status"] == "succeeded", with_note.get("error_summary")
    bounds = with_note["result"]["layout"]["bounds_check"]
    assert bounds["valid"] is True
    assert "user_note" in bounds["user_items"]
    assert "user_note" not in bounds["items"]
    preserved = await execute_qgis(
        "project_inspect", {"project_path": with_note["result"]["project_file"]},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert preserved["status"] == "succeeded", preserved.get("error_summary")
    assert "user_note" in preserved["result"]["layouts"][0]["bounds_check"]["user_items"]
    moved = await execute_qgis(
        "project_export", {"project_path": str(moved_legend), "dpi": 96},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert moved["status"] == "succeeded", moved.get("error_summary")
    assert moved["result"]["layout"]["bounds_check"]["valid"] is False
    assert moved["result"]["quality_status"] == "draft"
    assert moved["result"]["layout"]["bounds_check"]["items"]["legend"][:2] == [70, 55]


@pytest.mark.parametrize("part", ["title", "legend", "footer"])
async def test_native_template_rejects_content_overflow_without_exporting_preview(
    tmp_path, native_settings, part
):
    payload = {"layers": _sources(tmp_path), "title": "测试地图", "layout": {}}
    if part == "title":
        payload["title"] = "超长标题" * 100
    elif part == "footer":
        payload["layout"]["footer_text"] = "说明文字" * 1000
    else:
        payload["layers"][0]["style"]["ranges"][0]["label"] = "超长图例" * 100
    result = await execute_qgis(
        "project_create", payload, cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert result["status"] == "failed"
    assert "reserved rectangle" in result["error_summary"]
    assert not (Path(result["run_dir"]) / "preview.png").exists()


async def test_native_hillshade_roundtrip_preserves_renderer_and_rejects_missing_band(
    tmp_path, native_settings
):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    source = tmp_path / "elevation.tif"
    values = np.arange(400, dtype=np.float32).reshape(20, 20)
    with rasterio.open(
        source, "w", driver="GTiff", width=20, height=20, count=1,
        dtype="float32", crs="EPSG:3857", transform=from_origin(0, 2000, 100, 100),
    ) as dataset:
        dataset.write(values, 1)
    style = {"renderer": "hillshade", "band": 1, "azimuth": 300, "altitude": 35}
    payload = GeoQgisProjectInput(
        action="create", layers=[{"file_path": str(source), "kind": "raster",
                                  "raster_style": style}],
        title="地形阴影", layout={"legend": False}, dpi=72,
    ).model_dump(exclude={"action"}, exclude_none=True)
    created = await execute_qgis(
        "project_create", payload, cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert created["status"] == "succeeded", created.get("error_summary")
    assert created["result"]["layers"][0]["raster_style"] == style
    inspected = await execute_qgis(
        "project_inspect", {"project_path": created["result"]["project_file"]},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert inspected["status"] == "succeeded", inspected.get("error_summary")
    assert inspected["result"]["layers"][0]["raster_style"] == style
    payload["layers"][0]["raster_style"]["band"] = 2
    rejected = await execute_qgis(
        "project_create", payload, cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert rejected["status"] == "failed"
    assert "Hillshade band 2 does not exist" in rejected["error_summary"]


async def test_explicit_font_is_packaged_and_restored_without_system_font_registration(
    tmp_path, native_settings
):
    system_root = os.environ.get("WINDIR", os.environ.get("SystemRoot", ""))
    installed = Path(system_root) / "Fonts/msyh.ttc"
    if not system_root or not installed.is_file():
        pytest.skip("A locally installed static Chinese font is required for this fixture")
    configured = tmp_path / "user-font.ttc"
    shutil.copy2(installed, configured)
    source_digest = hashlib.sha256(configured.read_bytes()).hexdigest()
    sources = _sources(tmp_path)
    for source in sources:
        source["label_style"] = {"font_file": str(configured)}
    payload = GeoQgisProjectInput(
        action="create", layers=sources, title="自定义中文字体", dpi=96,
        layout={"font_file": str(configured), "footer_text": "字体资源验证"},
    ).model_dump(exclude={"action"}, exclude_none=True)
    created = await execute_qgis(
        "project_create", payload, cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert created["status"] == "succeeded", created.get("error_summary")
    result = created["result"]
    info = result["layout"]["font"]
    assert info["packaged"] is True and info["registered"] is True
    assert not Path(info["file"]).is_absolute()
    snapshot = Path(result["project_dir"]) / info["file"]
    assert snapshot.is_file()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == source_digest
    assert hashlib.sha256(configured.read_bytes()).hexdigest() == source_digest
    manifest = json.loads(Path(result["project_manifest"]).read_text(encoding="utf-8"))
    assert [value for value in manifest["files"] if value.startswith("assets/fonts/")] == [
        info["file"]
    ]
    readme = (Path(result["project_dir"]) / "README.md").read_text(encoding="utf-8")
    assert "QGIS desktop does not automatically register" in readme
    configured.unlink()  # The original source is no longer present in subsequent processes.
    runtime = discover_qgis(native_settings, cwd=tmp_path)
    worker = Path(__file__).parents[1] / "src/geoharness/qgis/worker.py"
    script = """
import json,os,runpy,sys
from pathlib import Path
runpy.run_path(sys.argv[1],run_name='font_isolation')
from qgis.core import QgsApplication,QgsProject
from qgis.PyQt.QtGui import QFontDatabase
from geoharness.qgis.project_worker import dispatch
from geoharness.qgis.worker_context import WorkerContext
QgsApplication.setPrefixPath(os.environ['QGIS_PREFIX_PATH'],True)
app=QgsApplication([],False);app.initQgis()
database=QFontDatabase();database.removeAllApplicationFonts()
before=database.families()
context=WorkerContext(Path(sys.argv[3]),Path(sys.argv[4]),[Path(sys.argv[3])])
details=dispatch('project_export',{'project_path':sys.argv[2],'dpi':96},context)
print(json.dumps({'before':before,'details':details},ensure_ascii=False))
QgsProject.instance().clear();app.exitQgis()
"""
    isolated = subprocess.run(
        [str(runtime.python_executable), "-c", script, str(worker), result["project_file"],
         str(tmp_path), str(tmp_path / "isolated-export")],
        env=child_environment(runtime, tmp_path / "isolated-runtime"),
        capture_output=True, text=True, encoding="utf-8", timeout=45,
    )
    assert isolated.returncode == 0, isolated.stderr
    response = json.loads(isolated.stdout)
    assert info["family"] not in response["before"]
    exported = response["details"]
    assert exported["layout"]["font"]["registered"] is True
    assert exported["layout"]["font"]["loaded"] is True
    assert (Path(exported["project_dir"]) / exported["layout"]["font"]["file"]).is_file()
    assert exported["image_sha256"] == result["image_sha256"]
    inspected = await execute_qgis(
        "project_inspect", {"project_path": exported["project_file"]},
        cwd=tmp_path, settings=native_settings, read_roots=[tmp_path],
    )
    assert inspected["status"] == "succeeded", inspected.get("error_summary")
    assert inspected["result"]["layouts"][0]["font"]["registered"] is True
