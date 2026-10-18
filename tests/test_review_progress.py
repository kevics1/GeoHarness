from geoharness.cartography.review_progress import assess_repair_progress


def test_only_repeated_located_unchanged_concerns_request_user_decision():
    current = {'status': 'issues', 'image_sha256': 'new', 'issues': [{'severity': 'warning',
        'issue_code': 'label_overlap', 'frame_id': 'main_map', 'roi_ref': 'main_map:1',
        'description': '编号9与10的引线难区分', 'location': '东南点簇'}]}
    history = [{**current, 'image_sha256': 'old1'}, {**current, 'image_sha256': 'old2'}]
    assert assess_repair_progress(history, current)['requires_user_decision']
    assert not assess_repair_progress([current, current], current)['requires_user_decision']
    assert not assess_repair_progress([current], current)['requires_user_decision']
    assert not assess_repair_progress([current, current], {'status': 'passed'})[
        'requires_user_decision']
    changed = {**current, 'issues': [{**current['issues'][0], 'location': '西北点簇'}]}
    assert not assess_repair_progress([current, current], changed)['requires_user_decision']
    unlocated = {**current, 'issues': [{**current['issues'][0], 'location': ''}]}
    assert not assess_repair_progress([unlocated, unlocated], unlocated)['requires_user_decision']
