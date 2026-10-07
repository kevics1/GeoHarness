"""GeoCartographyTool — native cartography pipeline.

Pipeline: ``symbolize → compose → export``.

Two modes:

* **Plan mode (no data source given)** — returns a human-readable plan
  describing how the layer *would* be symbolized/composed/exported. Used by
  the agent to reason about styling before loading data.
* **Render mode (a data source is given)** — resolves the source to a
  ``GeoDataFrame`` and renders it natively via
  :mod:`geoharness.cartography` (matplotlib → PNG/PDF/SVG, folium → HTML).

Bridge rules (analysis type → symbolization) stay config-driven: adding a new
analysis type is a YAML edit, not a code change. No external desktop GIS and
no ``.qgs``/``.qpt`` dependency — rendering is pure Python.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import AliasChoices, BaseModel, Field

from geoharness.cartography.workflow import (
    refund_map_render,
    reserve_map_render,
    review_with_budget,
)
from geoharness.config.settings import CartographyConfig, MapLayout

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

    action: Literal["symbolize", "compose", "export", "full", "project"] = Field(
        default="full",
        description=(
            "symbolize: plan symbolization for a layer (no rendering); "
            "compose: describe map composition elements; "
            "export: arrange elements and write the map file; "
            "full: symbolize + compose + export in one step; "
            "project: create an editable QGIS project for an existing output map"
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
    map_extent: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        validation_alias=AliasChoices("map_extent", "project_extent"),
        description=(
            "Actual map-frame xmin,ymin,xmax,ymax in project/output CRS. "
            "Locks multilayer rendering or action=project's editable map frame. "
            "Use the existing map's bounds when context layers extend beyond its frame."
        ),
    )


class GeoCartographyTool(BaseTool):
    """Cartography pipeline orchestration + native rendering tool."""

    name: str = "geo_cartography"
    description: str = (
        "Cartography pipeline — render maps natively (PNG/PDF/SVG + interactive "
        "HTML) via matplotlib/geopandas/folium: symbolize → compose → export. "
        "Reads bridge rules from config to auto-select symbolization by analysis "
        "type. Provide a data source (file_path/source_type+source_name/geojson) "
        "to actually render; omit it to get a symbolization plan only. "
        "Rendering returns an editable QGIS project with data and print layout. "
        "action=project packages an existing map with its actual sources without redrawing. "
        "Keep the same map_id across all versions; use geo_deliver for final delivery."
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
            except Exception as exc:  # noqa: BLE001
                logger.exception("Layer %d (%s) failed to resolve", i, name)
                return ToolResult(
                    output=(
                        f"Error: layer {i + 1} ({name or 'unnamed'}) could not be loaded — {exc}"
                    ),
                    is_error=True,
                )
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
        return await self._attach_project(args, rule, context, checked)

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
                    status="unverified", summary="Map repair could not render.",
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

        def export() -> dict[str, str]:
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
                layers.append(
                    {
                        "gdf": gdf,
                        "label": str(spec.get("label") or args.layer_name or f"layer{index}"),
                        **(_layer_style_options(spec) if specs else {
                            "column": args.field_name,
                            "render_method": rule.get("render_method", "categorized"),
                            "color_scheme": rule.get("color_scheme", "Set1"),
                            "n_classes": rule.get("n_classes", 5),
                        }),
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
            directory = Path(
                tempfile.mkdtemp(
                    prefix=f"{written.stem}.project-",
                    dir=written.parent,
                )
            )
            return export_map_project(
                directory,
                layers=layers,
                output_path=written,
                render_kwargs=kwargs,
                layout=MapLayout.model_validate(
                    result.metadata.get("final_layout") or (args.layout or MapLayout()).model_dump()
                ),
                extent=args.map_extent,
            )

        try:
            project = await asyncio.to_thread(export)
        except Exception as exc:
            return ToolResult(
                output=f"{result.output}\nEditable project export failed: {exc}",
                is_error=True,
                metadata={**result.metadata, "project_status": "failed"},
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
        path = self._resolve_output_path(args, outputs_dir)
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
        return await self._attach_project(
            args.model_copy(
                update={
                    "layout": args.layout or context.metadata["geoharness_config"].map_review.layout
                }
            ),
            self._get_bridge_rule(args.analysis_type, rules),
            context,
            ToolResult(output=f"Map retained: {path}", metadata={
                "output_path": str(path), "review_status": "unverified",
                "quality_status": "draft",
            }),
        )

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
        return arguments.action in ("symbolize", "compose")
