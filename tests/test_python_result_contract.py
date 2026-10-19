import json

import pytest
from test_python_execution import run_script

from geoharness.execution.contracts import preflight_code, write_analysis_report


async def test_plain_python_returns_structured_json_without_dummy_artifact(tmp_path):
    result = await run_script(tmp_path, "CONSOLE_RESULT={'rows':13,'fields':['id','distance_m']}")
    assert result['status'] == 'succeeded', result
    assert result['result']['console_result'] == {'rows': 13, 'fields': ['id', 'distance_m']}
    assert result['files'] == []
    assert json.loads(__import__('pathlib').Path(result['manifest_path']).read_text(
        encoding='utf-8'))['result'] == result['result']


async def test_return_name_is_configurable_and_invalid_native_result_is_actionable(tmp_path):
    good = await run_script(tmp_path, "MY_RESULT={'count':2}", result_variable='MY_RESULT')
    assert good['result']['console_result'] == {'count': 2}
    bad = await run_script(tmp_path, 'CONSOLE_RESULT=object()')
    assert bad['status'] == 'failed'
    assert 'must contain JSON values' in bad['stderr']


def test_markdown_report_request_generates_both_explicit_paths(tmp_path):
    source = tmp_path / 'rows.csv'
    source.write_text('id,value\na,1\nb,2\n', encoding='utf-8')
    report = write_analysis_report(tmp_path / '分析报告.md', source=source, key='id',
                                   method={'operation': 'attribute_summary'})
    assert report['facts']['row_count'] == 2
    assert report['paths']['json'].endswith('分析报告.json')
    assert report['paths']['markdown'].endswith('分析报告.md')
    assert (tmp_path / '分析报告.json').is_file()


def test_bad_report_extension_is_caught_before_script_side_effects():
    with pytest.raises(ValueError, match='end in .json or .md'):
        preflight_code("(RUN_DIR/'map.png').write_bytes(b'x')\n"
                       "write_analysis_report(RUN_DIR/'facts.txt',source='rows.csv',"
                       "key='id',method={'operation':'summary'})")
