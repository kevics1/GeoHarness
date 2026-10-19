import json

import pytest
from test_data_refs import _native
from test_managed_inputs import setup_context

from geoharness.data.task_context import (
    data_ref_snapshot,
    model_data_context,
    resolve_registered_path,
)


def test_issued_artifact_survives_context_rotation_and_process_restore(tmp_path, monkeypatch):
    context, _ = setup_context(tmp_path)
    config = context.metadata['geoharness_config']
    manifest = _native(tmp_path, config)
    visible = json.loads(model_data_context(context.metadata, cwd=tmp_path,
                                           config=config).split('\n')[-1])
    ref = next(ref for ref, item in visible['data_refs'].items()
               if item['path'] == manifest['paths']['result']['geojson'])
    monkeypatch.setattr('geoharness.data.task_context.recent_outputs', lambda *a, **k: [])
    model_data_context(context.metadata, cwd=tmp_path, config=config)
    assert ref not in context.metadata['_data_refs_snapshot']
    assert resolve_registered_path(ref, context.metadata, cwd=tmp_path, config=config) == (
        manifest['paths']['result']['geojson'])
    fresh = {'geoharness_config': config,
             'geo_task_record': context.metadata['geo_task_record'], '_data_refs_snapshot': {}}
    assert ref in data_ref_snapshot(fresh, cwd=tmp_path, config=config)
    assert resolve_registered_path(ref, fresh, cwd=tmp_path, config=config) == (
        manifest['paths']['result']['geojson'])
    from pathlib import Path

    Path(manifest['paths']['result']['geojson']).write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='stale|changed'):
        resolve_registered_path(ref, fresh, cwd=tmp_path, config=config)
