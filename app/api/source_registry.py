"""
任务级来源登记表模块

在工具层自动登记每次任务实际收集到的外部来源：网络链接、RAGFlow 文档、
SQL 查询记录。登记的是"工具真实返回的原始来源"，不经过模型转述——
即使搜索子智能体汇总时丢弃了 URL，闸门仍能把完整清单递回给模型。

供 generate_markdown 的质量闸门在报告交付前检查引用完整性：
网络来源已收集但报告无链接时拒绝写入，并把来源清单喂回模型重写。

状态保存在进程内存、按 thread_id 隔离，run_deep_agent 每次任务启动时重置；
与 budget.py 一致，任务持久化暂不实施，重启丢失是已接受的取舍。
"""

import threading
from typing import Any

from app.api.context import get_thread_context

_lock = threading.Lock()

# thread_id -> {"web": [{"title","url"}], "docs": [{"doc","page"}], "sql": [str],
#               "gate_rejections": int}
_task_sources: dict[str, dict[str, Any]] = {}

# 清单中单条 SQL 的最大长度，避免长查询撑爆闸门返回信息
_SQL_DISPLAY_LEN = 150


def _ensure_entry(thread_id: str) -> dict[str, Any]:
    """取到（或初始化）指定任务的来源登记结构，调用方需已持有锁"""
    return _task_sources.setdefault(
        thread_id, {"web": [], "docs": [], "sql": [], "gate_rejections": 0}
    )


def reset_task_sources(thread_id: str) -> None:
    """任务启动时清空该会话的来源登记；同一 thread_id 复用会话时必须重置"""
    with _lock:
        _task_sources[thread_id] = {
            "web": [],
            "docs": [],
            "sql": [],
            "gate_rejections": 0,
        }


def cleanup_task_sources(thread_id: str) -> None:
    """
    任务结束后删除该会话的来源登记

    与 budget 清理同理：只重置不删除会让条目随任务次数线性增长，
    删除会话后也会残留孤儿数据
    """
    with _lock:
        _task_sources.pop(thread_id, None)


def register_web_sources(items: list) -> None:
    """
    登记网络来源（标题 + URL），按 URL 去重

    :param items: [{"title": ..., "url": ...}]，缺 url 的条目忽略
    """
    thread_id = get_thread_context()
    if not thread_id:
        return

    with _lock:
        entry = _ensure_entry(thread_id)
        known_urls = {s["url"] for s in entry["web"]}
        for item in items:
            url = str(item.get("url") or "").strip()
            if not url or url in known_urls:
                continue
            known_urls.add(url)
            entry["web"].append(
                {"title": str(item.get("title") or "").strip(), "url": url}
            )


def register_doc_source(doc: str, page: str = "") -> None:
    """登记 RAGFlow 文档来源，按（文档名, 页码）去重"""
    thread_id = get_thread_context()
    if not thread_id:
        return

    doc_name = str(doc or "").strip()
    if not doc_name:
        return

    with _lock:
        entry = _ensure_entry(thread_id)
        key = (doc_name, str(page or "").strip())
        if key not in {(s["doc"], s["page"]) for s in entry["docs"]}:
            entry["docs"].append({"doc": doc_name, "page": key[1]})


def register_sql(query: str) -> None:
    """登记实际执行的 SQL 查询，按原文去重"""
    thread_id = get_thread_context()
    if not thread_id:
        return

    text = str(query or "").strip()
    if not text:
        return

    with _lock:
        entry = _ensure_entry(thread_id)
        if text not in entry["sql"]:
            entry["sql"].append(text)


def count_gate_rejection() -> int:
    """
    记录一次质量闸门拒绝，返回累计拒绝次数

    闸门据此决定是再次退回模型重写，还是兜底自动附注来源后放行
    """
    thread_id = get_thread_context()
    if not thread_id:
        return 0

    with _lock:
        entry = _ensure_entry(thread_id)
        entry["gate_rejections"] += 1
        return entry["gate_rejections"]


def get_task_sources() -> dict[str, Any]:
    """
    读取当前任务的全部登记来源

    :return: {"web": [...], "docs": [...], "sql": [...]}；无会话上下文时为空结构
    """
    thread_id = get_thread_context()
    if not thread_id:
        return {"web": [], "docs": [], "sql": []}

    with _lock:
        entry = _ensure_entry(thread_id)
        return {
            "web": [dict(s) for s in entry["web"]],
            "docs": [dict(s) for s in entry["docs"]],
            "sql": list(entry["sql"]),
        }


def format_source_manifest() -> str:
    """
    把登记来源渲染成可直接写进报告的 Markdown 清单

    网络来源用 Markdown 链接格式；RAGFlow 来源带页码；SQL 截断展示。
    闸门拒绝信息与自动附注章节共用此渲染，保证模型拿到的是同一份清单
    """
    sources = get_task_sources()
    lines: list[str] = []

    web = sources.get("web") or []
    if web:
        lines.append("网络来源：")
        lines.extend(
            f"{i}. [{s['title'] or '无标题'}]({s['url']})"
            for i, s in enumerate(web, start=1)
        )

    docs = sources.get("docs") or []
    if docs:
        lines.append("RAGFlow 文档来源：")
        lines.extend(
            f"{i}. 《{s['doc']}》" + (f"第{s['page']}页" if s["page"] else "")
            for i, s in enumerate(docs, start=1)
        )

    sql_records = sources.get("sql") or []
    if sql_records:
        lines.append("数据库查询记录：")
        lines.extend(
            f"{i}. {q if len(q) <= _SQL_DISPLAY_LEN else q[:_SQL_DISPLAY_LEN] + '...'}"
            for i, q in enumerate(sql_records, start=1)
        )

    return "\n".join(lines) if lines else "（本次任务没有登记到外部来源）"
