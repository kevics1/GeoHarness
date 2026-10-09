"""Create, inspect and export editable projects in the separate QGIS runtime."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.cartography.derivation import MapDerivation, derive_frame
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

    action: Literal["create", "inspect", "export", "preflight"]
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

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisProjectInput:
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
        "Network/database layers are unsupported. Outputs are drafts until geo_map_review "
        "checks actual pixels. Keep the same map_id and use geo_deliver for final delivery."
    )
    input_model = GeoQgisProjectInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoQgisProjectInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        if not config.qgis.enabled and arguments.action != "preflight":
            return ToolResult(output="Error: QGIS integration is disabled.", is_error=True)
        if arguments.action == "create" or (
            arguments.action == "preflight" and arguments.layout is not None and config.qgis.enabled
        ):
            # Inputs remain alive through worker shutdown, including cancellation.
            with TemporaryDirectory(prefix="geoh-qgis-inputs-") as directory:
                return await self._execute(arguments, context, Path(directory).resolve())
        return await self._execute(arguments, context)

    async def _execute(self, arguments, context, snapshot_directory=None) -> ToolResult:
        config = context.metadata["geoharness_config"]
        preflight = None
        if arguments.action in {"create", "preflight"}:
            try:
                if snapshot_directory is not None:
                    pending = asyncio.create_task(asyncio.to_thread(
                        self._freeze_and_preflight, arguments, context, snapshot_directory,
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
                                  metadata={"preflight_status": "failed", "render_executed": False})
        from geoharness.qgis.runner import execute_qgis

        payload = arguments.model_dump(exclude={"action"}, exclude_none=True)
        if preflight:
            payload.update(crs=preflight["display_crs"], map_extent=preflight["map_extent"])
            facts = preflight.get("analysis") or context.metadata.get("map_analysis_facts")
            if facts and facts.get("status") == "verified":
                from geoharness.cartography.provenance import apply_analysis_footer

                try:
                    payload = apply_analysis_footer(payload, facts)
                except ValueError as exc:
                    return ToolResult(output=f"Error: analysis footer layout invalid: {exc}",
                                      is_error=True)
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
                                      + result.get("error_summary", "unknown layout error"),
                                      is_error=True, metadata={"render_executed": False})
                preflight["layout"] = result["result"]["layout"]
            elif arguments.layout is not None:
                preflight["layout"] = {"bounds_check": {"checked": False, "valid": None},
                                       "reason": "QGIS is disabled"}
            return ToolResult(output=json.dumps(preflight, ensure_ascii=False, indent=2),
                              metadata={"preflight": preflight, "render_executed": False})
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
            stop = reserve_map_render(identity, context.metadata, config)
            if stop is not None:
                return ToolResult(output=stop, metadata={"render_budget_exhausted": True})
        payload["map_id"] = identity
        # NULL is a real category key, rather than an absent optional setting.
        for source, packed in zip(arguments.layers, payload.get("layers", [])):
            if source.style is not None:
                for category, encoded in zip(source.style.categories,
                                             packed.get("style", {}).get("categories", [])):
                    encoded["value"] = category.value
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
        metadata = {**details, **result, "map_id": identity, "preflight": preflight}
        if metadata.get("project_file"):
            metadata["qgis_project"] = metadata["project_file"]
        response = ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata=metadata,
        )
        if reserved and result.get("status") == "succeeded":
            from geoharness.cartography.tracking import track_render_result

            analysis = (preflight or {}).get("analysis") or (
                context.metadata.get("map_analysis_facts") if arguments.action == "create" else None
            )
            return await asyncio.to_thread(track_render_result, response, context, identity,
                                           preflight=preflight, analysis=analysis)
        return response

    def is_read_only(self, arguments: BaseModel) -> bool:
        return isinstance(arguments, GeoQgisProjectInput) and arguments.action in {
            "inspect", "preflight",
        }

    def _freeze_and_preflight(self, arguments, context, directory):
        snapshot = freeze_qgis_inputs(
            [layer.model_dump(exclude_none=True) for layer in arguments.layers],
            directory=directory, cwd=Path(context.cwd),
            read_roots=qgis_read_roots(context.metadata["geoharness_config"], Path(context.cwd)),
        )
        frozen = arguments.model_copy(update={
            "layers": [layer.model_copy(update=updates)
                       for layer, updates in zip(arguments.layers, snapshot.layers)],
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
                               "style": layer.style.model_dump() if layer.style else {}})
                original_path = snapshot.original_sources[index]["file_path"] if snapshot else (
                    str(path)
                )
                mapped_sources.append({"file_path": original_path, "gdf": gdf,
                                       "label": common["label"],
                                       "derivation": layer.derivation.model_dump()
                                       if layer.derivation is not None else None})
        settings = config.cartography
        report = validate_frames(
            frames, map_crs=arguments.crs, map_extent=arguments.map_extent,
            detail_extent=(arguments.layout.detail_extent.model_dump()
                           if arguments.layout and arguments.layout.detail_extent else None),
            coerce_numeric=False, minimum_span_geographic=settings.minimum_span_geographic,
            minimum_span_projected=settings.minimum_span_projected,
            extent_padding=settings.extent_padding,
        )
        if arguments.analysis_manifests:
            from geoharness.cartography.facts import load_analysis_facts

            report["analysis"] = load_analysis_facts(
                arguments.analysis_manifests, cwd=context.cwd,
                read_roots=qgis_read_roots(config, context.cwd), mapped_sources=mapped_sources,
                display_crs=report["display_crs"],
            )
        return report


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
