"""One task owns native map mutations; receipts retain observable recovery state."""

import asyncio

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.cartography.jobs import ensure_map_job
from geoharness.config.settings import GeoConfig


def context_for(tmp_path):
    return ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})


def job_for(context):
    return ensure_map_job(context.metadata, cwd=context.cwd,
                          config=context.metadata["geoharness_config"], map_id="native-task")


async def test_same_job_blocks_other_task_across_metadata_but_allows_owner_nested_create(tmp_path):
    from geoharness.cartography.mutation_control import MapMutationConflict, map_mutation

    context = context_for(tmp_path)
    other = context_for(tmp_path)
    identity = job_for(context)
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def automatic():
        async with map_mutation(context, map_id=identity, operation="automatic_review") as owner:
            owner.stage("repair_plan")
            async with map_mutation(context, map_id=identity, operation="create"):
                entered.set()
                await finish.wait()

    task = asyncio.create_task(automatic())
    await entered.wait()
    try:
        with pytest.raises(MapMutationConflict, match="in progress") as failure:
            async with map_mutation(other, map_id=identity, operation="spec"):
                pytest.fail("Another task entered an owned mutation")
        assert failure.value.recovery["map_id"] == identity
        assert failure.value.recovery["mutation"]["stage"] == "repair_plan"
    finally:
        finish.set()
        await task
    async with map_mutation(other, map_id=identity, operation="spec"):
        pass


@pytest.mark.parametrize("termination", ["failure", "cancel"])
async def test_owner_failure_or_cancel_releases_control_and_records_terminal_state(
    tmp_path, termination,
):
    from geoharness.cartography.jobs import map_mutation_state
    from geoharness.cartography.mutation_control import map_mutation

    context = context_for(tmp_path)
    identity = job_for(context)
    entered = asyncio.Event()

    async def work():
        async with map_mutation(context, map_id=identity, operation="automatic_review") as owner:
            owner.stage("native_presentation_create")
            entered.set()
            if termination == "failure":
                raise RuntimeError("synthetic failure")
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await entered.wait()
    if termination == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await task
    state = map_mutation_state(context.metadata, cwd=context.cwd,
        config=context.metadata["geoharness_config"], map_id=identity)
    assert state["mutation"]["status"] == ("cancelled" if termination == "cancel" else "failed")
    assert state["mutation"]["stage"] == "native_presentation_create"
    assert state["mutation"]["finished_at"]
    async with map_mutation(context, map_id=identity, operation="spec"):
        pass


async def test_old_prepared_binding_rejected_with_latest_editable_draft(tmp_path):
    from geoharness.cartography.jobs import map_mutation_state, record_map_draft
    from geoharness.cartography.mutation_control import (
        MapMutationConflict,
        check_plan_binding,
        plan_binding,
    )

    context = context_for(tmp_path)
    config = context.metadata["geoharness_config"]
    identity = job_for(context)
    old_ref = "qgis-spec:" + "a" * 64
    new_ref = "qgis-spec:" + "b" * 64
    record_map_draft(identity, context.metadata, cwd=tmp_path, config=config, spec_ref=old_ref)
    binding = plan_binding(context, map_id=identity, spec_ref=old_ref)
    check_plan_binding(context, binding)
    receipt_path = tmp_path / config.cartography.jobs_dir / identity / "receipt.json"
    from geoharness.cartography.ledger_store import update_head

    update_head(receipt_path, {"current_version": 1})
    record_map_draft(identity, context.metadata, cwd=tmp_path, config=config, spec_ref=new_ref)
    with pytest.raises(MapMutationConflict, match="stale") as failure:
        check_plan_binding(context, binding)
    assert failure.value.recovery["latest_version"] == 1
    assert failure.value.recovery["preflight_call"] == {
        "action": "preflight", "spec_ref": new_ref}
    state = map_mutation_state(context.metadata, cwd=tmp_path, config=config, map_id=identity)
    assert state["latest_spec_ref"] == new_ref


async def test_other_job_can_mutate_while_automatic_owner_active(tmp_path):
    from geoharness.cartography.mutation_control import map_mutation

    context = context_for(tmp_path)
    first = job_for(context)
    # A second identity is a separate existing ledger job, rather than an alias.
    other = context_for(tmp_path)
    second = ensure_map_job(other.metadata, cwd=tmp_path,
        config=other.metadata["geoharness_config"], map_id="another-map")
    assert second != first
    async with map_mutation(context, map_id=first, operation="automatic_review"):
        async with map_mutation(other, map_id=second, operation="create"):
            pass
