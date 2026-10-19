"""Project tool contract, render budgets and project source preflight."""

from __future__ import annotations

import asyncio
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from xml.sax.saxutils import escape

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError

from geoharness.config.settings import DataConfig, GeoConfig, MapReviewConfig, QgisConfig
from geoharness.qgis.project_worker import (
    _builtin_svg_roots,
    _local_source,
    _preflight_project,
    _project_layers,
    _relocate_symbol_assets,
    _resolve_asset,
)
from geoharness.qgis.worker_context import WorkerContext
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


@pytest.fixture(autouse=True)
def source_for_create_contracts(tmp_path):
    from shapely.geometry import Point

    directory = tmp_path / "data"
    directory.mkdir(exist_ok=True)
    gpd.GeoDataFrame({"risk": ["high", "low"], "name": ["A", "B"]},
                     geometry=[Point(0, 0), Point(1, 1)], crs=4326).to_file(
        directory / "source.geojson", driver="GeoJSON",
    )


def _context(tmp_path: Path, *, enabled: bool = True, repairs: int = 2) -> ToolExecutionContext:
    config = GeoConfig(
        qgis=QgisConfig(enabled=enabled),
        map_review=MapReviewConfig(max_repairs=repairs),
        data=DataConfig(file_dir=str(tmp_path / "data")),
    )
    return ToolExecutionContext(
        cwd=tmp_path,
        hook_executor=None,
        metadata={"geoharness_config": config},
    )


def _create(**updates) -> GeoQgisProjectInput:
    return GeoQgisProjectInput(
        **{"action": "create", "layers": [{"file_path": "data/source.geojson"}], **updates},
    )


async def test_unrelated_previous_analysis_cannot_attach_to_new_thematic_map(tmp_path, monkeypatch):
    payloads = []

    async def runner(action, payload, **kwargs):
        payloads.append(payload)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    context = _context(tmp_path)
    context.metadata["map_analysis_facts"] = {
        "status": "verified", "analyses": [], "footer_text": "旧地图的统计",
    }
    result = await GeoQgisProjectTool().execute(_create(), context)
    assert result.is_error and 'NativeOutputContractError' in result.output
    assert payloads[0]["analysis_facts"]["status"] == "none"
    assert payloads[0]["analysis_facts"]["analyses"] == []
    assert "旧地图" not in json.dumps(payloads[0]["analysis_facts"], ensure_ascii=False)
    assert "旧地图" not in (payloads[0].get("layout") or {}).get("footer_text", "")


async def test_qgis_deterministic_layout_failure_is_cached_but_transient_failure_is_not(
    tmp_path, monkeypatch,
):
    calls = []

    async def runner(action, payload, **kwargs):
        calls.append(action)
        return {"status": "failed", "error_summary": "ValueError: footer_text exceeds bounds"}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    tool, context = GeoQgisProjectTool(), _context(tmp_path)
    first = await tool.execute(_create(), context)
    second = await tool.execute(_create(), context)
    assert first.is_error and second.metadata["duplicate_error_blocked"]
    assert len(calls) == 1
    json.dumps(context.metadata["map_deterministic_errors"])

    async def transient(action, payload, **kwargs):
        calls.append(action)
        return {"status": "failed", "error_summary": "PermissionError: file temporarily locked"}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", transient)
    context = _context(tmp_path)
    await tool.execute(_create(), context)
    await tool.execute(_create(), context)
    assert len(calls) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "create"},
        {"action": "inspect"},
        {"action": "export", "project_path": "map.qgz", "crs": "EPSG:3857"},
        {"action": "export", "project_path": "map.qgz", "layers": [{"file_path": "map.gpkg"}]},
        {
            "action": "create",
            "layers": [{"file_path": "source.geojson"}],
            "map_extent": [0, 0, 0, 1],
        },
        {
            "action": "create",
            "layers": [{"file_path": "source.geojson"}],
            "map_extent": [0, 0, float("inf"), 1],
        },
        {
            "action": "create",
            "layers": [{"file_path": "raster.tif", "kind": "raster", "style": {"color": "red"}}],
        },
        {
            "action": "create",
            "layers": [
                {
                    "file_path": "source.geojson",
                    "style": {"renderer": "categorized", "field": "risk"},
                }
            ],
        },
        {
            "action": "create",
            "layers": [
                {
                    "file_path": "source.geojson",
                    "style": {
                        "renderer": "categorized",
                        "field": "risk",
                        "categories": [
                            {"value": "high", "color": "red"},
                            {"value": "high", "color": "blue"},
                        ],
                    },
                }
            ],
        },
        {
            "action": "create",
            "layers": [{"file_path": "source.geojson"}],
            "layout": {"width_mm": 60, "height_mm": 60, "margin_mm": 12},
        },
    ],
)
def test_invalid_or_ambiguous_requests_are_rejected(payload):
    with pytest.raises(ValidationError):
        GeoQgisProjectInput.model_validate(payload)


async def test_create_forwards_styles_and_roots_and_flattens_delivery_metadata(
    tmp_path, monkeypatch
):
    calls = []

    async def fake_runner(action, payload, **kwargs):
        calls.append((action, payload, kwargs))
        return {
            "status": "succeeded",
            "run_dir": str(tmp_path / "run"),
            "result": {
                "project_dir": str(tmp_path / "run"),
                "project_file": "",
                "project_manifest": str(tmp_path / 'run/project.json'),
                "preview_path": str(tmp_path / "run/preview.png"),
                "quality_status": "draft",
                "visual_review_status": "unverified",
                "image_sha256": "digest",
            },
        }

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", fake_runner)
    context = _context(tmp_path)
    arguments = _create(
        map_id="fire-risk",
        crs="EPSG:3857",
        layers=[
            {
                "file_path": "data/source.geojson",
                "style": {
                    "renderer": "categorized",
                    "field": "risk",
                    "categories": [
                        {"value": "high", "label": "High risk", "color": "#ff0000"},
                        {"value": "low", "label": "Low risk", "color": "#00ff00"},
                    ],
                },
                "label_field": "name",
            }
        ],
    )
    result = await GeoQgisProjectTool().execute(arguments, context)
    assert not result.is_error
    assert not result.metadata.get('project_file')
    assert calls[0][1]['_preview_only'] is True
    assert result.metadata["quality_status"] == "draft"
    assert result.metadata["visual_review_status"] == "unverified"
    assert calls[0][0] == "project_create"
    assert calls[0][1]["layers"][0]["style"]["categories"][0]["color"] == "#ff0000"
    assert (tmp_path / "data").resolve() in calls[0][2]["read_roots"]
    assert context.metadata["map_workflow_state"]["maps"]["fire-risk"]["renders"] == 1


async def test_inspection_does_not_reserve_a_map_version(tmp_path, monkeypatch):
    async def fake_runner(action, payload, **kwargs):
        assert action == "project_inspect"
        assert payload["project_path"] == "map.qgz"
        return {"status": "succeeded", "result": {"layouts": [{"name": "Edited"}]}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", fake_runner)
    context = _context(tmp_path)
    arguments = GeoQgisProjectInput(action="inspect", project_path="map.qgz")
    tool = GeoQgisProjectTool()
    result = await tool.execute(arguments, context)
    assert not result.is_error
    assert tool.is_read_only(arguments)
    assert "map_workflow_state" not in context.metadata


async def test_create_and_export_share_budget_and_keep_saved_layout(tmp_path, monkeypatch):
    calls = []

    async def fake_runner(action, payload, **kwargs):
        calls.append((action, payload))
        return {"status": "succeeded", "result": {"quality_status": "draft",
            'project_dir': str(tmp_path / 'run'),
            'project_manifest': str(tmp_path / 'run/project.json'),
            'preview_path': str(tmp_path / 'run/preview.png')}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", fake_runner)
    context = _context(tmp_path, repairs=0)
    await GeoQgisProjectTool().execute(_create(map_id="stable-map"), context)
    exported = GeoQgisProjectInput(
        action="export",
        project_path="edited.qgz",
        layout_name="User layout",
        map_id="stable-map",
    )
    result = await GeoQgisProjectTool().execute(exported, context)
    assert not result.metadata.get("render_budget_exhausted", False)
    assert len(calls) == 2
    context = _context(tmp_path)
    result = await GeoQgisProjectTool().execute(exported, context)
    assert not result.is_error
    assert calls[-1][0] == "project_export"
    assert calls[-1][1]["layout_name"] == "User layout"
    assert calls[-1][1]["layers"] == []


@pytest.mark.parametrize("map_id", ["", "explicit"])
async def test_export_recovers_saved_project_job_unless_map_id_is_explicit(
    tmp_path, monkeypatch, map_id,
):
    from geoharness.cartography.tracking import bind_map_job

    context = _context(tmp_path)
    saved_id = bind_map_job(context, "saved-map")
    (tmp_path / "saved-as.qgs").write_text("<qgis/>", encoding="utf-8")
    recovered = []
    calls = []

    def recover(metadata, **kwargs):
        recovered.append(kwargs["project_path"])
        return saved_id

    async def runner(action, payload, **kwargs):
        calls.append(payload)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.cartography.jobs.saved_project_job", recover)
    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="export", project_path="saved-as.qgs", map_id=map_id), context,
    )
    assert result.is_error and 'NativeOutputContractError' in result.output
    assert calls[0]["project_path"] == "saved-as.qgs"
    assert recovered == ([] if map_id else [Path("saved-as.qgs")])
    if not map_id:
        assert calls[0]["map_id"] == saved_id


@pytest.mark.parametrize("failure", ["failed", "exception", "cancelled"])
async def test_failed_or_cancelled_render_refunds_owned_reservation(tmp_path, monkeypatch, failure):
    async def fake_runner(*args, **kwargs):
        if failure == "exception":
            raise OSError("runtime unavailable")
        if failure == "cancelled":
            raise asyncio.CancelledError
        return {"status": "failed", "result": None, "error_summary": "QGIS export failed"}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", fake_runner)
    context = _context(tmp_path, repairs=0)
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await GeoQgisProjectTool().execute(_create(map_id="same"), context)
    else:
        result = await GeoQgisProjectTool().execute(_create(map_id="same"), context)
        assert result.is_error
    assert context.metadata["map_workflow_state"]["maps"]["same"]["renders"] == 0


async def test_disabled_backend_stops_before_native_process(tmp_path, monkeypatch):
    async def forbidden_runner(*args, **kwargs):
        pytest.fail("disabled QGIS must not start a process")

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", forbidden_runner)
    result = await GeoQgisProjectTool().execute(_create(), _context(tmp_path, enabled=False))
    assert result.is_error
    assert "disabled" in result.output


def _project(path: Path, *, source: str, provider: str = "ogr", extra: str = "") -> Path:
    xml = (
        '<qgis><projectlayers><maplayer type="vector"><provider>'
        + escape(provider)
        + "</provider><datasource>"
        + escape(source)
        + "</datasource></maplayer></projectlayers>"
        + extra
        + "</qgis>"
    )
    if path.suffix == ".qgz":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("map.qgs", xml)
    else:
        path.write_text(xml, encoding="utf-8")
    return path


@pytest.mark.parametrize("suffix", [".qgs", ".qgz"])
def test_preflight_resolves_project_relative_sources_and_assets(tmp_path, suffix):
    folder = tmp_path / "package"
    folder.mkdir()
    source = folder / "data.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    asset = folder / "marker.svg"
    asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    path = _project(
        folder / ("map" + suffix),
        source="./data.geojson",
        extra=('<symbols><Option name="name" value="./marker.svg" /></symbols>'),
    )
    context = WorkerContext(tmp_path, tmp_path / "run")
    assert _preflight_project(path, context) == [asset.resolve()]


@pytest.mark.parametrize(
    "provider,source,extra",
    [
        ("wms", "https://example.com/service", ""),
        ("postgres", "dbname='private' password='private'", ""),
        ("ogr", "https://example.com/data.geojson", ""),
        ("ogr", "data.geojson|subset=run_code()", ""),
        ("ogr", "data.geojson", '<LayoutItem picturePath="https://example.com/map.png" />'),
        ("ogr", "data.geojson", '<prop k="name" v="https://example.com/marker.svg" />'),
        (
            "ogr",
            "data.geojson",
            "<properties><Macros><pythonCode>print('run')</pythonCode></Macros></properties>",
        ),
        ("ogr", "data.geojson", '<layer-tree-layer embedded="1" />'),
    ],
)
def test_preflight_denies_unsafe_sources_before_qgis_read(tmp_path, provider, source, extra):
    (tmp_path / "data.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    path = _project(tmp_path / "map.qgs", source=source, provider=provider, extra=extra)
    with pytest.raises((ValueError, PermissionError)):
        _preflight_project(path, WorkerContext(tmp_path, tmp_path / "run"))


def test_preflight_does_not_grant_access_to_source_outside_allowed_roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.geojson"
    outside.write_text('{"type":"FeatureCollection","features":[]}')
    path = _project(workspace / "map.qgs", source=str(outside))
    with pytest.raises(PermissionError):
        _preflight_project(path, WorkerContext(workspace, workspace / "run"))


def test_local_source_rejects_vrt_indirection_and_credentials(tmp_path):
    context = WorkerContext(tmp_path, tmp_path / "run")
    path = tmp_path / "data.vrt"
    path.write_text("<VRTDataset/>")
    with pytest.raises(ValueError, match="VRT"):
        _local_source(str(path), context)
    path = tmp_path / ".env.geojson"
    path.write_text("{}")
    with pytest.raises(PermissionError, match="Credential"):
        _local_source(str(path), context)


def test_saved_project_snapshots_hidden_dependencies_without_changing_tree_order():
    base = SimpleNamespace(id=lambda: "base")
    overlay = SimpleNamespace(id=lambda: "overlay")
    hidden_join = SimpleNamespace(id=lambda: "hidden_join")
    saved_tree_order = [overlay, base]
    project = SimpleNamespace(
        layerTreeRoot=lambda: SimpleNamespace(layerOrder=lambda: saved_tree_order),
        mapLayers=lambda: {"hidden_join": hidden_join, "base": base, "overlay": overlay},
    )
    assert _project_layers(project) == [base, overlay, hidden_join]
    assert saved_tree_order == [overlay, base]


@pytest.mark.parametrize(
    "resource",
    [
        '<Option name="name" value="health/marker.svg" />',
        '<prop k="name" v="health/marker.svg" />',
    ],
)
def test_builtin_svg_names_and_legacy_keys_resolve_to_authorized_runtime_assets(
    tmp_path,
    monkeypatch,
    resource,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.geojson").write_text('{"type":"FeatureCollection","features":[]}')
    official_svg = tmp_path / "runtime" / "svg"
    asset = official_svg / "health" / "marker.svg"
    asset.parent.mkdir(parents=True)
    asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    monkeypatch.setattr("geoharness.qgis.project_worker._builtin_svg_roots", lambda: [official_svg])
    context = WorkerContext(workspace, workspace / "run")
    path = _project(workspace / "map.qgs", source="data.geojson", extra=resource)
    sources = {}
    assert _preflight_project(path, context, sources) == [asset.resolve()]
    assert sources == {"health/marker.svg": asset.resolve()}
    with pytest.raises(PermissionError):
        context.read_path(str(asset))  # Builtin permission must not widen general read roots.
    outside = tmp_path / "outside.svg"
    outside.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    with pytest.raises(PermissionError):
        _resolve_asset(str(outside), path, context)
    with pytest.raises(PermissionError):
        _resolve_asset("../../outside.svg", path, context)


def test_builtin_svg_roots_exclude_personal_and_custom_search_directories(tmp_path, monkeypatch):
    prefix = tmp_path / "runtime"
    official_svg = prefix / "svg"
    personal_svg = tmp_path / "profile" / "svg"
    custom_svg = tmp_path / "custom" / "svg"
    core = ModuleType("qgis.core")
    core.QgsApplication = SimpleNamespace(
        prefixPath=lambda: str(prefix),
        pkgDataPath=lambda: str(prefix),
        svgPaths=lambda: [str(personal_svg), str(official_svg), str(custom_svg)],
    )
    monkeypatch.setitem(sys.modules, "qgis", ModuleType("qgis"))
    monkeypatch.setitem(sys.modules, "qgis.core", core)
    assert _builtin_svg_roots() == [official_svg.resolve()]


def test_asset_relocation_updates_nested_markers_and_fills_without_replacing_symbols():
    changed = []
    nested_fill = SimpleNamespace(
        svgFilePath=lambda: "builtin/fill.svg",
        setSvgFilePath=lambda value: changed.append(value),
        subSymbol=lambda: None,
    )
    untouched = SimpleNamespace(
        path=lambda: "local/unchanged.svg",
        setPath=lambda value: pytest.fail("unexpected rewrite"),
        subSymbol=lambda: SimpleNamespace(symbolLayers=lambda: [nested_fill]),
    )
    marker = SimpleNamespace(
        path=lambda: "builtin/marker.svg",
        setPath=lambda value: changed.append(value),
        subSymbol=lambda: None,
    )
    originals = [marker, untouched]
    symbol = SimpleNamespace(symbolLayers=lambda: originals)
    replacements = {"builtin/marker.svg": "assets/0.svg", "builtin/fill.svg": "assets/1.svg"}
    _relocate_symbol_assets(symbol, lambda value: replacements.get(value, value))
    assert changed == ["assets/0.svg", "assets/1.svg"]
    assert symbol.symbolLayers() == originals
