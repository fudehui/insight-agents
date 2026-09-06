"""
任务级工具调用预算与重复调用检测的单元测试

不依赖任何外部服务，直接验证 budget 模块的计数、去重和重置行为
"""

import pytest

from app.api import budget
from app.api.context import reset_thread_context, set_thread_context


@pytest.fixture
def task_context():
    """为每个用例提供独立的 thread_id 上下文，测试结束后恢复"""
    thread_id = "budget_test_session"
    token = set_thread_context(thread_id)
    budget.reset_task_budget(thread_id)
    yield thread_id
    reset_thread_context(token)


def test_first_call_allowed(task_context):
    allowed, reason = budget.consume_tool_quota("internet_search", {"query": "AI 趋势"})
    assert allowed is True
    assert reason == ""


def test_duplicate_call_blocked(task_context):
    args = {"query": "AI 趋势", "topic": "general"}
    allowed_first, _ = budget.consume_tool_quota("internet_search", args)
    allowed_second, reason_second = budget.consume_tool_quota("internet_search", args)
    assert allowed_first is True
    assert allowed_second is False
    assert "重复调用" in reason_second


def test_same_args_different_order_still_duplicate(task_context):
    # sort_keys 保证参数书写顺序不同也视为重复
    budget.consume_tool_quota("internet_search", {"query": "q", "topic": "news"})
    allowed, reason = budget.consume_tool_quota(
        "internet_search", {"topic": "news", "query": "q"}
    )
    assert allowed is False
    assert "重复调用" in reason


def test_different_args_are_independent(task_context):
    budget.consume_tool_quota("internet_search", {"query": "问题一"})
    allowed, _ = budget.consume_tool_quota("internet_search", {"query": "问题二"})
    assert allowed is True


def test_quota_exhausted_after_limit(task_context):
    # 默认限额 internet_search = 5，参数各不相同以避开去重
    for i in range(5):
        allowed, _ = budget.consume_tool_quota("internet_search", {"query": f"问题{i}"})
        assert allowed is True

    allowed, reason = budget.consume_tool_quota(
        "internet_search", {"query": "第六个问题"}
    )
    assert allowed is False
    assert "预算已耗尽" in reason
    assert "5 次" in reason


def test_duplicate_does_not_consume_quota(task_context):
    # 重复调用被拦截时不应占用次数：1 次真实调用 + 4 次重复拦截后，仍可再调 4 次
    args = {"query": "唯一问题"}
    budget.consume_tool_quota("internet_search", args)
    for _ in range(4):
        budget.consume_tool_quota("internet_search", args)

    allowed, _ = budget.consume_tool_quota("internet_search", {"query": "新问题"})
    assert allowed is True


def test_unlimited_tool_has_no_quota(task_context):
    # 未列入限额表的工具不设次数上限
    for i in range(50):
        allowed, _ = budget.consume_tool_quota("unknown_tool", {"i": i})
        assert allowed is True


def test_reset_clears_state(task_context):
    budget.consume_tool_quota("internet_search", {"query": "重置前"})
    budget.reset_task_budget(task_context)

    # 重置后相同参数不再视为重复
    allowed, reason = budget.consume_tool_quota("internet_search", {"query": "重置前"})
    assert allowed is True
    assert reason == ""


def test_no_thread_context_allows_all(monkeypatch):
    # 无会话上下文（本地脚本调试）时不设限，避免工具直接不可用
    # 直接把 get_thread_context 打桩为 None，精确覆盖无上下文分支
    monkeypatch.setattr(budget, "get_thread_context", lambda: None)

    for i in range(10):
        allowed, _ = budget.consume_tool_quota("internet_search", {"query": f"q{i}"})
        assert allowed is True
