"""Explicit image capability and map probes, independent of business map ledgers."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any

from geoharness.cartography import quality
from geoharness.cartography.review_config import review_configuration
from geoharness.config.settings import GeoConfig


def _capability_image(path: Path, code: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((60, 60, 180, 180), fill="red")
    draw.rectangle((260, 60, 380, 180), fill="blue")
    draw.polygon([(520, 60), (460, 180), (580, 180)], fill="green")
    # Integer scaling of the built-in font keeps the test independent of system fonts.
    text = Image.new("RGB", (160, 24), "white")
    ImageDraw.Draw(text).text((0, 0), code, font=ImageFont.load_default(), fill="black")
    image.paste(text.resize((480, 72)), (80, 250))
    image.save(path)


def _identified(summary: str, code: str) -> bool:
    summary = summary.lower()
    return code in summary and all(re.search(pattern, summary) for pattern in (
        r"红(?:色|的|色的)?(?:实心)?圆|red\s+(?:filled\s+)?circle",
        r"蓝(?:色|的|色的)?(?:正)?方|blue\s+(?:filled\s+)?square",
        r"绿(?:色|的|色的)?三角|green\s+(?:filled\s+)?triangle",
    ))


async def run_review_probe(
    config: GeoConfig, output_dir: Path, image: Path | None = None,
) -> dict[str, Any]:
    """Send exactly one explicit request and save diagnostics without changing a map."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="review-probe-", dir=output_dir))
    synthetic = image is None
    code = str(secrets.randbelow(900000) + 100000) if synthetic else ""
    if synthetic:
        image = work / "capability.png"
        _capability_image(image, code)
    assert image is not None
    image = image.resolve()
    if not image.is_file():
        raise ValueError("Review probe image does not exist.")
    before = hashlib.sha256(image.read_bytes()).hexdigest()
    task = (
        "这是图片输入能力测试。请在summary中复述图中实际读到的六位数字，"
        "以及三种形状各自的颜色和形状名称，不猜测。不要从任务文字推断答案。"
        if synthetic else "检查此页图片中可确认的文字、图例、遮挡、裁切及布局问题。"
    )
    result = await quality.review_map(
        image, config=config, task=task, evidence={}, pixel_only=True)
    unchanged = hashlib.sha256(image.read_bytes()).hexdigest() == before
    completed = result.diagnostics.get("outcome") == "completed" and result.status in {
        "passed", "issues"} and unchanged
    verified = completed and _identified(result.summary, code) if synthetic else None
    fields = ("status", "summary", "issues", "limitations", "diagnostics")
    report = {
        "status": "succeeded" if (verified if synthetic else completed) else "failed",
        "probe_kind": "image_capability" if synthetic else "map_review",
        "configuration": review_configuration(config),
        "review_completed": completed,
        "image_capability_verified": verified,
        "input_unchanged": unchanged,
        "image_sha256": before,
        "business_quality_verified": False,
        "review": {key: result.model_dump(mode="json")[key] for key in fields},
        "report_path": str(work / "report.json"),
    }
    # The common reviewer redacts provider/config secrets from its own output.
    # Apply the same redaction to configuration metadata as a final boundary.
    report = quality._redact_review(report, quality._review_secrets(config))
    (work / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
