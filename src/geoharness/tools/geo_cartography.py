"""GeoCartographyTool — native cartography pipeline.

Pipeline: ``symbolize → compose → export``.

Two modes:

* **Plan mode (no data source given)** — returns a human-readable plan
  describing how the layer *would* be symbolized/composed/exported. Used by
  the agent to reason about styling before loading data.
* **Render mode (a data source is given)** — resolves the source to a
  ``GeoDataFrame`` and renders it natively via
  :mod:`geoharness.cartography` (QGIS → PNG/PDF, matplotlib → SVG, folium → HTML).

Bridge rules (analysis type → symbolization) stay config-driven: adding a new
analysis type is a YAML edit, not a code change. The native QGIS worker runs
in its separate installed runtime; an explicit matplotlib compatibility path remains.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import AliasChoices, BaseModel, Field, model_validator

from geoharness.cartography.workflow import (
    refund_map_render,
    reserve_map_render,
    review_with_budget,
)
from geoharness.config.settings import CartographyConfig, MapLayout
from geoharness.tools.geo_qgis_project import QgisExtent, QgisProjectLayout

logger = logging.getLogger(__name__)

# Hard ceiling for a single render. Prevents a pathological source (huge
# table, unreachable DB) from hanging the tool forever.
_RENDER_TIMEOUT_SECONDS = 300.0

# Bridge-rule render methods understood by the native renderer.
_RENDER_METHODS = ("categorized", "graduated", "flow", "none")

# Bridge rule used when a cartography request has no spatial-analysis stage
# (thematic maps: administrative boundaries, fire perimeters, POIs...).
_THEMATIC_RULE_KEY = "thematic"

_FORMAT_SUFFIXES = {
    "pdf": ".pdf",
    "svg": ".svg",
    "png": ".png",
    "image": ".png",
    "html": ".html",
}


class GeoCartographyInput(BaseModel):
    """Input model for geo_cartography tool."""

    action: Literal[
        "symbolize",
        "compose",
        "export",
        "full",
        "project",
        "preflight",
        "status",
    ] = Field(
        default="full",
        description=(
            "symbolize: plan symbolization for a layer (no rendering); "
            "compose: describe map composition elements; "
            "export: arrange elements and write the map file; "
            "full: symbolize + compose + export in one step; "
            "project: create an editable QGIS project for an existing output map; "
            "preflight: read-only source, field, CRS and window checks without rendering"
        ),
    )
    layer_name: str = Field(
        default="",
        description="Label for the layer being symbolized (for symbolize/full).",
    )
    analysis_type: str = Field(
        default="",
        description=(
            "Analysis type for bridge rule lookup (e.g., 'moran_local', "
            "'getis_ord', 'cluster_detect', 'od_flow'). Determines symbolization."
        ),
    )
    field_name: str = Field(
        default="",
        description="Attribute field to symbolize (categorized/graduated/flow).",
    )
    output_format: Literal["pdf", "image", "png", "svg", "html"] = Field(
        default="pdf",
        description="Output format: pdf/png/svg (static) or html (interactive Leaflet).",
    )
    output_path: str = Field(
        default="",
        description=(
            "Output file path. If empty, writes to the configured outputs "
            "directory (workspace exports by default) using layer_name as file stem."
        ),
    )
    # ── Data source (required for actual rendering) ──
    source_type: Literal["", "file", "postgis", "admin_kg"] = Field(
        default="",
        description="Data source kind for rendering. Empty = plan mode only.",
    )
    file_path: str = Field(
        default="",
        description="Path to a local spatial file (.shp/.geojson/.gpkg) to render.",
    )
    source_name: str = Field(
        default="",
        description=(
            "Source name for rendering: file name, PostGIS table, or "
            "administrative region name/adcode (depends on source_type)."
        ),
    )
    geojson: str = Field(
        default="",
        description="Inline GeoJSON FeatureCollection to render instead of a source.",
    )
    layers: str = Field(
        default="",
        description=(
            "MULTI-LAYER composition (recommended for thematic maps): a JSON "
            "array of layer specs, e.g. "
            '[{"source_type":"file","source_name":"西昌市行政区划"},'
            '{"source_type":"file","source_name":"当前火场范围","color":"#d7301f"}]. '
            "Each spec: source_type (file/postgis/admin_kg) + source_name or "
            "file_path, optional label (legend text, defaults to source_name), "
            "color, and optional geojson inline data. For per-feature styles use column "
            "(or field_name alias), render_method (categorized/graduated/flow), color_scheme, "
            "n_classes; categorized styles accept categories (ordered values or "
            "{value,label,color} objects) and colors (matching list or value-to-color object). "
            "These options may also be nested in style; unsupported styles are errors. "
            "For example field_name=RiskTier, categories=[High,Low], colors=[#d73027,#1a9850]. "
            "Layers draw in array "
            "order; output shows ONE combined map with a layer legend. Use "
            "this instead of calling export once per layer."
        ),
    )
    basemap_raster: str = Field(
        default="",
        description=(
            "Optional raster file path (e.g. elevation GeoTIFF) drawn as the "
            "static-map backdrop beneath all layers."
        ),
    )
    title: str = Field(
        default="",
        description="Map title. Defaults to layer_name when empty.",
    )
    review_task: str = Field(
        default="",
        description="User goal and concrete requirements for visual map inspection.",
    )
    map_id: str = Field(
        default="",
        description="Stable logical map ID across all versions/formats.",
    )
    layout: MapLayout | None = Field(
        default=None,
        description="Optional presentation settings; visual review may adjust these.",
    )
    map_path: str = Field(
        default="", description="For action=project: existing rendered map input."
    )
    project_output_dir: str = Field(
        default="",
        description="New editable project directory within the workspace; never overwrites data.",
    )
    renderer: Literal["", "qgis", "matplotlib"] = Field(
        default="",
        description="Static renderer override; default QGIS. HTML/SVG keep their renderer.",
    )
    map_crs: str = Field(default="", description="Explicit display CRS; map_extent uses this CRS.")
    analysis_manifests: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Actual geo_analysis manifests for source-verified statistics and methods.",
    )
    qgis_layout: QgisProjectLayout | None = Field(
        default=None,
        description="Native layout: main map, optional detail, legend and footer.",
    )
    map_extent: list[float] | QgisExtent | None = Field(
        default=None,
        validation_alias=AliasChoices("map_extent", "project_extent"),
        description=(
            "Actual map-frame xmin,ymin,xmax,ymax in project/output CRS. "
            "Locks multilayer rendering or action=project's editable map frame. "
            "Use the existing map's bounds when context layers extend beyond its frame."
        ),
    )

    @model_validator(mode="after")
    def validate_extent(self) -> GeoCartographyInput:
        if isinstance(self.map_extent, QgisExtent):
            self.map_extent = [
                self.map_extent.xmin,
                self.map_extent.ymin,
                self.map_extent.xmax,
                self.map_extent.ymax,
            ]
        if self.map_extent is not None:
            import math

            if len(self.map_extent) != 4 or not all(map(math.isfinite, self.map_extent)):
                raise ValueError("map_extent requires four finite xmin,ymin,xmax,ymax coordinates")
            if self.map_extent[0] >= self.map_extent[2] or self.map_extent[1] >= self.map_extent[3]:
                raise ValueError("map_extent must have positive width and height")
            if not self.map_crs and self.action != "project":
                raise ValueError("explicit map_extent requires map_crs")
        if self.qgis_layout and self.qgis_layout.detail_extent and not self.map_crs:
            raise ValueError("explicit detail_extent requires map_crs")
        return self


class GeoCartographyTool(BaseTool):
    """Cartography pipeline orchestration + native rendering tool."""

    name: str = "geo_cartography"
    description: str = (
        "Cartography pipeline — static PNG/PDF uses the same QGIS layout and editable project. "
        "Explicit matplotlib and SVG/HTML remain available: symbolize → compose → export. "
        "Reads bridge rules from config to auto-select symbolization by analysis "
        "type. Provide a data source (file_path/source_type+source_name/geojson) "
        "to actually render; omit it to get a symbolization plan only. "
        "Rendering returns an editable QGIS project with data and print layout. "
        "action=preflight checks fields, geometry, CRS, bounds and visible counts without drawing. "
        "action=project packages an existing map with its actual sources without redrawing. "
        "Use analysis_manifests for output-backed statistics/method/footer. "
        "Returned map_id is the persistent task UUID; action=status reads its factual receipt. "
        "Keep that ID across all versions; use geo_deliver for final delivery."
    )
    input_model: type[BaseModel] = GeoCartographyInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        """Execute the geo_cartography tool."""
        assert isinstance(arguments, GeoCartographyInput)
        args: GeoCartographyInput = arguments

        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(
                output="Error: GeoHarness config not found in context.",
                is_error=True,
            )

        bridge_rules = config.cartography.bridge_rules
        template_path = config.cartography.default_template
        outputs_dir = config.cartography.outputs_dir or CartographyConfig().outputs_dir

        # Validate the layers JSON up front so a malformed payload is a clean
        # tool error instead of an exception inside the worker thread.
        if args.layers.strip():
            try:
                self._parse_layers(args.layers)
            except ValueError as exc:
                return ToolResult(output=f"Error: {exc}", is_error=True)

        if args.action == "symbolize":
            return self._symbolize(args, bridge_rules)
        if args.action == "compose":
            return self._compose(args, template_path)
        if args.action == "project":
            return await self._project_only(args, bridge_rules, outputs_dir, context)
        if args.action == "status":
            from geoharness.cartography.jobs import map_job_receipt
            from geoharness.cartography.tracking import receipt_summary

            try:
                receipt = map_job_receipt(
                    context.metadata,
                    cwd=context.cwd,
                    config=config,
                    map_id=args.map_id,
                    artifact_path=Path(args.map_path) if args.map_path else None,
                )
            except (OSError, ValueError) as exc:
                return ToolResult(output=f"Error: map status unavailable: {exc}", is_error=True)
            return ToolResult(
                output=json.dumps(receipt_summary(receipt), ensure_ascii=False, indent=2),
                metadata={"map_receipt": receipt},
            )
        if args.action == "preflight":
            if not self._has_source(args):
                return ToolResult(output="Error: preflight requires data sources", is_error=True)
            rule = self._get_bridge_rule(args.analysis_type or _THEMATIC_RULE_KEY, bridge_rules)
            return await self._render(args, rule, outputs_dir, context)
        if args.action == "export":
            return await self._export(args, bridge_rules, outputs_dir, context)
        if args.action == "full":
            return await self._full_pipeline(
                args, bridge_rules, template_path, outputs_dir, context
            )
        return ToolResult(output=f"Unknown action: {args.action}", is_error=True)

    # ── Bridge rules ──────────────────────────────────────────────────

    def _get_bridge_rule(
        self,
        analysis_type: str,
        bridge_rules: dict[str, Any],
    ) -> dict[str, Any]:
        """Look up the bridge rule for an analysis type (with fallback)."""
        rule = bridge_rules.get(analysis_type)
        if rule is None:
            return {
                "render_method": "categorized",
                "color_scheme": "Set1",
                "n_classes": 5,
            }
        if hasattr(rule, "render_method"):
            return {
                "render_method": rule.render_method,
                "color_scheme": rule.color_scheme,
                "n_classes": rule.n_classes,
            }
        return rule if isinstance(rule, dict) else dict(rule)

    # ── Plan mode ─────────────────────────────────────────────────────

    def _symbolize(
        self,
        args: GeoCartographyInput,
        bridge_rules: dict[str, Any],
    ) -> ToolResult:
        """Describe how the layer will be symbolized (no rendering).

        ``analysis_type`` is OPTIONAL: thematic cartography (火灾态势图等)
        has no spatial-analysis stage, so an empty value falls back to the
        plain "thematic" bridge rule instead of aborting the whole pipeline.
        """
        if not args.layer_name:
            return ToolResult(
                output="Error: 'layer_name' is required for symbolize action.",
                is_error=True,
            )

        analysis_type = args.analysis_type.strip() or _THEMATIC_RULE_KEY
        rule = self._get_bridge_rule(analysis_type, bridge_rules)
        method = rule.get("render_method", "categorized")
        colors = rule.get("color_scheme", "Set1")
        n_classes = rule.get("n_classes", 5)

        lines = [
            f"Symbolization plan for layer '{args.layer_name}':",
            f"  Analysis type: {analysis_type}",
            f"  Render method: {method}",
            f"  Color scheme: {colors}",
        ]

        if method == "categorized":
            field = args.field_name or "(auto-detect category field)"
            lines.append(
                f"  → Categorized rendering: field='{field}', "
                f"color_scheme='{colors}' (one color per category)"
            )
        elif method == "graduated":
            field = args.field_name or "(auto-detect value field)"
            lines.append(
                f"  → Graduated rendering: field='{field}', "
                f"color_scheme='{colors}', n_classes={n_classes}"
            )
        elif method == "flow":
            lines.append(
                f"  → Flow-line rendering for '{args.layer_name}' "
                f"(line width scales with field='{args.field_name or 'value'}')"
            )
        elif method == "none":
            lines.append(
                "  → No map symbolization needed for this analysis type (e.g., global statistic)."
            )

        return ToolResult(output="\n".join(lines), metadata={"bridge_rule": rule})

    def _compose(
        self,
        args: GeoCartographyInput,
        template_path: str,
    ) -> ToolResult:
        """Describe the composition (map elements) stage."""
        lines = [
            f"Composition plan (template: {template_path or '(none — native layout)'}):",
        ]
        lines.append("  1. map frame — symbolize the layer into the plot area")
        lines.append("  2. legend — color/class legend")
        lines.append("  3. scale bar — metric scale")
        lines.append("  4. north arrow — orientation indicator")
        lines.append("  5. title — analysis type and data source caption")
        return ToolResult(output="\n".join(lines))

    # ── Render helpers ────────────────────────────────────────────────

    def _has_source(self, args: GeoCartographyInput) -> bool:
        """True when the request carries renderable data."""
        return bool(
            args.geojson.strip() or args.file_path or args.source_name or args.layers.strip()
        )

    def _parse_layers(self, raw: str) -> list[dict[str, Any]]:
        """Parse the ``layers`` JSON array into plain dicts."""
        import json

        text = (raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"layers must be a JSON array of layer objects: {exc}") from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("layers must be a non-empty JSON array of layer objects.")
        out: list[dict[str, Any]] = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"layers[{i}] must be an object with source_type/source_name.")
            from geoharness.cartography.renderer import _layer_style_options

            _layer_style_options(item)
            if "required" in item and not isinstance(item["required"], bool):
                raise ValueError("Layer required must be a boolean")
            for key in ("expected_feature_count", "minimum_visible_features"):
                if key in item and (
                    not isinstance(item[key], int) or isinstance(item[key], bool) or item[key] < 0
                ):
                    raise ValueError(f"Layer {key} must be a nonnegative integer")
            out.append(item)
        return out

    def _resolve_output_path(self, args: GeoCartographyInput, outputs_dir: str) -> Path:
        """Resolve the output path (explicit, or outputs_dir/layer_name.*)."""
        suffix = _FORMAT_SUFFIXES.get(args.output_format, ".png")
        if args.output_path:
            path = Path(args.output_path).expanduser()
            if not path.suffix:
                path = path.with_suffix(suffix)
            return path

        stem = args.layer_name or args.source_name or "map"
        # Avoid path separators sneaking in from a layer label.
        stem = Path(str(stem).replace("/", "_").replace("\\", "_")).name
        directory = Path(outputs_dir).expanduser()
        return directory / f"{stem}{suffix}"

    def _render_blocking(
        self,
        args: GeoCartographyInput,
        rule: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Resolve the source, render the map, and report the written path.

        Blocking by design (geopandas/psycopg/matplotlib). Callers must run it
        off the event loop via ``asyncio.to_thread`` — a synchronous DB or
        render call would otherwise freeze the whole TUI.
        """
        catalog = context.metadata.get("data_catalog")

        # ── Multi-layer composition ───────────────────────────────────
        layer_specs = self._parse_layers(args.layers)
        if layer_specs:
            return self._render_layers_blocking(
                args,
                layer_specs,
                catalog,
                outputs_dir,
                context.metadata.get("_map_source_cache"),
            )

        from geoharness.cartography import render_map
        from geoharness.cartography.sources import resolve_geodataframe

        try:
            cache = context.metadata.get("_map_source_cache", {})
            if "single" not in cache:
                cache["single"] = resolve_geodataframe(
                    source_type=args.source_type,
                    file_path=args.file_path,
                    source_name=args.source_name,
                    geojson_text=args.geojson,
                    catalog=catalog,
                )
            gdf = cache["single"]
            if gdf.crs is None:
                raise ValueError("Data source has no CRS; explicitly declare it before mapping.")
            if args.map_crs:
                gdf = gdf.to_crs(args.map_crs)
        except ValueError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except Exception as exc:  # network / driver failures
            logger.exception("Data source resolution failed")
            return ToolResult(output=f"Error: failed to load source — {exc}", is_error=True)

        method = rule.get("render_method", "categorized")
        scheme = rule.get("color_scheme", "Set1")
        n_classes = rule.get("n_classes", 5) or 5
        output_path = self._resolve_output_path(args, outputs_dir)
        title = args.title or args.layer_name

        try:
            written = render_map(
                gdf,
                output_path=output_path,
                column=args.field_name,
                render_method=method,
                color_scheme=scheme,
                n_classes=n_classes,
                title=title,
                extent=args.map_extent,
                **({"layout": args.layout} if output_path.suffix.lower() != ".html" else {}),
            )
        except ValueError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("Rendering failed")
            return ToolResult(output=f"Error: map rendering failed — {exc}", is_error=True)

        size_kb = written.stat().st_size / 1024.0
        lines = [
            f"Rendered map: {written}",
            f"  Format: {args.output_format}",
            f"  Features: {len(gdf)}",
            f"  Render method: {method}",
            f"  Color scheme: {scheme}",
        ]
        if args.field_name:
            lines.append(f"  Symbolized field: {args.field_name}")
        if method == "flow":
            lines.append("  Line width scales with the symbolized field")
        lines.append(f"  Size: {size_kb:.1f} KB")

        return ToolResult(
            output="\n".join(lines),
            metadata={
                "bridge_rule": rule,
                "output_path": str(written),
                "feature_count": len(gdf),
                "source_crs": str(gdf.crs),
                "bounds": gdf.total_bounds.tolist(),
            },
        )

    def _render_layers_blocking(
        self,
        args: GeoCartographyInput,
        layer_specs: list[dict[str, Any]],
        catalog: Any,
        outputs_dir: str,
        source_cache: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Resolve every layer spec and compose ONE multi-layer map."""
        from geoharness.cartography import render_layers
        from geoharness.cartography.renderer import _layer_style_options
        from geoharness.cartography.sources import resolve_geodataframe

        layers: list[dict[str, Any]] = []
        total_features = 0
        for i, spec in enumerate(layer_specs):
            source_type = str(spec.get("source_type", "") or "file")
            name = str(spec.get("source_name") or spec.get("file_path") or "").strip()
            try:
                cache = source_cache if source_cache is not None else {}
                cache_key = f"layer_{i}"
                if cache_key not in cache:
                    cache[cache_key] = resolve_geodataframe(
                        source_type=source_type,
                        file_path=str(spec.get("file_path", "") or ""),
                        source_name=name,
                        geojson_text=str(spec.get("geojson", "") or ""),
                        catalog=catalog,
                    )
                gdf = cache[cache_key]
                if args.map_crs:
                    gdf = gdf.to_crs(args.map_crs)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Layer %d (%s) failed to resolve", i, name)
                return ToolResult(
                    output=(
                        f"Error: layer {i + 1} ({name or 'unnamed'}) could not be loaded — {exc}"
                    ),
                    is_error=True,
                )
            if gdf.empty and not spec.get("required", True):
                continue
            total_features += len(gdf)
            layers.append(
                {
                    "gdf": gdf,
                    "label": str(spec.get("label") or name or f"layer_{i + 1}"),
                    **_layer_style_options(spec),
                }
            )

        output_path = self._resolve_output_path(args, outputs_dir)
        title = args.title or args.layer_name
        try:
            written = render_layers(
                layers,
                title=title,
                output_path=output_path,
                basemap_raster=str(args.basemap_raster or ""),
                layout=args.layout,
                raster_cache=source_cache,
                extent=args.map_extent,
                extent_crs=args.map_crs,
            )
        except ValueError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Multi-layer rendering failed")
            return ToolResult(
                output=f"Error: multi-layer rendering failed — {exc}",
                is_error=True,
            )

        size_kb = written.stat().st_size / 1024.0
        lines = [
            f"Rendered multi-layer map: {written}",
            f"  Layers ({len(layers)}): " + ", ".join(str(item["label"]) for item in layers),
            f"  Total features: {total_features}",
            f"  Format: {args.output_format}",
        ]
        if args.basemap_raster:
            lines.append(f"  Basemap raster: {args.basemap_raster}")
        lines.append(f"  Size: {size_kb:.1f} KB")
        return ToolResult(
            output="\n".join(lines),
            metadata={
                "output_path": str(written),
                "feature_count": total_features,
                "layer_count": len(layers),
            },
        )

    async def _run_off_loop(self, work: Any, *, what: str) -> ToolResult:
        """Run blocking render work in a worker thread under a hard timeout.

        Keeps the async event loop (and therefore the TUI) responsive while
        geopandas/psycopg/matplotlib work happens. ``what`` names the job in
        user-facing timeout/error messages.
        """
        try:
            return await asyncio.wait_for(asyncio.to_thread(work), timeout=_RENDER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"Error: {what} timed out after "
                    f"{_RENDER_TIMEOUT_SECONDS:.0f}s. The source may be very "
                    "large — reduce the row/layer count, narrow the extent, "
                    "or choose a faster format."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("%s failed", what)
            return ToolResult(output=f"Error: {what} failed — {exc}", is_error=True)

    async def _render(
        self,
        args: GeoCartographyInput,
        rule: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Render a single layer off the event loop."""
        try:
            from geoharness.cartography import render_map  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency guard
            return ToolResult(
                output=f"Error: rendering dependencies unavailable ({exc}).",
                is_error=True,
            )

        config = context.metadata["geoharness_config"]
        context.metadata.setdefault("map_workflow_state", {})
        context.metadata.setdefault("map_job_state", {})
        context = ToolExecutionContext(
            cwd=context.cwd,
            metadata={**context.metadata, "_map_source_cache": {}},
            hook_executor=context.hook_executor,
        )
        desired = self._resolve_output_path(args, outputs_dir)
        if not desired.is_absolute():
            desired = context.cwd / desired
        desired = desired.resolve()
        configured_root = Path(outputs_dir).expanduser()
        if not configured_root.is_absolute():
            configured_root = context.cwd / configured_root
        if not any(
            desired.is_relative_to(root)
            for root in (
                context.cwd.resolve(),
                configured_root.resolve(),
            )
        ):
            return ToolResult(
                output="Error: map output must stay in the workspace or outputs_dir.", is_error=True
            )

        def workspace_path(value: str) -> str:
            if not value:
                return ""
            source = Path(value).expanduser()
            return str((source if source.is_absolute() else context.cwd / source).resolve())

        catalog = context.metadata.get("data_catalog")
        connector = catalog.get_connector("file") if catalog is not None else None

        def file_name(value: str, source_type: str) -> str:
            if not value or source_type not in {"", "file"}:
                return value
            candidate = Path(workspace_path(value))
            if candidate.is_file():
                return str(candidate)
            if connector is not None:
                for attribute in ("resolve_path", "resolve"):
                    resolver = getattr(connector, attribute, None)
                    if callable(resolver):
                        resolved = resolver(value)
                        if resolved is not None:
                            return str(Path(resolved).resolve())
                        break
            return value

        import json

        try:
            specs = self._parse_layers(args.layers)
            for spec in specs:
                if spec.get("file_path"):
                    spec["file_path"] = workspace_path(str(spec["file_path"]))
                if spec.get("source_name"):
                    spec["source_name"] = file_name(
                        str(spec["source_name"]),
                        str(spec.get("source_type") or "file"),
                    )
            args = args.model_copy(
                update={
                    "file_path": workspace_path(args.file_path),
                    "basemap_raster": workspace_path(args.basemap_raster),
                    "source_name": file_name(args.source_name, args.source_type),
                    "layers": json.dumps(specs, ensure_ascii=False) if specs else "",
                }
            )
        except Exception as exc:
            return ToolResult(output=f"Error: source path resolution failed — {exc}", is_error=True)
        inputs = [args.file_path, args.basemap_raster]
        if args.source_type in {"", "file"}:
            inputs.append(args.source_name)
        for spec in specs:
            inputs.append(spec.get("file_path", ""))
            if spec.get("source_type", "file") in {"", "file"}:
                inputs.append(spec.get("source_name", ""))
        for value in inputs:
            if value:
                source = Path(value).expanduser()
                source = source if source.is_absolute() else context.cwd / source
                if source.resolve() == desired:
                    return ToolResult(
                        output="Error: map output must not overwrite a data source.", is_error=True
                    )
        args = args.model_copy(
            update={
                "output_path": str(desired),
                "layout": args.layout or config.map_review.layout,
            }
        )
        preflight = await self._run_off_loop(
            lambda: self._preflight_blocking(args, rule, context),
            what="map preflight",
        )
        if preflight.is_error or args.action == "preflight":
            return preflight
        args = args.model_copy(
            update={
                "map_crs": preflight.metadata["preflight"]["display_crs"],
                "map_extent": preflight.metadata["preflight"]["map_extent"],
            }
        )
        context.metadata["map_preflight"] = preflight.metadata["preflight"]
        renderer = args.renderer or config.cartography.static_renderer
        native = renderer == "qgis" and desired.suffix.lower() in {".png", ".pdf"}
        if not native and args.basemap_raster and desired.suffix.lower() != ".html":
            from pyproj import CRS

            raster_report = next(
                layer
                for layer in preflight.metadata["preflight"]["layers"]
                if layer["kind"] == "raster"
            )
            same_crs = CRS.from_user_input(raster_report["source_crs"]) == CRS.from_user_input(
                args.map_crs
            )
            if not same_crs:
                return ToolResult(
                    output="Error: Matplotlib requires the basemap CRS; use QGIS "
                    "to reproject the backdrop to map_crs.",
                    is_error=True,
                )
        if not native and args.qgis_layout is not None:
            return ToolResult(
                output="Error: qgis_layout requires QGIS PNG/PDF output", is_error=True
            )
        import hashlib

        identity = (
            args.map_id
            or hashlib.sha256(
                json.dumps(
                    {
                        "source": [
                            args.source_type,
                            args.source_name,
                            args.file_path,
                            args.geojson,
                        ],
                        "layers": [
                            {
                                k: v
                                for k, v in spec.items()
                                if k in {"source_type", "source_name", "file_path", "geojson"}
                            }
                            for spec in specs
                        ],
                        "field": args.field_name,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:16]
        )
        args = args.model_copy(update={"map_id": identity})
        from geoharness.cartography.tracking import bind_map_job, track_render_result

        try:
            identity = bind_map_job(context, identity)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: map task could not start: {exc}", is_error=True)
        args = args.model_copy(update={"map_id": identity})
        if native:
            from geoharness.cartography.qgis_bridge import render_qgis_map

            return await render_qgis_map(self, args, rule, context)
        stopped = reserve_map_render(identity, context.metadata, config)
        if stopped:
            return ToolResult(output=stopped, metadata={"render_budget_exhausted": True})
        try:
            result = await self._run_off_loop(
                lambda: self._render_blocking(args, rule, outputs_dir, context),
                what="map rendering",
            )
        except BaseException:
            refund_map_render(identity, context.metadata)
            raise
        if result.is_error or not result.metadata.get("output_path"):
            refund_map_render(identity, context.metadata)
            return result
        checked = await self._inspect_and_repair(args, rule, outputs_dir, context, result)
        attached = await self._attach_project(args, rule, context, checked)
        attached.metadata["preflight"] = preflight.metadata["preflight"]
        return await asyncio.to_thread(
            track_render_result,
            attached,
            context,
            identity,
            preflight=preflight.metadata["preflight"],
            analysis=context.metadata.get("map_analysis_facts"),
        )

    def _preflight_blocking(self, args, rule, context) -> ToolResult:
        import json

        from geoharness.cartography.preflight import validate_frames
        from geoharness.cartography.renderer import _layer_style_options
        from geoharness.cartography.sources import resolve_geodataframe

        specs = self._parse_layers(args.layers)
        if not specs:
            specs = [
                {
                    "source_type": args.source_type,
                    "file_path": args.file_path,
                    "source_name": args.source_name,
                    "geojson": args.geojson,
                    "label": args.layer_name or args.source_name or "Map",
                }
            ]
        frames = []
        cache = context.metadata["_map_source_cache"]
        for index, spec in enumerate(specs):
            gdf = resolve_geodataframe(
                source_type=spec.get("source_type", ""),
                file_path=spec.get("file_path", ""),
                source_name=spec.get("source_name", ""),
                geojson_text=spec.get("geojson", ""),
                catalog=context.metadata.get("data_catalog"),
            )
            if spec.get("derivation") is not None:
                from geoharness.cartography.derivation import derive_frame

                gdf = derive_frame(gdf, spec["derivation"])
            options = (
                _layer_style_options(spec)
                if args.layers
                else {
                    **rule,
                    "column": args.field_name,
                    "render_method": rule.get("render_method", "categorized")
                    if args.field_name
                    else "none",
                }
            )
            frames.append(
                {
                    "gdf": gdf,
                    "style": options,
                    "label": spec.get("label", ""),
                    "label_field": spec.get("label_field", ""),
                    "required": spec.get("required", True),
                    "expected_feature_count": spec.get("expected_feature_count"),
                    "minimum_visible_features": spec.get(
                        "minimum_visible_features", 1 if spec.get("required", True) else 0
                    ),
                }
            )
        if args.basemap_raster:
            frames.append({"raster_path": args.basemap_raster, "label": "Basemap"})
        settings = context.metadata["geoharness_config"].cartography
        report = validate_frames(
            frames,
            map_crs=args.map_crs,
            map_extent=args.map_extent,
            detail_extent=(
                args.qgis_layout.detail_extent.model_dump()
                if args.qgis_layout and args.qgis_layout.detail_extent
                else None
            ),
            minimum_span_geographic=settings.minimum_span_geographic,
            minimum_span_projected=settings.minimum_span_projected,
            extent_padding=settings.extent_padding,
        )
        for index, frame in enumerate(frames[: len(specs)]):
            cache[f"layer_{index}" if args.layers else "single"] = frame["gdf"]
        context.metadata["map_analysis_facts"] = None
        if args.analysis_manifests:
            from geoharness.cartography.facts import load_analysis_facts
            from geoharness.tools.geo_qgis import qgis_read_roots

            facts = load_analysis_facts(
                args.analysis_manifests,
                cwd=context.cwd,
                read_roots=qgis_read_roots(context.metadata["geoharness_config"], context.cwd),
                mapped_sources=[
                    {
                        "file_path": spec.get("file_path") or spec.get("source_name", ""),
                        "gdf": frame["gdf"],
                        "label": spec.get("label", ""),
                        "derivation": spec.get("derivation"),
                    }
                    for spec, frame in zip(specs, frames)
                ],
                display_crs=report["display_crs"],
            )
            report["analysis"] = facts
            context.metadata["map_analysis_facts"] = facts
        return ToolResult(
            output=json.dumps(report, ensure_ascii=False, indent=2), metadata={"preflight": report}
        )

    async def _inspect_and_repair(
        self,
        args: GeoCartographyInput,
        rule: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
        result: ToolResult,
    ) -> ToolResult:
        from geoharness.cartography.quality import (
            MapReview,
            apply_layout_patch,
            save_review,
        )

        config = context.metadata["geoharness_config"]
        written = Path(result.metadata["output_path"])
        artifacts = Path(tempfile.mkdtemp(prefix=f"{written.stem}.review-", dir=written.parent))
        history: list[dict[str, Any]] = []
        budget = {"review_cached": False, "review_budget_exhausted": False}
        evidence = {
            "title": args.title or args.layer_name,
            "field": args.field_name,
            "analysis_type": args.analysis_type,
            "render_metadata": result.metadata,
            "expected_layers": [
                {key: spec[key] for key in ("label", "source_name", "color") if key in spec}
                for spec in self._parse_layers(args.layers)
            ],
        }
        current = args
        preview: Path | None = written if written.suffix.lower() == ".png" else None
        client = context.metadata.get("map_review_client")
        key = config.map_review.api_key or config.api_key
        can_review = config.map_review.enabled and (client is not None or (key and "${" not in key))
        for attempt in range(config.map_review.max_repairs + 1):
            if not can_review:
                checked = MapReview(
                    status="unverified",
                    summary=(
                        "Visual map review is disabled."
                        if not config.map_review.enabled
                        else "No usable map review API credential configured."
                    ),
                )
                history.append({"attempt": attempt, **checked.model_dump()})
                break
            if written.suffix.lower() != ".png":
                preview = artifacts / "preview.png"
                preview_args = current.model_copy(
                    update={
                        "output_format": "png",
                        "output_path": str(preview),
                    }
                )
                preview_result = await self._run_off_loop(
                    lambda: self._render_blocking(preview_args, rule, outputs_dir, context),
                    what="map preview rendering",
                )
                if preview_result.is_error:
                    checked = MapReview(
                        status="unverified",
                        summary="PNG preview could not render.",
                    )
                    history.append({"attempt": attempt, **checked.model_dump()})
                    break
            checked, budget = await review_with_budget(
                preview,
                config=config,
                task=args.review_task or args.title or args.layer_name,
                evidence={**evidence, "layout": current.layout.model_dump()},
                metadata=context.metadata,
                map_id=args.map_id,
                client=client,
            )
            if written.suffix.lower() == ".html" and checked.status == "passed":
                checked = MapReview(
                    status="unverified",
                    summary="Static preview passed; interactive HTML has not been inspected.",
                )
            history.append(
                {
                    "attempt": attempt,
                    "layout": current.layout.model_dump(),
                    **checked.model_dump(),
                    **budget,
                }
            )
            if checked.status != "issues" or attempt >= config.map_review.max_repairs:
                break
            if not any(issue.severity in {"error", "warning"} for issue in checked.issues):
                break
            patched = apply_layout_patch(current.layout, checked.layout_patch)
            if patched == current.layout:
                break
            stopped = reserve_map_render(args.map_id, context.metadata, config)
            if stopped:
                break
            snapshot = artifacts / f"attempt-{attempt}{written.suffix}"
            previous = current
            try:
                await asyncio.to_thread(shutil.copyfile, written, snapshot)
                history[-1]["map_snapshot"] = str(snapshot)
                current = current.model_copy(update={"layout": patched})
                next_result = await self._run_off_loop(
                    lambda: self._render_blocking(current, rule, outputs_dir, context),
                    what="map repair rendering",
                )
            except BaseException:
                refund_map_render(args.map_id, context.metadata)
                raise
            if next_result.is_error or not next_result.metadata.get("output_path"):
                refund_map_render(args.map_id, context.metadata)
                await asyncio.to_thread(shutil.copyfile, snapshot, written)
                current = previous
                checked = MapReview(
                    status="unverified",
                    summary="Map repair could not render.",
                    issues=checked.issues,
                )
                history.append({"attempt": attempt + 1, **checked.model_dump()})
                break
            result = next_result
        report_payload = {
            "output_path": str(written),
            "preview_path": str(preview) if preview else None,
            **budget,
            **checked.model_dump(),
            "repair_count": max(0, len(history) - 1),
            "history": history,
            "evidence": evidence,
            "scope": "Visual presentation; numeric analysis correctness requires data validation.",
            "reviewed_artifact": (
                ("exported_png" if preview == written else "static_png_preview")
                if can_review and preview
                else None
            ),
        }
        report = await asyncio.to_thread(save_review, written, report_payload, report_dir=artifacts)
        return ToolResult(
            output=(
                f"{result.output}\nVisual review: {checked.status}\n"
                f"  {checked.summary}\n  Report: {report}\n"
                f"  Repairs: {report_payload['repair_count']}"
            ),
            metadata={
                **result.metadata,
                "review_status": checked.status,
                "review_path": str(report),
                "preview_path": str(preview) if preview else None,
                "reviewed_artifact": report_payload["reviewed_artifact"],
                "final_layout": current.layout.model_dump(),
                **budget,
                "repair_count": report_payload["repair_count"],
            },
        )

    async def _attach_project(
        self,
        args: GeoCartographyInput,
        rule: dict[str, Any],
        context: ToolExecutionContext,
        result: ToolResult,
    ) -> ToolResult:
        from geoharness.cartography.project import export_map_project
        from geoharness.cartography.renderer import _layer_style_options
        from geoharness.cartography.sources import resolve_geodataframe

        written = Path(result.metadata["output_path"])
        cache = context.metadata.get("_map_source_cache", {})
        catalog = context.metadata.get("data_catalog")

        def export() -> dict[str, Any]:
            specs = self._parse_layers(args.layers)
            layers = []
            for index, spec in enumerate(
                specs
                or [
                    {
                        "source_type": args.source_type,
                        "file_path": args.file_path,
                        "source_name": args.source_name,
                        "geojson": args.geojson,
                        "label": args.layer_name,
                    }
                ]
            ):
                key = f"layer_{index}" if specs else "single"
                gdf = cache.get(key)
                if gdf is None:
                    source_type = str(spec.get("source_type") or "file")
                    source_name = str(spec.get("source_name") or "")
                    if source_name and source_type == "file":
                        named_path = Path(source_name).expanduser()
                        named_path = (
                            named_path if named_path.is_absolute() else context.cwd / named_path
                        )
                        if named_path.is_file():
                            source_name = str(named_path.resolve())
                    source = str(spec.get("file_path") or "")
                    if source:
                        p = Path(source).expanduser()
                        source = str(p if p.is_absolute() else context.cwd / p)
                    gdf = resolve_geodataframe(
                        source_type=source_type,
                        file_path=source,
                        source_name=source_name,
                        geojson_text=str(spec.get("geojson") or ""),
                        catalog=catalog,
                    )
                if args.map_crs:
                    gdf = gdf.to_crs(args.map_crs)
                if gdf.empty and not spec.get("required", True):
                    continue
                layers.append(
                    {
                        "gdf": gdf,
                        "label": str(spec.get("label") or args.layer_name or f"layer{index}"),
                        **(
                            _layer_style_options(spec)
                            if specs
                            else {
                                "column": args.field_name,
                                "render_method": rule.get("render_method", "categorized"),
                                "color_scheme": rule.get("color_scheme", "Set1"),
                                "n_classes": rule.get("n_classes", 5),
                            }
                        ),
                    }
                )
            kwargs = {
                "mode": "layers" if specs else "single",
                "title": args.title or args.layer_name,
            }
            if specs:
                raster = Path(args.basemap_raster).expanduser() if args.basemap_raster else None
                kwargs["basemap_raster"] = (
                    str(raster if raster is None or raster.is_absolute() else context.cwd / raster)
                    if raster
                    else ""
                )
                kwargs["raster_cache"] = cache
            else:
                kwargs.update(
                    {
                        k: layers[0][k]
                        for k in (
                            "column",
                            "render_method",
                            "color_scheme",
                            "n_classes",
                        )
                    }
                )
            if args.project_output_dir:
                directory = Path(args.project_output_dir).expanduser()
                directory = (
                    directory if directory.is_absolute() else context.cwd / directory
                ).resolve()
                if not directory.is_relative_to(context.cwd.resolve()):
                    raise ValueError("project_output_dir must remain inside the workspace")
            else:
                directory = written.parent / f"{written.stem}.project-{uuid.uuid4().hex[:12]}"
            return export_map_project(
                directory,
                layers=layers,
                output_path=written,
                render_kwargs=kwargs,
                layout=MapLayout.model_validate(
                    result.metadata.get("final_layout") or (args.layout or MapLayout()).model_dump()
                ),
                extent=args.map_extent,
                extent_crs=args.map_crs,
                publication_settings=context.metadata["geoharness_config"].publication,
            )

        try:
            project = await asyncio.to_thread(export)
        except Exception as exc:
            return ToolResult(
                output=f"{result.output}\nEditable project export failed: {exc}",
                is_error=True,
                metadata={
                    **result.metadata,
                    "project_status": "failed",
                    **({"publication": exc.diagnostics} if hasattr(exc, "diagnostics") else {}),
                },
            )
        return ToolResult(
            output=(
                f"{result.output}\nEditable QGIS project: {project['qgis_project']}"
                f"\nPortable project folder: {project['project_dir']}"
            ),
            metadata={
                **result.metadata,
                **project,
                "project_status": "created",
            },
        )

    async def _project_only(
        self,
        args: GeoCartographyInput,
        rules: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        path = (
            Path(args.map_path) if args.map_path else self._resolve_output_path(args, outputs_dir)
        )
        path = (path if path.is_absolute() else context.cwd / path).resolve()
        root = Path(outputs_dir).expanduser()
        root = (root if root.is_absolute() else context.cwd / root).resolve()
        if (
            not path.is_file()
            or path.suffix.lower()
            not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".pdf",
                ".svg",
                ".html",
            }
            or not any(path.is_relative_to(p) for p in (context.cwd.resolve(), root))
        ):
            return ToolResult(
                output="Error: project requires an existing map in workspace/outputs.",
                is_error=True,
            )
        if not self._has_source(args):
            return ToolResult(
                output="Error: project requires the map's actual layer sources.", is_error=True
            )
        result = await self._attach_project(
            args.model_copy(
                update={
                    "layout": args.layout or context.metadata["geoharness_config"].map_review.layout
                }
            ),
            self._get_bridge_rule(args.analysis_type, rules),
            context,
            ToolResult(
                output=f"Map retained: {path}",
                metadata={
                    "output_path": str(path),
                    "review_status": "unverified",
                    "quality_status": "draft",
                },
            ),
        )
        if result.is_error:
            return result
        from geoharness.cartography.tracking import bind_map_job, track_render_result

        identity = bind_map_job(context, args.map_id, path)
        return await asyncio.to_thread(track_render_result, result, context, identity)

    async def _export(
        self,
        args: GeoCartographyInput,
        bridge_rules: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Render the map when data is available, else return the export plan."""
        if self._has_source(args):
            rule = self._get_bridge_rule(args.analysis_type, bridge_rules)
            return await self._render(args, rule, outputs_dir, context)

        output_path = self._resolve_output_path(args, outputs_dir)
        method = self._get_bridge_rule(args.analysis_type, bridge_rules).get(
            "render_method", "categorized"
        )
        suffix = output_path.suffix
        lines = [
            f"Export plan ({args.output_format}) — Native renderer:",
            f"  Render method: {method}",
            f"  Target file: {output_path}",
            "  No data source supplied — provide file_path / source_type + "
            "source_name / geojson to render.",
        ]
        if suffix == ".html":
            lines.append("  → interactive Leaflet map (folium)")
        else:
            lines.append("  → static map (matplotlib, headless Agg backend)")
        return ToolResult(output="\n".join(lines))

    async def _full_pipeline(
        self,
        args: GeoCartographyInput,
        bridge_rules: dict[str, Any],
        template_path: str,
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Full pipeline: symbolize → compose → export.

        With a ``layers`` payload each spec already carries its own styling,
        so the per-layer symbolize step (which needs ``layer_name``) is
        skipped and the request goes straight to multi-layer rendering.
        """
        layer_specs = self._parse_layers(args.layers)
        if layer_specs or (self._has_source(args) and not args.layer_name):
            rule = self._get_bridge_rule(args.analysis_type, bridge_rules)
            return await self._render(args, rule, outputs_dir, context)

        sym_result = self._symbolize(args, bridge_rules)
        if sym_result.is_error:
            return sym_result

        comp_result = self._compose(args, template_path)
        if comp_result.is_error:
            return comp_result

        rule = sym_result.metadata.get("bridge_rule", {})
        if self._has_source(args):
            exp_result = await self._render(args, rule, outputs_dir, context)
        else:
            exp_result = await self._export(args, bridge_rules, outputs_dir, context)

        combined = "\n\n".join([sym_result.output, comp_result.output, exp_result.output])
        return ToolResult(
            output=f"Full cartography pipeline:\n\n{combined}",
            is_error=exp_result.is_error,
            metadata={
                "bridge_rule": rule,
                **exp_result.metadata,
            },
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """symbolize/compose only plan; export/full write files."""
        assert isinstance(arguments, GeoCartographyInput)
        return arguments.action in ("symbolize", "compose", "preflight", "status")
