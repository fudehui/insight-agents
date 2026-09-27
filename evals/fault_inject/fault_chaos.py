"""
故障注入演练 CLI（P0-1 W3-W4）

编排模式（默认）：
    python evals/fault_inject/fault_chaos.py --scenario disconnect --runs 10
    python evals/fault_inject/fault_chaos.py --scenario restart_mid_approval --runs 10
    python evals/fault_inject/fault_chaos.py --scenario ws_disconnect --runs 10
    python evals/fault_inject/fault_chaos.py --scenario process_restart_mid_task --runs 10
    每次演练的明细落 workdir/run_N.json（或 run_N/record.json），汇总落
    workdir/summary.json；默认 mock-llm 零 token，可加 --real-llm 走真模型
    （此时数字才计入简历口径，mock 结果标注为机制验证）。

worker 模式（内部，两段子进程场景）：
    fault_chaos.py --stage1 --thread-id T --workdir W --out O   # 跑到审批中断即退出
    fault_chaos.py --stage2 --thread-id T --workdir W --out O   # 新进程 resume 恢复
    fault_chaos.py --stage3 --thread-id T --workdir W --out O   # 任务执行中 os._exit 强杀
    fault_chaos.py --stage4 --thread-id T --workdir W --out O   # 新进程 None input 续跑
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# __file__ = evals/fault_inject/fault_chaos.py：parents[2] 才是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.fault_inject.scenarios import (  # noqa: E402
    CHAOS_QUERY,
    KILL_DELAY_S,
    KILL_TRIGGER_EVENT,
    SEARCH_SLEEP_S,
    TASK_TIMEOUT_S,
    _close_checkpoint,
    _isolate_checkpoint,
    _session_dir_of,
    read_events,
    resume_span_of,
    run_disconnect_scenario,
    run_process_restart_scenario,
    run_restart_scenario,
    run_ws_disconnect_scenario,
)


def _stage1(thread_id: str, workdir: Path, out: Path, timeout_s: float) -> int:
    """
    任务进程：standard 档跑到 generate_markdown 审批中断，不恢复直接退出

    中断后图协程返回（approval_required 已推送），进程退出即模拟任务执行中
    的进程死亡——checkpoint 与事件均已落盘，等待"重启后"的恢复进程
    """
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from app.agent.main_agent import run_deep_agent
    from evals.fault_inject.mock_llm import install_mock_llm

    started_at = datetime.now().isoformat()
    _isolate_checkpoint(workdir)
    fake_model, _ = install_mock_llm()
    session_dir = _session_dir_of(workdir, thread_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(thread_id)

    start = time.perf_counter()
    error: str | None = None
    try:
        asyncio.run(
            asyncio.wait_for(
                run_deep_agent(task_query=CHAOS_QUERY, session_id=thread_id, approval_mode="standard"),
                timeout=timeout_s,
            )
        )
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        reset_session_context(dir_token, thread_token)

    _close_checkpoint()

    # 中断判定：脚本推进到 generate_markdown（第 2 个模型调用）且任务未产出结果
    events = read_events(session_dir)
    event_types = {str(e.get("event") or "") for e in events}
    interrupted = "approval_required" in event_types and "task_result" not in event_types
    out.write_text(
        json.dumps(
            {
                "interrupted": interrupted,
                "elapsed_s": round(time.perf_counter() - start, 2),
                "error": error,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(),
                "events_count": len(events),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if interrupted or error is None else 1


def _stage2(thread_id: str, workdir: Path, out: Path, timeout_s: float) -> int:
    """
    恢复进程：新进程内对同一 thread_id 提交 approve，任务应从 checkpoint 继续
    """
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from app.agent.main_agent import resume_deep_agent
    from evals.fault_inject.mock_llm import RESUME_SCRIPT, install_mock_llm

    started_at = datetime.now().isoformat()
    # 保留 stage1 落盘的半程 checkpoint：恢复语义必须建立在"同一 checkpoint
    # 库"上——清库后 resume 会在空线程上重新起跑（W4 复核发现的真实性问题）
    _isolate_checkpoint(workdir, keep_checkpoint=True)
    # 收敛脚本：resume 后 markdown 直接执行，模型下一轮返回完成文本即结束
    install_mock_llm(script=RESUME_SCRIPT)
    session_dir = _session_dir_of(workdir, thread_id)
    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(thread_id)

    start = time.perf_counter()
    error: str | None = None
    try:
        asyncio.run(
            asyncio.wait_for(
                resume_deep_agent(thread_id, [{"type": "approve"}]),
                timeout=timeout_s,
            )
        )
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        reset_session_context(dir_token, thread_token)

    _close_checkpoint()

    events = read_events(session_dir)
    task_span = _task_span_of(events, thread_id)
    recovered = any(e.get("event") == "task_result" for e in events)
    out.write_text(
        json.dumps(
            {
                "recovered": recovered and error is None,
                "task_span_s": task_span,
                "elapsed_s": round(time.perf_counter() - start, 2),
                "error": error,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(),
                "events_count": len(events),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if recovered else 1


def _stage3(thread_id: str, workdir: Path, out: Path, timeout_s: float) -> int:
    """
    任务进程：off 档任务（无审批中断点）跑到子智能体派发后强杀

    = 任务执行中进程死亡：checkpoint 停在最后一个已完成超步，子智能体节点
    执行到一半。击发点取 KILL_TRIGGER_EVENT（assistant_call）出现后延时
    KILL_DELAY_S——事件已落盘、checkpoint 提交竞态已落定、而 fake Tavily
    被 SEARCH_SLEEP_S 拉长的检索仍在执行中。

    击发路径经 os._exit(1) 离开进程：不写 out json、不回卷上下文、不关
    checkpoint 连接——这些正是"进程死亡"语义本身；sqlite 事务原子性兜底
    半写风险（hot journal/WAL 在下次打开时自动恢复），不使用 kill -9 制造
    不可控的写窗口。走到正常退出路径 = 未按设计击发（任务先完成或触发点
    缺失），如实落盘 killed=false 交编排方判定。
    """
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from app.agent.main_agent import run_deep_agent
    from evals.fault_inject.mock_llm import install_mock_llm

    started_at = datetime.now().isoformat()
    _isolate_checkpoint(workdir)
    _, fake_tavily = install_mock_llm()
    # 运行时拉长检索窗口（不改 mock_llm）：mock 任务全程不足 0.5s，不拉窗的话
    # 击发竞态下可能杀在任务完成之后，"执行中死亡"就失真了
    orig_search = fake_tavily.search

    def _slow_search(**kwargs):
        time.sleep(SEARCH_SLEEP_S)
        return orig_search(**kwargs)

    fake_tavily.search = _slow_search

    session_dir = _session_dir_of(workdir, thread_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(thread_id)

    async def _run_and_kill() -> None:
        task = asyncio.create_task(
            run_deep_agent(task_query=CHAOS_QUERY, session_id=thread_id, approval_mode="off")
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            events = read_events(session_dir)
            if any(e.get("event") == KILL_TRIGGER_EVENT for e in events):
                await asyncio.sleep(KILL_DELAY_S)
                os._exit(1)  # 击发：连同未完成协程与缓冲输出一起进程死亡
            await asyncio.sleep(0.02)
        raise TimeoutError(f"{timeout_s:.0f}s 内未等到 {KILL_TRIGGER_EVENT} 击发点")

    start = time.perf_counter()
    error: str | None = None
    try:
        asyncio.run(asyncio.wait_for(_run_and_kill(), timeout=timeout_s + 30))
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        reset_session_context(dir_token, thread_token)

    _close_checkpoint()

    events = read_events(session_dir)
    out.write_text(
        json.dumps(
            {
                "killed": False,
                "elapsed_s": round(time.perf_counter() - start, 2),
                "error": error,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(),
                "events_count": len(events),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 1


def _stage4(thread_id: str, workdir: Path, out: Path, timeout_s: float) -> int:
    """
    恢复进程：新进程对同一 thread_id 以 input=None 从 checkpoint 续跑

    取证结论（W4 实验脚本验证，langgraph 1.1.10）：LangGraph 对中途被杀
    （无 interrupt 挂起）的 checkpointed 线程支持 None input 续跑——图重放
    最后一个未完成超步，被杀时在执行的节点重新执行，重复的检索调用被去重
    闸门拦截（blocked，不破坏收敛），脚本 cycle=True 保证重放轮次自愈对齐。
    不走 run_deep_agent（那会作为新任务重置预算/来源/基线并重发 task_start），
    也不走 resume_deep_agent（Command(resume=) 只适用于 interrupt 挂起）；
    _execute_agent_run 是新任务与审批恢复共用的最小恢复驱动，这里直驱同一
    内部路径。恢复判定以事件流为准（error 只记录诊断——收尾阶段的无关异常
    不否定已产出的 task_result）。
    """
    from app.agent import main_agent
    from app.api.context import reset_session_context, set_session_context, set_thread_context
    from evals.fault_inject.mock_llm import install_mock_llm

    started_at = datetime.now().isoformat()
    # 关键差异（对照 _stage1/_stage2）：保留 stage1 的半程 checkpoint（keep_checkpoint）
    _isolate_checkpoint(workdir, keep_checkpoint=True)
    install_mock_llm()
    session_dir = _session_dir_of(workdir, thread_id)
    dir_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(thread_id)

    # 恢复耗时的起点 = 恢复进程首事件，按 stage1 已落盘的最大 seq 切分
    events_before = read_events(session_dir)
    base_seq = max(
        (int(e["seq"]) for e in events_before if isinstance(e.get("seq"), int)),
        default=0,
    )

    start = time.perf_counter()
    error: str | None = None
    try:
        asyncio.run(
            asyncio.wait_for(
                main_agent._execute_agent_run(
                    thread_id,
                    session_dir,
                    None,  # None input：从 checkpoint 续跑，不作为新任务重新提交
                    main_agent._build_agent_config(thread_id),
                    "off",
                ),
                timeout=timeout_s,
            )
        )
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
    finally:
        reset_session_context(dir_token, thread_token)

    _close_checkpoint()

    events = read_events(session_dir)
    task_span = resume_span_of(events, base_seq)
    resumed = any(e.get("event") == "task_result" for e in events) and len(events) > len(events_before)
    out.write_text(
        json.dumps(
            {
                "resumed": resumed,
                "base_seq": base_seq,
                "task_span_s": task_span,
                "elapsed_s": round(time.perf_counter() - start, 2),
                "error": error,
                "started_at": started_at,
                "finished_at": datetime.now().isoformat(),
                "events_count": len(events),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if resumed else 1


def _task_span_of(events: list[dict], thread_id: str) -> float | None:
    """
    stage2 的恢复耗时：approval_resumed→task_result 的墙钟跨度

    resume_deep_agent 不补发 task_start（区别于新任务），task_start 来自
    stage1 的原始任务——按它算跨度会把"进程死亡间隔"也计进去（W3 实测
    出过 300s 的假数字），恢复耗时应从提交审批决策起算
    """
    from evals.fault_inject.scenarios import _ts

    starts = [_ts(e) for e in events if e.get("event") == "approval_resumed"]
    ends = [_ts(e) for e in events if e.get("event") == "task_result"]
    starts = [t for t in starts if t]
    ends = [t for t in ends if t]
    if not starts or not ends:
        return None
    return round((max(ends) - max(starts)).total_seconds(), 3)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fault_chaos", description="故障注入演练编排")
    parser.add_argument(
        "--scenario",
        choices=["disconnect", "restart_mid_approval", "ws_disconnect", "process_restart_mid_task"],
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument(
        "--workdir", default=None, help="演练产物目录（默认 evals/results/fault_{scenario}）"
    )
    parser.add_argument("--timeout", type=float, default=TASK_TIMEOUT_S)
    parser.add_argument(
        "--real-llm", action="store_true", help="走真实 LLM/Tavily（烧 token；默认 mock）"
    )
    # 内部 worker 模式
    parser.add_argument("--stage1", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage2", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage3", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--stage4", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--out", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.stage1 or args.stage2 or args.stage3 or args.stage4:
        if not (args.thread_id and args.workdir and args.out):
            parser.error("worker 模式需要 --thread-id/--workdir/--out")
        workdir, out = Path(args.workdir), Path(args.out)
        if args.stage1:
            return _stage1(args.thread_id, workdir, out, args.timeout)
        if args.stage2:
            return _stage2(args.thread_id, workdir, out, args.timeout)
        if args.stage3:
            return _stage3(args.thread_id, workdir, out, args.timeout)
        return _stage4(args.thread_id, workdir, out, args.timeout)

    if not args.scenario:
        parser.error("编排模式需要 --scenario")
    if args.real_llm:
        print("[chaos] 警告：--real-llm 会产生真实 API 调用与费用")
    workdir = Path(args.workdir or f"evals/results/fault_{args.scenario}")
    if args.scenario == "disconnect":
        summary = run_disconnect_scenario(args.runs, workdir, args.timeout)
    elif args.scenario == "restart_mid_approval":
        summary = run_restart_scenario(args.runs, workdir, args.timeout)
    elif args.scenario == "ws_disconnect":
        summary = run_ws_disconnect_scenario(args.runs, workdir, args.timeout)
    else:
        summary = run_process_restart_scenario(args.runs, workdir, args.timeout)
    print(
        f"\n[chaos] {args.scenario} 完成：runs={summary['runs']} "
        f"recovery={summary['recovery_success_rate']} "
        f"event_completion={summary['event_completion_rate_avg']} "
        f"recovery_time_p95={summary['recovery_time_p95_s']}s"
    )
    print(f"[chaos] 汇总已写入 {workdir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
