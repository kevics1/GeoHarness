"""Tests for multi-layer map composition (render_layers + tool layers param).

Fire-incident and thematic maps stack several layers on ONE canvas. The old
single-layer-only API made the model fake composition by passing a directory
path or wildcard as file_path — which rendered nothing and burned turns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import CartographyConfig, GeoConfig
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool

gpd = pytest.importorskip("geopandas")
plt = pytest.importorskip("matplotlib.pyplot")
shapely_point = pytest.importorskip("shapely.geometry")


def _poly_gdf(crs: str = "EPSG:4326"):
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[box(0, 0, 1, 1)], crs=crs
    )


def _point_gdf(crs: str = "EPSG:4326"):
    return gpd.GeoDataFrame(
        {"name": ["p"]},
        geometry=[shapely_point.Point(0.5, 0.5)],
        crs=crs,
    )


class TestRenderLayers:
    def test_png_composes_layers(self, tmp_path: Path) -> None:
        from geoharness.cartography import render_layers

        out = tmp_path / "multi.png"
        written = render_layers(
            [
                {"gdf": _poly_gdf(), "label": "面图层"},
                {"gdf": _point_gdf(), "label": "点图层"},
            ],
            title="组合测试",
            output_path=out,
        )
        assert written.exists()
        assert written.stat().st_size > 1000

    def test_empty_layers_raises(self, tmp_path: Path) -> None:
        from geoharness.cartography import render_layers

        with pytest.raises(ValueError, match="at least one layer"):
            render_layers([], output_path=tmp_path / "x.png")

    def test_missing_gdf_raises(self, tmp_path: Path) -> None:
        from geoharness.cartography import render_layers

        with pytest.raises(ValueError, match="missing 'gdf'"):
            render_layers([{"label": "no-data"}], output_path=tmp_path / "x.png")

    def test_invalid_layer_gdf_raises(self, tmp_path: Path) -> None:
        from geoharness.cartography import render_layers

        with pytest.raises(Exception):
            render_layers(
                [{"gdf": "not-a-gdf", "label": "bad"}],
                output_path=tmp_path / "x.png",
            )

    def test_reprojects_to_raster_crs(self, tmp_path: Path) -> None:
        """Vector layers in degrees + UTM raster basemap must still compose.

        Regression: the canvas was previously sized from a mixed-unit extent,
        squeezing every vector layer into an invisible corner speck.
        """
        rasterio = pytest.importorskip("rasterio")
        import numpy as np
        from rasterio.transform import from_origin

        raster_path = tmp_path / "dem.tif"
        data = np.ones((20, 20), dtype="int16")
        with rasterio.open(
            raster_path,
            "w",
            driver="GTiff",
            height=20,
            width=20,
            count=1,
            dtype="int16",
            crs="EPSG:32647",
            transform=from_origin(500000, 4000000, 100, 100),
            nodata=-32768,
        ) as ds:
            ds.write(data, 1)

        # A tiny polygon in degrees around lon=102, lat=28 (inside UTM47N).
        from shapely.geometry import box

        vec = gpd.GeoDataFrame(
            {"n": [1]},
            geometry=[box(101.9, 27.9, 102.1, 28.1)],
            crs="EPSG:4326",
        )

        from geoharness.cartography import render_layers

        out = tmp_path / "with_raster.png"
        written = render_layers(
            [{"gdf": vec, "label": "行政区划", "color": "#4c78a8"}],
            title="CRS融合",
            output_path=out,
            basemap_raster=str(raster_path),
        )
        assert written.exists()
        assert written.stat().st_size > 1000

    def test_html_composes_layers(self, tmp_path: Path) -> None:
        pytest.importorskip("folium")
        from geoharness.cartography import render_layers

        out = tmp_path / "multi.html"
        written = render_layers(
            [
                {"gdf": _poly_gdf(), "label": "面"},
                {"gdf": _point_gdf(), "label": "点"},
            ],
            title="交互",
            output_path=out,
        )
        assert written.exists()
        html = written.read_text(encoding="utf-8")
        # Two named GeoJson layers + layer control present (labels are stored
        # as JS variables, not necessarily inline).
        assert html.count("geoJson") >= 2 or html.count("GeoJson") >= 2
        assert "layer" in html.lower() or "图层" in html or "控制" in html


class TestLayersToolParam:
    @pytest.fixture
    def context(self, tmp_path: Path) -> ToolExecutionContext:
        cfg = GeoConfig(
            cartography=CartographyConfig(outputs_dir=str(tmp_path / "out"))
        )
        return ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": cfg, "data_catalog": None},
            hook_executor=None,
        )

    @pytest.mark.asyncio
    async def test_layers_param_renders_one_map(
        self, context: ToolExecutionContext, tmp_path: Path
    ) -> None:
        import pandas as _pd  # noqa: F401

        # geojson inline layers avoid needing a catalog.
        fc1 = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {"n": 1},
                "geometry": {"type": "Polygon", "coordinates":
                             [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
            }],
        }
        fc2 = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature", "properties": {"n": 2},
                "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            }],
        }
        layers = [
            {"geojson": json.dumps(fc1), "label": "面", "color": "#4c78a8"},
            {"geojson": json.dumps(fc2), "label": "点", "color": "#e4572e"},
        ]
        res = await GeoCartographyTool().execute(
            GeoCartographyInput(
                action="export",
                output_format="png",
                title="工具层",
                layers=json.dumps(layers),
                output_path=str(tmp_path / "tool_multi.png"),
            ),
            context,
        )
        assert not res.is_error, res.output
        assert "multi-layer" in res.output.lower()
        assert res.metadata.get("layer_count") == 2
        assert Path(res.metadata["output_path"]).exists()

    @pytest.mark.asyncio
    async def test_invalid_layers_json_errors(
        self, context: ToolExecutionContext
    ) -> None:
        res = await GeoCartographyTool().execute(
            GeoCartographyInput(
                action="export", output_format="png", layers="not-json"
            ),
            context,
        )
        assert res.is_error
        assert "JSON" in res.output

    @pytest.mark.asyncio
    async def test_one_bad_layer_fails_with_message(
        self, context: ToolExecutionContext, tmp_path: Path
    ) -> None:
        layers = [
            {"source_type": "file", "source_name": "不存在.shp", "label": "x"},
        ]
        res = await GeoCartographyTool().execute(
            GeoCartographyInput(
                action="export",
                output_format="png",
                layers=json.dumps(layers),
                output_path=str(tmp_path / "bad.png"),
            ),
            context,
        )
        assert res.is_error
        assert "could not be loaded" in res.output
