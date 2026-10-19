"""Review a map written by a Python script or an external cartography workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Annotated, Any

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
    file_path: str = Field(description='必填，实际PNG/JPEG图片路径；PDF/SVG须先使用其PNG预览。')
    task: str = Field(default="", description=(
        "检查任务的简短说明；已登记地图仅作备注。新增实质检查要求请用page_checks，"
        "不要把最终复核等进度措辞当作新目标。"))
    checks: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=16,
        description='未登记单图的检查要求；已登记地图兼容作备注，新增验收要求请用page_checks。')
    page_checks: dict[str, list[Annotated[str, Field(min_length=1, max_length=200)]]] = Field(
        default_factory=dict, max_length=64,
        description='真实page_id到该页新增验收要求的映射；省略沿用，空列表清除自定义要求。'
        '首次可声明，后续变更须显式update_page_requirements=true；仅对应页失效。'
        '每页最多16项。修复进度写repair_notes，不重复改写验收要求。')
    update_page_requirements: bool = Field(default=False,
        description='默认false。仅当业务验收要求确实变化时为true；不可为最终复核或修复说明开启。')
    repair_notes: str = Field(default='', max_length=1000,
        description='本轮已修改项/进度备注，不改变验收要求与缓存，不传坐标猜测或新的业务要求。')
    review_layout_draft: bool = Field(default=False,
        description='默认false，已知布局缺陷先修复再请求视觉。仅明确需要查看带布局错误的草稿时为true；不能授权交付。')
    map_id: str = Field(default="",
        description='逻辑地图ID，默认空按图片登记识别；各版本保持一致。')
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Expected layers, CRS, source counts and rendering parameters.",
    )
    retry_invalid_response: bool = Field(
        default=False,
        description='默认false；仅允许对已缓存的JSON/Schema失败显式恢复一次。',
    )
    retry_service_failure: bool = Field(
        default=False,
        description='默认false；瞬态服务故障恢复后显式重试，同样受冷却约束，不重画图片。',
    )
    auto_repair: bool | None = Field(
        default=None, exclude=True,
        description="Compatibility only: this tool is always read-only.",
    )
    all_pages: bool = Field(default=False, description=(
        '默认false；true核对已登记原生制图清单的全部页面，仅缺少有效证据的页发送请求，已有issues也复用；'
        '未变页面复用当前版本证据，不需要QGIS工程。'))

    @model_validator(mode="after")
    def validate_review_controller(self):
        if any(len(checks) > 16 for checks in self.page_checks.values()):
            raise ValueError('page_checks每页最多16项')
        if self.auto_repair is True:
            raise ValueError("geo_map_review is read-only; automatic repair has been removed")
        return self


def review_goal(task, metadata, identity, config, *, page_index=0):
    state = metadata.setdefault('map_workflow_state', {})
    tasks = state.setdefault('review_tasks', {})
    key = f'{identity}:{page_index}'
    goal = task.strip() or tasks.get(key) or tasks.get(identity) or config.map_review.default_task
    tasks[key] = goal
    for old in list(tasks)[:-64]:
        tasks.pop(old)
    return goal


async def review_pixels(path, *, arguments, context, identity, evidence):
    config = context.metadata['geoharness_config']
    binding = evidence.get('review_binding') or {}
    page_index = binding.get('page_index')
    if type(page_index) is not int:
        page_index = (evidence.get('render_metadata') or {}).get('review_page_index', 0)
    scoped = 'review_checks' in evidence
    task = ('只检查当前页的可见像素问题，遵循当前页职责和验收要求。'
            if scoped else review_goal(arguments.task, context.metadata, identity, config,
                                      page_index=page_index))
    # A map has several pages with different acceptance duties. Storing checks
    # under the job alone broadcasts an overview/roster requirement to pages
    # that cannot satisfy it and defeats cache reuse. Bind explicit checks to
    # the actual page; omitted checks retain that page's prior requirements.
    scope_key = f'{identity}:{page_index}'
    checks = context.metadata.setdefault('map_workflow_state', {}).setdefault('review_checks', {})
    if not scoped and 'checks' in arguments.model_fields_set:
        checks[scope_key] = list(arguments.checks)
    if not scoped:
        evidence = {**evidence, 'review_checks': checks.get(scope_key, [])}
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
        first_diagnostics = dict(result.diagnostics)
        result, flags = await review_with_budget(path, **kwargs, retry_invalid_response=True)
        result.diagnostics.update(format_recovery_attempted=True,
                                  format_recovery_initial_stage=stage,
                                  format_recovery_initial_diagnostics=first_diagnostics)
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
    # Materialization relocates the same reviewed pages. Keep the hash-bound
    # preview package as a read-only alias instead of inventing a render version.
    versions = [candidate for version in versions if isinstance(version, dict)
        for candidate in ([version, {**version, 'stages': {**version.get('stages', {}),
            'packaged': version['preview_package']}}] if version.get('preview_package')
            else [version])]
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
                    "page_id": next((p.get('page_id') for p in previews
                        if p.get('page_index') == page_index), None),
                    "primary_image_sha256": primary_digest,
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "source_bindings_sha256": hashlib.sha256(json.dumps(
                        manifest.get("source_inventory") or
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


def _layout_precondition(verified, arguments):
    metadata = verified.get('render_metadata') or {}
    checks = (metadata.get('layout') or {}).get('bounds_check') or {}
    if (not arguments.review_layout_draft and verified.get('backend') == 'matplotlib'
            and (metadata.get('native_facts') or {}).get('frames')
            and checks.get('valid') is False):
        return ToolResult(output=json.dumps({'status':'precondition_failed',
            'error_code':'layout_not_ready', 'request_sent':False,
            'problems':checks.get('problems', []),
            'next_action':'先按实际布局问题合并修复并render，再审受影响页；'
                '如确需读有布局错误的草稿，显式review_layout_draft=true，仍不能交付。'},
            ensure_ascii=False), is_error=True, metadata={'request_sent':False,
                'render_executed':False,'failure_stage':'layout_precondition'})
    return None


class GeoMapReviewTool(BaseTool):
    name = "geo_map_review"
    description = (
        (
        '只读取真实地图图片并找出可见问题：文字难读、重叠、遮挡、裁切、留白与图例'
        '不符。返回status/summary/issues/limitatio'
        'ns和绑定图片的报告；不修改地图。all_pages=true核对全册，未变页有效报告'
        '直接复用，仅无有效证据页发给图片模型。page_checks按真实page_id持久修改验收要求。服务失败保持unve'
        'rified；先读request_sent和diagnostics再决定'
        '恢复，缓存/冷却命中不算新服务请求。'
    )
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
        """Review only pages lacking reusable evidence for the current package.

        ``all_pages`` means "account for every page", not "send every page again".
        Passing pixels plus a completed page/review configuration is bound by the
        ledger and by the review cache. We therefore resolve the current package
        before making any service request, reuse recorded or cached conclusions,
        and send only pages whose current evidence cannot be reused.
        """
        from geoharness.cartography.jobs import map_job_receipt
        from geoharness.qgis.artifacts import returned_page_previews

        config = context.metadata["geoharness_config"]
        path = Path(arguments.file_path).expanduser()
        if not path.is_absolute():
            path = context.cwd / path
        path = path.resolve()
        if not path.is_file():
            return ToolResult(output="Error: an existing PNG/JPEG/WebP map image is required.",
                              is_error=True)
        try:
            from geoharness.cartography.tracking import bind_map_job

            identity = bind_map_job(context, arguments.map_id, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                      map_id=identity, artifact_path=path)
            verified = _verified_render_evidence(path, digest, receipt,
                                                  [context.cwd.resolve()])
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Batch review unavailable ({type(exc).__name__}): {exc}",
                              is_error=True, metadata={"render_executed": False})
        proof = verified.get("backend_proof", {})
        blocked = _layout_precondition(verified, arguments)
        if blocked:
            return blocked
        if verified.get("backend") not in {"qgis", "matplotlib"} or not proof.get(
                "manifest_path"):
            return ToolResult(output="Batch unavailable: no verified map package.",
                              is_error=True, metadata={"map_id": identity,
                                                       "render_executed": False})
        root = Path(proof["manifest_path"]).parent
        entries = returned_page_previews(verified.get("render_metadata", {}))
        if not entries:
            return ToolResult(output="Batch unavailable: the package declares no pages.",
                              is_error=True, metadata={"map_id": identity,
                                                       "render_executed": False})
        pages = []
        for entry in entries:
            locator = entry.get("path")
            if not isinstance(locator, str) or not locator:
                raise ValueError("Verified package contains a page without a path")
            page_path = (root / locator).resolve()
            page_index = entry.get("page_index", 0)
            if type(page_index) is not int or page_index < 0:
                raise ValueError("Verified package contains an invalid page index")
            if not page_path.is_relative_to(root) or not page_path.is_file():
                raise ValueError("Page path leaves the verified package")
            page_digest = hashlib.sha256(page_path.read_bytes()).hexdigest()
            if page_path != path and page_digest != digest:
                bound = _verified_render_evidence(page_path, page_digest, receipt,
                                                  [context.cwd.resolve()])
                if (bound.get("review_page_index") != page_index
                        or bound.get("backend") not in {"qgis", "matplotlib"}):
                    return ToolResult(
                        output="Batch stopped: a packaged page changed or is unbound.",
                        is_error=True, metadata={"map_id": identity,
                                                 "render_executed": False})
            pages.append((page_index, page_path, entry, page_digest))
        page_ids = {entry.get("page_id") or str(n) for n, _, entry, _ in pages}
        unknown = sorted(set(arguments.page_checks) - page_ids)
        if unknown:
            raise ValueError("page_checks引用未知页面：" + ", ".join(unknown))
        single = arguments.model_copy(update={"all_pages": False, "auto_repair": False})
        summary, results = [], {}
        # Entries are visited in declared order. Existing evidence is checked
        # before any provider call; a service outage still stops later pages
        # rather than walking all of them through the same cooldown.
        for page_index, page_path, entry, page_digest in pages:
            page_arguments = single.model_copy(update={
                "file_path": str(page_path), "map_id": identity})
            result = await self._review_one(page_arguments, context)
            results[page_index] = result
            if result.is_error:
                return result
            body, _ = json.JSONDecoder().raw_decode(result.output.lstrip())
            diagnostics = body.get("diagnostics", {})
            if diagnostics.get("failure_stage"):
                result.metadata['completed_page_count'] = len(summary) + 1
                result.metadata['verified_page_count'] = len(summary)
                result.metadata['batch_status'] = 'unverified'
                result.metadata['partial_pages'] = summary
                result.metadata['pending_pages'] = [n for n, _, _, _ in pages
                                                     if n not in {p['page_index']
                                                                  for p in summary}]
                return result
            issues = body.get("issues", [])
            status = ("issues" if any(i.get("severity") in {"warning", "error"}
                                      for i in issues) else
                      diagnostics.get("page_visual_status") or
                      result.metadata.get("visual_review_status",
                                          result.metadata.get("review_status", "unverified")))
            summary.append({"page_index": page_index, "status": status,
                "image_path": str(page_path),
                "review_binding": result.metadata.get("review_binding", {}),
                "report_path": result.metadata.get("review_path"), "issue_count": len(issues),
                "issues": issues[:5], "issues_truncated": len(issues) > 5,
                "page_role": diagnostics.get('page_role', entry.get('role', 'unknown')),
                "page_id": entry.get("page_id") or str(page_index),
                "review_outcome": ("reused" if result.metadata.get("review_cached")
                    else diagnostics.get('review_outcome',
                        'service_or_contract_failure' if diagnostics.get('failure_stage')
                        else 'model_unable_to_assess' if status == 'unverified'
                        else 'completed')),
                "repair_progress": diagnostics.get('repair_progress', {}),
                "request_sent": result.metadata.get("request_sent", False),
                "registration_status": result.metadata.get('registration_status'),
                "reused_reason": ("cached_pixel_conclusion"
                                  if result.metadata.get("review_cached") else None)})
        summary.sort(key=lambda page: page["page_index"])
        expected = {item[0] for item in pages} | set(_unreviewed_pages(
            verified.get('render_metadata') or {}))
        missing = sorted(expected - {item["page_index"] for item in summary})
        statuses = [item["status"] for item in summary]
        status = ("unverified" if missing or "unverified" in statuses else
                  "issues" if "issues" in statuses else "passed")
        payload = {"status": status, "map_id": identity, "pages": summary,
                   "unreviewed_page_indices": missing,
                   "reviewed_pages": [item["page_index"] for item in summary
                                      if item["request_sent"]],
                   "reused_pages": [item["page_index"] for item in summary
                                    if not item["request_sent"]],
                   "pending_pages": sorted(set(missing) | {
                       item['page_index'] for item in summary if item['status'] == 'unverified'}),
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
        payload['diagnostics'] = {'repair_progress': pending,
            'request_count': len(payload['reviewed_pages']),
            'reused_count': len(payload['reused_pages']),
            'page_contracts_changed': [item["page_index"] for item in summary
                                        if item.get("reused_reason") == "page_contract_changed"]}
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  map_id=identity, artifact_path=path)
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
        from geoharness.cartography.tracking import bind_map_job, receipt_summary

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
            # Reviews observe registered renders. Never manufacture a map
            # version from a bare image; that was the source of pseudo-versions
            # whose "main map" was actually a roster preview.
            registered = bool(page_evidence.get("review_binding")) or any(
                version.get("image_sha256") == digest
                for version in existing.get("versions", []) if isinstance(version, dict))
        except (OSError, ValueError) as exc:
            return await self._review_without_ledger(arguments, context, path, str(exc))
        evidence = dict(arguments.evidence)
        verified = _verified_render_evidence(path, digest, existing, roots)
        blocked = _layout_precondition(verified, arguments)
        if blocked:
            return blocked
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
            from geoharness.cartography.review_schedule import (
                page_checks,
                page_key,
                reusable_review,
                update_requirements,
            )

            try:
                requirements = update_requirements(context, identity, evidence,
                    arguments.page_checks, allow_change=arguments.update_page_requirements)
            except ValueError as exc:
                return ToolResult(output=str(exc), is_error=True,
                                  metadata={'request_sent': False})
            existing = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                       map_id=identity, artifact_path=path)
            key = page_key(evidence)
            evidence['page_acceptance_requirements'] = requirements.get(key, [])
            evidence['review_checks'] = page_checks(evidence, requirements.get(key, []))
            reused = reusable_review(path, digest, existing, evidence, config,
                                     evidence['review_checks'])
            if reused:
                saved, report = reused
                payload = review_execution_payload(public_payload(saved))
                # Return this version's binding while retaining the original report.
                payload.setdefault('diagnostics', {})['review_binding'] = dict(
                    evidence['review_binding'])
                return ToolResult(output=json.dumps(payload, ensure_ascii=False), metadata={
                    'review_status': saved['status'], 'visual_review_status': saved['status'],
                    'review_cached': True, 'request_sent': False, 'render_executed': False,
                    'review_binding': evidence['review_binding'], 'page_id': key,
                    'review_page_index': evidence['review_binding'].get('page_index', 0),
                    'review_path': str(report), 'image_path': str(path),
                    'image_sha256': digest, 'registration_status': 'reused',
                    'pixel_sha256': saved.get('pixel_sha256', ''),
                    'reused_reason': 'verified_page_observation', 'map_id': identity,
                    'map_receipt': existing,
                    'review_checks': evidence['review_checks']})
        else:
            if arguments.page_checks:
                return ToolResult(output='page_checks需要已验证的页面清单；'
                                  '先用实际page_id和已登记图片。',
                                  is_error=True, metadata={'request_sent': False})
            evidence.pop("render_metadata", None)
            evidence.pop("backend_proof", None)
        targets = []
        if registered:
            from geoharness.cartography.review_contract import stable_contract
            from geoharness.cartography.review_findings import (
                pending_page_targets,
                sync_review_pages,
            )

            sync_review_pages(context.metadata, cwd=context.cwd, map_id=identity,
                pages=(verified.get('render_metadata') or {}).get('page_previews', []),
                version=verified.get('review_binding', {}).get('version'), receipt=existing)

            targets = pending_page_targets(context.metadata, cwd=context.cwd, map_id=identity,
                page_index=verified.get('review_page_index', 0),
                page_id=verified.get('review_binding', {}).get('page_id') or '',
                page_contract=stable_contract(evidence))
            evidence['review_targets'] = [{'issue_id': item['issue_id'], 'issue': item['issue']}
                                          for item in targets[:64]]
        # Geometry checks belong to rendering; they never substitute for image review.
        result, budget = await review_pixels(path, arguments=arguments, context=context,
            identity=identity, evidence=evidence)
        # Persist the exact page/review question with the conclusion. A later
        # batch may reuse the pixels only if the page duty and checks still match.
        from geoharness.cartography.review_contract import stable_contract

        page_index = verified.get("review_page_index", 0)
        result.diagnostics["page_contract"] = stable_contract({
            **evidence,
            "review_checks": evidence.get("review_checks", []),
            "render_metadata": {**(evidence.get("render_metadata") or {}),
                                 "review_page_index": page_index},
        })
        from geoharness.cartography.review_progress import assess_repair_progress

        result.diagnostics['repair_progress'] = assess_repair_progress(
            evidence.get('previous_visual_reviews', []),
            {**result.model_dump(), 'image_sha256': digest,
             'page_index': verified.get('review_page_index', 0)})
        if registered:
            from geoharness.cartography.review_findings import observe_findings

            result.diagnostics['finding_registry'] = observe_findings(context.metadata,
                cwd=context.cwd, map_id=identity, page_index=verified.get('review_page_index', 0),
                image_sha256=digest, pixel_sha256=budget.get('pixel_sha256', ''),
                page_id=verified.get('review_binding', {}).get('page_id') or '',
                page_contract=result.diagnostics['page_contract'],
                review=result.model_dump())
        # Cached conclusions predate newer layouts too; page-zero pixels cannot accept page one.
        page_index = verified.get("review_page_index", 0)
        unreviewed = [page for page in _unreviewed_pages(verified.get("render_metadata") or {})
                      if page not in verified.get("reviewed_page_indices", [])]
        current_binding = verified.get("review_binding")
        if current_binding and result.diagnostics.get('failure_stage'):
            # Failed service attempts still belong to a concrete page/version.
            # Recording unverified evidence grants no visual approval.
            result.diagnostics['review_binding'] = dict(current_binding)
        if budget.get("review_cached") and current_binding:
            if (result.status in {'passed', 'issues'}
                    and not result.diagnostics.get('failure_stage')):
                result.diagnostics.setdefault('source_review_binding',
                                              result.diagnostics.get('review_binding', {}))
                result.diagnostics['review_binding'] = dict(current_binding)
                result.diagnostics['pixel_conclusion_rebound_to_current_package'] = True
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
        # This result describes one page. The ledger aggregates whole-book acceptance.
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
        if not registered:
            # The report is retained for inspection, but an unregistered image
            # cannot acquire a ledger version or authorize formal delivery.
            payload = public_payload(result.model_dump())
            budget["visual_review_status"] = result.status
            return ToolResult(
                output=(json.dumps(review_execution_payload(payload), ensure_ascii=False,
                                   indent=2)
                        + "\nReport: " + str(report)
                        + "\nMap receipt: unregistered; this image is not bound to a "
                          "rendered ledger version and cannot be used for formal delivery."),
                metadata={"image_path": str(path), "review_path": str(report),
                          "review_status": result.status, "quality_status": "draft",
                          "backend": verified['backend'],
                          "registration_status": "unregistered", "render_executed": False,
                          **budget},
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
                metadata={"review_path": str(report), 'execution_status': 'completed',
                          'registration_status': 'failed', 'review_status': result.status,
                          'render_executed': False},
            )
        expected_version = verified.get('review_binding', {}).get(
            'version', receipt.get('selected_version'))
        selected = next((v for v in receipt.get('versions', [])
                         if v.get('version') == expected_version), {})
        stored = selected.get('page_reviews', {}).get(str(page_index), {})
        if not stored and page_index == 0:
            stored = selected.get('stages', {}).get('visual_checked', {})
        reference = stored.get('report_reference') or {}
        if (stored.get('image_sha256') != digest or reference.get('path') != str(report)
                or reference.get('sha256') != hashlib.sha256(report.read_bytes()).hexdigest()):
            return ToolResult(output=f'Review retained at {report}; page evidence was not '
                'committed to its map version. Inspect the ledger before delivery.', is_error=True,
                metadata={'review_path': str(report), 'execution_status': 'completed',
                          'registration_status': 'failed', 'review_status': result.status,
                          'render_executed': False})
        from geoharness.cartography.review_findings import apply_targeted_assessments

        targeted = apply_targeted_assessments(context.metadata, cwd=context.cwd,
            map_id=identity, page_index=page_index, image_path=path, image_sha256=digest,
            pixel_sha256=budget.get('pixel_sha256', ''), review=result.model_dump(),
            targets=targets[:64], report_path=report)
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
                'execution_status': 'completed', 'registration_status': 'committed',
                "review_status": result.status,
                "render_executed": False,
                "review_page_index": page_index,
                "review_binding": result.diagnostics.get("review_binding", {}),
                "backend": verified["backend"],
                "review_path": str(report),
                "image_path": str(path),
                "image_sha256": digest,
                "finding_registry": result.diagnostics.get("finding_registry", {}),
                "targeted_recheck": targeted,
                "issues": [issue.model_dump() for issue in result.issues],
                "review_checks": evidence.get('review_checks', []),
                "map_id": identity,
                "map_receipt": receipt,
                **budget,
            }),
        )
