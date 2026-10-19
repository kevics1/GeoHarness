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


def test_explicit_no_reading_impact_and_other_page_duty_are_observations():
    review = MapReview.model_validate({'status':'issues','summary':'仅描述布局', 'issues':[
        {'severity':'warning','description':'框外留白但主体可读',
         'issue_code':'extent_whitespace','reading_impact':'none'},
        {'severity':'warning','description':'其他时段在详图而非总览',
         'assessment_scope':'other_page','issue_code':'legend_mismatch'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'passed'
    assert len(review.diagnostics['nonvisual_observations']) == 2


def test_uncertain_pixel_impact_is_unverified_and_true_occlusion_remains_blocking():
    review = MapReview.model_validate({'status':'issues','summary':'不能确认小符号', 'issues':[
        {'severity':'warning','description':'缩图看不清', 'reading_impact':'uncertain',
         'affected_content':'编号归属'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'unverified'
    review = MapReview.model_validate({'status':'issues','summary':'确证遮挡', 'issues':[
        {'severity':'warning','description':'文字被符号遮住', 'reading_impact':'hidden',
         'affected_content':'对象编号', 'issue_code':'label_overlap'}]})
    distinguish_pixel_findings(review)
    assert review.status == 'issues' and review.issues[0].severity == 'warning'
