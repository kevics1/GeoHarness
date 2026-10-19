"""Score policy confirmation is established by the user-answer seam, not model flags."""

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoreComponent(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    field: str = Field(min_length=1, max_length=128, description='实际存在的数值指标字段名。')
    weight: float = Field(gt=0, le=1, description='正权重比例，所有指标权重之和必须为1。')
    minimum: float = Field(description='归一化下限，与该指标原始字段单位一致；必须小于maximum。')
    maximum: float = Field(description='归一化上限，与原始字段单位一致；不能从外观猜测范围。')
    direction: Literal['higher', 'lower'] = Field(
        description='higher为原值越大得分越高；lower为原值越小得分越高。')

    @model_validator(mode='after')
    def valid_range(self):
        if self.minimum >= self.maximum:
            raise ValueError('minimum must be below maximum')
        return self


class ScoreBand(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    minimum: float = Field(ge=0, le=1, description='归一化分级下界，含该值。')
    maximum: float = Field(ge=0, le=1, description='归一化分级上界，通常不含；最后一档包含1。')
    label: str = Field(min_length=1, max_length=80, description='经用户确认的该档中文名称。')

    @model_validator(mode='after')
    def valid_range(self):
        if self.minimum >= self.maximum:
            raise ValueError('classification minimum must be below maximum')
        return self


class ScorePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purpose: str = Field(min_length=1, max_length=1000, description='评分用途及拟支持的判断。')
    components: list[ScoreComponent] = Field(min_length=1, max_length=32,
        description='各指标字段、权重、归一化范围和方向；字段不得重复，权重合计1。')
    missing_policy: Literal['error', 'unknown', 'renormalize'] = Field(default='unknown',
        description='error遇缺失失败；unknown保留未知分数；renormalize按可用指标重分权重，须用户确认。')
    classification: list[ScoreBand] | None = Field(default=None, max_length=8,
        description='可选0..1分数分级，连续无重叠覆盖全范围；省略只生成连续分数。')

    @model_validator(mode='after')
    def valid_components(self):
        if len({c.field for c in self.components}) != len(self.components):
            raise ValueError('Component fields must be distinct')
        if not math.isclose(sum(c.weight for c in self.components), 1, abs_tol=1e-9):
            raise ValueError('Weights must sum to one; do not silently normalize')
        if self.classification:
            ordered = sorted(self.classification, key=lambda item: item.minimum)
            if ordered[0].minimum != 0 or ordered[-1].maximum != 1:
                raise ValueError('classification must cover normalized scores from 0 to 1')
            if any(left.maximum != right.minimum for left, right in zip(ordered, ordered[1:])):
                raise ValueError('classification bands must be contiguous and non-overlapping')
        return self


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    policy: ScorePolicy
    status: Literal['pending', 'confirmed', 'rejected', 'needs_revision'] = 'pending'
    user_answer: str | None = Field(default=None, max_length=8192)


def propose_score_policy(metadata, policy):
    task = metadata.get('geo_task_record')
    if not task:
        raise ValueError('Score policy requires an active task')
    identity = 'policy:' + hashlib.sha256((task['task_id'] + policy.model_dump_json()).encode(
        'utf-8')).hexdigest()[:24]
    pending = metadata.get('geo_clarification', {}).get('pending') or {}
    if pending and pending.get('policy_ref') != identity:
        raise ValueError('已有待回答问题；不能覆盖尚未确认的方案')
    decisions = task.setdefault('score_policies', {})
    previous = decisions.get(identity)
    if previous and previous.get('status') in {'confirmed', 'rejected', 'needs_revision'}:
        return identity, None
    if identity not in decisions and len(decisions) >= 16:
        raise ValueError('Too many policy proposals; revise an existing task deliberately')
    decisions[identity] = PolicyDecision(policy=policy).model_dump(mode='json')
    question = policy_question(policy)
    metadata.setdefault('geo_clarification', {})['pending'] = {
        'question': question, 'policy_ref': identity}
    return identity, question


def policy_question(policy):
    return ('是否同意以下评分政策？请明确回答“同意”或说明需要修改的部分。'
                '未确认前只执行独立的数据核对，不执行依赖该政策的评分。\n'
                + json.dumps(policy.model_dump(mode='json'), ensure_ascii=False))


def resolve_policy_answer(metadata, answer):
    pending = metadata.get('geo_clarification', {}).get('pending') or {}
    identity = pending.get('policy_ref')
    decisions = metadata.get('geo_task_record', {}).get('score_policies', {})
    if not identity or identity not in decisions:
        return
    decision = PolicyDecision.model_validate(decisions[identity])
    decision.user_answer = answer[:8192]
    normalized = answer.strip().strip('。.!！').lower()
    decision.status = ('confirmed' if normalized in {
        '同意', '可以', '按这个方案', '按此方案', '同意上述方案', 'yes', 'approve'} else
        'rejected' if normalized in {'不同意', '拒绝', 'no', 'reject'} else 'needs_revision')
    decisions[identity] = decision.model_dump(mode='json')
    task = metadata.get('geo_task_record')
    if task:
        from geoharness.task_store import save_record

        save_record(metadata, cwd=task['workspace'])


def approved_score_policy(metadata, identity):
    raw = metadata.get('geo_task_record', {}).get('score_policies', {}).get(identity)
    if not raw:
        raise ValueError('Unknown policy_ref; propose a policy before scoring')
    decision = PolicyDecision.model_validate(raw)
    if decision.status != 'confirmed':
        raise ValueError('评分政策尚未得到用户明确确认；不能执行依赖评分。')
    return decision.policy.model_dump(mode='json')
