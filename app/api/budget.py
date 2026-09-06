"""
任务级工具调用预算与重复调用检测模块

在代码层面约束单个任务内各工具的最大调用次数，并拦截参数完全相同的重复调用，
不再单纯依赖提示词里的"最多检索 5 次"等约定。

状态保存在进程内存中，按 thread_id 隔离：run_deep_agent 每次任务启动时调用
reset_task_budget 重置。任务持久化暂不实施，重启丢失计数是已接受的取舍。
"""

import hashlib
import json
import os
import threading
from typing import Any, Optional, Tuple

from app.api.context import get_thread_context

# 工具注册名 -> 单任务最大调用次数；未列出的工具不设次数上限（仍可做去重）
DEFAULT_TOOL_LIMITS = {
    # 与网络搜索助手提示词"一共最多进行 5 次检索"对齐，由代码强制兜底
    "internet_search": 5,
    # 提示词要求复杂问题至少 3 个角度，8 次留出追问余量
    "create_ask_delete": 8,
    "get_assistant_list": 5,
    "get_table_data": 10,
    "execute_sql_query": 15,
}

# 可选的环境变量覆盖：TOOL_BUDGET_JSON='{"internet_search": 3}'，便于不改代码调参
_ENV_OVERRIDE = os.getenv("TOOL_BUDGET_JSON")

_lock = threading.Lock()
# thread_id -> {"counters": {工具名: 已调用次数}, "seen": {调用指纹集合}}
_task_budgets: dict[str, dict[str, Any]] = {}


def _tool_limits() -> dict[str, int]:
    """
    合并默认限额与环境变量覆盖

    环境变量格式非法时直接忽略并退回默认值，不让配置问题阻断任务
    """
    limits = dict(DEFAULT_TOOL_LIMITS)
    if _ENV_OVERRIDE:
        try:
            overrides = json.loads(_ENV_OVERRIDE)
            if isinstance(overrides, dict):
                for tool_name, value in overrides.items():
                    if isinstance(value, int) and value > 0:
                        limits[tool_name] = value
        except json.JSONDecodeError:
            print("[Budget] TOOL_BUDGET_JSON 不是合法 JSON，已忽略并使用默认限额")
    return limits


def reset_task_budget(thread_id: str) -> None:
    """
    任务启动时清空该会话的计数与去重状态

    同一 thread_id 可能先后执行多次任务（前端复用会话），每次启动都必须重置，
    否则第二次任务会继承上一次的剩余预算
    """
    with _lock:
        _task_budgets[thread_id] = {"counters": {}, "seen": set()}


def cleanup_task_budget(thread_id: str) -> None:
    """
    任务结束后删除该会话的预算状态

    只在任务启动时重置会让条目只增不减：长期运行（尤其是删除会话后），
    进程内会积累大量孤儿 thread 的计数与去重指纹
    """
    with _lock:
        _task_budgets.pop(thread_id, None)


def _fingerprint(tool_name: str, args: Optional[dict]) -> str:
    """
    生成一次调用的去重指纹：工具名 + 规范化参数

    sort_keys 保证相同参数不同书写顺序的调用也视为重复
    """
    canonical = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.md5(f"{tool_name}|{canonical}".encode("utf-8")).hexdigest()
    return digest


def consume_tool_quota(tool_name: str, args: Optional[dict]) -> Tuple[bool, str]:
    """
    检查并占用一次工具调用配额

    执行顺序：先查重复（重复不消耗次数），再查剩余次数，最后计数 + 1。
    guard 层的重试不经过本函数，只有模型发起的真实调用才占用预算。
    :param tool_name: 工具注册名（限额表的 key）
    :param args: 本次调用参数，用于生成去重指纹
    :return: (是否放行, 拒绝原因说明)。拒绝原因面向模型，引导其复用结果或收尾
    """
    thread_id = get_thread_context()
    if not thread_id:
        # 无会话上下文（本地脚本调试等场景）时不设限，避免工具直接不可用
        return True, ""

    fingerprint = _fingerprint(tool_name, args)

    with _lock:
        budget = _task_budgets.setdefault(thread_id, {"counters": {}, "seen": set()})

        if fingerprint in budget["seen"]:
            return False, (
                f"检测到重复调用：本次 {tool_name} 的参数与之前的调用完全相同，"
                "请直接复用之前返回的结果；如需补充新信息，请调整参数后重试。"
            )

        limit = _tool_limits().get(tool_name)
        used = budget["counters"].get(tool_name, 0)
        if limit is not None and used >= limit:
            return False, (
                f"调用预算已耗尽：{tool_name} 在本次任务中最多允许调用 {limit} 次，"
                "已达上限。请基于已收集的信息继续完成任务并输出结论。"
            )

        budget["seen"].add(fingerprint)
        budget["counters"][tool_name] = used + 1
        return True, ""
