"""Execute validated native-tool dependency plans, retaining valid upstream work.

This controller owns execution/dependency validity only. Map quality belongs to
the map ledger, and a successful node never certifies scientific correctness.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from geoharness.data.artifacts import assert_usable_artifact, sha256_file
from geoharness.task_store import connect, execution_lock, task_directory

_TOOLS = Literal['geo_qgis', 'geo_python', 'geo_analysis', 'geo_accessibility',
                 'geo_cartography', 'geo_qgis_project', 'geo_map_review', 'geo_deliver',
                 'geo_task']


class TaskNode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$',
                    description='任务内稳定节点名，ASCII字母数字/下划线/短横线；修改同一步保留ID。')
    tool: _TOOLS = Field(description='实际执行工具；arguments必须符合该工具的字段契约。')
    arguments: dict[str, Any] = Field(default_factory=dict,
        description='目标工具的完整参数对象；引用前置结果使用{"$ref":"节点ID#/JSON/路径"}。')
    depends_on: list[str] = Field(default_factory=list, max_length=32,
        description='前置节点ID；可引用任务已有节点；每个$ref的节点须列入此处。')
    revision: int = Field(default=1, ge=1,
        description='实现/运行环境修正后可增加版本；不得仅为重试同一错误而递增。')


def execution_gate(context, tool, arguments, *, node_id=None):
    """Legacy entry points cannot mutate a frozen managed plan behind its back."""
    managed_score = tool.name == 'geo_task' and arguments.action in {'score', 'publish_analysis'}
    if node_id or (not managed_score and tool.name not in {
            'geo_python', 'geo_qgis', 'geo_analysis', 'geo_accessibility',
            'geo_cartography', 'geo_qgis_project', 'geo_deliver'}) or tool.is_read_only(arguments):
        return None
    directory = task_directory(context.metadata, cwd=Path(context.cwd))
    if directory is None or not (directory / 'task.sqlite3').is_file():
        return None
    with connect(directory) as db:
        active = bool(db.execute('SELECT 1 FROM nodes LIMIT 1').fetchone())
    if not active and not context.metadata['geoharness_config'].agent.managed_workflow:
        return None
    return {'managed_dispatch_required': True}


def _directory(context):
    directory = task_directory(context.metadata, cwd=Path(context.cwd))
    if directory is None:
        raise ValueError('先建立当前任务，再提交执行计划')
    return directory


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=1)
def _implementation_identity():
    root = Path(__file__).parent
    return _hash([(path.relative_to(root).as_posix(), sha256_file(path))
                  for path in sorted(root.rglob('*.py')) if '__pycache__' not in path.parts])


def _environment(node, context):
    """Bind only execution-relevant settings; persist their digest, never credentials."""
    config = context.metadata['geoharness_config']
    sections = {
        'geo_qgis': ('qgis',),
        'geo_python': ('qgis',) if node.arguments.get('backend') == 'qgis' else ('execution',),
        'geo_analysis': ('analysis', 'data'),
        'geo_accessibility': ('analysis', 'data'),
        'geo_cartography': ('cartography', 'data'),
        'geo_qgis_project': ('qgis', 'cartography'),
        'geo_map_review': ('map_review', 'model', 'base_url', 'api_key'),
        'geo_deliver': ('delivery', 'cartography', 'qgis'),
        'geo_task': ('analysis',),
    }[node.tool]
    values = {'implementation': _implementation_identity()}
    for name in sections:
        value = getattr(config, name)
        values[name] = (asdict(value) if is_dataclass(value) else
                        value.model_dump(mode='json') if hasattr(value, 'model_dump') else value)
    return _hash(values)


def _references(value):
    if isinstance(value, dict):
        if '$ref' in value:
            if set(value) != {'$ref'} or not isinstance(value['$ref'], str):
                raise ValueError('结果引用必须仅包含字符串$ref')
            identity, sep, pointer = value['$ref'].partition('#')
            if not identity or not sep or (pointer and not pointer.startswith('/')):
                raise ValueError('$ref格式为node_id#/result/outputs/OUTPUT（JSON Pointer）')
            yield identity
        else:
            for item in value.values():
                yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def submit_plan(context, nodes: list[TaskNode], *, replace=False, _return_status=True):
    with execution_lock(_directory(context)):
        return _submit_plan(context, nodes, replace=replace, _return_status=_return_status)


def _submit_plan(context, nodes: list[TaskNode], *, replace=False, _return_status=True):
    """Upsert by default; deleting omitted nodes requires explicit replacement."""
    directory = _directory(context)
    if not nodes or len(nodes) > 64:
        raise ValueError('计划必须包含1到64个有稳定ID的节点')
    definitions = {node.id: node for node in nodes}
    if len(definitions) != len(nodes):
        raise ValueError('计划节点ID不能重复')
    with connect(directory) as db:
        previous = {row['id']: dict(row) for row in db.execute('SELECT * FROM nodes')}
    if not replace:
        definitions = {**{key: TaskNode.model_validate_json(row['definition'])
                           for key, row in previous.items()}, **definitions}
    nodes = list(definitions.values())
    for node in nodes:
        if node.tool == 'geo_task' and node.arguments.get('action') not in {
                'publish_analysis', 'score'}:
            raise ValueError('计划中的geo_task只允许publish_analysis/score，禁止递归计划/执行')
        if set(node.depends_on) - definitions.keys() or node.id in node.depends_on:
            raise ValueError('依赖必须为当前任务已有或本次新增的其他节点；plan默认增量更新')
        if set(_references(node.arguments)) - set(node.depends_on):
            raise ValueError('每个$ref必须声明对应depends_on，禁止隐式依赖')
    visited, visiting = set(), set()

    def visit(identity):
        if identity in visiting:
            raise ValueError('计划存在循环依赖')
        if identity in visited:
            return
        visiting.add(identity)
        for dep in definitions[identity].depends_on:
            visit(dep)
        visiting.remove(identity)
        visited.add(identity)

    for identity in definitions:
        visit(identity)
    # Schemas are validated after references resolve, before any tool runs.
    from geoharness.task_store import _safe

    declared = [n.model_dump(mode='json') for n in nodes]
    if _safe(declared, context.metadata) != declared:
        raise ValueError('计划包含凭证；请使用配置引用')
    with connect(directory) as db:
        if db.execute("SELECT 1 FROM nodes WHERE status='running'").fetchone():
            raise ValueError('有运行中或未恢复的节点，先查看evidence，不能覆盖计划')
        changed = set(previous) ^ definitions.keys()
        for node in nodes:
            definition = json.dumps(node.model_dump(mode='json'), ensure_ascii=False,
                                    sort_keys=True)
            if previous.get(node.id, {}).get('definition') == definition:
                continue
            changed.add(node.id)
            db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?)', (
                node.id, definition, '', 'pending', None, None, None, '{}', None))
        for identity in previous.keys() - definitions.keys():
            db.execute('DELETE FROM nodes WHERE id=?', (identity,))
        # Invalidate transitive descendants even before a new input exists.
        while True:
            stale = {n.id for n in nodes if set(n.depends_on) & changed} - changed
            if not stale:
                break
            changed.update(stale)
        for identity in changed & definitions.keys():
            db.execute("UPDATE nodes SET status='pending',fingerprint='' WHERE id=?", (identity,))
    from geoharness.task_store import save_record

    save_record(context.metadata, cwd=context.cwd)
    return (plan_status(context) if _return_status else
            {'task_id': context.metadata['geo_task_record']['task_id'],
             'updated_nodes': sorted(changed)})


def _inventory(arguments, context):
    """Hash actual local input bytes, not temporary result directory names alone."""
    inventory = {}

    def walk(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items():
                if name != 'code':
                    walk(item, key + '/' + name)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, key + '/' + str(index))
        elif isinstance(value, str) and 0 < len(value) < 4096:
            from geoharness.data.task_context import resolve_registered_path

            if value.startswith('data:'):
                value = resolve_registered_path(value, context.metadata, cwd=Path(context.cwd),
                                                config=context.metadata['geoharness_config'])
            try:
                path = Path(value).expanduser()
                path = (path if path.is_absolute() else Path(context.cwd) / path).resolve()
                if path.is_file():
                    producer = assert_usable_artifact(path)
                    if producer and producer.get('manifest'):
                        manifest = Path(producer['manifest'])
                        inventory[key + '/producer'] = {
                            'path': str(manifest), 'sha256': sha256_file(manifest)}
                    inventory[key] = {'path': str(path), 'sha256': sha256_file(path)}
                    if path.suffix.lower() == '.shp':
                        for suffix in ('.dbf', '.shx', '.prj', '.cpg'):
                            side = path.with_suffix(suffix)
                            if side.is_file():
                                inventory[key + suffix] = {'path': str(side),
                                                          'sha256': sha256_file(side)}
            except (OSError, ValueError):
                # Exact registered refs are resolved by the tool. Never use a
                # failed producer as a reusable existing file.
                if key in inventory:
                    inventory.pop(key)

    walk(arguments)
    if arguments.get('scene_ref') or arguments.get('scene'):
        from geoharness.cartography.scene import Scene, load_scene
        from geoharness.cartography.scene_render import _font

        scene = (load_scene(arguments['scene_ref'], context) if arguments.get('scene_ref')
                 else Scene.model_validate(arguments['scene']))
        walk(scene.model_dump(mode='json'), '/scene')
        for layer in scene.layers:
            if layer.kind == 'raster':
                import rasterio

                from geoharness.data.task_context import resolve_registered_path

                path = resolve_registered_path(layer.source_ref, context.metadata,
                    cwd=context.cwd, config=context.metadata['geoharness_config'])
                with rasterio.open(path) as source:
                    for index, dependency in enumerate(source.files):
                        walk(dependency, f'/scene_raster/{layer.id}/{index}')
        _, font = _font(scene)
        walk(str(font), '/scene_font')
    if arguments.get('spec_ref'):
        from geoharness.qgis.specification import load_spec

        spec = load_spec(arguments['spec_ref'], cwd=context.cwd,
                         settings=context.metadata['geoharness_config'].qgis)
        walk(spec, '/spec')
    # Literal Python reads can be auto-bound by the legacy entry point. Include
    # those measured inputs in the controller fingerprint as well.
    if isinstance(arguments.get('code'), str) and arguments.get('backend', 'python') == 'python':
        from types import SimpleNamespace

        from geoharness.execution.managed_inputs import bind_managed_inputs

        class Inputs(SimpleNamespace):
            def model_copy(self, *, update):
                return Inputs(**{**vars(self), **update})

        try:
            bound = bind_managed_inputs(Inputs(code=arguments['code'],
                input_refs=arguments.get('input_refs', {}),
                input_requirements={}, stage_id=arguments.get('stage_id', ''),
                name=arguments.get('name', 'analysis')), context)
            walk(bound.input_refs, '/bound_inputs')
        except ValueError:
            if not arguments.get('input_refs'):
                raise
    return inventory


def _artifacts(result):
    files = {}

    def remember(value):
        if isinstance(value, str):
            path = Path(value)
            if path.is_absolute() and path.is_file():
                files[str(path.resolve())] = sha256_file(path)

    def walk(value, *, paths=False):
        if isinstance(value, dict):
            remember(value.get('path'))
            for name, item in value.items():
                if name in {'manifest_path', 'project_manifest', 'review_path',
                            'preview_path', 'project_path', 'package_path',
                            'report_path', 'result_path'}:
                    remember(item)
                elif paths or name in {'files', 'outputs', 'result', 'layers',
                                       'page_previews', '_metadata'}:
                    walk(item, paths=paths or name in {'files', 'outputs'})
        elif isinstance(value, list):
            for item in value:
                walk(item, paths=paths)
        elif paths:
            remember(value)

    walk(result)
    return files


def _valid_artifacts(files, *, digests=None):
    """Share digests only within this validation pass, never across executions."""
    digests = {} if digests is None else digests
    try:
        for path, expected in files.items():
            canonical = Path(path).resolve()
            if not canonical.is_file():
                return False
            if canonical not in digests:
                digests[canonical] = sha256_file(canonical)
            if digests[canonical] != expected:
                return False
        return bool(files)
    except OSError:
        return False


def plan_status(context, *, node_id=''):
    directory = _directory(context)
    with connect(directory) as db:
        rows = {row['id']: dict(row) for row in db.execute('SELECT * FROM nodes ORDER BY rowid')}
    if node_id:
        selected = set()
        def include(identity):
            if identity not in rows:
                raise ValueError('Unknown node_id')
            if identity not in selected:
                selected.add(identity)
                for dep in TaskNode.model_validate_json(rows[identity]['definition']).depends_on:
                    include(dep)
        include(node_id)
        rows = {identity: row for identity, row in rows.items() if identity in selected}
    summaries = []
    valid = set()
    checked = set()
    digests = {}

    def check(identity):
        if identity in checked:
            return identity in valid
        checked.add(identity)
        row = rows[identity]
        node = TaskNode.model_validate_json(row['definition'])
        if (row['status'] == 'succeeded' and all(check(dep) for dep in node.depends_on)
                and _valid_artifacts(json.loads(row['artifacts'] or '{}'), digests=digests)):
            try:
                dependencies = json.loads(row['dependencies'] or '{}')
                environment = _environment(node, context)
                if dependencies.get('environment') != environment:
                    return False
                captured = dependencies.get('inputs', {})
                if captured and not _valid_artifacts({v['path']: v['sha256']
                                                      for v in captured.values()}, digests=digests):
                    return False
                expected = _hash([node.model_dump(mode='json'), environment, captured,
                                  {d: rows[d]['fingerprint'] for d in node.depends_on}])
                if expected == row['fingerprint']:
                    valid.add(identity)
            except (ValueError, KeyError, OSError, TypeError):
                pass
        return identity in valid

    for identity in rows:
        check(identity)
    for identity, row in rows.items():
        node = TaskNode.model_validate_json(row['definition'])
        status = row['status']
        if status == 'succeeded' and identity not in valid:
            status = 'stale'
        if status == 'pending' and all(dep in valid for dep in node.depends_on):
            status = 'ready'
        summaries.append({'id': identity, 'tool': node.tool, 'status': status,
                          'depends_on': node.depends_on, 'operation_id': row['operation_id'],
                          'error': row['error']})
    return {'task_id': context.metadata['geo_task_record']['task_id'], 'nodes': summaries,
            'ready': [n['id'] for n in summaries if n['status'] == 'ready'],
            'complete': bool(rows) and len(valid) == len(rows),
            'validation_scope': 'target_and_dependencies' if node_id else 'full_plan',
            'quality': 'execution_dependencies_only; consult_map_ledger_and_analysis_packages'}


def _load_result(row, context):
    if not row.get('operation_id'):
        raise ValueError('前置节点没有可恢复的结果证据')
    from geoharness.task_store import committed_result

    operation, document = committed_result(context.metadata, cwd=context.cwd,
                                           operation_id=row['operation_id'])
    if operation['status'] != 'succeeded':
        raise ValueError('前置节点证据没有成功提交')
    try:
        result, _ = json.JSONDecoder().raw_decode(document['output'].lstrip())
        if not isinstance(result, dict):
            result = {'output': result}
    except (ValueError, TypeError):
        result = {'output': document['output']}
    # Several compatible tools append a human-readable receipt after their JSON.
    # Preserve that public JSON shape and expose exact registered metadata too.
    result['_metadata'] = document.get('metadata', {})
    return result


async def execute_direct(context, tool, arguments):
    """Preserve native tool calls while the system owns durable single-step dispatch."""
    from openharness.tools.base import ToolResult

    from geoharness.task_store import committed_result

    # Preserve true explicit-field identity across automatic dispatch. A full
    # model_dump() materializes every default and makes a frozen spec_ref/plan_ref
    # call look as if the caller supplied unrelated fields; revalidation then
    # rejects legitimate runs. Unset fields are reconstructed by the tool's
    # Pydantic defaults at execution time.
    values = arguments.model_dump(mode='json', exclude_unset=True)
    identity = 'auto_' + tool.name + '_' + _hash(values)[:16]
    node = TaskNode(id=identity, tool=tool.name, arguments=values)
    try:
        # Sibling tool calls from one assistant batch are legal sequential work.
        # Serialize them with an asyncio lock, not a synchronous wait inside the
        # event loop; submit_plan and run_plan must share that critical section.
        async with _in_process_task_lock(context):
            submit_plan(context, [node], _return_status=False)
            state = await run_plan(context, node_id=identity, _lock_held=True)
        selected = next(item for item in state['nodes'] if item['id'] == identity)
        if selected['status'] == 'interrupted' or not selected['operation_id']:
            raise ValueError(selected.get('error') or '节点尚未完成，先查看inspect/arguments证据')
        operation, document = committed_result(context.metadata, cwd=context.cwd,
                                               operation_id=selected['operation_id'])
        return ToolResult(output=document.get('output') or selected.get('error') or '',
            is_error=selected['status'] != 'succeeded', metadata={
                **document.get('metadata', {}), 'managed_node_id': identity,
                'managed_operation_id': operation['id'],
                'reused': identity in state['reused_nodes'],
            })
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return ToolResult(output=json.dumps({'status': 'precondition_failed',
            'code': 'managed_dispatch_failed', 'tool_executed': False,
            'error_summary': str(exc), 'next_action': '修正当前输入，保留已成功产物；'
            '用geo_task inspect查看节点，evidence查看具体失败。'}, ensure_ascii=False),
            is_error=True,
            metadata={'script_executed': False})


def _resolve(value, rows, directory, context):
    if isinstance(value, dict):
        if '$ref' in value:
            identity, _, pointer = value['$ref'].partition('#')
            result = _load_result(rows[identity], context)
            for part in pointer.split('/')[1:]:
                part = part.replace('~1', '/').replace('~0', '~')
                result = result[int(part)] if isinstance(result, list) else result[part]
            return result
        return {key: _resolve(item, rows, directory, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, rows, directory, context) for item in value]
    return value


def _in_process_task_lock(context):
    directory = _directory(context)
    locks = context.metadata.setdefault('_geo_task_locks', {})
    return locks.setdefault(str(directory), asyncio.Lock())

async def run_plan(context, *, node_id='', _lock_held=False):
    """Run a deterministic ready chain; stop on a real failure or pending decision."""
    directory = _directory(context)
    if _lock_held:
        return await _run_plan_locked(context, directory=directory, node_id=node_id)
    async with _in_process_task_lock(context):
        return await _run_plan_locked(context, directory=directory, node_id=node_id)

async def _run_plan_locked(context, *, directory, node_id=''):
    with execution_lock(directory):
        from geoharness.performance import MeasuredTool
        from geoharness.runtime import _build_permission_checker
        from geoharness.tools import build_geo_tool_registry

        registry = context.metadata.get('tool_registry')
        if registry is None:
            registry = build_geo_tool_registry(config=context.metadata['geoharness_config'])
        from geoharness.performance import MeasuredRegistry

        if isinstance(registry, MeasuredRegistry):
            registry = registry.registry
        checker = context.metadata.get('geoharness_permission_checker') or (
            _build_permission_checker(context.metadata['geoharness_config']))
        completed = []
        reused = [n['id'] for n in plan_status(context, node_id=node_id)['nodes']
                  if n['status'] == 'succeeded']
        while True:
            state = plan_status(context, node_id=node_id)
            statuses = {item['id']: item['status'] for item in state['nodes']}
            if node_id and node_id not in statuses:
                raise ValueError('Unknown node_id')
            if node_id and statuses[node_id] == 'succeeded':
                break
            ready = [item['id'] for item in state['nodes']
                     if item['status'] in {'ready', 'stale', 'failed', 'interrupted'}]
            if node_id:
                ready = [n for n in ready if n == node_id]
            if not ready or context.metadata.get('geo_clarification', {}).get('pending'):
                break
            with connect(directory) as db:
                rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM nodes')}
            selected = next((n for n in ready if all(statuses[d] == 'succeeded' for d in
                TaskNode.model_validate_json(rows[n]['definition']).depends_on)), None)
            if selected is None:
                break
            row = rows[selected]
            if (row['status'] == 'interrupted'
                    and 'process_outcome_unknown' in (row['error'] or '')):
                break  # Inspect the saved request; revise the plan deliberately.
            node = TaskNode.model_validate_json(row['definition'])
            deps = {d: rows[d]['fingerprint'] for d in node.depends_on}
            environment = _environment(node, context)
            try:
                arguments = _resolve(node.arguments, rows, directory, context)
                inventory = _inventory(arguments, context)
                fingerprint = _hash([node.model_dump(mode='json'), environment,
                                     inventory, deps])
                if (row['status'] in {'failed', 'interrupted'}
                        and row['fingerprint'] == fingerprint):
                    # No blind retry: change implementation/input/plan first.
                    break
                tool = registry.get(node.tool)
                if tool is None:
                    raise ValueError(f'{node.tool}当前未启用')
                parsed = tool.input_model.model_validate(arguments)
            except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
                from openharness.tools.base import ToolResult

                from geoharness.task_store import begin_operation, finish_operation

                fingerprint = _hash([node.model_dump(mode='json'), environment,
                                     'precondition', deps])
                if row['status'] == 'failed' and row['fingerprint'] == fingerprint:
                    break
                with connect(directory) as db:
                    db.execute("UPDATE nodes SET status='running',fingerprint=?,"
                               "dependencies=? WHERE id=?", (
                        fingerprint, json.dumps({'nodes': deps, 'inputs': {},
                                                 'environment': environment}), selected))
                handle = begin_operation(context.metadata, cwd=context.cwd, tool=node.tool,
                                         arguments=node.arguments, node_id=selected)
                evidence = finish_operation(handle, context.metadata,
                    result=ToolResult(output=str(exc), is_error=True),
                    error=type(exc).__name__)
                with connect(directory) as db:
                    db.execute("UPDATE nodes SET status='failed',operation_id=?,error=? "
                               'WHERE id=?',
                               (evidence['operation_id'], str(exc)[:1000], selected))
                break
            from openharness.engine.query import (
                _extract_permission_command,
                _resolve_permission_file_path,
            )
            from openharness.hooks import HookEvent

            if context.hook_executor:
                hooks = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE, {
                    'tool_name': node.tool, 'tool_input': arguments,
                    'event': HookEvent.PRE_TOOL_USE.value})
                if hooks.blocked:
                    raise ValueError(hooks.reason or f'PRE_TOOL_USE blocked {node.tool}')
            decision = checker.evaluate(node.tool, is_read_only=tool.is_read_only(parsed),
                file_path=_resolve_permission_file_path(context.cwd, arguments, parsed),
                command=_extract_permission_command(arguments, parsed))
            if not decision.allowed or decision.requires_confirmation:
                raise ValueError(f'当前权限未授权计划节点{selected}的{node.tool}调用')
            with connect(directory) as db:
                changed = db.execute("UPDATE nodes SET status='running',fingerprint=?,"
                    "dependencies=?,error=NULL WHERE id=? AND status!='running'", (
                        fingerprint, json.dumps({'nodes': deps, 'inputs': inventory,
                                                 'environment': environment}),
                        selected)).rowcount
                if changed != 1:
                    raise ValueError('节点已经运行；请读取证据，不能并发重发')
            try:
                result = await MeasuredTool(tool, context.metadata, node_id=selected).execute(
                    parsed, context)
                packet = {**result.metadata}
                try:
                    raw, _ = json.JSONDecoder().raw_decode(result.output.lstrip())
                    if isinstance(raw, dict):
                        packet.update(raw)
                except (ValueError, TypeError):
                    pass
                artifacts = _artifacts(packet)
                # The immutable execution evidence makes read-only nodes reusable.
                if result.metadata.get('evidence_path'):
                    artifacts[result.metadata['evidence_path']] = (
                        result.metadata['evidence_sha256'])
                incomplete_review = (node.tool == 'geo_map_review' and
                    (packet.get('status') == 'unverified' or
                     packet.get('visual_review_status') == 'unverified' or
                     packet.get('review_status') == 'unverified'))
                status = 'failed' if result.is_error or incomplete_review else 'succeeded'
                if _inventory(arguments, context) != inventory:
                    status = 'failed'
                    error = '执行期间输入发生变化，结果保留但不得复用'
                else:
                    error = ('视觉审查未完成；保留原图与证据，按服务恢复说明继续。'
                             if incomplete_review else
                             result.output[:1000] if result.is_error else None)
                with connect(directory) as db:
                    db.execute('UPDATE nodes SET status=?,operation_id=?,artifacts=?,'
                               'dependencies=?,error=? WHERE id=?',
                               (status, result.metadata.get('operation_id'),
                        json.dumps(artifacts), json.dumps({'nodes': deps, 'inputs': inventory,
                                                           'environment': environment}),
                        error, selected))
                if context.hook_executor:
                    await context.hook_executor.execute(HookEvent.POST_TOOL_USE, {
                        'tool_name': node.tool, 'tool_input': arguments,
                        'tool_output': result.output, 'tool_is_error': result.is_error,
                        'event': HookEvent.POST_TOOL_USE.value})
                if status != 'succeeded':
                    break
                completed.append(selected)
            except BaseException as exc:
                with connect(directory) as db:
                    db.execute('UPDATE nodes SET status=?,error=? WHERE id=?', (
                        'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                        type(exc).__name__, selected))
                raise
            if node_id:
                break
        state = plan_status(context, node_id=node_id)
        with connect(directory) as db:
            rows = {r['id']: dict(r) for r in db.execute('SELECT * FROM nodes')}
        for item in state['nodes']:
            if item['status'] in {'succeeded', 'failed'} and item['operation_id']:
                item['evidence_call'] = {'action': 'evidence',
                                         'operation_id': item['operation_id']}
                if item['status'] == 'succeeded' and (not node_id or item['id'] == node_id):
                    item['result'] = _visible_result(_load_result(rows[item['id']], context),
                        limit=context.metadata['geoharness_config'].agent.context_result_max_chars)
        chosen = next((item for item in reversed(state['nodes']) if item.get('result')
                       and (not node_id or item['id'] == node_id)), None)
        # The last/target business result must survive model-context trimming
        # even when a multi-node status listing is long.
        front = {'result_node': chosen['id'], 'result': chosen['result']} if chosen else {}
        return {**front, **state, 'executed': completed, 'reused_nodes': reused,
                'reused': not completed and bool(reused),
                'next_action': ('执行已完成；直接使用nodes[].result的业务结果和产物引用。'
                                if state['complete'] else
                                '只修正失败节点或补齐前置条件；成功结果已保留，无需重做。')}


def _visible_result(result, *, limit):
    """Business results precede provenance; oversized values remain in evidence."""
    fields = ('status', 'result', 'files', 'outputs', 'paths', 'map_id', 'project_dir',
              'project_file', 'qgis_project', 'project_manifest', 'preview_path',
              'report_path', 'analysis_ref', 'package_path', 'manifest_path', 'summary_path',
              'scene_ref', 'analysis_status',
              'analysis_correctness', 'facts', 'policy_ref', 'stdout', 'stderr')
    visible, omitted = {}, []
    metadata = result.get('_metadata', {})
    for key in fields:
        if key not in result and key not in metadata:
            continue
        value = result.get(key, metadata.get(key))
        if key in {'stdout', 'stderr'} and isinstance(value, str) and len(value) > 1000:
            value = value[-1000:]
            visible[key + '_truncated'] = True
        candidate = {**visible, key: value}
        if len(json.dumps(candidate, ensure_ascii=False)) <= limit:
            visible[key] = value
        else:
            omitted.append(key)
    if omitted:
        visible['omitted_sections'] = omitted
    return visible


def _resume_task(context, identity):
    """Restore persisted task references, without replaying unknown external operations."""
    from uuid import UUID

    from geoharness.analysis.policy import PolicyDecision, policy_question
    from geoharness.task_state import TaskRecord

    identity = str(UUID(identity or context.metadata.get('geo_task_record', {}).get('task_id', '')))
    root = Path(context.cwd).resolve()
    configured = Path(context.metadata['geoharness_config'].agent.task_state_dir).expanduser()
    directory = (configured if configured.is_absolute() else root / configured).resolve() / identity
    if not directory.is_relative_to(root) or not (directory / 'task.sqlite3').is_file():
        raise ValueError('当前工作区没有这个持久任务')
    with connect(directory) as db:
        saved = db.execute("SELECT payload FROM state WHERE key='task'").fetchone()
        stages = db.execute("SELECT payload FROM state WHERE key='analysis_stages'").fetchone()
        page_reuse = db.execute("SELECT payload FROM state WHERE key='page_reuse'").fetchone()
        profiles = db.execute("SELECT payload FROM state WHERE key='input_profiles'").fetchone()
    if saved is None:
        raise ValueError('任务缺少已提交的任务记录')
    task = TaskRecord.model_validate_json(saved['payload'])
    if Path(task.workspace).resolve() != root or str(task.task_id) != identity:
        raise ValueError('持久任务身份或工作区不匹配')
    bucket = context.metadata.setdefault('geo_task_record', {})
    bucket.clear()
    bucket.update(task.model_dump(mode='json'))
    if stages:
        from geoharness.session_state import _analysis_stages

        context.metadata['analysis_workflow_state'] = _analysis_stages(
            json.loads(stages['payload']), ())
    if page_reuse:
        context.metadata['geo_page_reuse'] = json.loads(page_reuse['payload'])
    if profiles:
        from geoharness.data.profiles import record_profiles

        record_profiles(context.metadata, list(json.loads(profiles['payload']).values()))
    # The caller owns the OS lease: an old executor is no longer running locally.
    # Unknown outcomes become interrupted, retain their exact fingerprint, and
    # cannot replay until inputs/implementation change after evidence inspection.
    with connect(directory) as db:
        running = [dict(r) for r in db.execute("SELECT * FROM nodes WHERE status='running'")]
    for node in running:
        if not node.get('operation_id'):
            with connect(directory) as db:
                db.execute("UPDATE nodes SET status='interrupted',error=? WHERE id=?",
                           ('process_outcome_unknown; inspect_before_revising', node['id']))
            continue
        with connect(directory) as db:
            operation = db.execute('SELECT * FROM operations WHERE id=?',
                                   (node['operation_id'],)).fetchone()
        if operation is None or operation['status'] == 'running':
            with connect(directory) as db:
                db.execute("UPDATE nodes SET status='interrupted',error=? WHERE id=?",
                           ('process_outcome_unknown; inspect_before_revising', node['id']))
                if operation:
                    db.execute("UPDATE operations SET status='interrupted',error=? WHERE id=?",
                               ('process_outcome_unknown', node['operation_id']))
            continue
        if operation['status'] == 'succeeded':
            raw = _load_result(node, context)
            files = _artifacts(raw)
            files[operation['evidence']] = operation['digest']
            status = 'succeeded'
        else:
            files, status = {}, operation['status']
        with connect(directory) as db:
            db.execute('UPDATE nodes SET status=?,artifacts=? WHERE id=?',
                       (status, json.dumps(files), node['id']))
    for ref, raw in task.score_policies.items():
        decision = PolicyDecision.model_validate(raw)
        if decision.status == 'pending':
            context.metadata.setdefault('geo_clarification', {})['pending'] = {
                'question': policy_question(decision.policy), 'policy_ref': ref}
    return plan_status(context)


def resume_task(context, identity):
    from uuid import UUID

    root = Path(context.cwd).resolve()
    configured = Path(context.metadata['geoharness_config'].agent.task_state_dir).expanduser()
    task_id = str(UUID(identity or context.metadata.get('geo_task_record', {}).get('task_id', '')))
    directory = (configured if configured.is_absolute() else root / configured).resolve() / task_id
    if not directory.is_relative_to(root) or not (directory / 'task.sqlite3').is_file():
        raise ValueError('当前工作区没有这个持久任务')
    with execution_lock(directory):
        return _resume_task(context, identity)
