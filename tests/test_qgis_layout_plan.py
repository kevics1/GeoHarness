"""Public layout plans freeze native frames independently of temporary containers."""

import copy
import hashlib

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig, QgisConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import (
    GeoQgisProjectInput,
    GeoQgisProjectTool,
    QgisLayoutPlan,
)


def test_frames_select_original_layer_indices_without_reordering_sources():
    arguments = GeoQgisProjectInput(
        action="create",
        layers=[{"file_path": f"layer-{index}.geojson"} for index in range(3)],
        layout={
            "main_frame": {"focus_layers": [1], "render_layers": [1, 0]},
            "detail_frame": {"focus_layers": [2], "render_layers": [2]},
        },
    )
    assert arguments.layout.main_frame.render_layers == [0, 1]
    assert arguments.layout.detail_frame.focus_layers == [2]
    assert [layer.file_path for layer in arguments.layers] == [
        "layer-0.geojson", "layer-1.geojson", "layer-2.geojson",
    ]


def test_plan_roundtrip_preserves_native_numeric_digest():
    from geoharness.qgis.project_worker import _json_digest

    plan = {"schema_version": 1, "input_digest": "a" * 64, "runtime_identity": {},
            "source_identities": [], "font_identities": {}, "parameters": {},
            "regions": {"main_map": [12, 14.5, 200, 90.3]}, "frames": {},
            "content_metrics": {}, "adaptations": []}
    plan["plan_digest"] = _json_digest(plan)
    serialized = QgisLayoutPlan.model_validate(plan).model_dump(exclude_none=True)
    assert _json_digest({k: v for k, v in serialized.items() if k != "plan_digest"}) == (
        plan["plan_digest"])


@pytest.mark.parametrize("overrides", [
    {"layout": {"main_frame": {"focus_layers": [4]}}},
    {"layout": {"main_frame": {"render_layers": [0]}}},
    {"layout": {"main_frame": {"focus_layers": [0, 0]}}},
    {"layout": {"main_frame": {"focus_layers": [1], "render_layers": [0]}}},
    {"layout": {"detail_frame": {"render_layers": [1]}}},
    {"map_extent": [0, 0, 10, 10], "crs": "EPSG:3857",
     "layout": {"main_frame": {"focus_layers": [0]}}},
])
def test_invalid_frame_contracts_fail_before_execution(overrides):
    with pytest.raises(ValidationError):
        GeoQgisProjectInput(action="create", layers=[
            {"file_path": "first.geojson"}, {"file_path": "second.geojson"}], **overrides)


def _native_case(tmp_path):
    settings = QgisConfig(outputs_dir=str(tmp_path / "native-output"))
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    sources = [tmp_path / f"source-{index}.geojson" for index in range(3)]
    for path, names, geometries in (
        (sources[0], ["background"], [box(-10000, -10000, 20000, 20000)]),
        (sources[1], ["main subject"], [box(1000, 1000, 1100, 1200)]),
        (sources[2], ["A", "B"], [Point(5000, 5000), Point(5030, 5000)]),
    ):
        gpd.GeoDataFrame({"name": names}, geometry=geometries, crs=3857).to_file(path)
    arguments = GeoQgisProjectInput(action="preflight", crs="EPSG:3857", title="布局计划",
        layers=[
            {"file_path": str(sources[0]), "name": "背景", "required": False},
            {"file_path": str(sources[1]), "name": "主体范围及经原生字体测量的长图例名称"},
            {"file_path": str(sources[2]), "name": "详图关注点", "label_field": "name",
             "style": {"size_mm": 1}},
        ], layout={
            "width_mm": 297, "height_mm": 420, "sidebar_width_mm": 40,
            "footer_height_mm": 10, "footer_text": "原生测量说明\n" * 6,
            "main_frame": {"focus_layers": [1], "render_layers": [1, 0], "padding_ratio": 0.1},
            "detail_frame": {"focus_layers": [2], "render_layers": [2, 0]},
        })
    return arguments, ToolExecutionContext(cwd=tmp_path,
        metadata={"geoharness_config": GeoConfig(qgis=settings)})


async def test_native_preflight_freezes_effective_frames_and_excludes_background(tmp_path):
    arguments, context = _native_case(tmp_path)
    before = {layer.file_path: hashlib.sha256(open(layer.file_path, "rb").read()).hexdigest()
              for layer in arguments.layers}
    result = await GeoQgisProjectTool().execute(arguments, context)
    assert not result.is_error, result.output
    plan = result.metadata["preflight"]["layout_plan"]
    assert result.metadata["render_executed"] is False
    assert not list(tmp_path.rglob("preview.png"))
    assert plan["parameters"]["sidebar_width_mm"] > 40
    assert plan["parameters"]["footer_height_mm"] > 10
    main, detail = plan["frames"]["main_map"], plan["frames"]["detail_map"]
    assert main["render_layers"] == [0, 1] and detail["render_layers"] == [0, 2]
    assert main["focus_bbox"] == [1000, 1000, 1100, 1200]
    assert (main["extent"][0] + main["extent"][2]) / 2 == pytest.approx(1050)
    assert (main["extent"][1] + main["extent"][3]) / 2 == pytest.approx(1100)
    assert main["extent"][2] - main["extent"][0] < 5000
    composition = result.metadata["preflight"]["composition"]["map_frames"]
    assert composition["main_map"]["labeled_point_count"] == 0
    assert composition["detail_map"]["labeled_point_count"] == 2
    assert composition["detail_map"]["warnings"][0]["layer_indices"] == [2]
    assert result.metadata["preflight"]["map_extent"] == main["extent"]
    create = arguments.model_dump(exclude_unset=True)
    create.update(action="create", dpi=150)
    create["layout"]["layout_plan"] = plan
    rendered = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput.model_validate(create), context)
    assert not rendered.is_error, rendered.output
    actual = rendered.metadata["result"]["layout_plan"]
    assert actual == plan
    budget = context.metadata["map_workflow_state"]["maps"]
    renders = sum(entry["renders"] for entry in budget.values())
    for field, value in (("title", "changed"), ("dpi", 300), ("style", {"width_mm": 2})):
        changed = copy.deepcopy(create)
        if field == "style":
            changed["layers"][1][field] = value
        else:
            changed[field] = value
        rejected = await GeoQgisProjectTool().execute(
            GeoQgisProjectInput.model_validate(changed), context)
        assert rejected.is_error and "layout_plan is stale" in rejected.output
        assert rejected.metadata["render_executed"] is False
    assert sum(entry["renders"] for entry in budget.values()) == renders
    for path, digest in before.items():
        assert hashlib.sha256(open(path, "rb").read()).hexdigest() == digest
    source = arguments.layers[1].file_path
    gpd.GeoDataFrame({"name": ["main subject"]}, geometry=[box(1000, 1000, 1120, 1200)],
                     crs=3857).to_file(source)
    rejected = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput.model_validate(create), context)
    assert rejected.is_error and "layout_plan is stale" in rejected.output
    assert sum(entry["renders"] for entry in budget.values()) == renders


async def test_required_detail_visibility_and_optional_threshold_use_final_frame_union(tmp_path):
    arguments, context = _native_case(tmp_path)
    payload = arguments.model_dump()
    payload.update(map_extent=[900, 900, 1200, 1300])
    payload["layout"]["main_frame"].pop("focus_layers")
    payload["layers"][0]["minimum_visible_features"] = 10
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput.model_validate(payload), context)
    assert not result.is_error, result.output
    checks = result.metadata["preflight"]["frame_checks"]
    assert checks["valid"] is True
    assert checks["layers"][0]["valid"] is False
    assert checks["layers"][1]["frames"] == {"main_map": 1}
    assert checks["layers"][2]["frames"] == {"detail_map": 2}


async def test_native_hard_collision_is_reported_before_render_and_rejected_on_create(tmp_path):
    arguments, context = _native_case(tmp_path)
    source = arguments.layers[2].file_path
    gpd.GeoDataFrame({'name': ['A', 'B']},
        geometry=[Point(5000, 5000), Point(5001, 5001)], crs=3857).to_file(source)
    preflight = await GeoQgisProjectTool().execute(arguments, context)
    assert not preflight.is_error, preflight.output
    assert preflight.metadata['preflight']['status'] == 'needs_repair'
    assert 'create_call' not in __import__('json').loads(preflight.output)
    rendered = await GeoQgisProjectTool().execute(
        arguments.model_copy(update={'action': 'create'}), context)
    assert rendered.is_error and not rendered.metadata['render_executed']
    assert not list(tmp_path.rglob('preview.png'))
