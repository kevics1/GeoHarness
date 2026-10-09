"""Bind tool results to file-backed map receipts without trusting narrative text."""

from __future__ import annotations

import json
from pathlib import Path

from openharness.tools.base import ToolResult

from geoharness.cartography.jobs import (
    ensure_map_job,
    map_job_receipt,
    record_map_review,
    record_map_version,
)


def bind_map_job(context, map_id: str = "", artifact_path: Path | None = None) -> str:
    return ensure_map_job(
        context.metadata,
        cwd=context.cwd,
        config=context.metadata["geoharness_config"],
        map_id=map_id,
        artifact_path=artifact_path,
    )


def receipt_summary(receipt: dict) -> dict:
    """Keep repeated tool outputs short; full evidence remains in the saved receipt."""
    return {
        **{
            key: receipt.get(key)
            for key in (
                "job_id",
                "map_id",
                "current_version",
                "quality_status",
                "status",
                "quality_scope",
                "analysis_correctness",
            )
        },
        "stages": {
            name: value.get("status", "unverified")
            for name, value in receipt.get("stages", {}).items()
        },
    }


def track_render_result(
    result: ToolResult, context, map_id: str, *, preflight=None, analysis=None
) -> ToolResult:
    """Register only an existing rendered artifact; missing checks remain unverified."""
    metadata = result.metadata
    image = (
        metadata.get("primary_map") or metadata.get("preview_path") or metadata.get("output_path")
    )
    if not image or not Path(image).is_file():
        return result
    image = Path(image)
    try:
        job_id = bind_map_job(context, map_id, image)
        config = context.metadata["geoharness_config"]
        layout = metadata.get("layout")
        if image.suffix.lower() == ".png" and layout is None:
            layout_path = image.with_suffix(".layout.json")
            if layout_path.is_file():
                try:
                    layout = json.loads(layout_path.read_text(encoding="utf-8"))
                    layout["report_path"] = str(layout_path)
                except (OSError, ValueError):
                    layout = None
        project = metadata.get("project_dir")
        record_map_version(
            job_id,
            context.metadata,
            cwd=context.cwd,
            config=config,
            image_path=image,
            preflight=preflight,
            layout=layout,
            project_dir=Path(project) if project else None,
            analysis=analysis,
        )
        report_path = metadata.get("review_path")
        if report_path and Path(report_path).is_file():
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            if report.get("image_sha256"):
                record_map_review(
                    job_id,
                    context.metadata,
                    cwd=context.cwd,
                    config=config,
                    image_path=image,
                    review={**report, "review_path": str(report_path)},
                )
        receipt = map_job_receipt(
            context.metadata, cwd=context.cwd, config=config, map_id=job_id, artifact_path=image
        )
    except (OSError, ValueError, TypeError) as exc:
        return ToolResult(
            output=result.output + f"\nMap receipt could not be saved: {exc}",
            is_error=True,
            metadata={**metadata, "quality_status": "draft", "receipt_status": "failed"},
        )
    return ToolResult(
        output=result.output
        + "\nMap receipt: "
        + json.dumps(receipt_summary(receipt), ensure_ascii=False),
        is_error=result.is_error,
        metadata={
            **metadata,
            "map_id": job_id,
            "map_receipt": receipt,
            "quality_status": receipt.get("quality_status", "draft"),
        },
    )
