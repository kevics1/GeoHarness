"""Evidence-based map preflight, without a QGIS runtime or source-file writes."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from geoharness.cartography.preflight import to_imshow_extent, validate_frames

gpd = pytest.importorskip("geopandas")
geometry = pytest.importorskip("shapely.geometry")


def _frame(x: float = 101, y: float = 28, *, crs: str | None = "EPSG:4326", **kwargs):
    return {
        "gdf": gpd.GeoDataFrame({"name": ["site"], "value": [2]},
                                geometry=[geometry.Point(x, y)], crs=crs),
        "label": "sites", **kwargs,
    }


def test_named_bounds_and_imshow_order_are_explicit() -> None:
    report = validate_frames([_frame()], map_crs="EPSG:4326", map_extent=[100, 27, 102, 29])
    assert report["named_bounds"] == {"xmin": 100, "ymin": 27, "xmax": 102, "ymax": 29}
    assert report["imshow_extent"] == [100, 102, 27, 29]
    assert to_imshow_extent(report["map_extent"]) == [100, 102, 27, 29]
    assert report["layers"][0]["visible_main_feature_count"] == 1
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("extent", [
    [100, 102, 27, 29], [100, 27, 100, 29], [100, 27, 102, 27],
    [100, 27, np.nan, 29], [100, 27, 102, np.inf],
])
def test_bad_extents_fail(extent) -> None:
    with pytest.raises(ValueError, match="map_extent"):
        validate_frames([_frame()], map_crs="EPSG:4326", map_extent=extent)


@pytest.mark.parametrize("kwargs", [
    {"map_extent": [100, 27, 102, 29]},
    {"detail_extent": {"xmin": 100, "ymin": 27, "xmax": 102, "ymax": 29}},
])
def test_explicit_extent_requires_map_crs(kwargs) -> None:
    with pytest.raises(ValueError, match="declared map_crs"):
        validate_frames([_frame()], **kwargs)


def test_source_requires_declared_crs() -> None:
    with pytest.raises(ValueError, match="no declared CRS"):
        validate_frames([_frame(crs=None)], map_crs="EPSG:4326")


def test_vector_requires_active_geometry_column() -> None:
    with pytest.raises(ValueError, match="no active geometry"):
        validate_frames([{"gdf": gpd.GeoDataFrame({"name": ["site"]}), "label": "sites"}])


@pytest.mark.parametrize("x,y", [(181, 28), (101, 91), (-181, 28), (101, -91)])
def test_geographic_source_coordinates_are_legal(x, y) -> None:
    with pytest.raises(ValueError, match="longitude/latitude limits"):
        validate_frames([_frame(x, y)])


def test_geographic_map_extent_is_legal() -> None:
    with pytest.raises(ValueError, match="longitude/latitude limits"):
        validate_frames([_frame()], map_crs="EPSG:4326", map_extent=[-181, -90, 180, 90])


def test_mixed_crs_aligns_geometry_before_counting() -> None:
    geographic = _frame()
    projected = _frame()
    projected["gdf"] = projected["gdf"].to_crs("EPSG:32647")
    projected["label"] = "projected sites"
    report = validate_frames([geographic, projected], map_crs="EPSG:4326",
                             map_extent=[100, 27, 102, 29])
    assert [layer["visible_main_feature_count"] for layer in report["layers"]] == [1, 1]
    assert report["layers"][1]["source_crs"] == "EPSG:32647"
    assert report["layers"][1]["display_bounds"] == pytest.approx([101, 28, 101, 28])
    assert projected["gdf"].crs.to_epsg() == 32647


def test_required_layer_outside_main_and_detail_fails() -> None:
    with pytest.raises(ValueError, match="Required layer.*main=0, detail=0"):
        validate_frames([_frame()], map_crs="EPSG:4326", map_extent=[0, 0, 1, 1])


def test_optional_outside_layer_remains_with_zero_count_and_warning() -> None:
    report = validate_frames([_frame(required=False, minimum_visible_features=0)],
                             map_crs="EPSG:4326", map_extent=[0, 0, 1, 1])
    assert report["status"] == "passed"
    assert report["layers"][0]["feature_count"] == 1
    assert report["layers"][0]["visible_main_feature_count"] == 0
    assert any("outside" in warning for warning in report["warnings"])


def test_required_layer_can_be_visible_in_detail_only() -> None:
    report = validate_frames([_frame()], map_crs="EPSG:4326", map_extent=[0, 0, 1, 1],
                             detail_extent={"xmin": 100, "ymin": 27, "xmax": 102, "ymax": 29})
    layer = report["layers"][0]
    assert layer["visible_main_feature_count"] == 0
    assert layer["visible_detail_feature_count"] == 1
    assert not report["warnings"]


def test_detail_extent_with_own_crs_aligns_to_display_crs() -> None:
    projected = _frame()["gdf"].to_crs("EPSG:32647")
    x, y = projected.geometry.iloc[0].coords[0]
    report = validate_frames([_frame()], map_crs="EPSG:4326", map_extent=[0, 0, 1, 1],
                             detail_extent={"xmin": x - 1000, "ymin": y - 1000,
                                            "xmax": x + 1000, "ymax": y + 1000,
                                            "crs": "EPSG:32647"})
    assert report["layers"][0]["visible_detail_feature_count"] == 1
    assert report["detail_extent"][0] < 101 < report["detail_extent"][2]


def test_visible_count_uses_geometry_not_only_layer_bounds() -> None:
    polygon = geometry.Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        holes=[[(2, 2), (8, 2), (8, 8), (2, 8)]],
    )
    frame = {"gdf": gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326"), "label": "ring"}
    with pytest.raises(ValueError, match="main=0"):
        validate_frames([frame], map_crs="EPSG:4326", map_extent=[3, 3, 7, 7])


def test_optional_empty_affected_layer_retains_true_zero() -> None:
    empty = _frame(required=False, minimum_visible_features=0, expected_feature_count=0)
    empty["gdf"] = empty["gdf"].iloc[:0]
    report = validate_frames([_frame(), empty])
    assert report["layers"][1]["feature_count"] == 0
    assert report["layers"][1]["bounds"] is None
    assert report["layers"][1]["visible_main_feature_count"] == 0
    assert any("empty" in warning for warning in report["warnings"])


def test_required_empty_layer_fails() -> None:
    empty = _frame()
    empty["gdf"] = empty["gdf"].iloc[:0]
    with pytest.raises(ValueError, match="is empty"):
        validate_frames([empty])


@pytest.mark.parametrize("geom,match", [
    (None, "null or empty"), (geometry.Point(), "null or empty"),
    (geometry.Point(np.nan, 28), "finite"),
    (geometry.Polygon([(0, 0), (1, 1), (0, 1), (1, 0)]), "invalid geometries"),
])
def test_null_empty_nonfinite_and_invalid_geometries_fail(geom, match) -> None:
    frame = _frame()
    frame["gdf"] = gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326")
    with pytest.raises(ValueError, match=match):
        validate_frames([frame])


def test_numeric_strings_are_converted_in_copy_only() -> None:
    frame = _frame(style={"column": "value", "render_method": "graduated"})
    source = frame["gdf"]
    source["value"] = ["2.5"]
    report = validate_frames([frame])
    assert frame["gdf"] is not source
    assert source["value"].iloc[0] == "2.5"
    assert frame["gdf"]["value"].iloc[0] == 2.5
    assert report["layers"][0]["converted_fields"] == ["value"]
    assert report["layers"][0]["fields"]["value"]["numeric_parseable"]


def test_numeric_strings_rejected_when_source_cannot_be_copied() -> None:
    frame = _frame(style={"field": "value", "renderer": "graduated"})
    frame["gdf"]["value"] = ["2.5"]
    with pytest.raises(ValueError, match="must have numeric dtype"):
        validate_frames([frame], coerce_numeric=False)


@pytest.mark.parametrize("value", ["high", "0.1234567890123456789012345", "inf", np.inf])
def test_graduated_nonnumeric_nonfinite_and_lossy_values_fail(value) -> None:
    frame = _frame(style={"column": "value", "render_method": "graduated"})
    frame["gdf"]["value"] = [value]
    with pytest.raises(ValueError, match="must be numeric"):
        validate_frames([frame])


@pytest.mark.parametrize("kwargs", [
    {"style": {"column": "missing", "render_method": "graduated"}},
    {"style": {"field": "missing", "renderer": "categorized"}},
    {"label_field": "missing"},
])
def test_symbolization_and_label_fields_must_exist(kwargs) -> None:
    with pytest.raises(ValueError, match="field 'missing' does not exist"):
        validate_frames([_frame(**kwargs)])


def test_feature_count_expectation_is_exact() -> None:
    with pytest.raises(ValueError, match="expected_feature_count=2, actual=1"):
        validate_frames([_frame(expected_feature_count=2)])


def test_configurable_minimum_visibility() -> None:
    with pytest.raises(ValueError, match="minimum"):
        validate_frames([_frame(minimum_visible_features=2)])


@pytest.mark.parametrize("crs,x,y,span", [
    ("EPSG:4326", 101, 28, 0.1), ("EPSG:32647", 500000, 3100000, 1200),
])
def test_single_point_auto_extent_has_configurable_positive_span(crs, x, y, span) -> None:
    report = validate_frames([_frame(x, y, crs=crs)], minimum_span_geographic=span,
                             minimum_span_projected=span, extent_padding=0)
    xmin, ymin, xmax, ymax = report["map_extent"]
    assert xmax - xmin == pytest.approx(span)
    assert ymax - ymin == pytest.approx(span)
    assert report["map_extent_source"] == "auto"
    assert report["display_crs_source"].startswith("first_vector_layer")


def test_field_report_is_bounded_and_prioritizes_used_fields() -> None:
    frame = _frame(label_field="last")
    columns = {f"field{i}": [i] for i in range(100)}
    columns["last"] = ["label"]
    frame["gdf"] = gpd.GeoDataFrame(columns, geometry=[geometry.Point(101, 28)], crs="EPSG:4326")
    report = validate_frames([frame])
    assert len(report["layers"][0]["fields"]) == 64
    assert "last" in report["layers"][0]["fields"]
    assert report["layers"][0]["fields_truncated"]


def test_categorized_data_must_be_scalar_and_declared() -> None:
    frame = _frame(style={"field": "name", "renderer": "categorized",
                          "categories": [{"value": "other", "color": "#000000"}]})
    with pytest.raises(ValueError, match="absent from categories"):
        validate_frames([frame])
    frame["style"]["categories"] = [{"value": "site", "color": "#000000"}]
    assert validate_frames([frame])["layers"][0]["renderer"] == "categorized"
    frame["gdf"]["name"] = [["site"]]
    with pytest.raises(ValueError, match="readable scalar"):
        validate_frames([frame])


def test_graduated_ranges_cannot_omit_actual_values():
    frame = _frame(114, 30)
    frame["gdf"]["value"] = [7]
    frame["style"] = {"renderer": "graduated", "field": "value",
                      "ranges": [{"lower": 0, "upper": 3}]}
    with pytest.raises(ValueError, match="unclassified"):
        validate_frames([frame])
    frame["style"]["ranges"] = [{"lower": 0, "upper": 7}]
    assert validate_frames([frame])["status"] == "passed"


def _raster(tmp_path: Path, *, crs="EPSG:32647") -> Path:
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    path = tmp_path / "terrain.tif"
    with rasterio.open(path, "w", driver="GTiff", height=2, width=2, count=1, dtype="uint8",
                       crs=crs, transform=from_origin(500000, 3100000, 100, 100)) as raster:
        raster.write(np.ones((2, 2), dtype="uint8"), 1)
    return path


def test_raster_crs_is_default_and_visibility_is_bounds_only(tmp_path: Path) -> None:
    frame = _frame(500050, 3099950, crs="EPSG:32647")
    frame["gdf"] = frame["gdf"].to_crs("EPSG:4326")
    report = validate_frames([frame, {"raster_path": _raster(tmp_path), "label": "terrain"}])
    assert report["display_crs"] == "EPSG:32647"
    assert report["display_crs_source"] == "first_raster_layer:terrain"
    layer = report["layers"][1]
    assert layer["visible_main"]
    assert layer["visible_main_feature_count"] is None
    assert layer["visibility_basis"] == "raster_bounds_only"
    assert layer["semantic_correctness"] == "unverified"


def test_required_raster_outside_fails_and_optional_raster_warns(tmp_path: Path) -> None:
    raster = {"raster_path": _raster(tmp_path), "label": "terrain"}
    with pytest.raises(ValueError, match="Required layer 'terrain'"):
        validate_frames([raster], map_crs="EPSG:32647", map_extent=[0, 0, 100, 100])
    raster["required"] = False
    report = validate_frames([raster], map_crs="EPSG:32647", map_extent=[0, 0, 100, 100])
    assert any("outside" in warning for warning in report["warnings"])


def test_raster_source_requires_crs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no declared CRS"):
        validate_frames([{"raster_path": _raster(tmp_path, crs=None), "label": "terrain"}])
