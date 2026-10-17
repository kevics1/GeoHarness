"""Dependency-aware Python stages. Completion is backed by real manifests/files."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from geoharness.data.artifacts import assert_usable_artifact, sha256_file


def classify_failure(message: str) -> dict:
    rules = (
        ("syntax", ("SyntaxError", "IndentationError", "invalid_json")),
        ("api_contract", ("unexpected keyword argument", "unsupported options=")),
        ("array_shape", ("RasterShapeError", "Source shape", "tuple index out of range",
                          "inconsistent with given indexes")),
        ("raster_contract", ("RasterNodataError", "RasterTransformError", "RasterDtypeError",
                             "RasterValueError", "RasterBandError")),
        ("permission", ("PermissionError", "outside allowed data/runtime directories")),
        ("dependency", ("ModuleNotFoundError", "Required dependency")),
        ("entity_integrity", ("Entity/geometry mismatch", "Entity key")),
        ("crs_geometry", ("CRS", "geometry", "NaN", "nonfinite")),
        ("input_reference", ("Unknown data ref", "Ambiguous", "FileNotFoundError",
                             "does not exist", "No such file", "not a published output")),
        ("output_contract", ("OutputContractError", "may write only", "partial outputs")),
        ("schema", ("KeyError", "not in index", "validation error", "NameError")),
    )
    category = next((name for name, words in rules if any(w in message for w in words)),
                    "execution")
    if category == "execution" and re.search(
        r"\b(?:HTTP(?:Error|StatusError)?[ :]+|status(?:_code)?[=: ]+)(?:502|503)\b"
        r"|\b(?:TimeoutError|ConnectionError|ConnectTimeout|ReadTimeout)\b", message,
        flags=re.IGNORECASE,
    ):
        category = "transient"
    return {"category": category, "retry_without_change": category == "transient",
            "required_action": "Retry service once after recovery" if category == "transient"
            else "Change the reported input/implementation, then rerun only this stage"}


def validate_expected_outputs(values: list[str]) -> list[str]:
    for value in values:
        path = Path(value)
        if path.is_absolute() or path.drive or '..' in path.parts or not path.name:
            raise ValueError("expected_outputs must be relative files inside RUN_DIR")
        if path.parts[0] == '.cache' or path.name in {
            'analysis.py', 'manifest.json', '.execution.json', 'stdout.txt', 'stderr.txt',
            'integrity.json',
        }:
            raise ValueError("expected_outputs cannot name reserved runtime files")
    return values


def stage_fingerprint(code: str, inputs: dict[str, str], dependencies: dict) -> str:
    inventory = {}
    for name, value in inputs.items():
        path = Path(value)
        assert_usable_artifact(path)
        inventory[name] = {"path": str(path), "sha256": sha256_file(path)}
        if path.suffix.lower() == '.shp':
            inventory[name]['sidecars'] = {suffix: sha256_file(path.with_suffix(suffix))
                for suffix in ('.dbf', '.shx', '.prj', '.cpg')
                if path.with_suffix(suffix).is_file()}
    return hashlib.sha256(json.dumps([code, inventory, dependencies], sort_keys=True)
                          .encode()).hexdigest()


def prepare_stage(metadata: dict, *, stage_id: str, dependencies: list[str],
                  code: str, inputs: dict[str, str], expected_outputs: list[str]) -> str:
    stages = metadata.setdefault('analysis_workflow_state', {})
    if stage_id not in stages and len(stages) >= 64:
        raise ValueError('Stage registry contains 64 distinct names; reuse a stable stage ID')
    versions = {}

    def check_dependency(dep, visiting):
        if dep in visiting or dep == stage_id:
            raise ValueError('Cyclic stage dependency')
        record = stages.get(dep)
        if not isinstance(record, dict) or record.get('status') != 'succeeded':
            raise ValueError(f"Dependency '{dep}' is not complete; run that stage first")
        for version in record.get('input_versions', {}).values():
            path = Path(version['path'])
            assert_usable_artifact(path)
            if sha256_file(path) != version['sha256']:
                raise ValueError(f"Dependency '{dep}' input changed; rerun it before '{stage_id}'")
        for ancestor, version in record.get('dependencies', {}).items():
            if stages.get(ancestor, {}).get('fingerprint') != version:
                raise ValueError(f"Dependency '{dep}' is stale after '{ancestor}' changed")
            check_dependency(ancestor, visiting | {dep})
        for item in record.get('files', []):
            assert_usable_artifact(item['path'])
            if sha256_file(Path(item['path'])) != item['sha256']:
                raise ValueError(f"Dependency '{dep}' changed; rerun it before '{stage_id}'")
        return record.get('fingerprint')

    for dep in dependencies:
        versions[dep] = check_dependency(dep, set())
    fingerprint = stage_fingerprint(code + json.dumps(expected_outputs), inputs, versions)
    previous = stages.get(stage_id)
    if (previous and previous.get('status') == 'failed'
            and previous.get('fingerprint') == fingerprint
            and previous.get('failure', {}).get('category') not in {
                'transient', 'execution', 'dependency'}):
        raise ValueError("Identical deterministic stage failure; no script executed. "
                         f"Inspect {previous.get('manifest_path')} and change the failing "
                         "input/implementation. Successful dependency stages are retained.")
    return fingerprint


def finish_stage(metadata: dict, *, stage_id: str, fingerprint: str, result: dict,
                 dependencies: list[str] | None = None) -> dict:
    stages = metadata.setdefault('analysis_workflow_state', {})
    record = {"status": result['status'], "fingerprint": fingerprint,
              "manifest_path": result['manifest_path'],
              'input_versions': result.get('input_versions', {}),
              'dependencies': {name: stages[name]['fingerprint'] for name in dependencies or []},
              "files": [{"path": f['path'], "sha256": f['sha256']}
                        for f in result.get('files', []) if f.get('state') == 'succeeded'],
              "failure": classify_failure(result.get('stderr', ''))
                         if result['status'] != 'succeeded' else None,
              "scientific_correctness": "unverified"}
    stages[stage_id] = record
    return record
