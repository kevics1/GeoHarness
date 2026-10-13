"""Create, inspect and export editable projects in the separate QGIS runtime."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from geoharness.cartography.derivation import MapDerivation, derive_frame
from geoharness.cartography.retry_guard import guard_map_errors
from geoharness.cartography.snapshot import FrozenQgisInputs, freeze_qgis_inputs
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
    fill_opacity: float = Field(default=1.0, ge=0, le=1, description=(
        "Symbol fill alpha only; stroke and labels remain visible. Hollow point rings "
        "can preserve a classified point underneath. Never use to hide required polygons."
    ))
    size_mm: float = Field(default=3, gt=0, le=50)
    width_mm: float = Field(default=0.3, ge=0, le=20)
    outline_color: str = "#ffffff"
    obstacle: bool | None = Field(default=None, description=(
        "Optional PAL labeling obstacle, including unlabeled lines/polygons. None keeps "
        "the normal behavior; True protects this geometry from other layers' labels."
    ))
    obstacle_factor: float = Field(default=1, ge=0, le=10)

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
        # A reversed legend order is a harmless permutation, not an invalid interval.
        # Keep each interval's label and color together; actual overlap still fails.
        self.ranges = sorted(self.ranges, key=lambda item: (item.lower, item.upper))
        for previous, current in zip(self.ranges, self.ranges[1:]):
            if current.lower < previous.upper or current.upper <= previous.upper:
                raise ValueError("graduated ranges must be ordered and non-overlapping")
        return self


class QgisLabelStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_family: str = "Microsoft YaHei"
    font_file: str = Field(default="", description=(
        "Optional TTF/TTC/OTF file in workspace/data read roots. For installed system fonts "
        "prefer font_family; explicit system-directory paths are not granted."
    ))
    size_pt: float = Field(default=9, ge=5, le=40)
    color: str = "#222222"
    buffer_size_mm: float = Field(default=0.8, ge=0, le=5)
    buffer_color: str = "#ffffff"
    placement: Literal["auto", "around_point", "over_point", "parallel", "horizontal"] = "auto"
    distance_mm: float = Field(default=1.5, ge=0, le=20, description=(
        "Distance from the point for around_point placement; over_point ignores this value."
    ))
    offset_x_mm: float = Field(default=0, ge=-20, le=20, description=(
        "Fixed horizontal offset for over_point placement. around_point ignores X/Y offsets; "
        "use distance_mm for around_point, or explicitly choose over_point for fixed offsets."
    ))
    offset_y_mm: float = Field(default=0, ge=-20, le=20, description=(
        "Fixed vertical offset for over_point placement; around_point uses distance_mm instead."
    ))
    priority: int = Field(default=5, ge=0, le=10,
                          description="PAL label priority: 0 lowest, 10 highest.")


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
    layer_key: str = Field(default="", max_length=128, description=(
        "Stable business layer identity across versions. Keep when replacing a derived file; "
        "otherwise generated from original source, layer selector, kind and name."
    ))
    layer_name: str = Field(default="", description="Optional source GeoPackage layer name.")
    style: QgisVectorStyle | None = None
    raster_style: QgisRasterStyle | None = None
    label_field: str = ""
    label_style: QgisLabelStyle | None = None
    required: bool = True
    expected_feature_count: int | None = Field(default=None, ge=0)
    minimum_visible_features: int = Field(default=1, ge=0)
    derivation: MapDerivation | None = Field(
        default=None,
        description="Verified map subset/field selection/new annotations of file_path.",
    )

    @model_validator(mode="after")
    def validate_kind(self) -> QgisProjectLayer:
        if self.kind == "raster" and (
            self.style is not None or self.label_field or self.label_style or self.layer_name
            or self.derivation is not None
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


class QgisMapFrameSpec(BaseModel):
    """Choose an extent's subject separately from the layers drawn in that frame."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    label_layers: list[Annotated[int, Field(strict=True, ge=0)]] | None = Field(
        default=None, max_length=64, description=(
            "Layers allowed to label in this frame. [] hides labels here while keeping symbols; "
            "use a detail frame and label_table to retain dense names. None preserves labels."
        ))

    focus_layers: list[Annotated[int, Field(strict=True, ge=0)]] | None = Field(
        default=None, min_length=1, max_length=64,
        description="0-based layer indices whose bounds define the subject; excludes backgrounds.",
    )
    render_layers: list[Annotated[int, Field(strict=True, ge=0)]] | None = Field(
        default=None, min_length=1, max_length=64,
        description="0-based selected layers; original bottom-to-top drawing order is preserved.",
    )
    padding_ratio: float | None = Field(
        default=None, ge=0, le=1,
        description="Symmetric padding per side for focus bounds; defaults to cartography config.",
    )

    @model_validator(mode="after")
    def validate_selection(self) -> QgisMapFrameSpec:
        for name in ("focus_layers", "render_layers", "label_layers"):
            indices = getattr(self, name)
            if indices is not None:
                if len(indices) != len(set(indices)):
                    raise ValueError(f"{name} must not contain duplicate layer indices")
                setattr(self, name, sorted(indices))
        if self.focus_layers and self.render_layers is not None and not (
            set(self.focus_layers) <= set(self.render_layers)
        ):
            raise ValueError("focus_layers must be included in render_layers")
        if self.label_layers and self.render_layers is not None and not (
            set(self.label_layers) <= set(self.render_layers)
        ):
            raise ValueError("label_layers must be included in render_layers")
        return self


class QgisLayoutPlan(BaseModel):
    """An unchanged plan returned by native preflight, not a caller-authored layout."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1]
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_identity: dict[str, str]
    font_identities: dict[str, dict[str, Any]]
    source_identities: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any]
    regions: dict[str, list[int | float]]
    frames: dict[str, dict[str, Any]]
    content_metrics: dict[str, Any]
    adaptations: list[dict[str, Any]]


class QgisLabelTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_index: int = Field(ge=0, description="Numbered source vector layer; never guessed.")
    fields: list[str] = Field(min_length=2, max_length=8, description=(
        "Existing fields in display order. First field is a unique non-null stable roster key."
    ))
    headers: dict[str, str] = Field(default_factory=dict, max_length=8,
                                   description="Optional display headings for existing fields.")
    columns: int = Field(default=3, ge=1, le=4)
    height_mm: float = Field(default=50, ge=15, le=400)
    font_size_pt: float = Field(default=8, ge=5, le=20)


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
    main_frame: QgisMapFrameSpec | None = None
    detail_frame: QgisMapFrameSpec | None = None
    label_table: QgisLabelTable | None = None
    detail_minimum_zoom: float | None = Field(default=None, ge=1, le=20, description=(
        "Require main scale/detail scale >= this value, measured after native layout fitting. "
        "Use >1 for a real magnified detail; unset allows a locator/legacy inset."
    ))
    layout_plan: QgisLayoutPlan | None = Field(default=None, description=(
        "Unchanged preflight.layout_plan to consume; changed sources/styles/fonts are rejected."
    ))
    footer_text: str = Field(default="", description=(
        "Source/context notes only. Bind native analysis_manifests for verified statistics; "
        "use geo_analysis area for grouped time areas, sums and deduplicated coverage."
    ))
    font_family: str = "Microsoft YaHei"
    font_file: str = Field(default="", description=(
        "Optional TTF/TTC/OTF file in workspace/data read roots. For installed fonts "
        "prefer font_family; explicit system-directory paths are not granted."
    ))
    title_font_size_pt: float = Field(default=18, ge=8, le=40)
    body_font_size_pt: float = Field(default=9, ge=5, le=20)
    legend_font_size_pt: float | None = Field(default=None, ge=5, le=20)
    legend_auto_fit: bool = Field(default=True, description=(
        "Measure native legend content and expand the sidebar or shorten the detail map "
        "within the requested page; never remove entries or shrink text."
    ))
    detail_height_ratio: float = Field(default=0.4, ge=0.1, le=0.8)
    sidebar_width_mm: float = Field(default=60, ge=20, le=500)
    gap_mm: float = Field(default=5, ge=1, le=30)
    title_height_mm: float = Field(default=16, ge=8, le=60)
    footer_height_mm: float = Field(default=18, ge=10, le=100)

    @model_validator(mode="after")
    def validate_page(self) -> QgisProjectLayout:
        if min(self.width_mm, self.height_mm) - 2 * self.margin_mm < 40:
            raise ValueError("page dimensions must leave at least 40mm inside margins")
        sidebar = self.sidebar_width_mm + self.gap_mm if (
            self.legend or self.detail_extent is not None or self.detail_frame is not None
        ) else 0
        if self.width_mm - 2 * self.margin_mm - sidebar < 40:
            raise ValueError("page must leave at least 40mm for the main map beside the sidebar")
        if self.height_mm - 2 * self.margin_mm - self.footer_height_mm - self.gap_mm < 40:
            raise ValueError("page must leave at least 40mm for the main map above the footer")
        if self.detail_frame is not None:
            if self.detail_frame.focus_layers and self.detail_extent is not None:
                raise ValueError("detail_extent conflicts with detail_frame.focus_layers")
            if self.detail_extent is None and not self.detail_frame.focus_layers:
                raise ValueError("detail_frame requires focus_layers or detail_extent")
        return self


class GeoQgisProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "inspect", "export", "preflight"]
    plan_ref: str = Field(default="", description=(
        "Returned preflight plan_ref. create accepts only action+plan_ref and revalidates sources."
    ))
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
    map_extent: list[float] | QgisExtent | None = None
    layout: QgisProjectLayout | None = None
    layout_name: str = Field(
        default="", description="Saved layout to export; required if ambiguous."
    )
    output_format: Literal["png", "pdf"] = "png"
    output_path: str = Field(default="", description="Relative filename in the new run directory.")
    project_format: Literal["qgz", "qgs"] = "qgz"
    dpi: int = Field(default=150, ge=36, le=600)
    map_id: str = Field(default="", description="Stable logical map ID across versions and tools.")
    analysis_manifests: list[str] = Field(
        default_factory=list, max_length=8,
        description="Native analysis manifests used to derive verified statistics and methods.",
    )

    @field_validator("layers", mode="before")
    @classmethod
    def normalize_complete_layers(cls, value):
        if isinstance(value, str):
            def unique_object(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise ValueError(f"layers JSON contains duplicate key: {key}")
                    result[key] = item
                return result

            def reject_constant(constant):
                raise ValueError(f"layers JSON cannot contain {constant}")

            try:
                value = json.loads(value, object_pairs_hook=unique_object,
                                   parse_constant=reject_constant)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ValueError("layers requires a complete JSON array of layer objects") from exc
            if not isinstance(value, list):
                raise ValueError("layers JSON must contain an array of layer objects")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisProjectInput:
        if self.plan_ref:
            if self.action != "create" or self.model_fields_set - {"action", "plan_ref"}:
                raise ValueError("plan_ref create accepts only action and plan_ref; "
                                 "preflight changes first")
            return self
        if self.action in {"create", "preflight"}:
            if not self.layers:
                raise ValueError("create requires at least one local layer")
            if self.project_path:
                raise ValueError("create cannot replace an existing project; use export")
        elif not self.project_path:
            raise ValueError("inspect/export requires project_path")
        elif self.layers or self.crs or self.map_extent is not None or self.layout is not None \
                or self.analysis_manifests:
            raise ValueError("inspect/export preserves the saved project's layers, CRS and layout")
        if self.map_extent is not None:
            if isinstance(self.map_extent, QgisExtent):
                self.map_extent = [self.map_extent.xmin, self.map_extent.ymin,
                                   self.map_extent.xmax, self.map_extent.ymax]
            if len(self.map_extent) != 4:
                raise ValueError("map_extent requires four coordinates")
            xmin, ymin, xmax, ymax = self.map_extent
            if not all(math.isfinite(value) for value in self.map_extent):
                raise ValueError("map_extent must contain finite coordinates")
            if xmin >= xmax or ymin >= ymax:
                raise ValueError("map_extent must be xmin,ymin,xmax,ymax with positive area")
            if not self.crs:
                raise ValueError("explicit map_extent requires crs")
        if self.layout and self.layout.detail_extent and not self.crs:
            raise ValueError("explicit detail_extent requires crs")
        if self.layout:
            if self.map_extent is not None and self.layout.main_frame \
                    and self.layout.main_frame.focus_layers:
                raise ValueError("map_extent conflicts with main_frame.focus_layers")
            rendered = set()
            for name in ("main_frame", "detail_frame"):
                frame = getattr(self.layout, name)
                if name == "detail_frame" and frame is None and self.layout.detail_extent is None:
                    continue
                for indices in ((frame.focus_layers, frame.render_layers, frame.label_layers)
                                if frame else ()):
                    if indices is not None and any(index >= len(self.layers) for index in indices):
                        raise ValueError(f"{name} contains an out-of-range layer index")
                rendered.update(frame.render_layers if frame and frame.render_layers is not None
                                else range(len(self.layers)))
            if any(layer.required and index not in rendered
                   for index, layer in enumerate(self.layers)):
                raise ValueError("Required layer is omitted from every map frame")
            if self.layout.label_table and self.layout.label_table.layer_index >= len(self.layers):
                raise ValueError("label_table references an unknown layer_index")
        return self


class GeoQgisProjectTool(BaseTool):
    name = "geo_qgis_project"
    description = (
        "Preflight map data/fields/CRS/extents without rendering, create an editable QGIS "
        "project, inspect one, or export a user-edited saved layout. "
        "QGIS runs offscreen in a separate process. Create accepts local vector/raster layers, "
        "explicit category colors/fields, labels, CRS and extent; layer order is bottom first. "
        "Create/export packages snapshots as GPKG/TIFF and writes a new project plus a PNG "
        "preview exported from the same editable layout; optional PDF comes from that layout. "
        "Export preserves saved styles/layouts and never overwrites the source project. "
        "Use layer.derivation for verified subsets/fields/annotations of native output files. "
        "Native analysis manifests are shared with geo_cartography and may be inferred; "
        "previous map facts are never inherited. Datetime categories normalize automatically. "
        "preflight measures actual fonts/footer/legend without exporting. Legend auto-fit "
        "widens the sidebar or shortens the detail map within the requested page. "
        "Use main_frame/detail_frame focus_layers, render_layers and label_layers to separate "
        "overview and detail duties without removing required features. label_table retains "
        "number/name attributes in an editable table separate from footer notes. "
        "detail_minimum_zoom checks actual paper magnification after native fitting. "
        "Use effective_parameters, adaptations and content_metrics for layout diagnosis. "
        "legend_font_size_pt controls the legend; body_font_size_pt controls footer text. "
        "duplicate_error_blocked requires changing the diagnosed arguments or input data. "
        "Network/database layers are unsupported. Outputs are drafts until geo_map_review "
        "checks actual pixels. Keep the same map_id and use geo_deliver for final delivery."
        " Preflight with layout returns plan_ref and an exact readable report_path; "
        "create with only action+plan_ref avoids repeating checked layer arrays/layout_plan. "
        "file_path accepts registered data: references; stale/unknown references are rejected."
    )
    input_model = GeoQgisProjectInput

    @guard_map_errors
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext,
                      *, _source_bindings: list[dict] | None = None) -> ToolResult:
        assert isinstance(arguments, GeoQgisProjectInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        if arguments.plan_ref:
            from geoharness.qgis.prepared import load_preflight

            try:
                packet = load_preflight(arguments.plan_ref, cwd=context.cwd, settings=config.qgis)
                arguments = GeoQgisProjectInput.model_validate({**packet["parameters"],
                                                                "action": "create"})
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: prepared preflight failed: {exc}", is_error=True,
                                  metadata={"deterministic_error": True, "render_executed": False})
        from geoharness.data.task_context import resolve_registered_path

        try:
            arguments = arguments.model_copy(update={"layers": [layer.model_copy(update={
                "file_path": resolve_registered_path(layer.file_path, context.metadata,
                    cwd=Path(context.cwd), config=config)}) for layer in arguments.layers]})
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: data reference failed: {exc}", is_error=True,
                              metadata={"render_executed": False})
        if not config.qgis.enabled and arguments.action != "preflight":
            return ToolResult(output="Error: QGIS integration is disabled.", is_error=True)
        if arguments.action == "create" or (
            arguments.action == "preflight" and arguments.layout is not None and config.qgis.enabled
        ):
            # Inputs remain alive through worker shutdown, including cancellation.
            with TemporaryDirectory(prefix="geoh-qgis-inputs-") as directory:
                return await self._execute(arguments, context, Path(directory).resolve(),
                                           source_bindings=_source_bindings)
        return await self._execute(arguments, context, source_bindings=_source_bindings)

    async def _execute(self, arguments, context, snapshot_directory=None,
                       *, source_bindings=None) -> ToolResult:
        config = context.metadata["geoharness_config"]
        requested = arguments.model_dump(exclude={"action", "plan_ref"}, exclude_unset=True)
        preflight = None
        if arguments.action in {"create", "preflight"}:
            try:
                if snapshot_directory is not None:
                    pending = asyncio.create_task(asyncio.to_thread(
                        self._freeze_and_preflight, arguments, context, snapshot_directory,
                        **({"source_bindings": source_bindings} if source_bindings is not None
                           else {}),
                    ))
                    try:
                        arguments, preflight = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=config.qgis.timeout_seconds,
                        )
                    except (TimeoutError, asyncio.CancelledError):
                        # Cancelling to_thread cannot stop native file reads/writes.
                        await _drain_task(pending)
                        raise
                else:
                    preflight = await asyncio.wait_for(
                        asyncio.to_thread(self._preflight, arguments, context),
                        timeout=config.qgis.timeout_seconds,
                    )
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                return ToolResult(output=f"Error: QGIS map preflight failed: {exc}", is_error=True,
                                  metadata={"preflight_status": "failed", "render_executed": False,
                                            "deterministic_error": isinstance(exc, ValueError),
                                            **({"diagnostics": exc.diagnostics}
                                               if hasattr(exc, "diagnostics") else {})})
        from geoharness.qgis.runner import execute_qgis

        payload = arguments.model_dump(exclude={"action"}, exclude_none=True)
        if preflight:
            creation = arguments.model_dump(exclude={"action"},
                                            exclude_unset=True)
            if not creation.get("crs"):
                creation["crs"] = preflight["display_crs"]
            if creation.get("layout"):
                creation["layout"].pop("layout_plan", None)
            payload["_creation_spec"] = {
                "parameters": creation,
                "source_bindings": preflight.get("source_bindings", []),
                "analysis_manifests": preflight.get("analysis_manifests", []),
            }
        if preflight:
            payload.update(crs=preflight["display_crs"], map_extent=preflight["map_extent"])
            payload["_explicit_map_extent"] = preflight["map_extent_source"] == "explicit"
            payload["_layout_sources"] = preflight.get("layout_sources", [])
            payload["_extent_policy"] = {
                key: getattr(config.cartography, key) for key in (
                    "minimum_span_geographic", "minimum_span_projected", "extent_padding")
            }
            facts = preflight.get("analysis") or {
                "status": "none", "analyses": [], "footer_text": ""}
            if facts.get("status") in {"verified", "none"}:
                from geoharness.cartography.provenance import apply_analysis_footer

                try:
                    payload = apply_analysis_footer(payload, facts)
                except ValueError as exc:
                    return ToolResult(output=f"Error: analysis footer layout invalid: {exc}",
                                      is_error=True, metadata={"deterministic_error": True})
        # NULL is a real category key, and must participate in the stable style binding.
        for source, packed in zip(arguments.layers, payload.get("layers", [])):
            if not source.required and "minimum_visible_features" not in source.model_fields_set:
                packed["minimum_visible_features"] = 0
            if source.style is not None:
                for category, encoded in zip(source.style.categories,
                                             packed.get("style", {}).get("categories", [])):
                    encoded["value"] = category.value
        plan = (payload.get("layout") or {}).get("layout_plan")
        if plan is not None:
            from geoharness.qgis.project_worker import _layout_input_digest

            if plan["input_digest"] != _layout_input_digest(payload):
                return ToolResult(output="Error: layout_plan is stale: sources/styles/parameters "
                    "changed; run preflight again", is_error=True,
                    metadata={"deterministic_error": True, "render_executed": False})
        if arguments.action == "preflight":
            if arguments.layout is not None and config.qgis.enabled:
                try:
                    result = await execute_qgis(
                        "project_preflight", payload, cwd=Path(context.cwd), settings=config.qgis,
                        read_roots=[*qgis_read_roots(config, Path(context.cwd)),
                                    *([snapshot_directory]
                                      if snapshot_directory is not None else [])],
                    )
                except (OSError, ValueError, RuntimeError) as exc:
                    return ToolResult(output=f"Error: QGIS layout preflight failed: {exc}",
                                      is_error=True, metadata={"render_executed": False})
                if result.get("status") != "succeeded":
                    return ToolResult(output="Error: QGIS layout preflight failed: "
                                      + (result.get("error_summary") or "unknown layout error")
                                      + "\n" + json.dumps(result.get("diagnostics"),
                                                            ensure_ascii=False),
                                      is_error=True, metadata={"render_executed": False,
                                      "diagnostics": result.get("diagnostics"),
                                      "deterministic_error": (result.get("error_summary") or "")
                                      .startswith("ValueError:")})
                _apply_native_frame_evidence(preflight, result["result"])
            elif arguments.layout is not None:
                preflight["layout"] = {"bounds_check": {"checked": False, "valid": None},
                                       "reason": "QGIS is disabled"}
            from geoharness.qgis.prepared import save_preflight, summary

            if arguments.layout is None and len(json.dumps(preflight, ensure_ascii=False)) <= 6000:
                return ToolResult(output=json.dumps(preflight, ensure_ascii=False, indent=2),
                                  metadata={"preflight": preflight, "render_executed": False})
            if preflight.get("layout_plan"):
                requested.setdefault("layout", {})["layout_plan"] = preflight["layout_plan"]
            try:
                reference, report_path = save_preflight(requested, preflight, cwd=context.cwd,
                                                        settings=config.qgis)
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: QGIS preflight report could not be saved: {exc}",
                    is_error=True, metadata={"render_executed": False,
                                            "preflight_status": "failed"})
            # Keep small legacy reports readable inline; large reports use an exact capsule.
            visible = summary(preflight, reference, report_path)
            if len(json.dumps(preflight, ensure_ascii=False)) <= 6000:
                visible = {**preflight, **{key: visible[key] for key in (
                    "plan_ref", "report_path", "create_call", "note")}}
            return ToolResult(output=json.dumps(visible, ensure_ascii=False, indent=2),
                metadata={"preflight": preflight, "render_executed": False,
                          "plan_ref": reference, "report_path": str(report_path)})
        identity = arguments.map_id or map_identity(
            arguments.title or Path(arguments.project_path).stem or "qgis-map"
        )
        reserved = arguments.action in {"create", "export"}
        if reserved:
            from geoharness.cartography.tracking import bind_map_job

            try:
                project_path = Path(arguments.project_path) if arguments.project_path else None
                saved_path = project_path
                if saved_path is not None and not saved_path.is_absolute():
                    saved_path = Path(context.cwd) / saved_path
                if arguments.action == "export" and not arguments.map_id \
                        and saved_path and saved_path.is_file():
                    from geoharness.cartography.jobs import saved_project_job

                    identity = await asyncio.to_thread(
                        saved_project_job, context.metadata, cwd=context.cwd, config=config,
                        project_path=project_path,
                    ) or identity
                identity = bind_map_job(context, identity,
                                        project_path)
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: map task could not start: {exc}", is_error=True)
            reserve_map_render(identity, context.metadata, config)
        payload["map_id"] = identity
        try:
            execution = execute_qgis(
                f"project_{arguments.action}",
                payload,
                cwd=Path(context.cwd),
                settings=config.qgis,
                read_roots=[*qgis_read_roots(config, Path(context.cwd)),
                            *([snapshot_directory] if snapshot_directory is not None else [])],
            )
            if snapshot_directory is not None:
                pending = asyncio.create_task(execution)
                try:
                    result = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    pending.cancel()
                    await _drain_task(pending)
                    raise
            else:
                result = await execution
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
        if preflight and details.get("layout_plan"):
            _apply_native_frame_evidence(preflight, details)
        metadata = {**details, **result, "map_id": identity, "preflight": preflight,
                    "deterministic_error": (result.get("error_summary") or "")
                    .startswith("ValueError:"),
                    "render_executed": result.get("status") == "succeeded" and reserved}
        if metadata.get("project_file"):
            metadata["qgis_project"] = metadata["project_file"]
        from geoharness.qgis.prepared import execution_summary

        response = ToolResult(
            output=json.dumps(execution_summary(result), ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata=metadata,
        )
        if reserved and result.get("status") == "succeeded":
            from geoharness.cartography.tracking import track_render_result

            analysis = (preflight or {}).get("analysis")
            return await asyncio.to_thread(track_render_result, response, context, identity,
                                           preflight=preflight, analysis=analysis)
        return response

    def is_read_only(self, arguments: BaseModel) -> bool:
        return isinstance(arguments, GeoQgisProjectInput) and arguments.action in {
            "inspect", "preflight",
        }

    def _freeze_and_preflight(self, arguments, context, directory, source_bindings=None):
        snapshot = freeze_qgis_inputs(
            [layer.model_dump(exclude_none=True) for layer in arguments.layers],
            directory=directory, cwd=Path(context.cwd),
            read_roots=qgis_read_roots(context.metadata["geoharness_config"], Path(context.cwd)),
        )
        if source_bindings is not None:
            if len(source_bindings) != len(snapshot.original_sources):
                raise ValueError("Map bridge source bindings must match actual layers")
            for original, binding in zip(snapshot.original_sources, source_bindings):
                primary_key = original.get("snapshot_primary_key")
                business_fields = original.get("business_fields")
                original.update(binding)
                if primary_key is not None:
                    original.update(snapshot_primary_key=primary_key,
                                    business_fields=business_fields)
            for source, binding in zip(snapshot.layout_sources, source_bindings):
                source.update(source=binding.get("file_path", source["source"]),
                              derivation=binding.get("derivation"),
                              layer_key=binding.get("layer_key", source["layer_key"]))
        for original, layer in zip(snapshot.original_sources, arguments.layers):
            if "derivation" not in original and layer.derivation is not None:
                original["derivation"] = layer.derivation.model_dump(exclude_none=True)
        frozen = arguments.model_copy(update={
            "layers": [layer.model_copy(update={**updates, "layer_key": original["layer_key"]})
                       for layer, updates, original in zip(arguments.layers, snapshot.layers,
                                                          snapshot.original_sources)],
        })
        from geoharness.cartography.values import TEMPORAL_FIELDS, normalize_style

        for layer, original in zip(frozen.layers, snapshot.original_sources):
            if layer.style and original.get("temporal_fields"):
                import geopandas as gpd

                frame = gpd.read_file(layer.file_path, layer=layer.layer_name)
                frame.attrs[TEMPORAL_FIELDS] = original["temporal_fields"]
                layer.style = type(layer.style).model_validate(
                    normalize_style(frame, layer.style.model_dump())
                )
        report = self._preflight(frozen, context, snapshot=snapshot)
        report["layout_sources"] = snapshot.layout_sources
        report["source_bindings"] = snapshot.original_sources
        # Analysis evidence was checked at original locators, against frozen GDFs.
        # Sources may change after this point: the worker only uses the snapshots.
        snapshot.verify_sources()
        return frozen, report

    def _preflight(self, arguments, context, *, snapshot: FrozenQgisInputs | None = None) -> dict:
        import geopandas as gpd
        import rasterio

        from geoharness.cartography.preflight import validate_frames
        from geoharness.qgis.project_worker import _local_source
        from geoharness.qgis.worker_context import WorkerContext

        config = context.metadata["geoharness_config"]
        guard = WorkerContext(Path(context.cwd), Path(context.cwd),
                              [*qgis_read_roots(config, Path(context.cwd)),
                               *([snapshot.directory] if snapshot else [])])
        frames = []
        mapped_sources = []
        for index, layer in enumerate(arguments.layers):
            path, options = _local_source(layer.file_path, guard)
            common = {"label": layer.name or path.stem, "required": layer.required,
                      "expected_feature_count": layer.expected_feature_count,
                      "minimum_visible_features": layer.minimum_visible_features if layer.required
                      else (layer.minimum_visible_features
                            if "minimum_visible_features" in layer.model_fields_set else 0)}
            if layer.kind == "raster":
                if layer.raster_style:
                    with rasterio.open(path) as source:
                        if layer.raster_style.band > source.count:
                            raise ValueError(f"Raster band {layer.raster_style.band} is missing")
                frames.append({**common, "raster_path": str(path)})
            else:
                name: str | int = layer.layer_name
                if not name and options:
                    selector, selected = options.split("=", 1)
                    name = int(selected) if selector == "layerid" else selected
                gdf = gpd.read_file(path, **({"layer": name} if name else {}))
                if layer.derivation is not None and snapshot is None:
                    gdf = derive_frame(gdf, layer.derivation)
                if snapshot:
                    from geoharness.cartography.values import TEMPORAL_FIELDS

                    gdf.attrs[TEMPORAL_FIELDS] = snapshot.original_sources[index].get(
                        "temporal_fields", []
                    )
                frames.append({**common, "gdf": gdf, "label_field": layer.label_field,
                               "style": layer.style.model_dump() if layer.style else {},
                               "label_style": (layer.label_style.model_dump()
                                               if layer.label_style else {})})
                original_path = snapshot.original_sources[index]["file_path"] if snapshot else (
                    str(path)
                )
                derivation = layer.derivation.model_dump() if layer.derivation is not None else None
                if snapshot and "derivation" in snapshot.original_sources[index]:
                    derivation = snapshot.original_sources[index]["derivation"]
                mapped_sources.append({"file_path": original_path, "gdf": gdf,
                                       "label": common["label"],
                                       "layer_index": index,
                                       "derivation": derivation})
        settings = config.cartography
        table = arguments.layout.label_table if arguments.layout else None
        if table:
            frame = frames[table.layer_index].get("gdf")
            if frame is None or len(set(table.fields)) != len(table.fields) or any(
                field not in frame.columns or field == frame.geometry.name for field in table.fields
            ):
                raise ValueError("label_table requires distinct existing vector attributes")
            key = frame[table.fields[0]]
            if key.isna().any() or key.duplicated().any():
                raise ValueError("label_table first field must be a unique non-null roster key")
        planned_frames = bool(arguments.layout and (
            arguments.layout.main_frame is not None or arguments.layout.detail_frame is not None
        ))
        report = validate_frames(
            frames, map_crs=arguments.crs,
            map_extent=None if planned_frames else arguments.map_extent,
            detail_extent=(arguments.layout.detail_extent.model_dump()
                           if not planned_frames and arguments.layout
                           and arguments.layout.detail_extent else None),
            coerce_numeric=False, minimum_span_geographic=settings.minimum_span_geographic,
            minimum_span_projected=settings.minimum_span_projected,
            extent_padding=settings.extent_padding,
        )
        if planned_frames:
            # Field/geometry/CRS checks use the whole frozen input. Actual visibility
            # belongs to the final native frame union, after font/content measurement.
            from geoharness.cartography.preflight import named_bounds, to_imshow_extent

            report["visibility_scope"] = "deferred_to_final_native_frame_union"
            if arguments.map_extent is not None:
                report.update(map_extent=arguments.map_extent, map_extent_source="explicit",
                              named_bounds=named_bounds(arguments.map_extent),
                              imshow_extent=to_imshow_extent(arguments.map_extent))
        from geoharness.cartography.composition import composition_risks

        report["composition"] = composition_risks(
            frames, report=report, layout=arguments.layout.model_dump() if arguments.layout else {},
        )
        from geoharness.cartography.facts import infer_analysis_manifests, load_analysis_facts

        roots = qgis_read_roots(config, context.cwd)
        manifests = list(dict.fromkeys([
            *arguments.analysis_manifests,
            *infer_analysis_manifests(mapped_sources, cwd=context.cwd, read_roots=roots),
        ]))
        report["analysis_evidence_status"] = "verified" if manifests else "not_provided"
        report["analysis_manifests"] = manifests
        if manifests:

            report["analysis"] = load_analysis_facts(
                manifests, cwd=context.cwd,
                read_roots=qgis_read_roots(config, context.cwd), mapped_sources=mapped_sources,
                display_crs=report["display_crs"],
            )
        return report


def _apply_native_frame_evidence(preflight: dict, details: dict) -> None:
    """Expose final native windows rather than the preliminary data envelope."""
    from geoharness.cartography.preflight import named_bounds, to_imshow_extent

    plan = details["layout_plan"]
    preflight.update(layout_plan=plan, composition=details["composition"],
                     frame_checks=details["frame_checks"])
    if details.get("layout"):
        preflight["layout"] = details["layout"]
    extent = plan["frames"]["main_map"]["extent"]
    preflight["preliminary_extent"] = preflight["map_extent"]
    preflight.update(map_extent=extent, named_bounds=named_bounds(extent),
                     imshow_extent=to_imshow_extent(extent),
                     visibility_scope="union_of_rendered_map_frames")
    for layer, checked in zip(preflight.get("layers", []), details["frame_checks"]["layers"]):
        counts = checked["frames"]
        layer.update(visible_feature_count=checked["visible_feature_count"],
                     frame_visibility=counts, visibility_valid=checked["valid"],
                     visible_main=bool(counts.get("main_map", 0)),
                     visible_detail=bool(counts.get("detail_map", 0)),
                     visible_main_feature_count=counts.get("main_map", 0),
                     visible_detail_feature_count=counts.get("detail_map", 0))
    detail = plan["frames"].get("detail_map")
    preflight.update(detail_extent=detail["extent"] if detail else None,
                     detail_named_bounds=named_bounds(detail["extent"]) if detail else None)
    preflight["warnings"] = [item for item in preflight.get("warnings", [])
                              if not item.endswith("is outside map/detail extent.")]
    preflight["warnings"].extend(details["frame_checks"].get("warnings", []))


async def _drain_task(task: asyncio.Task) -> None:
    """Finish an owned reader before deleting its files, even on repeated cancel."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()  # Retrieve failures without replacing the original cancel/timeout.
