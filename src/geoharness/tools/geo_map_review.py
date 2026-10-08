"""Review a map written by a Python script or an external cartography workflow."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.cartography.quality import MapIssue, MapReview, save_review
from geoharness.cartography.workflow import review_with_budget


class GeoMapReviewInput(BaseModel):
    file_path: str = Field(description="PNG/JPEG map image to inspect; PDF/SVG need a PNG preview.")
    task: str = Field(description="User's map requirements and the analysis goal.")
    map_id: str = Field(default="", description="Stable logical map ID; keep it across versions.")
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Expected layers, CRS, source counts and rendering parameters.",
    )
    retry_invalid_response: bool = Field(
        default=False,
        description="Allow one budgeted retry of a cached JSON/schema failure; never loops.",
    )


class GeoMapReviewTool(BaseTool):
    name = "geo_map_review"
    description = (
        "Read actual map pixels and review layer visibility, legends, text and layout. "
        "Use after geo_python produces a PNG map. Returns issues and presentation fixes; "
        "apply those fixes to the script and re-render, then review again within max_repairs. "
        "A failed API call means unverified, never passed."
    )
    input_model = GeoMapReviewInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoMapReviewInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        path = Path(arguments.file_path).expanduser()
        if not path.is_absolute():
            path = context.cwd / path
        path = path.resolve()
        roots = [context.cwd.resolve()]
        for configured in (config.cartography.outputs_dir, config.execution.outputs_dir):
            if configured:
                root = Path(configured).expanduser()
                roots.append((root if root.is_absolute() else context.cwd / root).resolve())
        if not any(path.is_relative_to(root) for root in roots):
            return ToolResult(
                output="Error: map must be in the workspace or output directory.", is_error=True
            )
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not path.is_file():
            return ToolResult(
                output="Error: an existing PNG/JPEG/WebP map image is required.", is_error=True
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        evidence = dict(arguments.evidence)
        layout_status = "unverified"
        layout_issues = []
        layout_path = path.with_suffix(".layout.json")
        if layout_path.is_file():
            try:
                layout = json.loads(layout_path.read_text(encoding="utf-8"))
                if (
                    layout.get("image_sha256") == digest
                    and layout.get("scope") == "layout_geometry"
                ):
                    keys = (
                        "panel_overlaps", "label_overlaps", "clipped_labels", "overflow_text",
                        "figure_text_overlaps", "clipped_figure_text", "text_panel_overlaps",
                        "panels_outside_canvas",
                    )
                    for key in keys:
                        if layout.get(key):
                            layout_issues.append(MapIssue(
                                severity="error", location=key,
                                description=(
                                    f"Measured layout defect: {key}, count={len(layout[key])}."
                                ),
                            ))
                    layout_status = "issues" if layout_issues else "passed"
                    evidence["measured_layout"] = {
                        "scope": "layout_geometry", "status": layout_status,
                        **{key: layout.get(key, []) for key in keys},
                    }
            except (OSError, ValueError, TypeError, AttributeError):
                layout_status = "unverified"
        if layout_issues:
            # Fix known collisions first; avoid paying for an image already known to fail.
            result = MapReview(
                status="issues", summary="Layout geometry has collisions or clipped content; "
                "repair these before requesting visual review.", issues=layout_issues,
            )
            budget = {"review_cached": False, "review_budget_exhausted": False,
                      "image_sha256": digest, "visual_review_status": "unverified"}
            context.metadata.setdefault("map_workflow_state", {}).setdefault(
                "cache", {}
            )[digest] = result.model_dump()
        else:
            result, budget = await review_with_budget(
                path, config=config, task=arguments.task, evidence=evidence,
                retry_invalid_response=arguments.retry_invalid_response,
                metadata=context.metadata, map_id=arguments.map_id,
                client=context.metadata.get("map_review_client"),
            )
        report = save_review(path, {"image_path": str(path), **result.model_dump(), **budget,
                                    "layout_status": layout_status})
        return ToolResult(
            output=(
                result.model_dump_json(indent=2)
                + f"\nReport: {report}\n"
                + (
                    "Unchanged image: cached result; do not retry.\n"
                    if budget["review_cached"]
                    else ""
                )
                + (
                    "STOP visual retries: deliver an unreviewed draft with remaining issues.\n"
                    if budget["review_budget_exhausted"]
                    else ""
                )
            ),
            metadata={
                "review_status": result.status,
                "review_path": str(report),
                "image_path": str(path),
                "layout_status": layout_status,
                **budget,
            },
        )
