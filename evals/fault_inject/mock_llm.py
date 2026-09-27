"""
故障注入演练的 mock-llm 基建（P0-1 W3）

目标：让真实 LangGraph 图（run_deep_agent / resume_deep_agent）在无 LLM API、
无 Tavily API 的环境下按脚本跑通「检索 → （审批中断）→ 报告 → 收尾」，
使演练可重复、零 token 成本、CI 可跑（升级计划硬要求：10 次演练不烧真 token）。

注入点（取证结论）：
- model 仅在 app/agent/main_agent.py 被按值导入（`from app.agent.llm import model`），
  子智能体是字典配置、共用主 model——patch `app.agent.main_agent.model` 即可；
- internet_search 工具内部引用模块级 `tavily_client`（app/tools/tavily_tool.py:54），
  patch `app.tools.tavily_tool.tavily_client` 即断开真实网络。

ScriptedChatModel 按脚本顺序产出消息；脚本项：
    ("tool", name, args_dict)  -> 带单个 tool_call 的 AI 消息
    ("text", content)          -> 纯文本 AI 消息
    ("error", exc)             -> 抛出该异常（模拟断网 / 服务不可达）
cycle=True 时脚本耗尽后从头循环（审批恢复后的后续轮次可继续对上）。
"""

from __future__ import annotations

from typing import Any

import requests

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """按脚本顺序回复的假 ChatModel；error 项抛异常用于注入故障"""

    def __init__(self, script: list[tuple], cycle: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._script = script
        self._cycle = cycle
        self._pos = 0
        # 每次模型调用的计数（含失败），用于演练时间线核对；
        # BaseChatModel 是 pydantic 模型，非字段实例状态须经 object.__setattr__
        object.__setattr__(self, "calls", 0)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake-chat-model"

    def _next(self) -> tuple:
        if self._pos >= len(self._script):
            if not self._cycle:
                raise RuntimeError("mock 脚本已耗尽且未开启 cycle")
            self._pos = 0
        item = self._script[self._pos]
        self._pos += 1
        return item

    def bind_tools(self, tools, **kwargs):
        # deepagents 的图会 bind_tools；假模型直接返回自身即可
        return self

    def _generate(
        self,
        messages: list,
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        object.__setattr__(self, "calls", self.calls + 1)
        item = self._next()
        if item[0] == "error":
            raise item[1]
        if item[0] == "tool":
            _, name, args = item
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": f"call_{self.calls}"}],
            )
        else:
            message = AIMessage(content=str(item[1]))
        return ChatResult(generations=[ChatGeneration(message=message)])


# 标准检索-报告脚本：主模型先经 deepagents 的 task 工具派发网络搜索子智能体
# （internet_search 在子智能体工具清单里，主图直接调用是非法工具——W3 实测
# 踩坑：脚本直接调 internet_search 会被图忽略并跳到下一项），子模型返回检索
# 调用与汇报，主模型再交付报告。报告内容带网络链接与【参考来源】章节，可通过
# generate_markdown 的引用闸门。cycle=True 时脚本耗尽后从头循环。
RESEARCH_SCRIPT = [
    (
        "tool",
        "task",
        {
            "subagent_type": "网络搜索助手",
            "description": "演练任务：检索并总结相关信息",
        },
    ),
    ("tool", "internet_search", {"query": "演练检索词", "max_results": 2}),
    ("text", "已检索到演练来源：https://example.com/chaos，汇报给主智能体。"),
    (
        "tool",
        "generate_markdown",
        {
            "content": (
                "# 故障注入演练报告\n\n"
                "结论：检索到演练内容 [1]。\n\n"
                "【参考来源】\n1. [演练来源](https://example.com/chaos)\n"
            ),
            "filename": "chaos_report.md",
        },
    ),
    ("text", "报告已生成，任务完成。"),
]

# 审批恢复进程（stage2）专用收敛脚本：resume 后 generate_markdown 直接执行
# （无需模型决策），执行完回到模型节点取下一项即收敛结束——若仍从
# RESEARCH_SCRIPT 开头循环，恢复进程会再次派发搜索甚至再次触发审批中断
RESUME_SCRIPT = [("text", "报告已生成，任务完成。")]


class _FakeTavilyClient:
    """假 Tavily 客户端；fail_first_n 次抛异常模拟断网，之后恢复"""

    def __init__(self, fail_first_n: int = 0, error: Exception | None = None):
        self.fail_first_n = fail_first_n
        # 注意用 requests 的 ConnectionError：call_guard.is_retryable 的重试
        # 白名单认 requests.exceptions.ConnectionError，内建 ConnectionError
        # 不在其列（W3 实测踩坑：内建类型不会触发重试，断网恢复演练失真）
        self.error = error or requests.exceptions.ConnectionError(
            "模拟断网：peer closed connection"
        )
        self.search_calls = 0

    def search(self, **kwargs):
        self.search_calls += 1
        if self.search_calls <= self.fail_first_n:
            raise self.error
        return {
            "results": [
                {
                    "title": "演练来源",
                    "url": "https://example.com/chaos",
                    "content": "演练用检索结果摘要",
                }
            ],
            "response_time": 0.01,
        }


def install_mock_llm(script: list[tuple] | None = None, fail_first_n: int = 0):
    """
    在当前进程内安装 mock：返回 (model, fake_tavily) 供演练断言与核对

    必须在 import app.agent.main_agent 之后、调用 run_deep_agent 之前执行；
    pytest 与 fault_chaos 的 worker 入口均走本函数。
    """
    import app.agent.main_agent as main_agent
    from app.tools import tavily_tool

    fake_model = ScriptedChatModel(script or RESEARCH_SCRIPT, cycle=True)
    fake_tavily = _FakeTavilyClient(fail_first_n=fail_first_n)

    main_agent.model = fake_model
    tavily_tool.tavily_client = fake_tavily
    return fake_model, fake_tavily
