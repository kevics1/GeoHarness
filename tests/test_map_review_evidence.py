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
