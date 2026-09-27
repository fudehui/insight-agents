"""
长期记忆写入工具（docs/upgrade-plan.md P0-3）

供主智能体把用户偏好、研究结论、报告修订沉淀为跨会话记忆。写入走
HITL 审批（APPROVAL_MODE_PRESETS 的 standard/strict 档拦截
memory_write），是"修订反馈闭环"的最后一环：用户修订 → PATCH 暂存
pending → 下次 run 先验召回 → 模型发起 memory_write → 审批 → 入库。
"""

from pathlib import Path
from typing import Annotated, Optional

try:
    from typing import Literal
except ImportError:  # pragma: no cover
    from typing_extensions import Literal

from langchain_core.tools import tool

from app.api.monitor import monitor
from app.memory import prior
from app.memory.sanitize import sanitize_text, scan_sensitive
from app.memory.store import get_memory_store

# 记忆库文件（与 store 单例同一个 db），用于日志展示规模
_MEMORY_DB_NAME = "data/memory.db"

# 记忆类别白名单：约束 namespace 第三级键，避免模型随手造 namespace
_ALLOWED_KINDS = ("revision", "preference", "finding")


@tool
async def memory_write(
    kind: Annotated[str, "记忆类别：revision（报告修订）/ preference（用户偏好）/ finding（研究结论）"],
    content: Annotated[str, "要沉淀的记忆内容（纯文本，一到三句话）"],
    source_session_id: Annotated[Optional[str], "来源会话 ID（沉淀报告修订时必填）"] = None,
) -> str:
    """
    把用户偏好、研究结论或报告修订写入跨会话长期记忆

    写入前会做敏感信息扫描（手机号/身份证/邮箱/疑似密钥，命中即拒绝）
    与净化（去标记、限长）；本工具受审批档位拦截，批准后才会真正落库。
    :param kind: 记忆类别
    :param content: 记忆内容（纯文本）
    :param source_session_id: 来源会话 ID，沉淀报告修订时用于关单
    :return: 写入结果说明；被安全扫描或审批拦截时返回原因
    """
    monitor.report_tool(
        "记忆写入工具",
        {"内容长度(字符)": len(content), "类别": kind, "内容预览": content[:80]},
    )

    if kind not in _ALLOWED_KINDS:
        return (
            f"错误：kind 必须是 {'/'.join(_ALLOWED_KINDS)} 之一，"
            f"收到的是 {kind!r}。"
        )

    hits = scan_sensitive(content)
    if hits:
        return (
            f"错误：内容包含敏感信息（{'、'.join(hits)}），已拒绝写入记忆。"
            "请去除敏感信息后重试，或改用概括性描述。"
        )

    cleaned = sanitize_text(content)
    if not cleaned:
        return "错误：净化后内容为空，未写入记忆。"

    store = get_memory_store()
    key = f"{kind}-{int(cleaned[:120].encode('utf-8').hex()[:12], 16):x}"
    await store.aput(
        (*prior.MEMORY_NAMESPACE, kind),
        key,
        {
            "text": cleaned,
            "meta": {
                "kind": kind,
                "session_id": source_session_id or "",
            },
        },
    )

    if kind == "revision" and source_session_id:
        prior.mark_revisions_done_for_session(source_session_id)

    return (
        f"已写入长期记忆（类别 {kind}，库文件 {_MEMORY_DB_NAME}，"
        f"键 {key[:24]}…）。同类主题的下一次任务将自动召回该记忆作为先验。"
    )
