from pathlib import Path

from geoharness.cartography.review_findings import (
    observe_findings,
    pending_findings,
    pending_page_targets,
    sync_review_pages,
)
from geoharness.config.settings import GeoConfig
from geoharness.task_state import begin_task


def context(tmp_path):
    metadata = {"geoharness_config": GeoConfig()}
    begin_task(metadata, cwd=tmp_path, goal="稳定页面问题验证")
    return metadata


def test_generic_reading_effect_does_not_merge_disjoint_entity_problems():
    from geoharness.cartography.review_progress import _same_concern

    a = {
        "issue_code": "label_overlap",
        "frame_id": "detail",
        "roi_ref": "detail",
        "affected_content": "编号归属",
        "bbox": [0.1, 0.1, 0.2, 0.2],
    }
    b = {**a, "bbox": [0.7, 0.7, 0.8, 0.8]}
    assert not _same_concern(a, b)


def test_reordering_and_retiring_page_does_not_transfer_its_findings(tmp_path):
    m = context(tmp_path)
    issue = {
        "status": "issues",
        "issues": [
            {
                "severity": "error",
                "issue_code": "label_overlap",
                "description": "重点图编号相压",
                "location": "北部",
            }
        ],
    }
    observe_findings(
        m,
        cwd=tmp_path,
        map_id="map",
        page_id="detail",
        page_index=2,
        image_sha256="old",
        review=issue,
    )
    sync_review_pages(
        m,
        cwd=tmp_path,
        map_id="map",
        version=2,
        pages=[{"page_id": "detail", "page_index": 0}, {"page_id": "roster", "page_index": 2}],
    )
    assert not pending_page_targets(m, cwd=tmp_path, map_id="map", page_id="roster", page_index=2)
    assert (
        len(pending_page_targets(m, cwd=tmp_path, map_id="map", page_id="detail", page_index=0))
        == 1
    )
    sync_review_pages(
        m, cwd=tmp_path, map_id="map", version=3, pages=[{"page_id": "roster", "page_index": 0}]
    )
    assert not pending_findings(m, cwd=tmp_path, map_ids={"map"})
    old_observation = dict(issue, diagnostics={"review_binding": {"version": 2}})
    response = observe_findings(
        m,
        cwd=tmp_path,
        map_id="map",
        page_id="detail",
        page_index=2,
        image_sha256="historical",
        review=old_observation,
    )
    assert response["state"] == "historical_observation"
    assert not pending_findings(m, cwd=tmp_path, map_ids={"map"})
    from geoharness.task_store import connect, task_directory

    with connect(task_directory(m, cwd=Path(tmp_path))) as db:
        import json

        saved = json.loads(
            db.execute("SELECT payload FROM state WHERE key='review_findings'").fetchone()[
                "payload"
            ]
        )
    assert list(saved.values())[0]["retirement"]["version"] == 3
    sync_review_pages(
        m, cwd=tmp_path, map_id="map", version=2, pages=[{"page_id": "detail", "page_index": 0}]
    )
    assert not pending_findings(m, cwd=tmp_path, map_ids={"map"})


def test_legacy_index_cannot_be_guessed_and_stale_bbox_needs_relocation(tmp_path):
    m = context(tmp_path)
    observe_findings(
        m,
        cwd=tmp_path,
        map_id="map",
        page_index=0,
        image_sha256="old",
        review={
            "status": "issues",
            "issues": [
                {
                    "severity": "error",
                    "issue_code": "label_overlap",
                    "description": "编号相压",
                    "bbox": [0.2, 0.2, 0.3, 0.3],
                    "roi_ref": "whole",
                }
            ],
        },
    )
    sync_review_pages(
        m, cwd=tmp_path, map_id="map", version=2, pages=[{"page_id": "roster", "page_index": 0}]
    )
    assert not pending_page_targets(m, cwd=tmp_path, map_id="map", page_id="roster", page_index=0)
    assert len(pending_findings(m, cwd=tmp_path, map_ids={"map"})) == 1
    sync_review_pages(
        m,
        cwd=tmp_path,
        map_id="map",
        version=3,
        pages=[{"page_id": "detail", "page_index": 1, "sha256": "old"}],
    )
    targets = pending_page_targets(
        m,
        cwd=tmp_path,
        map_id="map",
        page_id="detail",
        page_index=1,
        page_contract={"regions": {"detail": [10, 20, 100, 100]}},
    )
    assert targets[0]["issue"]["bbox"] is None
    assert "relocation_notice" in targets[0]["issue"]
