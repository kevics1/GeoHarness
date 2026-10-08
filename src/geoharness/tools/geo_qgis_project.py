"""Create, inspect and export editable projects in the separate QGIS runtime."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_map_render
from geoharness.tools.geo_qgis import qgis_read_roots


class QgisCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | int | float | bool | None
    label: str = ""
    color: str = Field(description="Explicit QGIS/HTML color for this category, e.g. #d73027.")


class QgisVectorStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: Literal["single", "categorized"] = "single"
    color: str = "#4c78a8"
    field: str = ""
    categories: list[QgisCategory] = Field(default_factory=list)
    opacity: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_categories(self) -> QgisVectorStyle:
        if self.renderer == "categorized" and (not self.field or not self.categories):
            raise ValueError("categorized styling requires field and explicit categories/colors")
        if self.renderer == "single" and (self.field or self.categories):
            raise ValueError("field/categories require renderer=categorized")
        values = [json.dumps(category.value, sort_keys=True) for category in self.categories]
        if len(values) != len(set(values)):
            raise ValueError("category values must be unique")
        return self


class QgisProjectLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1, description="Existing local vector/raster source.")
    kind: Literal["vector", "raster"] = "vector"
    name: str = ""
    layer_name: str = Field(default="", description="Optional source GeoPackage layer name.")
    style: QgisVectorStyle | None = None
    label_field: str = ""

    @model_validator(mode="after")
    def validate_kind(self) -> QgisProjectLayer:
        if self.kind == "raster" and (
            self.style is not None or self.label_field or self.layer_name
        ):
            raise ValueError("vector style/labels/layer_name do not apply to raster sources")
        return self


class QgisProjectLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Map"
    width_mm: float = Field(default=297, ge=60, le=2000)
    height_mm: float = Field(default=210, ge=60, le=2000)
    margin_mm: float = Field(default=12, ge=0, le=100)
    legend: bool = True
    scale_bar: bool = True

    @model_validator(mode="after")
    def validate_page(self) -> QgisProjectLayout:
        if min(self.width_mm, self.height_mm) - 2 * self.margin_mm < 40:
            raise ValueError("page dimensions must leave at least 40mm inside margins")
        return self


class GeoQgisProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "inspect", "export"]
    layers: list[QgisProjectLayer] = Field(
        default_factory=list,
        max_length=64,
        description="Create sources in drawing order, bottom first and top last.",
    )
    project_path: str = Field(
        default="", description="Existing local .qgs/.qgz for inspect/export."
    )
    title: str = ""
    crs: str = Field(default="", description="Explicit project CRS, e.g. EPSG:3857.")
    map_extent: list[float] | None = Field(default=None, min_length=4, max_length=4)
    layout: QgisProjectLayout | None = None
    layout_name: str = Field(
        default="", description="Saved layout to export; required if ambiguous."
    )
    output_format: Literal["png", "pdf"] = "png"
    output_path: str = Field(default="", description="Relative filename in the new run directory.")
    project_format: Literal["qgz", "qgs"] = "qgz"
    dpi: int = Field(default=150, ge=36, le=600)
    map_id: str = Field(default="", description="Stable logical map ID across versions and tools.")

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisProjectInput:
        if self.action == "create":
            if not self.layers:
                raise ValueError("create requires at least one local layer")
            if self.project_path:
                raise ValueError("create cannot replace an existing project; use export")
        elif not self.project_path:
            raise ValueError("inspect/export requires project_path")
        elif self.layers or self.crs or self.map_extent is not None or self.layout is not None:
            raise ValueError("inspect/export preserves the saved project's layers, CRS and layout")
        if self.map_extent is not None:
            xmin, ymin, xmax, ymax = self.map_extent
            if not all(math.isfinite(value) for value in self.map_extent):
                raise ValueError("map_extent must contain finite coordinates")
            if xmin >= xmax or ymin >= ymax:
                raise ValueError("map_extent must be xmin,ymin,xmax,ymax with positive area")
        return self


class GeoQgisProjectTool(BaseTool):
    name = "geo_qgis_project"
    description = (
        "Create a real editable QGIS project, inspect one, or export a user-edited saved layout. "
        "QGIS runs offscreen in a separate process. Create accepts local vector/raster layers, "
        "explicit category colors/fields, labels, CRS and extent; layer order is bottom first. "
        "Create/export packages snapshots as GPKG/TIFF and writes a new project plus a PNG "
        "preview exported from the same editable layout; optional PDF comes from that layout. "
        "Export preserves saved styles/layouts and never overwrites the source project. "
        "Network/database layers are unsupported. Outputs are drafts until geo_map_review "
        "checks actual pixels. Keep the same map_id and use geo_deliver for final delivery."
    )
    input_model = GeoQgisProjectInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoQgisProjectInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        if not config.qgis.enabled:
            return ToolResult(output="Error: QGIS integration is disabled.", is_error=True)
        from geoharness.qgis.runner import execute_qgis

        identity = arguments.map_id or map_identity(
            arguments.title or Path(arguments.project_path).stem or "qgis-map"
        )
        reserved = arguments.action in {"create", "export"}
        if reserved:
            stop = reserve_map_render(identity, context.metadata, config)
            if stop is not None:
                return ToolResult(output=stop, metadata={"render_budget_exhausted": True})
        payload = arguments.model_dump(exclude={"action"}, exclude_none=True)
        try:
            result = await execute_qgis(
                f"project_{arguments.action}",
                payload,
                cwd=Path(context.cwd),
                settings=config.qgis,
                read_roots=qgis_read_roots(config, Path(context.cwd)),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            if reserved:
                refund_map_render(identity, context.metadata)
            return ToolResult(
                output=f"Error: QGIS project task could not run: {exc}", is_error=True
            )
        except asyncio.CancelledError:
            if reserved:
                refund_map_render(identity, context.metadata)
            raise
        if result.get("status") != "succeeded" and reserved:
            refund_map_render(identity, context.metadata)
        details: dict[str, Any] = result.get("result") or {}
        metadata = {**details, **result, "map_id": identity}
        return ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata=metadata,
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return isinstance(arguments, GeoQgisProjectInput) and arguments.action == "inspect"
