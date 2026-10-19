"""Measured execution timing around local tools, independent of UI batching."""

from __future__ import annotations

import json
from dataclasses import replace
from time import perf_counter, time
from uuid import uuid4


class MeasuredRegistry:
    def __init__(self, registry, metadata):
        self.registry = registry
        self.metadata = metadata
        metadata.setdefault("geo_tool_metrics", [])
        metadata.setdefault("geo_current_state", {})
        metadata.setdefault("geo_render_reuse", {})

    def __getattr__(self, name):
        return getattr(self.registry, name)

    def get(self, name):
        tool = self.registry.get(name)
        return MeasuredTool(tool, self.metadata) if tool is not None else None

class MeasuredTool:
    def __init__(self, tool, metadata, *, node_id=None):
        self.tool = tool
        self.metadata = metadata
        self.node_id = node_id
        metadata.setdefault('geo_tool_metrics', [])
        metadata.setdefault('geo_current_state', {})
        metadata.setdefault('geo_render_reuse', {})

    def __getattr__(self, name):
        return getattr(self.tool, name)

    async def execute(self, arguments, context):
        started, clock = time(), perf_counter()
        result = None
        error = None
        from geoharness.task_store import begin_operation, finish_operation, save_record

        handle = begin_operation(self.metadata, cwd=context.cwd, tool=self.tool.name,
                                 arguments=arguments, node_id=self.node_id)
        try:
            from geoharness.task_controller import execution_gate

            blocked = execution_gate(context, self.tool, arguments, node_id=self.node_id)
            if blocked:
                from openharness.tools.base import ToolResult

                result = ToolResult(output=json.dumps(blocked, ensure_ascii=False), is_error=True,
                                    metadata={'execution_status': 'failed'})
            else:
                result = await self.tool.execute(arguments, context)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            elapsed = round(perf_counter() - clock, 6)
            try:
                encoded = arguments.model_dump_json()
            except (AttributeError, TypeError, ValueError):
                encoded = ""
            config = self.metadata.get("geoharness_config")
            limit = getattr(getattr(config, "agent", None), "request_metrics_limit", 2048)
            metrics = self.metadata["geo_tool_metrics"]
            metrics.append({
                "execution_id": uuid4().hex, "tool": self.tool.name,
                "action": getattr(arguments, "action", None) or getattr(arguments, "name", None),
                "started_at": started, "elapsed_seconds": elapsed,
                "is_error": result.is_error if result is not None else True,
                "error_type": error,
                "input_chars": len(encoded),
                "output_chars": len(result.output or "") if result is not None else 0,
                "reused": bool(result is not None and result.metadata.get("reused")),
            })
            del metrics[:-limit]
            summary = self.metadata.setdefault("geo_timing_summary", {})
            summary["tool_seconds"] = round(summary.get("tool_seconds", 0) + elapsed, 6)
            summary["tool_calls"] = summary.get("tool_calls", 0) + 1
            if result is None or result.is_error:
                summary["tool_errors"] = summary.get("tool_errors", 0) + 1
            if result is not None and self.tool.name.startswith("geo_"):
                from geoharness.task_state import observe_operation

                observe_operation(self.metadata, tool=self.tool.name,
                                  action=getattr(arguments, 'action', None), result=result)
                self._remember_result(result, arguments)
            # Persist before upstream truncation/compaction can discard the result.
            # A failed commit is visible and never silently upgrades an operation.
            evidence = finish_operation(handle, self.metadata, result=result, error=error)
            if evidence and result is not None:
                result.metadata.update(evidence)
                call = {'action': 'evidence', 'operation_id': evidence['operation_id']}
                try:
                    packet = json.loads(result.output)
                    if isinstance(packet, dict):
                        if self.tool.name == 'geo_python':
                            # The full packet was committed above; repeated API help
                            # belongs in evidence rather than every model response.
                            packet.pop('execution_contract', None)
                            for key in ('stdout', 'stderr'):
                                if isinstance(packet.get(key), str) and len(packet[key]) > 2000:
                                    packet[key] = packet[key][-2000:]
                                    packet[key + '_truncated'] = True
                        packet.update(operation_id=evidence['operation_id'], evidence_call=call)
                        result = replace(result, output=json.dumps(packet, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
                self.metadata.setdefault('geo_current_state', {})['last_operation'] = {
                    'tool': self.tool.name, **evidence, 'evidence_call': call}
            save_record(self.metadata, cwd=context.cwd)
        return result

    def _remember_result(self, result, arguments):
        fields = {"status", "spec_ref", "latest_spec_ref", "manifest_path", "result_path",
                  "preview_path", "project_manifest", "map_id", "quality_status",
                  "analysis_status", "analysis_correctness", "summary_path", "report_path",
                  "build_call", "create_call", "inspect_call", "plan_ref", "run_dir",
                  "visual_review_status", "latest_version", "output_path", "page_previews",
                  "stage", "error_summary", "reused", "script_executed"}
        packet = {}
        try:
            raw, _ = json.JSONDecoder().raw_decode(result.output.lstrip())
            if isinstance(raw, dict):
                packet.update({key: value for key, value in raw.items()
                               if key in fields and isinstance(
                                   value, (str, bool, int, float, list, dict))})
                diagnostic = raw.get("diagnostics", {})
                if isinstance(diagnostic, dict) and diagnostic.get("repair_progress"):
                    packet["repair_progress"] = diagnostic["repair_progress"]
        except (ValueError, TypeError, AttributeError):
            pass
        packet.update({key: value for key, value in result.metadata.items()
                       if key in fields and isinstance(
                           value, (str, bool, int, float, list, dict))})
        packet["is_error"] = result.is_error
        action = getattr(arguments, "action", None)
        name = self.tool.name
        state = self.metadata["geo_current_state"]
        if result.is_error:
            state.setdefault(name, {})["last_attempt_failed"] = True
            state.setdefault("failures", []).append({
                "tool": name, "action": action or getattr(arguments, "name", None),
                "error_summary": packet.get("error_summary") or result.output[:500]})
            del state["failures"][:-8]
            return
        if name == "geo_qgis_project" and action in {"help", "inspect"}:
            return
        if name == "geo_qgis_project":
            state["qgis"] = {**state.get("qgis", {}), **packet}
            if action == "spec" and packet.get("spec_ref"):
                state["qgis"]["draft"] = {"spec_ref": packet["spec_ref"],
                                          "build_call": packet.get("build_call")}
            if action == "preflight" and packet.get("plan_ref"):
                state["qgis"]["prepared"] = {"plan_ref": packet["plan_ref"]}
            if action in {"build", "create"} and packet.get("manifest_path"):
                state["qgis"]["deliverable"] = {
                    key: packet.get(key) for key in (
                        "map_id", "manifest_path", "preview_path", "project_manifest",
                        "quality_status", "visual_review_status") if packet.get(key)}
        elif name == "geo_python":
            state["python"] = {
                "run_dir": packet.get("run_dir"), "manifest_path": packet.get("manifest_path"),
                "status": packet.get("status"), "is_error": result.is_error,
                "stage": getattr(arguments, "stage_id", ""), "reused": packet.get("reused", False),
                "error_summary": packet.get("error_summary"),
            }
        elif name == "geo_map_review":
            state["review"] = packet
        else:
            state[name] = {**state.get(name, {}), **packet}
