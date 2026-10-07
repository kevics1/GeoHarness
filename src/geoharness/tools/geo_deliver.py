"""Publish explicit analysis artifacts into the user's workspace exports folder."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.cartography.project import MapProject


class GeoDeliverInput(BaseModel):
    files: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Explicit map, table, report or script file paths to deliver.",
    )
    project_dir: str = Field(default="", description="Portable map project folder to include.")
    name: str = Field(
        default="delivery",
        min_length=1,
        max_length=80,
        pattern=r'^[^\\/:*?"<>|.][^\\/:*?"<>|]*$',
        description="User-facing delivery folder name; existing files are preserved.",
    )


def _source(path: Path, roots: list[Path]) -> Path:
    path = path.resolve()
    matched = [root for root in roots if path.is_relative_to(root)]
    if not matched:
        raise ValueError("Delivery inputs must be in the workspace or configured artifact folders.")
    relative = path.relative_to(max(matched, key=lambda root: len(root.parts)))
    if any(
        part.lower()
        in {
            ".env",
            "credentials.json",
            "secrets.json",
            "settings.json",
            "auth.json",
            ".ssh",
            ".aws",
            ".codex",
            ".agents",
            ".openharness",
            ".geoharness",
        }
        or part.lower().startswith(".env.")
        for part in relative.parts
    ) or path.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}:
        raise ValueError("Credential files cannot be delivered.")
    if not path.is_file():
        raise ValueError(f"Delivery input is not an existing file: {path}")
    return path


class GeoDeliverTool(BaseTool):
    name = "geo_deliver"
    description = (
        "Copy selected maps, data, reports and an editable map project into workspace exports. "
        "Use this after Python/native analysis; scripts must not write outside RUN_DIR. "
        "Returns the actual user-facing delivery folder and files; preserves prior deliveries."
    )
    input_model = GeoDeliverInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoDeliverInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        workspace = context.cwd.resolve()
        directory = Path(config.delivery.outputs_dir).expanduser()
        directory = (directory if directory.is_absolute() else workspace / directory).resolve()
        if not directory.is_relative_to(workspace) or directory == workspace:
            return ToolResult(
                output="Error: delivery.outputs_dir must be a subfolder of workspace.",
                is_error=True,
            )
        roots = [workspace]
        for section in ("analysis", "execution", "cartography"):
            path = Path(getattr(config, section).outputs_dir).expanduser()
            roots.append((path if path.is_absolute() else workspace / path).resolve())

        def resolve(value: str) -> Path:
            path = Path(value).expanduser()
            return _source(path if path.is_absolute() else workspace / path, roots)

        def publish() -> dict:
            files = [resolve(value) for value in arguments.files]
            project_files: list[tuple[Path, Path]] = []
            if arguments.project_dir:
                project_root = Path(arguments.project_dir).expanduser()
                project_root = (
                    project_root if project_root.is_absolute() else workspace / project_root
                ).resolve()
                manifest = resolve(str(project_root / "project.json"))
                project = MapProject.model_validate_json(manifest.read_text(encoding="utf-8"))
                relative_files = {
                    "project.json",
                    "map.qgs",
                    "redraw.py",
                    "README.md",
                    project.preview,
                    *(layer.file for layer in project.layers),
                }
                for relative in sorted(relative_files):
                    target = (project_root / relative).resolve()
                    if not target.is_relative_to(project_root):
                        raise ValueError("Project references must stay inside its folder.")
                    project_files.append((resolve(str(target)), Path("project") / relative))
            if not files and not project_files:
                raise ValueError("Select at least one artifact or a portable project.")
            directory.mkdir(parents=True, exist_ok=True)
            destination = Path(tempfile.mkdtemp(prefix=arguments.name + "-", dir=directory))
            copied = []
            for index, source in enumerate(files):
                target = destination / source.name
                suffix = index
                while target.exists() or target.name.casefold() in {"delivery.json", "project"}:
                    target = destination / f"{source.stem}-{suffix}{source.suffix}"
                    suffix += 1
                shutil.copy2(source, target)
                copied.append(str(target))
            for source, relative in project_files:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(target))
            payload = {
                "delivery_dir": str(destination),
                "files": copied,
                "qgis_project": str(destination / "project" / "map.qgs") if project_files else None,
            }
            manifest = destination / "delivery.json"
            manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {**payload, "manifest_path": str(manifest)}

        try:
            result = await asyncio.to_thread(publish)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: artifact delivery failed: {exc}", is_error=True)
        return ToolResult(output=json.dumps(result, ensure_ascii=False, indent=2), metadata=result)

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
