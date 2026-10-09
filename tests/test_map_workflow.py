"""Map checks must share limits across tools and changing filenames."""

import asyncio
import json
from dataclasses import replace

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext
from PIL import Image

from geoharness.cartography.jobs import begin_map_request, ensure_map_job
from geoharness.cartography.workflow import (
    refund_map_render,
    reserve_map_render,
    reserve_python_render,
    reset_map_workflow,
    review_with_budget,
)
from geoharness.config.settings import GeoConfig, MapReviewConfig
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool


class Reviewer:
    def __init__(self):
        self.calls = 0

    async def stream_message(self, request):
        self.calls += 1
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(
                json.dumps(
                    {
                        "status": "unverified",
                        "summary": "No definitive visual conclusion.",
                    }
                )
            ),
            usage=UsageSnapshot(),
        )


@pytest.mark.asyncio
async def test_unchanged_map_is_not_sent_to_model_repeatedly(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)
    reviewer = Reviewer()
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "geoharness_config": GeoConfig(),
            "map_review_client": reviewer,
            "map_workflow_state": {},
        },
    )
    tool = GeoMapReviewTool()
    first = await tool.execute(GeoMapReviewInput(file_path=str(image), task="Check map"), context)
    second = await tool.execute(
        GeoMapReviewInput(file_path=str(image), task="Check again"),
        context,
    )
    assert first.metadata["review_status"] == second.metadata["review_status"] == "unverified"
    assert reviewer.calls == 1
    assert second.metadata["review_cached"] is True


@pytest.mark.asyncio
async def test_changed_names_cannot_reset_request_review_budget(tmp_path):
    reviewer = Reviewer()
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "geoharness_config": GeoConfig(map_review=MapReviewConfig(max_reviews_per_request=1)),
            "map_review_client": reviewer,
            "map_workflow_state": {},
        },
    )
    results = []
    for name, color in (("first", "white"), ("changed-name-v8", "black")):
        image = tmp_path / f"{name}.png"
        Image.new("RGB", (32, 32), color).save(image)
        results.append(
            await GeoMapReviewTool().execute(
                GeoMapReviewInput(file_path=str(image), task="Check map"),
                context,
            )
        )
    assert reviewer.calls == 1
    assert results[-1].metadata["review_budget_exhausted"] is True
    assert not results[-1].is_error


def test_request_reset_preserves_shared_dict_and_starts_new_budget():
    metadata = {"map_workflow_state": {}}
    shallow_copy = dict(metadata)
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    assert reserve_map_render("fire", shallow_copy, config) is None
    assert reserve_map_render("fire", metadata, config) is not None
    reset_map_workflow(metadata)
    assert shallow_copy["map_workflow_state"] is metadata["map_workflow_state"]
    assert reserve_map_render("fire", shallow_copy, config) is None


def test_failed_render_refund_preserves_the_next_valid_attempt():
    metadata = {}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    assert reserve_map_render("fire", metadata, config) is None
    assert refund_map_render("fire", metadata)
    assert not refund_map_render("fire", metadata)
    assert reserve_map_render("fire", metadata, config) is None
    assert reserve_map_render("fire", metadata, config) is not None


@pytest.mark.asyncio
async def test_rendering_and_analysis_time_between_reviews_do_not_expire_review_budget(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)
    metadata = {"map_workflow_state": {"seconds": 1, "started_at": 0}}
    reviewer = Reviewer()
    review, flags = await review_with_budget(
        image,
        config=GeoConfig(),
        task="Check",
        evidence={},
        metadata=metadata,
        client=reviewer,
    )
    assert review.status == "unverified"
    assert not flags["review_budget_exhausted"]
    assert len(flags["image_sha256"]) == 64
    assert reviewer.calls == 1


@pytest.mark.asyncio
async def test_explicit_provider_setting_change_can_recheck_a_cached_unverified_image(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)
    metadata = {}
    reviewer = Reviewer()
    config = GeoConfig()
    results = []
    for current in (
        config,
        config,
        replace(
            config,
            map_review=MapReviewConfig(
                request_extra_body={"thinking": {"type": "disabled"}},
            ),
        ),
    ):
        results.append(
            await review_with_budget(
                image,
                config=current,
                task="Check",
                evidence={},
                metadata=metadata,
                client=reviewer,
            )
        )
    assert reviewer.calls == 2
    assert [flags["review_cached"] for _, flags in results] == [False, True, False]
    assert len({flags["image_sha256"] for _, flags in results}) == 1


@pytest.mark.asyncio
async def test_parallel_distinct_images_reserve_and_release_only_available_review_seconds(
    tmp_path,
    monkeypatch,
):
    from geoharness.cartography import workflow
    from geoharness.cartography.quality import MapReview

    paths = [tmp_path / f"map-{index}.png" for index in range(2)]
    for path, color in zip(paths, ("white", "black")):
        Image.new("RGB", (32, 32), color).save(path)
    metadata = {}
    entered = asyncio.Event()
    release = asyncio.Event()
    timeouts = []

    async def blocking_review(path, *, config, **kwargs):
        timeouts.append(config.map_review.timeout_seconds)
        if len(timeouts) == 2:
            entered.set()
        await release.wait()
        return MapReview(status="unverified", summary="Test review")

    monkeypatch.setattr(workflow, "review_map", blocking_review)
    tasks = [
        asyncio.create_task(
            review_with_budget(
                path,
                config=GeoConfig(),
                task="Check",
                evidence={},
                metadata=metadata,
            )
        )
        for path in paths
    ]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert sorted(timeouts) == [30, 90]
        assert metadata["map_workflow_state"]["reserved_seconds"] == 120
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert metadata["map_workflow_state"]["reserved_seconds"] == 0
    assert metadata["map_workflow_state"]["seconds"] < 1


@pytest.mark.asyncio
async def test_review_time_budget_stops_new_images_without_calling_provider(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)
    metadata = {"map_workflow_state": {"seconds": 121}}
    reviewer = Reviewer()
    review, flags = await review_with_budget(
        image, config=GeoConfig(), task="Check", evidence={}, metadata=metadata, client=reviewer
    )
    assert flags["review_budget_exhausted"]
    assert review.status == "unverified"
    assert reviewer.calls == 0


@pytest.mark.parametrize(
    "code",
    [
        "from matplotlib.pyplot import savefig; savefig('map.png')",
        "from matplotlib.pyplot import savefig as save; save('map.png')",
        "from geoharness.cartography import render_map as draw; draw(data, output_path='map.png')",
        "import geoharness.cartography as cart; cart.render_map(data, output_path='map.png')",
        "import geoharness.cartography; "
        "geoharness.cartography.render_map(data, output_path='map.png')",
        "from geoharness import cartography as cart; "
        "cart.render_layers(data, output_path='map.png')",
        "from geoharness.cartography import save_map_figure as save; save(fig, 'map.png')",
        "import geoharness.cartography as cart; cart.save_map_figure(fig, 'map.png')",
    ],
)
def test_imported_drawing_aliases_share_python_version_budget(code):
    metadata = {}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    kwargs = {
        "code": code,
        "name": "fire",
        "map_id": "fire",
        "metadata": metadata,
        "config": config,
    }
    assert reserve_python_render(**kwargs) is None
    assert reserve_python_render(**kwargs) is not None


@pytest.mark.asyncio
async def test_parallel_checks_of_identical_pixels_share_one_model_request(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)
    reviewer = Reviewer()
    original = reviewer.stream_message

    async def slow(request):
        await asyncio.sleep(0.02)
        async for event in original(request):
            yield event

    reviewer.stream_message = slow
    metadata = {}
    results = await asyncio.gather(
        *(
            review_with_budget(
                image,
                config=GeoConfig(),
                task="Check",
                evidence={},
                metadata=metadata,
                client=reviewer,
            )
            for _ in range(2)
        )
    )
    assert reviewer.calls == metadata["map_workflow_state"]["reviews"] == 1
    assert sorted(flags["review_cached"] for _, flags in results) == [False, True]


def test_request_job_aliases_share_render_budget_and_legacy_entry_identity(tmp_path):
    metadata = {"map_job_state": {}, "map_workflow_state": {}}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    begin_map_request(metadata, cwd=tmp_path, config=config)
    job_id = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="risk")
    assert reserve_map_render(job_id, metadata, config) is None
    assert reserve_map_render("different-name-v9", dict(metadata), config) is not None
    maps = metadata["map_workflow_state"]["maps"]
    assert maps["risk"] is maps[job_id] is maps["different-name-v9"]
    assert maps["risk"]["renders"] == 1
    assert refund_map_render("risk", metadata)
    assert reserve_map_render("another-alias", metadata, config) is None
    assert maps["another-alias"] is maps[job_id]


def test_continuation_keeps_job_and_real_new_input_resets_budget(tmp_path):
    metadata = {"map_job_state": {}, "map_workflow_state": {}}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    begin_map_request(metadata, cwd=tmp_path, config=config)
    first = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="risk")
    assert reserve_map_render(first, metadata, config) is None
    assert reserve_map_render("new-name", metadata, config) is not None
    reset_map_workflow(metadata)
    begin_map_request(metadata, cwd=tmp_path, config=config, continuation=True)
    assert ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="continue") == first
    assert reserve_map_render(first, metadata, config) is None
    reset_map_workflow(metadata)
    begin_map_request(metadata, cwd=tmp_path, config=config)
    second = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="new-user-map")
    assert second != first and reserve_map_render(second, metadata, config) is None


def test_legacy_budget_does_not_create_job_state_without_cwd():
    metadata = {}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    assert reserve_map_render("one", metadata, config) is None
    assert reserve_map_render("two", metadata, config) is None
    assert "map_job_state" not in metadata


def test_historical_alias_cannot_replenish_current_request_budget(tmp_path):
    metadata = {"map_job_state": {}}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0))
    begin_map_request(metadata, cwd=tmp_path, config=config)
    first = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="old-A")
    begin_map_request(metadata, cwd=tmp_path, config=config)
    second = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="old-B")
    assert first != second
    reset_map_workflow(metadata)
    begin_map_request(metadata, cwd=tmp_path, config=config)
    assert reserve_map_render("old-A", metadata, config) is None
    assert reserve_map_render("old-B", metadata, config) is not None
    assert refund_map_render("old-A", metadata)
    assert reserve_map_render("old-B", metadata, config) is None


@pytest.mark.asyncio
async def test_changed_aliases_share_per_job_review_limit(tmp_path):
    metadata = {"map_job_state": {}, "map_workflow_state": {}}
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=0, max_reviews_per_request=5))
    begin_map_request(metadata, cwd=tmp_path, config=config)
    reviewer = Reviewer()
    results = []
    for name, color in (("first", "white"), ("second", "black")):
        image = tmp_path / f"{name}.png"
        Image.new("RGB", (32, 32), color).save(image)
        results.append(
            await review_with_budget(
                image,
                config=config,
                task="Check",
                evidence={},
                metadata=metadata,
                map_id=name,
                client=reviewer,
            )
        )
    assert reviewer.calls == 1
    assert results[-1][1]["review_budget_exhausted"]
    maps = metadata["map_workflow_state"]["maps"]
    assert maps["first"] is maps["second"]
