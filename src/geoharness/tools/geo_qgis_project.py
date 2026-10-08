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
    size_mm: float | None = Field(default=None, gt=0, le=50)
    width_mm: float | None = Field(default=None, ge=0, le=20)


class QgisRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lower: float = Field(allow_inf_nan=False)
    upper: float = Field(allow_inf_nan=False)
    label: str = ""
    color: str
    size_mm: float | None = Field(default=None, gt=0, le=50)
    width_mm: float | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="after")
    def validate_bounds(self) -> QgisRange:
        if self.lower > self.upper:
            raise ValueError("graduated range lower must not exceed upper")
        return self


class QgisVectorStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: Literal["single", "categorized", "graduated"] = "single"
    color: str = "#4c78a8"
    field: str = ""
    categories: list[QgisCategory] = Field(default_factory=list)
    ranges: list[QgisRange] = Field(default_factory=list)
    opacity: float = Field(default=1.0, ge=0, le=1)
    size_mm: float = Field(default=3, gt=0, le=50)
    width_mm: float = Field(default=0.3, ge=0, le=20)
    outline_color: str = "#ffffff"

    @model_validator(mode="after")
    def validate_categories(self) -> QgisVectorStyle:
        if self.renderer == "categorized" and (not self.field or not self.categories):
            raise ValueError("categorized styling requires field and explicit categories/colors")
        if self.renderer == "graduated" and (not self.field or not self.ranges):
            raise ValueError("graduated styling requires field and explicit ranges/colors")
        if self.renderer != "categorized" and self.categories:
            raise ValueError("categories require renderer=categorized")
        if self.renderer != "graduated" and self.ranges:
            raise ValueError("ranges require renderer=graduated")
        if self.renderer == "single" and self.field:
            raise ValueError("field requires renderer=categorized or graduated")
        values = [json.dumps(category.value, sort_keys=True) for category in self.categories]
        if len(values) != len(set(values)):
            raise ValueError("category values must be unique")
        for previous, current in zip(self.ranges, self.ranges[1:]):
            if current.lower < previous.upper or current.upper <= previous.upper:
                raise ValueError("graduated ranges must be ordered and non-overlapping")
        return self


class QgisLabelStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_family: str = "Microsoft YaHei"
    font_file: str = ""
    size_pt: float = Field(default=9, ge=5, le=40)
    color: str = "#222222"
    buffer_size_mm: float = Field(default=0.8, ge=0, le=5)
    buffer_color: str = "#ffffff"


class QgisRasterStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    renderer: Literal["default", "hillshade"] = "default"
    band: int = Field(default=1, ge=1)
    azimuth: float = Field(default=315, ge=0, le=360)
    altitude: float = Field(default=45, gt=0, le=90)


class QgisProjectLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(min_length=1, description="Existing local vector/raster source.")
    kind: Literal["vector", "raster"] = "vector"
    name: str = ""
    layer_name: str = Field(default="", description="Optional source GeoPackage layer name.")
    style: QgisVectorStyle | None = None
    raster_style: QgisRasterStyle | None = None
    label_field: str = ""
    label_style: QgisLabelStyle | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> QgisProjectLayer:
        if self.kind == "raster" and (
            self.style is not None or self.label_field or self.label_style or self.layer_name
        ):
            raise ValueError("vector style/labels/layer_name do not apply to raster sources")
        if self.kind == "vector" and self.raster_style is not None:
            raise ValueError("raster_style applies only to raster sources")
        return self


class QgisExtent(BaseModel):
    """A positive rectangle in the project CRS, independent of any layer CRS."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @model_validator(mode="after")
    def validate_area(self) -> QgisExtent:
        if self.xmin >= self.xmax or self.ymin >= self.ymax:
            raise ValueError("detail_extent must be xmin,ymin,xmax,ymax with positive area")
        return self


class QgisProjectLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Map"
    width_mm: float = Field(default=297, ge=60, le=2000)
    height_mm: float = Field(default=210, ge=60, le=2000)
    margin_mm: float = Field(default=12, ge=0, le=100)
    legend: bool = True
    scale_bar: bool = True
    detail_extent: QgisExtent | None = Field(
        default=None, description="Optional detail-map rectangle in the project CRS."
    )
    footer_text: str = ""
    font_family: str = "Microsoft YaHei"
    font_file: str = ""
    title_font_size_pt: float = Field(default=18, ge=8, le=40)
    body_font_size_pt: float = Field(default=9, ge=5, le=20)
    sidebar_width_mm: float = Field(default=60, ge=20, le=500)
    gap_mm: float = Field(default=5, ge=1, le=30)
    title_height_mm: float = Field(default=16, ge=8, le=60)
    footer_height_mm: float = Field(default=18, ge=10, le=100)

    @model_validator(mode="after")
    def validate_page(self) -> QgisProjectLayout:
        if min(self.width_mm, self.height_mm) - 2 * self.margin_mm < 40:
            raise ValueError("page dimensions must leave at least 40mm inside margins")
        sidebar = self.sidebar_width_mm + self.gap_mm if (
            self.legend or self.detail_extent is not None
        ) else 0
        if self.width_mm - 2 * self.margin_mm - sidebar < 40:
            raise ValueError("page must leave at least 40mm for the main map beside the sidebar")
        if self.height_mm - 2 * self.margin_mm - self.footer_height_mm - self.gap_mm < 40:
            raise ValueError("page must leave at least 40mm for the main map above the footer")
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
        # NULL is a real category key, rather than an absent optional setting.
        for source, packed in zip(arguments.layers, payload.get("layers", [])):
            if source.style is not None:
                for category, encoded in zip(source.style.categories,
                                             packed.get("style", {}).get("categories", [])):
                    encoded["value"] = category.value
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
