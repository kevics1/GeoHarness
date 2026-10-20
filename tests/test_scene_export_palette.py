import pytest

from geoharness.qgis.scene_export import frozen_palette


def test_old_reviewed_locator_palette_uses_exact_literal_without_mutating_plan():
    plan = {'colors': {'#ffffff': '255,255,255,255'}, 'pages': [{'legend_items': [
        {'color': '#ffffff', 'symbol': {'outline': '#222222'}}]}]}
    assert frozen_palette(plan)['#222222'] == '34,34,34,255'
    assert '#222222' not in plan['colors']


def test_missing_nonliteral_color_is_not_guessed():
    with pytest.raises(ValueError, match='Missing frozen'):
        frozen_palette({'colors': {}, 'pages': [{'legend_items': [
            {'color': 'unknown-color', 'symbol': {'outline': '#222222'}}]}]})
