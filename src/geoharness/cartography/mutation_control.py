"""Exclusive map editing and revision checks over existing job receipts."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

from openharness.tools.base import ToolResult

from geoharness.cartography import jobs

_OWNERS = {}
_CURRENT = ContextVar("map_mutation", default=None)


class MapMutationConflict(ValueError):
    def __init__(self, message, recovery):
        super().__init__(message)
        self.recovery = recovery


def _state(context, map_id):
    state = jobs.map_mutation_state(context.metadata, cwd=context.cwd,
        config=context.metadata["geoharness_config"], map_id=map_id)
    if state["latest_spec_ref"]:
        state["inspect_call"] = {"action": "inspect", "spec_ref": state["latest_spec_ref"]}
        state["preflight_call"] = {"action": "preflight", "spec_ref": state["latest_spec_ref"]}
    return state


def plan_binding(context, *, map_id, spec_ref=""):
    state = _state(context, map_id)
    return {"map_id": map_id, "base_version": state["latest_version"],
            "spec_ref": spec_ref or state["latest_spec_ref"]}


def check_plan_binding(context, binding):
    if not binding:
        return  # Existing returned capsules remain usable, explicitly marked legacy/unbound.
    state = _state(context, binding["map_id"])
    if (binding["base_version"] != state["latest_version"] or
            binding.get("spec_ref") and state["latest_spec_ref"] != binding["spec_ref"]):
        raise MapMutationConflict("Prepared plan is stale; inspect and preflight the latest draft",
                                  state)


class _Owner:
    def __init__(self, context, map_id, operation):
        self.context, self.map_id = context, map_id
        self.task, self.operation = asyncio.current_task(), operation
        self.status = "completed"
        self.ledger_error = None
        self.started = jobs._now()

    def stage(self, stage):
        jobs.record_map_mutation(self.map_id, self.context.metadata, cwd=self.context.cwd,
            config=self.context.metadata["geoharness_config"], value={
                "operation": self.operation, "stage": stage, "status": "running",
                "started_at": self.started, "finished_at": None})


@asynccontextmanager
async def map_mutation(context, *, map_id, operation):
    key = (str(context.cwd.resolve()), map_id)
    current = _OWNERS.get(key)
    if current is not None:
        if current.task is not asyncio.current_task():
            raise MapMutationConflict("Map mutation already in progress", _state(context, map_id))
        yield current
        return
    owner = _Owner(context, map_id, operation)
    _OWNERS[key] = owner
    token = _CURRENT.set(owner)
    try:
        owner.stage(operation)
        yield owner
    except asyncio.CancelledError:
        owner.status = "cancelled"
        raise
    except BaseException:
        owner.status = "failed"
        raise
    finally:
        try:
            jobs.record_map_mutation(map_id, context.metadata, cwd=context.cwd,
                config=context.metadata["geoharness_config"], value={
                    "status": owner.status, "finished_at": jobs._now()})
        except (OSError, ValueError) as exc:
            # Cleanup failure must not replace a completed native result or the
            # original exception/cancellation. Ownership still ends below.
            owner.ledger_error = str(exc)
        finally:
            _CURRENT.reset(token)
            _OWNERS.pop(key, None)


def current_owner():
    return _CURRENT.get()


def _enrich(result, state):
    output = result.output
    try:
        payload, end = json.JSONDecoder().raw_decode(result.output)
        if isinstance(payload, dict):
            output = (json.dumps({**state, **payload}, ensure_ascii=False)
                      + result.output[end:])
    except ValueError:
        pass
    return ToolResult(output=output, is_error=result.is_error,
                      metadata={**result.metadata, **state})


def guard_map_mutations(execute):
    """Bind edits before any draft save and hold ownership through registered rendering."""
    @wraps(execute)
    async def guarded(self, arguments, context, **kwargs):
        config = context.metadata.get("geoharness_config")
        action = getattr(arguments, "action", "")
        reviewing = self.name == "geo_map_review"
        if config is None or (not reviewing and action not in {
                "spec", "preflight", "create", "export"}):
            return await execute(self, arguments, context, **kwargs)
        if reviewing and (arguments.auto_repair is False or arguments.retry_service_failure):
            return await execute(self, arguments, context, **kwargs)
        from geoharness.qgis.prepared import load_preflight
        from geoharness.qgis.specification import load_spec, save_spec

        try:
            packet = None
            parameters = arguments.model_dump(exclude_unset=True)
            if not reviewing and arguments.plan_ref:
                packet = load_preflight(arguments.plan_ref, cwd=context.cwd, settings=config.qgis)
                parameters = packet["parameters"]
            elif not reviewing and arguments.spec_ref:
                parameters = load_spec(arguments.spec_ref, cwd=context.cwd, settings=config.qgis)
            artifact = None
            if reviewing:
                artifact = (context.cwd / arguments.file_path).resolve()
                roots = [context.cwd.resolve()]
                for section in (config.cartography, config.execution, config.qgis):
                    roots.append((context.cwd / section.outputs_dir).resolve())
                if not any(artifact.is_relative_to(root) for root in roots):
                    return await execute(self, arguments, context, **kwargs)
            identity = parameters.get("map_id") or parameters.get("title", "")
            if not reviewing and action == "export" and not identity:
                identity = jobs.saved_project_job(context.metadata, cwd=context.cwd, config=config,
                                                  project_path=arguments.project_path) or ""
            job = jobs.ensure_map_job(context.metadata, cwd=context.cwd, config=config,
                                     map_id=identity, artifact_path=artifact)
            async def perform(owner, arguments):
                if packet:
                    check_plan_binding(context, packet.get("binding"))
                if not reviewing and arguments.spec_ref:
                    state = _state(context, job)
                    if state["latest_spec_ref"] and state["latest_spec_ref"] != arguments.spec_ref:
                        raise MapMutationConflict("Draft is stale; inspect the latest draft", state)
                if not reviewing and not (arguments.spec_ref or arguments.plan_ref):
                    arguments = arguments.model_copy(update={"map_id": job})
                result = await execute(self, arguments, context, **kwargs)
                if result.is_error:
                    owner.status = "failed"
                    return result
                if not reviewing and action in {"spec", "preflight"}:
                    ref = result.metadata.get("spec_ref")
                    if ref:
                        jobs.record_map_draft(job, context.metadata, cwd=context.cwd,
                                              config=config, spec_ref=ref)
                if not reviewing and action == "create":
                    raw = packet["parameters"] if packet else arguments.model_dump(
                        exclude={"action", "plan_ref"}, exclude_unset=True)
                    raw = json.loads(json.dumps(raw))
                    if raw.get("layout"):
                        raw["layout"].pop("layout_plan", None)
                    try:
                        ref, _ = save_spec({**raw, "map_id": job}, cwd=context.cwd,
                                           settings=config.qgis)
                        jobs.record_map_draft(job, context.metadata, cwd=context.cwd,
                                              config=config, spec_ref=ref)
                    except (OSError, ValueError) as exc:
                        return ToolResult(output=result.output + "\nRendered artifact retained; "
                            f"draft registration failed: {exc}", is_error=True,
                            metadata={**result.metadata, "render_executed": True,
                                      "receipt_status": "failed", "quality_status": "draft"})
                if reviewing or action == "create":
                    try:
                        state = _state(context, job)
                    except (OSError, ValueError) as exc:
                        return ToolResult(output=result.output + f"\nLedger refresh failed: {exc}",
                            is_error=True, metadata={**result.metadata,
                                "render_executed": (not reviewing or bool(
                                    result.metadata.get("repair_count"))),
                                "receipt_status": "failed", "quality_status": "draft"})
                    public = {key: state[key] for key in
                              ("map_id", "latest_version", "latest_spec_ref")}
                    if packet and not packet.get("binding"):
                        public["version_binding"] = "legacy_unbound; preflight for revision checks"
                    result = _enrich(result, public)
                return result
            async with map_mutation(context, map_id=job,
                    operation="automatic_review" if reviewing else action) as owner:
                result = await perform(owner, arguments)
            if owner.ledger_error:
                return ToolResult(output=result.output + "\nMap metadata cleanup failed; "
                    "actual outputs retained: " + owner.ledger_error, is_error=True,
                    metadata={**result.metadata, "receipt_status": "failed",
                              "quality_status": "draft", "cleanup_error": owner.ledger_error})
            return result
        except MapMutationConflict as exc:
            recovery = _state(context, exc.recovery["map_id"])
            return ToolResult(output=json.dumps({"status": "mutation_conflict", "reason": str(exc),
                **recovery}, ensure_ascii=False), is_error=True,
                metadata={"mutation_conflict": True, "render_executed": False, **recovery})
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Map mutation could not start: {exc}", is_error=True,
                              metadata={"render_executed": False})
    return guarded
