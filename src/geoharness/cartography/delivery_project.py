"""Materialize an explicitly selected map specification at delivery time."""

from __future__ import annotations


async def materialize_project(context, spec_ref):
    from openharness.engine.query import (
        _extract_permission_command,
        _resolve_permission_file_path,
    )
    from openharness.hooks import HookEvent

    from geoharness.performance import MeasuredTool
    from geoharness.runtime import _build_permission_checker
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    tool = GeoQgisProjectTool()
    arguments = GeoQgisProjectInput(action='build', spec_ref=spec_ref)
    values = arguments.model_dump(mode='json', exclude_unset=True)
    checker = context.metadata.get('geoharness_permission_checker') or (
        _build_permission_checker(context.metadata['geoharness_config']))
    decision = checker.evaluate(tool.name, is_read_only=False,
        file_path=_resolve_permission_file_path(context.cwd, values, arguments),
        command=_extract_permission_command(values, arguments))
    if not decision.allowed or decision.requires_confirmation:
        raise ValueError('当前权限未授权交付时生成QGIS工程；'
                         '先在已授权入口执行geo_qgis_project build')
    if context.hook_executor:
        hooks = await context.hook_executor.execute(HookEvent.PRE_TOOL_USE, {
            'tool_name': tool.name, 'tool_input': values, 'event': HookEvent.PRE_TOOL_USE.value})
        if hooks.blocked:
            raise ValueError(hooks.reason or '工程创建被PRE_TOOL_USE拒绝')
    # The parent task owns the execution lease. Permission and hooks still apply,
    # but this child must not recursively acquire the controller's OS lock.
    result = await MeasuredTool(tool, context.metadata, internal_dispatch=True).execute(
        arguments, context)
    if context.hook_executor:
        await context.hook_executor.execute(HookEvent.POST_TOOL_USE, {
            'tool_name': tool.name, 'tool_input': values, 'tool_output': result.output,
            'tool_is_error': result.is_error, 'event': HookEvent.POST_TOOL_USE.value})
    if result.is_error:
        raise ValueError('project_build_failed: ' + result.output[:2000])
    metadata = result.metadata
    if not metadata.get('project_dir'):
        raise ValueError('工程生成未返回project_dir；保留原始结果并检查本次工程证据')
    return metadata
