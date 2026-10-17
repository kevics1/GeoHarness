"""Review a map written by a Python script or an external cartography workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, model_validator

from geoharness.cartography.mutation_control import current_owner, guard_map_mutations
from geoharness.cartography.quality import (
    MapIssue,
    MapReview,
    _unreviewed_pages,
    review_execution_payload,
    save_review,
)
from geoharness.cartography.review_context import build_review_iteration_context
from geoharness.cartography.workflow import review_with_budget


def _repair_stage(value):
    owner = current_owner()
    if owner:
        owner.stage(value)
    return value


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
    retry_service_failure: bool = Field(
        default=False,
        description="Explicitly request review of the same image after a transient outage. "
        "Never bypasses service cooldown/Retry-After, retries credentials/input errors, "
        "or redraws the map. After cooldown a new call can send one request.",
    )
    auto_repair: bool | None = Field(
        default=None, description="False disables evidence-bound automatic presentation repair.",
    )
    all_pages: bool = Field(default=False, description=(
        "Review all verified pages of the same registered QGIS package in one tool call. "
        "Review-only; merge findings into one build. No guessed page paths or hidden rebuilds."))

    @model_validator(mode="after")
    def validate_review_controller(self):
        if self.all_pages and self.auto_repair is True:
            raise ValueError("all_pages is review-only; omit auto_repair or set it to false")
        return self


def _verified_render_evidence(
    path: Path, digest: str, receipt: dict[str, Any], roots: list[Path],
) -> dict[str, Any]:
    """Bind backend identity to the actual image, never a caller's backend claim."""
    from geoharness.cartography.jobs import _matching_version

    candidates = {}
    recorded_versions = receipt.get("versions")
    recorded_versions = recorded_versions if isinstance(recorded_versions, list) else []
    matching = _matching_version(receipt, path) if recorded_versions else None
    versions = ([matching] if matching else []) + list(reversed(recorded_versions))
    for version in versions:
        if not isinstance(version, dict):
            continue
        stages = version.get("stages")
        stage = stages.get("packaged", {}) if isinstance(stages, dict) else {}
        if not isinstance(stage, dict) or not isinstance(stage.get("manifest_path"), str):
            continue
        manifest_path = Path(stage["manifest_path"]).resolve()
        files = stage.get("files")
        recorded = {Path(item["path"]).resolve(): item.get("sha256")
                    for item in (files if isinstance(files, list) else []) if isinstance(item, dict)
                    and isinstance(item.get("path"), str)}
        candidates.setdefault(manifest_path, (recorded, version))
    candidates.setdefault((path.parent / "project.json").resolve(), ({}, None))
    for manifest_path, (recorded, version) in candidates.items():
        if not any(manifest_path.is_relative_to(root) for root in roots):
            continue
        try:
            if manifest_path.stat().st_size > 1024 * 1024:
                continue
            raw = manifest_path.read_bytes()
            recorded_hash = recorded.get(manifest_path)
            if recorded_hash and hashlib.sha256(raw).hexdigest() != recorded_hash:
                continue
            manifest = json.loads(raw)
            if not isinstance(manifest, dict) or not isinstance(manifest.get("preview"), str):
                continue
            primary = (manifest_path.parent / manifest["preview"]).resolve()
            if not primary.is_relative_to(manifest_path.parent):
                continue
            primary_digest = hashlib.sha256(primary.read_bytes()).hexdigest()
            if primary_digest != manifest.get("preview_sha256"):
                continue
            backend = manifest.get("backend")
            if backend not in {"matplotlib", "qgis"} or backend == "qgis" and not recorded_hash:
                continue
            previews = manifest.get("page_previews") or (manifest.get("native_facts") or {}).get(
                "page_previews", [])
            page_index = 0
            if path != primary:
                page = next((item for item in previews if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and (manifest_path.parent / item["path"]).resolve() == path), None)
                if (page is None or not path.is_relative_to(manifest_path.parent)
                        or recorded.get(path) != digest
                        or type(page.get("page_index")) is not int):
                    continue
                page_index = page["page_index"]
            elif primary_digest != digest:
                continue
            completed_pages = []
            for item in previews:
                page = item.get("page_index")
                page_path = (manifest_path.parent / item.get("path", "")).resolve()
                previous = (version or {}).get("page_reviews", {}).get(str(page), {})
                if previous.get("status") != "passed" or not page_path.is_relative_to(
                        manifest_path.parent) or recorded.get(page_path) != previous.get(
                            "image_sha256"):
                    continue
                if hashlib.sha256(page_path.read_bytes()).hexdigest() != previous["image_sha256"]:
                    continue
                reference = previous.get("report_reference")
                if isinstance(reference, dict):
                    reference_path = Path(reference["path"]).resolve()
                    if not any(reference_path.is_relative_to(root) for root in roots):
                        continue
                    report_digest = hashlib.sha256(reference_path.read_bytes()).hexdigest()
                    if report_digest != reference.get("sha256"):
                        continue
                completed_pages.append(page)
            return {
                "backend": backend, "review_page_index": page_index,
                "reviewed_page_indices": completed_pages,
                "review_binding": {
                    "map_id": receipt.get("job_id"),
                    "version": version.get("version") if version else None,
                    "page_index": page_index, "image_sha256": digest,
                    "primary_image_sha256": primary_digest,
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_bindings_sha256": hashlib.sha256(json.dumps(
                        (manifest.get("creation_spec") or {}).get("source_bindings", []),
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                },
                "backend_proof": {"manifest_path": str(manifest_path), "image_sha256": digest,
                                  "manifest_sha256": hashlib.sha256(raw).hexdigest()},
                "render_metadata": {
                    "semantic_sources": (manifest.get("creation_spec") or {}).get(
                        "source_bindings", []),
                    "creation_spec_available": isinstance(manifest.get("creation_spec"), dict),
                    **{key: manifest[key] for key in ("layers", "layout", "crs", "extent",
                        "analysis", "layout_plan", "native_facts", "frame_checks", "composition")
                        if key in manifest},
                    "page_previews": previews, "review_page_index": page_index,
                },
            }
        except (OSError, ValueError, TypeError):
            continue

    return {"backend": "unknown"}


class GeoMapReviewTool(BaseTool):
    name = "geo_map_review"
    description = (
        "Read actual map pixels and review layer visibility, legends, text and layout. "
        "Use after a Python or QGIS workflow produces a PNG map. "
        "Use all_pages=true for one registered QGIS package, auto_repair=false; merge "
        "actionable findings into ONE build(spec_ref, edits). Batch mode never rebuilds. "
        "Returns issues and suggestions in the verified backend's units; QGIS uses qgis_patch "
        "and explicit zero-based layer_index. Automatic mode applies only image/ROI-bound "
        "native presentation plans, re-renders in the same map job and reviews the "
        "actual new PNG. Returns the selected candidate, final_render and repair_stop_reason; "
        "use these returned artifacts and do not repeat an already applied repair plan. "
        "Unsupported suggestions remain issues. Review-only mode does not apply patches; "
        "a supported manual presentation change needs a new render and pixel review. "
        "A failed API call means unverified, never passed. Cached results are not new service "
        "failures. Transient outages expire after service cooldown; retry_service_failure "
        "requests reinspection of the same image. Read request_sent, image_provider_calls, "
        "image_cache_hits, visual_review_completed and next_action; ordinary continue does "
        "not guarantee a new provider request or visual success."
    )
    input_model = GeoMapReviewInput

    @guard_map_mutations
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        from geoharness.cartography.workflow import repair_loop_lock

        # Include the first review: parallel callers share one provider request and repair loop.
        async with repair_loop_lock(context.metadata, "review-and-repair"):
            if arguments.all_pages:
                try:
                    return await self._review_all_pages(arguments, context)
                except (OSError, ValueError) as exc:
                    return ToolResult(output=f"Batch review stopped ({type(exc).__name__}); "
                        "completed reports retained. Inspect the returned project/report files.",
                        is_error=True, metadata={"render_executed": False})
            return await self._execute_locked(arguments, context)

    async def _review_all_pages(self, arguments, context) -> ToolResult:
        """Review one immutable package, serializing ledger writes and returning a short receipt."""
        from geoharness.cartography.jobs import map_job_receipt
        from geoharness.qgis.artifacts import returned_page_previews

        single = arguments.model_copy(update={"all_pages": False, "auto_repair": False})
        first = await self._review_one(single, context)
        if first.is_error or first.metadata.get("receipt_status") == "unregistered":
            return first
        config = context.metadata["geoharness_config"]
        image = Path(first.metadata["image_path"]).resolve()
        identity = first.metadata["map_id"]
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  map_id=identity, artifact_path=image)
        verified = _verified_render_evidence(image, hashlib.sha256(image.read_bytes()).hexdigest(),
                                              receipt, [context.cwd.resolve()])
        proof = verified.get("backend_proof", {})
        if verified.get("backend") != "qgis" or not proof.get("manifest_path"):
            return ToolResult(
                output=first.output + "\nBatch unavailable: no verified QGIS package.",
                is_error=True, metadata=first.metadata)
        root = Path(proof["manifest_path"]).parent
        pages = returned_page_previews(verified.get("render_metadata", {}))
        results = [first]
        seen = {image}
        for page in pages:
            path = (root / page["path"]).resolve()
            if path in seen:
                continue
            seen.add(path)
            if not path.is_relative_to(root):
                raise ValueError("Page path leaves the verified package")
            # Recheck each page against packaged file hashes before sending any pixels.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            bound = _verified_render_evidence(path, digest, receipt, [context.cwd.resolve()])
            if not path.is_relative_to(root) or bound.get("backend") != "qgis":
                return ToolResult(output="Batch stopped: a packaged page changed or is unbound.",
                    is_error=True, metadata={"map_id": identity, "render_executed": False,
                                            "completed_page_count": len(results)})
            result = await self._review_one(single.model_copy(update={
                "file_path": str(path), "map_id": identity}), context)
            results.append(result)
            if result.is_error:
                return result
        summary = []
        for result in results:
            body, _ = json.JSONDecoder().raw_decode(result.output.lstrip())
            issues = body.get("issues", [])
            diagnostics = body.get("diagnostics", {})
            status = ("unverified" if diagnostics.get("failure_stage") else
                      "issues" if any(i.get("severity") in {"warning", "error"}
                                      for i in issues) else
                      diagnostics.get("page_visual_status") or
                      result.metadata.get("visual_review_status",
                                          result.metadata.get("review_status", "unverified")))
            summary.append({"page_index": result.metadata.get("review_page_index", 0),
                "status": status,
                "image_path": result.metadata["image_path"],
                "review_binding": result.metadata.get("review_binding", {}),
                "report_path": result.metadata.get("review_path"), "issue_count": len(issues),
                "issues": issues[:5], "issues_truncated": len(issues) > 5,
                "request_sent": result.metadata.get("request_sent", False)})
        summary.sort(key=lambda page: page["page_index"])
        expected = set(_unreviewed_pages(verified.get("render_metadata", {})))
        expected.add(verified.get("review_page_index", 0))
        missing = sorted(expected - {item["page_index"] for item in summary})
        statuses = [item["status"] for item in summary]
        status = ("unverified" if missing or "unverified" in statuses else
                  "issues" if "issues" in statuses else "passed")
        payload = {"status": status, "map_id": identity, "pages": summary,
                   "unreviewed_page_indices": missing,
                   "repair_controller": "agent", "render_executed": False,
                   "next_action": (
                       "Review complete; deliver the package if other required checks pass. "
                       "No new build is needed." if status == "passed" else
                       "Review incomplete; inspect per-page reports and retry only failed pages. "
                       "Do not redraw for a service failure." if status == "unverified" else
                       "Merge actionable findings into one build; read report_path for full "
                       "issues. Retain unresolved limitations.")}
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  map_id=identity, artifact_path=image)
        payload["quality_status"] = receipt.get("quality_status", "draft")
        return ToolResult(output=json.dumps(payload, ensure_ascii=False), metadata=payload)

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
        if (first.is_error or first.metadata.get("review_page_index", 0)
                or first.metadata.get("receipt_status") == "unregistered"):
            return first
        identity = first.metadata["map_id"]
        async with repair_loop_lock(context.metadata, identity):
            current = first
            latest_render = None
            repaired = 0
            stop = "review_complete"
            repair_failure = None
            repair_plans = []
            first_image = Path(first.metadata["image_path"])
            first_digest = hashlib.sha256(first_image.read_bytes()).hexdigest()
            seen_images = {first_digest}
            enabled = (arguments.auto_repair if arguments.auto_repair is not None
                       else config.map_review.auto_repair)
            if arguments.retry_service_failure:
                # Recovery is review-only; a service outage must not cause a new map.
                enabled = False
            while True:
                from geoharness.task_budget import budget_status

                if enabled and budget_status(context.metadata, config).get("phase") in {
                        "delivery", "deadline"}:
                    stop = "Task delivery reserve reached; issues retained, no hidden rebuild."
                    break
                if current.metadata.get("review_page_index", 0):
                    stop = ("Additional page review is review-only; "
                            "repair the registered main package.")
                    break
                if current.metadata.get("review_status") != "issues" or not enabled:
                    stop = (
                        "visual_review_unverified" if enabled and current.metadata.get(
                            "review_status") == "unverified" else
                        "review_complete" if enabled else "review_only"
                    )
                    break
                checker = context.metadata.get("geoharness_permission_checker")
                if checker is None:
                    stop = "Automatic create permission is unavailable; issues retained."
                    break
                decision = checker.evaluate("geo_qgis_project", is_read_only=False)
                if not decision.allowed or decision.requires_confirmation:
                    stop = "Automatic create is not currently allowed; issues retained."
                    break
                repair_stage = _repair_stage("presentation_evidence_validation")
                try:
                    image = Path(current.metadata["image_path"])
                    receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                              map_id=identity, artifact_path=image)
                    from geoharness.cartography.jobs import map_mutation_state

                    live = map_mutation_state(context.metadata, cwd=context.cwd, config=config,
                                               map_id=identity)
                    if receipt.get("selected_version") != live["latest_version"]:
                        raise RepairUnavailable(
                            "Historical image reviewed; repair the latest draft")
                    reference = receipt.get("stages", {}).get("visual_checked", {}).get(
                        "report_reference", {})
                    report_path = Path(current.metadata["review_path"])
                    if (reference.get("path") != str(report_path)
                            or hashlib.sha256(report_path.read_bytes()).hexdigest()
                            != reference.get("sha256")):
                        stop = "Visual evidence changed; prior candidate retained."
                        break
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if report.get("image_sha256") != hashlib.sha256(image.read_bytes()).hexdigest():
                        raise RepairUnavailable("Visual report belongs to a different map image.")
                    review = MapReview.model_validate({k: v for k, v in report.items()
                                                       if k in MapReview.model_fields})
                    repair_stage = _repair_stage("repair_plan")
                    state_owner = current_owner()
                    if state_owner:
                        state_owner.stage("repair_plan")
                    plan = build_repair_plan(image, review, receipt,
                                             config=config, cwd=context.cwd)
                    repair_plans.append({"issue_ids": getattr(plan, "issue_ids", []),
                        "binding": getattr(plan, "binding", {}),
                        "rejected": getattr(plan, "rejected", [])})
                    if not claim_repair_attempt(context.metadata,
                            getattr(plan, "semantic_digest", "") or plan.digest):
                        stop = "This presentation plan was already attempted; issues retained."
                        break
                    repair_stage = _repair_stage("native_presentation_create")
                    rendered = await GeoQgisProjectTool().execute(
                        plan.parameters, context, _source_bindings=plan.source_bindings)
                    if rendered.is_error:
                        repair_failure = {"stage": "native_presentation_create",
                                          "output": rendered.output,
                                          "metadata": rendered.metadata}
                        stop = ("Native presentation create failed; prior candidate retained. "
                                + rendered.output)
                        break
                    repair_stage = _repair_stage("new_render_validation")
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
                    repair_stage = _repair_stage("new_render_review")
                    current = await self._review_one(arguments.model_copy(update={
                        "file_path": str(preview), "map_id": identity,
                        "retry_invalid_response": False,
                        "retry_service_failure": False,
                    }), context)
                    if current.is_error:
                        stop = "Review of the new PNG failed; visual quality is unverified."
                        break
                except Exception as exc:
                    # Preserve the completed review if an optional repair fails internally.
                    # Cancellation and process-control BaseExceptions must still propagate.
                    repair_failure = {"stage": repair_stage, "error": str(exc),
                                      "exception_type": type(exc).__name__}
                    stop = (str(exc) if isinstance(exc, RepairUnavailable)
                            else f"Presentation repair failed at {repair_stage}; "
                                 f"completed review retained. {exc}")
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
                "repair_plans": repair_plans,
                "selected_candidate": candidate, "final_render": final_render,
                "image_path": selected_image, "review_status": selected_review,
                "image_sha256": selected.get("image_sha256"), "review_path": selected_review_path,
                "quality_status": selected.get("quality_status", "draft")}
            return ToolResult(output=current.output + "\nPresentation repair: " + stop
                              + "\nSelected map: " + selected_image
                              + f"\nSelected visual status: {selected_review}; quality: "
                              + selected.get("quality_status", "draft"),
                              is_error=current.is_error,
                              metadata=review_execution_payload(metadata))

    async def _review_without_ledger(self, arguments, context, path, error):
        """Review pixels locally without certifying or automatically repairing a map job."""
        config = context.metadata["geoharness_config"]
        result, budget = await review_with_budget(path, config=config, task=arguments.task,
            evidence={"backend": "unknown", "ledger_unavailable": True},
            retry_invalid_response=arguments.retry_invalid_response,
            retry_service_failure=arguments.retry_service_failure,
            metadata=context.metadata, map_id=arguments.map_id or path.stem,
            client=context.metadata.get("map_review_client"))
        result.diagnostics.update(ledger_error=error, repair_authorized=False,
                                  quality_status="draft", receipt_status="unregistered")
        report = save_review(path, {"image_path": str(path), **result.model_dump(), **budget})
        return ToolResult(output=json.dumps(review_execution_payload(result.model_dump()),
            ensure_ascii=False) + f"\nLocal review retained: {report}; map quality is draft.",
            metadata={"image_path": str(path), "review_path": str(report),
                      "review_status": result.status, "quality_status": "draft",
                      "receipt_status": "unregistered", "ledger_error": error})

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
            page_evidence = _verified_render_evidence(path, digest, existing, roots)
            if not any(version.get("image_sha256") == digest
                       for version in existing.get("versions", [])) and not page_evidence.get(
                           "review_page_index"):
                registered = await asyncio.to_thread(
                    track_render_result,
                    ToolResult(output="Existing map retained", metadata={"output_path": str(path)}),
                    context,
                    identity,
                )
                if registered.is_error:
                    return registered
        except (OSError, ValueError) as exc:
            if arguments.auto_repair is False or arguments.retry_service_failure:
                return await self._review_without_ledger(arguments, context, path, str(exc))
            return ToolResult(output=f"Error: map receipt unavailable: {exc}", is_error=True)
        evidence = dict(arguments.evidence)
        verified = _verified_render_evidence(path, digest, existing, roots)
        evidence.pop("review_binding", None)
        evidence["backend"] = verified["backend"]
        evidence.update(build_review_iteration_context(
            context.metadata, identity, config, receipt=existing, image_sha256=digest,
            page_index=verified.get("review_page_index", 0),
        ))
        # A user supplied layer list must not select the targets of native fixes.
        if "render_metadata" in verified:
            evidence["render_metadata"] = verified["render_metadata"]
            evidence["backend_proof"] = verified["backend_proof"]
            evidence["review_binding"] = verified.get("review_binding", {})
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
                diagnostics={"stage": "layout", "outcome": "blocked", "request_sent": False,
                             "visual_review_completed": False,
                             "next_action": "repair_measured_layout_first"},
            )
            budget = {
                "review_cached": False,
                "review_budget_exhausted": False,
                "image_sha256": digest,
                "visual_review_status": "unverified",
                "request_sent": False,
                "visual_review_completed": False,
                "next_action": "repair_measured_layout_first",
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
                retry_service_failure=arguments.retry_service_failure,
                metadata=context.metadata,
                map_id=identity,
                client=context.metadata.get("map_review_client"),
            )
        # Cached conclusions predate newer layouts too; page-zero pixels cannot accept page one.
        page_index = verified.get("review_page_index", 0)
        unreviewed = [page for page in _unreviewed_pages(verified.get("render_metadata") or {})
                      if page not in verified.get("reviewed_page_indices", [])]
        current_binding = verified.get("review_binding")
        if budget.get("review_cached") and current_binding:
            clean_pixels = (result.status == "passed" or result.diagnostics.get(
                "page_visual_status") == "passed") and (
                not result.diagnostics.get("failure_stage")
                and not any(issue.severity in {"warning", "error"} for issue in result.issues)
                and result.qgis_patch is None and result.next_strategy is None
                and not result.layout_patch.model_dump(exclude_none=True))
            if clean_pixels:
                # Only a completed pixel pass is reusable across verified package versions.
                result.diagnostics["review_binding"] = dict(current_binding)
                result.diagnostics["page_visual_status"] = "passed"
                result.diagnostics["pixel_pass_rebound_to_current_package"] = True
                if not unreviewed or page_index != 0:
                    result.status = "passed"
                    budget["visual_review_status"] = "passed"
            elif result.diagnostics.get("review_binding") != current_binding:
                result.qgis_patch = None
                result.next_strategy = None
                result.diagnostics["repair_binding_status"] = "stale_cached_package"
        result.diagnostics["review_scope"] = {"reviewed_page_indices": [page_index],
                                             "unreviewed_page_indices": unreviewed}
        if (not unreviewed and result.diagnostics.get("page_visual_status") == "passed"
                and not result.diagnostics.get("failure_stage")
                and not any(issue.severity in {"warning", "error"} for issue in result.issues)
                and result.qgis_patch is None and result.next_strategy is None
                and not result.layout_patch.model_dump(exclude_none=True)):
            result.status = "passed"
            budget["visual_review_status"] = "passed"
        if unreviewed:
            if result.status == "passed" and page_index == 0:
                result.status = "unverified"
                result.summary = "Additional layout pages have no completed pixel review."
                budget["visual_review_status"] = "unverified"
                budget["visual_review_completed"] = False
        from geoharness.cartography.quality_repair import review_semantic_state

        result.diagnostics["semantic_quality"] = review_semantic_state(
            result, verified.get("render_metadata") or {}, page_index=page_index,
            map_id=identity)
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
                layout_status=(layout_status if page_index == 0 and layout_status != "unverified"
                               else None),
                measured_layout=measured_layout if page_index == 0 else None,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=f"Review retained at {report}; receipt update failed: {exc}",
                is_error=True,
                metadata={"review_path": str(report)},
            )
        return ToolResult(
            output=(
                json.dumps(review_execution_payload(result.model_dump()),
                           ensure_ascii=False, indent=2)
                + f"\nReport: {report}\nMap receipt: "
                + json.dumps(receipt_summary(receipt), ensure_ascii=False)
                + "\n"
                + (
                    "No new review request was sent: cached conclusion or service cooldown. "
                    "Follow diagnostics.next_action and service_retry_after_seconds.\n"
                    if budget["review_cached"]
                    else ""
                )
                + (
                    "STOP visual retries: deliver an unreviewed draft with remaining issues.\n"
                    if budget["review_budget_exhausted"]
                    else ""
                )
            ),
            metadata=review_execution_payload({
                "review_status": result.status,
                "review_page_index": page_index,
                "review_binding": result.diagnostics.get("review_binding", {}),
                "backend": verified["backend"],
                "review_path": str(report),
                "image_path": str(path),
                "layout_status": layout_status,
                "map_id": identity,
                "map_receipt": receipt,
                **budget,
            }),
        )
