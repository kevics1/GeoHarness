"""Small native cartography interface; QGIS is a final delivery adapter only."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from openharness.tools.base import BaseTool, ToolResult
from pydantic import Field, model_validator

from geoharness.cartography.scene import Contract, Edit, Scene, edit_scene, load_scene, save_scene


class NativeMapInput(Contract):
    action: Literal['spec', 'render', 'inspect', 'help'] = Field(
        description=('spec保存计划不渲染；render应用edits并原生制图；inspect只读取已有计划；'
                     'help返回字段说明。普通制图必须用本工具，不得调用geo_qgis_project。'))
    scene: Scene | None = Field(default=None,
        description=('首次保存/渲染的完整计划，与scene_ref必须二选一。纸张/图框/符号直径用mm，'
                     '字号用pt，页面extent用Scene.crs的显示坐标。'))
    scene_ref: str = Field(default='', description=(
        '工具返回的map-scene:引用，后续修改只需它加edits；不要重复传完整scene。'))
    edits: list[Edit] = Field(default_factory=list, max_length=32,
        description=('只按返回的稳定Layer.id/Page.id合并变化字段；数组整体替换。'
                     '应用后重新校验和渲染，不直接写QGIS工程。'))

    @model_validator(mode='before')
    @classmethod
    def legacy_static(cls, value):
        """Translate file-backed legacy calls without invoking the old QGIS bridge."""
        if not isinstance(value, dict) or value.get('action') not in {'full', 'export'}:
            return value
        if value.get('qgis_layout'):
            raise ValueError('旧qgis_layout不能混入原生计划；使用scene.pages声明图框和名册。')
        sources = value.get('layers') or [{'file_path': value.get('file_path') or
            value.get('source_name'), 'label': value.get('layer_name', '')}]
        if isinstance(sources, str):
            sources = json.loads(sources)
        layers = []
        for index, source in enumerate(sources):
            path = source.get('file_path') or source.get('source_name')
            if not path or source.get('source_type', 'file') not in {'', 'file'}:
                raise ValueError('旧调用先加载为真实文件/data:引用，再使用scene原生制图。')
            options = source.get('style') or source
            if options.get('render_method') == 'graduated' or source.get('derivation'):
                raise ValueError('旧分级/派生调用请用scene的真实分类或select_keys。')
            categories = []
            raw, colors = options.get('categories') or [], options.get('colors') or []
            for ci, category in enumerate(raw):
                if isinstance(category, dict):
                    categories.append(category)
                else:
                    color = colors.get(str(category)) if isinstance(colors, dict) else (
                        colors[ci] if len(colors) > ci else None)
                    if not color:
                        raise ValueError('旧分类缺少颜色，请使用scene.layers.symbol.categories。')
                    categories.append({'value': category, 'label': str(category), 'color': color})
            layers.append({'id': f'layer-{index}', 'source_ref': path,
                'name': source.get('label') or source.get('source_name') or f'图层{index + 1}',
                'symbol': {'color': options.get('color') or '#4186b5',
                    'field': options.get('column') or options.get('field_name') or '',
                    'categories': categories}})
        crs = value.get('map_crs')
        if not crs:
            raise ValueError('需明确scene.crs或兼容参数map_crs，不猜显示坐标系。')
        layout = value.get('layout') or {}
        return {'action': 'render', 'scene': {'title': value.get('title') or
            value.get('layer_name') or '', 'map_id': value.get('map_id') or '',
            'crs': crs, 'layers': layers, 'pages': [{'id': 'main',
                                                  'extent': value.get('map_extent')}],
            'width_mm': layout.get('width', 297 / 25.4) * 25.4,
            'height_mm': layout.get('height', 210 / 25.4) * 25.4,
            'dpi': layout.get('dpi', 150), 'analysis_refs': value.get('analysis_manifests', []),
            'extra_formats': [value['output_format']] if value.get('output_format') in {
                'html', 'svg'} else []}}

    @model_validator(mode='after')
    def source(self):
        if self.action not in {'help'} and bool(self.scene) == bool(self.scene_ref):
            raise ValueError('scene与scene_ref必须且只能提供一个')
        return self


class GeoNativeMapTool(BaseTool):
    name = 'geo_cartography'
    description = (
        '非QGIS原生制图。数据审查/空间分析后，用scene保存一次页面与真实图层计划，'
        '以后render(scene_ref,edits)合并修图。仅输出PNG/PDF/页面清单，不生成工程。'
        '返回实际页面路径、稳定ID、字段/布局诊断；geo_map_review(all_pages=true)只读审图；'
        '全部必要页面通过后geo_deliver(project_dir)才一次生成可编辑QGIS工程。'
        '图例categories使用实际字段值；名册自动续页。不会通过移动真实对象修复标注。'
        '不清楚业务含义时问用户；不要调用geo_qgis_project反复构建。'
    )
    input_model = NativeMapInput

    async def execute(self, arguments, context):
        from geoharness.cartography.jobs import ensure_map_job, map_job_receipt, record_map_version
        from geoharness.cartography.mutation_control import map_mutation
        from geoharness.cartography.scene_render import render_scene
        from geoharness.cartography.tracking import receipt_summary

        try:
            if arguments.action == 'help':
                return ToolResult(output=json.dumps({
                    'purpose': '原生Matplotlib多页制图；不启动QGIS、不生成.qgs/.qgz。',
                    'units': '纸张与图框mm，符号直径/线宽mm，字号pt，extent使用screen CRS。',
                    'actions': {
                        'spec': '只保存并校验scene，不渲染；返回scene_ref。',
                        'render': '保存、渲染并登记页面；首次传scene，后续传scene_ref+edits。',
                        'inspect': '读取已有scene/台账，不重新渲染。',
                        'help': '本说明。'},
                    'minimal_example': {'action': 'render', 'scene': {
                        'title': '实际分析标题', 'crs': 'EPSG:32647',
                        'layers': [{'id': 'result', 'source_ref': '实际data:引用或路径',
                            'key': '唯一实体ID', 'label_field': '实际标注字段',
                            'symbol': {'size_mm': 2.0, 'color': '#d73027'}}],
                        'pages': [{'id': 'main', 'role': 'map', 'layers': ['result']}]}},
                    'edit_example': {'action': 'render', 'scene_ref': '实际map-scene:引用',
                        'edits': [{'target': 'layer', 'id': 'result',
                                   'changes': {'symbol': {'size_mm': 1.2}}}]},
                    'fields': {
                        'Layer.id': '稳定图层ID，页面和edits引用它，不是文件名。',
                        'Layer.source_ref': '实际工具返回的data:引用或可读文件路径。',
                        'Layer.key': '唯一非空实体字段；select_keys/标注时必须提供。',
                        'Layer.select_keys': '按key选择对象；null保留全部，空数组为空集。',
                        'Layer.label_field': '真实字段；只避让文字，不移动几何。',
                        'Layer.symbol.categories': '按真实分类值声明label/color；未声明值会拒绝。',
                        'Page.layers': '从底到顶的Layer.id；不填使用全部。',
                        'Page.extent': 'xmin,ymin,xmax,ymax；与focus_layers二选一。',
                        'Page.table': '名册定义layer/fields/headers/sort_by；自动续页不截断。'},
                    'common_errors': [
                        '页面引用了不存在的Layer.id或table.layer。',
                        'extent与focus_layers同时提供；二者必须二选一。',
                        '分类值未声明、key不唯一/为空、标注字段不存在。',
                        '把纸张英寸当mm，或把QGIS符号面积当Matplotlib点数。',
                        '重复传完整scene而不是scene_ref+edits，导致页面文件漂移。'],
                    'workflow': '数据审查→分析/结果验证→geo_cartography render→'
                        '处理layout确定缺陷→geo_map_review(all_pages=true)→全部通过且问题'
                        '处置后geo_deliver一次生成可编辑工程。',
                    'no_qgis': '普通制图、修图、预览、审图阶段不要调用geo_qgis_project。',
                }, ensure_ascii=False))

            if arguments.action == 'inspect' and not arguments.scene_ref:
                raise ValueError('inspect需要已有scene_ref；不要以inspect创建新地图')
            if arguments.action == 'inspect' and arguments.edits:
                raise ValueError('inspect只读，不能携带edits；修改请用spec或render')
            scene = arguments.scene or load_scene(arguments.scene_ref, context)
            scene = edit_scene(scene, arguments.edits)
            config = context.metadata['geoharness_config']
            if arguments.action == 'inspect':
                identity, reference = scene.map_id, arguments.scene_ref
            else:
                identity = ensure_map_job(context.metadata, cwd=context.cwd, config=config,
                                          map_id=scene.map_id)
                scene = scene.model_copy(update={'map_id': identity})
                reference = save_scene(scene, context)
            if arguments.action == 'inspect':
                receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                          map_id=identity)
                status = receipt.get('stages', {})
                return ToolResult(output=json.dumps({'status': 'scene_loaded',
                    'scene_ref': reference, 'map_id': identity,
                    'scene': scene.model_dump(mode='json'),
                    'stages': {name: status.get(name, {}).get('status', 'unverified')
                               for name in ('data_checked', 'rendered', 'layout_checked',
                                            'visual_checked', 'packaged', 'delivered')},
                    'selected_version': receipt.get('selected_version'),
                    'receipt': receipt_summary(receipt),
                    'inspect_call': {'action': 'inspect', 'scene_ref': reference},
                    'render_call': {'action': 'render', 'scene_ref': reference}},
                    ensure_ascii=False), metadata={'scene_ref': reference, 'map_id': identity,
                                                   'map_receipt': receipt})
            if arguments.action == 'spec':
                return ToolResult(output=json.dumps({'status': 'scene_saved',
                    'scene_ref': reference, 'map_id': identity,
                    'scene': scene.model_dump(mode='json'),
                    'render_call': {'action': 'render', 'scene_ref': reference}},
                    ensure_ascii=False), metadata={'scene_ref': reference, 'map_id': identity})
            async with map_mutation(context, map_id=identity, operation='native_scene_render'):
                # Cancellation drains the thread before releasing the mutation lease.
                pending = asyncio.create_task(asyncio.to_thread(render_scene, scene, reference,
                                                                 context))
                try:
                    result = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    await pending
                    raise
                from pathlib import Path

                receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                          artifact_path=Path(result['preview_path']))
                # Rendering and ledger registration are separate operations. A
                # cached image from an interrupted write still needs registration.
                if not result.get('reused') or not receipt.get('artifact_registered'):
                    record_map_version(identity, context.metadata, cwd=context.cwd, config=config,
                        image_path=Path(result['preview_path']),
                        project_dir=Path(result['project_dir']), layout=result['layout'],
                        preflight={'status': 'passed', 'scope': 'map_data_preflight',
                            'display_crs': scene.crs, 'map_extent': result['extent'],
                            'layers': [v.model_dump() for v in scene.layers],
                            'checks': {'sources': 'passed', 'crs': 'passed'}},
                        analysis=result['analysis'])
                receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config,
                                          artifact_path=Path(result['preview_path']))
            problems = result.get('layout', {}).get('bounds_check', {}).get('problems', [])
            visible = {key: result[key] for key in ('scene_ref', 'project_dir', 'preview_path',
                'output_path', 'reused', 'render_executed', 'layout', 'native_facts')}
            page_reviews = receipt.get('stages', {}).get('visual_checked', {}).get(
                'page_reviews', [])
            visible.update(map_id=identity, receipt=receipt_summary(receipt),
                page_paths=[page['path'] for page in result['native_facts']['page_previews']],
                page_review_status={str(item.get('page_index')): item.get('status')
                                    for item in page_reviews},
                layout_problem_count=len(problems),
                next_action=('先修复layout确定缺陷并重新render，再对全部页面执行'
                    'geo_map_review(file_path=preview_path,all_pages=true)；不要生成QGIS工程。'
                    if problems else
                    '对全部页面执行geo_map_review(file_path=preview_path,all_pages=true)；'
                    '不要生成QGIS工程。'))
            return ToolResult(output=json.dumps(visible, ensure_ascii=False), metadata={
                **result, 'map_id': identity, 'map_receipt': receipt})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(output=json.dumps({'status': 'failed', 'error_summary': str(exc),
                'error_code': 'native_scene_contract',
                'next_action': '只修正诊断字段或引用；保留已成功结果，不生成QGIS工程。'
                '不清楚业务含义时询问用户。'}, ensure_ascii=False), is_error=True,
                metadata={'render_executed': False})

    def is_read_only(self, arguments):
        return arguments.action in {'inspect', 'help'}
