"""Producer state and version checks cover both refs and explicit file paths."""

import json

import pytest

from geoharness.config.settings import GeoConfig
from geoharness.data.artifacts import assert_usable_artifact, lookup_reference, sha256_file
from geoharness.data.task_context import data_references, recent_outputs


def _job(tmp_path, name="job", **overrides):
    job = tmp_path / ".geoharness/runs" / name
    job.mkdir(parents=True)
    output = job / "result.csv"
    output.write_text("feature_id,value\na,1\n", encoding="utf-8")
    document = {"run_dir": str(job), "exit_code": 0, "files": [{
        "path": str(output), "schema": {"feature_id": "string", "value": "int64"},
        "state": "succeeded", "validation_state": "passed", "crs": "EPSG:32647",
        "sha256": sha256_file(output)}], **overrides}
    manifest = job / "manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return output, manifest


@pytest.mark.parametrize("state", [
    {"exit_code": 1}, {"exit_code": None}, {"status": "failed"}, {"status": "partial"},
    {"timed_out": True}, {"status": "running"},
])
def test_failed_partial_producer_is_rejected_even_by_explicit_path(tmp_path, state):
    output, _ = _job(tmp_path, **state)
    assert recent_outputs(tmp_path, GeoConfig()) == []
    with pytest.raises(ValueError, match="producer did not succeed"):
        assert_usable_artifact(output)


def test_output_refs_expose_producer_version_contract_without_certifying_science(tmp_path):
    output, _ = _job(tmp_path)
    records = recent_outputs(tmp_path, GeoConfig())
    record = next(iter(data_references({}, records).values()))
    assert record["producer"] == "geo_python"
    assert record["state"] == "succeeded" and record["validation_state"] == "passed"
    assert record["sha256"] == record["version"] == sha256_file(output)
    assert record["artifact_id"].startswith("artifact:")
    assert record["schema"]["feature_id"] == "string" and record["crs"] == "EPSG:32647"
    assert "analysis_manifest" not in record
    assert assert_usable_artifact(output)["state"] == "succeeded"
    output.write_text("changed", encoding="utf-8")
    assert recent_outputs(tmp_path, GeoConfig()) == []
    with pytest.raises(ValueError, match="fingerprint changed"):
        assert_usable_artifact(output)


def test_legacy_success_accepted_but_partial_file_not_published(tmp_path):
    output, manifest = _job(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["files"] = [{"path": str(output)}]
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert assert_usable_artifact(output)["producer"] == "geo_python"
    another = output.parent / "unpublished.csv"
    another.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="not a published output"):
        assert_usable_artifact(another)
    document["files"][0]["state"] = "failed"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert recent_outputs(tmp_path, GeoConfig()) == []
    with pytest.raises(ValueError, match="not usable"):
        assert_usable_artifact(output)


def test_unknown_sources_are_allowed_and_ambiguous_names_offer_exact_candidates(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("x", encoding="utf-8")
    assert assert_usable_artifact(source) is None
    _job(tmp_path, "one")
    _job(tmp_path, "two")
    refs = data_references({}, recent_outputs(tmp_path, GeoConfig()))
    with pytest.raises(ValueError, match="Ambiguous.*Candidates"):
        lookup_reference("result.csv", refs)
    record = next(iter(refs.values()))
    assert lookup_reference(record["ref"], refs) is record
    assert lookup_reference(record["path"], refs) is record


def test_accessibility_output_is_registered_with_method_producer(tmp_path):
    job = tmp_path / ".geoharness/analysis/accessibility"
    job.mkdir(parents=True)
    output = job / "result.geojson"
    output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    (job / "manifest.json").write_text(json.dumps({
        "status": "succeeded", "producer": "geo_accessibility", "action": "accessibility",
        "export_crs": "EPSG:4326", "paths": {"result": {
            "geojson": str(output), "geojson_sha256": sha256_file(output),
            "state": "succeeded", "validation_state": "passed"}},
    }), encoding="utf-8")
    record = next(iter(data_references({}, recent_outputs(tmp_path, GeoConfig())).values()))
    assert record["producer"] == "geo_accessibility" and record["crs"] == "EPSG:4326"
    assert record["analysis_manifest"] == str(job / "manifest.json")
    assert assert_usable_artifact(output)["producer"] == "geo_accessibility"


@pytest.mark.parametrize("producer,action", [("geo_analysis", "area"),
                                           ("geo_accessibility", "accessibility")])
def test_native_success_manifest_can_be_delivered_as_a_record_only(tmp_path, producer, action):
    path = tmp_path / "manifest.json"
    document = {"status": "succeeded", "producer": producer, "action": action, "paths": {}}
    path.write_text(json.dumps(document), encoding="utf-8")
    record = assert_usable_artifact(path)
    assert record["role"] == "reproducibility_record"
    assert record["validation_state"] == "unverified"
    unrelated = tmp_path / "not-published.json"
    unrelated.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a published output"):
        assert_usable_artifact(unrelated)
    document["status"] = "failed"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="did not succeed"):
        assert_usable_artifact(path)
