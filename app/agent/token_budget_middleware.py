"""
Token 预算中间件（docs/upgrade-plan.md W7 后半）

仿 langchain 官方 ModelCallLimitMiddleware 的结构做 token 维度扩展：
按 thread 累计模型调用的 token 用量（随 checkpoint 持久化，服务重启不丢），
超过阈值后跳转到图末尾强制收尾，防止失控任务耗尽免费档 LLM 窗口。

与 ModelCallLimitMiddleware 的差异：
- 计数对象是 token 而非调用次数——每次 after_model 从 state 消息里
  全量重算累计值（幂等，checkpoint 恢复后无需补账）；
- 只统计主智能体图内消息的用量，子智能体的消耗不在本中间件视野内
  （子图消息不进主图 state）；任务级全口径统计见 usage_callback 与
  evals 的 token_usage 事件，两者口径互补。

配套的 SummarizationMiddleware（langchain 官方组件）负责在逼近上限前
压缩历史，两者组合：先压缩延缓触顶，触顶后强制收尾兜底。

挂载说明：deepagents 的 create_deep_agent 默认中间件栈已内置摘要压缩
（SummarizationMiddleware），不要再手动叠加 langchain 同名实例——
重名会被 create_deep_agent 以"重复中间件"拒绝；本中间件是用户层
唯一需要显式传入的 token 治理组件。
"""

import os
from typing import TYPE_CHECKING, Annotated, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.messages import AIMessage
from typing_extensions import NotRequired, override

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

# 默认预算 150 万 tokens/会话：正常任务基线约 15-30 万（见 evals 基线报告），
# 该值远高于正常波动、低于免费档 429 窗口（约 220-240 万），专拦失控循环。
# 可用环境变量 TOKEN_BUDGET 调整
DEFAULT_TOKEN_BUDGET = 1_500_000


def _message_tokens(message: AIMessage) -> int:
    """从一条 AI 消息提取本次调用的 token 总量，取不到按 0 计"""
    usage = getattr(message, "usage_metadata", None) or {}
    total = usage.get("total_tokens")
    if total:
        return int(total)
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    return int(input_tokens) + int(output_tokens)


def _summarize_thread_tokens(state: dict[str, Any]) -> int:
    """全量重算当前 thread 已消耗的 token（只认 AI 消息上的 usage_metadata）"""
    return sum(
        _message_tokens(message)
        for message in state.get("messages", [])
        if isinstance(message, AIMessage)
    )


class TokenBudgetState(AgentState[Any]):
    """扩展 agent state：thread 级 token 累计（随 checkpoint 持久化）"""

    thread_tokens_used: NotRequired[Annotated[int, PrivateStateAttr]]


class TokenBudgetExceededError(Exception):
    """exit_behavior='error' 时抛出，携带已用量与限额供上层记录"""

    def __init__(self, tokens_used: int, token_limit: int) -> None:
        self.tokens_used = tokens_used
        self.token_limit = token_limit
        super().__init__(
            f"Token budget exceeded: {tokens_used}/{token_limit} tokens"
        )


class TokenBudgetMiddleware(AgentMiddleware[TokenBudgetState, Any, Any]):
    """按 thread 累计 token 用量，超限后强制收尾（jump_to end）"""

    state_schema = TokenBudgetState  # type: ignore[assignment]

    def __init__(
        self,
        *,
        token_limit: int | None = None,
        exit_behavior: str = "end",
    ) -> None:
        """
        :param token_limit: 单个 thread 允许消耗的 token 上限；
            不传时读环境变量 TOKEN_BUDGET，仍无则用 DEFAULT_TOKEN_BUDGET
        :param exit_behavior: 超限行为——'end' 跳到图末尾注入预算耗尽说明
            （强制收尾，已有产物保留）；'error' 抛 TokenBudgetExceededError
        """
        super().__init__()
        if token_limit is None:
            token_limit = int(os.getenv("TOKEN_BUDGET", str(DEFAULT_TOKEN_BUDGET)))
        if token_limit <= 0:
            raise ValueError("token_limit must be positive")
        if exit_behavior not in {"end", "error"}:
            raise ValueError(f"Invalid exit_behavior: {exit_behavior}")
        self.token_limit = token_limit
        self.exit_behavior = exit_behavior

    def _exceeded_result(self, tokens_used: int) -> dict[str, Any] | None:
        """超限后的统一处理：end 注入说明 / error 抛异常"""
        if self.exit_behavior == "error":
            raise TokenBudgetExceededError(tokens_used, self.token_limit)
        notice = AIMessage(
            content=(
                f"Token 预算已耗尽（本会话累计消耗 {tokens_used} tokens，"
                f"上限 {self.token_limit}），任务提前收尾。"
                "已生成的报告与产物文件保持有效；如需继续，请开新会话或"
                "联系管理员调整 TOKEN_BUDGET。"
            )
        )
        return {"jump_to": "end", "messages": [notice]}

    @hook_config(can_jump_to=["end"])
    @override
    def before_model(
        self, state: TokenBudgetState, runtime: "Runtime[Any]"
    ) -> dict[str, Any] | None:
        tokens_used = state.get("thread_tokens_used", 0)
        if tokens_used >= self.token_limit:
            return self._exceeded_result(tokens_used)
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_model(
        self, state: TokenBudgetState, runtime: "Runtime[Any]"
    ) -> dict[str, Any] | None:
        return self.before_model(state, runtime)

    @override
    def after_model(
        self, state: TokenBudgetState, runtime: "Runtime[Any]"
    ) -> dict[str, Any] | None:
        # 全量重算而非增量：同一轮模型可能并行产生多条 AI 消息，
        # 增量计数需要去重逻辑；消息全量在 state 里，重算幂等且
        # checkpoint 恢复后无需补账
        return {"thread_tokens_used": _summarize_thread_tokens(state)}

    async def aafter_model(
        self, state: TokenBudgetState, runtime: "Runtime[Any]"
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
