"""
MCP Server 模块（docs/upgrade-plan.md P0-4，W8）

把 guarded_call 包裹后的内部工具经 FastMCP 暴露为标准 MCP Server，
供 Claude Code 等外部 Agent 宿主直接调用；反向互操作验证见
tests/test_mcp_server.py（langchain-mcp-adapters）。

两种传输与进程模型（架构终审案 A）：
- SSE：in-process 挂载进现有 FastAPI（见 mount_mcp_sse）——共享进程内的
  预算、审计与令牌配置；鉴权用本模块的 Bearer 包装（支持 Authorization:
  Bearer 与 access_token 查询参数两种形态，与 verify_access 的令牌同源），
  **未配置 APP_ACCESS_TOKEN 时拒绝挂载 SSE**（不继承 verify_access
  "未配置即放行"的语义），默认路径 /mcp，docker-compose 不映射端口。
- stdio：`python -m app.mcp_server` 本地调试模式。工具调用链路与配额
  逻辑与 SSE 完全一致，但运行在独立进程：不与主服务共享配额计数，
  也拿不到内部任务的会话上下文（thread 级预算天然不适用）。

安全约定（评审口径）：
- 默认白名单仅暴露 4 个工具（tavily 检索 1 个 + SQLite 查询 3 个）；
  RAGFlow 与文件交付类默认关闭、read_file_content 与沙箱不封装——
  需要时在 _EXPOSED_TOOLS 增删并同步更新测试与文档。
- MCP 来源调用走 consume_mcp_quota 独立配额（限次数、不去重），
  并打 caller=mcp 审计标记，与内部会话的评测数据可区分。
"""

import logging
from typing import Any

from fastmcp import FastMCP

from app.api.budget import consume_mcp_quota
from app.api.monitor import monitor
from app.tools.db_tools import execute_sql_query, get_table_data, list_sql_tables
from app.tools.tavily_tool import internet_search

logger = logging.getLogger(__name__)

# 暴露白名单：注释即暴露理由（最小暴露原则，见模块 docstring）
_EXPOSED_TOOLS = {
    "internet_search": internet_search,
    "list_sql_tables": list_sql_tables,
    "get_table_data": get_table_data,
    "execute_sql_query": execute_sql_query,
}


def _call_with_quota(tool_name: str, arguments: dict[str, Any]) -> Any:
    """
    MCP 侧统一调用通道：独立配额 -> caller=mcp 审计 -> 调用原工具

    原工具内部的 guarded_call 在缺少会话上下文时预算放行，与本层的
    MCP 独立配额互不冲突（两层语义不同：任务内 thread 预算 vs 跨任务
    的 MCP 服务配额）
    """

    async def call() -> str:
        allowed, reason = consume_mcp_quota(tool_name)
        if not allowed:
            return reason
        monitor.report_tool(
            "MCP工具调用",
            {"caller": "mcp", "tool": tool_name, "参数预览": str(arguments)[:120]},
        )
        return await _EXPOSED_TOOLS[tool_name].ainvoke(arguments)

    return call()


def create_mcp_server() -> FastMCP:
    """构建 MCP Server：按白名单注册工具，名称与内部工具保持一致

    包装函数写显式签名（FastMCP 不支持 **kwargs 工具），参数含义与
    内部工具 docstring 一致，返回值同为面向调用方的文本
    """
    mcp = FastMCP("huiyan-mcp")

    @mcp.tool
    async def internet_search(
        query: str,
        topic: str = "general",
        max_results: int = 5,
        include_raw_content: bool = False,
    ) -> str:
        """根据用户问题检索互联网公开信息（topic 可选 news/finance/general）"""
        return await _call_with_quota(
            "internet_search",
            {
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
            },
        )

    @mcp.tool
    async def list_sql_tables() -> str:
        """查询当前数据库中所有可用的表名"""
        return await _call_with_quota("list_sql_tables", {})

    @mcp.tool
    async def get_table_data(table_name: str) -> str:
        """预览指定表的结构与样例数据（先用本工具了解表结构，再写 SQL）"""
        return await _call_with_quota("get_table_data", {"table_name": table_name})

    @mcp.tool
    async def execute_sql_query(query: str) -> str:
        """执行自定义 SQL 查询（只读），返回 CSV 格式数据"""
        return await _call_with_quota("execute_sql_query", {"query": query})

    return mcp


class _BearerAuthMiddleware:
    """
    MCP SSE 的 Bearer 鉴权 ASGI 包装

    接受两种令牌形态：Authorization: Bearer <token>（MCP 客户端主流）与
    access_token 查询参数（EventSource 等不支持自定义头的 SSE 客户端）。
    令牌比较用常量时间比较，避免时序侧信道
    """

    def __init__(self, app: Any, access_token: str) -> None:
        self.app = app
        self._token = access_token.encode("utf-8")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        import hmac as _hmac

        headers = {k.lower(): v for k, v in scope.get("headers") or []}
        supplied = b""
        if headers.get(b"authorization", b"").lower().startswith(b"bearer "):
            supplied = headers[b"authorization"][7:].strip()
        if not supplied:
            from urllib.parse import parse_qs

            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            supplied = (query.get("access_token") or [""])[0].encode("utf-8")
        if not supplied or not _hmac.compare_digest(supplied, self._token):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Unauthorized: MCP SSE requires a valid access token",
                }
            )
            return
        await self.app(scope, receive, send)


def mount_mcp_sse(app: Any, access_token: str, path: str = "/mcp") -> Any:
    """
    把 MCP Server 以 in-process 方式挂载进现有 FastAPI（案 A）

    安全约定：access_token 为空时抛 RuntimeError 拒绝挂载——MCP 把工具
    暴露给外部宿主，未鉴权的 SSE 等于局域网裸奔，不继承 verify_access
    "未配置即放行"的本地开发语义。

    :return: MCP 子应用的 lifespan 异步上下文管理器。SSE 会话管理器
        需要在服务生命周期内启动，调用方（server.py 的 lifespan）负责
        `async with 返回值():` 把它并入主服务生命周期
    """
    if not (access_token or "").strip():
        raise RuntimeError(
            "MCP SSE 未启用：APP_ACCESS_TOKEN 未配置。"
            "按安全约定（P0-4）未配置令牌时拒绝启动 MCP SSE 传输；"
            "本地调试请使用 stdio 模式：python -m app.mcp_server"
        )
    mcp = create_mcp_server()
    asgi_app = mcp.http_app(transport="sse")
    app.mount(path, _BearerAuthMiddleware(asgi_app, access_token.strip()))
    logger.info("MCP SSE 已挂载于 %s（Bearer 鉴权已启用）", path)

    from contextlib import asynccontextmanager

    # fastmcp 的 .lifespan 是 Starlette 风格函数（lifespan(app)），
    # 这里绑定自身后返回无参 asynccontextmanager，方便调用方并入主服务
    starlette_lifespan = asgi_app.lifespan

    @asynccontextmanager
    async def merged_lifespan():
        async with starlette_lifespan(asgi_app):
            yield

    return merged_lifespan


if __name__ == "__main__":
    # stdio 本地调试模式：python -m app.mcp_server
    # 注意：独立进程运行，配额为进程内独立实例，不与主服务共享；
    # 内部任务的 thread 级预算与审计上下文在此模式不适用（见模块 docstring）
    create_mcp_server().run(transport="stdio")
