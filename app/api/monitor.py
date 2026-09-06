"""
Agent 执行过程监控模块

负责把工具调用、子智能体调用、任务结果和会话目录等事件统一包装后推送给前端
在 Web 服务中优先通过 WebSocket 定向推送；在脚本调试场景中保留控制台输出
"""

import asyncio
import builtins
import datetime
import json
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket

from app.api.context import get_thread_context

# 本文件位于 app/api/，parents[1] 即 app 目录；会话目录与 server.py/main_agent.py 一致收敛在 app/output
_project_root = Path(__file__).resolve().parents[1]
_output_dir = _project_root / "output"
_EVENTS_FILENAME = "events.jsonl"

# 事件序号：每个 thread 单调递增，随事件落盘与 WebSocket 一起下发。
# 前端据此对"断线重连后回放的历史事件"与"实时事件"去重，避免同一条事件被并入两次。
# 进程内首次遇到某 thread 时按 events.jsonl 现有行数初始化，
# 保证后端重启后新事件的序号仍大于重启前已落盘事件的序号。
_seq_lock = threading.Lock()
_seq_counters: dict[str, int] = {}


class ToolMonitor:
    """
    工具和助手调用的统一监控入口

    业务工具只需要导入全局 monitor，并调用 report_tool/report_assistant 等方法
    具体是通过 WebSocket 推送，还是输出到脚本运行时，由本类内部统一处理
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None
        return cls._instance

    def set_websocket_manager(self, manager: "ConnectionManager") -> None:
        """绑定 FastAPI WebSocket 连接管理器"""
        self.websocket_manager = manager

    def _emit(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
        persist: bool = True,
        console: bool = True,
    ) -> None:
        """
        构造统一监控事件，并尝试推送到当前 thread_id 对应的前端连接

        :param event_type: 事件类型，例如 tool_start、assistant_call
        :param message: 面向前端展示的事件说明
        :param data: 附加结构化数据
        :param persist: 是否落盘 events.jsonl。流式增量（task_delta）只走
            WebSocket 不落盘——回放恢复靠完整的 task_result，落盘 delta 只会
            把事件文件撑大数倍
        :param console: 是否输出控制台日志。高频事件（如流式增量）跳过，避免刷屏
        """
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }

        # 只落盘的事件才分配序号：序号必须与 events.jsonl 的行一一对应，
        # 否则重启后按行数初始化的基数会与历史序号错位
        if persist:
            try:
                thread_id = get_thread_context()
                if thread_id:
                    payload["seq"] = self._next_seq(thread_id)
            except Exception as e:
                print(f"[Monitor] 分配事件序号失败: {e}")

        if self.websocket_manager:
            try:
                thread_id = get_thread_context()
                manager_loop = self.websocket_manager.loop

                if manager_loop and thread_id:
                    self._send_to_websocket(payload, thread_id, manager_loop)
            except Exception as e:
                print(f"[Monitor] WebSocket send failed: {e}")

        # 事件落盘：追加到 output/session_{thread_id}/events.jsonl，
        # 前端重启后通过回放接口恢复历史对话；会话目录由 run_deep_agent 先行创建
        if persist:
            try:
                persist_thread_id = get_thread_context()
                if persist_thread_id:
                    events_file = (
                        _output_dir / f"session_{persist_thread_id}" / _EVENTS_FILENAME
                    )
                    if events_file.parent.exists():
                        with events_file.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[Monitor] events.jsonl 写入失败: {e}")

        # DeepAgents 脚本调试时，如果运行时暴露了 stream_writer，也同步写入流式输出
        if hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 控制台保底输出，便于无前端场景下观察执行过程
        if console:
            print(f"\n[Monitor:{event_type}] {message}")

    def _next_seq(self, thread_id: str) -> int:
        """
        取得指定会话的下一个事件序号

        首次遇到该 thread 时以 events.jsonl 现有行数为基数，覆盖"后端重启后
        继续向旧会话追加事件"的场景：新序号必须大于重启前任何已落盘序号
        """
        with _seq_lock:
            if thread_id not in _seq_counters:
                base = 0
                events_file = _output_dir / f"session_{thread_id}" / _EVENTS_FILENAME
                if events_file.exists():
                    try:
                        with events_file.open("rb") as f:
                            base = sum(1 for _ in f)
                    except OSError:
                        base = 0
                _seq_counters[thread_id] = base
            _seq_counters[thread_id] += 1
            return _seq_counters[thread_id]

    def _send_to_websocket(
        self,
        payload: dict[str, Any],
        thread_id: str,
        manager_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        将监控事件投递到 WebSocket 所在事件循环

        FastAPI 的 WebSocket 必须在创建它的事件循环中发送消息
        如果当前代码已经在同一个循环里，直接 create_task；否则使用线程安全投递
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        coroutine = self.websocket_manager.send_to_thread(payload, thread_id)
        if current_loop and current_loop == manager_loop:
            current_loop.create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, manager_loop)

    def report_task_start(self, query: str) -> None:
        """报告一次研究任务开始，携带原始问题，前端据此切分对话轮次"""
        self._emit("task_start", f"研究任务已启动: {query}", {"query": query})

    def report_tool(
        self,
        tool_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告开始执行某个工具"""
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args},
        )

    def report_tool_end(
        self,
        tool_name: str,
        duration_ms: int,
        status: str,
        retries: int = 0,
        summary: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        """
        报告某个工具执行结束

        与 tool_start 成对出现，构成轻量 Trace：status 区分 success / failed /
        blocked（被预算或去重拦截），duration_ms 记录含重试的总耗时。
        截断后的结果摘要直接拼进 message，前端只渲染 message 文本也能看到
        工具返回了什么；长度由 call_guard 的 _summarize 统一控制，不会撑爆展示
        """
        message = f"工具执行结束: {tool_name}（{status}，{duration_ms}ms）"
        if summary:
            label = {"failed": "失败原因", "blocked": "拦截原因"}.get(status, "结果")
            message += f"｜{label}：{summary}"
        self._emit(
            "tool_end",
            message,
            {
                "tool_name": tool_name,
                "duration_ms": duration_ms,
                "status": status,
                "retries": retries,
                "summary": summary,
                "error_type": error_type,
            },
        )

    def report_token_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        """报告一次模型调用的 Token 用量，供成本观测和预算控制使用"""
        self._emit(
            "token_usage",
            f"Token 用量：输入 {input_tokens} / 输出 {output_tokens} / 合计 {total_tokens}",
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        )

    def report_budget_exceeded(self, tool_name: str, reason: str) -> None:
        """报告调用预算或重复检测被触发，对应代码级流程约束生效的事件"""
        self._emit(
            "budget_exceeded",
            f"调用预算限制触发: {tool_name}",
            {"tool_name": tool_name, "reason": reason},
        )

    def report_assistant(
        self,
        assistant_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告正在调用某个子智能体"""
        self._emit(
            "assistant_call",
            f"正在调用助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
        )

    def report_task_result(self, result: str) -> None:
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_task_delta(self, delta: str) -> None:
        """
        报告一段流式生成的回答增量文本

        只推 WebSocket、不落盘、不打印控制台：回放恢复靠完整 task_result，
        delta 落盘只会把事件文件撑大数倍；高频调用也不应刷屏
        """
        self._emit(
            "task_delta",
            "回答生成中",
            {"delta": delta},
            persist=False,
            console=False,
        )

    def report_task_cancelled(self) -> None:
        """报告任务已被用户取消"""
        self._emit("task_cancelled", "任务已取消")

    def report_session_dir(self, path: str) -> None:
        """报告当前任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})

    def report_task_files(self, files: list[dict[str, Any]]) -> None:
        """
        报告本次任务新生成或修改的文件清单

        文件对象结构与 /api/files 返回一致（name/path/size/mtime），
        前端据此把每个对话轮次的产物列表限定为"本轮生成"，不再累积历史文件
        """
        self._emit(
            "task_files",
            f"本次任务共生成 {len(files)} 个文件",
            {"files": files},
        )

    def report_task_sources(self, sources: dict[str, Any]) -> None:
        """
        报告本次任务实际收集到的来源清单（溯源信息）

        数据来自 source_registry 登记的原始来源（不经过模型转述），结构与
        get_task_sources 一致：{"web": [{"title","url"}], "docs": [{"doc","page"}],
        "sql": [str]}。前端据此在最终回答下方渲染"参考来源"区块；
        事件随 _emit 落盘 events.jsonl，历史回放时同样可恢复
        """
        web_count = len(sources.get("web") or [])
        doc_count = len(sources.get("docs") or [])
        sql_count = len(sources.get("sql") or [])
        self._emit(
            "task_sources",
            f"本次任务共引用 {web_count + doc_count + sql_count} 个来源"
            f"（网络 {web_count} / 文档 {doc_count} / 数据库 {sql_count}）",
            {
                "web": sources.get("web") or [],
                "docs": sources.get("docs") or [],
                "sql": sources.get("sql") or [],
            },
        )


monitor = ToolMonitor()


class ConnectionManager:
    """
    WebSocket 连接管理器

    active_connections 使用 thread_id 作为 key，保证监控事件只推送给对应任务的前端连接
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        # WebSocket 发送必须回到创建连接的事件循环，因此启动时需要显式绑定 loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定 FastAPI 主事件循环，并同步注册到 monitor"""
        self.loop = loop
        monitor.set_websocket_manager(self)
        print(f"[Monitor] ConnectionManager manually bound to loop: {id(self.loop)}")

    async def connect(self, websocket: WebSocket, thread_id: str) -> None:
        """接受 WebSocket 连接，并按 thread_id 保存"""
        await websocket.accept()
        self.active_connections[thread_id] = websocket
        print(f"Client connected: {thread_id}")

    def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """移除已经断开的 WebSocket 连接"""
        if self.active_connections.get(thread_id) is websocket:
            del self.active_connections[thread_id]
            print(f"Client disconnected: {thread_id}")
        else:
            print(f"Stale websocket disconnected, current connection kept: {thread_id}")

    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """向指定 WebSocket 发送纯文本消息"""
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict[str, Any], thread_id: str) -> None:
        """向指定 thread_id 对应的前端连接发送 JSON 消息"""
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()
