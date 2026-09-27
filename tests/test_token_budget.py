"""token_budget_middleware 的预算判定与用量累计测试"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.token_budget_middleware import (
    DEFAULT_TOKEN_BUDGET,
    TokenBudgetExceededError,
    TokenBudgetMiddleware,
)

_LIMIT = 1_000


def _middleware(limit: int = _LIMIT, **kwargs) -> TokenBudgetMiddleware:
    return TokenBudgetMiddleware(token_limit=limit, **kwargs)


def _ai(content: str, total: int | None) -> AIMessage:
    message = AIMessage(content=content)
    if total is not None:
        message.usage_metadata = {
            "input_tokens": total - 10,
            "output_tokens": 10,
            "total_tokens": total,
        }
    return message


def _state(messages: list, tokens_used: int | None = None) -> dict:
    state: dict = {"messages": messages}
    if tokens_used is not None:
        state["thread_tokens_used"] = tokens_used
    return state


class _Runtime:
    """before/after hook 的 runtime 形参，本中间件不读取其字段"""


def test_under_budget_returns_none():
    mw = _middleware()
    state = _state([HumanMessage("问"), _ai("答", 500)], tokens_used=500)

    assert mw.before_model(state, _Runtime()) is None


def test_at_limit_jumps_to_end_with_notice():
    mw = _middleware()
    state = _state([], tokens_used=_LIMIT)

    result = mw.before_model(state, _Runtime())
    assert result is not None
    assert result["jump_to"] == "end"
    (notice,) = result["messages"]
    assert "Token 预算已耗尽" in notice.content
    assert str(_LIMIT) in notice.content
    assert "收尾" in notice.content


def test_over_limit_same_as_at_limit():
    mw = _middleware()
    result = mw.before_model(_state([], tokens_used=_LIMIT * 3), _Runtime())

    assert result is not None and result["jump_to"] == "end"


def test_error_behavior_raises_with_counts():
    mw = _middleware(exit_behavior="error")

    with pytest.raises(TokenBudgetExceededError) as exc_info:
        mw.before_model(_state([], tokens_used=1_500), _Runtime())

    assert exc_info.value.tokens_used == 1_500
    assert exc_info.value.token_limit == _LIMIT


def test_after_model_sums_ai_message_usage_only():
    mw = _middleware()
    state = _state(
        [
            HumanMessage("问题本身不带 usage"),
            _ai("第一轮", 400),
            _ai("第二轮（并行两条）", 250),
            _ai("无 usage 的消息按 0 计", None),
        ]
    )

    result = mw.after_model(state, _Runtime())
    assert result == {"thread_tokens_used": 650}


def test_after_model_falls_back_to_input_plus_output():
    mw = _middleware()
    message = AIMessage("x")
    message.usage_metadata = {"input_tokens": 30, "output_tokens": 12}
    result = mw.after_model(_state([message]), _Runtime())

    assert result == {"thread_tokens_used": 42}


def test_before_model_persists_counter_across_restarts():
    """计数来自 state 而非内存：checkpoint 恢复（新进程）后依旧生效"""
    mw = _middleware()
    # 模拟重启后从 checkpoint 还原的 state：无内存计数，只有历史消息
    state = _state([_ai("历史轮次", 900)])

    assert mw.before_model(state, _Runtime()) is None  # 900 < 1000
    # 又完成一轮消耗 200 的调用：消息列表里多一条 AI 消息
    state["messages"].append(_ai("本轮", 200))
    state["thread_tokens_used"] = mw.after_model(state, _Runtime())[
        "thread_tokens_used"
    ]
    assert state["thread_tokens_used"] == 1100
    assert mw.before_model(state, _Runtime()) is not None  # 1100 >= 1000


def test_env_var_used_when_limit_omitted(monkeypatch):
    monkeypatch.setenv("TOKEN_BUDGET", "77777")
    mw = TokenBudgetMiddleware()

    assert mw.token_limit == 77_777


def test_default_budget_when_no_env(monkeypatch):
    monkeypatch.delenv("TOKEN_BUDGET", raising=False)

    assert TokenBudgetMiddleware().token_limit == DEFAULT_TOKEN_BUDGET


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        _middleware(limit=0)
    with pytest.raises(ValueError):
        _middleware(limit=-1)
    with pytest.raises(ValueError):
        _middleware(exit_behavior="explode")


def test_async_hooks_match_sync_behavior():
    import asyncio

    mw = _middleware()
    state = _state([], tokens_used=_LIMIT)

    assert asyncio.run(mw.abefore_model(state, _Runtime())) == mw.before_model(
        state, _Runtime()
    )
    messages = [_ai("答", 123)]
    async_state = _state(messages)
    assert asyncio.run(mw.aafter_model(async_state, _Runtime())) == mw.after_model(
        async_state, _Runtime()
    )


def test_end_to_end_jump_to_end_inside_deepagents_graph():
    """真实 deepagents 图内验证：预算超限后下一轮模型调用前强制收尾"""
    from deepagents import create_deep_agent
    from langchain.tools import tool as lc_tool
    from langchain_core.language_models import GenericFakeChatModel

    @lc_tool
    def noop() -> str:
        """无操作工具（驱动图进入第二轮模型调用）"""
        return "ok"

    class _FakeModel(GenericFakeChatModel):
        """deepagents 组图时会 bind_tools，fake 模型透传即可"""

        def bind_tools(self, tools, **kwargs):
            return self

    first_round = AIMessage(
        content="",
        tool_calls=[{"name": "noop", "args": {}, "id": "c1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 790, "output_tokens": 10, "total_tokens": 800},
    )
    fake_model = _FakeModel(messages=iter([first_round, _ai("不会被调用", 1)]))
    agent = create_deep_agent(
        model=fake_model,
        system_prompt="测试用最小图",
        tools=[noop],
        middleware=[_middleware(limit=800)],
    )

    result = agent.invoke({"messages": [HumanMessage("长任务")]})

    # 工具轮消耗 800 达到限额，第二次模型调用前跳转图末尾收尾
    assert "Token 预算已耗尽" in result["messages"][-1].content
    assert "800" in result["messages"][-1].content
