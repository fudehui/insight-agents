"""
模型 Token 用量回调处理器

通过 LangChain 的回调机制上报每一次模型调用的 Token 用量。回调会随
RunnableConfig 自动穿透到子智能体的图执行，因此主智能体和网络搜索、
数据库、RAGFlow 子智能体内部的模型调用全部覆盖——弥补了此前只在
astream 外层读取消息 usage_metadata 时，子智能体消耗不可见的盲区。

替换说明：主智能体的调用同样由本回调上报（每次模型调用恰好触发一次
on_llm_end，不存在流式片段重复），run_deep_agent 里原有的按消息 id
去重上报逻辑已随之移除。
"""

from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.api.monitor import monitor


def _pick(usage: Any, *names: str) -> Optional[int]:
    """
    从 provider 原生 token 用量对象中按候选字段名取值

    不同来源形态不一：langchain 归一化的 usage_metadata 是 dict，
    OpenAI 原生对象可能是 dict 也可能是带属性的 CompletionUsage，
    这里统一兼容并转成 int，取不到返回 None
    """
    for name in names:
        value = (
            usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        )
        if value is not None:
            return int(value)
    return None


class TokenUsageCallbackHandler(BaseCallbackHandler):
    """每次模型调用结束时上报 Token 用量事件"""

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        # 首选 langchain 归一化的 usage_metadata：字段名统一、必然是 dict
        message_usage: Optional[dict] = None
        try:
            message_usage = response.generations[0][0].message.usage_metadata
        except (IndexError, AttributeError, TypeError):
            message_usage = None

        if message_usage:
            self._report(message_usage, "input_tokens", "output_tokens", "total_tokens")
            return

        # 兜底：llm_output 中 provider 原生字段（OpenAI 风格命名）
        try:
            usage = (response.llm_output or {}).get("token_usage")
        except (AttributeError, TypeError):
            usage = None
        if usage:
            self._report(usage, "prompt_tokens", "completion_tokens", "total_tokens")

    def _report(self, usage: Any, in_key: str, out_key: str, total_key: str) -> None:
        input_tokens = _pick(usage, in_key)
        if input_tokens is None:
            # 完全取不到用量的调用（如部分流式场景）静默跳过，不上报 0 值噪音
            return
        output_tokens = _pick(usage, out_key) or 0
        total_tokens = _pick(usage, total_key) or (input_tokens + output_tokens)
        monitor.report_token_usage(input_tokens, output_tokens, total_tokens)
