"""Review a map written by a Python script or an external cartography workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.cartography.quality import MapIssue, MapReview, save_review
from geoharness.cartography.review_context import build_review_iteration_context
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


def _verified_render_evidence(
    path: Path, digest: str, receipt: dict[str, Any], roots: list[Path],
) -> dict[str, Any]:
    """Bind backend identity to the actual image, never a caller's backend claim."""
    candidates: dict[Path, str | None] = {}
    versions = receipt.get("versions", []) if isinstance(receipt, dict) else []
    for version in versions if isinstance(versions, list) else []:
        if not isinstance(version, dict):
            continue
        if version.get("image_sha256") != digest:
            continue
        stages = version.get("stages", {})
        packaged = stages.get("packaged", {}) if isinstance(stages, dict) else {}
        if not isinstance(packaged, dict):
            continue
        manifest = packaged.get("manifest_path")
        if isinstance(manifest, str):
            try:
                manifest_path = Path(manifest).resolve()
            except (OSError, ValueError):
                continue
            files = packaged.get("files", [])
            recorded_hash = next((file.get("sha256") for file in (
                                      files if isinstance(files, list) else []
                                  )
                                  if isinstance(file, dict)
                                  and file.get("path") == str(manifest_path)), None)
            candidates[manifest_path] = recorded_hash
    candidates.setdefault((path.parent / "project.json").resolve(), None)
    for manifest_path, recorded_hash in candidates.items():
        manifest_path = manifest_path.resolve()
        if not any(manifest_path.is_relative_to(root) for root in roots):
            continue
        try:
            if manifest_path.stat().st_size > 1024 * 1024:
                continue
            raw = manifest_path.read_bytes()
            if recorded_hash and hashlib.sha256(raw).hexdigest() != recorded_hash:
                continue
            manifest = json.loads(raw)
            if not isinstance(manifest, dict) or manifest.get("preview_sha256") != digest:
                continue
            preview = manifest.get("preview")
            if not isinstance(preview, str) or not preview:
                continue
            bound_image = (manifest_path.parent / preview).resolve()
            if not bound_image.is_relative_to(manifest_path.parent):
                continue
            if hashlib.sha256(bound_image.read_bytes()).hexdigest() != digest:
                continue
            backend = manifest.get("backend")
            if backend not in {"matplotlib", "qgis"}:
                continue
            return {
                "backend": backend,
                "backend_proof": {"manifest_path": str(manifest_path), "image_sha256": digest},
                "render_metadata": {key: manifest[key] for key in (
                    "layers", "layout", "crs", "extent", "analysis",
                ) if key in manifest},
            }
        except (OSError, ValueError, TypeError):
            continue
    return {"backend": "unknown"}


class GeoMapReviewTool(BaseTool):
    name = "geo_map_review"
    description = (
        "Read actual map pixels and review layer visibility, legends, text and layout. "
        "Use after a Python or QGIS workflow produces a PNG map. Returns issues and "
        "suggestions using the verified backend's units; QGIS suggestions use qgis_patch "
        "and explicit zero-based layer_index. Apply suggestions to the next render call "
        "or script and re-render, then review actual pixels again within max_repairs. "
        "The review does not apply patches or prove successful label placement. "
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
        for configured in (
            config.cartography.outputs_dir, config.execution.outputs_dir, config.qgis.outputs_dir,
        ):
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
        from geoharness.cartography.tracking import (
            bind_map_job,
            receipt_summary,
            track_render_result,
        )

        try:
            identity = bind_map_job(context, arguments.map_id, path)
            from geoharness.cartography.jobs import map_job_receipt

            existing = await asyncio.to_thread(
                map_job_receipt,
                context.metadata,
                cwd=context.cwd,
                config=config,
                map_id=identity,
                artifact_path=path,
            )
            if not any(
                version.get("image_sha256") == digest for version in existing.get("versions", [])
            ):
                registered = await asyncio.to_thread(
                    track_render_result,
                    ToolResult(output="Existing map retained", metadata={"output_path": str(path)}),
                    context,
                    identity,
                )
                if registered.is_error:
                    return registered
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: map receipt unavailable: {exc}", is_error=True)
        evidence = dict(arguments.evidence)
        verified = _verified_render_evidence(path, digest, existing, roots)
        evidence["backend"] = verified["backend"]
        evidence.update(build_review_iteration_context(
            context.metadata, identity, config, receipt=existing, image_sha256=digest,
        ))
        # A user supplied layer list must not select the targets of native fixes.
        if "render_metadata" in verified:
            evidence["render_metadata"] = verified["render_metadata"]
            evidence["backend_proof"] = verified["backend_proof"]
        else:
            evidence.pop("render_metadata", None)
            evidence.pop("backend_proof", None)
        layout_status = "unverified"
        measured_layout = None
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
                        "panel_overlaps",
                        "label_overlaps",
                        "clipped_labels",
                        "overflow_text",
                        "figure_text_overlaps",
                        "clipped_figure_text",
                        "text_panel_overlaps",
                        "panels_outside_canvas",
                    )
                    if not all(isinstance(layout.get(key), list) for key in keys):
                        raise ValueError("Layout geometry report has incomplete measured fields.")
                    measured_layout = {**layout, "report_path": str(layout_path)}
                    for key in keys:
                        if layout.get(key):
                            layout_issues.append(
                                MapIssue(
                                    severity="error",
                                    location=key,
                                    description=(
                                        f"Measured layout defect: {key}, count={len(layout[key])}."
                                    ),
                                )
                            )
                    layout_status = "issues" if layout_issues else "passed"
                    evidence["measured_layout"] = {
                        "scope": "layout_geometry",
                        "status": layout_status,
                        **{key: layout.get(key, []) for key in keys},
                    }
            except (OSError, ValueError, TypeError, AttributeError):
                layout_status = "unverified"
        if layout_issues:
            # Fix known collisions first; avoid paying for an image already known to fail.
            result = MapReview(
                status="issues",
                summary="Layout geometry has collisions or clipped content; "
                "repair these before requesting visual review.",
                issues=layout_issues,
            )
            budget = {
                "review_cached": False,
                "review_budget_exhausted": False,
                "image_sha256": digest,
                "visual_review_status": "unverified",
            }
            context.metadata.setdefault("map_workflow_state", {}).setdefault("cache", {})[
                digest
            ] = result.model_dump()
        else:
            result, budget = await review_with_budget(
                path,
                config=config,
                task=arguments.task,
                evidence=evidence,
                retry_invalid_response=arguments.retry_invalid_response,
                metadata=context.metadata,
                map_id=identity,
                client=context.metadata.get("map_review_client"),
            )
        report = save_review(
            path,
            {
                "image_path": str(path),
                **result.model_dump(),
                **budget,
                "layout_status": layout_status,
                "backend": verified["backend"],
            },
        )
        from geoharness.cartography.jobs import record_map_review

        try:
            receipt = await asyncio.to_thread(
                record_map_review,
                identity,
                context.metadata,
                cwd=context.cwd,
                config=config,
                image_path=path,
                review={
                    "image_sha256": digest,
                    "review_path": str(report),
                    **result.model_dump(),
                    "visual_review_status": budget.get("visual_review_status", result.status),
                },
                layout_status=layout_status if layout_status != "unverified" else None,
                measured_layout=measured_layout,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=f"Review retained at {report}; receipt update failed: {exc}",
                is_error=True,
                metadata={"review_path": str(report)},
            )
        return ToolResult(
            output=(
                result.model_dump_json(indent=2)
                + f"\nReport: {report}\nMap receipt: "
                + json.dumps(receipt_summary(receipt), ensure_ascii=False)
                + "\n"
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
                "backend": verified["backend"],
                "review_path": str(report),
                "image_path": str(path),
                "layout_status": layout_status,
                "map_id": identity,
                "map_receipt": receipt,
                **budget,
            },
        )
