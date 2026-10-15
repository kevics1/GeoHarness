"""Reproducible road accessibility with explicit topology and method assumptions.

The graph uses existing XY vertices: it never invents junctions at line crossings
or interprets direction codes. Source records and their geometry stay in the same
GeoDataFrame throughout; all cross-layer references use validated stable IDs.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any

from geoharness.analysis.vector import (
    _check_deadline,
    _output_stem,
    _resolve_source,
    _validate_frame,
)
from geoharness.files.publication import DirectoryPublicationError, publish_directory


class AccessibilityContractError(ValueError):
    """An actionable input/method failure, rather than an unreachable resident."""

    def __init__(self, code: str, message: str, **details: Any):
        self.diagnostics = {"code": code, **details}
        super().__init__(message)


def _ids(frame: Any, field: str, label: str) -> list[str]:
    if field not in frame.columns or field == frame.geometry.name:
        raise AccessibilityContractError(
            "missing_identity",
            f"{label}: id_field '{field}' must be an existing attribute",
            source=label,
            id_field=field,
        )
    if frame[field].isna().any():
        raise AccessibilityContractError(
            "invalid_identity",
            f"{label}: IDs must be non-null and unique",
            source=label,
        )
    values = []
    for value in frame[field]:
        value = value.item() if hasattr(value, "item") else value
        if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
            raise AccessibilityContractError(
                "invalid_identity",
                f"{label}: IDs must be nonempty strings or integers",
                source=label,
                id_field=field,
            )
        values.append(str(value))
    if len(values) != len(set(values)):
        raise AccessibilityContractError(
            "duplicate_identity",
            f"{label}: IDs must be unique after string serialization",
            source=label,
            id_field=field,
        )
    return values


def _projection(roads: Any, target_crs: str) -> Any:
    from geoharness._proj_fix import ensure_proj_data

    ensure_proj_data()
    from pyproj import CRS

    value = CRS.from_user_input(target_crs or roads.crs)
    if (
        not value.is_projected
        or len(value.axis_info) < 2
        or any(not math.isclose(axis.unit_conversion_factor, 1) for axis in value.axis_info[:2])
    ):
        raise AccessibilityContractError(
            "metric_crs_required",
            "Use a projected metre target_crs; geographic/degrees and "
            "foot-based coordinates cannot be used as metre distances",
        )
    return value


def _add_edge(graph: Any, start: tuple, end: tuple, length: float, road_id: str) -> None:
    existing = graph.get_edge_data(start, end)
    # Coincident parallel segments need only their minimum distance. No capacity,
    # road-specific speed or turn penalty is implied by this distance-only graph.
    if existing is None or length < existing["weight"]:
        graph.add_edge(start, end, weight=length, road_id=road_id)


def _connect_segment(
    graph: Any, start: tuple, end: tuple, length: float, direction: str, road_id: str
) -> None:
    if direction in {"both", "forward"}:
        _add_edge(graph, start, end, length, road_id)
    if direction in {"both", "reverse"}:
        _add_edge(graph, end, start, length, road_id)


def compute_accessibility(
    roads: Any,
    residents: Any,
    shelters: Any,
    *,
    road_id_field: str,
    resident_id_field: str,
    shelter_id_field: str,
    target_crs: str = "",
    direction_mode: str,
    speed_kmh: float,
    max_snap_distance_m: float,
    connector_speed_kmh: float = 5,
    direction_field: str = "",
    direction_values: dict[str, str] | None = None,
    hazards: Any = None,
    hazard_buffer_m: float = 0,
    routes_per_resident: int = 1,
    deadline: float = math.inf,
    cancelled: Event | None = None,
) -> dict[str, Any]:
    """Compute nearest shelters on a hazard-filtered, directed metric graph.

    Every resident has a result and reason, including failed snaps and disconnected
    networks. Routes minimize total distance, including both endpoint connectors.
    Estimated time uses explicit constant road/connector speeds, without capacity
    allocation, traffic, elevation, turn restrictions or emergency certification.
    """
    import geopandas as gpd
    import networkx as nx
    import numpy as np
    from shapely import STRtree, union_all
    from shapely.geometry import LineString, Point

    cancelled = cancelled or Event()

    def check() -> None:
        _check_deadline(deadline, cancelled)

    check()
    for name, value, allow_zero in (
        ("speed_kmh", speed_kmh, False),
        ("connector_speed_kmh", connector_speed_kmh, False),
        ("max_snap_distance_m", max_snap_distance_m, True),
        ("hazard_buffer_m", hazard_buffer_m, True),
    ):
        if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
            raise AccessibilityContractError("invalid_method_parameter", f"Invalid {name}")
    if (
        isinstance(routes_per_resident, bool)
        or not isinstance(routes_per_resident, int)
        or routes_per_resident < 1
        or routes_per_resident > 10
    ):
        raise AccessibilityContractError("invalid_method_parameter", "routes_per_resident: 1..10")
    if direction_mode not in {"bidirectional", "field"}:
        raise AccessibilityContractError("direction_contract", "Set direction_mode explicitly")
    if direction_mode == "bidirectional" and (direction_field or direction_values):
        raise AccessibilityContractError(
            "direction_contract",
            "bidirectional mode does not accept direction fields/mappings",
        )
    if direction_mode == "field" and (not direction_field or not direction_values):
        raise AccessibilityContractError(
            "direction_contract",
            "field mode requires direction_field and direction_values",
        )
    if direction_values and not set(direction_values.values()) <= {
        "forward",
        "reverse",
        "both",
        "closed",
    }:
        raise AccessibilityContractError("direction_contract", "Unknown mapped direction")
    if hazards is None and hazard_buffer_m:
        raise AccessibilityContractError("hazard_contract", "hazard_buffer_m requires hazards")

    source_frames = {"roads": roads, "residents": residents, "shelters": shelters}
    if hazards is not None:
        source_frames["hazards"] = hazards
    for label, frame in source_frames.items():
        _validate_frame(frame, label)
        if frame.geometry.has_z.any():
            raise AccessibilityContractError(
                "unsupported_geometry",
                f"{label}: supply explicit 2D geometry; Z topology "
                "must not silently become a junction",
            )
    if roads.empty:
        raise AccessibilityContractError("empty_roads", "Roads must contain at least one line")
    if not roads.geom_type.isin(["LineString", "MultiLineString"]).all():
        raise AccessibilityContractError("wrong_geometry", "roads requires line geometry")
    for label, frame in (("residents", residents), ("shelters", shelters)):
        if not frame.geom_type.eq("Point").all():
            raise AccessibilityContractError("wrong_geometry", f"{label} requires point geometry")
        conflicts = [field for field in frame.columns if str(field).startswith("gh_")]
        if conflicts:
            raise AccessibilityContractError(
                "reserved_fields",
                f"{label}: result fields conflict: {conflicts}",
                fields=conflicts,
            )
    if hazards is not None and not hazards.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise AccessibilityContractError("wrong_geometry", "hazards requires polygon geometry")
    road_ids = _ids(roads, road_id_field, "roads")
    resident_ids = _ids(residents, resident_id_field, "residents")
    shelter_ids = _ids(shelters, shelter_id_field, "shelters")
    projection = _projection(roads, target_crs)
    metric = {label: frame.to_crs(projection).copy() for label, frame in source_frames.items()}
    for label, frame in metric.items():
        _validate_frame(frame, label)
    hazard = union_all(metric["hazards"].geometry.array) if hazards is not None else None
    if hazard is not None and hazard_buffer_m:
        hazard = hazard.buffer(hazard_buffer_m)
    if hazard is not None and hazard.is_empty:
        hazard = None

    directions = ["both"] * len(roads)
    if direction_mode == "field":
        if direction_field not in roads.columns or direction_field == roads.geometry.name:
            raise AccessibilityContractError(
                "direction_contract",
                "direction_field must be an existing attribute",
            )
        if roads[direction_field].isna().any():
            raise AccessibilityContractError("unknown_direction", "Direction values include null")
        values = roads[direction_field].map(str)
        unknown = sorted(set(values) - set(direction_values or {}))
        if unknown:
            raise AccessibilityContractError(
                "unknown_direction",
                f"Explicitly map every road direction value: {unknown}",
                unknown_values=unknown,
            )
        directions = [direction_values[value] for value in values]

    original = nx.DiGraph()
    segments = []
    blocked_segments = 0
    closed_segments = 0
    for geometry, road_id, direction in zip(metric["roads"].geometry, road_ids, directions):
        check()
        parts = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
        for part_index, part in enumerate(parts):
            for vertex_index, (start, end) in enumerate(zip(part.coords, list(part.coords)[1:])):
                start, end = tuple(start), tuple(end)
                if start == end:
                    continue
                line = LineString([start, end])
                if direction == "closed":
                    closed_segments += 1
                    continue
                _connect_segment(original, start, end, line.length, direction, road_id)
                if hazard is not None and hazard.intersects(line):
                    blocked_segments += 1
                    continue
                segments.append(
                    {
                        "geometry": line,
                        "road_id": road_id,
                        "direction": direction,
                        "token": (road_id, part_index, vertex_index),
                    }
                )
    segments.sort(key=lambda item: item["token"])
    tree = STRtree([segment["geometry"] for segment in segments]) if segments else None
    attachments: dict[tuple[str, str], dict] = {}
    splits: dict[int, list[float]] = {}

    def snap(point: Any) -> dict:
        if hazard is not None and hazard.intersects(point):
            return {"status": "inside_hazard", "reason": "endpoint intersects buffered hazards"}
        if tree is None:
            return {"status": "no_safe_network", "reason": "no traversable safe road segment"}
        query = point.buffer(max_snap_distance_m).envelope if max_snap_distance_m else point
        candidates = []
        for index in tree.query(query):
            index = int(index)
            line = segments[index]["geometry"]
            distance = point.distance(line)
            if distance <= max_snap_distance_m + 1e-8:
                candidates.append((distance, index))
        if not candidates:
            nearest = int(tree.nearest(point))
            return {
                "status": "snap_too_far",
                "reason": "nearest safe road exceeds snap limit",
                "snap_distance_m": float(point.distance(segments[nearest]["geometry"])),
            }
        for distance, index in sorted(candidates):
            line = segments[index]["geometry"]
            position = line.project(point)
            if math.isclose(position, 0, abs_tol=1e-8):
                position = 0.0
            elif math.isclose(position, line.length, rel_tol=0, abs_tol=1e-8):
                position = line.length
            on_road = line.interpolate(position)
            connector = LineString([point.coords[0], on_road.coords[0]]) if distance else point
            if hazard is not None and hazard.intersects(connector):
                continue
            splits.setdefault(index, []).append(position)
            return {
                "status": "connected",
                "reason": "safe endpoint connector",
                "snap_distance_m": float(point.distance(on_road)),
                "node": tuple(on_road.coords[0]),
                "segment": index,
            }
        return {
            "status": "connector_blocked",
            "reason": "all candidate connectors intersect hazards",
        }

    for label, ids in (("residents", resident_ids), ("shelters", shelter_ids)):
        for key, point in zip(ids, metric[label].geometry):
            check()
            attachments[label, key] = snap(point)
    graph = nx.DiGraph()
    for index, segment in enumerate(segments):
        check()
        line = segment["geometry"]
        positions = sorted(set([0.0, line.length, *splits.get(index, [])]))
        for lower, upper in zip(positions, positions[1:]):
            if upper == lower:
                continue
            start = tuple(line.interpolate(lower).coords[0])
            end = tuple(line.interpolate(upper).coords[0])
            _connect_segment(
                graph, start, end, math.dist(start, end), segment["direction"], segment["road_id"]
            )
    safe_shelters = [
        (key, row.geometry, attachments["shelters", key])
        for key, (_, row) in zip(shelter_ids, metric["shelters"].iterrows())
        if attachments["shelters", key]["status"] == "connected"
    ]
    resident_records, route_records, route_geometries = [], [], []
    for key, (_, row) in zip(resident_ids, metric["residents"].iterrows()):
        check()
        attachment = attachments["residents", key]
        record = {
            "gh_resident_id": key,
            "gh_status": attachment["status"],
            "gh_reason": attachment["reason"],
            "gh_reachable_shelters": 0,
            "gh_shelter_id": None,
            "gh_distance_m": None,
            "gh_time_min": None,
            "gh_snap_distance_m": attachment.get("snap_distance_m"),
        }
        resident_records.append(record)
        if attachment["status"] != "connected":
            continue
        if not safe_shelters:
            record.update(gh_status="no_available_shelters", gh_reason="no safe connected shelter")
            continue
        distances, paths = nx.single_source_dijkstra(graph, attachment["node"], weight="weight")
        check()
        candidates = []
        for shelter_id, shelter_point, shelter_attachment in safe_shelters:
            node = shelter_attachment["node"]
            if node not in distances:
                continue
            connector_distance = (
                attachment["snap_distance_m"] + shelter_attachment["snap_distance_m"]
            )
            total = distances[node] + connector_distance
            candidates.append(
                (
                    total,
                    shelter_id,
                    shelter_point,
                    shelter_attachment,
                    connector_distance,
                    distances[node],
                    paths[node],
                )
            )
        if not candidates:
            record.update(
                gh_status="network_unreachable",
                gh_reason="no directed safe path to any connected shelter",
            )
            continue
        candidates.sort(key=lambda item: (item[0], item[1]))
        record.update(
            gh_status="reachable",
            gh_reason="shortest safe total distance",
            gh_reachable_shelters=len(candidates),
        )
        for rank, candidate in enumerate(candidates[:routes_per_resident], start=1):
            total, shelter_id, shelter_point, _, connector_distance, road_distance, nodes = (
                candidate
            )
            coordinates = [tuple(row.geometry.coords[0]), *nodes, tuple(shelter_point.coords[0])]
            coordinates = [
                point
                for index, point in enumerate(coordinates)
                if index == 0 or point != coordinates[index - 1]
            ]
            geometry = LineString(coordinates) if len(coordinates) > 1 else Point(coordinates[0])
            if not math.isclose(geometry.length, total, rel_tol=1e-9, abs_tol=1e-6):
                raise RuntimeError("Route geometry length does not match reported distance")
            if hazard is not None and hazard.intersects(geometry):
                raise RuntimeError("Route unexpectedly intersects a blocked hazard")
            minutes = road_distance / (speed_kmh * 1000 / 60) + connector_distance / (
                connector_speed_kmh * 1000 / 60
            )
            route_records.append(
                {
                    "gh_resident_id": key,
                    "gh_shelter_id": shelter_id,
                    "gh_rank": rank,
                    "gh_distance_m": total,
                    "gh_road_distance_m": road_distance,
                    "gh_connector_distance_m": connector_distance,
                    "gh_time_min": minutes,
                }
            )
            route_geometries.append(geometry)
            if rank == 1:
                record.update(gh_shelter_id=shelter_id, gh_distance_m=total, gh_time_min=minutes)

    # Assignment stays in original row order; no positional join after sorting.
    result = metric["residents"].copy().reset_index(drop=True)
    import pandas as pd

    result_fields = [
        "gh_resident_id",
        "gh_status",
        "gh_reason",
        "gh_reachable_shelters",
        "gh_shelter_id",
        "gh_distance_m",
        "gh_time_min",
        "gh_snap_distance_m",
    ]
    attributes = pd.DataFrame(resident_records, columns=result_fields)
    for field in attributes:
        result[field] = attributes[field].values
    route_fields = [
        "gh_resident_id",
        "gh_shelter_id",
        "gh_rank",
        "gh_distance_m",
        "gh_road_distance_m",
        "gh_connector_distance_m",
        "gh_time_min",
    ]
    routes = gpd.GeoDataFrame(
        pd.DataFrame(route_records, columns=route_fields), geometry=route_geometries, crs=projection
    )
    shelter_result = metric["shelters"].copy().reset_index(drop=True)
    shelter_result["gh_shelter_id"] = shelter_ids
    for field in ("status", "reason", "snap_distance_m"):
        shelter_result[f"gh_{field}"] = [
            attachments["shelters", key].get(field) for key in shelter_ids
        ]
    if len(result) and not np.array_equal(
        result.geometry.to_wkb(), metric["residents"].geometry.to_wkb()
    ):
        raise RuntimeError("Resident identity/geometry binding changed during accessibility")
    statistics = {
        "feature_count": len(result),
        "resident_count": len(residents),
        "shelter_count": len(shelters),
        "safe_connected_shelters": len(safe_shelters),
        "route_count": len(routes),
        "resident_status_counts": {
            str(key): int(value) for key, value in result.gh_status.value_counts().items()
        },
        "original_nodes": original.number_of_nodes(),
        "original_edges": original.number_of_edges(),
        "original_weak_components": nx.number_weakly_connected_components(original),
        "safe_nodes": graph.number_of_nodes(),
        "safe_edges": graph.number_of_edges(),
        "safe_weak_components": nx.number_weakly_connected_components(graph),
        "safe_strong_components": nx.number_strongly_connected_components(graph),
        "hazard_blocked_segments": blocked_segments,
        "closed_direction_segments": closed_segments,
    }
    return {
        "artifacts": {"result": result, "routes": routes, "shelters": shelter_result},
        "metric_crs": projection.to_string(),
        "statistics": statistics,
        "assumptions": {
            "topology": "Existing shared XY vertices only; crossings without a shared vertex "
            "are not junctions. Supply correctly noded/grade-separated road data.",
            "direction_mode": direction_mode,
            "direction_field": direction_field,
            "direction_values": direction_values or {},
            "objective": "minimum total distance including both endpoint connectors",
            "road_speed_kmh": speed_kmh,
            "connector_speed_kmh": connector_speed_kmh,
            "time_model": "constant speeds; no traffic/elevation/turn restrictions",
            "snap_limit_m": max_snap_distance_m,
            "hazards": "Remove intersecting road vertex segments; block hazard endpoints "
            "and connectors. Hazard boundary contact is blocked.",
            "hazard_buffer_m": hazard_buffer_m,
            "capacity_allocation": False,
            "capacity_note": "Shelter capacity is retained as a source attribute only; "
            "routes do not allocate demand or guarantee available capacity.",
            "analysis_correctness": "unverified",
            "data_integrity": "validated IDs, geometry binding, metric CRS, endpoint "
            "connectors, path directions and route-length consistency",
        },
    }


def analyze_accessibility(
    args: Any,
    *,
    cwd: Path,
    outputs_dir: str,
    catalog: Any = None,
    metric_crs: str = "",
    timeout_seconds: float = 120,
    cancelled: Event | None = None,
    publication_settings: Any = None,
) -> dict[str, Any]:
    """Resolve inputs and atomically publish a complete, CRS-safe native result."""
    cancelled = cancelled or Event()
    deadline = time.monotonic() + timeout_seconds
    loaded, provenance = {}, {}
    for label in ("roads", "residents", "shelters", "hazards"):
        _check_deadline(deadline, cancelled)
        source = getattr(args, label)
        if source is not None:
            loaded[label], provenance[label] = _resolve_source(source, catalog, cwd, label)
    kwargs = {
        key: getattr(args, key)
        for key in (
            "road_id_field",
            "resident_id_field",
            "shelter_id_field",
            "direction_mode",
            "direction_field",
            "direction_values",
            "speed_kmh",
            "connector_speed_kmh",
            "max_snap_distance_m",
            "hazard_buffer_m",
            "routes_per_resident",
        )
    }
    computed = compute_accessibility(
        loaded["roads"],
        loaded["residents"],
        loaded["shelters"],
        hazards=loaded.get("hazards"),
        target_crs=args.target_crs or metric_crs,
        deadline=deadline,
        cancelled=cancelled,
        **kwargs,
    )
    root = Path(outputs_dir).expanduser()
    root = (root if root.is_absolute() else Path(cwd) / root).resolve()
    stem = _output_stem(args.output_name or "accessibility")
    directory = root / f"{stem}-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=root, prefix=".staging-"))
    ready = False
    try:
        paths, previews, layers = {}, {}, []
        for label, frame in computed["artifacts"].items():
            _check_deadline(deadline, cancelled)
            exported = frame.to_crs("EPSG:4326")
            payload = exported.to_json(default=str, drop_id=True)
            geojson = stage / f"{label}.geojson"
            geojson.write_text(payload, encoding="utf-8")
            table = exported.drop(columns=exported.geometry.name)
            csv = stage / f"{label}.csv"
            table.to_csv(csv, index=False, encoding="utf-8")
            paths[label] = {
                "geojson": str(directory / geojson.name),
                "csv": str(directory / csv.name),
                "feature_count": len(frame),
                "crs": "EPSG:4326",
                "state": "succeeded",
                "validation_state": "passed",
                "geometry_type": sorted(set(frame.geom_type)),
                "geojson_sha256": hashlib.sha256(geojson.read_bytes()).hexdigest(),
                "csv_sha256": hashlib.sha256(csv.read_bytes()).hexdigest(),
                "schema": {str(field): str(dtype) for field, dtype in exported.dtypes.items()},
            }
            previews[label] = json.loads(
                table.head(12).to_json(orient="records", date_format="iso")
            )
            if not frame.empty:
                layers.append(
                    {
                        "kind": "vector",
                        "name": label,
                        "file_path": str(directory / geojson.name),
                        "expected_feature_count": len(frame),
                    }
                )
        manifest = {
            "status": "succeeded",
            "producer": "geo_accessibility",
            "action": "accessibility",
            "validation_state": "passed",
            "metric_crs": computed["metric_crs"],
            "export_crs": "EPSG:4326",
            "sources": provenance,
            "parameters": kwargs,
            "assumptions": computed["assumptions"],
            "statistics": computed["statistics"],
            "paths": paths,
            "layers": layers,
            "table_previews": previews,
            "preview_row_limit": 12,
            "manifest_path": str(directory / "manifest.json"),
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2),
            encoding="utf-8",
        )
        ready = True
        manifest["publication"] = publish_directory(
            stage,
            directory,
            settings=publication_settings,
            before_attempt=lambda: _check_deadline(deadline, cancelled),
        )
        return manifest
    except BaseException as error:
        diagnostics = getattr(error, "diagnostics", None)
        if ready and (
            isinstance(error, DirectoryPublicationError)
            or isinstance(diagnostics, dict)
            and diagnostics.get("status") == "interrupted"
        ):
            (stage / "publication.json").write_text(json.dumps(diagnostics), encoding="utf-8")
        else:
            shutil.rmtree(stage, ignore_errors=True)
        raise
