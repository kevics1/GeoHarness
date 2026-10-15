"""Small checks for incremental specs, explicit selection and native roster paging."""

import csv
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.layout_table import select_roster
from geoharness.qgis.specification import load_spec, patch_spec, save_spec
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_spec_patch_is_immutable_and_hash_checked(tmp_path):
    settings = GeoConfig().qgis
    original = {"layers": [{"file_path": "a.gpkg", "style": {"color": "#ffffff"}}]}
    reference, path = save_spec(original, cwd=tmp_path, settings=settings)
    updated = patch_spec(load_spec(reference, cwd=tmp_path, settings=settings),
                         [{"path": ["layers", 0, "style", "color"], "value": "#000000"}])
    assert original["layers"][0]["style"]["color"] == "#ffffff"
    assert updated["layers"][0]["style"]["color"] == "#000000"
    Path(path).write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        load_spec(reference, cwd=tmp_path, settings=settings)


def test_roster_selection_is_explicit_and_complete_by_default():
    records = [{"id": index, "risk": index / 10} for index in range(234)]
    assert len(select_roster(records, {"fields": ["id", "risk"]})) == 234
    chosen = select_roster(records, {"fields": ["id", "risk"], "sort_by": "risk",
                                     "descending": True, "top_n": 12})
    assert [row["id"] for row in chosen] == list(range(233, 221, -1))
    with pytest.raises(ValueError, match="absent"):
        select_roster(records, {"fields": ["id", "risk"], "keys": [999]})
    with pytest.raises(ValueError, match="sort_by"):
        GeoQgisProjectInput(action="spec", layers=[{"file_path": "a.gpkg"}],
            layout={"label_table": {"layer_index": 0, "fields": ["id", "risk"], "top_n": 12}})


def test_explicit_geographic_extent_is_checked_with_native_frames(tmp_path):
    source = tmp_path / "points.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(102, 28)], crs=4326).to_file(source)
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    arguments = GeoQgisProjectInput(action="preflight", crs="EPSG:4326",
        map_extent=[791596, 3068661, 822956, 3118342],
        layers=[{"file_path": str(source)}],
        layout={"main_frame": {"render_layers": [0]}})
    with pytest.raises(ValueError, match="longitude/latitude"):
        GeoQgisProjectTool()._preflight(arguments, context)


async def test_native_incremental_spec_paginates_all_roster_rows(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    source = tmp_path / "residents.gpkg"
    gpd.GeoDataFrame({"id": range(234), "name": [f"居民点 {i}" for i in range(234)]},
        geometry=[Point(i * 10, i % 20 * 100) for i in range(234)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    draft = await tool.execute(GeoQgisProjectInput(action='spec', title='草案'), context)
    assert not draft.is_error, draft.output
    saved = await tool.execute(GeoQgisProjectInput(action="spec",
        spec_ref=draft.metadata['spec_ref'],
        patches=[{'path':['crs'], 'value':'EPSG:3857'}, {'path':['dpi'], 'value':36},
                 {'path':['layers',0], 'value':{'file_path':str(source)}},
                 {'path':['layout'], 'value':{"legend": False, "scale_bar": False,
            "label_table": {"layer_index": 0, "fields": ["id", "name"], "columns": 3,
                            "sort_by":"id", "descending":True, "height_mm": 20}}}]), context)
    assert not saved.is_error, saved.output
    checked = await tool.execute(GeoQgisProjectInput(action="preflight",
        spec_ref=saved.metadata["spec_ref"],
        patches=[{"path": ["title"], "value": "居民点名册"}]), context)
    assert not checked.is_error, checked.output
    created = await tool.execute(GeoQgisProjectInput(action="create",
        plan_ref=checked.metadata["plan_ref"]), context)
    assert not created.is_error, created.output
    manifest = json.loads(Path(created.metadata["project_manifest"]).read_text(encoding="utf-8"))
    tables = manifest["layout"]["label_tables"]
    assert sum(table["rows"] for table in tables) == 234
    assert sum(table["visible_rows"] for table in tables) == 234
    assert manifest["layout"]["pages"] > 1
    assert manifest["layout"]["bounds_check"]["valid"]
    assert manifest["native_facts"]["roster_previews"]
    assert 'roster_full.csv' in manifest['files']
    assert all(Path(path).name in manifest['files']
               for path in manifest['native_facts']['roster_previews'])
    assert tables[0]['contents'][0][0] == 233
    with (Path(created.metadata["project_manifest"]).parent / "roster_full.csv").open(
        encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 234
    exported = await tool.execute(GeoQgisProjectInput(action='export',
        project_path=created.metadata['project_file'], dpi=36), context)
    assert not exported.is_error, exported.output
    package = json.loads(Path(exported.metadata['project_manifest']).read_text(encoding='utf-8'))
    assert 'roster_full.csv' in package['files']
    assert sum(table['rows'] for table in package['layout']['label_tables']) == 234
