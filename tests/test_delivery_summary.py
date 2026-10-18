"""Delivery facts come from the copied selected package, including native renderer counts."""

import hashlib
import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.cartography.delivery_summary import build_delivery_summary
from geoharness.cartography.provenance import apply_analysis_footer
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool


def _package(directory, *, status="verified"):
    directory.mkdir(parents=True)
    image = b"the exact selected native PNG"
    (directory / "preview.png").write_bytes(image)
    (directory / "map.qgz").write_bytes(b"saved project snapshot")
    manifest = {
        "backend": "qgis", "preview": "preview.png", "project_file": "map.qgz",
        "preview_sha256": hashlib.sha256(image).hexdigest(),
        "project_fidelity": "saved_layout",
        "files": ["project.json", "map.qgz", "preview.png"],
        "layers": [{"name": "Whole risk layer", "renderer": "categorizedSymbol",
                    "categories": [{"value": index} for index in range(30)]},
                   {"name": "Ranges", "renderer": "graduatedSymbol",
                    "ranges": [{"lower": 0, "upper": 1}, {"lower": 1, "upper": 2}]}],
        "layout": {"map_frames": [{"id": "main_map", "crs": "EPSG:4326"},
                                  {"id": "detail_map", "crs": "EPSG:3857"}],
                   "legends": {"legend": {"rows": [{"text": "actual saved legend"}],
                                          "row_count": 1, "truncated": False}}},
        "native_facts": {"encoded_groups": [{"sample_count": 2}]},
        "analysis": {"status": status, "source_notes": "完整来源说明",
                     "analyses": [{"statistics": {"affected_count": 12},
                                   "parameters": {"distance_m": 15},
                                   "output_fingerprints": {"result": {"sha256": "a" * 64}}}]},
    }
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    expected = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir()}
    delivery = {"primary_map": str(directory / "preview.png"), "quality_status": "draft",
                "project_fidelity": "saved_layout", "selection": {"selected_version": 2}}
    return manifest, expected, delivery


def test_summary_uses_actual_renderer_and_legend_in_hash_checked_selected_manifest(tmp_path):
    manifest, expected, delivery = _package(tmp_path / "selected")
    receipt = {"selected_version": 2, "artifact_registered": True,
               "analysis_correctness": "unverified",
               "stages": {"data_checked": {"status": "passed"},
                          "visual_checked": {"status": "unverified"}}}
    summary = build_delivery_summary(delivery, receipt,
        manifest_path=tmp_path / "selected/project.json", package_hashes=expected)
    assert summary["package_integrity"] == "passed"
    assert summary["primary_map_sha256"] == manifest["preview_sha256"]
    assert summary["selected_version"] == 2
    assert summary["report_status"]["data_checked"] == "passed"
    assert summary["map_frames"][0]["crs_details"]["official_name"] == "WGS 84"
    assert summary["map_frames"][1]["crs_details"]["official_name"] == "WGS 84 / Pseudo-Mercator"
    assert summary["renderer_counts"][0]["category_count"] == 30
    assert summary["renderer_counts"][1]["range_count"] == 2
    assert summary["actual_legend"] == manifest["layout"]["legends"]
    assert summary["analysis"] == manifest["analysis"]
    assert "不表示分析未执行" in summary["analysis_explanation"]


def test_secondary_page_problems_are_not_omitted_from_delivery_notice(tmp_path):
    _, expected, delivery = _package(tmp_path / 'selected')
    issue = {'severity': 'warning', 'description': '名单表头裁切', 'location': '名单页'}
    receipt = {'selected_version': 2, 'artifact_registered': True, 'versions': [
        {'version': 2, 'page_reviews': {'1': {'status': 'issues',
            'report': {'issues': [issue], 'limitations': ['未核对原始数据']}}}}]}
    summary = build_delivery_summary(delivery, receipt,
        manifest_path=tmp_path / 'selected/project.json', package_hashes=expected)
    assert summary['page_reviews']['1']['status'] == 'issues'
    assert summary['remaining_issues'][0]['description'] == '名单表头裁切'
    assert '1项' in summary['quality_notice']


@pytest.mark.parametrize("change", ["manifest", "project", "missing_hash"])
def test_unchecked_or_changed_package_does_not_supply_facts(tmp_path, change):
    _, expected, delivery = _package(tmp_path / "selected")
    if change == "manifest":
        path = tmp_path / "selected/project.json"
        data = json.loads(path.read_text())
        data["layers"][0]["categories"] = ["invented"]
        path.write_text(json.dumps(data), encoding="utf-8")
    elif change == "project":
        (tmp_path / "selected/map.qgz").write_bytes(b"changed")
    else:
        expected.pop(str(tmp_path / "selected/map.qgz"))
    summary = build_delivery_summary(delivery, {},
        manifest_path=tmp_path / "selected/project.json", package_hashes=expected)
    assert summary["package_integrity"] != "passed"
    assert summary["renderer_counts"] == []
    assert summary["analysis"] == {}


def test_historical_analysis_is_retained_without_certifying_current_export(tmp_path):
    manifest, expected, delivery = _package(tmp_path / "selected", status="historical_unverified")
    summary = build_delivery_summary(delivery, {},
        manifest_path=tmp_path / "selected/project.json", package_hashes=expected)
    assert summary["analysis"] == manifest["analysis"]
    assert summary["analysis_status"] == "historical_unverified"
    assert "不能作为当前地图的已验证统计" in summary["analysis_explanation"]


def test_long_multiline_source_notes_are_preserved_and_numbers_are_not_truncated():
    notes = ("来源：开放数据；时间：2024-01-01 至 2024-12-31\n"
             + "面积 1234567890.123456789 m²\n" * 30)
    payload = {"layout": {"footer_text": notes}}
    result = apply_analysis_footer(payload, {"status": "none", "analyses": [], "footer_text": ""})
    assert result["analysis_facts"]["source_notes"] == notes
    assert len(result["layout"]["footer_text"].splitlines()) <= 3
    assert len(result["layout"]["footer_text"]) <= 400
    assert "时间：2024-01-01 至 2024-12-31" in result["layout"]["footer_text"]
    assert "123456" not in result["layout"]["footer_text"]
    assert payload["layout"]["footer_text"] == notes


def test_free_source_notes_without_analysis_do_not_assert_a_metric_crs_verification():
    notes = "来源：用户数据；计量CRS EPSG:3857"
    result = apply_analysis_footer({"layout": {"footer_text": notes}},
                                  {"status": "none", "analyses": [], "footer_text": ""})
    assert result["analysis_facts"]["status"] == "none"
    assert result["analysis_facts"]["source_notes"] == notes


def test_short_manual_statistics_do_not_mix_with_verified_footer():
    notes = "受影响 999999，未影响 0"
    result = apply_analysis_footer({"layout": {"footer_text": notes}}, {
        "status": "verified", "analyses": [{"metric_crs": "EPSG:3857"}],
        "footer_text": "受影响 2，未影响 1\n计量CRS EPSG:3857",
    })
    assert result["analysis_facts"]["source_notes"] == notes
    assert "受影响 2，未影响 1" in result["layout"]["footer_text"]
    assert "999999" not in result["layout"]["footer_text"]


@pytest.mark.asyncio
async def test_delivery_writes_summary_from_selected_package_and_ignores_attachment_json(tmp_path):
    directory = tmp_path / "selected"
    manifest, _, _ = _package(directory)
    unrelated = tmp_path / "summary.json"
    unrelated.write_text(json.dumps({"layers": [{"categories": ["fake"]}],
                                     "analysis": {"status": "verified"}}), encoding="utf-8")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(),
                                      "latest_map": {"categories": ["also fake"]}})
    result = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(directory),
                                          files=[str(unrelated)]), context)
    assert not result.is_error, result.output
    summary = json.loads(Path(result.metadata["summary_json"]).read_text(encoding="utf-8"))
    assert summary["primary_map_sha256"] == manifest["preview_sha256"]
    assert summary["renderer_counts"][0]["category_count"] == 30
    assert summary["selected_manifest"] == str(Path(result.metadata["delivery_dir"])
                                                / "project/project.json")
    assert Path(result.metadata["summary_markdown"]).is_file()
    assert Path(result.metadata["files"][0]).name != "summary.json"


@pytest.mark.asyncio
async def test_copy_race_cannot_supply_selected_package_facts(tmp_path, monkeypatch):
    import geoharness.tools.geo_deliver as deliver_module

    directory = tmp_path / "selected"
    _package(directory)
    original_copy = deliver_module.shutil.copy2

    def changed_copy(source, target):
        original_copy(source, target)
        if Path(source).name == "map.qgz":
            Path(target).write_bytes(b"modified copied project")

    monkeypatch.setattr(deliver_module.shutil, "copy2", changed_copy)
    result = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(directory)),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}))
    assert not result.is_error, result.output
    summary = json.loads(Path(result.metadata["summary_json"]).read_text(encoding="utf-8"))
    assert result.metadata["snapshot_status"] == "changed"
    assert summary["package_integrity"] == "changed"
    assert summary["renderer_counts"] == []
    assert summary["analysis"] == {}
