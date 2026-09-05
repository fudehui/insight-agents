"""
外部调用防护层的单元测试

用假的外部函数验证：超时、重试、错误分类、取消传播、预算拦截和埋点事件。
所有用例不发起真实网络请求
"""

import asyncio
import time
from unittest import mock

import pytest
import requests

from app.api import budget
from app.api.context import reset_thread_context, set_thread_context
from app.tools import call_guard


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def monitor_spy():
    """捕获 guard 发出的埋点事件，同时避免真实落盘"""
    with (
        mock.patch.object(call_guard.monitor, "report_tool") as start,
        mock.patch.object(call_guard.monitor, "report_tool_end") as end,
        mock.patch.object(call_guard.monitor, "report_budget_exceeded") as exceeded,
    ):
        yield {"start": start, "end": end, "exceeded": exceeded}


@pytest.fixture
def task_context():
    thread_id = "guard_test_session"
    token = set_thread_context(thread_id)
    budget.reset_task_budget(thread_id)
    yield thread_id
    reset_thread_context(token)


def test_success_returns_result_and_emits_events(monitor_spy):
    async def run():
        return await call_guard.guarded_call(
            "demo_tool", {"x": 1}, lambda: "结果文本", display_name="演示工具"
        )

    result = asyncio.run(run())

    assert result == "结果文本"
    monitor_spy["start"].assert_called_once_with("演示工具", {"x": 1})
    monitor_spy["end"].assert_called_once()
    _, kwargs = monitor_spy["end"].call_args
    assert kwargs["status"] == "success"
    assert kwargs["retries"] == 0
    assert kwargs["summary"] == "结果文本"


def test_timeout_raises_after_retries(monitor_spy):
    async def run():
        return await call_guard.guarded_call(
            "demo_tool",
            {},
            lambda: time.sleep(0.3),
            timeout_s=0.05,
            max_retries=1,
            backoff_base_s=0.01,
        )

    with pytest.raises(call_guard.ExternalCallError) as exc_info:
        asyncio.run(run())

    assert exc_info.value.error_type == "timeout"
    assert exc_info.value.attempts == 2  # 首次 + 1 次重试都超时
    _, kwargs = monitor_spy["end"].call_args
    assert kwargs["status"] == "failed"
    assert kwargs["error_type"] == "timeout"
    assert kwargs["retries"] == 1


def test_non_retryable_fails_immediately(monitor_spy):
    calls = {"n": 0}

    def always_bad_request():
        calls["n"] += 1
        raise requests.exceptions.HTTPError("bad request", response=FakeResponse(400))

    async def run():
        return await call_guard.guarded_call(
            "demo_tool", {}, always_bad_request, max_retries=3, backoff_base_s=0.01
        )

    with pytest.raises(call_guard.ExternalCallError) as exc_info:
        asyncio.run(run())

    # 400 不可重试：只尝试 1 次
    assert calls["n"] == 1
    assert exc_info.value.error_type == "http_400"


def test_retryable_http_error_then_success(monitor_spy):
    calls = {"n": 0}

    def flaky_429():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.HTTPError("rate limited", response=FakeResponse(429))
        return "恢复后的结果"

    async def run():
        return await call_guard.guarded_call(
            "demo_tool", {}, flaky_429, max_retries=2, backoff_base_s=0.01
        )

    result = asyncio.run(run())

    assert result == "恢复后的结果"
    assert calls["n"] == 2
    _, kwargs = monitor_spy["end"].call_args
    assert kwargs["status"] == "success"
    assert kwargs["retries"] == 1


def test_connection_error_is_retryable():
    # 构造异常对象而非 raise，直接交给分类函数判断
    assert call_guard.is_retryable(
        requests.exceptions.ConnectionError("connection refused")
    ) is True


def test_tavily_custom_timeout_is_retryable():
    # tavily 的 TimeoutError 与内建类同名但无继承关系，按模块名识别
    tavily_timeout = mock.Mock(spec=Exception)
    type(tavily_timeout).__name__ = "TimeoutError"
    type(tavily_timeout).__module__ = "tavily.errors"
    with mock.patch.object(call_guard, "_status_code_of", return_value=None):
        assert call_guard.is_retryable(tavily_timeout) is True


def test_status_code_classification():
    http_429 = requests.exceptions.HTTPError("rate", response=FakeResponse(429))
    http_403 = requests.exceptions.HTTPError("forbidden", response=FakeResponse(403))
    assert call_guard.is_retryable(http_429) is True
    assert call_guard.is_retryable(http_403) is False


def test_budget_block_returns_message_without_executing(monitor_spy, task_context):
    executed = {"n": 0}

    def should_never_run():
        executed["n"] += 1
        return "不应该被执行"

    args = {"query": "重复问题"}

    async def run():
        first = await call_guard.guarded_call(
            "internet_search", args, should_never_run, budgeted=True
        )
        second = await call_guard.guarded_call(
            "internet_search", args, should_never_run, budgeted=True
        )
        return first, second

    first, second = asyncio.run(run())

    assert executed["n"] == 1  # 第二次被去重拦截，函数体只执行一次
    assert "重复调用" in second
    monitor_spy["exceeded"].assert_called_once()


def test_cancelled_error_propagates(monitor_spy):
    async def run():
        task = asyncio.create_task(
            call_guard.guarded_call(
                "demo_tool", {}, lambda: time.sleep(1.0), timeout_s=10.0
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    # 任务取消必须原样上抛，不能被吞成 ExternalCallError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


def test_summary_truncated(monitor_spy):
    long_result = "x" * 500

    async def run():
        return await call_guard.guarded_call("demo_tool", {}, lambda: long_result)

    asyncio.run(run())

    _, kwargs = monitor_spy["end"].call_args
    assert len(kwargs["summary"]) <= call_guard._SUMMARY_MAX_LEN + 3  # 截断 + "..."
    assert kwargs["summary"].endswith("...")
