"""Resolve explicit read expressions into measured inputs, without rewriting user code."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from geoharness.data.artifacts import lookup_reference
from geoharness.data.task_context import data_ref_snapshot, resolve_registered_path
from geoharness.execution.contracts import _reachable_ast


def bind_managed_inputs(arguments, context):
    """Managed tasks must declare dynamic reads; literal registered reads bind automatically."""
    from geoharness.tools.geo_python import InputRequirement

    tree = ast.parse(arguments.code)
    values, imports, reads = {}, {}, []

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            a, b = literal(node.left), literal(node.right)
            if a is not None and b is not None:
                return a + b if isinstance(node.op, ast.Add) else str(Path(a) / b)
        if isinstance(node, ast.Call) and len(node.args) == 1 and isinstance(node.func, ast.Name):
            if node.func.id in {'Path', 'str'}:
                return literal(node.args[0])
        return None

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            imports.update({a.asname or a.name: f'{node.module}.{a.name}' for a in node.names})
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = literal(node.value)
    # Only visit nodes reachable in the actual control flow. A literal
    # `if False:` probe or a debug branch must not force unrelated inputs to be
    # declared for code that will never execute.
    for node in _reachable_ast(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ''
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            name = imports.get(node.func.value.id, node.func.value.id) + '.' + node.func.attr
        else:
            name = imports.get(name, name)
        if name == 'rasterio.open':
            mode = literal(node.args[1]) if len(node.args) > 1 else next((literal(k.value)
                for k in node.keywords if k.arg == 'mode'), None)
            if mode and any(flag in mode for flag in 'wa+'):
                continue
        if name in {'read_vector', 'resolve_data', 'read_raster', 'mask_raster',
                    'raster_backdrop', 'sample_raster_points', 'native_layer',
                    'geopandas.read_file', 'pandas.read_csv', 'rasterio.open'}:
            reads.append(literal(node.args[0]))
    if any(value is None for value in reads) and not arguments.input_refs:
        raise ValueError('动态业务读取需要input_refs；系统不会猜测变量最终指向哪个文件。')
    config, cwd = context.metadata['geoharness_config'], Path(context.cwd)
    records = data_ref_snapshot(context.metadata, cwd=cwd, config=config)
    bindings = dict(arguments.input_refs)
    requirements = dict(arguments.input_requirements)
    for value in dict.fromkeys(v for v in reads if v is not None):
        try:
            record = lookup_reference(value, records)
        except ValueError:
            target = Path(value).expanduser()
            target = (target if target.is_absolute() else cwd / target).resolve()
            matches = [r for r in records.values() if os.path.normcase(str(
                Path(r['path']).resolve())) == os.path.normcase(str(target))]
            if len(matches) != 1:
                # An explicit alias remains usable; its file/hash are checked by the tool.
                explicit = [v for v in bindings.values() if v == value]
                # An alias may be supplied as its key in read_vector(alias), not
                # only as the resolved file value. Never send a valid declared
                # input to an unregistered-reference precondition failure.
                if explicit or value in bindings:
                    continue
                raise ValueError(f'读取引用未登记或不唯一：{value!r}；'
                                 '请inspect数据或声明input_refs。')
            record = matches[0]
        resolve_registered_path(record['ref'], context.metadata, cwd=cwd, config=config)
        if record['ref'] in bindings.values():
            continue
        alias = 'bound_' + record['ref'].split(':')[-1]
        bindings[alias] = record['ref']
        if record.get('kind') == 'vector':
            requirements[alias] = InputRequirement(valid_geometry=True, crs=record.get('crs'))
    return arguments.model_copy(update={'input_refs': bindings,
        'input_requirements': requirements, 'stage_id': arguments.stage_id or arguments.name})
