"""Resolve existing cartography sources and render a portable native QGIS layout."""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from openharness.tools.base import ToolResult

from geoharness.cartography.project import _vector_style
from geoharness.cartography.quality import save_review
from geoharness.cartography.renderer import _layer_style_options
from geoharness.cartography.sources import resolve_geodataframe
from geoharness.cartography.workflow import review_with_budget
from geoharness.files.publication import publish_directory
from geoharness.tools.geo_qgis_project import (
    GeoQgisProjectInput,
    GeoQgisProjectTool,
    QgisProjectLayout,
)


def prepare_layers(args: Any, rule: dict, context: Any, directory: Path) -> list[dict]:
    """Resolve once, preserving the existing bridge-rule classification and colors."""
    specs = (
        json.loads(args.layers)
        if args.layers
        else [
            {
                "source_type": args.source_type,
                "source_name": args.source_name,
                "file_path": args.file_path,
                "geojson": args.geojson,
                "label": args.layer_name or args.source_name or "Map",
            }
        ]
    )
    layers: list[dict] = []
    if args.basemap_raster:
        layers.append(
            {
                "file_path": args.basemap_raster,
                "kind": "raster",
                "name": "Basemap",
                "raster_style": {"renderer": "hillshade"},
            }
        )
    for index, spec in enumerate(specs):
        cache = context.metadata.get("_map_source_cache", {})
        key = f"layer_{index}" if args.layers else "single"
        gdf = cache.get(key)
        if gdf is None:
            gdf = resolve_geodataframe(
                source_type=spec.get("source_type") or "file",
                source_name=spec.get("source_name", ""),
                file_path=spec.get("file_path", ""),
                geojson_text=spec.get("geojson", ""),
                catalog=context.metadata.get("data_catalog"),
            )
        if gdf.empty and not spec.get("required", True):
            continue
        if gdf.crs is None:
            raise ValueError(f"Layer {index + 1} has no CRS; explicitly declare it before mapping")
        options = (
            _layer_style_options(spec)
            if args.layers
            else {
                "column": args.field_name,
                **rule,
            }
        )
        gdf, derived = _vector_style(gdf, options, "layers" if args.layers else "single")
        styles = {
            "singleSymbol": "single",
            "categorizedSymbol": "categorized",
            "graduatedSymbol": "graduated",
        }
        style = {"renderer": styles[derived["type"]]}
        style.update(
            size_mm=math.sqrt(args.layout.point_size) * 25.4 / 72,
            width_mm=args.layout.line_width * 25.4 / 72,
            opacity=args.layout.polygon_alpha if derived["geometry"] == "polygon" else 1,
        )
        if "color" in derived:
            style["color"] = derived["color"]
        if "column" in derived:
            style["field"] = derived["column"]
        for key in ("categories", "ranges"):
            if key in derived:
                style[key] = []
                for value in derived[key]:
                    entry = {k: v for k, v in value.items() if k != "width"}
                    if value.get("width") is not None:
                        entry["width_mm"] = value["width"] * 25.4 / 72
                    style[key].append(entry)
        path = directory / f"source-{index}.gpkg"
        gdf.to_file(path, layer="features", driver="GPKG", index=False)
        layers.append(
            {
                "file_path": str(path),
                "layer_name": "features",
                "kind": "vector",
                "name": spec.get("label") or spec.get("source_name") or f"Layer {index + 1}",
                "style": style,
                "label_field": spec.get("label_field", ""),
                "label_style": spec.get("label_style"),
                "required": spec.get("required", True),
                "expected_feature_count": len(gdf),
                "minimum_visible_features": spec.get(
                    "minimum_visible_features", 1 if spec.get("required", True) else 0
                ),
            }
        )
    return layers


def native_layout(args: Any) -> QgisProjectLayout:
    if args.qgis_layout is not None:
        return args.qgis_layout
    layout = args.layout
    updates = {}
    mappings = {
        "width": "width_mm",
        "height": "height_mm",
        "title_font_size": "title_font_size_pt",
        "legend_font_size": "body_font_size_pt",
    }
    for old, new in mappings.items():
        if old in layout.model_fields_set:
            updates[new] = getattr(layout, old) * (25.4 if old in {"width", "height"} else 1)
    return QgisProjectLayout(**updates)


def relocate_package(metadata: dict, destination: Path, config: Any) -> dict:
    """Copy the portable package to an explicitly selected new workspace folder."""
    if destination.exists():
        raise ValueError("project_output_dir already exists; choose a new directory")
    source = Path(metadata["project_dir"])
    manifest = json.loads((source / "project.json").read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for relative in set(manifest["files"]) | {"project.json"}:
            original = (source / relative).resolve()
            target = (stage / relative).resolve()
            if not original.is_relative_to(source) or not target.is_relative_to(stage):
                raise ValueError("QGIS package references must stay inside their folder")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        publication = publish_directory(stage, destination, settings=config.publication)
    except BaseException as exc:
        if not getattr(exc, "diagnostics", None):
            shutil.rmtree(stage, ignore_errors=True)
        raise
    result = {**metadata, "project_dir": str(destination), "publication": publication}
    for key in ("project_file", "project_manifest", "preview_path", "primary_map", "output_path"):
        result[key] = str(destination / Path(metadata[key]).relative_to(source))
    result["qgis_project"] = result["project_file"]
    return result


async def render_qgis_map(tool: Any, args: Any, rule: dict, context: Any) -> ToolResult:
    config = context.metadata["geoharness_config"]
    if not config.qgis.enabled:
        return ToolResult(
            output="Error: QGIS is disabled; enable it or choose renderer=matplotlib.",
            is_error=True,
        )
    project_destination = None
    if args.project_output_dir:
        project_destination = Path(args.project_output_dir).expanduser()
        project_destination = (
            project_destination
            if project_destination.is_absolute()
            else context.cwd / project_destination
        ).resolve()
        if not project_destination.is_relative_to(context.cwd.resolve()):
            return ToolResult(
                output="Error: project_output_dir must stay inside the workspace", is_error=True
            )
        if project_destination.exists() or project_destination.is_symlink():
            return ToolResult(
                output="Error: project_output_dir already exists; choose a new directory",
                is_error=True,
            )
    root = Path(config.qgis.outputs_dir).expanduser()
    root = (root if root.is_absolute() else context.cwd / root).resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="sources-", dir=root))
    except OSError as exc:
        return ToolResult(
            output=f"Error: QGIS source directory could not be created: {exc}", is_error=True
        )
    try:
        preparation = asyncio.create_task(
            asyncio.to_thread(prepare_layers, args, rule, context, directory)
        )
        try:
            layers = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            # A cancelled await does not stop a native file writer in the thread.
            # Wait until it has closed its handles before removing our temporary sources.
            try:
                await preparation
            except Exception:
                # Consume a writer failure without replacing the user's cancellation.
                pass
            raise
        native = GeoQgisProjectInput(
            action="create",
            layers=layers,
            title=args.title or args.layer_name,
            crs=args.map_crs,
            map_extent=args.map_extent,
            layout=native_layout(args),
            output_format="pdf" if Path(args.output_path).suffix.lower() == ".pdf" else "png",
            dpi=args.layout.dpi,
            map_id=args.map_id,
        )
        result = await GeoQgisProjectTool().execute(native, context)
    except (ValueError, OSError, RuntimeError) as exc:
        return ToolResult(output=f"Error: QGIS map preparation failed — {exc}", is_error=True)
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, True)
    if result.is_error or not result.metadata.get("output_path"):
        return result
    if project_destination is not None:
        try:
            publication = asyncio.create_task(
                asyncio.to_thread(
                    relocate_package,
                    result.metadata,
                    project_destination,
                    config,
                )
            )
            try:
                relocated = await asyncio.shield(publication)
            except asyncio.CancelledError:
                try:
                    await publication
                except Exception:
                    pass
                raise
            result = ToolResult(output=result.output, metadata=relocated)
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=f"QGIS map retained; project copy failed: {exc}",
                is_error=True,
                metadata={
                    **result.metadata,
                    **({"publication": exc.diagnostics} if hasattr(exc, "diagnostics") else {}),
                },
            )
    preview = Path(result.metadata["preview_path"])
    written = Path(result.metadata["output_path"])
    desired = Path(args.output_path)
    try:
        desired.parent.mkdir(parents=True, exist_ok=True)
        if desired.exists():
            desired = desired.with_stem(f"{desired.stem}-{written.parent.name}")
        await asyncio.to_thread(shutil.copy2, written, desired)
    except OSError as exc:
        return ToolResult(
            output=f"Map retained at {written}; output copy failed: {exc}",
            is_error=True,
            metadata=result.metadata,
        )
    checked, budget = await review_with_budget(
        preview,
        config=config,
        task=args.review_task or args.title or args.layer_name,
        evidence={
            "backend": "qgis",
            "render_metadata": {
                key: result.metadata[key]
                for key in ("layers", "crs", "extent", "layout", "image_sha256", "analysis")
                if key in result.metadata
            },
        },
        metadata=context.metadata,
        map_id=args.map_id,
        client=context.metadata.get("map_review_client"),
    )
    report = await asyncio.to_thread(
        save_review,
        preview,
        {
            **checked.model_dump(),
            "image_sha256": result.metadata["image_sha256"],
            "map_id": args.map_id,
            "repair_count": 0,
            "reviewed_artifact": "exported_png",
        },
    )
    response = ToolResult(
        output=(
            f"QGIS map: {desired}\nEditable project: {result.metadata['project_file']}\n"
            f"Visual review: {checked.status} — {checked.summary}\nReport: {report}\n"
            "Use the recorded primary_map PNG with geo_deliver; PDF is an additional export."
        ),
        metadata={
            **result.metadata,
            "output_path": str(desired),
            "primary_map": str(desired) if desired.suffix.lower() == ".png" else str(preview),
            "map_id": args.map_id,
            "review_status": checked.status,
            "review_path": str(report),
            "quality_status": "draft",
            "repair_count": 0,
            "preflight": context.metadata.get("map_preflight"),
            **budget,
        },
    )
    from geoharness.cartography.tracking import track_render_result

    return await asyncio.to_thread(
        track_render_result,
        response,
        context,
        args.map_id,
        preflight=context.metadata.get("map_preflight"),
        analysis=context.metadata.get("map_analysis_facts"),
    )
