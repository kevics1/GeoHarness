import hashlib
import json
from pathlib import Path

from openharness.tools.base import ToolExecutionContext

from geoharness.cartography.pixel_review import pixel_prompt
from geoharness.cartography.quality import MapReview
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
from geoharness.tools.geo_map_review import GeoMapReviewInput, review_goal, review_pixels
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


def test_bundle_follows_published_relations_without_exporting_external_companions(tmp_path):
    from geoharness.cartography.analysis_bundle import complete_analysis_bundle

    run = tmp_path / 'run'
    run.mkdir()
    source = run / 'result.csv'
    source.write_text('id,value\n1,2\n', encoding='utf-8')
    external = tmp_path / 'external.md'
    external.write_text('unrelated user file', encoding='utf-8')
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    report = run / 'facts.json'
    report.write_text(json.dumps({'kind': 'persisted_analysis_report',
        'facts': {'source': str(source), 'sha256': digest(source)},
        'companions': {'markdown': {'path': str(external), 'sha256': digest(external)}}}),
        encoding='utf-8')
    manifest = run / 'manifest.json'
    manifest.write_text(json.dumps({'producer': 'geo_python', 'status': 'succeeded',
        'run_dir': str(run), 'files': [{'path': str(p), 'sha256': digest(p)}
                                      for p in (source, report, external)]}), encoding='utf-8')
    files, _ = complete_analysis_bundle([source], lambda p: Path(p))
    assert files == [source, report] and external not in files
    # A malformed optional relation never becomes an arbitrary filesystem read.
    raw = json.loads(report.read_text(encoding='utf-8'))
    raw['companions'] = ['not a declared relation']
    report.write_text(json.dumps(raw), encoding='utf-8')
    saved = json.loads(manifest.read_text(encoding='utf-8'))
    saved['files'][1]['sha256'] = digest(report)
    manifest.write_text(json.dumps(saved), encoding='utf-8')
    assert complete_analysis_bundle([source], lambda p: Path(p)) == ([source], [])


def test_review_uses_same_goal_when_omitted_but_honors_changed_requirements():
    state, config = {}, GeoConfig()
    assert review_goal('检查编号与符号', state, 'map', config) == '检查编号与符号'
    assert review_goal('', state, 'map', config) == '检查编号与符号'
    assert review_goal('检查表格可读性', state, 'map', config) == '检查表格可读性'
    assert review_goal('', state, 'other-map', config) == config.map_review.default_task
    text = pixel_prompt('检查名单', {'render_metadata': {'review_page_index': 1,
        'page_previews': [{'page_index': 1, 'role': 'roster'},
                          {'page_index': 2, 'role': 'roster'}]}})
    assert '不要求本页独自包含全部行' in text and 'roster_page_indices' in text


async def test_only_response_format_failure_gets_one_local_recovery(tmp_path, monkeypatch):
    from geoharness.tools import geo_map_review

    calls = []

    async def fake(path, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return MapReview(status='unverified', summary='bad JSON', diagnostics={
                'failure_stage': 'json'}), {'request_sent': True}
        return MapReview(status='passed', summary='readable'), {
            'request_sent': True, 'invalid_response_retry': True}

    monkeypatch.setattr(geo_map_review, 'review_with_budget', fake)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    result, flags = await review_pixels(tmp_path / 'image.png',
        arguments=GeoMapReviewInput(file_path='image.png'), context=context,
        identity='map', evidence={})
    assert result.status == 'passed' and len(calls) == 2
    assert calls[0]['task'] == calls[1]['task']
    assert calls[1]['retry_invalid_response'] is True
    assert flags['format_recovery_request_count'] == 2
    calls.clear()
    await review_pixels(tmp_path / 'image.png', arguments=GeoMapReviewInput(
        file_path='image.png', retry_invalid_response=False), context=context,
        identity='map', evidence={})
    assert len(calls) == 1


async def test_delivering_only_markdown_attaches_declared_report_and_actual_source(tmp_path):
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(name='report_bundle', code='''
import pandas as pd
source = RUN_DIR/'result.csv'
pd.DataFrame({'id':[1,2,3], 'type':['学校','住宅区','学校'], 'tier':['一级','一级','二级'],
              'distance_m':[1284.1,7621.2,5445.2]}).to_csv(source,index=False)
write_analysis_report(RUN_DIR/'facts.json',source=source,key='id',numeric_fields=['distance_m'],
 group_by=['tier','type'],stratify_by=['tier','type'],method={'scope':'测试数据，阈值为假设'})
''', expected_outputs=['facts.json', 'facts.md', 'result.csv']), context)
    assert not result.is_error, result.output
    run = result.metadata['run_dir']
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[str(run) + '/facts.md'], name='配套报告测试'), context)
    assert not delivered.is_error, delivered.output
    payload = json.loads(delivered.output)
    summary = json.loads(open(payload['summary_json'], encoding='utf-8').read())
    facts = summary['final_answer_facts'][0]
    assert facts['numeric']['distance_m']['max'] == 7621.2
    assert facts['numeric']['distance_m']['median'] == 5445.2
    assert any(s['group'] == {'tier': '一级', 'type': '住宅区'} and s['row_count'] == 1
               for s in facts['strata'])
    assert summary['analysis_correctness'] == 'unverified'
    assert summary['attached_analysis_reports'][0]['status'] == 'persisted_values_checked'
    paths = payload['files']
    assert any(p.endswith('facts.json') for p in paths)
    assert any(p.endswith('result.csv') for p in paths)
    report = json.loads(open(next(p for p in paths if p.endswith('facts.json')),
                             encoding='utf-8').read())
    md = next(p for p in paths if p.endswith('facts.md'))
    assert hashlib.sha256(open(md, 'rb').read()).hexdigest() == (
        report['companions']['markdown']['sha256'])
