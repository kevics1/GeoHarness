"""Task-owned references and execution outcomes; artifact facts remain with their producers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from geoharness.analysis.policy import PolicyDecision


class TaskReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    owner: Literal['python', 'qgis', 'map_ledger', 'review', 'delivery']
    locator: str = Field(min_length=1, max_length=4096)
    sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class OperationOutcome(BaseModel):
    model_config = ConfigDict(extra='forbid')
    execution: Literal['completed', 'failed']
    assessment: Literal['passed', 'issues', 'unverified', 'not_applicable'] = 'not_applicable'
    registration: Literal['committed', 'failed', 'not_applicable'] = 'not_applicable'
    reference: TaskReference | None = None


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: UUID = Field(default_factory=uuid4)
    workspace: str = Field(max_length=4096)
    goal: str = Field(max_length=8192)
    revision: int = Field(default=0, ge=0)
    operations: dict[str, OperationOutcome] = Field(default_factory=dict, max_length=64)
    score_policies: dict[str, PolicyDecision] = Field(default_factory=dict, max_length=16)
    analysis_packages: list[str] = Field(default_factory=list)
    analysis_requirement: Literal['unclassified', 'required', 'not_applicable'] = 'unclassified'


def begin_task(metadata, *, cwd, goal):
    record = TaskRecord(workspace=str(Path(cwd).resolve()), goal=goal[:8192])
    # Keep a shared bucket: upstream tools shallow-copy the metadata dictionary.
    bucket = metadata.setdefault('geo_task_record', {})
    bucket.clear()
    bucket.update(record.model_dump(mode='json'))
    from geoharness.task_store import save_record

    save_record(metadata, cwd=cwd)


def task_snapshot(metadata, *, cwd):
    raw = metadata.get('geo_task_record')
    if not raw:
        return None
    record = TaskRecord.model_validate(raw)
    if Path(record.workspace).resolve() != Path(cwd).resolve():
        raise ValueError('Task record belongs to another workspace')
    return record.model_dump(mode='json')


def observe_operation(metadata, *, tool, action, result):
    """Store only execution observations and immutable locators, never whole report bodies."""
    raw = metadata.get('geo_task_record')
    if not raw:
        return
    record = TaskRecord.model_validate(raw)
    details = result.metadata
    execution = details.get('execution_status') or ('failed' if result.is_error else 'completed')
    registration = details.get('registration_status', 'not_applicable')
    assessment = details.get('review_status', 'not_applicable') if tool == 'geo_map_review' else (
        'unverified' if tool == 'geo_deliver' else 'not_applicable')
    owner = {'geo_python': 'python', 'geo_task': 'python', 'geo_map_review': 'review',
             'geo_deliver': 'delivery', 'geo_qgis': 'qgis',
             'geo_cartography': 'map_ledger', 'geo_qgis_project': 'map_ledger'}.get(tool)
    locator = next((details[k] for k in ('review_path', 'manifest_path', 'project_manifest')
                    if isinstance(details.get(k), str)), None)
    reference = None
    if owner and locator:
        path = Path(locator)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        except OSError:
            digest = None
        reference = TaskReference(owner=owner, locator=locator, sha256=digest)
    outcome = OperationOutcome(execution=execution,
        assessment=assessment if assessment in {'passed', 'issues', 'unverified'}
        else 'not_applicable', registration=registration, reference=reference)
    key = tool + ':' + (action or 'execute')
    record.operations.pop(key, None)
    record.operations[key] = outcome
    if execution == 'completed' and (
        tool in {'geo_analysis', 'geo_accessibility'}
        or tool == 'geo_qgis' and action in {'run', 'model'}
        or tool == 'geo_task' and action in {'score', 'publish_analysis'}
        or tool == 'geo_python' and any(isinstance(f, dict) and
            Path(f.get('path', '')).suffix.lower() in {'.csv', '.geojson', '.gpkg', '.shp', '.tif'}
            for f in details.get('files', []))
    ):
        record.analysis_requirement = 'required'
    record.operations = dict(list(record.operations.items())[-64:])
    record.revision += 1
    raw.clear()
    raw.update(record.model_dump(mode='json'))


def task_context(metadata):
    raw = metadata.get('geo_task_record')
    if not raw:
        return ''
    compact = {key: raw.get(key) for key in ('task_id', 'goal', 'workspace',
        'analysis_requirement')}
    packages = raw.get('analysis_packages', [])
    compact['analysis_packages'] = packages[-8:]
    compact['analysis_package_count'] = len(packages)
    compact['recent_outcomes'] = dict(list(raw.get('operations', {}).items())[-6:])
    compact['score_policies'] = {k: {'status': v.get('status')}
                                for k, v in raw.get('score_policies', {}).items()}
    return ('\n当前任务记录（执行观察与权威记录引用，不代表业务验收）：'
            + json.dumps(compact, ensure_ascii=False, separators=(',', ':'))
            + '\n完整操作使用geo_task evidence分页读取；已有plan后通过run执行其节点。\n')
