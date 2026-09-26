"""Tests for the native cartography pipeline.

All tests are hermetic: synthetic GeoJSON in ``tmp_path``, no network, no
QGIS, no PostGIS. Assertions check real artefacts (magic bytes, file size)
rather than "a string was returned".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from geoharness.cartography import render_interactive, render_map, render_static
from geoharness.cartography.sources import resolve_geodataframe

# ── Synthetic data ────────────────────────────────────────────────────────

_TILES = [
    ((0, 0, 1, 1), 10.0, "low"),
    ((1, 0, 2, 1), 20.0, "low"),
    ((0, 1, 1, 2), 30.0, "mid"),
    ((1, 1, 2, 2), 40.0, "mid"),
    ((2, 0, 3, 1), 50.0, "high"),
    ((2, 1, 3, 2), 60.0, "high"),
]


def _features() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for (minx, miny, maxx, maxy), value, cat in _TILES:
        out.append(
            {
                "type": "Feature",
                "properties": {"value": value, "cat": cat, "name": f"z{int(value)}"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [minx, miny],
                            [maxx, miny],
                            [maxx, maxy],
                            [minx, maxy],
                            [minx, miny],
                        ]
                    ],
                },
            }
        )
    return out


def _feature_collection() -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": _features()}


@pytest.fixture
def geojson_path(tmp_path: Path) -> Path:
    path = tmp_path / "zones.geojson"
    path.write_text(json.dumps(_feature_collection(), ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def gdf() -> Any:
    import geopandas as gpd

    return gpd.GeoDataFrame.from_features(_features(), crs="EPSG:4326")


# ── Source resolution ─────────────────────────────────────────────────────


class TestResolveGeoDataFrame:
    def test_from_file_path(self, geojson_path: Path) -> None:
        result = resolve_geodataframe(file_path=str(geojson_path))
        assert len(result) == 6
        assert result.crs is not None

    def test_from_inline_geojson(self) -> None:
        result = resolve_geodataframe(
            geojson_text=json.dumps(_feature_collection())
        )
        assert len(result) == 6

    def test_inline_geojson_takes_precedence(self, geojson_path: Path) -> None:
        result = resolve_geodataframe(
            file_path=str(geojson_path),
            geojson_text=json.dumps(_feature_collection()),
        )
        assert len(result) == 6

    def test_missing_source_raises(self) -> None:
        with pytest.raises(ValueError, match="No data source"):
            resolve_geodataframe()

    def test_unknown_source_type_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown source_type"):
            resolve_geodataframe(source_type="oracle", source_name="x")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not found"):
            resolve_geodataframe(file_path=str(tmp_path / "nope.geojson"))

    def test_invalid_geojson_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_geodataframe(geojson_text="{not geojson")

    def test_connector_resolution(self, geojson_path: Path) -> None:
        """Bare file names resolve through the file connector."""

        class FakeFileConnector:
            def resolve_path(self, name: str) -> Path:
                assert name == "zones"
                return geojson_path

        class FakeCatalog:
            def get_connector(self, name: str) -> Any:
                return FakeFileConnector() if name == "file" else None

        result = resolve_geodataframe(
            source_type="file", source_name="zones", catalog=FakeCatalog()
        )
        assert len(result) == 6

    def test_postgis_without_connector_raises(self) -> None:
        with pytest.raises(ValueError, match="PostGIS is not configured"):
            resolve_geodataframe(
                source_type="postgis", source_name="t", catalog=None
            )

    def test_postgis_uses_connector(self, gdf: Any) -> None:
        class FakePostGIS:
            def query_geodataframe(self, table: str) -> Any:
                assert table == "wuhan"
                return gdf

        class FakeCatalog:
            def get_connector(self, name: str) -> Any:
                return FakePostGIS() if name == "postgis" else None

        result = resolve_geodataframe(
            source_type="postgis", source_name="wuhan", catalog=FakeCatalog()
        )
        assert len(result) == 6

    def test_admin_kg_without_connector_raises(self) -> None:
        with pytest.raises(ValueError, match="connector unavailable"):
            resolve_geodataframe(
                source_type="admin_kg", source_name="湖北省", catalog=None
            )

    def test_admin_kg_uses_connector(self, gdf: Any) -> None:
        class FakeAdmin:
            def load_geodataframe(self, region: str) -> Any:
                assert region == "湖北省"
                return gdf

        class FakeCatalog:
            def get_connector(self, name: str) -> Any:
                return FakeAdmin() if name == "admin_kg" else None

        result = resolve_geodataframe(
            source_type="admin_kg", source_name="湖北省", catalog=FakeCatalog()
        )
        assert len(result) == 6


# ── Static rendering ──────────────────────────────────────────────────────


class TestRenderStatic:
    def test_png_magic_bytes(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf,
            column="value",
            render_method="graduated",
            color_scheme="YlOrRd",
            n_classes=3,
            output_path=tmp_path / "m.png",
        )
        assert out.exists()
        assert out.read_bytes()[:4] == b"\x89PNG"
        assert out.stat().st_size > 2000

    def test_pdf_magic_bytes(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="cat", render_method="categorized",
            color_scheme="Set1", output_path=tmp_path / "m.pdf",
        )
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_svg_is_xml(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="value", render_method="graduated",
            output_path=tmp_path / "m.svg",
        )
        head = out.read_text(encoding="utf-8")[:60]
        assert "<?xml" in head and "<svg" in out.read_text(encoding="utf-8")

    def test_suffix_added_when_missing(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(gdf, column="value", output_path=tmp_path / "bare")
        assert out.suffix == ".png"

    def test_creates_parent_directories(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="value", output_path=tmp_path / "deep" / "nested" / "m.png"
        )
        assert out.exists()

    def test_chinese_title_renders(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="value", title="武汉市空间自相关分析",
            output_path=tmp_path / "cjk.png",
        )
        assert out.stat().st_size > 2000

    def test_flow_render(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="value", render_method="flow", color_scheme="Blues",
            output_path=tmp_path / "flow.png",
        )
        assert out.read_bytes()[:4] == b"\x89PNG"

    def test_none_method(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, render_method="none", output_path=tmp_path / "plain.png"
        )
        assert out.exists()

    def test_unknown_scheme_falls_back(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="value", color_scheme="NotAScheme",
            output_path=tmp_path / "fallback.png",
        )
        assert out.exists()

    def test_non_numeric_graduated_falls_back(self, gdf: Any, tmp_path: Path) -> None:
        out = render_static(
            gdf, column="cat", render_method="graduated",
            output_path=tmp_path / "fallback2.png",
        )
        assert out.exists()

    def test_empty_gdf_raises(self, tmp_path: Path) -> None:
        import geopandas as gpd

        empty = gpd.GeoDataFrame({"value": []}, geometry=[], crs="EPSG:4326")
        with pytest.raises(ValueError, match="no features"):
            render_static(empty, column="value", output_path=tmp_path / "x.png")

    def test_null_geometries_raise(self, tmp_path: Path) -> None:
        import geopandas as gpd

        nulls = gpd.GeoDataFrame(
            {"value": [1.0, 2.0]}, geometry=[None, None], crs="EPSG:4326"
        )
        with pytest.raises(ValueError, match="null"):
            render_static(nulls, column="value", output_path=tmp_path / "x.png")

    def test_missing_output_path_raises(self, gdf: Any) -> None:
        with pytest.raises(ValueError, match="output_path"):
            render_static(gdf, column="value", output_path="")


# ── Interactive rendering ─────────────────────────────────────────────────


class TestRenderInteractive:
    def test_html_contains_leaflet(self, gdf: Any, tmp_path: Path) -> None:
        out = render_interactive(
            gdf, column="value", render_method="graduated",
            output_path=tmp_path / "m.html",
        )
        text = out.read_text(encoding="utf-8")
        assert out.suffix == ".html"
        assert "leaflet" in text.lower()
        assert len(text) > 2000

    def test_reprojects_to_wgs84(self, gdf: Any, tmp_path: Path) -> None:
        """EPSG:3857 input must be reprojected before leaflet plotting."""
        web = gdf.to_crs("EPSG:3857")
        out = render_interactive(web, column="value", output_path=tmp_path / "r.html")
        assert out.exists()

    def test_categorized_html(self, gdf: Any, tmp_path: Path) -> None:
        out = render_interactive(
            gdf, column="cat", render_method="categorized",
            color_scheme="Set2", title="分类图", output_path=tmp_path / "c.html",
        )
        text = out.read_text(encoding="utf-8")
        assert "分类图" in text

    def test_tooltip_fields_filtered(self, gdf: Any, tmp_path: Path) -> None:
        out = render_interactive(
            gdf, column="value",
            tooltip_fields=["value", "nonexistent_column"],
            output_path=tmp_path / "t.html",
        )
        assert out.exists()


# ── Dispatcher ────────────────────────────────────────────────────────────


class TestRenderMap:
    def test_html_suffix_dispatches_interactive(self, gdf: Any, tmp_path: Path) -> None:
        out = render_map(
            gdf, column="value", output_path=tmp_path / "d.html"
        )
        assert out.suffix == ".html"
        assert "leaflet" in out.read_text(encoding="utf-8").lower()

    def test_png_suffix_dispatches_static(self, gdf: Any, tmp_path: Path) -> None:
        out = render_map(gdf, column="value", output_path=tmp_path / "d.png")
        assert out.read_bytes()[:4] == b"\x89PNG"

    def test_pdf_without_suffix_defaults(self, gdf: Any, tmp_path: Path) -> None:
        out = render_map(gdf, column="value", output_path=tmp_path / "d")
        assert out.suffix == ".png"


# ── Headless safety ───────────────────────────────────────────────────────


def test_agg_backend_forced() -> None:
    """Importing the package must force the headless Agg backend."""
    import matplotlib

    from geoharness.cartography import renderer  # noqa: F401

    assert matplotlib.get_backend().lower() == "agg"
