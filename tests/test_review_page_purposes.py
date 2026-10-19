"""Page-role checks preserve native pixel review and do not invent missing furniture."""

from geoharness.cartography.pixel_review import page_scope, pixel_prompt


def test_roster_role_uses_bound_metadata_not_page_number():
    evidence = {'render_metadata': {'review_page_index': 4, 'page_previews': [
        {'page_index': 4, 'role': 'roster', 'frames': [], 'path': 'table.png'}]}}
    prompt = pixel_prompt('检查主图、定位图和编号', evidence)
    assert page_scope(evidence)['page_role'] == 'roster'
    assert '不得要求本页出现主图' in prompt and '表头' in prompt
    assert '简体中文' in prompt
    assert page_scope({'render_metadata': {'review_page_index': 4}})['page_role'] == 'unknown'


def test_legacy_roster_requires_actual_roster_preview_binding():
    metadata = {'review_page_index': 1, 'page_previews': [
        {'page_index': 1, 'frames': [], 'path': 'table.png'}],
        'native_facts': {'roster_previews': ['table.png']}}
    assert page_scope({'render_metadata': metadata})['page_role'] == 'roster'
    metadata['native_facts']['roster_previews'] = ['other.png']
    assert page_scope({'render_metadata': metadata})['page_role'] == 'unknown'


async def test_pixel_cache_reuses_unchanged_page_but_not_changed_purpose(tmp_path, monkeypatch):
    from PIL import Image

    from geoharness.cartography import workflow
    from geoharness.cartography.quality import MapReview
    from geoharness.config.settings import GeoConfig

    path = tmp_path / 'page.png'
    Image.new('RGB', (40, 40), 'white').save(path)
    calls = []

    async def review(*args, **kwargs):
        calls.append(kwargs)
        return MapReview(status='passed', summary='合成审查',
                         diagnostics={'request_sent': True, 'outcome': 'completed'})

    monkeypatch.setattr(workflow, 'review_map', review)
    metadata = {}

    def evidence(version, role='roster'):
        return {'backend': 'qgis', 'review_binding': {'version': version},
            'render_metadata': {'project_path': f'run-{version}/map.qgz',
                'review_page_index': 1, 'page_previews': [
                    {'page_index': 1, 'role': role, 'path': f'run-{version}/page.png'}]}}

    kwargs = {'config': GeoConfig(), 'task': '检查名单', 'metadata': metadata, 'pixel_only': True}
    await workflow.review_with_budget(path, evidence=evidence(1), **kwargs)
    _, budget = await workflow.review_with_budget(path, evidence=evidence(2), **kwargs)
    assert len(calls) == 1 and budget['review_cached']
    await workflow.review_with_budget(path, evidence=evidence(3, 'map'), **kwargs)
    assert len(calls) == 2
    await workflow.review_with_budget(path, evidence=evidence(3, 'map'),
                                     **{**kwargs, 'task': '检查图例'})
    assert len(calls) == 2  # Prose alone cannot create a new pixel check.
    await workflow.review_with_budget(path,
        evidence={**evidence(3, 'map'), 'review_checks': ['检查图例与本页类别是否对应']}, **kwargs)
    assert len(calls) == 3
