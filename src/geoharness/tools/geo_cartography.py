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
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_OUTPUTS_DIR = "~/.geoharness/exports"

# Hard ceiling for a single render. Prevents a pathological source (huge
# table, unreachable DB) from hanging the tool forever.
_RENDER_TIMEOUT_SECONDS = 300.0

# Bridge-rule render methods understood by the native renderer.
_RENDER_METHODS = ("categorized", "graduated", "flow", "none")

_FORMAT_SUFFIXES = {
    "pdf": ".pdf",
    "svg": ".svg",
    "png": ".png",
    "image": ".png",
    "html": ".html",
}


class GeoCartographyInput(BaseModel):
    """Input model for geo_cartography tool."""

    action: Literal["symbolize", "compose", "export", "full"] = Field(
        default="full",
        description=(
            "symbolize: plan symbolization for a layer (no rendering); "
            "compose: describe map composition elements; "
            "export: arrange elements and write the map file; "
            "full: symbolize + compose + export in one step"
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
            "directory (~/.geoharness/exports) using layer_name as file stem."
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
            "color, and optional geojson inline data. Layers draw in array "
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


class GeoCartographyTool(BaseTool):
    """Cartography pipeline orchestration + native rendering tool."""

    name: str = "geo_cartography"
    description: str = (
        "Cartography pipeline — render maps natively (PNG/PDF/SVG + interactive "
        "HTML) via matplotlib/geopandas/folium: symbolize → compose → export. "
        "Reads bridge rules from config to auto-select symbolization by analysis "
        "type. Provide a data source (file_path/source_type+source_name/geojson) "
        "to actually render; omit it to get a symbolization plan only."
    )
    input_model: type[BaseModel] = GeoCartographyInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
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
        outputs_dir = getattr(config.cartography, "outputs_dir", "") or _DEFAULT_OUTPUTS_DIR

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
        """Describe how the layer will be symbolized (no rendering)."""
        if not args.layer_name:
            return ToolResult(
                output="Error: 'layer_name' is required for symbolize action.",
                is_error=True,
            )
        if not args.analysis_type:
            return ToolResult(
                output="Error: 'analysis_type' is required for symbolize. "
                "Specify the analysis type (e.g., 'moran_local', 'getis_ord').",
                is_error=True,
            )

        rule = self._get_bridge_rule(args.analysis_type, bridge_rules)
        method = rule.get("render_method", "categorized")
        colors = rule.get("color_scheme", "Set1")
        n_classes = rule.get("n_classes", 5)

        lines = [
            f"Symbolization plan for layer '{args.layer_name}':",
            f"  Analysis type: {args.analysis_type}",
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
                "  → No map symbolization needed for this analysis type "
                "(e.g., global statistic)."
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
            args.geojson.strip() or args.file_path or args.source_name
            or args.layers.strip()
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
            raise ValueError(
                f"layers must be a JSON array of layer objects: {exc}"
            ) from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError(
                "layers must be a non-empty JSON array of layer objects."
            )
        out: list[dict[str, Any]] = []
        for i, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(
                    f"layers[{i}] must be an object with source_type/source_name."
                )
            out.append(item)
        return out

    def _resolve_output_path(
        self, args: GeoCartographyInput, outputs_dir: str
    ) -> Path:
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
                args, layer_specs, catalog, outputs_dir
            )

        from geoharness.cartography import render_map
        from geoharness.cartography.sources import resolve_geodataframe

        try:
            gdf = resolve_geodataframe(
                source_type=args.source_type,
                file_path=args.file_path,
                source_name=args.source_name,
                geojson_text=args.geojson,
                catalog=catalog,
            )
        except ValueError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except Exception as exc:  # network / driver failures
            logger.exception("Data source resolution failed")
            return ToolResult(
                output=f"Error: failed to load source — {exc}", is_error=True
            )

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
            )
        except ValueError as exc:
            return ToolResult(output=f"Error: {exc}", is_error=True)
        except Exception as exc:
            logger.exception("Rendering failed")
            return ToolResult(
                output=f"Error: map rendering failed — {exc}", is_error=True
            )

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
            },
        )

    def _render_layers_blocking(
        self,
        args: GeoCartographyInput,
        layer_specs: list[dict[str, Any]],
        catalog: Any,
        outputs_dir: str,
    ) -> ToolResult:
        """Resolve every layer spec and compose ONE multi-layer map."""
        from geoharness.cartography import render_layers
        from geoharness.cartography.sources import resolve_geodataframe

        layers: list[dict[str, Any]] = []
        total_features = 0
        for i, spec in enumerate(layer_specs):
            source_type = str(spec.get("source_type", "") or "file")
            name = str(
                spec.get("source_name") or spec.get("file_path") or ""
            ).strip()
            try:
                gdf = resolve_geodataframe(
                    source_type=source_type,
                    file_path=str(spec.get("file_path", "") or ""),
                    source_name=name,
                    geojson_text=str(spec.get("geojson", "") or ""),
                    catalog=catalog,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Layer %d (%s) failed to resolve", i, name)
                return ToolResult(
                    output=(
                        f"Error: layer {i + 1} ({name or 'unnamed'}) could not "
                        f"be loaded — {exc}"
                    ),
                    is_error=True,
                )
            total_features += len(gdf)
            layers.append({
                "gdf": gdf,
                "label": str(spec.get("label") or name or f"layer_{i + 1}"),
                "color": str(spec.get("color", "") or ""),
            })

        output_path = self._resolve_output_path(args, outputs_dir)
        title = args.title or args.layer_name
        try:
            written = render_layers(
                layers,
                title=title,
                output_path=output_path,
                basemap_raster=str(args.basemap_raster or ""),
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
            f"  Layers ({len(layers)}): " + ", ".join(
                str(item["label"]) for item in layers
            ),
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

    async def _render(
        self,
        args: GeoCartographyInput,
        rule: dict[str, Any],
        outputs_dir: str,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Run the blocking render pipeline in a worker thread.

        Keeps the async event loop (and therefore the TUI) responsive while
        geopandas/psycopg/matplotlib work happens. A hard timeout guarantees
        the tool can never hang a session indefinitely.
        """
        try:
            from geoharness.cartography import render_map  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependency guard
            return ToolResult(
                output=f"Error: rendering dependencies unavailable ({exc}).",
                is_error=True,
            )

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._render_blocking, args, rule, outputs_dir, context
                ),
                timeout=_RENDER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"Error: map rendering timed out after "
                    f"{_RENDER_TIMEOUT_SECONDS:.0f}s. The source may be very "
                    "large — reduce the row count, narrow the extent, or "
                    "choose a faster format."
                ),
                is_error=True,
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
        """Full pipeline: symbolize → compose → export."""
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

        combined = "\n\n".join(
            [sym_result.output, comp_result.output, exp_result.output]
        )
        return ToolResult(
            output=f"Full cartography pipeline:\n\n{combined}",
            is_error=exp_result.is_error,
            metadata={
                "bridge_rule": rule,
                **(
                    {"output_path": exp_result.metadata["output_path"]}
                    if exp_result.metadata.get("output_path")
                    else {}
                ),
            },
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """symbolize/compose only plan; export/full write files."""
        assert isinstance(arguments, GeoCartographyInput)
        return arguments.action in ("symbolize", "compose")
