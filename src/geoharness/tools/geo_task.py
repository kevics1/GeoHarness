"""Small task-level entry point for confirmed, deterministic scoring."""

import json
from pathlib import Path
from typing import Literal

from openharness.tools.base import BaseTool, ToolResult
from pydantic import BaseModel, ConfigDict, model_validator

from geoharness.analysis.policy import ScorePolicy, approved_score_policy, propose_score_policy


class GeoTaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['inspect', 'propose_score', 'score']
    policy: ScorePolicy | None = None
    policy_ref: str = ''
    source_ref: str = ''
    entity_key: str = ''

    @model_validator(mode='after')
    def valid_action(self):
        if self.action == 'propose_score' and self.policy is None:
            raise ValueError('propose_score requires a policy')
        if self.action == 'score' and not all((self.policy_ref, self.source_ref, self.entity_key)):
            raise ValueError('score requires policy_ref, source_ref and entity_key')
        if self.action != 'propose_score' and self.policy is not None:
            raise ValueError('Only propose_score accepts policy parameters')
        return self


class GeoTaskTool(BaseTool):
    name = 'geo_task'
    description = ('任务状态和标准加权评分。inspect读取当前任务；propose_score提交目的、字段、'
        '权重、归一化范围、缺失政策和可选0..1分级并等待真实用户确认；score用已确认policy_ref、'
        'source_ref和entity_key计算并生成同源结果/报告。不能自行设置confirmed或代替用户回答。')
    input_model = GeoTaskInput

    async def execute(self, arguments, context):
        from geoharness.task_state import task_snapshot

        if arguments.action == 'inspect':
            return ToolResult(output=json.dumps(task_snapshot(context.metadata, cwd=context.cwd),
                                                ensure_ascii=False))
        try:
            if arguments.action == 'propose_score':
                identity, question = propose_score_policy(context.metadata, arguments.policy)
                if question:
                    from geoharness.tools.geo_question import GeoAskUserQuestionTool

                    tool = GeoAskUserQuestionTool()
                    await tool.execute(tool.input_model(question=question), context)
                status = context.metadata['geo_task_record']['score_policies'][identity]['status']
                return ToolResult(output=json.dumps({'policy_ref': identity, 'status': status,
                    'next_action': 'score' if status == 'confirmed' else
                    'await_user_answer' if status == 'pending' else 'revise_policy'},
                    ensure_ascii=False), metadata={'policy_ref': identity,
                                                  'clarification_required': status == 'pending'})
            policy = approved_score_policy(context.metadata, arguments.policy_ref)
            from geoharness.data.artifacts import lookup_reference
            from geoharness.data.task_context import data_ref_snapshot
            from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

            refs = data_ref_snapshot(context.metadata, cwd=Path(context.cwd),
                                     config=context.metadata['geoharness_config'])
            record = lookup_reference(arguments.source_ref, refs)
            vector = record['kind'] == 'vector'
            if not vector and Path(record['path']).suffix.lower() != '.csv':
                raise ValueError('Standard scoring accepts a registered vector or CSV')
            load = f"read_vector({record['ref']!r})" if vector else "pd.read_csv(INPUTS['source'])"
            filename = 'scored.geojson' if vector else 'scored.csv'
            write = ("result.to_file(target,driver='GeoJSON')" if vector else
                     'result.to_csv(target,index=False)')
            code = (f"import pandas as pd\nsource={load}\n"
                    f"result=score_risk(source,key={arguments.entity_key!r})\n"
                    f"target=RUN_DIR/{filename!r}\n{write}\n"
                    "write_analysis_report(RUN_DIR/'score_facts.json',source=target,"
                    f"key={arguments.entity_key!r},numeric_fields=['risk_score'],"
                    "group_by=['risk_status'],method="
                    f"{dict(policy_ref=arguments.policy_ref, policy=policy)!r})\n")
            return await GeoPythonTool().execute(GeoPythonInput(code=code, name='standard_score',
                policy_ref=arguments.policy_ref, input_refs={'source': record['ref']},
                expected_outputs=[filename, 'score_facts.json', 'score_facts.md']), context)
        except (ValueError, OSError) as exc:
            return ToolResult(output=f'Task precondition failed: {exc}', is_error=True,
                              metadata={'script_executed': False})

    def is_read_only(self, arguments):
        return arguments.action == 'inspect'
