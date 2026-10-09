"""Manifest evidence is tied to actual output files and mapped sources."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from geoharness.analysis.vector import analyze_vectors
from geoharness.cartography.facts import load_analysis_facts
from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource


def _source(geometry, properties=None):
    frame = gpd.GeoDataFrame(properties or {}, geometry=geometry, crs="EPSG:32650")
    return SpatialSource(geojson=frame.to_json(), crs="EPSG:32650")


def _analysis(tmp_path, action="impact", *, zones=False):
    arguments = {
        "action": action,
        "source": _source([Point(500000, 3400000), Point(500040, 3400000)], {"asset": ["a", "b"]}),
    }
    if action in {"buffer", "impact"}:
        arguments["distance_m"] = 15
    if action in {"clip", "intersection", "distance", "impact"}:
        arguments["reference"] = _source([box(499990, 3399990, 500010, 3400010)])
    if action == "count" or zones:
        arguments["zones"] = _source([box(499990, 3399990, 500050, 3400010)])
    return analyze_vectors(
        GeoAnalysisInput(**arguments),
        cwd=tmp_path,
        outputs_dir="analysis",
        metric_crs="EPSG:32650",
    )


def _write_manifest(manifest):
    path = Path(manifest["manifest_path"])
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def _facts(tmp_path, manifest, source=None, **kwargs):
    return load_analysis_facts(
        [manifest["manifest_path"]],
        cwd=tmp_path,
        read_roots=[tmp_path],
        mapped_sources=[{"file_path": source or manifest["paths"]["result"]["geojson"]}],
        display_crs=kwargs.pop("display_crs", "EPSG:3857"),
        **kwargs,
    )


def _legacy(manifest):
    for output in manifest["paths"].values():
        output.pop("geojson_sha256")
        output.pop("csv_sha256")
    _write_manifest(manifest)


@pytest.mark.parametrize("tamper", ["union", "group", "per_feature"])
def test_area_facts_recompute_group_and_union_coverage_from_bound_output(tmp_path, tamper):
    manifest = analyze_vectors(GeoAnalysisInput(action="area", group_field="time",
        source=_source([box(500000,3400000,500100,3400100),
                        box(500000,3400000,500200,3400200)], {"time": ["t1","t2"]})),
        cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650")
    facts = _facts(tmp_path, manifest)
    stats = facts["analyses"][0]["statistics"]["area_statistics"]
    assert stats["union_area_m2"] == pytest.approx(40000)
    assert stats["feature_area_sum_m2"] == pytest.approx(50000)
    assert "去重覆盖" in facts["footer_text"] and "面积之和" in facts["footer_text"]
    if tamper == "union":
        manifest["statistics"]["area_statistics"]["union_area_m2"] = 50000
    elif tamper == "group":
        manifest["statistics"]["area_statistics"]["groups"][0]["union_area_m2"] = 15000
    else:
        entry = manifest["paths"]["result"]
        path = Path(entry["geojson"])
        document = json.loads(path.read_text(encoding="utf-8"))
        document["features"][0]["properties"]["area_m2"] = 15000
        path.write_text(json.dumps(document), encoding="utf-8")
        entry["geojson_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="area statistics"):
        _facts(tmp_path, manifest)


@pytest.mark.parametrize("crs", [[1], {"properties": []}, {"properties": {"name": 1}}])
def test_invalid_nested_geojson_crs_is_a_bounded_validation_error(tmp_path, crs):
    manifest = _analysis(tmp_path)
    entry = manifest["paths"]["reference"]
    path = Path(entry["geojson"])
    document = json.loads(path.read_text(encoding="utf-8"))
    document["crs"] = crs
    path.write_text(json.dumps(document), encoding="utf-8")
    entry["geojson_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="GeoJSON CRS"):
        _facts(tmp_path, manifest)


def test_cached_mapped_features_must_match_manifest_outputs(tmp_path):
    manifest = _analysis(tmp_path)
    path = manifest["paths"]["result"]["geojson"]
    cached = gpd.read_file(path)
    cached.loc[0, "geometry"] = Point(0, 0)
    with pytest.raises(ValueError, match="changed after it was loaded"):
        load_analysis_facts(
            [manifest["manifest_path"]],
            cwd=tmp_path,
            read_roots=[tmp_path],
            mapped_sources=[{"file_path": path, "gdf": cached}],
            display_crs="EPSG:3857",
        )


@pytest.mark.parametrize("business_id", [False, True])
def test_driver_datetime_and_feature_id_roundtrip_preserves_evidence(tmp_path, business_id):
    manifest = _analysis(tmp_path, "distance")
    entry = manifest["paths"]["result"]
    path = Path(entry["geojson"])
    document = json.loads(path.read_text(encoding="utf-8"))
    for index, feature in enumerate(document["features"]):
        feature["properties"]["time"] = "2020/03/30 19:00:00.000" if index == 0 else None
        if business_id:
            feature["properties"]["id"] = "business-" + str(index)
        else:
            feature["properties"].pop("id", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    entry["geojson_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_manifest(manifest)
    cached = gpd.read_file(path)
    assert str(cached["time"].dtype).startswith("datetime64")
    facts = load_analysis_facts(
        [manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
        mapped_sources=[{"file_path": str(path), "gdf": cached}], display_crs="EPSG:3857",
    )
    assert facts["status"] == "verified"
    assert entry["geojson_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    cached.loc[0, "id"] = "changed-id"
    with pytest.raises(ValueError, match="changed after it was loaded"):
        load_analysis_facts(
            [manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
            mapped_sources=[{"file_path": str(path), "gdf": cached}], display_crs="EPSG:3857",
        )


def test_numeric_feature_fid_does_not_authorize_a_new_id_attribute(tmp_path):
    manifest = _analysis(tmp_path, "distance")
    entry = manifest["paths"]["result"]
    path = Path(entry["geojson"])
    document = json.loads(path.read_text(encoding="utf-8"))
    for index, feature in enumerate(document["features"]):
        feature["id"] = index
        feature["properties"].pop("id", None)
    path.write_text(json.dumps(document), encoding="utf-8")
    entry["geojson_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write_manifest(manifest)
    cached = gpd.read_file(path)
    assert "id" not in cached.columns  # Numeric IDs became driver FIDs, not attributes.
    cached["id"] = list(range(len(cached)))
    with pytest.raises(ValueError, match="changed after it was loaded"):
        load_analysis_facts(
            [manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
            mapped_sources=[{"file_path": str(path), "gdf": cached}], display_crs="EPSG:3857",
        )


def test_impact_facts_use_real_counts_distinct_crs_and_output_fingerprints(tmp_path):
    manifest = _analysis(tmp_path, zones=True)
    facts = _facts(tmp_path, manifest)
    assert facts["status"] == "verified"
    analysis = facts["analyses"][0]
    assert analysis["statistics"] == {
        "source_count": 2,
        "feature_count": 2,
        "affected_count": 1,
        "unaffected_count": 1,
    }
    assert analysis["verification"] == "sha256_verified"
    assert analysis["metric_crs"] == "EPSG:32650"
    assert analysis["export_crs"] == "EPSG:4326"
    assert analysis["display_crs"] == "EPSG:3857"
    assert analysis["parameters"] == {"distance_m": 15}
    assert analysis["mapped_outputs"] == [{"output": "result", "layer_index": 0, "match": "path"}]
    for label, output in manifest["paths"].items():
        for kind in ("geojson", "csv"):
            digest = hashlib.sha256(Path(output[kind]).read_bytes()).hexdigest()
            assert output[kind + "_sha256"] == digest
            assert analysis["output_fingerprints"][label][kind + "_sha256"] == digest
    assert "受影响 1，未影响 1" in facts["footer_text"]
    assert "距离阈值 ≤ 15 m" in facts["footer_text"]
    assert "计量CRS EPSG:32650；显示CRS EPSG:3857" in facts["footer_text"]
    assert "不可相加" in facts["footer_text"]
    assert str(tmp_path) not in facts["footer_text"]


def test_no_manifest_returns_no_claims(tmp_path):
    assert load_analysis_facts(
        [],
        cwd=tmp_path,
        read_roots=[],
        mapped_sources=[],
        display_crs="",
    ) == {"status": "none", "analyses": [], "footer_text": ""}


@pytest.mark.parametrize(
    "key", ["feature_count", "source_count", "affected_count", "unaffected_count"]
)
def test_false_manifest_counts_are_rejected(tmp_path, key):
    manifest = _analysis(tmp_path)
    manifest["statistics"][key] += 1
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="count"):
        _facts(tmp_path, manifest)


@pytest.mark.parametrize(
    "output,kind",
    [
        ("result", "geojson"),
        ("affected", "geojson"),
        ("reference", "geojson"),
        ("result", "csv"),
        ("affected", "csv"),
    ],
)
def test_changed_output_with_same_feature_count_is_rejected(tmp_path, output, kind):
    manifest = _analysis(tmp_path)
    path = Path(manifest["paths"][output][kind])
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA256"):
        _facts(tmp_path, manifest)


def test_partially_hashed_manifest_cannot_downgrade_verification(tmp_path):
    manifest = _analysis(tmp_path)
    manifest["paths"]["affected"].pop("geojson_sha256")
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="incomplete fingerprints"):
        _facts(tmp_path, manifest)


def test_legacy_manifest_verifies_counts_without_claiming_recomputation(tmp_path):
    manifest = _analysis(tmp_path)
    _legacy(manifest)
    facts = _facts(tmp_path, manifest)
    analysis = facts["analyses"][0]
    assert analysis["verification"] == "legacy_count_verified"
    assert "no recomputation" in analysis["verification_scope"]
    assert "旧清单仅核对输出计数" in facts["footer_text"]
    manifest["statistics"]["affected_count"] = 2
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="affected/unaffected"):
        _facts(tmp_path, manifest)


@pytest.mark.parametrize("flags", [[1, 0], [True, True], ["true", "false"]])
def test_legacy_affected_flags_must_be_strict_booleans_matching_threshold(tmp_path, flags):
    manifest = _analysis(tmp_path)
    _legacy(manifest)
    path = Path(manifest["paths"]["result"]["geojson"])
    document = json.loads(path.read_text())
    for feature, flag in zip(document["features"], flags):
        feature["properties"]["affected"] = flag
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="booleans|threshold"):
        _facts(tmp_path, manifest)


def test_legacy_affected_subset_is_verified(tmp_path):
    manifest = _analysis(tmp_path)
    _legacy(manifest)
    path = Path(manifest["paths"]["affected"]["geojson"])
    document = json.loads(path.read_text())
    document["features"][0]["properties"]["asset"] = "different"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="subset"):
        _facts(tmp_path, manifest)


@pytest.mark.parametrize("distance", [-1, float("inf"), float("nan"), True, "15"])
def test_invalid_distance_threshold_is_rejected(tmp_path, distance):
    manifest = _analysis(tmp_path)
    manifest["parameters"]["distance_m"] = distance
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="distance"):
        _facts(tmp_path, manifest)


@pytest.mark.parametrize("metric_crs", ["EPSG:4326", "EPSG:2263", "invalid-secret-value"])
def test_metric_crs_requires_finite_metre_projection_and_no_raw_error_echo(tmp_path, metric_crs):
    manifest = _analysis(tmp_path)
    manifest["metric_crs"] = metric_crs
    _write_manifest(manifest)
    with pytest.raises(ValueError) as raised:
        _facts(tmp_path, manifest)
    assert metric_crs not in str(raised.value)


def test_only_known_native_manifests_are_accepted(tmp_path):
    manifest = _analysis(tmp_path)
    manifest["action"] = "processing.run"
    _write_manifest(manifest)
    with pytest.raises(ValueError, match="known geo_analysis"):
        _facts(tmp_path, manifest)


def test_unrelated_mapped_layer_is_rejected(tmp_path):
    manifest = _analysis(tmp_path)
    other = tmp_path / "other.geojson"
    other.write_text(_source([Point(500100, 3400100)]).geojson, encoding="utf-8")
    with pytest.raises(ValueError, match="actual mapped layer"):
        _facts(tmp_path, manifest, source=str(other))


def test_identical_copied_layer_matches_content(tmp_path):
    manifest = _analysis(tmp_path)
    copied = tmp_path / "copy.geojson"
    shutil.copyfile(manifest["paths"]["result"]["geojson"], copied)
    report = _facts(tmp_path, manifest, source=str(copied))
    assert report["analyses"][0]["mapped_outputs"][0]["match"] == "content"


@pytest.mark.parametrize("value", ["https://example.invalid/m.json", ".env", "credentials.json"])
def test_remote_and_credential_paths_are_rejected_without_echo(tmp_path, value):
    (tmp_path / ".env").write_text("protected", encoding="utf-8")
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        load_analysis_facts(
            [value],
            cwd=tmp_path,
            read_roots=[],
            mapped_sources=[],
            display_crs="EPSG:4326",
        )
    assert value not in str(raised.value)


def test_outside_roots_cannot_be_read_even_if_manifest_is_valid(tmp_path):
    other_root = tmp_path / "outside"
    other_root.mkdir()
    manifest = _analysis(other_root)
    isolated = tmp_path / "workspace"
    isolated.mkdir()
    with pytest.raises(ValueError, match="outside read roots"):
        _facts(isolated, manifest)


def test_free_text_labels_parameters_sources_and_evidence_never_become_facts(tmp_path):
    manifest = _analysis(tmp_path)
    marker = "PASSWORD=must-not-be-exported"
    manifest["parameters"]["output_name"] = marker
    manifest["statistics"]["LLM_prediction"] = 999
    manifest["count_semantics"] = marker
    manifest["geometry_semantics"] = marker
    manifest["sources"] = {"source": {"source_name": marker}}
    manifest["evidence"] = marker
    _write_manifest(manifest)
    report = load_analysis_facts(
        [manifest["manifest_path"]],
        cwd=tmp_path,
        read_roots=[],
        display_crs="EPSG:4326",
        mapped_sources=[{"file_path": manifest["paths"]["result"]["geojson"], "label": marker}],
    )
    assert marker not in json.dumps(report)
    assert "LLM_prediction" not in json.dumps(report)
    assert "999" not in report["footer_text"]


@pytest.mark.parametrize("action", ["buffer", "count", "clip", "intersection", "distance"])
def test_all_native_actions_have_output_verified_counts(tmp_path, action):
    manifest = _analysis(tmp_path, action)
    report = _facts(tmp_path, manifest)
    analysis = report["analyses"][0]
    assert analysis["statistics"]["feature_count"] == manifest["statistics"]["feature_count"]
    if action in {"clip", "intersection"}:
        assert "source_count" not in analysis["statistics"]
        assert analysis["unverified_statistics"] == ["source_count"]
    else:
        assert analysis["statistics"]["source_count"] == 2


def test_manifest_size_and_count_are_bounded(tmp_path, monkeypatch):
    import geoharness.cartography.facts as facts_module

    manifest = _analysis(tmp_path)
    with pytest.raises(ValueError, match="eight"):
        load_analysis_facts(
            [manifest["manifest_path"]] * 9,
            cwd=tmp_path,
            read_roots=[],
            mapped_sources=[],
            display_crs="EPSG:4326",
        )
    monkeypatch.setattr(facts_module, "_MANIFEST_BYTES", 32)
    with pytest.raises(ValueError, match="bounded file size"):
        _facts(tmp_path, manifest)


def test_multiple_analysis_footer_is_bounded_and_summary_is_complete(tmp_path):
    manifests = [_analysis(tmp_path, zones=True) for _ in range(8)]
    report = load_analysis_facts(
        [item["manifest_path"] for item in manifests],
        cwd=tmp_path,
        read_roots=[],
        mapped_sources=[{"file_path": item["paths"]["result"]["geojson"]} for item in manifests],
        display_crs="EPSG:3857",
    )
    assert len(report["footer_text"]) <= 600
    assert len(report["analyses"]) == 8
    assert report["footer_text"].count("受影响 1，未影响 1") == 8
