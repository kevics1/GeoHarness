"""Isolated PyQGIS console for ordinary trusted scripts, not an OS sandbox.

Python audit checks and common native QGIS writers constrain accidental writes.
Processing uses the same validated built-in algorithm path as geo_qgis. Native
extensions are trusted; hostile Python requires operating-system isolation.
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from .worker_context import WorkerContext

_RESERVED = {'request.json', 'result.json', 'manifest.json', 'stdout.txt', 'stderr.txt',
             'analysis.py', '.start'}
_IMPORTS = {'qgis', 'processing', 'json', 'math', 're', 'collections', 'itertools',
            'datetime', 'pathlib', 'numpy', 'pandas', 'geopandas', 'shapely', 'rasterio'}


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        raise ValueError('Console results must be bounded JSON values, not native QGIS objects')
    if value is None or type(value) in (bool, int, float, str):
        if isinstance(value, str) and len(value) > 20000:
            raise ValueError('Console string result exceeds 20000 characters')
        return value
    if isinstance(value, (list, tuple)) and len(value) <= 1000:
        return [_json_safe(item, depth + 1) for item in value]
    if isinstance(value, dict) and len(value) <= 1000 and all(isinstance(k, str) for k in value):
        return {key: _json_safe(item, depth + 1) for key, item in value.items()}
    raise ValueError('Console results must be bounded JSON values, not native QGIS objects')


def _validate_code(code):
    if not isinstance(code, str) or not code.strip() or len(code) > 200000:
        raise ValueError('Console requires a complete script of at most 200000 characters')
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ''])
            if any(name.split('.')[0] not in _IMPORTS for name in names):
                raise PermissionError('Console imports are limited to GIS/data/standard helpers')
        if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            raise PermissionError('Console native reflection is unavailable')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                'eval', 'exec', 'compile', '__import__', 'getattr', 'setattr', 'delattr'}:
            raise PermissionError('Console dynamic execution/reflection is unavailable')
    return compile(tree, '<qgis-console>', 'exec')


class ConsoleGuard:
    def __init__(self, context):
        self.context = context
        self.active = False
        self.runtime_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
        # QGIS imports Python modules from its separate installation tree.
        self.runtime_roots.extend(Path(p).resolve() for p in sys.path if p and Path(p).is_dir())

    def path(self, value, *, writing=False):
        if not isinstance(value, (str, bytes, os.PathLike)):
            raise PermissionError('Console file access requires a local path')
        path = Path(os.fsdecode(value)).expanduser().resolve()
        if writing:
            if not path.is_relative_to(self.context.run_dir):
                raise PermissionError('Console outputs must stay inside RUN_DIR')
            relative = path.relative_to(self.context.run_dir)
            if not relative.parts or relative.parts[0] == '.profile' or path.name in _RESERVED:
                raise PermissionError('Console provenance/profile files are read-only')
            return path
        if any(path.is_relative_to(root) for root in self.runtime_roots):
            return path
        if path.is_dir():
            if any(path.is_relative_to(root) for root in self.context.read_roots):
                return path
            raise PermissionError('Console directory is outside read roots')
        return self.context.read_path(str(path))

    def audit(self, event, args):
        if not self.active:
            return
        if event == 'open':
            mode, flags = args[1:3]
            writing = bool((flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                           os.O_TRUNC | os.O_APPEND)) or bool(
                mode and any(c in mode for c in 'wax+'))
            self.path(args[0], writing=writing)
        elif event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime', 'os.truncate',
                       'os.replace', 'os.unlink'}:
            self.path(args[0], writing=True)
            if event == 'os.replace' and len(args) > 1:
                self.path(args[1], writing=True)
        elif event == 'os.rename':
            self.path(args[0], writing=True)
            self.path(args[1], writing=True)
        elif event in {'os.symlink', 'os.link'} or event.startswith(
                ('socket.', 'subprocess.', 'ctypes.', 'os.system', 'os.exec', 'os.spawn',
                 'os.fork', 'os.startfile')):
            raise PermissionError('Console network/process/link operations are unavailable')

    @contextmanager
    def enabled(self):
        self.active = True
        try:
            yield
        finally:
            self.active = False

    def install_native(self):
        from qgis.core import (
            QgsProject,
            QgsRasterFileWriter,
            QgsRasterLayer,
            QgsVectorDataProvider,
            QgsVectorFileWriter,
            QgsVectorLayer,
            QgsVectorLayerExporter,
        )

        def wrap(cls, name, index, *, uri=False):
            original = getattr(cls, name)

            def checked(*args, **kwargs):
                value = args[index] if len(args) > index else next(
                    (kwargs[k] for k in ('fileName', 'filename', 'uri', 'outputUrl')
                     if k in kwargs),
                    None)
                if value is None:
                    raise ValueError('Use an explicit RUN_DIR destination')
                self.path(str(value).split('|')[0] if uri else value, writing=True)
                return original(*args, **kwargs)
            setattr(cls, name, checked)

        for name in ('writeAsVectorFormat', 'writeAsVectorFormatV2', 'writeAsVectorFormatV3'):
            wrap(QgsVectorFileWriter, name, 1)
        wrap(QgsVectorFileWriter, 'deleteShapeFile', 0)
        wrap(QgsVectorFileWriter, 'create', 0)
        wrap(QgsVectorFileWriter, '__init__', 1)
        wrap(QgsRasterFileWriter, '__init__', 1)
        wrap(QgsVectorLayerExporter, 'exportLayer', 1, uri=True)
        original_write = QgsProject.write

        def project_write(project, *args, **kwargs):
            self.path(args[0] if args else project.fileName(), writing=True)
            return original_write(project, *args, **kwargs)
        QgsProject.write = project_write

        def protect_load(cls):
            original = cls.__init__

            def checked(obj, *args, **kwargs):
                source = args[0] if args else kwargs.get('path', '')
                provider = args[2] if len(args) > 2 else kwargs.get('providerLib', '')
                if source and provider != 'memory':
                    self.context.read_path(str(source).split('|')[0])
                return original(obj, *args, **kwargs)
            cls.__init__ = checked
        protect_load(QgsVectorLayer)
        protect_load(QgsRasterLayer)

        # Native providers can edit data without Python open() events. Keep source
        # layers read-only, including provider-level attribute/geometry changes.
        def protect_edit(cls, name, locator):
            original = getattr(cls, name, None)
            if original is None:
                return

            def checked(obj, *args, **kwargs):
                source = locator(obj)
                if not source.startswith(('memory:', 'Point?', 'LineString?', 'Polygon?')):
                    self.path(source.split('|')[0], writing=True)
                return original(obj, *args, **kwargs)
            setattr(cls, name, checked)
        protect_edit(QgsVectorLayer, 'startEditing', lambda obj: (
            'memory:' if obj.providerType() == 'memory' else obj.source()))
        for name in ('addFeatures', 'deleteFeatures', 'changeAttributeValues',
                     'changeGeometryValues', 'addAttributes', 'deleteAttributes',
                     'createSpatialIndex', 'createAttributeIndex', 'truncate'):
            protect_edit(QgsVectorDataProvider, name, lambda obj: obj.dataSourceUri())


def dispatch(action: str, payload: dict[str, Any], context: WorkerContext) -> dict[str, Any]:
    if action != 'console':
        raise ValueError('Unsupported console action')
    compiled = _validate_code(payload.get('code'))
    state = payload.get('console_state') or {}
    if not isinstance(state, dict) or len(state) > 64:
        raise ValueError('console_state must be a bounded JSON object')
    state = _json_safe(state)
    if len(json.dumps(state, allow_nan=False)) > 20000:
        raise ValueError('console_state exceeds 20000 characters')
    inputs = {key: context.read_path(value) for key, value in (payload.get('inputs') or {}).items()}
    import processing
    from qgis.core import QgsProject

    from .processing_worker import dispatch as processing_dispatch

    guard = ConsoleGuard(context)
    guard.install_native()
    original_run = processing.run

    def checked_run(algorithm_id, parameters, **options):
        if not guard.active:
            return original_run(algorithm_id, parameters, **options)
        if set(options) - {'context', 'feedback', 'is_child_algorithm'}:
            raise ValueError('Unsupported processing.run option')
        # The standard Processing worker validates paths, providers and parameters
        # before trusted native providers execute. Keep user-script process denial.
        guard.active = False
        try:
            return processing_dispatch('run', {'algorithm_id': algorithm_id,
                                               'parameters': parameters}, context)['outputs']
        finally:
            guard.active = True

    processing.run = checked_run
    processing.runAndLoadResults = checked_run
    namespace = {'__name__': '__main__', 'RUN_DIR': context.run_dir, 'SOURCE_CWD': context.cwd,
                 'INPUTS': inputs, 'CONSOLE_STATE': state, 'Project': QgsProject.instance(),
                 'processing': processing}
    out, err = io.StringIO(), io.StringIO()
    result = None
    error_summary = None
    sys.addaudithook(guard.audit)
    try:
        with redirect_stdout(out), redirect_stderr(err), guard.enabled():
            exec(compiled, namespace)
            result_name = payload.get('console_result', 'CONSOLE_RESULT')
            if result_name in namespace:
                result = _json_safe(namespace[result_name])
                if len(json.dumps(result, allow_nan=False)) > 20000:
                    raise ValueError('Console result exceeds 20000 characters')
            else:
                result = None
    except BaseException as exc:
        error_summary = f'{type(exc).__name__}: {exc}'
        traceback.print_exc(file=err)
        raise
    finally:
        # Parent captures these streams into complete, immutable task logs even
        # when script execution or result serialization fails.
        sys.stdout.write(out.getvalue())
        sys.stderr.write(err.getvalue())
    return {'console_result': result,
            'console_result_present': payload.get('console_result', 'CONSOLE_RESULT') in namespace,
            'console_state': state,
            'error_summary': error_summary,
            'execution_boundary': '独立QGIS进程；每次调用新进程；显式JSON状态；'
                                  '应用层文件保护，非OS沙盒'}
