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
            numeric = frame[field].dropna().tolist()
            integral_floats = bool(numeric) and all(
                isinstance(item, float) and math.isfinite(item) and item.is_integer()
                for item in numeric
            )
            raise AccessibilityContractError(
                "invalid_identity",
                f"{label}: IDs must be nonempty strings or integers",
                source=label,
                id_field=field,
                dtype=str(frame[field].dtype),
                recovery={
                    "integral_float_values": integral_floats,
                    "instruction": "Create a new ID attribute in a derived output; preserve "
                    "the original source and ID mapping. Before explicit conversion, verify "
                    "non-null values, integer precision when relevant, uniqueness and collision "
                    "freedom after string serialization. Then use the derived ID field.",
                },
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


def _status_definitions(hazard_buffer_m: float) -> dict[str, dict[str, str]]:
    """Labels describe the model, without claiming real evacuation or fire exposure."""
    hazard_label = "位于所选危险范围内"
    if hazard_buffer_m:
        hazard_label = f"位于所选危险范围及{hazard_buffer_m:g} m缓冲区内"
    descriptions = {
        "reachable": ("当前模型条件下可达", "A directed model path exists to an eligible shelter."),
        "inside_hazard": (
            hazard_label,
            "Endpoint intersects selected hazard union plus buffer; "
            "not proof of being inside a current fire.",
        ),
        "snap_too_far": (
            "超出设定路网接驳距离",
            "No candidate under the input data/connector limit; not proof of physical isolation.",
        ),
        "network_unreachable": ("当前模型路网不可达", "No directed path to an eligible shelter."),
        "road_access_blocked": (
            "模型接入道路受阻",
            "All original candidate road segments blocked.",
        ),
        "connector_blocked": ("模型接驳线受阻", "All eligible straight access connectors blocked."),
        "no_safe_network": (
            "当前模型无可用路网",
            "No traversable road segment remains in this model.",
        ),
        "no_available_shelters": (
            "当前模型无可用目的地",
            "No eligible shelter has safe model access.",
        ),
        "connected": (
            "具有模型路网接入",
            "Has eligible access candidates; not shelter activation "
            "or a capacity/demand allocation.",
        ),
    }
    return {
        code: {"label_zh": label, "meaning": meaning}
        for code, (label, meaning) in descriptions.items()
    }


def _describe_artifact(label: str, frame: Any, *, compare: bool) -> dict:
    """Declare roles using actual exported fields; never promise guessed merge suffixes."""
    scenario = "selected_scenario"
    name = label
    for prefix in ("baseline_", "hazard_"):
        if label.startswith(prefix):
            scenario, name = prefix[:-1], label[len(prefix) :]
            break
    fields = set(frame.columns)
    roles = {}
    if label == "result" and compare:
        role, scenario = "resident_scenario_comparison", "comparison"
        meaning = "One row per resident, retaining source attributes; baseline/hazard comparison."
        roles = {
            "join_key": "gh_resident_id",
            "baseline_status": "gh_baseline_status",
            "hazard_status": "gh_hazard_status",
            "baseline_status_label_zh": "gh_baseline_status_label_zh",
            "hazard_status_label_zh": "gh_hazard_status_label_zh",
            "baseline_time_minutes": "gh_baseline_time_min",
            "hazard_time_minutes": "gh_hazard_time_min",
            "baseline_distance_metres": "gh_baseline_distance_m",
            "hazard_distance_metres": "gh_hazard_distance_m",
            "baseline_objective": "gh_baseline_objective",
            "hazard_objective": "gh_hazard_objective",
            "objective_delta": "gh_objective_delta",
            "lost_model_access": "gh_lost_access",
        }
    elif name == "result":
        role, meaning = "resident_status", "One row per resident, retaining source attributes."
        roles = {
            "join_key": "gh_resident_id",
            "status": "gh_status",
            "status_label_zh": "gh_status_label_zh",
            "time_minutes": "gh_time_min",
            "distance_metres": "gh_distance_m",
        }
    elif name == "routes":
        role, meaning = "model_routes", "Model paths including road and endpoint connectors."
        roles = {
            "resident_key": "gh_resident_id",
            "shelter_key": "gh_shelter_id",
            "time_minutes": "gh_time_min",
            "distance_metres": "gh_distance_m",
        }
    elif name == "shelters":
        role = "shelter_access_status"
        meaning = "Shelter access candidates; not shelter activation or allocated capacity."
        roles = {
            "join_key": "gh_shelter_id",
            "status": "gh_status",
            "status_label_zh": "gh_status_label_zh",
        }
    else:
        role, meaning = "access_candidate_audit", "Original access candidates for model auditing."
        scenario = "shared_candidates"
    return {
        "role": role,
        "scenario": scenario,
        "meaning": meaning,
        "field_roles": {key: value for key, value in roles.items() if value in fields},
        "source_attribute_fields": [
            str(field)
            for field in frame.columns
            if field != frame.geometry.name and not str(field).startswith("gh_")
        ],
        "native_result_fields": [
            str(field) for field in frame.columns if str(field).startswith("gh_")
        ],
    }


def _label_artifacts(artifacts: dict, definitions: dict) -> None:
    labels = {code: entry["label_zh"] for code, entry in definitions.items()}
    for frame in artifacts.values():
        for field in ("gh_status", "gh_baseline_status", "gh_hazard_status"):
            if field in frame.columns:
                frame[f"{field}_label_zh"] = frame[field].map(labels)


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
    objective: str = "distance",
    compare: bool = False,
    deadline: float = math.inf,
    cancelled: Event | None = None,
) -> dict[str, Any]:
    """Compute nearest shelters on a hazard-filtered, directed metric graph.

    Every resident retains a result/reason. All eligible original-road access
    candidates are frozen before hazard filtering. The selected distance/time
    objective includes both endpoint connectors. compare=True enforces common
    candidate/weight inputs and deletion-only objective monotonicity.
    """
    from shapely import union_all

    cancelled = cancelled or Event()

    def check() -> None:
        _check_deadline(deadline, cancelled)

    check()
    if objective not in {"distance", "time"}:
        raise AccessibilityContractError("invalid_objective", "objective must be distance or time")
    if compare and hazards is None:
        raise AccessibilityContractError("hazard_contract", "compare requires hazards")
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
    raw_count = (
        int(metric["residents"].geometry.intersects(hazard).sum()) if hazard is not None else 0
    )
    if hazard is not None and hazard_buffer_m:
        hazard = hazard.buffer(hazard_buffer_m)
    if hazard is not None and hazard.is_empty:
        hazard = None
    hazard_scope = {
        "selected_feature_count": len(hazards) if hazards is not None else 0,
        "geometry_operation": "union_then_buffer",
        "hazard_buffer_m": hazard_buffer_m,
        "resident_intersects_selected_count": raw_count,
        "resident_intersects_buffered_count": int(
            metric["residents"].geometry.intersects(hazard).sum()
        )
        if hazard is not None
        else 0,
        "count_unit": "resident_features",
        "meaning": "Intersection counts refer to the supplied/filtered geometries and configured "
        "buffer, not observed fire exposure. No automatic forecast-time selection.",
    }
    definitions = _status_definitions(hazard_buffer_m)

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

    from geoharness.analysis.accessibility_comparison import (
        candidate_artifact,
        compare_access_results,
        prepare_access_network,
        solve_access_scenario,
    )

    prepared = prepare_access_network(
        metric,
        road_ids,
        resident_ids,
        shelter_ids,
        directions,
        max_snap_distance_m=max_snap_distance_m,
        objective=objective,
        speed_kmh=speed_kmh,
        connector_speed_kmh=connector_speed_kmh,
        check=check,
    )
    prepared["routes_per_resident"] = routes_per_resident
    scenario = solve_access_scenario(prepared, hazard, check=check)
    assumptions = {
        "method_version": "shared-candidates-v2",
        "topology": "Existing shared XY vertices only; crossings without a shared vertex "
        "are not junctions. Supply correctly noded/grade-separated road data.",
        "direction_mode": direction_mode,
        "direction_field": direction_field,
        "direction_values": direction_values or {},
        "objective": objective,
        "objective_unit": "m" if objective == "distance" else "min",
        "access_policy": "All segment-projection candidates within the original-road connector "
        "limit, frozen before hazard filtering; connectors are OD entry/exit only.",
        "candidate_set_sha256": prepared["candidate_set_sha256"],
        "road_speed_kmh": speed_kmh,
        "connector_speed_kmh": connector_speed_kmh,
        "time_model": "constant speeds; no traffic/elevation/turn restrictions",
        "snap_limit_m": max_snap_distance_m,
        "hazards": "Remove intersecting original vertex segments and unsafe terminal/access "
        "connectors. Hazard boundary contact is blocked; no new candidates are added.",
        "hazard_buffer_m": hazard_buffer_m,
        "capacity_allocation": False,
        "capacity_note": "Shelter capacity remains a source attribute; no demand allocation.",
        "unverified_access": "Straight connectors lack pedestrian/river/wall/terrain validation. "
        "snap_too_far only means no candidate under this data/parameter limit.",
        "analysis_correctness": "unverified",
        "data_integrity": "IDs, geometry binding, metric CRS, directions, endpoint connectors, "
        "route lengths and objective weights checked",
    }
    if compare:
        baseline = solve_access_scenario(prepared, None, check=check)
        comparison = compare_access_results(prepared, baseline, scenario)
        artifacts = {"result": comparison["result"]}
        for label, output in (("baseline", baseline), ("hazard", scenario)):
            for name, frame in output["artifacts"].items():
                artifacts[f"{label}_{name}"] = frame
        artifacts["access_candidates"] = candidate_artifact(prepared, scenario)
        _label_artifacts(artifacts, definitions)
        return {
            "artifacts": artifacts,
            "metric_crs": projection.to_string(),
            "statistics": {
                "feature_count": len(residents),
                "baseline": baseline["statistics"],
                "hazard": scenario["statistics"],
            },
            "assumptions": assumptions,
            "comparison": comparison["summary"],
            "hazard_scope": hazard_scope,
            "status_definitions": definitions,
        }
    scenario["artifacts"]["access_candidates"] = candidate_artifact(prepared, scenario)
    _label_artifacts(scenario["artifacts"], definitions)
    return {
        "artifacts": scenario["artifacts"],
        "metric_crs": projection.to_string(),
        "statistics": scenario["statistics"],
        "assumptions": assumptions,
        "hazard_scope": hazard_scope,
        "status_definitions": definitions,
    }


def compute_accessibility_comparison(
    roads: Any, residents: Any, shelters: Any, **kwargs: Any
) -> dict[str, Any]:
    """A baseline/hazard comparison sharing every original-road access candidate."""
    return compute_accessibility(roads, residents, shelters, compare=True, **kwargs)


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
    kwargs["objective"] = getattr(args, "objective", "distance")
    action = getattr(args, "action", "route")
    computed = compute_accessibility(
        loaded["roads"],
        loaded["residents"],
        loaded["shelters"],
        hazards=loaded.get("hazards"),
        target_crs=args.target_crs or metric_crs,
        deadline=deadline,
        cancelled=cancelled,
        compare=action == "compare",
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
                **_describe_artifact(label, exported, compare=action == "compare"),
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
            "input_selection": {
                label: {
                    "selection_mode": "explicit_filters"
                    if entry["requested_filters"]
                    else "all_supplied_features",
                    "requested_filters": entry["requested_filters"],
                    "applied_filters": entry["filters"],
                    "feature_count": entry["feature_count"],
                }
                for label, entry in provenance.items()
            },
            "parameters": {**kwargs, "action": action, "target_crs": args.target_crs or metric_crs},
            "assumptions": computed["assumptions"],
            "hazard_scope": computed["hazard_scope"],
            "status_definitions": computed["status_definitions"],
            "statistics": computed["statistics"],
            "paths": paths,
            "layers": layers,
            "table_previews": previews,
            "preview_row_limit": 12,
            "manifest_path": str(directory / "manifest.json"),
        }
        if "comparison" in computed:
            manifest["comparison"] = computed["comparison"]
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
