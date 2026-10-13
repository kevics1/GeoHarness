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
        description="Allow one explicit retry of a cached JSON/schema failure; never loops.",
    )
    auto_repair: bool | None = Field(
        default=None, description="False disables evidence-bound automatic presentation repair.",
    )


def _verified_render_evidence(
    path: Path, digest: str, receipt: dict[str, Any], roots: list[Path],
) -> dict[str, Any]:
    """Bind backend identity to the actual image, never a caller's backend claim."""
    from geoharness.cartography.jobs import _matching_version

    candidates: dict[Path, str | None] = {}
    matching = _matching_version(receipt, path) if receipt.get("versions") else None
    for version in [matching] if matching else []:
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
            if backend == "qgis" and not recorded_hash:
                # An adjacent JSON file written by a script is not native engineering proof.
                continue
            return {
                "backend": backend,
                "backend_proof": {"manifest_path": str(manifest_path), "image_sha256": digest,
                                  "manifest_sha256": hashlib.sha256(raw).hexdigest()},
                "render_metadata": {
                    "creation_spec_available": isinstance(manifest.get("creation_spec"), dict),
                    **{key: manifest[key] for key in (
                    "layers", "layout", "crs", "extent", "analysis", "layout_plan",
                    "native_facts", "frame_checks", "composition",
                ) if key in manifest}},
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
        "and explicit zero-based layer_index. Automatic mode applies only image/ROI-bound "
        "native presentation plans, re-renders in the same map job and reviews the "
        "actual new PNG. Returns the selected candidate, final_render and repair_stop_reason; "
        "use these returned artifacts and do not repeat an already applied repair plan. "
        "Unsupported suggestions remain issues. Review-only mode does not apply patches; "
        "a supported manual presentation change needs a new render and pixel review. "
        "A failed API call means unverified, never passed."
    )
    input_model = GeoMapReviewInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        from geoharness.cartography.workflow import repair_loop_lock

        # Include the first review: parallel callers share one provider request and repair loop.
        async with repair_loop_lock(context.metadata, "review-and-repair"):
            return await self._execute_locked(arguments, context)

    async def _execute_locked(self, arguments, context) -> ToolResult:
        assert isinstance(arguments, GeoMapReviewInput)
        from geoharness.cartography.jobs import map_job_receipt
        from geoharness.cartography.quality_repair import (
            RepairUnavailable,
            build_repair_plan,
            verified_package,
        )
        from geoharness.cartography.workflow import claim_repair_attempt, repair_loop_lock
        from geoharness.tools.geo_qgis_project import GeoQgisProjectTool

        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        first = await self._review_one(arguments, context)
        if first.is_error:
            return first
        identity = first.metadata["map_id"]
        async with repair_loop_lock(context.metadata, identity):
            current = first
            latest_render = None
            repaired = 0
            stop = "review_complete"
            repair_failure = None
            first_image = Path(first.metadata["image_path"])
            first_digest = hashlib.sha256(first_image.read_bytes()).hexdigest()
            seen_images = {first_digest}
            enabled = (arguments.auto_repair if arguments.auto_repair is not None
                       else config.map_review.auto_repair)
            while True:
                if current.metadata.get("review_status") != "issues" or not enabled:
                    stop = "review_complete" if enabled else "review_only"
                    break
                checker = context.metadata.get("geoharness_permission_checker")
                if checker is None:
                    stop = "Automatic create permission is unavailable; issues retained."
                    break
                decision = checker.evaluate("geo_qgis_project", is_read_only=False)
                if not decision.allowed or decision.requires_confirmation:
                    stop = "Automatic create is not currently allowed; issues retained."
                    break
                try:
                    image = Path(current.metadata["image_path"])
                    receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                              map_id=identity, artifact_path=image)
                    reference = receipt.get("stages", {}).get("visual_checked", {}).get(
                        "report_reference", {})
                    report_path = Path(current.metadata["review_path"])
                    if (reference.get("path") != str(report_path)
                            or hashlib.sha256(report_path.read_bytes()).hexdigest()
                            != reference.get("sha256")):
                        stop = "Visual evidence changed; prior candidate retained."
                        break
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    review = MapReview.model_validate({k: v for k, v in report.items()
                                                       if k in MapReview.model_fields})
                    plan = build_repair_plan(image, review, receipt,
                                             config=config, cwd=context.cwd)
                    if not claim_repair_attempt(context.metadata, plan.digest):
                        stop = "This presentation plan was already attempted; issues retained."
                        break
                    rendered = await GeoQgisProjectTool().execute(
                        plan.parameters, context, _source_bindings=plan.source_bindings)
                    if rendered.is_error:
                        repair_failure = {"stage": "native_presentation_create",
                                          "output": rendered.output,
                                          "metadata": rendered.metadata}
                        stop = ("Native presentation create failed; prior candidate retained. "
                                + rendered.output)
                        break
                    preview = (rendered.metadata.get("preview_path")
                               or rendered.metadata.get("primary_map"))
                    if not preview or not Path(preview).is_file():
                        stop = "Native create returned no actual PNG; prior candidate retained."
                        break
                    repaired += 1
                    latest_render = rendered.metadata
                    new_digest = hashlib.sha256(Path(preview).read_bytes()).hexdigest()
                    if new_digest in seen_images:
                        current = ToolResult(output=current.output, metadata={
                            **current.metadata, "image_path": str(preview),
                        })
                        stop = ("Presentation repair produced previously reviewed pixels; "
                                "no repeat review.")
                        break
                    seen_images.add(new_digest)
                    current = await self._review_one(arguments.model_copy(update={
                        "file_path": str(preview), "map_id": identity,
                        "retry_invalid_response": False,
                    }), context)
                    if current.is_error:
                        stop = "Review of the new PNG failed; visual quality is unverified."
                        break
                except (OSError, ValueError, RuntimeError) as exc:
                    repair_failure = {"stage": "presentation_validation", "error": str(exc)}
                    stop = (str(exc) if isinstance(exc, RepairUnavailable)
                            else f"Presentation validation failed; issues retained. {exc}")
                    break
            latest_image = current.metadata.get("image_path", first.metadata["image_path"])
            receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                      map_id=identity, artifact_path=Path(latest_image))
            candidate = receipt.get("delivery_candidate")
            selected_image = candidate["image_path"] if candidate else latest_image
            selected = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                       map_id=identity, artifact_path=Path(selected_image))
            final_render = None
            try:
                package = verified_package(Path(selected_image), selected, cwd=context.cwd,
                                           config=config, require_facts=False)
                manifest, root = package["manifest"], package["root"]
                pdf = next((p for p in package["recorded"] if p.suffix.lower() == ".pdf"), None)
                packaged_preview = str(root / manifest["preview"])
                final_render = {"project_dir": str(root),
                    "project_file": str(root / manifest["project_file"]),
                    "project_manifest": str(root / "project.json"),
                    "preview_path": packaged_preview, "primary_map": packaged_preview,
                    "output_path": str(pdf) if pdf else packaged_preview,
                    "preflight": selected.get("stages", {}).get("data_checked", {}).get("report"),
                    **{k: manifest[k] for k in ("layout", "layers", "native_facts", "frame_checks",
                                               "analysis", "preflight") if k in manifest}}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            visual = selected.get("stages", {}).get("visual_checked", {})
            selected_layout = selected.get("stages", {}).get("layout_checked", {})
            selected_review = ("issues" if selected_layout.get("status") == "issues"
                               else visual.get("status", "unverified"))
            selected_review_path = visual.get("report_reference", {}).get("path")
            metadata = {**current.metadata, "map_receipt": selected, "latest_image": latest_image,
                "latest_image_sha256": hashlib.sha256(Path(latest_image).read_bytes()).hexdigest(),
                "latest_review_status": current.metadata.get("review_status", "unverified"),
                "latest_review_path": current.metadata.get("review_path"),
                "latest_render": latest_render, "repair_count": repaired,
                "repair_stop_reason": stop,
                "repair_failure": repair_failure,
                "selected_candidate": candidate, "final_render": final_render,
                "image_path": selected_image, "review_status": selected_review,
                "image_sha256": selected.get("image_sha256"), "review_path": selected_review_path,
                "quality_status": selected.get("quality_status", "draft")}
            return ToolResult(output=current.output + "\nPresentation repair: " + stop
                              + "\nSelected map: " + selected_image
                              + f"\nSelected visual status: {selected_review}; quality: "
                              + selected.get("quality_status", "draft"),
                              is_error=current.is_error, metadata=metadata)

    async def _review_one(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
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
