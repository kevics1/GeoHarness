"""Publish recomputed, selection-bound facts for QGIS and custom Python results.

Facts certify persisted values, never a user-declared scientific method. Each
dataset/selection has its own identity, including a distinct missing-value denominator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from openharness.utils.fs import atomic_write_text
from pydantic import BaseModel, ConfigDict, Field

from geoharness.data.artifacts import assert_usable_artifact, sha256_file
from geoharness.execution.contracts import artifact_facts, write_analysis_report


class FactRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=256,
        description='源文件内实际存在、唯一且非空的实体ID字段；系统用其冻结成员集合。')
    numeric_fields: list[str] = Field(default_factory=list, max_length=64,
        description='需要复算统计的实际数值字段；缺失单独计数，类别代码不应当作连续数值求均值。')
    group_by: list[str] = Field(default_factory=list, max_length=16,
        description='分别计算各类别数量的字段名；不改变源文件和当前结果选集。')
    predicates: list[dict] = Field(default_factory=list, max_length=64,
        description='条件计数列表：每项严格为{name,field,op,value}；op为<、<=、>、>=、==、!=。'
        '例如{name:"near",field:"distance_m",op:"<=",value:500}，不等同于空间包含。')
    scope_name: str = Field(default='当前完整结果集', min_length=1, max_length=256,
        description='明确命名当前文件的完整成员范围，例如“全体234处”或“重点13处”；分母不跨选集混用。')
    units: dict[str, str] = Field(default_factory=dict, max_length=64,
        description='numeric_fields内字段到单位的映射，例如{distance_m:"m",population:"人"}。')
    method: dict = Field(min_length=1,
        description='实际方法/参数/规则/缺失政策；距离面积方法注明metric_crs。方法是声明，'
        '不因统计核对通过而获得科学认证；未知业务阈值须明确假设或询问用户。')


def _members(source, key):
    import geopandas as gpd
    import pandas as pd

    frame = pd.read_csv(source) if source.suffix.lower() == '.csv' else gpd.read_file(source)
    if key not in frame or frame[key].isna().any() or not frame[key].is_unique:
        raise ValueError('结果包要求唯一非空的真实实体主键')
    values = sorted(json.dumps(value.item() if hasattr(value, 'item') else value,
                               ensure_ascii=False, default=str) for value in frame[key])
    return hashlib.sha256(json.dumps([key, values], ensure_ascii=False).encode()).hexdigest()


def _source_inventory(source):
    if source.suffix.lower() == '.gpkg':
        import pyogrio

        if len(pyogrio.list_layers(source)) != 1:
            raise ValueError('结果包要求单图层GPKG；请先将目标图层另存为独立结果')
    paths = [source]
    if source.suffix.lower() == '.shp':
        paths.extend(source.with_suffix(suffix) for suffix in ('.dbf', '.shx', '.prj', '.cpg')
                     if source.with_suffix(suffix).is_file())
    return {str(path): sha256_file(path) for path in paths}


def _selection(request, facts, inventory, member_digest):
    identity = hashlib.sha256(json.dumps(
        [request.key, sorted(inventory.items()), member_digest], ensure_ascii=False).encode()
    ).hexdigest()
    return {'id': 'selection:' + identity, 'key': request.key,
            'member_sha256': member_digest, 'row_count': facts['row_count'],
            'scope_name': request.scope_name, 'scope': 'all_rows_of_exact_source'}


def publish_package(context, *, source_ref: str, request: FactRequest):
    from geoharness.data.task_context import resolve_registered_path
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.task_store import save_record
    from geoharness.tools.geo_qgis import qgis_read_roots

    config, cwd = context.metadata['geoharness_config'], Path(context.cwd).resolve()
    value = resolve_registered_path(source_ref, context.metadata, cwd=cwd, config=config)
    source = WorkerContext(cwd, cwd, qgis_read_roots(config, cwd)).read_path(value)
    producer = assert_usable_artifact(source)
    # Publication is a trust transition, not a tool for laundering untracked outputs.
    if not producer:
        raise ValueError('分析结果包需要已登记成功的分析产物；原始文件请先执行分析或显式复制结果')
    if source.suffix.lower() not in {'.csv', '.geojson', '.gpkg', '.shp', '.json'}:
        raise ValueError('结果事实目前支持单图层矢量或CSV；不能将栅格类别代码直接平均')
    if set(request.units) - set(request.numeric_fields):
        raise ValueError('units只能声明numeric_fields中已有字段的单位')
    from geoharness.task_store import _safe

    if _safe(request.model_dump(mode='json'), context.metadata) != request.model_dump(mode='json'):
        raise ValueError('结果包声明包含凭证，请移除敏感内容')
    inventory = _source_inventory(source)
    task = context.metadata.get('geo_task_record')
    # Reuse an identical package, including issues/unknown method status.
    for reference in context.metadata.get('geo_task_record', {}).get('analysis_packages', []):
        try:
            old = verify_package(Path(reference))
            if (Path(old['facts']['source']).resolve() == source
                    and old['request'] == request.model_dump(mode='json')):
                return {'package_path': reference, 'status': 'persisted_values_checked',
                        'manifest_path': str(Path(reference).parent / 'manifest.json'),
                        'report_path': old['companions']['markdown']['path'],
                        'facts': old['facts'], 'selection': old['selection'], 'reused': True,
                        'analysis_correctness': 'unverified'}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    directory = Path(config.analysis.outputs_dir).expanduser()
    directory = (directory if directory.is_absolute() else cwd / directory).resolve()
    directory = directory / ('result-' + uuid4().hex)
    directory.mkdir(parents=True)
    target = directory / 'facts.json'
    report = write_analysis_report(target, source=source, key=request.key,
        numeric_fields=request.numeric_fields, group_by=request.group_by,
        predicates=request.predicates, method=request.method)
    identity = _members(source, request.key)
    report.update(kind='analysis_result_package', request=request.model_dump(mode='json'),
                  selection=_selection(request, report['facts'], inventory, identity),
                  source_inventory=inventory,
                  units=request.units, producer=producer,
                  status='persisted_values_checked')
    # Actual execution evidence is sourced from the producer, never method prose.
    execution_evidence = {'storage_crs': report['facts'].get('crs'),
        'metric_crs': None, 'metric_crs_status': 'unknown',
        'producer_manifest': producer['manifest'], 'scientific_correctness': 'unverified'}
    if producer.get('manifest'):
        manifest = Path(producer['manifest'])
        document = json.loads(manifest.read_text(encoding='utf-8'))
        execution_evidence['producer_manifest_sha256'] = sha256_file(manifest)
        if producer['producer'] == 'geo_qgis':
            execution_evidence['algorithm_id'] = document.get('result', {}).get('algorithm_id')
            execution_evidence['parameters'] = document.get('result', {}).get('parameters')
        elif producer['producer'] in {'geo_analysis', 'geo_accessibility'}:
            execution_evidence.update(metric_crs=document.get('metric_crs'),
                                       metric_crs_status='native_execution_record')
    report['execution_evidence'] = execution_evidence
    if inventory != _source_inventory(source):
        raise ValueError('发布期间源数据发生变化，不能提交当前结果包')
    # Keep the visible report exactly aligned with the published package.
    markdown = directory / 'facts.md'
    text = ('# 分析结果与事实\n\n'
            f"选集：{request.scope_name}\n\nselection_id：{report['selection']['id']}\n\n"
            f"实体数：{report['facts']['row_count']}\n\n"
            '统计来自当前落盘结果；方法和推断尚未独立验证。\n\n```json\n'
            + json.dumps({'facts': report['facts'], 'units': request.units,
                          'method': request.method, 'execution_evidence': execution_evidence},
                         ensure_ascii=False, indent=2) + '\n```\n')
    atomic_write_text(markdown, text)
    report['companions'] = {'markdown': {'path': str(markdown), 'sha256': sha256_file(markdown)}}
    atomic_write_text(target, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    files = [{'path': str(p), 'sha256': sha256_file(p), 'state': 'succeeded'}
             for p in (target, markdown)]
    atomic_write_text(directory / 'manifest.json', json.dumps({
        'producer': 'geo_result_package', 'status': 'succeeded', 'run_dir': str(directory),
        'files': files}, ensure_ascii=False, indent=2))
    if task is not None:
        packages = task.setdefault('analysis_packages', [])
        packages.append(str(target))
        task['analysis_requirement'] = 'required'
        save_record(context.metadata, cwd=cwd)
    return {'package_path': str(target), 'manifest_path': str(directory / 'manifest.json'),
            'report_path': str(markdown), 'status': 'persisted_values_checked',
            'selection': report['selection'], 'facts': report['facts'],
            'analysis_correctness': 'unverified', 'reused': False}


def verify_package(path: Path, *, guard=None):
    if guard:
        path = guard.read_path(str(path))
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('结果包超过读取上限')
    producer = assert_usable_artifact(path)
    if not producer or producer['producer'] != 'geo_result_package':
        raise ValueError('结果包没有已提交的生产者清单')
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('kind') != 'analysis_result_package':
        raise ValueError('结果包类型不匹配')
    request = FactRequest.model_validate(report['request'])
    source = Path(report['facts']['source']).resolve()
    if guard:
        source = guard.read_path(str(source))
    actual_producer = assert_usable_artifact(source)
    if actual_producer != report.get('producer'):
        raise ValueError('结果包的生产者来源发生变化')
    inventory = _source_inventory(source)
    if inventory != report.get('source_inventory'):
        raise ValueError('结果包源数据或伴随文件已经变化')
    actual = artifact_facts(source, key=request.key, numeric_fields=request.numeric_fields,
                            group_by=request.group_by, predicates=request.predicates,
                            include_median=True)
    if (actual != report['facts'] or report['selection'] != _selection(
            request, actual, inventory, _members(source, request.key))):
        raise ValueError('结果包的统计或成员与当前源文件不一致，须重建')
    if report.get('units') != request.units or report.get('method') != request.method:
        raise ValueError('结果包的单位或方法声明不一致')
    evidence = report.get('execution_evidence', {})
    if evidence.get('producer_manifest'):
        manifest = Path(evidence['producer_manifest'])
        if guard:
            manifest = guard.read_path(str(manifest))
        if sha256_file(manifest) != evidence.get('producer_manifest_sha256'):
            raise ValueError('分析生产者证据已改变')
    return report


def infer_packages(metadata, mapped_sources):
    """Only exact source bindings qualify; filenames and prose are never evidence."""
    sources = {str(Path(item.get('file_path', item) if isinstance(item, dict) else item).resolve())
               for item in mapped_sources if item and (
                   not isinstance(item, dict) or item.get('file_path'))}
    result = []
    for reference in metadata.get('geo_task_record', {}).get('analysis_packages', []):
        path = Path(reference)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            continue
        try:
            locator = json.loads(path.read_text(encoding='utf-8'))
            related_source = str(Path(locator['facts']['source']).resolve())
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if related_source not in sources:
            continue
        verify_package(path)
        result.append(reference)
    return result


def delivery_packages(metadata, files, project_document, resolve):
    """Attach only published facts actually referenced by this selected deliverable."""
    selected = set(files)
    bound = {str(Path(a['manifest_path']).resolve()) for a in
             project_document.get('analysis', {}).get('analyses', [])
             if isinstance(a, dict) and isinstance(a.get('manifest_path'), str)}
    for reference in metadata.get('geo_task_record', {}).get('analysis_packages', []):
        path = Path(reference).resolve()
        if str(path) not in bound and path not in selected:
            # Inspect only the small package locator to decide relevance; full
            # validation is mandatory as soon as a selected source matches.
            if not path.is_file() or path.stat().st_size > 1024*1024:
                continue
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                related_source = Path(raw['facts']['source']).resolve()
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if related_source not in selected:
                continue
        package = verify_package(path)
        for related in (path, Path(package['facts']['source']),
                        Path(package['companions']['markdown']['path'])):
            target = resolve(str(related))
            if target not in files:
                if len(files) >= 100:
                    raise ValueError('分析交付包超出100文件上限')
                files.append(target)
    return files


def require_candidate_bindings(metadata, mapped_sources, analysis):
    """Every produced vector needs its own binding, not one token valid layer."""
    if metadata.get('geo_task_record', {}).get('analysis_requirement') != 'required':
        return
    bound = {item['layer_index'] for entry in analysis.get('analyses', [])
             for item in entry.get('mapped_outputs', []) if 'layer_index' in item}
    missing = []
    for index, item in enumerate(mapped_sources):
        if not item.get('file_path'):
            continue
        source = Path(item['file_path'])
        if source.suffix.lower() not in {'.geojson', '.gpkg', '.shp', '.json'}:
            continue
        producer = assert_usable_artifact(source)
        if (producer and producer.get('producer') in {
                'geo_python', 'geo_qgis', 'geo_analysis', 'geo_accessibility'}
                and item.get('layer_index', index) not in bound):
            missing.append(str(source))
    if missing:
        raise ValueError('以下分析候选图层尚未绑定结果事实，请分别 publish_analysis：'
                         + '; '.join(missing))
