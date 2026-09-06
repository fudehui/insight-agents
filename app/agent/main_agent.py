"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent（新任务）与 resume_deep_agent（审批恢复）两个执行入口。
运行时为每个 session_id 维护独立工作目录，把工具调用、子智能体调用、
流式增量、审批请求和最终结果推送给前端。
"""

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any

import aiosqlite
from deepagents import create_deep_agent
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.budget import cleanup_task_budget, reset_task_budget
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor
from app.api.source_registry import (
    cleanup_task_sources,
    get_task_sources,
    reset_task_sources,
)
from app.agent.usage_callback import TokenUsageCallbackHandler

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content

# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 会话记忆落盘到 data/checkpoints.db：服务重启（含 --reload 改码触发）后，
# 同一 thread_id 仍能延续对话上下文，与 events.jsonl 回放的历史保持一致。
# Agent 经 astream 异步驱动，checkpoint 走异步方法，必须用 AsyncSqliteSaver
# （同步 SqliteSaver 运行时报"does not support async methods"）；
# aiosqlite 连接只能在运行中的事件循环里创建，因此延迟到首次执行时组装
_checkpoint_db_path = project_root_path / "data" / "checkpoints.db"
_checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)

_main_agent = None
_main_agent_lock = asyncio.Lock()


# 三档审批档位的工具拦截清单（前端下拉与后端路由共用同一语义）
APPROVAL_MODE_PRESETS: dict[str, dict[str, bool]] = {
    # 关闭审批：工具直接执行（不会产生待审批任务）
    "off": {},
    # 标准审批（默认）：文件交付类动作需要确认
    "standard": {
        "generate_markdown": True,
        "convert_md_to_pdf": True,
    },
    # 严格审批：文件、SQL 查询、网络搜索、知识库提问均需确认；
    # 框架会把 interrupt_on 继承给子智能体，因此子智能体内部的
    # SQL/搜索/知识库工具调用同样会被拦截（探针验证见 .analysis）
    "strict": {
        "generate_markdown": True,
        "convert_md_to_pdf": True,
        "execute_sql_query": True,
        "internet_search": True,
        "create_ask_delete": True,
    },
}
_VALID_APPROVAL_MODES = set(APPROVAL_MODE_PRESETS)

_checkpointer: AsyncSqliteSaver | None = None
# 按档位缓存 agent 实例：同档位复用，所有实例共享同一检查点，
# 因此切换档位只影响新任务，待审批任务仍能按记录的档位恢复
_agents: dict[str, Any] = {}


def _server_default_interrupt_on() -> dict[str, bool]:
    """
    服务端缺省拦截清单（请求未指定档位时使用）

    HITL_APPROVAL_TOOLS 未设置时回落标准档；显式设为空串关闭审批
    """
    raw = os.getenv("HITL_APPROVAL_TOOLS")
    if raw is None:
        return APPROVAL_MODE_PRESETS["standard"]
    names = [name.strip() for name in raw.split(",") if name.strip()]
    return {name: True for name in names}


def _resolve_approval_mode(approval_mode: str | None) -> str:
    """
    归一化审批档位为 agent 缓存键

    合法档位直接使用；未传/未知值回落服务端缺省（env 自定义清单或标准档），
    保证旧版本前端不传参时行为与原先一致
    """
    if approval_mode in _VALID_APPROVAL_MODES:
        return approval_mode
    return "server_default"


def _interrupt_on_for(cache_key: str) -> dict[str, bool]:
    if cache_key == "off":
        return {}
    if cache_key == "standard":
        return APPROVAL_MODE_PRESETS["standard"]
    if cache_key == "strict":
        return APPROVAL_MODE_PRESETS["strict"]
    return _server_default_interrupt_on()


async def get_main_agent(approval_mode: str | None = None):
    """
    按审批档位取得主智能体；同档位复用缓存，首次调用时创建检查点连接

    所有档位的实例共享同一个 AsyncSqliteSaver：切换档位只影响新任务，
    已暂停的待审批任务仍能按记录的档位恢复。关闭档的图中没有审批
    中间件节点，恢复流程固定使用带中间件的实例（见 resume_deep_agent）
    """
    global _checkpointer
    cache_key = _resolve_approval_mode(approval_mode)
    async with _main_agent_lock:
        if _checkpointer is None:
            conn = await aiosqlite.connect(_checkpoint_db_path)
            _checkpointer = AsyncSqliteSaver(conn)
            await _checkpointer.setup()
        if cache_key not in _agents:
            interrupt_on = _interrupt_on_for(cache_key)
            _agents[cache_key] = create_deep_agent(
                model=model,
                system_prompt=main_agent_content["system_prompt"],
                tools=[generate_markdown, convert_md_to_pdf, read_file_content],
                checkpointer=_checkpointer,
                subagents=[
                    database_query_agent,
                    network_search_agent,
                    knowledge_base_agent,
                ],
                # 高危工具执行前中断等待人工审批；空配置等价于不启用
                interrupt_on=interrupt_on or None,
            )
    return _agents[cache_key]


# 整图最大执行步数（模型/工具轮次），是防止无限循环的最后保险。
# 默认 80：多助手调度 + 长报告生成的正常任务约需 30-50 步，80 留足余量；
# 失控循环已被工具级预算（次数上限 + 去重）先行拦截，这里只兜最底。
# 可通过环境变量 AGENT_RECURSION_LIMIT 按任务复杂度调整
AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "80"))

# 会话目录中的事件日志由 monitor 持续追加，不属于任务"生成"的产物
_EVENTS_FILENAME = "events.jsonl"

# 跨中断轮次的进程内任务状态：
# - 基线快照按 thread_id 延续，否则"批准→继续生成→再次中断"后，
#   首次批准前生成的文件不会出现在最终产物清单里
# - 待审动作登记审批暂停态，恢复/终结/删除会话时清除
# - 档位映射记录任务启动时的审批档位，恢复时据此路由到同一实例
#   （探针验证：严格档下子智能体内部工具的中断会冒泡到顶层，恢复正常）
_task_baselines: dict[str, dict[str, float]] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}
_task_modes: dict[str, str] = {}


def _snapshot_session_files(session_dir: Path) -> dict[str, float]:
    """
    记录会话目录中已有文件的相对路径和修改时间

    任务结束时与当前状态对比，diff 出本次任务真正生成或更新的文件，
    避免同一会话内多次任务的产物在文件面板中互相累积
    """
    snapshot: dict[str, float] = {}
    for file_path in session_dir.rglob("*"):
        if file_path.is_file() and file_path.name != _EVENTS_FILENAME:
            snapshot[file_path.relative_to(session_dir).as_posix()] = (
                file_path.stat().st_mtime
            )
    return snapshot


def _diff_task_files(session_dir: Path, baseline: dict[str, float]) -> list[dict]:
    """
    对比任务开始前的快照，返回本次任务新生成或修改的文件清单

    返回结构与 /api/files 一致（name/path/size/mtime），前端可直接用于渲染
    """
    generated: list[dict] = []
    for file_path in session_dir.rglob("*"):
        if not file_path.is_file() or file_path.name == _EVENTS_FILENAME:
            continue
        relative = file_path.relative_to(session_dir).as_posix()
        mtime = file_path.stat().st_mtime
        # 新出现的文件，或快照之后被覆写（mtime 变化）的文件都算本次产物
        if relative not in baseline or baseline[relative] != mtime:
            generated.append(
                {
                    "name": file_path.name,
                    "type": "file",
                    "path": str(file_path),
                    "size": file_path.stat().st_size,
                    "mtime": mtime,
                }
            )
    # 最新生成的排前面，与文件面板的排序习惯一致
    generated.sort(key=lambda item: item["mtime"], reverse=True)
    return generated


def _summarize_args(args: Any) -> dict[str, str]:
    """
    压缩待审动作的参数用于事件与前端展示

    generate_markdown 的 content 参数是整份报告全文，原样进事件会把
    events.jsonl 和审批卡片都撑爆；长值只保留前 300 字符加全文字数提示
    """
    if not isinstance(args, dict):
        return {}
    summary: dict[str, str] = {}
    for key, value in args.items():
        text = str(value)
        summary[str(key)] = (
            text if len(text) <= 300 else f"{text[:300]}...（全文 {len(text)} 字）"
        )
    return summary


def _parse_interrupt_actions(interrupts) -> list[dict[str, Any]]:
    """
    把 __interrupt__/检查点里的 Interrupt 对象解析成待审动作列表

    每个 Interrupt.value 形如 {"action_requests": [{"name", "args"}],
    "review_configs": [{"action_name", "allowed_decisions"}]}（探针已验证）
    """
    actions: list[dict[str, Any]] = []
    for interrupt in interrupts or ():
        value = getattr(interrupt, "value", None)
        if not isinstance(value, dict):
            continue
        allowed_by_action = {
            config.get("action_name"): config.get("allowed_decisions")
            for config in value.get("review_configs") or []
            if isinstance(config, dict)
        }
        for request in value.get("action_requests") or []:
            if not isinstance(request, dict) or not request.get("name"):
                continue
            actions.append(
                {
                    "name": str(request["name"]),
                    "args": _summarize_args(request.get("args")),
                    "allowed_decisions": allowed_by_action.get(request["name"])
                    or ["approve", "reject"],
                }
            )
    return actions


def _register_interrupt(session_id: str, interrupt_state) -> None:
    """登记本次中断的待审动作并推送 approval_required 事件"""
    actions = _parse_interrupt_actions(interrupt_state)
    if not actions:
        return
    _pending_approvals[session_id] = {"actions": actions}
    monitor.report_approval_required(actions)


async def get_pending_approval(thread_id: str) -> list[dict[str, Any]] | None:
    """
    读取指定会话的待审动作列表

    进程内登记优先；后端重启后登记丢失但中断检查点仍在，此时经
    aget_state 从检查点重建（探针验证：暂停态的 interrupts 字段保留
    完整 action_requests），重建结果写回登记供后续恢复流程使用
    """
    pending = _pending_approvals.get(thread_id)
    if pending:
        return pending["actions"]

    try:
        main_agent = await get_main_agent()
        state = await main_agent.aget_state({"configurable": {"thread_id": thread_id}})
    except Exception as e:
        print(f"[MainAgent] 读取检查点待审状态失败: {e}")
        return None

    actions = _parse_interrupt_actions(state.interrupts)
    if not actions:
        return None
    _pending_approvals[thread_id] = {"actions": actions}
    return actions


def clear_task_runtime_state(thread_id: str) -> None:
    """删除会话或任务真终结时清理进程内的基线与待审登记"""
    _task_baselines.pop(thread_id, None)
    _pending_approvals.pop(thread_id, None)
    _task_modes.pop(thread_id, None)


def _build_agent_config(thread_id: str) -> dict[str, Any]:
    """任务执行与审批恢复共用的 RunnableConfig"""
    return {
        "configurable": {"thread_id": thread_id},
        # 步数上限是防止无限循环的最后保险，即使模型无视提示词也会被硬性拦下
        "recursion_limit": AGENT_RECURSION_LIMIT,
        # 回调随 RunnableConfig 穿透到子智能体，Token 用量覆盖主+子全部模型调用
        "callbacks": [TokenUsageCallbackHandler()],
    }


async def _stream_agent(
    thread_id: str,
    agent_input: dict[str, Any],
    config: dict[str, Any],
    approval_mode: str,
) -> bool:
    """
    驱动一次双通道流式执行

    :param approval_mode: 本轮执行使用的审批档位（决定路由到哪个 agent 实例）
    :returns: 是否因命中人工审批而暂停（True = 任务未终结，等待恢复）
    """
    interrupted = False
    # 首次执行时在当前事件循环内组装主智能体（含异步 checkpoint 连接）
    main_agent = await get_main_agent(approval_mode)

    # 流式增量合帧缓冲：攒够约 150ms 的文本再推一次，避免逐 token
    # 高频事件拖垮 WebSocket 与前端渲染
    delta_buffer: list[str] = []
    last_delta_flush = time.perf_counter()

    def flush_delta() -> None:
        nonlocal last_delta_flush
        if delta_buffer:
            monitor.report_task_delta("".join(delta_buffer))
            delta_buffer.clear()
            last_delta_flush = time.perf_counter()

    # 双通道流式：updates 提供节点级状态（子智能体调用、审批中断、最终结果），
    # messages 提供主模型 token 级增量，支撑前端"边生成边显示"。
    # 探针验证（deepagents 0.5.7）：主图 model 节点增量的 checkpoint_ns
    # 形如 "model:<uuid>"；子智能体内部 token 不会冒泡到顶层流，天然隔离
    async for mode, chunk in main_agent.astream(
        agent_input, config=config, stream_mode=["updates", "messages"]
    ):
        if mode == "messages":
            msg, meta = chunk
            checkpoint_ns = str(meta.get("checkpoint_ns") or "")
            # 只转发主图 model 节点的纯文本增量；嵌套子图（含 task: 前缀或
            # 多级命名空间）与工具调用片段一律跳过
            if (
                meta.get("langgraph_node") == "model"
                and "task:" not in checkpoint_ns
                and "|" not in checkpoint_ns
            ):
                if isinstance(msg, AIMessageChunk) and not msg.tool_call_chunks:
                    text = msg.content if isinstance(msg.content, str) else ""
                    if text:
                        delta_buffer.append(text)
                        if time.perf_counter() - last_delta_flush >= 0.15:
                            flush_delta()
            continue

        # updates 通道
        for node_name, state in chunk.items():
            # 命中人工审批：登记待审动作并通知前端，本次 astream 随即结束
            if node_name == "__interrupt__":
                flush_delta()
                interrupted = True
                _register_interrupt(thread_id, state)
                continue
            if not state or "messages" not in state:
                continue
            messages = state["messages"]
            if messages and isinstance(messages, list):
                last_msg = messages[-1]
                if node_name == "model":
                    # Token 用量改由 TokenUsageCallbackHandler 统一上报，
                    # 这里只保留子智能体调用展示和最终结果提取
                    if last_msg.tool_calls:
                        # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                        for tool_call in last_msg.tool_calls:
                            if tool_call["name"] == "task":
                                # 子智能体调用单独上报，前端可以展示“正在调用哪个专家助手”
                                monitor.report_assistant(
                                    tool_call["args"]["subagent_type"],
                                    {"description": tool_call["args"]["description"]},
                                )
                    elif last_msg.content:
                        # 先把尚未推送的流式增量补齐，再发权威的完整结果
                        flush_delta()
                        # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                        print(f"主智能体执行结果，最终结果：{last_msg.content[:100]}")
                        monitor.report_task_result(last_msg.content)
    # 图执行正常收尾后兜底补发残余增量（正常情况下已在 task_result 前清空）
    flush_delta()
    return interrupted


def _finalize_task(thread_id: str, session_dir: Path) -> None:
    """
    任务真正终结（完成/取消/失败）时的收尾

    推送本次产物清单与来源登记，并清理进程内状态。审批暂停时不能走到
    这里——任务还没结束，预算/来源/基线都要留给恢复轮次继续使用
    """
    # 无论正常完成、取消还是异常，都把本次任务实际产生的文件清单推给前端，
    # 每个对话轮次的产物列表据此只展示本轮生成的文件
    try:
        baseline = _task_baselines.pop(thread_id, None)
        if baseline is None:
            # 后端重启后恢复的任务丢失了原始基线，退化为当前快照
            baseline = _snapshot_session_files(session_dir)
        task_files = _diff_task_files(session_dir, baseline)
        if task_files:
            monitor.report_task_files(task_files)
    except Exception as e:
        print(f"[MainAgent] 任务文件清单统计失败: {e}")
    # 溯源信息同步推送：来源登记表依赖 thread ContextVar，必须在恢复上下文前读取；
    # 即使任务中途失败/取消，已收集到的来源也要让用户看到
    try:
        task_sources = get_task_sources()
        if task_sources["web"] or task_sources["docs"] or task_sources["sql"]:
            monitor.report_task_sources(task_sources)
    except Exception as e:
        print(f"[MainAgent] 任务来源清单推送失败: {e}")
    # 进程内登记清理：必须在 sources 推送之后执行
    clear_task_runtime_state(thread_id)
    cleanup_task_budget(thread_id)
    cleanup_task_sources(thread_id)


async def _execute_agent_run(
    thread_id: str,
    session_dir: Path,
    agent_input: dict[str, Any],
    config: dict[str, Any],
    approval_mode: str,
) -> None:
    """
    驱动一次完整执行并统一处理异常与收尾

    新任务与审批恢复共用：调用方负责设置会话上下文（monitor 与深层工具
    依赖 ContextVar），本函数在退出前完成产物推送与状态清理
    """
    interrupted = False
    try:
        interrupted = await _stream_agent(thread_id, agent_input, config, approval_mode)
    except asyncio.CancelledError:
        # 用户取消任务必须原样上抛给 asyncio，但先上报事件
        monitor.report_task_cancelled()
        raise
    except GraphRecursionError:
        # 步数预算耗尽：上报原因后必须补发 task_result 终止事件，
        # 否则前端不会把任务标记为结束，界面将停留在"运行中"状态
        reason = (
            f"任务已达到最大执行步数上限（{AGENT_RECURSION_LIMIT} 步），"
            "为控制成本提前终止，已收集的信息保存在上方执行记录中。"
        )
        monitor.report_budget_exceeded("主智能体", reason)
        monitor.report_task_result(reason)
    except Exception as e:
        # 异步执行异常也走 monitor，保证前端能收到明确错误事件
        monitor._emit("error", f"执行主智能发生异常信息：{str(e)}")
    finally:
        # 审批暂停不是终结：产物/来源/预算/基线全部留给恢复轮次
        if not interrupted:
            _finalize_task(thread_id, session_dir)


async def run_deep_agent(
    task_query, session_id, approval_mode: str | None = None
) -> None:
    """
    异步流式执行一次新任务

    API 层会为每次任务传入用户问题和 session_id。本函数负责准备会话目录、
    复制上传文件、写入 ContextVar，并把执行过程推送给前端。
    :param task_query: 前端提交的原始任务问题
    :param approval_mode: 前端选择的审批档位（off/standard/strict），
        未传时使用服务端缺省（env 自定义清单或标准档）
    :param session_id: 当前任务 ID，同时用于 thread_id、输出目录和 WebSocket 定向推送
    """
    print(f"[MainAgent] 开始执行会话，session_id={session_id}")

    # 每个会话独立使用 output/session_{session_id}，避免不同用户的产物互相覆盖
    session_dir = project_root_path / "output" / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace(
        "\\", "/"
    )

    # 上传文件先落在 updated/session_{session_id}，执行前复制到本次 output 工作目录
    # 这样读文件工具和生成文件工具都只需要围绕同一个 session_dir 工作
    updated_dir_path = project_root_path / "updated" / f"session_{session_id}"
    updated_info_prompt = ""
    if updated_dir_path.exists():
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        if files:
            for filename in files:
                # copy2 会保留上传文件的修改时间、权限等元数据，便于后续排查文件来源
                shutil.copy2(updated_dir_path / filename, session_dir / filename)

            # 把上传文件列表注入用户消息，提醒模型先调用 read_file_content 获取附件内容
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"    - {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录和 WebSocket thread_id
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(session_id)

    try:
        # 上传文件复制完成后、任务执行前记录文件快照：结束时对比得出本次任务的产物。
        # 快照放在复制之后，用户上传的附件不会被误算成本次生成的文件；
        # 基线登记到进程内表，跨审批中断轮次延续
        _task_baselines[session_id] = _snapshot_session_files(session_dir)
        # 清掉同 thread_id 上一轮遗留的待审登记，避免误恢复旧审批
        _pending_approvals.pop(session_id, None)
        # 记录本任务的审批档位：恢复时路由到同一 agent 实例（图拓扑一致）
        task_mode = _resolve_approval_mode(approval_mode)
        _task_modes[session_id] = task_mode

        # 同一会话可能先后执行多次任务，每次启动都重置工具调用预算与去重记录
        reset_task_budget(session_id)
        # 来源登记表同步重置：本次任务收集到的外部来源只对本次报告生效
        reset_task_sources(session_id)

        # task_start 携带原始问题并最先落盘，回放时前端用它切分历史对话轮次
        monitor.report_task_start(task_query)

        # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
        monitor.report_session_dir(session_dir_str)

        # 工作环境指令是运行时动态补充的，约束模型只在当前会话目录读写文件
        path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

        agent_input = {
            "messages": [{"role": "user", "content": task_query + path_instruction}]
        }
        await _execute_agent_run(
            session_id,
            session_dir,
            agent_input,
            _build_agent_config(session_id),
            task_mode,
        )
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(session_dir_token, session_id_token)


async def resume_deep_agent(thread_id: str, decisions: list[dict[str, Any]]) -> None:
    """
    人工审批决策提交后，恢复被中断的任务

    与新任务的区别：不重置预算/来源/基线（它们在中断时被保留）、不复制
    上传文件、不补发 task_start；以 Command(resume) 驱动同一 thread_id 的
    检查点继续执行。若恢复后再次命中审批，会再次推送 approval_required。
    恢复实例按任务启动时记录的档位路由（图拓扑与中断时一致）；
    映射丢失（后端重启）时回落标准档实例——它带审批中间件，
    一定与产生中断的图结构兼容
    """
    print(f"[MainAgent] 审批决策已提交，恢复会话，thread_id={thread_id}")

    session_dir = project_root_path / "output" / f"session_{thread_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_dir_str = str(session_dir).replace("\\", "/")

    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(thread_id)

    try:
        monitor.report_approval_resumed(decisions)
        # 关闭档不会产生中断；防御性处理映射中的 off，恢复走标准档实例
        resume_mode = _task_modes.get(thread_id, "standard")
        if resume_mode == "off":
            resume_mode = "standard"
        await _execute_agent_run(
            thread_id,
            session_dir,
            Command(resume={"decisions": decisions}),
            _build_agent_config(thread_id),
            resume_mode,
        )
    finally:
        reset_session_context(session_dir_token, session_id_token)


if __name__ == "__main__":
    asyncio.run(
        run_deep_agent("从网络查询机器人信息，并生成Markdown文件", "test_session_001")
    )
