"""Freeze an exact preview and materialize its editable project once at delivery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


async def materialize_project(context, spec_ref='', *, preview_dir=''):
    from openharness.engine.query import (
        _extract_permission_command,
        _resolve_permission_file_path,
    )
    from openharness.hooks import HookEvent
    from openharness.utils.fs import atomic_write_text

    from geoharness.cartography.jobs import map_job_receipt, record_map_materialization
    from geoharness.cartography.mutation_control import map_mutation
    from geoharness.performance import MeasuredTool
    from geoharness.runtime import _build_permission_checker
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    tool = GeoQgisProjectTool()
    config = context.metadata['geoharness_config']
    checker = context.metadata.get('geoharness_permission_checker') or (
        _build_permission_checker(config))

    def authorize(arguments):
        values = arguments.model_dump(mode='json', exclude_unset=True)
        decision = checker.evaluate(tool.name, is_read_only=False,
            file_path=_resolve_permission_file_path(context.cwd, values, arguments),
            command=_extract_permission_command(values, arguments))
        if not decision.allowed or decision.requires_confirmation:
            raise ValueError('当前权限未授权交付时生成QGIS工程')
        return values

    async def invoke(arguments, *, materialize=False):
        values = authorize(arguments)
        if context.hook_executor:
            hooks = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE, {
                'tool_name': tool.name, 'tool_input': values,
                'event': HookEvent.PRE_TOOL_USE.value})
            if hooks.blocked:
                raise ValueError(hooks.reason or '工程创建被PRE_TOOL_USE拒绝')

        class DeliveryInvocation:
            name = tool.name

            async def execute(self, args, ctx):
                return await tool.execute(args, ctx, _materialize=materialize)

        result = await MeasuredTool(DeliveryInvocation(), context.metadata,
                                    internal_dispatch=True).execute(arguments, context)
        if context.hook_executor:
            await context.hook_executor.execute(HookEvent.POST_TOOL_USE, {
                'tool_name': tool.name, 'tool_input': values, 'tool_output': result.output,
                'tool_is_error': result.is_error, 'event': HookEvent.POST_TOOL_USE.value})
        if result.is_error:
            raise ValueError('project_build_failed: ' + result.output[:2000])
        return result.metadata

    from geoharness.tools.geo_deliver import _source

    roots = [context.cwd.resolve()]
    for section in ('analysis', 'execution', 'cartography', 'qgis'):
        folder = Path(getattr(config, section).outputs_dir).expanduser()
        roots.append((folder if folder.is_absolute() else context.cwd / folder).resolve())
    if not preview_dir:
        from geoharness.qgis.specification import load_spec

        build = GeoQgisProjectInput(action='build', spec_ref=spec_ref)
        authorize(build)
        parameters = load_spec(spec_ref, cwd=context.cwd, settings=config.qgis)
        identity = parameters.get('map_id')
        if identity:
            previous = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                       map_id=identity)
            package = previous.get('stages', {}).get('packaged', {})
            candidate = package.get('manifest_path')
            if candidate and previous.get('editable_draft', {}).get('spec_ref') == spec_ref:
                descriptor = _source(Path(candidate), roots)
                if descriptor.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError('预览清单过大；检查已登记的原生预览')
                manifest = json.loads(descriptor.read_text(encoding='utf-8'))
                selected = next((v for v in previous.get('versions', [])
                    if v['version'] == previous.get('selected_version')), {})
                bound = (selected.get('materialization') or {}).get('spec_ref') or (
                    manifest.get('creation_spec') or {}).get('spec_ref')
                if bound == spec_ref:
                    preview_dir = str(descriptor.parent)
        if not preview_dir:
            preview = await invoke(build)
            preview_dir = preview.get('project_dir')
    if not preview_dir:
        raise ValueError('没有可冻结的预览；先build并使用实际返回的project_dir')
    root = Path(preview_dir).expanduser()
    root = (root if root.is_absolute() else context.cwd / root).resolve()
    descriptor = _source(root / 'project.json', roots)
    if descriptor.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('预览清单过大；检查已登记的原生预览')
    manifest = json.loads(descriptor.read_text(encoding='utf-8'))
    relative = Path(manifest['preview'])
    image = (root / relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not image.is_relative_to(root):
        raise ValueError('预览图片必须位于同一原生输出目录')
    image = _source(image, roots)
    receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                             artifact_path=image)
    if not receipt.get('artifact_registered') or not receipt.get('selected_version'):
        raise ValueError('交付只接受已登记且未变化的预览版本')
    package = receipt['stages']['packaged']
    selected = next(v for v in receipt['versions']
                    if v['version'] == receipt['selected_version'])
    frozen = (selected.get('materialization') or {})
    descriptor_digest = hashlib.sha256(descriptor.read_bytes()).hexdigest()
    current_manifest = package.get('manifest_path') == str(descriptor)
    frozen_manifest = (frozen.get('preview_manifest') == str(descriptor)
                       and frozen.get('preview_manifest_sha256') == descriptor_digest)
    if package.get('status') == 'stale' or not (current_manifest or frozen_manifest):
        raise ValueError('冻结预览清单已变化或不属于选定版本')
    if manifest.get('project_fidelity') != 'preview_only':
        if package.get('status') != 'passed' or package.get('project_dir') != str(root):
            raise ValueError('已有工程已变化；不能直接复用')
        return {'project_dir': str(root), 'map_id': receipt['job_id'], 'reused': True}
    request = manifest.get('materialization_request')
    if isinstance(request, dict) and request.get('action') == 'export':
        values = {**request['parameters'], 'action': 'export', 'map_id': receipt['job_id']}
    else:
        capsule = manifest.get('creation_spec') or {}
        if not isinstance(capsule.get('parameters'), dict):
            raise ValueError('冻结预览缺少可物化的创建契约；重新build并审查')
        values = {**capsule['parameters'], 'map_id': receipt['job_id'], 'action': 'create'}
    arguments = GeoQgisProjectInput.model_validate(values)
    # Reject changed sources before generating another native project. The
    # post-materialization binding below also detects changes during rendering.
    inventory = dict((manifest.get('creation_spec') or {}).get('source_inventory') or {})
    exported = manifest.get('export_binding') or {}
    if exported:
        inventory[exported['source_project']] = exported['source_project_sha256']
        for source in exported.get('sources', []):
            inventory.update({entry['path']: entry['sha256'] for entry in source['files']})
        inventory.update({entry['path']: entry['sha256'] for entry in exported.get('assets', [])})
    from geoharness.data.artifacts import sha256_file
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    guard = WorkerContext(context.cwd, context.cwd, qgis_read_roots(config, context.cwd))
    version = receipt['selected_version']
    journal = root / 'materialization.json'
    binding = {'version': version, 'image_sha256': receipt['image_sha256'],
        'manifest_sha256': descriptor_digest}
    async with map_mutation(context, map_id=receipt['job_id'], operation='materialize_delivery'):
        generated = None
        if journal.is_file():
            if journal.is_symlink() or journal.stat().st_size > 64 * 1024:
                raise ValueError('无效的工程物化记录；保留记录供检查')
            saved = json.loads(journal.read_text(encoding='utf-8'))
            if saved.get('binding') != binding:
                raise ValueError('工程物化记录与冻结版本不符；保留记录并检查版本')
            generated = saved.get('generated')
            if not generated:
                raise ValueError('上次工程物化尚无确定结果；先检查geo_task操作证据与原生运行记录，'
                                 '不要对同一冻结预览重复生成工程')
        if generated is None:
            for locator, expected in inventory.items():
                if sha256_file(guard.read_path(locator)) != expected:
                    raise ValueError('冻结预览来源已变化；重新预览和审查后交付：' + locator)
            from openharness.tools.base import ToolResult

            from geoharness.qgis.page_cache import remember_pages

            pages = [{**entry, 'path': str(root / entry['path'])}
                     for entry in manifest.get('native_facts', {}).get('page_previews', [])]
            remember_pages(context.metadata, ToolResult(output='', metadata={
                'native_facts': {'page_previews': pages}}), receipt['job_id'])
            # Persist intent before starting the worker. An interrupted unknown
            # operation must be investigated rather than creating another package.
            atomic_write_text(journal, json.dumps({'binding': binding,
                'status': 'running', 'generated': None}, ensure_ascii=False))
            generated = await invoke(arguments, materialize=True)
            generated = {key: generated[key] for key in (
                'project_dir', 'project_file', 'project_manifest', 'preview_path', 'map_id')
                if key in generated}
            atomic_write_text(journal, json.dumps({'binding': binding, 'status': 'succeeded',
                                                 'generated': generated}, ensure_ascii=False))
        current = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  artifact_path=image)
        if current['selected_version'] != version:
            raise ValueError('工程生成期间所选版本已变化；不能交付')
        package = current['stages']['packaged']
        if package.get('project_dir') != generated['project_dir']:
            record_map_materialization(receipt['job_id'], context.metadata,
                cwd=context.cwd, config=config, image_path=image,
                project_dir=Path(generated['project_dir']), selected_version=version)
        elif package.get('status') != 'passed':
            raise ValueError('已生成的工程文件发生变化；保留原工程供检查')
    return {**generated, 'map_id': receipt['job_id'], 'selected_version': version}
