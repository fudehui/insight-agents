"""
Tavily 网络搜索工具模块

封装 internet_search 工具，供网络搜索子智能体检索互联网公开信息。
所有外部请求经过 call_guard 防护层：超时、有限重试、错误分类和
tool_start/tool_end 埋点；调用次数受任务级预算约束（默认最多 5 次）。
"""

import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from app.api import source_registry
from app.tools.call_guard import ExternalCallError, guarded_call

load_dotenv()


# TavilyClient 是实际访问搜索服务的客户端；模块级复用可避免每次工具调用重复初始化
tavily_client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

# 双层超时：客户端 15 秒先触发并干净返回（不产生孤儿线程），
# wait_for 的 18 秒只作为取消信号的兜底，略大于客户端超时
_SEARCH_CLIENT_TIMEOUT_S = 15.0
_SEARCH_GUARD_TIMEOUT_S = 18.0
# 网络抖动 / 429 / 临时 5xx 最多额外重试 2 次，退避基数 1 秒
_SEARCH_MAX_RETRIES = 2


# @tool 会把函数签名和 docstring 暴露给 DeepAgents，模型据此决定是否调用以及如何填参
@tool
async def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
):
    """
    根据用户问题检索互联网公开信息

    注意：本工具只用于外部公开网页、新闻、政策等信息，不用于查询业务数据库或 RAGFlow 私有知识库
    :param query: 搜索关键词或自然语言问题
    :param topic: 搜索主题，可选 news、finance、general
    :param max_results: 返回的最大结果数
    :param include_raw_content: 是否返回网页原文内容；False 返回摘要，True 尝试返回更完整正文
    :return: Tavily 返回的结构化搜索结果
    """

    def _do_search():
        # 客户端级超时让 requests 尽快返回，避免线程池里留下还在跑的请求
        return tavily_client.search(
            query=query,
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
            timeout=_SEARCH_CLIENT_TIMEOUT_S,
        )

    try:
        result = await guarded_call(
            "internet_search",
            {
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "include_raw_content": include_raw_content,
            },
            _do_search,
            display_name="网络搜索工具",
            timeout_s=_SEARCH_GUARD_TIMEOUT_S,
            max_retries=_SEARCH_MAX_RETRIES,
            backoff_base_s=1.0,
            budgeted=True,
        )
        # 把真实返回的来源登记进任务级登记表（不经过模型转述），
        # 供报告质量闸门在交付前检查引用完整性；被拦截时返回的是字符串，跳过
        if isinstance(result, dict):
            source_registry.register_web_sources(
                [
                    {"title": r.get("title"), "url": r.get("url")}
                    for r in (result.get("results") or [])
                    if isinstance(r, dict)
                ]
            )
        return result
    except ExternalCallError as e:
        # 失败信息以工具结果形式返回给模型，引导其换检索词或基于已有信息收尾
        return f"网络搜索失败：{e}"


if __name__ == "__main__":
    import asyncio
    from pprint import pprint

    # 本地调试入口：直接运行本文件可验证 TAVILY_API_KEY 和 Tavily API 是否可用
    pprint(
        asyncio.run(
            internet_search.ainvoke(
                {"query": "2026中国法定节假日放假安排表，我天天都想要放假"}
            )
        )
    )
