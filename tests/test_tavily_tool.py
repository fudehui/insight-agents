"""
Tavily 网络搜索工具的单元测试（W3 覆盖率补测）

覆盖：成功检索并把来源登记进 source_registry、guard 失败转工具结果字符串、
非 dict 结果（被预算/去重拦截的字符串）不触发来源登记。
全程 mock TavilyClient.search，不访问真实网络。
"""

import asyncio
from unittest import mock

import pytest

from app.api import source_registry
from app.api.budget import reset_task_budget
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.tools import tavily_tool as tavily_tools


@pytest.fixture
def search_env(tmp_path):
    """独立会话上下文 + 屏蔽真实检索客户端；预算重置避免去重指纹跨测试泄漏"""
    dir_token = set_session_context(str(tmp_path))
    thread_token = set_thread_context("tavily_test_session")
    source_registry.reset_task_sources("tavily_test_session")
    reset_task_budget("tavily_test_session")
    yield tmp_path
    reset_session_context(dir_token, thread_token)


def _fake_search_result():
    return {
        "results": [
            {"title": "报告A", "url": "https://e.com/a", "content": "摘要A"},
            {"title": "报告B", "url": "https://e.com/b", "content": "摘要B"},
            "脏数据不是 dict 应被跳过",
        ],
        "response_time": 0.3,
    }


def test_search_success_registers_web_sources(search_env, monkeypatch):
    fake_client = mock.Mock()
    fake_client.search.return_value = _fake_search_result()
    monkeypatch.setattr(tavily_tools, "tavily_client", fake_client)

    result = asyncio.run(
        tavily_tools.internet_search.ainvoke({"query": "测试检索词"})
    )

    assert result["results"][0]["title"] == "报告A"
    # 客户端收到检索参数且带 15s 客户端超时
    kwargs = fake_client.search.call_args[1]
    assert kwargs["query"] == "测试检索词"
    assert kwargs["timeout"] == 15.0
    # 登记进任务来源表：只有 dict 项，脏数据被过滤
    sources = source_registry.get_task_sources()
    assert [s["title"] for s in sources["web"]] == ["报告A", "报告B"]


def test_search_failure_returns_tool_result_string(search_env, monkeypatch):
    fake_client = mock.Mock()
    fake_client.search.side_effect = RuntimeError("连接超时")
    monkeypatch.setattr(tavily_tools, "tavily_client", fake_client)

    result = asyncio.run(
        tavily_tools.internet_search.ainvoke({"query": "测试检索词"})
    )

    # call_guard 把异常归类为 ExternalCallError，工具层转为引导模型的字符串
    assert "网络搜索失败" in result
    # 失败时不登记任何来源
    assert source_registry.get_task_sources()["web"] == []


def test_search_rejection_string_passes_through_without_registration(
    search_env, monkeypatch
):
    # guarded_call 被预算/去重拦截时返回字符串（非 dict），应原样返回且不登记
    async def fake_guarded_call(tool_name, args, fn, **kwargs):
        return "调用预算已耗尽：internet_search 已达上限"

    monkeypatch.setattr(tavily_tools, "guarded_call", fake_guarded_call)

    result = asyncio.run(
        tavily_tools.internet_search.ainvoke({"query": "测试检索词"})
    )

    assert result == "调用预算已耗尽：internet_search 已达上限"
    assert source_registry.get_task_sources()["web"] == []
