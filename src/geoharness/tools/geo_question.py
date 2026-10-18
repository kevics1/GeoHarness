"""中文澄清入口；没有答案时保留待回答状态，而非让模型自行假定。"""

from openharness.tools.ask_user_question_tool import AskUserQuestionTool
from openharness.tools.base import ToolResult


class GeoAskUserQuestionTool(AskUserQuestionTool):
    description: str = (
        "向用户询问无法从工具查明、会影响分析正确性的业务含义或选择。"
        "问题使用中文，说明已知信息、缺少什么及其影响，合并相关问题。"
        "等待真实回答；空回复、取消或仅要求继续均不代表同意默认选项。"
        "不要与依赖答案的分析或地图修改并行调用。"
    )

    async def execute(self, arguments, context):
        question = arguments.question.strip()
        state = context.metadata.setdefault('geo_clarification', {})
        previous = state.get('pending') or {}
        state['pending'] = {**(previous if previous.get('question') == question else {}),
                            'question': question}
        prompt = context.metadata.get('ask_user_prompt')
        if not callable(prompt):
            return ToolResult(output=f"等待用户回答：{question}",
                              metadata={'clarification_required': True})
        answer = await prompt(question)
        answer = '' if answer is None else str(answer).strip()
        if not answer or answer.strip('。.!！').lower() in {
                '继续', '接着做', 'continue', 'resume', '取消',
                'cancel', 'cancelled', '(no response)'}:
            return ToolResult(output=f"尚未收到明确答案，等待用户回答：{question}",
                              metadata={'clarification_required': True})
        from geoharness.analysis.policy import resolve_policy_answer

        resolve_policy_answer(context.metadata, answer)
        state.pop('pending', None)
        return ToolResult(output=answer, metadata={'clarification_required': False})
