"""Review a map written by a Python script or an external cartography workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.cartography.pixel_review import public_payload
from geoharness.cartography.quality import (
    _unreviewed_pages,
    review_execution_payload,
    save_review,
)
from geoharness.cartography.review_context import build_review_iteration_context
from geoharness.cartography.workflow import review_with_budget


def _input_schema(schema):
    # Accept old review-only calls, without teaching removed repair/evidence inputs.
    for key in ("auto_repair", "evidence"):
        schema.get("properties", {}).pop(key, None)


class GeoMapReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_input_schema)
    file_path: str = Field(description="PNG/JPEG map image to inspect; PDF/SVG need a PNG preview.")
    task: str = Field(default="", description=(
        "仅首次或检查要求改变时填写。复审省略则复用同地图的检查目标；"
        "不要把最终复核等进度措辞当作新目标。"))
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
        default=None, exclude=True,
        description="Compatibility only: this tool is always read-only.",
    )
    all_pages: bool = Field(default=False, description=(
        "Review all verified pages of the same registered QGIS package in one tool call. "
        "Review-only; merge findings into one build. No guessed page paths or hidden rebuilds."))

    @model_validator(mode="after")
    def validate_review_controller(self):
        if self.auto_repair is True:
            raise ValueError("geo_map_review is read-only; automatic repair has been removed")
        return self


def review_goal(task, metadata, identity, config):
    state = metadata.setdefault('map_workflow_state', {})
    tasks = state.setdefault('review_tasks', {})
    goal = task.strip() or tasks.get(identity) or config.map_review.default_task
    tasks[identity] = goal
    for old in list(tasks)[:-64]:
        tasks.pop(old)
    return goal


async def review_pixels(path, *, arguments, context, identity, evidence):
    config = context.metadata['geoharness_config']
    task = review_goal(arguments.task, context.metadata, identity, config)
    kwargs = dict(config=config, task=task, evidence=evidence,
        retry_service_failure=arguments.retry_service_failure, metadata=context.metadata,
        map_id=identity, client=context.metadata.get('map_review_client'), pixel_only=True)
    result, flags = await review_with_budget(path, **kwargs,
        retry_invalid_response=arguments.retry_invalid_response)
    stage = result.diagnostics.get('failure_stage')
    # Recover the response contract here, without another main-model turn or any redraw.
    if (stage in {'json', 'schema'} and config.map_review.recover_invalid_response
            and 'retry_invalid_response' not in arguments.model_fields_set
            and not flags.get('invalid_response_retry')):
        first_sent = bool(flags.get('request_sent'))
        result, flags = await review_with_budget(path, **kwargs, retry_invalid_response=True)
        result.diagnostics.update(format_recovery_attempted=True,
                                  format_recovery_initial_stage=stage)
        flags['format_recovery_request_count'] = int(first_sent) + int(
            bool(flags.get('request_sent')))
    return result, flags


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
        "Read actual map images and report visible problems: unreadable text, overlap, "
        "occlusion, clipping, blank space and legend mismatches. Returns status, summary, "
        "issues, limitations and image-bound reports. Read-only: never changes styles, "
        "plans parameter patches, rebuilds maps or selects another candidate. "
        "Use all_pages=true to inspect verified package pages. Whole-page images and "
        "original-resolution crops are inspected directly by the image model. "
        "A service/budget failure is unverified, never passed. Read request_sent and "
        "diagnostics before retrying; cooldown/cache hits are not new service requests."
    )
    input_model = GeoMapReviewInput

    def is_read_only(self, arguments: BaseModel) -> bool:
        return True

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        from geoharness.cartography.workflow import repair_loop_lock

        # Serialize batch reports and ledger writes; no map mutation lock is acquired.
        async with repair_loop_lock(context.metadata, "pixel-review"):
            if arguments.all_pages:
                try:
                    return await self._review_all_pages(arguments, context)
                except (OSError, ValueError) as exc:
                    return ToolResult(output=f"Batch review stopped ({type(exc).__name__}); "
                        "completed reports retained. Inspect the returned project/report files.",
                        is_error=True, metadata={"render_executed": False})
            return await self._review_one(arguments, context)

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
                "page_role": diagnostics.get('page_role', 'unknown'),
                "review_outcome": diagnostics.get('review_outcome',
                    'service_or_contract_failure' if diagnostics.get('failure_stage')
                    else 'model_unable_to_assess' if status == 'unverified' else 'completed'),
                "repair_progress": diagnostics.get('repair_progress', {}),
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
                   "review_mode": "pixels_only", "render_executed": False,
                   "next_action": (
                       "本次逐页审查完成；其他必要检查通过后交付，无需再次构建。"
                       if status == "passed" else
                       "逐页检查未完成，请查看review_outcome区分服务失败、缺页与模型无法判断。"
                       "仅重试真正失败的请求；用途或要求不明确时询问用户，不因服务问题重绘。"
                       if status == "unverified" else
                       "先核实图片报告的问题与限制，再决定修改；审查没有改变地图。")}
        pending = next((p['repair_progress'] for p in summary
                        if p['repair_progress'].get('repeated_concern_count')), {})
        payload['diagnostics'] = {'repair_progress': pending}
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  map_id=identity, artifact_path=image)
        payload["quality_status"] = receipt.get("quality_status", "draft")
        return ToolResult(output=json.dumps(payload, ensure_ascii=False), metadata=payload)

    async def _review_without_ledger(self, arguments, context, path, error):
        """Review pixels locally without certifying or automatically repairing a map job."""
        result, budget = await review_pixels(path, arguments=arguments, context=context,
            identity=arguments.map_id or path.stem,
            evidence={"backend": "unknown", "ledger_unavailable": True})
        result.diagnostics.update(ledger_error=error, repair_authorized=False,
                                  quality_status="draft", receipt_status="unregistered")
        payload = public_payload(result.model_dump())
        budget["visual_review_status"] = result.status
        report = save_review(path, {"image_path": str(path), **payload, **budget})
        return ToolResult(output=json.dumps(review_execution_payload(payload),
            ensure_ascii=False) + f"\nLocal review retained: {report}; map quality is draft.",
            metadata={"image_path": str(path), "review_path": str(report),
                      "review_status": result.status, "quality_status": "draft",
                      "receipt_status": "unregistered", "ledger_error": error,
                      "render_executed": False, **budget})

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
            return await self._review_without_ledger(arguments, context, path, str(exc))
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
        # Geometry checks belong to rendering; they never substitute for image review.
        result, budget = await review_pixels(path, arguments=arguments, context=context,
            identity=identity, evidence=evidence)
        from geoharness.cartography.review_progress import assess_repair_progress

        result.diagnostics['repair_progress'] = assess_repair_progress(
            evidence.get('previous_visual_reviews', []),
            {**result.model_dump(), 'image_sha256': digest,
             'page_index': verified.get('review_page_index', 0)})
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
        budget.setdefault("visual_review_status", result.status)
        report = save_review(
            path,
            {
                "image_path": str(path),
                **public_payload(result.model_dump()),
                **budget,
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
                    **public_payload(result.model_dump()),
                    "visual_review_status": budget.get("visual_review_status", result.status),
                },
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=f"Review retained at {report}; receipt update failed: {exc}",
                is_error=True,
                metadata={"review_path": str(report)},
            )
        return ToolResult(
            output=(
                json.dumps(review_execution_payload(public_payload(result.model_dump())),
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
                "render_executed": False,
                "review_page_index": page_index,
                "review_binding": result.diagnostics.get("review_binding", {}),
                "backend": verified["backend"],
                "review_path": str(report),
                "image_path": str(path),
                "map_id": identity,
                "map_receipt": receipt,
                **budget,
            }),
        )
