"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为后续 API 层调用的统一入口。运行时还会为每个
session_id 创建独立工作目录，并把工具调用、子智能体调用和最终结果推送给前端。
"""

import asyncio
import os
import shutil
from pathlib import Path

from deepagents import create_deep_agent
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.budget import reset_task_budget
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.api.monitor import monitor
from app.api.source_registry import get_task_sources, reset_task_sources
from app.agent.usage_callback import TokenUsageCallbackHandler

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content

# 主智能体是调度中心：
# 1. tools 只放最终交付相关的文件工具
# 2. subagents 放网络、数据库、RAGFlow 三类信息获取助手
# 3. checkpointer 通过 thread_id 保存同一会话中的执行上下文
main_agent = create_deep_agent(
    model=model,
    system_prompt=main_agent_content["system_prompt"],
    tools=[generate_markdown, convert_md_to_pdf, read_file_content],
    checkpointer=InMemorySaver(),
    subagents=[database_query_agent, network_search_agent, knowledge_base_agent],
)

# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 整图最大执行步数（模型/工具轮次），是防止无限循环的最后保险。
# 默认 80：多助手调度 + 长报告生成的正常任务约需 30-50 步，80 留足余量；
# 失控循环已被工具级预算（次数上限 + 去重）先行拦截，这里只兜最底。
# 可通过环境变量 AGENT_RECURSION_LIMIT 按任务复杂度调整
AGENT_RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "80"))

# 会话目录中的事件日志由 monitor 持续追加，不属于任务"生成"的产物
_EVENTS_FILENAME = "events.jsonl"


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


async def run_deep_agent(task_query, session_id):
    """
    异步流式执行主智能体

    API 层会为每次任务传入用户问题和 session_id。本函数负责准备会话目录、
    复制上传文件、写入 ContextVar，并在流式执行过程中把关键事件上报给前端。
    :param task_query: 前端提交的原始任务问题
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

    # 上传文件复制完成后、任务执行前记录文件快照：结束时对比得出本次任务的产物。
    # 快照放在复制之后，用户上传的附件不会被误算成本次生成的文件
    baseline_files = _snapshot_session_files(session_dir)

    # 同一会话可能先后执行多次任务，每次启动都重置工具调用预算与去重记录
    reset_task_budget(session_id)
    # 来源登记表同步重置：本次任务收集到的外部来源只对本次报告生效
    reset_task_sources(session_id)

    # task_start 携带原始问题并最先落盘，回放时前端用它切分历史对话轮次
    monitor.report_task_start(task_query)

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    # checkpointer 依赖 thread_id 区分会话记忆；同一 session_id 会复用同一条执行上下文
    config = {
        "configurable": {"thread_id": session_id},
        # 步数上限是防止无限循环的最后保险，即使模型无视提示词也会被硬性拦下
        "recursion_limit": AGENT_RECURSION_LIMIT,
        # 回调随 RunnableConfig 穿透到子智能体，Token 用量覆盖主+子全部模型调用
        "callbacks": [TokenUsageCallbackHandler()],
    }

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

    try:
        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        async for chunk in main_agent.astream(
            {"messages": [{"role": "user", "content": task_query + path_instruction}]},
            config=config,
        ):
            # chunk 形如 {"model": {"messages": [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
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
                                        {
                                            "description": tool_call["args"][
                                                "description"
                                            ]
                                        },
                                    )
                        elif last_msg.content:
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            print(
                                f"主智能体执行结果，最终结果：{last_msg.content[:100]}"
                            )
                            monitor.report_task_result(last_msg.content)

    except asyncio.CancelledError:
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
        # 无论正常完成、取消还是异常，都把本次任务实际产生的文件清单推给前端，
        # 每个对话轮次的产物列表据此只展示本轮生成的文件
        try:
            task_files = _diff_task_files(session_dir, baseline_files)
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
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(session_dir_token, session_id_token)


if __name__ == "__main__":
    import asyncio

    asyncio.run(
        run_deep_agent("从网络查询机器人信息，并生成Markdown文件", "test_session_001")
    )
