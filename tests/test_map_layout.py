"""Layout checks exercise rendered text bounds, not guessed pixel colors."""

import matplotlib.pyplot as plt

from geoharness.cartography.layout import (
    create_map_figure,
    inspect_figure_layout,
    place_point_labels,
    save_map_figure,
)
from geoharness.config.settings import MapLayout


def test_detail_legend_footer_are_separate_even_with_equal_map_aspect(tmp_path):
    page = create_map_figure(MapLayout(), title="Risk map", detail=True)
    for ax in (page.main, page.detail):
        ax.set_xlim(0, 20)
        ax.set_ylim(0, 100)
        ax.set_aspect("equal")
    page.legend.text(0, 1, "Legend", va="top")
    page.footer.text(0, 1, "Source and CRS", va="top")
    report = save_map_figure(page, tmp_path / "map.png")
    assert report["status"] == "passed"
    assert report["panel_overlaps"] == []
    assert report["image_sha256"]
    assert (tmp_path / "map.layout.json").is_file()
    plt.close(page.figure)


def test_clustered_labels_avoid_each_other_and_stay_inside_actual_map():
    page = create_map_figure(MapLayout(), title="Cluster", detail=False)
    ax = page.main
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    points = [(50 + i * .1, 50 + i * .1, f"Priority {i}") for i in range(7)]
    labels = place_point_labels(ax, points, font_size=9, max_offset_points=110)
    assert len(labels) == len(points)
    assert all(label["placed"] for label in labels)
    report = inspect_figure_layout(page)
    assert report["label_overlaps"] == []
    assert report["clipped_labels"] == []
    plt.close(page.figure)


def test_inspector_rejects_the_actual_overlay_pattern():
    page = create_map_figure(MapLayout(), title="Risk map", detail=True)
    page.detail.set_position(page.main.get_position())
    report = inspect_figure_layout(page)
    assert report["status"] == "issues"
    assert report["panel_overlaps"]
    plt.close(page.figure)


def test_report_does_not_claim_semantic_or_visual_model_verification(tmp_path):
    page = create_map_figure(MapLayout(), title="Only layout geometry")
    report = save_map_figure(page, tmp_path / "map.png")
    assert report["scope"] == "layout_geometry"
    assert report["visual_review_status"] == "unverified"
    assert report["data_correctness_status"] == "unverified"
    plt.close(page.figure)


def test_title_overflow_is_detected_before_visual_review():
    page = create_map_figure(MapLayout(width=4, title_font_size=32), title="X" * 100)
    report = inspect_figure_layout(page)
    assert report["status"] == "issues"
    assert report["clipped_figure_text"]
    plt.close(page.figure)


def test_multiline_title_cannot_cover_subtitle():
    page = create_map_figure(MapLayout(), title="First line\nSecond line", subtitle="Source")
    report = inspect_figure_layout(page)
    assert report["status"] == "issues"
    assert report["figure_text_overlaps"]
    plt.close(page.figure)


def test_panel_partially_outside_canvas_is_rejected():
    page = create_map_figure(MapLayout(), title="Map")
    page.main.set_position([-.2, .32, .4, .54])
    report = inspect_figure_layout(page)
    assert report["status"] == "issues"
    assert report["panels_outside_canvas"] == ["main"]
    plt.close(page.figure)
