"""Reuse upstream checklist semantics at one configured internal destination."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from openharness.permissions.checker import SENSITIVE_PATH_PATTERNS
from openharness.tools.base import ToolExecutionContext, ToolResult
from openharness.tools.todo_write_tool import TodoWriteTool, TodoWriteToolInput


class GeoTodoWriteTool(TodoWriteTool):
    description = (
        "Maintain the agent's internal checklist; item/checked semantics match todo_write. "
        "The legacy path TODO.md redirects to agent.internal_todo_path. "
        "Project TODO.md is preserved; arbitrary file destinations are rejected."
    )

    async def execute(self, arguments: TodoWriteToolInput,
                      context: ToolExecutionContext) -> ToolResult:
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        workspace = context.cwd.resolve()
        configured = Path(config.agent.internal_todo_path).expanduser()
        target = configured if configured.is_absolute() else workspace / configured
        target = target.absolute()
        resolved = target.resolve()
        supplied = Path(arguments.path).expanduser()
        supplied = supplied if supplied.is_absolute() else workspace / supplied
        try:
            if arguments.path != "TODO.md" and supplied.absolute() != target:
                raise ValueError("todo_write only accepts the configured internal checklist")
            if (target != resolved or not resolved.is_relative_to(workspace)
                    or resolved.parent == workspace):
                raise ValueError("Internal checklist must stay in an unredirected "
                                 "workspace subfolder")
            relative = resolved.relative_to(workspace)
            if any(part.casefold() in {
                ".git", ".ssh", ".aws", ".codex", ".agents", ".openharness",
                ".env", "credentials.json", "secrets.json", "auth.json", "settings.json",
            } or part.casefold().startswith(".env.") for part in relative.parts) \
                    or resolved.suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}:
                raise ValueError("Sensitive files cannot be used as an internal checklist")
            from geoharness.runtime import _path_pattern_variants

            patterns = [*SENSITIVE_PATH_PATTERNS, *(
                variant for pattern in config.permissions.deny_paths
                for variant in _path_pattern_variants(pattern))]
            paths = [str(resolved).replace("\\", "/"), str(resolved),
                     relative.as_posix(), str(relative)]
            if any(fnmatch.fnmatchcase(path.casefold(), pattern.casefold())
                   for path in paths for pattern in patterns):
                raise ValueError("Internal checklist destination is denied by path policy")
            if resolved.exists():
                stat = resolved.stat()
                if (not resolved.is_file() or stat.st_size > 1_048_576
                        or stat.st_nlink > 1):
                    raise ValueError("Internal checklist must be a bounded, unlinked text file")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # Do not use the upstream arbitrary path: only the validated internal target.
            result = await super().execute(arguments.model_copy(update={"path": str(resolved)}),
                                           context)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        return ToolResult(output=result.output, metadata={"internal_todo_path": str(resolved)})
