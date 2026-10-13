"""Cross-version retained content uses layer identities, never drawing positions."""

import hashlib

import pytest

from geoharness.cartography.jobs import (
    ensure_map_job,
    map_job_receipt,
    record_map_version,
)
from geoharness.config.settings import GeoConfig


def _candidate(tmp_path, reports):
    config, metadata = GeoConfig(), {}
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config)
    for number, report in enumerate(reports, 1):
        image = tmp_path / f"v{number}.png"
        image.write_bytes(f"version {number}".encode())
        record_map_version(
            job_id, metadata, cwd=tmp_path, config=config, image_path=image,
            preflight={
                "status": "passed", "scope": "map_data_preflight",
                "display_crs": "EPSG:4326", "map_extent": [0, 0, 1, 1],
                "layers": [{"feature_count": 1}], "checks": {"valid_geometry": "passed"},
                **report,
            },
            layout={"scope": "layout_geometry", "status": "passed",
                    "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                    **{key: [] for key in (
                        "panel_overlaps", "label_overlaps", "clipped_labels", "overflow_text",
                        "figure_text_overlaps", "clipped_figure_text", "text_panel_overlaps",
                        "panels_outside_canvas",
                    )}},
            project_dir=None,
        )
    return map_job_receipt(metadata, cwd=tmp_path, config=config,
                           map_id=job_id)["delivery_candidate"]


def _layer(index, key, count, *, required=True):
    return {"source_index": index, "layer_key": key, "required": required,
            "visible_feature_count": count}


def _checks(*layers, **metadata):
    return {"frame_checks": {"layers": list(layers)}, **metadata}


def test_reordering_preserves_full_retained_content(tmp_path):
    candidate = _candidate(tmp_path, [
        _checks(_layer(0, "annotations", 22), _layer(1, "event", 1)),
        _checks(_layer(0, "event", 1), _layer(1, "annotations", 22)),
    ])
    assert candidate["version"] == 2
    assert candidate["retained_coverage"] == 1
    assert candidate["coverage_status"] == "verified"


def test_deleted_required_annotations_lower_candidate_coverage(tmp_path):
    candidate = _candidate(tmp_path, [
        _checks(_layer(0, "annotations", 22), _layer(1, "event", 1)),
        _checks(_layer(0, "event", 1)),
    ])
    assert candidate["version"] == 1
    assert candidate["retained_coverage"] == 1


def test_replaced_same_name_layer_is_not_the_same_source(tmp_path):
    candidate = _candidate(tmp_path, [
        _checks(_layer(0, "same-label-source-A", 22)),
        _checks(_layer(0, "same-label-source-B", 22)),
    ])
    # Each version has lost one known required identity, despite identical names/counts.
    assert candidate["retained_coverage"] == 0


@pytest.mark.parametrize("identity", [{}, {"label": "annotations"}])
def test_legacy_position_or_name_alone_has_unknown_coverage(tmp_path, identity):
    layer = {"source_index": 0, "required": True, "visible_feature_count": 22, **identity}
    candidate = _candidate(tmp_path, [_checks(layer), _checks(layer)])
    assert candidate["retained_coverage"] is None
    assert candidate["coverage_status"] == "unknown"
    assert candidate["coverage_scope"] == "unknown"


def test_duplicate_keys_do_not_silently_merge_layers(tmp_path):
    candidate = _candidate(tmp_path, [
        _checks(_layer(0, "ambiguous", 22), _layer(1, "ambiguous", 1)),
    ])
    assert candidate["retained_coverage"] is None
    assert candidate["coverage_status"] == "unknown"


def test_changing_required_flag_does_not_erase_content_requirement(tmp_path):
    candidate = _candidate(tmp_path, [
        _checks(_layer(0, "annotations", 22)),
        _checks(_layer(0, "annotations", 22, required=False)),
    ])
    assert candidate["version"] == 2
    assert candidate["retained_coverage"] == 1


def test_legacy_source_bindings_compare_original_identity_after_reordering(tmp_path):
    def report(names):
        return {"frame_checks": {"layers": [
            {"source_index": i, "required": True, "visible_feature_count": 1}
            for i in range(len(names))]},
            "layers": [{"label": name, "kind": "vector"} for name in names],
            "source_bindings": [{"label": name, "file_path": str(tmp_path / (name + ".geojson"))}
                                for name in names]}
    candidate = _candidate(tmp_path, [report(["event", "sites"]), report(["sites", "event"])])
    assert candidate["version"] == 2
    assert candidate["retained_coverage"] == 1
    assert candidate["coverage_status"] == "verified"
