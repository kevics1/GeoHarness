"""Resolve existing cartography sources and render a portable native QGIS layout."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from openharness.tools.base import ToolResult

from geoharness.cartography.project import _vector_style
from geoharness.cartography.renderer import _layer_style_options
from geoharness.cartography.sources import resolve_geodataframe
from geoharness.cartography.values import normalize_frame, normalize_style
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
                "_source_binding": {"file_path": args.basemap_raster},
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
        gdf = normalize_frame(gdf)
        options = normalize_style(gdf, options)
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
                "_source_binding": {
                    "file_path": spec.get("file_path") or spec.get("source_name", ""),
                    "derivation": spec.get("derivation"),
                },
            }
        )
    return layers


def native_layout(args: Any) -> QgisProjectLayout:
    if args.qgis_layout is not None:
        native = args.qgis_layout
        if ("legend_font_size" in args.layout.model_fields_set
                and "legend_font_size_pt" not in native.model_fields_set):
            native = QgisProjectLayout.model_validate({
                **native.model_dump(), "legend_font_size_pt": args.layout.legend_font_size,
            })
        return native
    layout = args.layout
    updates = {}
    mappings = {
        "width": "width_mm",
        "height": "height_mm",
        "title_font_size": "title_font_size_pt",
        "legend_font_size": "legend_font_size_pt",
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
        if metadata.get(key):
            result[key] = str(destination / Path(metadata[key]).relative_to(source))
    if result.get('project_file'):
        result["qgis_project"] = result["project_file"]
    if metadata.get("native_facts", {}).get("page_previews"):
        result["native_facts"] = {**metadata["native_facts"], "page_previews": [
            {**item, "path": str(destination / Path(item["path"]).relative_to(source))}
            for item in metadata["native_facts"]["page_previews"]]}
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
        from geoharness.data.task_context import resolve_registered_path

        bindings = []
        for index, layer in enumerate(layers):
            binding = layer.pop('_source_binding')
            locator = binding.get('file_path')
            resolved = (resolve_registered_path(locator, context.metadata, cwd=context.cwd,
                config=config) if locator else '')
            path = Path(resolved) if resolved else None
            if path is not None and not path.is_absolute():
                path = context.cwd / path
            if path is not None and path.is_file():
                binding.update(file_path=str(path.resolve()), source_layer='')
            else:
                # Inline/PostGIS data has no persistent local source. Preserve
                # its real input once, before the temporary bridge files vanish;
                # it is input evidence, not an editable project or layer copy of
                # an existing business file.
                inputs = Path(config.execution.outputs_dir).expanduser()
                inputs = inputs if inputs.is_absolute() else context.cwd / inputs
                inputs.mkdir(parents=True, exist_ok=True)
                frozen = Path(tempfile.mkdtemp(prefix='map-input-', dir=inputs)) / (
                    f'source-{index}' + Path(layer['file_path']).suffix)
                shutil.copy2(layer['file_path'], frozen)
                layer['file_path'] = str(frozen)
                binding.update(file_path=str(frozen), source_layer='features', derivation=None)
            bindings.append(binding)
        native = GeoQgisProjectInput(
            action="preflight" if args.action == "preflight" else "create",
            layers=layers,
            title=args.title or args.layer_name,
            crs=args.map_crs,
            map_extent=args.map_extent,
            layout=native_layout(args),
            output_format="pdf" if Path(args.output_path).suffix.lower() == ".pdf" else "png",
            dpi=args.layout.dpi,
            map_id=args.map_id,
            analysis_manifests=args.analysis_manifests,
        )
        result = await GeoQgisProjectTool().execute(native, context, _source_bindings=bindings)
    except (ValueError, OSError, RuntimeError) as exc:
        return ToolResult(output=f"Error: QGIS map preparation failed — {exc}", is_error=True)
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, True)
    if args.action == "preflight" or result.is_error or not result.metadata.get("output_path"):
        return result
    # The original native package has the registered manifest/ROI binding.
    # Publication copies preserve the pixels but do not create another map version.
    review_preview = Path(result.metadata["preview_path"])
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
    preview = review_preview
    written = Path(result.metadata["output_path"])
    desired = Path(args.output_path)
    try:
        desired.parent.mkdir(parents=True, exist_ok=True)
        if desired.exists():
            desired = desired.with_stem(f"{desired.stem}-{written.parent.name}")
        await asyncio.to_thread(shutil.copy2, written, desired)
        copied_digest = hashlib.sha256(desired.read_bytes()).hexdigest()
    except OSError as exc:
        return ToolResult(
            output=f"Map retained at {written}; output copy failed: {exc}",
            is_error=True,
            metadata=result.metadata,
        )
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    checker = context.metadata.get("geoharness_permission_checker")
    decision = checker.evaluate("geo_map_review", is_read_only=True) if checker else None
    if decision is not None and (not decision.allowed or decision.requires_confirmation):
        checked = ToolResult(output="Map retained; current permissions block visual review.",
            metadata={"image_path": str(preview), "review_status": "unverified",
                      "visual_review_completed": False})
    else:
        checked = await GeoMapReviewTool().execute(GeoMapReviewInput(
            file_path=str(preview), task=args.review_task or args.title or args.layer_name,
            map_id=args.map_id,
        ), context)
    if checked.is_error:
        return checked
    selected_image = Path(checked.metadata["image_path"])
    digest = hashlib.sha256(selected_image.read_bytes()).hexdigest()
    metadata = dict(result.metadata)
    selected_version = result.metadata.get("map_receipt", {}).get("current_version")
    if selected_image.resolve() != preview.resolve() or digest != result.metadata["image_sha256"]:
        return ToolResult(
            output="Map changed during image review; retain the package and review "
                   "its current PNG.",
            is_error=True, metadata={**metadata, "review_status": "unverified"},
        )
    output_changed = (desired.resolve() != desired.absolute() or not desired.is_file()
                      or hashlib.sha256(desired.read_bytes()).hexdigest() != copied_digest)
    if output_changed:
        from uuid import uuid4

        # Preserve external edits; copy the original native export to a new path.
        desired = desired.with_stem(desired.stem + "-reviewed-" + uuid4().hex[:8])
        try:
            await asyncio.to_thread(shutil.copy2, written, desired)
        except OSError as exc:
            return ToolResult(output=f"Map retained; output copy failed: {exc}",
                              is_error=True, metadata={**metadata, **checked.metadata})
    metadata.pop("result", None)
    primary = desired if desired.suffix.lower() == ".png" else selected_image
    from geoharness.cartography.jobs import record_map_selection

    try:
        receipt = await asyncio.to_thread(
            record_map_selection, args.map_id, context.metadata, cwd=context.cwd, config=config,
            image_path=primary, project_dir=Path(metadata["project_dir"]),
            selected_version=selected_version,
        )
    except (OSError, ValueError) as exc:
        return ToolResult(output=f"Selected artifacts retained; selection receipt failed: {exc}",
                          is_error=True, metadata={**metadata, **checked.metadata})
    return ToolResult(
        output=(
            f"QGIS预览：{desired}\n预览目录：{metadata['project_dir']}\n"
            f"Visual review: {checked.metadata['review_status']}\n"
            f"Report: {checked.metadata.get('review_path', 'unverified')}\n"
            "将primary_map或project_dir交给geo_deliver生成一次可编辑工程；PDF可附加。"
        ),
        metadata={
            **metadata,
            **checked.metadata,
            "output_path": str(desired),
            "primary_map": str(primary),
            "preview_path": str(primary),
            "image_path": str(primary),
            "image_sha256": digest,
            "backend": "qgis",
            "project_fidelity": metadata.get('project_fidelity', 'preview_only'),
            "project_dir": metadata["project_dir"],
            "project_manifest": metadata["project_manifest"],
            "map_id": args.map_id,
            "map_receipt": receipt,
            "preflight": metadata.get("preflight"),
        },
    )
