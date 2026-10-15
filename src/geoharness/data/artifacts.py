"""Producer evidence for local artifacts, shared by discovery and file consumers.

Successful execution is distinct from data validation and scientific correctness.
Ordinary source files do not need a GeoHarness manifest. Known failed or partial
producer outputs, however, cannot become accepted inputs merely by using a path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

NATIVE_ACTIONS = {"buffer", "clip", "intersection", "distance", "count", "impact", "area"}
_BAD_STATES = {"failed", "partial", "prepared", "pending", "running", "cancelled", "interrupted",
               "error", "timeout", "timed_out"}
_GOOD_STATES = {"succeeded", "passed", "validated", "completed", "success"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def producer_kind(document: dict[str, Any]) -> str | None:
    if (isinstance(document.get("paths"), dict)
            and document.get("producer") == "geo_accessibility"
            and document.get("action") == "accessibility"):
        return "geo_accessibility"
    if (isinstance(document.get("run_dir"), str)
            and isinstance(document.get("request_path"), str)
            and isinstance(document.get("result_path"), str) and "exit_code" in document):
        return "geo_qgis"
    if isinstance(document.get("paths"), dict) and document.get("action") in NATIVE_ACTIONS:
        return "geo_analysis"
    if isinstance(document.get("run_dir"), str) and "exit_code" in document:
        return "geo_python"
    # New producers may expose an explicit status and an artifact inventory.
    if (isinstance(document.get("run_dir"), str)
            and isinstance(document.get("files"), list) and "status" in document):
        return str(document.get("producer") or "geo_python")
    return None


def producer_succeeded(document: dict[str, Any]) -> bool:
    status = str(document.get("status", "")).lower()
    if status in _BAD_STATES or document.get("timed_out"):
        return False
    if "exit_code" in document and document["exit_code"] != 0:
        return False
    if "exit_code" in document:
        return not status or status in _GOOD_STATES
    if producer_kind(document) in {"geo_analysis", "geo_accessibility"}:
        # Legacy native manifests are only published after atomic completion.
        return not status or status in _GOOD_STATES
    return status in _GOOD_STATES


def artifact_entries(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Yield locators in manifest order, preserving distinct native output formats."""
    records = []
    if producer_kind(document) in {"geo_analysis", "geo_accessibility"}:
        for label, entry in document["paths"].items():
            if not isinstance(label, str) or not isinstance(entry, dict):
                continue
            for file_format in ("geojson", "csv", "gpkg", "raster"):
                if isinstance(entry.get(file_format), str):
                    metadata = dict(entry)
                    metadata["format"] = file_format
                    digest = entry.get(file_format + "_sha256")
                    if digest is not None:
                        metadata["sha256"] = digest
                    records.append((entry[file_format], label, metadata))
    else:
        records.extend((entry["path"], "", entry) for entry in document.get("files", [])
                       if isinstance(entry, dict) and isinstance(entry.get("path"), str))
    return records


def artifact_succeeded(entry: dict[str, Any]) -> bool:
    state = str(entry.get("state", entry.get("status", ""))).lower()
    return ((not state or state in _GOOD_STATES or state == "unverified")
            and entry.get("usable") is not False
            and str(entry.get("validation_state", "")).lower() not in _BAD_STATES)


def read_manifest(path: Path) -> dict[str, Any] | None:
    if path.resolve() != path.absolute() or path.stat().st_size > 1024 * 1024:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def assert_usable_artifact(path: str | Path) -> dict[str, Any] | None:
    """Reject a known failed/partial/changed artifact; return its producer evidence.

    A normal input with no recognized manifest returns None. This does not confer
    validation. Only inspect ancestors of this exact file, never scan a workspace.
    """
    target = Path(path).absolute()
    if not target.is_file():
        raise FileNotFoundError(f"Artifact/source does not exist: {target}")
    for directory in list(target.parents)[:16]:
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            document = read_manifest(manifest)
        except (OSError, ValueError, TypeError):
            # A damaged manifest inside a recognized run cannot make its outputs
            # trustworthy. Unrelated directories with manifest.json remain sources.
            if (directory / ".execution.json").is_file():
                raise ValueError(f"Producer manifest is unreadable; rerun/recover job: {manifest}")
            continue
        if not document or not producer_kind(document):
            continue
        if document.get("run_dir") and Path(document["run_dir"]).resolve() != directory.resolve():
            continue
        if not producer_succeeded(document):
            raise ValueError("Artifact producer did not succeed; "
                             "partial outputs are inspection-only: "
                             f"{target}. Inspect {manifest} and rerun only the failed stage.")
        for value, label, entry in artifact_entries(document):
            candidate = Path(value)
            candidate = (candidate if candidate.is_absolute() else directory / candidate).absolute()
            if candidate != target:
                continue
            if not artifact_succeeded(entry):
                state = entry.get("state", entry.get("status"))
                raise ValueError(f"Artifact is not usable (state={state}): "
                                 f"{target}; inspect {manifest}")
            expected = entry.get("sha256")
            if expected is not None and sha256_file(target) != expected:
                raise ValueError(f"Artifact fingerprint changed: {target}; "
                                 "inspect/recreate its producer")
            return {"producer": producer_kind(document), "manifest": str(manifest),
                    "output_label": label, "state": "succeeded", **entry}
        # Legacy Python manifests named their immutable analysis script separately
        # from output files. New listed scripts above still require their hash.
        script = document.get("script_path")
        if (producer_kind(document) == "geo_python" and isinstance(script, str)
                and target == (directory / "analysis.py").absolute()
                and Path(script).absolute() == target):
            if document.get('script_sha256') and sha256_file(target) != document['script_sha256']:
                raise ValueError(f'Reproducibility script fingerprint changed: {target}')
            return {"producer": "geo_python", "manifest": str(manifest),
                    "path": str(target), "state": "succeeded",
                    "validation_state": "unverified", "role": "reproducibility_script"}
        if producer_kind(document) == 'geo_python' and target.parent == directory:
            if target.name in {'manifest.json', 'stdout.txt', 'stderr.txt'}:
                return {'producer': 'geo_python', 'manifest': str(manifest),
                        'path': str(target), 'state': 'succeeded',
                        'validation_state': 'unverified', 'role': 'reproducibility_record'}
            if (document.get('integrity_path') == str(target)
                    and document.get('integrity_sha256') == sha256_file(target)):
                return {'producer': 'geo_python', 'manifest': str(manifest),
                        'path': str(target), 'state': 'succeeded',
                        'validation_state': 'unverified', 'role': 'integrity_record'}
        raise ValueError(f"File is not a published output of its producer: {target}; "
                         f"inspect {manifest}")
    return None


def lookup_reference(value: str, references: dict[str, dict]) -> dict[str, Any]:
    """Exact ID/path/name lookup with bounded candidates; never select a near match."""
    if value in references:
        return references[value]
    matches = [item for item in references.values() if value in {
        item.get("path"), item.get("name"), item.get("output_label"),
        Path(item.get("path", "")).stem,
    }]
    if len(matches) == 1:
        return matches[0]
    candidates = matches if matches else list(references.values())
    hint = [{"ref": item.get("ref"), "name": item.get("name"),
             "producer": item.get("producer"), "path": item.get("path")}
            for item in candidates[:6]]
    category = "Ambiguous" if matches else "Unknown"
    raise ValueError(f"{category} data reference {value!r}; use one exact returned ref/path. "
                     f"Candidates: {json.dumps(hint, ensure_ascii=False)}")
