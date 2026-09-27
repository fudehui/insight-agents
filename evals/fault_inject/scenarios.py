"""
故障注入演练场景（P0-1 W3：断网 / 审批中重启；W4：WS 断连 / 任务执行中重启）

量化口径（docs/evaluation-metric-template.md"故障注入演练指标"小节）：
- 事件补齐率 event_completion_rate：演练结束后 events.jsonl 的 seq 连续性——
  无缺口且无重复即 1.0；有缺口按 max(seq)+1 分之实际条数计。对应升级计划
  的"量化事件补齐率"（复用系统的断线回放/事件落盘机制，落盘即视为可补齐）；
- 恢复成功率 recovery_success_rate：注入故障后任务仍按预期收尾（断网：任务
  completed 且重试真实发生；审批中重启：新进程 resume 后 task_result 产出；
  WS 断连：重连对账结果与落盘全量一致且断连窗口补齐率 1.0；任务执行中
  重启：强杀后新进程从 checkpoint 续跑产出 task_result）；
- 恢复耗时 recovery_time_s：断网 = 失败事件到下一次同工具成功事件的时间差；
  审批中重启 = stage2 内 approval_resumed→task_result 的时间差；
  WS 断连 = 重连对账接口调用耗时（毫秒级，真实口径）；
  任务执行中重启 = 恢复进程首事件→task_result 的时间差。

隔离纪律（升级计划第七节）：演练不碰真实业务库/checkpoints.db——checkpoint
库路径在 worker 进程内 patch 到演练 workdir（零 app/ 改动）；会话目录用
workdir 下的独立目录；默认 mock-llm，全程零 token。
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CHAOS_QUERY = "演练任务：检索并生成一份带引用的报告"

TASK_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# 事件与指标工具
# ---------------------------------------------------------------------------


def read_events(session_dir: Path) -> list[dict[str, Any]]:
    """读取演练会话的 events.jsonl（解析失败行跳过）"""
    events_file = session_dir / "events.jsonl"
    if not events_file.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in events_file.open("r", encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def event_completion_rate(events: list[dict[str, Any]]) -> float:
    """
    事件补齐率：seq 连续无缺口无重复 → 1.0；有缺口按 (max-min+1) 分之去重条数计

    monitor 事件带单调递增 seq（实测从 1 起）；无任何带 seq 的事件时返回 0.0
    """
    seqs = [int(e["seq"]) for e in events if isinstance(e.get("seq"), int)]
    if not seqs:
        return 0.0
    span = max(seqs) - min(seqs) + 1
    return round(len(set(seqs)) / span, 4)


def _ts(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def tool_recovery_time(events: list[dict[str, Any]], tool_hint: str) -> float | None:
    """
    断网恢复耗时：注入点（工具失败事件）到该工具下一次成功事件的时间差

    :param tool_hint: 工具名子串（如 "internet_search"）
    :return: 秒；未找到失败-成功对时 None
    """
    fail_at: datetime | None = None
    for event in events:
        data = event.get("data") or {}
        name = str(data.get("tool_name") or "")
        if tool_hint not in name:
            continue
        if event.get("event") == "tool_end" and "failed" in str(event.get("message") or ""):
            fail_at = _ts(event)
        elif fail_at is not None and event.get("event") == "tool_end":
            ok_at = _ts(event)
            if ok_at:
                return round((ok_at - fail_at).total_seconds(), 3)
    return None


def _task_span_seconds(events: list[dict[str, Any]]) -> float | None:
    """task_start→task_result 的墙钟跨度（恢复进程的实际恢复耗时）"""
    starts = [_ts(e) for e in events if e.get("event") == "task_start"]
    ends = [_ts(e) for e in events if e.get("event") == "task_result"]
    starts = [t for t in starts if t]
    ends = [t for t in ends if t]
    if not starts or not ends:
        return None
    return round((max(ends) - max(starts)).total_seconds(), 3)


def reconnect_backfill_rate(
    disk_events: list[dict[str, Any]],
    replayed_events: list[dict[str, Any]],
    cutoff_seq: int,
) -> float | None:
    """
    断连窗口补齐率：断连期间（seq > cutoff_seq）落盘的事件中，重连对账可取回的比例

    窗口内没有任何落盘事件时返回 None（口径：无窗口则补齐率不可判定）；
    只带 WS 不落盘的事件（如 task_delta）没有 seq，天然不参与统计
    """
    window = sorted(
        {
            int(e["seq"])
            for e in disk_events
            if isinstance(e.get("seq"), int) and int(e["seq"]) > cutoff_seq
        }
    )
    if not window:
        return None
    replayed_seqs = {
        e["seq"] for e in replayed_events if isinstance(e.get("seq"), int)
    }
    hit = sum(1 for s in window if s in replayed_seqs)
    return round(hit / len(window), 4)


def resume_span_of(events: list[dict[str, Any]], base_seq: int) -> float | None:
    """
    续跑恢复耗时：恢复进程首事件（seq > base_seq）→ task_result 的墙钟跨度

    任务执行中被杀的场景没有 approval_resumed / task_start 可作起点，恢复
    耗时只能从恢复进程自己产出的第一条事件算起（按 seq 与 stage1 末序号
    切分两段），避免把进程死亡间隔计进去（同 restart 场景的 W3 教训）。
    恢复进程未产出任何事件或未产出 task_result 时返回 None
    """
    def _after_base(e: dict[str, Any]) -> bool:
        return isinstance(e.get("seq"), int) and int(e["seq"]) > base_seq

    starts = [t for e in events if _after_base(e) for t in [_ts(e)] if t]
    ends = [
        t
        for e in events
        if _after_base(e) and e.get("event") == "task_result"
        for t in [_ts(e)]
        if t
    ]
    if not starts or not ends:
        return None
    return round((min(ends) - min(starts)).total_seconds(), 3)


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    汇总多次演练：恢复成功率（最近邻秩 P95 与 run_eval 口径一致）、事件补齐率均值
    """
    n = len(runs)
    successes = sum(1 for r in runs if r.get("recovery_success"))
    times = [float(r["recovery_time_s"]) for r in runs if r.get("recovery_time_s") is not None]
    rates = [float(r["event_completion_rate"]) for r in runs if r.get("event_completion_rate") is not None]
    p95 = 0.0
    if times:
        ordered = sorted(times)
        p95 = ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]
    return {
        "runs": n,
        "recovery_success_rate": round(successes / n, 4) if n else 0.0,
        "recovery_time_avg_s": round(sum(times) / len(times), 3) if times else None,
        "recovery_time_p95_s": p95,
        "event_completion_rate_avg": round(sum(rates) / len(rates), 4) if rates else None,
    }


# ---------------------------------------------------------------------------
# 场景一：断网（工具层网络故障 + call_guard 重试恢复），进程内执行
# ---------------------------------------------------------------------------


async def run_disconnect_once(workdir: Path, run_index: int, timeout_s: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    """
    单次断网演练：fake Tavily 第一次调用抛 ConnectionError，call_guard 重试后恢复

    任务应 completed（恢复成功）；事件流应无缺口（补齐率 1.0）
    """
    from app.api.budget import reset_task_budget
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from app.api import source_registry
    from app.agent.main_agent import run_deep_agent

    from evals.fault_inject.mock_llm import install_mock_llm

    session_id = f"chaos_disconnect_run{run_index}"
    session_dir = _session_dir_of(workdir, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(session_id)
    source_registry.reset_task_sources(session_id)
    reset_task_budget(session_id)

    # 注入：检索第 1 次断网，第 2 次恢复（call_guard max_retries 内）
    fake_model, fake_tavily = install_mock_llm(fail_first_n=1)

    started_at = datetime.now().isoformat()
    start = time.perf_counter()
    timed_out = False
    coroutine_error: str | None = None
    try:
        await asyncio.wait_for(
            run_deep_agent(task_query=CHAOS_QUERY, session_id=session_id, approval_mode="off"),
            timeout=timeout_s,
        )
    except TimeoutError:
        timed_out = True
    except Exception as e:  # noqa: BLE001 演练需记录任意失败形态
        coroutine_error = f"{type(e).__name__}: {e}"
    duration_s = round(time.perf_counter() - start, 2)

    reset_session_context(dir_token, thread_token)

    events = read_events(session_dir)
    event_types = {str(e.get("event") or "") for e in events}
    completed = "task_result" in event_types and not timed_out
    retry_happened = fake_tavily.search_calls >= 2
    record = {
        "run": run_index,
        "scenario": "disconnect",
        "status": "completed" if completed else ("timeout" if timed_out else "failed"),
        "recovery_success": completed and retry_happened,
        "recovery_time_s": tool_recovery_time(events, "internet_search"),
        "event_completion_rate": event_completion_rate(events),
        "tavily_search_calls": fake_tavily.search_calls,
        "duration_s": duration_s,
        "error": coroutine_error or (f"任务超时（>{timeout_s:.0f}s）" if timed_out else None),
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "events_count": len(events),
    }
    out = workdir / f"run_{run_index}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def run_disconnect_scenario(runs: int, workdir: Path, timeout_s: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    """
    断网场景 N 次。多次演练在同一事件循环内串行 await（AsyncSqliteSaver 连接
    绑定首个事件循环，与 run_eval 同一约束）；checkpoint 库路径在 import 后、
    首次初始化前 patch 到演练 workdir（隔离纪律）
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _isolate_checkpoint(workdir)

    async def _all():
        return [await run_disconnect_once(workdir, i, timeout_s) for i in range(1, runs + 1)]

    records = asyncio.run(_all())
    _close_checkpoint()
    summary = {"scenario": "disconnect", "mock_llm": True, **aggregate(records)}
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


# ---------------------------------------------------------------------------
# 场景三：WS 断连（客户端断开 WebSocket，重连后经对账接口补齐事件），进程内执行
# ---------------------------------------------------------------------------


async def run_ws_disconnect_once(
    workdir: Path, run_index: int, timeout_s: float = TASK_TIMEOUT_S
) -> dict[str, Any]:
    """
    单次 WS 断连演练（进程内，语义口径）：

    任务执行期间"客户端"断开 WebSocket 连接——服务端任务不受影响继续跑、
    事件照常落盘（进程内无前端连接时 monitor 本就只落盘不投递，天然成立，
    真实 WS 层的断连处理由 server.py 的连接管理兜底，不在本场景覆盖内）；
    任务结束后"重连"，通过对账接口 GET /api/sessions/{thread_id}/events
    补齐断连窗口内的事件并核对一致性。

    取证结论（W4 实测）：get_session_events 路由函数签名仅 thread_id，
    读模块级常量 output_dir（server.py:128），不依赖 FastAPI 请求上下文，
    patch 指向演练 output 根后可进程内直调（零服务起停）；断连点取
    task_start 后第 2 条（客户端已消费前 3 条）。
    """
    from app.api import server as server_module  # 延迟导入：仅本场景需要对账数据源
    from app.api.budget import reset_task_budget
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from app.api import source_registry
    from app.agent.main_agent import run_deep_agent

    from evals.fault_inject.mock_llm import install_mock_llm

    session_id = f"chaos_ws_run{run_index}"
    session_dir = _session_dir_of(workdir, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)

    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(session_id)
    source_registry.reset_task_sources(session_id)
    reset_task_budget(session_id)

    # 注入口径：故障不在任务侧——mock 全程无失败，任务照常跑完（= 服务端
    # 不受客户端断连影响）；"断连"只表现为客户端自断连点起不再消费事件
    install_mock_llm()

    started_at = datetime.now().isoformat()
    start = time.perf_counter()
    timed_out = False
    task_error: str | None = None
    try:
        await asyncio.wait_for(
            run_deep_agent(task_query=CHAOS_QUERY, session_id=session_id, approval_mode="off"),
            timeout=timeout_s,
        )
    except TimeoutError:
        timed_out = True
    except Exception as e:  # noqa: BLE001 演练需记录任意失败形态
        task_error = f"{type(e).__name__}: {e}"
    duration_s = round(time.perf_counter() - start, 2)

    reset_session_context(dir_token, thread_token)

    # 落盘全量 = 服务端视角的权威事件流；error 字段只记录诊断（收尾阶段的
    # 无关异常不否定已完成的任务与对账结果），恢复判定以事件流为准
    disk_events = read_events(session_dir)
    event_types = {str(e.get("event") or "") for e in disk_events}
    completed = "task_result" in event_types and not timed_out

    # 断连点：task_start 后第 2 条（客户端最后消费到 seq <= cutoff 的事件）
    task_start_seq = next(
        (
            int(e["seq"])
            for e in disk_events
            if e.get("event") == "task_start" and isinstance(e.get("seq"), int)
        ),
        None,
    )

    # 重连对账：直调历史回放路由函数，并测量真实对账耗时
    reconciliation_s: float | None = None
    replayed: list[dict[str, Any]] = []
    reconcile_error: str | None = None
    if task_start_seq is not None:
        saved_output_dir = server_module.output_dir
        server_module.output_dir = workdir / "output"
        try:
            start = time.perf_counter()
            result = await server_module.get_session_events(session_id)
            reconciliation_s = round(time.perf_counter() - start, 6)
            replayed = list(result.get("events") or [])
        except Exception as e:  # noqa: BLE001 对账失败也要留痕
            reconcile_error = f"{type(e).__name__}: {e}"
        finally:
            server_module.output_dir = saved_output_dir  # 还原数据源，不污染其他测试

    cutoff_seq = (task_start_seq + 2) if task_start_seq is not None else None
    window = (
        [
            e["seq"]
            for e in disk_events
            if isinstance(e.get("seq"), int) and e["seq"] > cutoff_seq
        ]
        if cutoff_seq is not None
        else []
    )
    backfill_rate = (
        reconnect_backfill_rate(disk_events, replayed, cutoff_seq=cutoff_seq)
        if cutoff_seq is not None
        else None
    )
    replay_consistent = bool(replayed) and replayed == disk_events

    record = {
        "run": run_index,
        "scenario": "ws_disconnect",
        "status": "completed" if completed else ("timeout" if timed_out else "failed"),
        "recovery_success": completed
        and backfill_rate == 1.0
        and replay_consistent
        and reconciliation_s is not None,
        "recovery_time_s": reconciliation_s,
        "event_completion_rate": event_completion_rate(disk_events),
        "cutoff_seq": cutoff_seq,
        "window_events": len(window),
        "replayed_events": len(replayed),
        "backfill_rate": backfill_rate,
        "replay_consistent": replay_consistent,
        "duration_s": duration_s,
        "error": task_error or reconcile_error,
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "events_count": len(disk_events),
    }
    out = workdir / f"run_{run_index}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def run_ws_disconnect_scenario(runs: int, workdir: Path, timeout_s: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    """
    WS 断连场景 N 次（进程内，串行约束与断网场景一致）
    """
    workdir.mkdir(parents=True, exist_ok=True)
    _isolate_checkpoint(workdir)

    async def _all():
        return [await run_ws_disconnect_once(workdir, i, timeout_s) for i in range(1, runs + 1)]

    records = asyncio.run(_all())
    _close_checkpoint()
    summary = {"scenario": "ws_disconnect", "mock_llm": True, **aggregate(records)}
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _isolate_checkpoint(workdir: Path, keep_checkpoint: bool = False) -> None:
    """
    演练环境隔离（必须在 run_deep_agent 首次执行前调用）：

    - main_agent.project_root_path：run_deep_agent / resume_deep_agent 的
      会话目录根（硬编码 app/output）——patch 后演练会话落在 workdir/output；
    - monitor._output_dir：monitor 的事件落盘根是**独立模块常量**（monitor.py:22），
      不随 main_agent 的 patch 变——必须一并 patch，否则演练事件写入真实
      app/output/session_*（W3 实测踩坑：事件"消失"实为写去了真实目录）；
    - _checkpoint_db_path 指到 workdir/checkpoints.db，并删除上次演练残留
      （同 thread_id 复跑会从旧 checkpoint 半程恢复，导致任务瞬间"完成"）；
      keep_checkpoint=True 时保留库文件（任务执行中重启场景的恢复进程
      必须带着 stage1 的半程 checkpoint 续跑，清库即等于丢记忆）；
    - 重置 _checkpointer/_agents 缓存：aiosqlite 连接绑定创建它的事件循环，
      且旧图实例持有按值导入的 model 引用，重置才能让 mock 与新库生效。
    """
    import app.agent.main_agent as main_agent
    from app.api import monitor

    main_agent.project_root_path = workdir
    main_agent._checkpoint_db_path = workdir / "checkpoints.db"
    main_agent._checkpointer = None
    main_agent._agents = {}
    monitor._output_dir = workdir / "output"
    if keep_checkpoint:
        return
    for stale in workdir.glob("checkpoints.db*"):
        stale.unlink(missing_ok=True)


def _session_dir_of(workdir: Path, thread_id: str) -> Path:
    """演练会话目录（与 patch 后 run_deep_agent 的规则一致）"""
    return workdir / "output" / f"session_{thread_id}"


def _close_checkpoint() -> None:
    """
    显式关闭 AsyncSqliteSaver 的 aiosqlite 连接

    aiosqlite 的工作线程是非 daemon（core.py:90），连接不关闭会挂住
    pytest/编排进程的退出（W3 实测：演练完成后 pytest 一直不结束）。
    close 经其内部命令队列由后台线程执行，与当前事件循环无关
    """
    import asyncio

    import app.agent.main_agent as main_agent

    checkpointer = main_agent._checkpointer
    if checkpointer is None:
        return
    try:
        asyncio.run(checkpointer.conn.close())
    except Exception as e:  # noqa: BLE001 关闭失败不影响演练结论
        print(f"[chaos] checkpoint 连接关闭失败（忽略）：{e}")
    main_agent._checkpointer = None
    main_agent._agents = {}


# ---------------------------------------------------------------------------
# 场景二：审批中重启（两段子进程真实模拟进程死亡与恢复）
# ---------------------------------------------------------------------------


def run_restart_scenario(runs: int, workdir: Path, timeout_s: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    """
    审批中重启场景 N 次，每次两个子进程：

    stage1（任务进程）：standard 档跑 run_deep_agent → generate_markdown 前中断
    （approval_required 事件推送）→ 不恢复、直接退出（= 进程死亡）；
    stage2（恢复进程）：新进程对同一 thread_id resume_deep_agent 提交 approve
    → 任务从 checkpoint 继续 → task_result 产出（= 恢复成功）。

    恢复耗时取 stage2 内 task_start→task_result 的事件跨度。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    # __file__ = evals/fault_inject/scenarios.py：parents[2] 才是项目根
    worker = Path(__file__).resolve().parents[2] / "evals" / "fault_inject" / "fault_chaos.py"
    records: list[dict[str, Any]] = []
    for i in range(1, runs + 1):
        run_dir = workdir / f"run_{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        thread_id = f"chaos_restart_run{i}"

        stage1_json, stage2_json = run_dir / "stage1.json", run_dir / "stage2.json"
        for stage_flag, out in (("--stage1", stage1_json), ("--stage2", stage2_json)):
            cmd = [
                sys.executable,
                str(worker),
                stage_flag,
                "--thread-id",
                thread_id,
                "--workdir",
                str(workdir),
                "--out",
                str(out),
                "--timeout",
                str(timeout_s),
            ]
            log = run_dir / ("stage1.log" if stage_flag == "--stage1" else "stage2.log")
            try:
                with log.open("w", encoding="utf-8") as lf:
                    proc = subprocess.run(
                        cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout_s + 120, check=False
                    )
                print(f"[chaos] run{i} {stage_flag} exit={proc.returncode}")
            except subprocess.TimeoutExpired:
                print(f"[chaos] run{i} {stage_flag} 硬超时被终止")

        stage1 = json.loads(stage1_json.read_text(encoding="utf-8")) if stage1_json.exists() else {}
        stage2 = json.loads(stage2_json.read_text(encoding="utf-8")) if stage2_json.exists() else {}
        interrupted = bool(stage1.get("interrupted"))
        recovered = bool(stage2.get("recovered"))
        events = read_events(_session_dir_of(workdir, thread_id))
        record = {
            "run": i,
            "scenario": "restart_mid_approval",
            "status": "recovered" if recovered else "failed",
            "recovery_success": interrupted and recovered,
            "recovery_time_s": stage2.get("task_span_s"),
            "event_completion_rate": event_completion_rate(events),
            "interrupted": interrupted,
            "stage1_error": stage1.get("error"),
            "stage2_error": stage2.get("error"),
            "started_at": stage1.get("started_at"),
            "finished_at": stage2.get("finished_at"),
            "events_count": len(events),
        }
        (run_dir / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        records.append(record)

    summary = {"scenario": "restart_mid_approval", "mock_llm": True, **aggregate(records)}
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


# ---------------------------------------------------------------------------
# 场景四：任务执行中重启（进程死亡于普通执行点，新进程 None input 续跑恢复）
# ---------------------------------------------------------------------------

# stage3 强杀触发点：子智能体派发事件（模型节点已完成、子智能体节点在执行）
KILL_TRIGGER_EVENT = "assistant_call"
# 触发后延时：等事件落盘与 checkpoint 提交的竞态落定再杀（模拟"可控死亡点"，
# 避免杀在 sqlite 写事务中间；sqlite 事务原子性 + 下次打开自动恢复兜底半写）
KILL_DELAY_S = 0.15
# worker 内对 fake Tavily 的运行时拉窗（不改 mock_llm）：mock 任务全程不足
# 0.5s，不拉长检索窗口的话击发竞态下可能杀在任务完成之后
SEARCH_SLEEP_S = 0.5


def run_process_restart_scenario(runs: int, workdir: Path, timeout_s: float = TASK_TIMEOUT_S) -> dict[str, Any]:
    """
    任务执行中重启场景 N 次，每次两个子进程（编排结构同 restart_mid_approval）：

    stage3（任务进程）：off 档跑 run_deep_agent（无审批中断点），事件流出现
        KILL_TRIGGER_EVENT 后短延时 os._exit(1) 强杀 = 任务执行中进程死亡，
        checkpoint 停在最后一个已完成超步；击发路径不写 out json，恢复进程
        由编排方按"触发点已出现且 task_result 未产出"的事件证据判定击发；
    stage4（恢复进程）：新进程对同一 thread_id 以 input=None 驱动同一张图——
        LangGraph 从 checkpoint 重放未完成超步（被杀时在执行的节点重新执行，
        重复的检索调用被去重闸门拦截，不破坏收敛），跑到 task_result 收敛。

    恢复耗时 = 恢复进程首事件（seq > stage1 末序号）→ task_result 的时间差。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    # __file__ = evals/fault_inject/scenarios.py：parents[2] 才是项目根
    worker = Path(__file__).resolve().parents[2] / "evals" / "fault_inject" / "fault_chaos.py"
    records: list[dict[str, Any]] = []
    for i in range(1, runs + 1):
        run_dir = workdir / f"run_{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        thread_id = f"chaos_proc_restart_run{i}"
        started_at = datetime.now().isoformat()

        stage3_json, stage4_json = run_dir / "stage3.json", run_dir / "stage4.json"
        stage3_log, stage4_log = run_dir / "stage3.log", run_dir / "stage4.log"

        # stage3：强杀。击发路径经 os._exit(1) 离开进程，stage3.json 不会写出
        # （returncode==1 且无 json = 设计内信号；写出 json = 未按设计击发）
        stage3_killed = False
        stage3_record: dict[str, Any] = {}
        try:
            with stage3_log.open("w", encoding="utf-8") as lf:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(worker),
                        "--stage3",
                        "--thread-id",
                        thread_id,
                        "--workdir",
                        str(workdir),
                        "--out",
                        str(stage3_json),
                        "--timeout",
                        str(timeout_s),
                    ],
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_s + 120,
                    check=False,
                )
            print(f"[chaos] run{i} --stage3 exit={proc.returncode}")
            stage3_killed = proc.returncode == 1 and not stage3_json.exists()
        except subprocess.TimeoutExpired:
            print(f"[chaos] run{i} --stage3 硬超时被终止")
        if stage3_json.exists():
            stage3_record = json.loads(stage3_json.read_text(encoding="utf-8"))

        # 击发证据以事件流为准：触发点已出现且任务尚未产出结果
        events_after_kill = read_events(_session_dir_of(workdir, thread_id))
        killed_types = {str(e.get("event") or "") for e in events_after_kill}
        killed = (
            stage3_killed
            and KILL_TRIGGER_EVENT in killed_types
            and "task_result" not in killed_types
        )
        stage1_last_seq = max(
            (int(e["seq"]) for e in events_after_kill if isinstance(e.get("seq"), int)),
            default=0,
        )

        # stage4：恢复进程（新进程带 stage1 半程 checkpoint 续跑）
        try:
            with stage4_log.open("w", encoding="utf-8") as lf:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(worker),
                        "--stage4",
                        "--thread-id",
                        thread_id,
                        "--workdir",
                        str(workdir),
                        "--out",
                        str(stage4_json),
                        "--timeout",
                        str(timeout_s),
                    ],
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_s + 120,
                    check=False,
                )
            print(f"[chaos] run{i} --stage4 exit={proc.returncode}")
        except subprocess.TimeoutExpired:
            print(f"[chaos] run{i} --stage4 硬超时被终止")
        stage4 = json.loads(stage4_json.read_text(encoding="utf-8")) if stage4_json.exists() else {}

        events = read_events(_session_dir_of(workdir, thread_id))
        final_types = {str(e.get("event") or "") for e in events}
        recovered = killed and "task_result" in final_types
        record = {
            "run": i,
            "scenario": "process_restart_mid_task",
            "status": "recovered" if recovered else "failed",
            "recovery_success": recovered,
            "recovery_time_s": stage4.get("task_span_s")
            or resume_span_of(events, stage1_last_seq),
            "event_completion_rate": event_completion_rate(events),
            "killed": killed,
            "stage1_last_seq": stage1_last_seq,
            "stage3_error": stage3_record.get("error"),
            "stage4_error": stage4.get("error"),
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(),
            "events_count": len(events),
        }
        (run_dir / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        records.append(record)

    summary = {
        "scenario": "process_restart_mid_task",
        "mock_llm": True,
        **aggregate(records),
    }
    (workdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
