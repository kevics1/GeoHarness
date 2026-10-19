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


class NearestCheck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: str = Field(min_length=1, max_length=256,
        description='当前源文件内需要逐实体独立复核的最近距离字段，单位米。')
    reference_ref: str = Field(min_length=1,
        description='实际用于距离计算的参照矢量文件路径或data:引用；必须是同一时次/选集。'
        '多图层GPKG须同时声明reference_layer，不猜测图层或按文件名寻找。')
    reference_layer: str = Field(default='', max_length=256,
        description='参照源内的真实图层名；多层GPKG必填，单层可省略。')
    metric_crs: str = Field(min_length=1,
        description='实际计算所用的米制投影，例如EPSG:32648；不可用经纬度或仅重贴CRS。')
    absolute_tolerance: float = Field(default=0.11, ge=0, le=10, allow_inf_nan=False,
        description='逐实体允许的舍入误差，单位米；默认0.11米，不能用于掩盖实体错配。')


class FactRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_layer: str = Field(default='', max_length=256,
        description='源文件内的真实图层名；多图层GPKG必填。事实、成员与制图引用均绑定此层。')
    key: str = Field(min_length=1, max_length=256,
        description='源文件内实际存在、唯一且非空的实体ID字段；系统用其冻结成员集合。')
    numeric_fields: list[str] = Field(default_factory=list, max_length=64,
        description='需要复算统计的实际数值字段；缺失单独计数，类别代码不应当作连续数值求均值。')
    carry_fields: list[str] = Field(default_factory=list, max_length=64,
        description='可选。从同一任务已发布结果包按唯一ID继承、且本结果不得丢失或退化的字段。'
        '用于派生几何/重采样后保留高程、人口等已有值；系统按key核对缺失数不能增加。'
        '只在确有同一实体来源时使用，不能跨选集或跨版本猜测。')
    carry_from: str = Field(default='', description=(
        '可选。提供carry_fields的已发布analysis_result_package路径；'
        '必须与当前source共享唯一key并有已登记来源，不能使用任意旧文件。'))
    group_by: list[str] = Field(default_factory=list, max_length=16,
        description='分别计算各类别数量的字段名；不改变源文件和当前结果选集。')
    missing_by: list[str] = Field(default_factory=list, max_length=8,
        description='按这些真实类别字段联合统计numeric_fields的有值/缺失数量；'
        '说明人口缺失属于哪些类别时必须声明，不能从总缺失数推断类别。')
    predicates: list[dict] = Field(default_factory=list, max_length=64,
        description='条件计数列表：每项严格为{name,field,op,value}；op为<、<=、>、>=、==、!=。'
        '例如{name:"near",field:"distance_m",op:"<=",value:500}，不等同于空间包含。')
    scope_name: str = Field(default='当前完整结果集', min_length=1, max_length=256,
        description='明确命名当前文件的完整成员范围，例如“全体234处”或“重点13处”；分母不跨选集混用。')
    units: dict[str, str] = Field(default_factory=dict, max_length=64,
        description='numeric_fields内字段到单位的映射，例如{distance_m:"m",population:"人"}。')
    nearest_checks: list[NearestCheck] = Field(default_factory=list, max_length=16,
        description='需要证明最近距离正确时逐字段声明field/reference_ref/metric_crs；'
        '系统直接从真实几何独立复算每个实体，错值或空值拒绝发布。'
        '不声明时只能证明落盘统计自洽，不能宣称距离计算正确；不适用于路网/时间成本距离。')
    field_changes: dict[str, str] = Field(default_factory=dict, max_length=64,
        description='有意删除或重新计算既有派生字段时，填写字段名到业务原因；默认禁止同一实体的已发布字段丢失或新增空值。原因未知应询问用户，不能为通过检查编造原因。')
    method: dict = Field(min_length=1,
        description='实际方法/参数/规则/缺失政策；距离面积方法注明metric_crs。方法是声明，'
        '不因统计核对通过而获得科学认证；未知业务阈值须明确假设或询问用户。')


def _members(source, key, source_layer=''):
    frame = _frame(source, source_layer)
    if key not in frame or frame[key].isna().any() or not frame[key].is_unique:
        raise ValueError('结果包要求唯一非空的真实实体主键')
    values = sorted(json.dumps(value.item() if hasattr(value, 'item') else value,
                               ensure_ascii=False, default=str) for value in frame[key])
    return hashlib.sha256(json.dumps([key, values], ensure_ascii=False).encode()).hexdigest()


def _source_inventory(source, source_layer=''):
    if source.suffix.lower() == '.gpkg':
        import pyogrio

        names = list(pyogrio.list_layers(source)[:, 0])
        if (len(names) != 1 and not source_layer or source_layer and source_layer not in names):
            raise ValueError('多图层GPKG须指定真实source_layer；可用：' + ', '.join(names))
    elif source.suffix.lower() == '.csv' and source_layer:
        raise ValueError('CSV不能指定source_layer')
    paths = [source]
    if source.suffix.lower() == '.shp':
        paths.extend(source.with_suffix(suffix) for suffix in ('.dbf', '.shx', '.prj', '.cpg')
                     if source.with_suffix(suffix).is_file())
    return {str(path): sha256_file(path) for path in paths}


def same_source_view(source, left='', right=''):
    """Only a verified single-layer file permits an omitted layer name."""
    if source.suffix.lower() != '.gpkg':
        return left == right
    import pyogrio

    names = list(pyogrio.list_layers(source)[:, 0])
    if len(names) == 1:
        return left in {'', names[0]} and right in {'', names[0]}
    return bool(left and left == right and left in names)


def _selection(request, facts, inventory, member_digest):
    identity = hashlib.sha256(json.dumps(
        [request.key, sorted(inventory.items()), member_digest,
         *([request.source_layer] if request.source_layer else [])], ensure_ascii=False).encode()
    ).hexdigest()
    return {'id': 'selection:' + identity, 'key': request.key,
            'member_sha256': member_digest, 'row_count': facts['row_count'],
            'scope_name': request.scope_name, 'scope': 'all_rows_of_exact_source'}


def _nearest_checks(source, request, resolve):

    from geoharness.execution.nearest import verify_nearest_distances

    if len({check.field for check in request.nearest_checks}) != len(request.nearest_checks):
        raise ValueError('每个最近距离字段只能声明一次复核')
    reports = []
    for check in request.nearest_checks:
        if check.field not in request.numeric_fields or request.units.get(check.field) != 'm':
            raise ValueError('最近距离复核字段必须列入numeric_fields并声明单位m')
        reference = Path(resolve(check.reference_ref))
        assert_usable_artifact(reference)
        before = _source_inventory(reference, check.reference_layer)
        report = verify_nearest_distances(_frame(source, request.source_layer),
            _frame(reference, check.reference_layer),
            key=request.key, field=check.field, metric_crs=check.metric_crs,
            absolute_tolerance=check.absolute_tolerance)
        if before != _source_inventory(reference, check.reference_layer):
            raise ValueError('距离复核期间参照数据发生变化')
        if report['status'] != 'passed':
            raise ValueError('nearest_distance_mismatch: ' + json.dumps(report, ensure_ascii=False))
        reports.append({**report, 'reference': str(reference), 'reference_inventory': before})
    return reports


def _frame(path, source_layer=''):
    import geopandas as gpd
    import pandas as pd

    return pd.read_csv(path) if path.suffix.lower() == '.csv' else gpd.read_file(
        path, **({'layer': source_layer} if source_layer else {}))


def _compare_fields(source, baseline, key, fields, exceptions, source_layer='', baseline_layer=''):
    current, previous = _frame(source, source_layer), _frame(baseline, baseline_layer)
    for frame in (current, previous):
        if key not in frame or frame[key].isna().any() or not frame[key].is_unique:
            raise ValueError('字段连续性要求前后结果具有同一唯一非空实体键')
    if not current[key].isin(previous[key]).all():
        raise ValueError('字段连续性无法证明全部实体来源；请明确真实前序结果和成员集合')
    previous = previous.set_index(key).reindex(current[key])
    regressions = {}
    for field in fields:
        if field in exceptions:
            if not exceptions[field].strip():
                raise ValueError('field_changes中的业务原因不能为空')
            continue
        if field not in previous:
            raise ValueError('前序结果没有声明的继承字段：' + field)
        if field not in current:
            regressions[field] = {'error': 'field_dropped'}
        else:
            # Compare each entity, not aggregate missing counts across selections.
            lost = previous[field].notna().to_numpy() & current[field].isna().to_numpy()
            if lost.any():
                regressions[field] = {'new_missing': int(lost.sum()),
                    'entity_ids': current.loc[lost, key].astype(str).head(5).tolist()}
    if regressions:
        raise ValueError('字段连续性检测到派生字段退化：' + json.dumps(regressions,
            ensure_ascii=False) + '；保留已有值，或在field_changes说明真实的变更原因')
    return {'source': str(baseline),
        'source_inventory': _source_inventory(baseline, baseline_layer),
        'key': key, 'fields': sorted(fields), 'field_changes': exceptions,
        **({'source_layer': source_layer, 'baseline_layer': baseline_layer}
           if source_layer or baseline_layer else {})}


def _continuity_checks(context, source, request, producer):
    from geoharness.data.task_context import resolve_registered_path
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    config, cwd = context.metadata['geoharness_config'], Path(context.cwd)
    guard = WorkerContext(cwd, cwd, qgis_read_roots(config, cwd))
    if bool(request.carry_fields) != bool(request.carry_from):
        raise ValueError('carry_fields与carry_from必须同时提供')
    candidates = {}
    if request.carry_from:
        ref = guard.read_path(resolve_registered_path(request.carry_from, context.metadata,
            cwd=cwd, config=config))
        old = verify_package(ref, guard=guard)
        if old['request']['key'] != request.key:
            raise ValueError('carry_fields要求前后结果使用同一实体键')
        candidates[(old['facts']['source'], old['request'].get('source_layer', ''))] = set(
            request.carry_fields)
    # Only follow measured producer dependencies to published fields. Never infer
    # lineage from filenames, similar IDs or unrelated earlier task outputs.
    document = json.loads(guard.read_path(producer['manifest']).read_text('utf-8'))
    inputs = document.get('inputs', {})
    actual_inputs = {str(Path(v).resolve()) for v in inputs.values() if isinstance(v, str)}
    for reference in context.metadata.get('geo_task_record', {}).get('analysis_packages', []):
        ref = guard.read_path(reference)
        old = json.loads(ref.read_text('utf-8'))
        previous = str(Path(old.get('facts', {}).get('source', '')).resolve())
        if previous not in actual_inputs or old.get('request', {}).get('key') != request.key:
            continue
        old = verify_package(ref, guard=guard)
        fields = set(old['request']['numeric_fields']) | set(old['request']['group_by'])
        candidates.setdefault((previous, old['request'].get('source_layer', '')), set()).update(
            fields)
    reports = []
    for (baseline, baseline_layer), fields in candidates.items():
        path = guard.read_path(baseline)
        if path.resolve() == source.resolve() and baseline_layer == request.source_layer:
            raise ValueError('不能以当前结果本身作为字段连续性基线')
        reports.append(_compare_fields(source, path, request.key, fields, request.field_changes,
            request.source_layer, baseline_layer))
    return reports or None


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
        raise ValueError('结果事实支持矢量指定图层或CSV；不能将栅格类别代码直接平均')
    if set(request.units) - set(request.numeric_fields):
        raise ValueError('units只能声明numeric_fields中已有字段的单位')
    _source_inventory(source, request.source_layer)
    carry_report = _continuity_checks(context, source, request, producer)
    from geoharness.task_store import _safe

    if _safe(request.model_dump(mode='json'), context.metadata) != request.model_dump(mode='json'):
        raise ValueError('结果包声明包含凭证，请移除敏感内容')
    inventory = _source_inventory(source, request.source_layer)
    task = context.metadata.get('geo_task_record')
    # Reuse an identical package, including issues/unknown method status.
    for reference in context.metadata.get('geo_task_record', {}).get('analysis_packages', []):
        try:
            old = verify_package(Path(reference))
            if (Path(old['facts']['source']).resolve() == source
                    and old['request'] == request.model_dump(mode='json')):
                return {'analysis_ref': reference, 'package_path': reference,
                        'status': 'persisted_values_checked',
                        'manifest_path': str(Path(reference).parent / 'manifest.json'),
                        'report_path': old['companions']['markdown']['path'],
                        'facts': old['facts'], 'selection': old['selection'], 'reused': True,
                        'analysis_correctness': 'unverified'}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    nearest = _nearest_checks(source, request, lambda ref: WorkerContext(
        cwd, cwd, qgis_read_roots(config, cwd)).read_path(resolve_registered_path(
            ref, context.metadata, cwd=cwd, config=config)))
    directory = Path(config.analysis.outputs_dir).expanduser()
    directory = (directory if directory.is_absolute() else cwd / directory).resolve()
    directory = directory / ('result-' + uuid4().hex)
    directory.mkdir(parents=True)
    target = directory / 'facts.json'
    report = write_analysis_report(target, source=source, key=request.key,
        numeric_fields=request.numeric_fields, group_by=request.group_by,
        missing_by=request.missing_by,
        predicates=request.predicates, method=request.method, source_layer=request.source_layer)
    identity = _members(source, request.key, request.source_layer)
    if carry_report is not None:
        report['carry_fields'] = carry_report
    report.update(kind='analysis_result_package', request=request.model_dump(mode='json'),
                  selection=_selection(request, report['facts'], inventory, identity),
                  source_inventory=inventory,
                  units=request.units, producer=producer,
                  status='persisted_values_checked')
    if request.nearest_checks:
        report['nearest_checks'] = nearest
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
    if request.nearest_checks:
        execution_evidence['nearest_distance_status'] = 'independently_recomputed'
        execution_evidence['nearest_fields'] = [item.field for item in request.nearest_checks]
    report['execution_evidence'] = execution_evidence
    if inventory != _source_inventory(source, request.source_layer):
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
    return {'analysis_ref': str(target), 'package_path': str(target),
            'manifest_path': str(directory / 'manifest.json'),
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
    inventory = _source_inventory(source, request.source_layer)
    if inventory != report.get('source_inventory'):
        raise ValueError('结果包源数据或伴随文件已经变化')
    actual = artifact_facts(source, key=request.key, numeric_fields=request.numeric_fields,
                            group_by=request.group_by, predicates=request.predicates,
                            missing_by=request.missing_by,
                            include_median=True, source_layer=request.source_layer)
    if (actual != report['facts'] or report['selection'] != _selection(
            request, actual, inventory, _members(source, request.key, request.source_layer))):
        raise ValueError('结果包的统计或成员与当前源文件不一致，须重建')
    if report.get('units') != request.units or report.get('method') != request.method:
        raise ValueError('结果包的单位或方法声明不一致')
    for check in report.get('carry_fields', []):
        baseline = Path(check['source'])
        if guard:
            baseline = guard.read_path(str(baseline))
        if _source_inventory(baseline, check.get('baseline_layer', '')) != check[
                'source_inventory']:
            raise ValueError('字段连续性基线已变化，不能复用结果包')
        if _compare_fields(source, baseline, check['key'], check['fields'],
                check['field_changes'], check.get('source_layer', ''),
                check.get('baseline_layer', '')) != check:
            raise ValueError('字段连续性证据与当前结果不一致')
    evidence = report.get('execution_evidence', {})
    if request.nearest_checks:
        # Reuse is bound to exact reference bytes as well as the output values.
        previous = report.get('nearest_checks', [])
        if len(previous) != len(request.nearest_checks):
            raise ValueError('最近距离复核证据缺失')
        references = {check.reference_ref: saved['reference']
                      for check, saved in zip(request.nearest_checks, previous)}
        nearest = _nearest_checks(source, request, lambda ref: (
            guard.read_path(references[ref]) if guard else references[ref]))
        if nearest != previous:
            raise ValueError('距离复核参照来源或结果发生变化，须重建结果包')
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
    views = {(str(Path(item['file_path']).resolve()), item.get('source_layer', ''))
             for item in mapped_sources if isinstance(item, dict) and item.get('file_path')}
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
        layer = locator.get('request', {}).get('source_layer', '')
        if not any(candidate == related_source and same_source_view(Path(candidate), layer, view)
                   for candidate, view in views) and layer:
            continue
        verify_package(path)
        result.append(reference)
    return result


def delivery_packages(metadata, files, project_document, resolve):
    """Attach only published facts actually referenced by this selected deliverable."""
    selected = set(files)
    analysis = project_document.get('analysis') or {}
    bound = {str(Path(a['manifest_path']).resolve()) for a in
             analysis.get('analyses', [])
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
        related_files = [path, Path(package['facts']['source']),
                         Path(package['companions']['markdown']['path'])]
        related_files.extend(Path(ref) for check in package.get('nearest_checks', [])
                             for ref in check['reference_inventory'])
        related_files.extend(Path(ref) for ref in package['source_inventory'])
        related_files.extend(Path(ref) for check in package.get('carry_fields', [])
                             for ref in check['source_inventory'])
        for related in related_files:
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
