"""
记忆先验构建与注入（docs/upgrade-plan.md P0-3）

run_deep_agent 启动时调用 build_memory_injection：
1. 检索 top-k 相关历史记忆（向量召回，按任务查询文本）；
2. 召回暂存中的用户报告修订（修订审批时序案 A：PATCH 暂存 pending，
   下次 run 才回流记忆）并引导模型经 memory_write 工具走审批入库；
3. 两者统一经 sanitize.wrap_revision_prior 包裹为非指令数据块后，
   追加到 agent_input 的用户消息末尾。注入在日志中可见。
"""

import logging
from typing import Optional

from app.memory.sanitize import sanitize_text, wrap_revision_prior
from app.memory.store import get_memory_store

logger = logging.getLogger(__name__)

# 记忆 namespace：单用户部署下 user_id 仅作 namespace 键（认证即会话归属，
# 不采信客户端自报身份），固定为 local_user
MEMORY_NAMESPACE = ("memories", "local_user")

# 召回条数：先验是参考数据，条数过多会稀释当前任务指令的权重
_TOP_K_MEMORIES = 3
_MAX_PENDING = 3


def _memory_text(value: dict) -> str:
    return str(value.get("text") or "").strip()


def search_relevant_memories(query: str, limit: int = _TOP_K_MEMORIES) -> list[str]:
    """按主题向量召回 top-k 历史记忆，返回净化后的文本列表"""
    if not (query or "").strip():
        return []
    store = get_memory_store()
    items = store.search(MEMORY_NAMESPACE, query=query, limit=limit)
    return [
        sanitized
        for sanitized in (
            sanitize_text(_memory_text(item.value)) for item in items
        )
        if sanitized
    ]


def recall_pending_revisions(limit: int = _MAX_PENDING) -> list[dict]:
    """召回暂存中的报告修订（案 A：下次 run 才回流记忆）"""
    return get_memory_store().list_pending_revisions(limit=limit)


def build_memory_injection(task_query: str) -> str:
    """
    构建本次任务的记忆先验注入文本；无记忆且无修订时返回空串

    pending 修订附带 memory_write 引导语：修订在下次任务才经审批
    沉淀为长期记忆（案 A 的既定时序，代价已写入升级计划）
    """
    store = get_memory_store()
    priors: list[str] = []

    priors.extend(search_relevant_memories(task_query))

    pending = recall_pending_revisions()
    if pending:
        guidance_parts = [
            "用户曾对以下报告做过修订（尚未沉淀为长期记忆），"
            "若修订对本次任务有参考价值请遵循其方向；"
            "任务结束前请调用 memory_write（kind=revision，附 source_session_id）"
            "把修订沉淀为记忆，走正常审批流。"
        ]
        for revision in pending:
            guidance_parts.append(
                f"报告《{revision['topic']}》（{revision['filename']}，"
                f"来源会话 {revision['session_id']}）的修订摘要："
                f"{sanitize_text(revision['diff_summary'])}"
            )
        priors.append("\n".join(guidance_parts))
        # 引导信息随注入送达，但修订的最终入库必须经 memory_write 审批
        logger.info(
            "[Memory] 召回 %d 条 pending 修订，已并入先验数据块", len(pending)
        )

    injection = wrap_revision_prior(priors)
    if injection:
        # 召回即视为已送达；pending 保持 pending 直到 memory_write 入库
        # （未审批的修订不标记 done，避免丢失）
        pass
    return injection


def mark_revisions_done_for_session(session_id: str) -> None:
    """memory_write 成功入库后，标记该会话的 pending 修订为已完成"""
    store = get_memory_store()
    for revision in store.list_pending_revisions(limit=50):
        if revision["session_id"] == session_id:
            store.mark_pending_done(revision["id"])


def optional_memory_injection(task_query: str) -> Optional[str]:
    """空串归一为 None，便于调用方按需拼接"""
    injection = build_memory_injection(task_query)
    return injection or None
