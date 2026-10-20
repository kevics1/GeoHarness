"""A current, reviewed native scene grants one final editable-project export."""

from __future__ import annotations

import json
from pathlib import Path

from geoharness.cartography.scene import digest
from geoharness.cartography.scene_render import _sha


async def materialize_scene(context, preview_dir):
    from openharness.utils.fs import atomic_write_text

    from geoharness.cartography.jobs import map_job_receipt, record_map_materialization
    from geoharness.cartography.mutation_control import map_mutation
    from geoharness.cartography.review_findings import pending_findings
    from geoharness.qgis.runner import execute_qgis
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    config = context.metadata['geoharness_config']
    root = (context.cwd / preview_dir).resolve()
    guard = WorkerContext(context.cwd, context.cwd, qgis_read_roots(config, context.cwd))
    manifest_path = guard.read_path(str(root / 'project.json'))
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('原生页面清单过大')
    manifest = json.loads(manifest_path.read_text('utf-8'))
    image = guard.read_path(str(root / manifest['preview']))
    receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                              artifact_path=image)
    if not receipt.get('artifact_registered'):
        raise ValueError('原生页面尚未登记，不能生成工程')
    identity, version = receipt['job_id'], receipt['selected_version']
    async with map_mutation(context, map_id=identity, operation='native_scene_delivery'):
        # Read again under the mutation lease; model parameters do not grant approval.
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  artifact_path=image)
        if receipt['selected_version'] != version:
            raise ValueError('准备交付期间候选版本已变化')
        stages = receipt['stages']
        from geoharness.cartography.review_policy import delivery_authorization, state

        policy_delivery = delivery_authorization(context.metadata, config, receipt)
        required_stages = ('data_checked', 'rendered') if policy_delivery else (
            'data_checked', 'layout_checked', 'visual_checked')
        if any(stages.get(k, {}).get('status') != 'passed'
               for k in required_stages):
            raise ValueError('delivery_not_ready: 数据/布局/全部页面图片审查必须通过；'
                             '本次未启动QGIS、未生成工程')
        findings = pending_findings(context.metadata, cwd=context.cwd, map_ids={identity})
        if findings and not policy_delivery:
            raise ValueError('delivery_not_ready: 仍有待处置审图问题；用geo_task review_issues核实')
        plan_path = guard.read_path(str(root / 'scene-plan.json'))
        if _sha(plan_path) != manifest['scene_plan_sha256']:
            raise ValueError('已审制图计划发生变化')
        for name, expected in manifest.get('file_hashes', {}).items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or _sha(guard.read_path(str(path))) != expected:
                raise ValueError('已审页面文件发生变化')
        binding = {'map_id': identity, 'version': version, 'scene_plan_sha256': _sha(plan_path),
            'manifest_sha256': _sha(manifest_path), 'image_sha256': _sha(image)}
        candidate = digest(binding)
        journal = root / 'scene-materialization.json'
        if journal.is_file():
            if journal.is_symlink() or journal.stat().st_size > 64 * 1024:
                raise ValueError('无效最终工程记录')
            saved = json.loads(journal.read_text('utf-8'))
            if saved.get('candidate') != candidate:
                raise ValueError('最终工程记录不属于当前冻结候选')
            generated = saved.get('generated')
            if not generated:
                raise ValueError('上次工程生成已失败，先检查原生运行记录并修复后恢复；不能盲目重建'
                    if saved.get('status') == 'failed' else
                    '上次工程生成尚无确定结果；检查原生运行记录，不能盲目重建')
            final = Path(generated['project_dir'])
        else:
            if not config.qgis.enabled:
                raise ValueError('最终工程导出需要启用QGIS；已审原生地图和分析仍保留。')
            from geoharness.qgis.discovery import discover_qgis

            discover_qgis(config.qgis, cwd=context.cwd)
            plan = json.loads(plan_path.read_text('utf-8'))
            for locator, expected in plan['source_inventory'].items():
                if _sha(guard.read_path(locator)) != expected:
                    raise ValueError('分析来源已变化，冻结候选失效')
            from geoharness.runtime import _build_permission_checker

            checker = context.metadata.get('geoharness_permission_checker') or (
                _build_permission_checker(config))
            decision = checker.evaluate('geo_qgis_project', is_read_only=False,
                                         file_path=str(plan_path), command=None)
            if not decision.allowed or decision.requires_confirmation:
                raise ValueError('当前权限不允许最终工程导出')
            from openharness.hooks import HookEvent

            from geoharness.tools.geo_deliver import GeoDeliverInput

            # This is delivery's internal compilation, not an invocation of the
            # public QGIS inspection/build tool. Hooks receive the owning tool's
            # real input contract; worker-only fields stay outside tool_input.
            invocation = GeoDeliverInput(project_dir=str(root), map_id=identity).model_dump(
                mode='json', exclude_defaults=True)
            hook_payload = {'tool_name': 'geo_deliver', 'tool_input': invocation,
                            'internal_operation': 'native_scene_export', 'candidate': candidate}
            if context.hook_executor:
                outcome = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE,
                    {**hook_payload, 'event': HookEvent.PRE_TOOL_USE.value})
                if outcome.blocked:
                    raise ValueError('project_build_failed: ' + (
                        outcome.reason or '最终工程导出被钩子拒绝'))
            atomic_write_text(journal, json.dumps({'candidate': candidate, 'status': 'running',
                'generated': None}, ensure_ascii=False))
            result = await execute_qgis('native_scene_export', {'plan_path': str(plan_path),
                'plan_sha256': _sha(plan_path)}, cwd=context.cwd, settings=config.qgis,
                read_roots=[*qgis_read_roots(config, context.cwd), root, plan_path.parent,
                            Path(plan['font']['path']).parent])
            if result.get('status') != 'succeeded':
                atomic_write_text(journal, json.dumps({'candidate': candidate, 'status': 'failed',
                    'generated': None, 'worker_run_dir': result.get('run_dir'),
                    'error_summary': result.get('error_summary')}, ensure_ascii=False))
            if context.hook_executor:
                await context.hook_executor.execute(HookEvent.POST_TOOL_USE,
                    {**hook_payload,
                     'tool_output': json.dumps(result.get('result') or {
                         'error_summary': result.get('error_summary')}, ensure_ascii=False),
                     'tool_is_error': result.get('status') != 'succeeded',
                     'event': HookEvent.POST_TOOL_USE.value})
            if result.get('status') != 'succeeded':
                raise ValueError('project_build_failed: 最终工程编译失败，原生图片/分析保留：'
                                 + str(result.get('error_summary')))
            final = Path(result['result']['project_file']).parent
            # Native images remain authoritative. No falsely inherited visual
            # approval is attached to an independently rendered QGIS image.
            import shutil

            for name in manifest['files']:
                if name == 'project.json':
                    continue
                path = (root / name).resolve()
                target = (final / name).resolve()
                if not path.is_relative_to(root) or not target.is_relative_to(final):
                    raise ValueError('制图文件不能越过交付目录')
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            check = result['result']
            files = [str(p.relative_to(final).as_posix()) for p in final.rglob('*') if p.is_file()
                     and p.name not in {'.start', 'request.json', 'result.json', 'manifest.json',
                                        'stdout.txt', 'stderr.txt'} and '.profile' not in p.parts]
            document = {**manifest, 'backend': 'qgis', 'render_backend': 'matplotlib',
                'project_fidelity': 'scene_bound', 'project_file': 'map.qgz',
                'image_origin': 'reviewed_native_scene',
                'project_validation': check, 'native_layers': check['layers'],
                'files': sorted(set([*files, 'project.json']))}
            if policy_delivery:
                document.update(review_workflow='single_pass',
                    delivery_authorization='user_requested_one_review',
                    post_repair_visual_review='not_performed' if state(
                        context.metadata).get('repair_rendered') else 'no_repair',
                    image_origin='native_scene_single_pass_delivery')
            (final / 'project.json').write_text(json.dumps(document, ensure_ascii=False), 'utf-8')
            generated = {'project_dir': str(final), 'map_id': identity,
                'selected_version': version, 'candidate': candidate}
            atomic_write_text(journal, json.dumps({'candidate': candidate, 'status': 'succeeded',
                                                   'generated': generated}, ensure_ascii=False))
        current = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                  artifact_path=image)
        if current['stages']['packaged'].get('project_dir') != str(final):
            record_map_materialization(identity, context.metadata, cwd=context.cwd, config=config,
                image_path=image, project_dir=final, selected_version=version)
        elif current['stages']['packaged']['status'] != 'passed':
            raise ValueError('已生成工程发生变化，不能复用')
        return generated
