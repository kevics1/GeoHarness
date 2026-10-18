from geoharness.cartography.review_progress import assess_repair_progress


def test_only_repeated_located_unchanged_concerns_request_user_decision():
    current = {'status': 'issues', 'image_sha256': 'new', 'issues': [{'severity': 'warning',
        'issue_code': 'label_overlap', 'frame_id': 'main_map', 'roi_ref': 'main_map:1',
        'description': '编号9与10的引线难区分', 'location': '东南点簇'}]}
    history = [{**current, 'image_sha256': 'old1'}, {**current, 'image_sha256': 'old2'}]
    progress = assess_repair_progress(history, current)
    assert progress['state'] == 'persistent_concerns'
    assert progress['next_action'] == 'change_repair_method'
    assert not progress['requires_user_decision']
    assert not assess_repair_progress([current, current], current)['requires_user_decision']
    assert not assess_repair_progress([current], current)['requires_user_decision']
    assert not assess_repair_progress([current, current], {'status': 'passed'})[
        'requires_user_decision']
    changed = {**current, 'issues': [{**current['issues'][0], 'location': '西北点簇'}]}
    assert assess_repair_progress(history, changed)['state'] == 'not_established'
    unlocated = {**current, 'issues': [{**current['issues'][0], 'location': ''}]}
    assert not assess_repair_progress([unlocated, unlocated], unlocated)['requires_user_decision']


def test_wording_changes_and_other_new_issues_do_not_erase_persistent_region():
    def review(digest, description, box, *, page=0, severity='warning'):
        return {'status': 'issues', 'image_sha256': digest,
            'diagnostics': {'review_page_index': page}, 'issues': [{
                'issue_code': 'label_overlap', 'severity': severity, 'frame_id': 'main_map',
                'roi_ref': 'main_map', 'bbox': box, 'description': description}]}

    old = review('one', '编号挤在一起', [.1, .1, .3, .3])
    previous = review('two', '标签拥挤需要看清', [.11, .1, .31, .3])
    current = review('three', '部分数字难分辨', [.1, .11, .3, .31])
    current['issues'].append({'issue_code': 'legend_mismatch', 'severity': 'warning'})
    assert assess_repair_progress([old, previous], current)['repeated_concern_count'] == 1
    previous['diagnostics']['review_page_index'] = 1
    assert assess_repair_progress([old, previous], current)['repeated_concern_count'] == 0
    previous['diagnostics']['review_page_index'] = 0
    previous['status'] = 'passed'
    assert assess_repair_progress([old, previous], current)['repeated_concern_count'] == 0
    previous['diagnostics']['review_page_index'] = 0
    old['issues'][0]['severity'] = previous['issues'][0]['severity'] = 'error'
    assert assess_repair_progress([old, previous], current)['repeated_concern_count'] == 0
