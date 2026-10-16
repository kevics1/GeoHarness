"""Shared access candidates and comparable hazard routing on one original network.

Endpoint connectors are used only as OD entry/exit costs. They are never global
transit edges, so other residents/shelters cannot become off-road shortcuts.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class AccessCandidate:
    identity: str
    terminal_type: str
    terminal_id: str
    segment: int
    node: tuple
    distance: float
    connector: Any


def prepare_access_network(
    metric: dict,
    road_ids: list[str],
    resident_ids: list[str],
    shelter_ids: list[str],
    directions: list[str],
    *,
    max_snap_distance_m: float,
    objective: str,
    speed_kmh: float,
    connector_speed_kmh: float,
    check: Callable,
) -> dict:
    """Freeze all eligible segment-projection candidates before applying hazards."""
    import networkx as nx
    from shapely import STRtree
    from shapely.geometry import LineString

    segments, closed = [], 0
    original = nx.DiGraph()
    for geometry, road_id, direction in zip(metric["roads"].geometry, road_ids, directions):
        check()
        parts = list(geometry.geoms) if geometry.geom_type == "MultiLineString" else [geometry]
        for part_index, part in enumerate(parts):
            coordinates = list(part.coords)
            for vertex_index, (start, end) in enumerate(zip(coordinates, coordinates[1:])):
                if start == end:
                    continue
                if direction == "closed":
                    closed += 1
                    continue
                geometry = LineString([start, end])
                token = json.dumps([road_id, part_index, vertex_index], ensure_ascii=False)
                segments.append(
                    {
                        "geometry": geometry,
                        "road_id": road_id,
                        "direction": direction,
                        "token": token,
                    }
                )
                if direction in {"both", "forward"}:
                    original.add_edge(start, end)
                if direction in {"both", "reverse"}:
                    original.add_edge(end, start)
    segments.sort(key=lambda value: value["token"])
    tree = STRtree([segment["geometry"] for segment in segments]) if segments else None
    candidates, nearest, splits = {}, {}, {}
    all_candidates = []
    for label, ids in (("residents", resident_ids), ("shelters", shelter_ids)):
        for identity, point in zip(ids, metric[label].geometry):
            check()
            key = label, identity
            candidates[key] = []
            nearest[key] = None
            if tree is None:
                continue
            nearest_segment = int(tree.nearest(point))
            nearest[key] = float(point.distance(segments[nearest_segment]["geometry"]))
            query = point.buffer(max_snap_distance_m).envelope if max_snap_distance_m else point
            for index in sorted(int(value) for value in tree.query(query)):
                line = segments[index]["geometry"]
                if point.distance(line) > max_snap_distance_m + 1e-8:
                    continue
                position = line.project(point)
                if math.isclose(position, 0, rel_tol=0, abs_tol=1e-8):
                    position = 0.0
                elif math.isclose(position, line.length, rel_tol=0, abs_tol=1e-8):
                    position = line.length
                if position == 0:
                    node = tuple(line.coords[0])
                elif position == line.length:
                    node = tuple(line.coords[-1])
                else:
                    xy = tuple(line.interpolate(position).coords[0])
                    # An inserted projection is not an original junction. Give
                    # it a segment-local identity even if another road crosses
                    # the same XY coordinate without an original shared vertex.
                    node = (*xy, segments[index]["token"])
                distance = math.dist(tuple(point.coords[0]), node[:2])
                connector = LineString([point.coords[0], node[:2]]) if distance else point
                encoded = json.dumps(
                    [
                        label,
                        identity,
                        segments[index]["token"],
                        position.hex(),
                        node[0].hex(),
                        node[1].hex(),
                    ],
                    ensure_ascii=False,
                ).encode("utf-8")
                access_id = "access-" + hashlib.sha256(encoded).hexdigest()[:24]
                candidate = AccessCandidate(
                    access_id, label, identity, index, node, distance, connector
                )
                candidates[key].append(candidate)
                all_candidates.append(candidate)
                splits.setdefault(index, {})[position] = node
    graph = nx.DiGraph()
    for index, segment in enumerate(segments):
        check()
        line = segment["geometry"]
        anchors = {
            0.0: tuple(line.coords[0]),
            line.length: tuple(line.coords[-1]),
            **splits.get(index, {}),
        }
        positions = sorted(anchors)
        for lower, upper in zip(positions, positions[1:]):
            start, end = anchors[lower], anchors[upper]
            if start == end:
                continue
            length = math.dist(start[:2], end[:2])
            cost = length if objective == "distance" else length / (speed_kmh * 1000 / 60)
            pairs = []
            if segment["direction"] in {"both", "forward"}:
                pairs.append((start, end))
            if segment["direction"] in {"both", "reverse"}:
                pairs.append((end, start))
            for a, b in pairs:
                graph.add_edge(a, b, length_m=length, cost=cost, segment=index)
    candidate_signature = hashlib.sha256(
        json.dumps(
            sorted((candidate.identity, candidate.distance.hex()) for candidate in all_candidates),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "metric": metric,
        "segments": segments,
        "graph": graph,
        "candidates": candidates,
        "nearest": nearest,
        "all_candidates": all_candidates,
        "candidate_set_sha256": candidate_signature,
        "resident_ids": resident_ids,
        "shelter_ids": shelter_ids,
        "objective": objective,
        "speed_kmh": speed_kmh,
        "connector_speed_kmh": connector_speed_kmh,
        "closed_segments": closed,
        "original_nodes": original.number_of_nodes(),
        "original_edges": original.number_of_edges(),
        "original_weak_components": nx.number_weakly_connected_components(original),
    }


def _connector_cost(candidate: AccessCandidate, prepared: dict) -> float:
    if prepared["objective"] == "distance":
        return candidate.distance
    return candidate.distance / (prepared["connector_speed_kmh"] * 1000 / 60)


def solve_access_scenario(prepared: dict, hazard: Any, *, check: Callable) -> dict:
    """Only remove unsafe road/access edges from the common original candidate graph."""
    import geopandas as gpd
    import networkx as nx
    import pandas as pd
    from shapely.geometry import LineString, Point

    blocked = {
        index
        for index, segment in enumerate(prepared["segments"])
        if hazard is not None and hazard.intersects(segment["geometry"])
    }
    graph = (
        prepared["graph"]
        .edge_subgraph(
            [
                (start, end)
                for start, end, data in prepared["graph"].edges(data=True)
                if data["segment"] not in blocked
            ]
        )
        .copy()
    )
    attachments, allowed_ids = {}, set()
    for key, candidates in prepared["candidates"].items():
        check()
        label, identity = key
        ids = prepared["resident_ids"] if label == "residents" else prepared["shelter_ids"]
        # Geometry stays bound to its stable ID; never sort attributes independently.
        point = prepared["metric"][label].geometry.iloc[ids.index(identity)]
        if hazard is not None and hazard.intersects(point):
            status, reason, safe = "inside_hazard", "endpoint intersects buffered hazards", []
        elif not candidates:
            if not prepared["segments"]:
                status, reason = "no_safe_network", "no traversable road segment in this data"
            else:
                status, reason = (
                    "snap_too_far",
                    "no original-road candidate within configured "
                    "connector limit; actual physical access is not established",
                )
            safe = []
        else:
            safe = [
                candidate
                for candidate in candidates
                if candidate.segment not in blocked
                and candidate.node in graph
                and (hazard is None or not hazard.intersects(candidate.connector))
            ]
            if safe:
                status, reason = "connected", "one or more eligible safe access candidates"
            elif not graph.number_of_edges():
                status, reason = "no_safe_network", "all traversable road segments blocked"
            elif all(candidate.segment in blocked for candidate in candidates):
                status, reason = "road_access_blocked", "all original access road segments blocked"
            else:
                status, reason = "connector_blocked", "all eligible access connectors blocked"
        allowed_ids.update(candidate.identity for candidate in safe)
        attachments[key] = {
            "status": status,
            "reason": reason,
            "candidates": safe,
            "snap_distance_m": min(
                (candidate.distance for candidate in safe), default=prepared["nearest"][key]
            ),
        }
    safe_shelters = {
        key: attachments["shelters", key]["candidates"]
        for key in prepared["shelter_ids"]
        if attachments["shelters", key]["candidates"]
    }
    resident_records, route_records, geometries = [], [], []
    for identity, point in zip(prepared["resident_ids"], prepared["metric"]["residents"].geometry):
        check()
        attachment = attachments["residents", identity]
        record = {
            "gh_resident_id": identity,
            "gh_status": attachment["status"],
            "gh_reason": attachment["reason"],
            "gh_reachable_shelters": 0,
            "gh_shelter_id": None,
            "gh_distance_m": None,
            "gh_time_min": None,
            "gh_snap_distance_m": attachment["snap_distance_m"],
            "gh_access_candidate_count": len(attachment["candidates"]),
            "gh_resident_access_id": None,
            "gh_shelter_access_id": None,
            "gh_objective": prepared["objective"],
            "gh_objective_value": None,
        }
        resident_records.append(record)
        if attachment["status"] != "connected":
            continue
        if not safe_shelters:
            record.update(gh_status="no_available_shelters", gh_reason="no eligible safe shelter")
            continue
        # A temporary source with outgoing connector costs cannot be used as a
        # transit shortcut, and no other terminal's connectors enter this graph.
        source = object()
        seeds = {}
        for candidate in attachment["candidates"]:
            existing = seeds.get(candidate.node)
            if existing is None or (_connector_cost(candidate, prepared), candidate.identity) < (
                _connector_cost(existing, prepared),
                existing.identity,
            ):
                seeds[candidate.node] = candidate
        graph.add_node(source)
        for node, candidate in sorted(seeds.items(), key=lambda item: item[1].identity):
            graph.add_edge(source, node, cost=_connector_cost(candidate, prepared))
        try:
            predecessors, distances = nx.dijkstra_predecessor_and_distance(
                graph,
                source,
                weight="cost",
            )
        finally:
            graph.remove_node(source)
        check()
        options = []
        for shelter_id, candidates in safe_shelters.items():
            reachable = [candidate for candidate in candidates if candidate.node in distances]
            if not reachable:
                continue
            best = min(
                reachable,
                key=lambda candidate: (
                    distances[candidate.node] + _connector_cost(candidate, prepared),
                    candidate.identity,
                ),
            )
            options.append(
                (distances[best.node] + _connector_cost(best, prepared), shelter_id, best)
            )
        if not options:
            record.update(
                gh_status="network_unreachable",
                gh_reason="no directed path between eligible access candidates in this road model",
            )
            continue
        options.sort(key=lambda item: (item[0], item[1], item[2].identity))
        record.update(
            gh_status="reachable",
            gh_reason="optimal objective within shared original-road access candidates",
            gh_reachable_shelters=len(options),
        )
        for rank, (cost, shelter_id, destination) in enumerate(
            options[: prepared["routes_per_resident"]], start=1
        ):
            node = destination.node
            reverse_path = [node]
            while predecessors[node][0] is not source:
                node = predecessors[node][0]
                reverse_path.append(node)
            nodes = list(reversed(reverse_path))
            entry = seeds[nodes[0]]
            road_length = sum(graph[a][b]["length_m"] for a, b in zip(nodes, nodes[1:]))
            connector_length = entry.distance + destination.distance
            total = road_length + connector_length
            minutes = road_length / (prepared["speed_kmh"] * 1000 / 60) + connector_length / (
                prepared["connector_speed_kmh"] * 1000 / 60
            )
            calculated_cost = total if prepared["objective"] == "distance" else minutes
            if not math.isclose(cost, calculated_cost, rel_tol=1e-9, abs_tol=1e-6):
                raise RuntimeError("Objective cost does not match route components")
            destination_index = prepared["shelter_ids"].index(shelter_id)
            destination_point = prepared["metric"]["shelters"].geometry.iloc[destination_index]
            coordinates = [
                tuple(point.coords[0]),
                *(node[:2] for node in nodes),
                tuple(destination_point.coords[0]),
            ]
            coordinates = [
                value
                for index, value in enumerate(coordinates)
                if index == 0 or value != coordinates[index - 1]
            ]
            geometry = LineString(coordinates) if len(coordinates) > 1 else Point(coordinates[0])
            if not math.isclose(geometry.length, total, rel_tol=1e-9, abs_tol=1e-6):
                raise RuntimeError("Route geometry length differs from reported distance")
            if hazard is not None and hazard.intersects(geometry):
                raise RuntimeError("Route intersects a blocked hazard")
            route_records.append(
                {
                    "gh_resident_id": identity,
                    "gh_shelter_id": shelter_id,
                    "gh_rank": rank,
                    "gh_distance_m": total,
                    "gh_road_distance_m": road_length,
                    "gh_connector_distance_m": connector_length,
                    "gh_time_min": minutes,
                    "gh_objective": prepared["objective"],
                    "gh_objective_value": calculated_cost,
                    "gh_resident_access_id": entry.identity,
                    "gh_shelter_access_id": destination.identity,
                    "gh_resident_road_id": prepared["segments"][entry.segment]["road_id"],
                    "gh_shelter_road_id": prepared["segments"][destination.segment]["road_id"],
                    "gh_resident_connector_m": entry.distance,
                    "gh_shelter_connector_m": destination.distance,
                    "gh_resident_access_x": entry.node[0],
                    "gh_resident_access_y": entry.node[1],
                    "gh_shelter_access_x": destination.node[0],
                    "gh_shelter_access_y": destination.node[1],
                }
            )
            geometries.append(geometry)
            if rank == 1:
                record.update(
                    gh_shelter_id=shelter_id,
                    gh_distance_m=total,
                    gh_time_min=minutes,
                    gh_snap_distance_m=entry.distance,
                    gh_objective_value=calculated_cost,
                    gh_resident_access_id=entry.identity,
                    gh_shelter_access_id=destination.identity,
                )
    result = prepared["metric"]["residents"].copy().reset_index(drop=True)
    resident_fields = [
        "gh_resident_id",
        "gh_status",
        "gh_reason",
        "gh_reachable_shelters",
        "gh_shelter_id",
        "gh_distance_m",
        "gh_time_min",
        "gh_snap_distance_m",
        "gh_access_candidate_count",
        "gh_resident_access_id",
        "gh_shelter_access_id",
        "gh_objective",
        "gh_objective_value",
    ]
    for field in resident_fields:
        result[field] = [record[field] for record in resident_records]
    route_fields = [
        "gh_resident_id",
        "gh_shelter_id",
        "gh_rank",
        "gh_distance_m",
        "gh_road_distance_m",
        "gh_connector_distance_m",
        "gh_time_min",
        "gh_objective",
        "gh_objective_value",
        "gh_resident_access_id",
        "gh_shelter_access_id",
        "gh_resident_road_id",
        "gh_shelter_road_id",
        "gh_resident_connector_m",
        "gh_shelter_connector_m",
        "gh_resident_access_x",
        "gh_resident_access_y",
        "gh_shelter_access_x",
        "gh_shelter_access_y",
    ]
    routes = gpd.GeoDataFrame(
        pd.DataFrame(route_records, columns=route_fields), geometry=geometries, crs=result.crs
    )
    shelters = prepared["metric"]["shelters"].copy().reset_index(drop=True)
    shelters["gh_shelter_id"] = prepared["shelter_ids"]
    for field in ("status", "reason", "snap_distance_m"):
        shelters[f"gh_{field}"] = [
            attachments["shelters", key][field] for key in prepared["shelter_ids"]
        ]
    shelters["gh_access_candidate_count"] = [
        len(attachments["shelters", key]["candidates"]) for key in prepared["shelter_ids"]
    ]
    statistics = {
        "feature_count": len(result),
        "resident_count": len(result),
        "shelter_count": len(shelters),
        "safe_connected_shelters": len(safe_shelters),
        "route_count": len(routes),
        "resident_status_counts": dict(Counter(record["gh_status"] for record in resident_records)),
        "original_nodes": prepared["original_nodes"],
        "original_edges": prepared["original_edges"],
        "original_weak_components": prepared["original_weak_components"],
        "safe_nodes": graph.number_of_nodes(),
        "safe_edges": graph.number_of_edges(),
        "safe_weak_components": nx.number_weakly_connected_components(graph),
        "safe_strong_components": nx.number_strongly_connected_components(graph),
        "hazard_blocked_segments": len(blocked),
        "closed_direction_segments": prepared["closed_segments"],
        "original_access_candidates": len(prepared["all_candidates"]),
        "allowed_access_candidates": len(allowed_ids),
    }
    return {
        "artifacts": {"result": result, "routes": routes, "shelters": shelters},
        "statistics": statistics,
        "allowed_access_ids": allowed_ids,
    }


def candidate_artifact(prepared: dict, hazard_result: dict) -> Any:
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Point

    columns = [
        "gh_access_id",
        "gh_terminal_type",
        "gh_terminal_id",
        "gh_road_id",
        "gh_segment_key",
        "gh_connector_m",
        "gh_baseline_allowed",
        "gh_hazard_allowed",
    ]
    records, geometries = [], []
    for candidate in prepared["all_candidates"]:
        segment = prepared["segments"][candidate.segment]
        records.append(
            {
                "gh_access_id": candidate.identity,
                "gh_terminal_type": candidate.terminal_type,
                "gh_terminal_id": candidate.terminal_id,
                "gh_road_id": segment["road_id"],
                "gh_segment_key": segment["token"],
                "gh_connector_m": candidate.distance,
                "gh_baseline_allowed": True,
                "gh_hazard_allowed": candidate.identity in hazard_result["allowed_access_ids"],
            }
        )
        geometries.append(Point(candidate.node[:2]))
    return gpd.GeoDataFrame(
        pd.DataFrame(records, columns=columns),
        geometry=geometries,
        crs=prepared["metric"]["residents"].crs,
    )


def compare_access_results(prepared: dict, baseline: dict, hazard_result: dict) -> dict:
    """Enforce deletion-only objective monotonicity before publishing any comparison."""
    base = baseline["artifacts"]["result"].set_index("gh_resident_id")
    fire = hazard_result["artifacts"]["result"].set_index("gh_resident_id")
    if set(base.index) != set(fire.index):
        raise RuntimeError("Scenario resident identity sets differ")
    if not hazard_result["allowed_access_ids"] <= baseline["allowed_access_ids"]:
        raise RuntimeError("Hazard scenario introduced access candidates absent from baseline")
    records, violations = [], []
    for identity in prepared["resident_ids"]:
        b, h = base.loc[identity], fire.loc[identity]
        b_ok, h_ok = b.gh_status == "reachable", h.gh_status == "reachable"
        delta = float(h.gh_objective_value - b.gh_objective_value) if b_ok and h_ok else None
        tolerance = 1e-6 if prepared["objective"] == "distance" else 1e-8
        monotonic = not ((not b_ok and h_ok) or (delta is not None and delta < -tolerance))
        if not monotonic:
            violations.append(identity)
        records.append(
            {
                "gh_resident_id": identity,
                "gh_baseline_status": b.gh_status,
                "gh_hazard_status": h.gh_status,
                "gh_transition": f"{b.gh_status} -> {h.gh_status}",
                "gh_lost_access": b_ok and not h_ok,
                "gh_gained_access": not b_ok and h_ok,
                "gh_comparable": b_ok and h_ok,
                "gh_objective": prepared["objective"],
                "gh_baseline_objective": float(b.gh_objective_value) if b_ok else None,
                "gh_hazard_objective": float(h.gh_objective_value) if h_ok else None,
                "gh_objective_delta": delta,
                "gh_baseline_shelter_id": b.gh_shelter_id if b_ok else None,
                "gh_hazard_shelter_id": h.gh_shelter_id if h_ok else None,
                "gh_shelter_changed": b.gh_shelter_id != h.gh_shelter_id if b_ok and h_ok else None,
                "gh_resident_access_changed": b.gh_resident_access_id != h.gh_resident_access_id
                if b_ok and h_ok
                else None,
                "gh_shelter_access_changed": b.gh_shelter_access_id != h.gh_shelter_access_id
                if b_ok and h_ok
                else None,
                "gh_baseline_resident_access_id": b.gh_resident_access_id if b_ok else None,
                "gh_hazard_resident_access_id": h.gh_resident_access_id if h_ok else None,
                "gh_baseline_shelter_access_id": b.gh_shelter_access_id if b_ok else None,
                "gh_hazard_shelter_access_id": h.gh_shelter_access_id if h_ok else None,
                "gh_monotonicity": "passed" if monotonic and b_ok and h_ok else "not_applicable",
            }
        )
    if violations:
        from geoharness.analysis.accessibility import AccessibilityContractError

        raise AccessibilityContractError(
            "scenario_monotonicity_failed",
            "Deletion-only scenario improved its optimum or "
            "created new reachability; comparison was not published",
            resident_ids=violations[:20],
        )
    result = prepared["metric"]["residents"].copy().reset_index(drop=True)
    fields = records[0].keys() if records else ["gh_resident_id", "gh_transition"]
    for field in fields:
        result[field] = [record[field] for record in records]
    summary = {
        "preconditions": {
            "same_original_network": True,
            "same_terminal_ids": True,
            "same_access_candidates": True,
            "same_weights_and_objective": True,
            "hazard_only_removes_road_and_access_edges": True,
        },
        "candidate_set_sha256": prepared["candidate_set_sha256"],
        "objective": prepared["objective"],
        "objective_unit": "m" if prepared["objective"] == "distance" else "min",
        "transition_counts": dict(Counter(record["gh_transition"] for record in records)),
        "lost_access_count": sum(record["gh_lost_access"] for record in records),
        "gained_access_count": sum(record["gh_gained_access"] for record in records),
        "comparable_count": sum(record["gh_comparable"] for record in records),
        "resident_access_changed_count": sum(
            record["gh_resident_access_changed"] is True for record in records
        ),
        "shelter_access_changed_count": sum(
            record["gh_shelter_access_changed"] is True for record in records
        ),
        "monotonicity": {"status": "passed", "violation_count": 0},
        "scope": "Road/access model comparison only. snap_too_far is a data/parameter limit, "
        "not proof of physical inaccessibility. No verified pedestrian network/capacity.",
    }
    return {"result": result, "summary": summary}
