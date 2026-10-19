from geoharness.cartography.quality import MapReview, distinguish_pixel_findings


def test_semantic_question_does_not_become_a_confirmed_map_defect():
    review = MapReview.model_validate({'status': 'issues', 'summary': '名称类型需核实',
        'issues': [{'severity': 'warning', 'description': '书房名称但类型是学校',
                    'evidence_kind': 'semantic'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'passed'
    assert review.issues[0].severity == 'info'
    assert review.diagnostics['nonvisual_observations'][0]['severity'] == 'warning'


def test_visible_occlusion_still_blocks_with_a_style_suggestion_present():
    review = MapReview.model_validate({'status': 'issues', 'summary': '文字遮挡',
        'issues': [{'severity': 'warning', 'description': '文字被截断', 'issue_code': 'other'},
                   {'severity': 'warning', 'description': '更喜欢单位只写在表头',
                    'evidence_kind': 'suggestion'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'issues'
    assert review.issues[0].severity == 'warning'


def test_uncertain_visual_review_is_not_promoted_by_nonvisual_advice():
    review = MapReview.model_validate({'status': 'unverified', 'summary': '看不清',
        'issues': [{'severity': 'warning', 'description': '字段含义需核实',
                    'evidence_kind': 'semantic'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'unverified'
