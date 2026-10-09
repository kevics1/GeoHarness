"""Pixels enter the model, bounded repairs change maps, and failures stay visible."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
from matplotlib.figure import Figure
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ImageBlock, ToolUseBlock
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point, box

from geoharness.cartography.quality import review_map
from geoharness.cartography.renderer import (
    _draw_scalebar,
    _quantile_classes,
    render_layers,
    render_static,
)
from geoharness.config.settings import (
    CartographyConfig,
    GeoConfig,
    MapLayout,
    MapReviewConfig,
    load_geo_config,
)
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool


class Reviewer:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(json.dumps(response)),
            usage=UsageSnapshot(),
        )


PASS = {"status": "passed", "summary": "Visible layers and readable legend.", "issues": []}
FIX = {
    "status": "issues", "summary": "Legend obscures points.",
    "issues": [{"severity": "warning", "description": "Legend overlap."}],
    "layout_patch": {"legend_outside": True, "width": 12},
}


@pytest.mark.asyncio
async def test_known_layout_collision_blocks_visual_call_before_repair(tmp_path):
    from geoharness.cartography.layout import create_map_figure, save_map_figure

    reviewer = Reviewer([PASS])
    page = create_map_figure(MapLayout(), title="Risk map", detail=True)
    page.detail.set_position(page.main.get_position())
    image = tmp_path / "map.png"
    save_map_figure(page, image)
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Inspect overlaps", map_id="risk",
    ), context(tmp_path, reviewer))
    assert result.metadata["layout_status"] == "issues"
    assert result.metadata["review_status"] == "issues"
    assert reviewer.requests == []
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    assert report["image_sha256"]
    assert report["visual_review_status"] == "unverified"
    assert any(i["location"] == "panel_overlaps" for i in report["issues"])


@pytest.mark.asyncio
async def test_stale_layout_report_cannot_prove_a_different_image(tmp_path):
    from PIL import Image

    reviewer = Reviewer([PASS])
    image = tmp_path / "map.png"
    Image.new("RGB", (20, 20), "white").save(image)
    image.with_suffix(".layout.json").write_text(json.dumps({
        "scope": "layout_geometry", "image_sha256": "0" * 64,
        "status": "passed", "panel_overlaps": [],
    }), encoding="utf-8")
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Inspect", map_id="risk",
    ), context(tmp_path, reviewer))
    assert result.metadata["layout_status"] == "unverified"
    assert len(reviewer.requests) == 1


def data() -> str:
    return gpd.GeoDataFrame(
        {"value": [1, 2]}, geometry=[Point(114, 30), Point(114.1, 30.1)], crs=4326,
    ).to_json()


def context(tmp_path, reviewer=None, *, repairs=2):
    return ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(
            cartography=CartographyConfig(static_renderer="matplotlib"),
            map_review=MapReviewConfig(max_repairs=repairs),
        ),
        **({"map_review_client": reviewer} if reviewer else {}),
    })


@pytest.mark.asyncio
async def test_style_patch_without_a_concrete_issue_does_not_overturn_visual_pass(tmp_path):
    from PIL import Image

    image = tmp_path / "map.png"
    Image.new("RGB", (20, 20), "white").save(image)
    response = {**PASS, "layout_patch": {"legend_font_size": 13}}
    review = await review_map(image, config=GeoConfig(), task="Inspect", evidence={},
                              client=Reviewer([response]))
    assert review.status == "passed"
    assert review.layout_patch.model_dump(exclude_none=True) == {}
    assert review.diagnostics["ignored_layout_patch"] == {"legend_font_size": 13}


@pytest.mark.asyncio
async def test_pixels_and_evidence_reach_review_and_real_map_is_repaired(tmp_path):
    reviewer = Reviewer([FIX, PASS])
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), field_name="value", output_format="png",
        output_path=str(tmp_path / "map.png"), title="居民点", review_task="检查居民点可读性",
    ), context(tmp_path, reviewer))
    assert not result.is_error, result.output
    assert result.metadata["review_status"] == "passed"
    assert result.metadata["repair_count"] == 1
    assert result.metadata["feature_count"] == 2
    assert len(reviewer.requests) == 2
    for request in reviewer.requests:
        image = next(b for b in request.messages[0].content if isinstance(b, ImageBlock))
        assert image.media_type == "image/png" and len(image.data) > 100
        assert "检查居民点可读性" in request.messages[0].text
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    snapshot = Path(report["history"][0]["map_snapshot"])
    assert snapshot.read_bytes() != (tmp_path / "map.png").read_bytes()
    assert report["history"][1]["layout"]["legend_outside"] is True


@pytest.mark.asyncio
async def test_repairs_stop_at_configured_limit_with_remaining_issues(tmp_path):
    reviewer = Reviewer([FIX, {**FIX, "layout_patch": {"height": 10}}])
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="full", layers=json.dumps([{"geojson": data(), "label": "居民点"}]),
        output_format="png", output_path=str(tmp_path / "map.png"),
    ), context(tmp_path, reviewer, repairs=1))
    assert result.metadata["review_status"] == "issues"
    assert result.metadata["repair_count"] == 1
    assert len(reviewer.requests) == 2


@pytest.mark.asyncio
async def test_api_failure_never_passes_and_preserves_map(tmp_path):
    reviewer = Reviewer([RuntimeError("provider unavailable")])
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), output_format="png",
        output_path=str(tmp_path / "map.png"),
    ), context(tmp_path, reviewer))
    assert not result.is_error
    assert result.metadata["review_status"] == "unverified"
    assert (tmp_path / "map.png").is_file()


@pytest.mark.asyncio
async def test_review_without_credentials_is_unverified(tmp_path):
    from PIL import Image

    path = tmp_path / "map.png"
    Image.new("RGB", (10, 10)).save(path)
    result = await review_map(path, config=GeoConfig(), task="map", evidence={})
    assert result.status == "unverified"


@pytest.mark.asyncio
async def test_exhausted_provider_token_budget_has_actionable_unverified_status(tmp_path):
    from PIL import Image

    class ExhaustedReviewer:
        async def stream_message(self, request):
            yield ApiMessageCompleteEvent(
                message=ConversationMessage.from_user_text(""), usage=UsageSnapshot(),
                stop_reason="length",
            )

    path = tmp_path / "map.png"
    Image.new("RGB", (10, 10)).save(path)
    result = await review_map(path, config=GeoConfig(), task="map", evidence={},
                              client=ExhaustedReviewer())
    assert result.status == "unverified"
    assert "max_tokens" in result.summary
    assert result.diagnostics["finish_reason"] == "length"
    assert result.diagnostics["duration_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason, expected", [("tool_calls", "passed"), ("length", "unverified")],
)
@pytest.mark.parametrize("options", [{}, {"thinking": {"type": "disabled"}}])
async def test_owned_reviewer_passes_provider_options_and_records_transport_evidence(
    tmp_path, monkeypatch, finish_reason, expected, options,
):
    from PIL import Image

    from geoharness.cartography import quality

    requests = []
    sdk_options = []
    closed = []

    async def create(**params):
        requests.append(params)
        return SimpleNamespace(
            model="provider-returned-model",
            usage=SimpleNamespace(
                prompt_tokens=80, completion_tokens=42, total_tokens=122,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=17),
            ),
            choices=[SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                    function=SimpleNamespace(name="record_map_review", arguments=json.dumps(PASS)),
                )]),
            )],
        )

    class Transport:
        chat = SimpleNamespace(completions=SimpleNamespace(create=create))

        def with_options(self, **options):
            sdk_options.append(options)
            return self

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(quality, "OpenAICompatibleClient", lambda **kwargs: SimpleNamespace(
        _client=Transport(),
    ))
    path = tmp_path / "map.png"
    Image.new("RGB", (10, 10)).save(path)
    config = GeoConfig(
        model="configured-main-model", api_key="test-only",
        map_review=MapReviewConfig(request_extra_body=options),
    )
    result = await review_map(path, config=config, task="Check", evidence={})
    assert result.status == expected
    assert requests[0]["model"] == config.model
    assert requests[0]["stream"] is False
    if options:
        assert requests[0]["extra_body"] == options
    else:
        assert "extra_body" not in requests[0]
    assert requests[0]["tool_choice"] == "required"
    assert "diagnostics" not in requests[0]["tools"][0]["function"]["parameters"]["properties"]
    assert sdk_options == [{"max_retries": 0}]
    assert closed == [True]
    expected_diagnostics = {
        "requested_model": config.model, "response_model": "provider-returned-model",
        "input_tokens": 80, "output_tokens": 42, "total_tokens": 122,
        "reasoning_tokens": 17, "finish_reason": finish_reason,
        "duration_seconds": result.diagnostics["duration_seconds"],
        **({"raw_review_status": "passed"} if finish_reason != "length" else {}),
    }
    assert {key: result.diagnostics[key] for key in expected_diagnostics} == expected_diagnostics
    assert result.diagnostics["request_sent"] is True
    assert result.diagnostics["stage"] == ("budget" if finish_reason == "length" else "schema")
    if finish_reason == "length":
        assert "unverified" in result.summary
        assert "thinking" in result.summary


def test_review_options_inherit_provider_defaults_unless_explicitly_configured():
    assert MapReviewConfig().request_extra_body == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,expected", [(PASS, "passed"), (
    {**FIX, "layout_patch": {"distance_m": 300}}, "unverified",
)])
async def test_structured_review_calls_are_validated_without_executing_tools(
    tmp_path, payload, expected,
):
    from PIL import Image

    class StructuredReviewer:
        async def stream_message(self, request):
            assert request.tools[0]["name"] == "record_map_review"
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[
                    ToolUseBlock(name="record_map_review", input=payload),
                ]), usage=UsageSnapshot(), stop_reason="tool_calls",
            )

    path = tmp_path / "map.png"
    Image.new("RGB", (10, 10)).save(path)
    checked = await review_map(path, config=GeoConfig(), task="map", evidence={},
                               client=StructuredReviewer())
    assert checked.status == expected


@pytest.mark.asyncio
async def test_default_map_export_can_be_read_as_python_input(tmp_path, monkeypatch):
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = GeoConfig(
        cartography=CartographyConfig(static_renderer="matplotlib"),
        map_review=MapReviewConfig(enabled=False),
    )
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": cfg})
    mapped = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), output_format="png",
    ), ctx)
    assert not mapped.is_error, mapped.output
    path = Path(mapped.metadata["output_path"])
    assert path.parent == tmp_path / "exports"
    read = await GeoPythonTool().execute(GeoPythonInput(code=(
        "from PIL import Image\n"
        f"with Image.open({str(path)!r}) as image:\n"
        "    print('map pixels', image.width, image.height)\n"
    )), ctx)
    assert not read.is_error, read.output
    assert "map pixels" in read.metadata["stdout"]


@pytest.mark.asyncio
async def test_data_change_patch_is_rejected(tmp_path):
    from PIL import Image

    path = tmp_path / "map.png"
    Image.new("RGB", (10, 10)).save(path)
    result = await review_map(path, config=GeoConfig(), task="map", evidence={},
                              client=Reviewer([{**FIX, "layout_patch": {"field_name": "other"}}]))
    assert result.status == "unverified"


@pytest.mark.asyncio
async def test_pdf_checks_png_preview_and_preserves_pdf(tmp_path):
    reviewer = Reviewer([PASS])
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), output_format="pdf",
        output_path=str(tmp_path / "map.pdf"),
    ), context(tmp_path, reviewer))
    assert result.metadata["review_status"] == "passed"
    assert Path(result.metadata["preview_path"]).read_bytes().startswith(b"\x89PNG")
    assert (tmp_path / "map.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_html_cannot_claim_actual_page_was_checked(tmp_path):
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), output_format="html",
        output_path=str(tmp_path / "map.html"),
    ), context(tmp_path, Reviewer([PASS])))
    assert result.metadata["review_status"] == "unverified"


@pytest.mark.asyncio
async def test_standalone_review_writes_report(tmp_path):
    from PIL import Image

    path = tmp_path / "python-map.png"
    Image.new("RGB", (20, 20), "white").save(path)
    result = await GeoMapReviewTool().execute(
        GeoMapReviewInput(file_path=str(path), task="检查地图"),
        context(tmp_path, Reviewer([PASS])),
    )
    assert result.metadata["review_status"] == "passed"
    assert Path(result.metadata["review_path"]).is_file()


def test_quantile_class_numbers_include_first_and_last_class():
    result = _quantile_classes(np.arange(30.0), 3)
    assert set(result) == {0, 1, 2}
    assert result[0] == 0 and result[-1] == 2


def test_static_layers_align_different_crs_and_keep_small_extent(tmp_path, monkeypatch):
    boundary = gpd.GeoDataFrame(geometry=[box(114, 30, 114.2, 30.2)], crs=4326)
    points = gpd.GeoDataFrame(geometry=[Point(114.1, 30.1)], crs=4326).to_crs(3857)
    extents = []
    original = Figure.savefig

    def inspect(figure, *args, **kwargs):
        extents.append(figure.axes[0].get_xlim())
        return original(figure, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", inspect)
    render_layers(
        [{"gdf": boundary, "label": "boundary"}, {"gdf": points, "label": "points"}],
        output_path=tmp_path / "map.png",
    )
    assert 0 < extents[0][1] - extents[0][0] < 1


def test_new_config_validates_limits_and_expands_vision_fields(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TEST_VISION_MODEL", "configured-model")
    path.write_text('map_review:\n  model: ${TEST_VISION_MODEL}\n  max_repairs: 1\n',
                    encoding="utf-8")
    assert load_geo_config(path).map_review.model == "configured-model"
    path.write_text('map_review:\n  max_repairs: -1\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_geo_config(path)
    with pytest.raises(ValidationError):
        MapReviewConfig(max_repairs=3)


@pytest.mark.asyncio
async def test_review_artifacts_preserve_sources_and_cache_basemap(tmp_path, monkeypatch):
    import rasterio
    from rasterio.transform import from_bounds

    basemap = tmp_path / "map.preview.png"
    with rasterio.open(
        basemap, "w", driver="PNG", height=20, width=20, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_bounds(113.9, 29.9, 114.2, 30.2, 20, 20),
    ) as dataset:
        dataset.write(np.full((1, 20, 20), 100, dtype="uint8"))
    protected = [basemap, tmp_path / "map.attempt-0.pdf", tmp_path / "map.pdf.review.json"]
    for path in protected[1:]:
        path.write_bytes(b"existing user file")
    originals = {path: path.read_bytes() for path in protected}
    original_open = rasterio.open
    reads = []

    def count_read(*args, **kwargs):
        reads.append(args[0])
        return original_open(*args, **kwargs)

    monkeypatch.setattr(rasterio, "open", count_read)
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        layers=json.dumps([{"geojson": data(), "label": "assets"}]),
        output_format="pdf", output_path="map.pdf", basemap_raster="map.preview.png",
    ), context(tmp_path, Reviewer([FIX, PASS])))
    assert not result.is_error, result.output
    assert result.metadata["repair_count"] == 1
    assert result.metadata["reviewed_artifact"] == "static_png_preview"
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert Path(result.metadata["preview_path"]).parent != tmp_path
    # Preflight reads its header once; exports and the project reuse the pixel cache.
    assert sum(Path(path).resolve() == basemap.resolve() for path in reads) == 2


@pytest.mark.asyncio
async def test_relative_vector_file_uses_execution_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    gpd.GeoDataFrame(geometry=[Point(114, 30)], crs=4326).to_file(data_dir / "assets.geojson")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", file_path="data/assets.geojson",
        output_format="png", output_path="map.png",
    ), context(tmp_path))
    assert not result.is_error, result.output
    assert result.metadata["feature_count"] == 1


@pytest.mark.asyncio
async def test_disabled_review_skips_unused_preview(tmp_path):
    ctx = context(tmp_path)
    ctx.metadata["geoharness_config"] = GeoConfig(
        cartography=CartographyConfig(static_renderer="matplotlib"),
        map_review=MapReviewConfig(enabled=False),
    )
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", geojson=data(), output_format="pdf", output_path="map.pdf",
    ), ctx)
    assert not result.is_error
    assert result.metadata["review_status"] == "unverified"
    assert result.metadata["preview_path"] is None


def test_single_layer_layout_changes_visible_lines_and_polygon_opacity(tmp_path):
    from shapely.geometry import LineString

    for geometry, field, first, second in (
        (LineString([(114, 30), (114.1, 30.1)]), "line_width", 0.2, 8),
        (box(114, 30, 114.1, 30.1), "polygon_alpha", 0.1, 1),
    ):
        gdf = gpd.GeoDataFrame(geometry=[geometry], crs=4326)
        paths = [tmp_path / f"{field}-{index}.png" for index in range(2)]
        for path, value in zip(paths, (first, second)):
            render_static(gdf, output_path=path, layout=MapLayout(**{field: value}))
        assert paths[0].read_bytes() != paths[1].read_bytes()


@pytest.mark.parametrize("crs,unit_to_m", [(32650, 1), (2263, 1200 / 3937)])
def test_scalebar_distance_matches_canvas_units_including_margins(crs, unit_to_m):
    import matplotlib.pyplot as plt

    gdf = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=crs)
    figure, axis = plt.subplots()
    try:
        axis.set_xlim(-50, 950)
        axis.set_ylim(-50, 150)
        _draw_scalebar(axis, gdf)
        label_m = float(axis.texts[0].get_text().removesuffix(" m"))
        displayed_m = axis.patches[0].get_width() * 1000 * unit_to_m
        assert displayed_m == pytest.approx(label_m)
    finally:
        plt.close(figure)
