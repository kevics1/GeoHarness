"""Small task-level entry point for confirmed, deterministic scoring."""

import json
from pathlib import Path
from typing import Literal

from openharness.tools.base import BaseTool, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.analysis.policy import ScorePolicy, approved_score_policy, propose_score_policy
from geoharness.analysis.result_package import FactRequest
from geoharness.task_controller import TaskNode


class GeoTaskInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['inspect', 'propose_score', 'score', 'plan', 'run', 'evidence',
                    'publish_analysis', 'resume']
    policy: ScorePolicy | None = None
    policy_ref: str = ''
    source_ref: str = ''
    entity_key: str = ''
    nodes: list[TaskNode] = Field(default_factory=list, max_length=64,
        description='完整依赖计划。arguments可用{"$ref":"node_id#/result/outputs/OUTPUT"}引用前置结果。')
    node_id: str = Field(default='',
        description='run时可只执行一个已就绪节点；省略执行确定性就绪链。')
    operation_id: str = Field(default='', max_length=64,
        description='evidence读取返回的准确operation_id；省略则分页列出调用。')
    section: Literal['output', 'arguments', 'metadata', 'record'] = 'output'
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=12000)
    facts: FactRequest | None = Field(default=None,
        description='publish_analysis的唯一实体键、统计字段、单位和声明方法；统计从source_ref重算。')
    task_id: str = Field(default='', max_length=36,
        description='resume的持久任务UUID；仅允许当前工作区内的已有任务。')

    @model_validator(mode='after')
    def valid_action(self):
        if self.action == 'propose_score' and self.policy is None:
            raise ValueError('propose_score requires a policy')
        if self.action == 'score' and not all((self.policy_ref, self.source_ref, self.entity_key)):
            raise ValueError('score requires policy_ref, source_ref and entity_key')
        if self.action != 'propose_score' and self.policy is not None:
            raise ValueError('Only propose_score accepts policy parameters')
        if self.action == 'plan' and not self.nodes:
            raise ValueError('plan requires nodes')
        if self.nodes and self.action != 'plan':
            raise ValueError('Only plan accepts nodes')
        if self.action == 'publish_analysis' and (not self.facts or not self.source_ref):
            raise ValueError('publish_analysis requires source_ref and facts')
        if self.facts and self.action != 'publish_analysis':
            raise ValueError('Only publish_analysis accepts facts')
        return self


class GeoTaskTool(BaseTool):
    name = 'geo_task'
    description = ('持久任务控制器。plan提交有稳定ID和depends_on的工具节点；run按依赖执行，'
        '复用未变化的成功结果，失败只修当前节点，不能重复同一失败。'
        '标准分析优先geo_qgis；自定义PyQGIS用geo_python backend=qgis。'
        'publish_analysis从已登记成功产物生成选集绑定的统计/中文报告；facts只验证落盘值，'
        '不认证任意方法。evidence按operation_id分页读原始结果/参数，无需启动Python打印日志；'
        'resume恢复当前工作区持久任务。inspect读取当前任务与就绪步骤。'
        'propose_score提交目的、字段、'
        '权重、归一化范围、缺失政策和可选0..1分级并等待真实用户确认；score用已确认policy_ref、'
        'source_ref和entity_key计算并生成同源结果/报告。不能自行设置confirmed或代替用户回答。')
    input_model = GeoTaskInput

    async def execute(self, arguments, context):
        from geoharness.task_state import task_snapshot

        if arguments.action == 'inspect':
            from geoharness.task_controller import plan_status

            snapshot = task_snapshot(context.metadata, cwd=context.cwd)
            if snapshot is not None:
                snapshot['plan'] = plan_status(context)
            return ToolResult(output=json.dumps(snapshot, ensure_ascii=False))
        try:
            if arguments.action == 'plan':
                from geoharness.task_controller import submit_plan

                return ToolResult(output=json.dumps(submit_plan(context, arguments.nodes),
                                                     ensure_ascii=False))
            if arguments.action == 'run':
                from geoharness.task_controller import run_plan

                packet = await run_plan(context, node_id=arguments.node_id)
                failed = any(n['status'] in {'failed', 'interrupted'} for n in packet['nodes'])
                return ToolResult(output=json.dumps(packet, ensure_ascii=False), is_error=failed,
                                  metadata={'reused': packet['reused']})
            if arguments.action == 'evidence':
                from geoharness.task_store import read_evidence

                packet = read_evidence(context.metadata, cwd=context.cwd,
                    operation_id=arguments.operation_id, section=arguments.section,
                    offset=arguments.offset, limit=arguments.limit)
                return ToolResult(output=json.dumps(packet, ensure_ascii=False))
            if arguments.action == 'publish_analysis':
                from geoharness.analysis.result_package import publish_package

                packet = publish_package(context, source_ref=arguments.source_ref,
                                         request=arguments.facts)
                return ToolResult(output=json.dumps(packet, ensure_ascii=False), metadata=packet)
            if arguments.action == 'resume':
                from geoharness.task_controller import resume_task

                return ToolResult(output=json.dumps(resume_task(context, arguments.task_id),
                                                     ensure_ascii=False))
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
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(output=f'Task precondition failed: {exc}', is_error=True,
                              metadata={'script_executed': False})

    def is_read_only(self, arguments):
        return arguments.action in {'inspect', 'evidence'}
