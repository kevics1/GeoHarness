"""Visual review uses original pixel crops with stable native frame identities."""

import base64
import io
import json

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ImageBlock
from PIL import Image

from geoharness.cartography.quality import review_map
from geoharness.config.settings import GeoConfig


class Reviewer:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or {"status": "passed", "summary": "Readable", "issues": []}

    async def stream_message(self, request):
        self.requests.append(request)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(json.dumps(self.response)),
            usage=UsageSnapshot(),
        )


async def test_review_sends_whole_and_original_native_region_pixels(tmp_path):
    image = Image.new("RGB", (800, 1000), "white")
    image.putpixel((121, 141), (20, 60, 180))
    path = tmp_path / "map.png"
    image.save(path)
    reviewer = Reviewer()
    evidence = {
        "backend": "qgis",
        "render_metadata": {
            "layers": [],
            "layout": {
                "effective_parameters": {"width_mm": 80, "height_mm": 100},
                "regions": {
                    "main_map": [10, 10, 45, 70],
                    "detail_map": [60, 10, 15, 30],
                    "legend": [60, 45, 15, 35],
                    "footer": [10, 85, 65, 10],
                },
            },
        },
    }
    result = await review_map(
        path, config=GeoConfig(), task="Read", evidence=evidence, client=reviewer
    )
    blocks = [b for b in reviewer.requests[0].messages[0].content if isinstance(b, ImageBlock)]
    assert len(blocks) == 5
    catalog = result.diagnostics["review_rois"]
    assert [r["roi_ref"] for r in catalog] == [
        "whole",
        "main_map",
        "detail_map",
        "legend",
        "footer",
    ]
    assert catalog[1]["frame_id"] == "main_map"
    assert catalog[1]["pixel_bbox"] == [100, 100, 550, 800]
    crop = Image.open(io.BytesIO(base64.b64decode(blocks[1].data)))
    assert crop.size == (450, 700)
    assert crop.getpixel((21, 41)) == (20, 60, 180)
    assert result.status == "passed"


async def test_separate_detail_page_is_not_cropped_from_page_zero_or_accepted(tmp_path):
    path = tmp_path / "map.png"
    Image.new("RGB", (100, 100), "white").save(path)
    result = await review_map(path, config=GeoConfig(), task="Read", client=Reviewer(), evidence={
        "backend": "qgis", "render_metadata": {
            "page_previews": [{"page_index": 0}, {"page_index": 1}],
            "layout_plan": {"frames": {"detail_map": {"page_index": 1}}},
            "native_facts": {"frames": {"detail_map": {"rect_mm": [10, 10, 80, 80]}}},
            "layout": {"effective_parameters": {"width_mm": 100, "height_mm": 100},
                       "regions": {"detail_map": [10, 10, 80, 80]}},
        }})
    assert result.status == "unverified"
    assert result.diagnostics["review_scope"]["unreviewed_page_indices"] == [1]
    assert [roi["roi_ref"] for roi in result.diagnostics["review_rois"]] == ["whole"]


async def test_bad_frame_roi_binding_quarantines_only_dependent_patch(tmp_path):
    path = tmp_path / "map.png"
    Image.new("RGB", (100, 100), "white").save(path)
    common = {"severity": "warning", "description": "Crowded", "bbox": [0, 0, 1, 1]}
    response = {"status": "issues", "summary": "Crowded labels and footer", "issues": [
        {**common, "issue_id": "I2", "frame_id": "main_map", "roi_ref": "main_map",
         "source_indices": [0]},
        {**common, "issue_id": "I5", "frame_id": "whole", "roi_ref": "footer"}],
        "qgis_patch": {"issue_ids": ["I2", "I5"], "layout": {"footer_height_mm": 30},
                       "layers": [{"layer_index": 0, "label_style": {"size_pt": 9}}]}}
    result = await review_map(path, config=GeoConfig(), task="Read", client=Reviewer(response),
        evidence={"backend": "qgis", "render_metadata": {
            "layers": [{"kind": "vector", "geometry_type": "Point"}],
            "native_facts": {"frames": {"main_map": {"rect_mm": [0, 0, 100, 80],
                                                     "source_indices": [0]}}},
            "layout": {"effective_parameters": {"width_mm": 100, "height_mm": 100},
                       "regions": {"footer": [0, 80, 100, 20]}},
        }})
    assert result.qgis_patch.issue_ids == ["I2"]
    assert result.qgis_patch.layout is None
    assert result.qgis_patch.layers[0].label_style.size_pt == 9
    assert result.diagnostics["issue_binding_errors"][0]["issue_id"] == "I5"
    assert len(result.issues) == 2


async def test_native_centred_focus_disputes_offset_claim_without_proving_pixel_readability(
    tmp_path,
):
    path = tmp_path / "map.png"
    Image.new("RGB", (1000, 1000), "white").save(path)
    reviewer = Reviewer(
        {
            "status": "issues",
            "summary": "Claimed offset",
            "issues": [
                {
                    "issue_id": "offcentre",
                    "severity": "warning",
                    "description": "Offset",
                    "native_assertion": "focus_off_center",
                    "frame_id": "detail_map",
                    "roi_ref": "detail_map",
                    "bbox": [0.1, 0.1, 0.9, 0.9],
                    "source_indices": [0],
                }
            ],
            "qgis_patch": {"issue_ids": ["offcentre"], "layout": {"detail_height_ratio": 0.6}},
        }
    )
    result = await review_map(
        path,
        config=GeoConfig(),
        task="Read",
        client=reviewer,
        evidence={
            "backend": "qgis",
            "render_metadata": {
                "native_facts": {
                    "frames": {
                        "detail_map": {
                            "extent": [0, 0, 100, 100],
                            "rect_mm": [10, 10, 80, 80],
                            "focus_bbox": [49.5309, 48.9537, 50.5309, 50.9537],
                        },
                    }
                },
                "layers": [],
                "layout": {"effective_parameters": {"width_mm": 100, "height_mm": 100}},
            },
        },
    )
    assert result.status == "issues"
    assert result.qgis_patch is None
    assert result.diagnostics["disputed_native_assertions"][0]["issue_id"] == "offcentre"
