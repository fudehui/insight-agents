"""MCP Server 协议级测试（docs/upgrade-plan.md P0-4 验收口径）

异步用例统一 asyncio.run 包装（项目未启用 pytest-asyncio 模式）

覆盖：未配置令牌拒绝挂载 SSE、Bearer 鉴权（无令牌/错令牌 401）、
默认白名单与越权工具拒绝、MCP 独立配额、caller=mcp 审计标记，
以及 langchain-mcp-adapters 经 stdio 跨进程互操作的真实冒烟
"""

import asyncio
import sys
from unittest import mock

import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from app.api import budget
from app.mcp_server import _EXPOSED_TOOLS, create_mcp_server, mount_mcp_sse

_TOKEN = "test-mcp-token"


@pytest.fixture(autouse=True)
def _clean_mcp_quota():
    """配额是进程级状态，测试间必须隔离"""
    budget._mcp_budget["counters"].clear()
    yield
    budget._mcp_budget["counters"].clear()


# ---------------------------------------------------------------------------
# 挂载与鉴权（HTTP 层）
# ---------------------------------------------------------------------------


def test_mount_without_token_rejected():
    """安全约定：未配置令牌时拒绝启动 SSE，不继承 verify_access 放行语义"""
    with pytest.raises(RuntimeError, match="APP_ACCESS_TOKEN"):
        mount_mcp_sse(FastAPI(), "")


def test_mount_returns_lifespan_to_merge():
    """挂载返回子应用 lifespan，调用方把它并入主服务生命周期"""
    lifespan = mount_mcp_sse(FastAPI(), _TOKEN)
    assert lifespan is not None


def test_sse_rejects_missing_or_wrong_token():
    app = FastAPI()
    mount_mcp_sse(app, _TOKEN)
    client = TestClient(app)

    for headers, query in (
        ({}, ""),  # 完全无令牌
        ({"Authorization": "Bearer wrong-token"}, ""),  # 错误 Bearer
        ({}, "?access_token=wrong-token"),  # 错误查询参数
    ):
        resp = client.post(f"/mcp/messages/{query}", json={}, headers=headers)
        assert resp.status_code == 401, (headers, query)


def test_sse_accepts_bearer_and_query_token():
    """正确令牌（Bearer 头或查询参数）应穿过鉴权层到达 MCP 应用"""
    app = FastAPI()
    mount_mcp_sse(app, _TOKEN)
    client = TestClient(app)

    authed = client.post(
        "/mcp/messages/", json={}, headers={"Authorization": f"Bearer {_TOKEN}"}
    )
    assert authed.status_code != 401
    via_query = client.post(f"/mcp/messages/?access_token={_TOKEN}", json={})
    assert via_query.status_code != 401


# ---------------------------------------------------------------------------
# 白名单与调用语义（in-memory 客户端，不经过 HTTP 层）
# ---------------------------------------------------------------------------


def test_whitelist_exposes_exactly_four_tools():
    expected = {"internet_search", "list_sql_tables", "get_table_data",
                "execute_sql_query"}
    assert set(_EXPOSED_TOOLS) == expected


def test_whitelisted_tools_callable_and_unlisted_rejected():
    async def scenario():
        async with Client(create_mcp_server()) as client:
            tools = {t.name for t in await client.list_tools()}
            assert tools == set(_EXPOSED_TOOLS)

            # 真实本地 SQLite 查询（不依赖外部服务）
            result = await client.call_tool(
                "execute_sql_query", {"query": "SELECT COUNT(*) AS n FROM drugs"}
            )
            assert "n" in result.content[0].text

            # 白名单外工具未注册，协议层直接拒绝
            with pytest.raises(ToolError, match="Unknown tool"):
                await client.call_tool("read_file_content", {})

    asyncio.run(scenario())


def test_mcp_quota_blocks_after_limit():
    """MCP 独立配额：execute_sql_query 限 15 次，超限返回配额耗尽说明"""
    async def scenario():
        async with Client(create_mcp_server()) as client:
            for i in range(15):
                result = await client.call_tool(
                    "execute_sql_query",
                    {"query": f"SELECT {i} AS marker FROM drugs LIMIT 1"},
                )
                assert "marker" in result.content[0].text, i

            blocked = await client.call_tool(
                "execute_sql_query", {"query": "SELECT 1 AS over FROM drugs"}
            )
            assert "MCP 调用配额已耗尽" in blocked.content[0].text

    asyncio.run(scenario())


def test_mcp_quota_not_shared_with_thread_budget(monkeypatch):
    """MCP 配额独立于内部 thread 预算：MCP 调用不消耗 thread 计数"""
    consumed: list[str] = []
    monkeypatch.setattr(
        budget,
        "consume_tool_quota",
        lambda name, args: consumed.append(name) or (True, ""),
    )

    async def scenario():
        async with Client(create_mcp_server()) as client:
            await client.call_tool("list_sql_tables", {})

    asyncio.run(scenario())
    assert consumed == []  # thread 预算未被触碰


def test_caller_mcp_audit_marked():
    """MCP 来源调用带 caller=mcp 审计标记，与内部会话可区分"""
    from app.api import monitor as monitor_module

    recorded = []

    async def scenario():
        with mock.patch.object(
            monitor_module.monitor,
            "report_tool",
            lambda name, data: recorded.append(data),
        ):
            async with Client(create_mcp_server()) as client:
                await client.call_tool("list_sql_tables", {})

    asyncio.run(scenario())
    # 原工具内部的 guarded_call 埋点也会被捕获；本断言只认 MCP 层的标记条目
    mcp_entries = [e for e in recorded if e.get("caller") == "mcp"]
    assert len(mcp_entries) == 1
    assert mcp_entries[0]["tool"] == "list_sql_tables"


# ---------------------------------------------------------------------------
# 跨宿主互操作（langchain-mcp-adapters，stdio 子进程）
# ---------------------------------------------------------------------------


def test_langchain_adapters_interop_over_stdio():
    """反向接入验证：langchain-mcp-adapters 经 stdio 连接本 server，
    取到白名单工具并完成一次真实调用——Claude Code 等宿主同理接入"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "huiyan": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "app.mcp_server"],
            }
        }
    )
    async def scenario():
        # 0.3.x 用法：连接由 client 内部管理，不再支持 async with
        tools = await client.get_tools()
        assert {t.name for t in tools} == set(_EXPOSED_TOOLS)

        sql_tool = next(t for t in tools if t.name == "execute_sql_query")
        result = await sql_tool.ainvoke(
            {"query": "SELECT COUNT(*) AS n FROM sales_records"}
        )
        assert "n" in str(result)

    asyncio.run(scenario())
