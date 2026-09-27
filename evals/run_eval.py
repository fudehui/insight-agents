"""
评测跑批执行脚本（P0-1 W1 骨架，W2 增强：--cases 多文件合并 + --resume 续跑）

复用 app/agent/main_agent.py 的 run_deep_agent 逐条执行 golden case，并按
固定契约把结果落盘到 evals/results/{run_id}/，供并行评分模块（evals/scoring/）
消费。指标口径衔接 docs/evaluation-metric-template.md，不在此重定义。

关键设计（对应 upgrade-plan P0-1 的两处实施细节）：
1. run_deep_agent 是"同一协程内完成、结果经 monitor 事件推送"的执行入口，
   没有结果返回值。本脚本以"await 该协程返回"为任务结束主判据——协程返回
   即代表图执行收尾（含 _finalize_task 的产物/来源推送）；再以 events.jsonl
   中出现 task_result 事件做二次确认并区分失败形态。超时（默认 600s，可配）
   由 asyncio.wait_for 强制取消协程。
2. 旁路收集全部走会话目录的 events.jsonl（monitor 落盘产物），路径为
   app/output/session_{session_id}/events.jsonl：
   - token 用量：累加 token_usage 事件。TokenUsageCallbackHandler 随
     RunnableConfig 穿透到子智能体（main_agent._build_agent_config），
     主 + 子全部模型调用都会上报，无需改动 app/ 即可在外部取到。
   - 来源清单：读取 task_sources 事件。get_task_sources() 依赖 thread
     ContextVar，且任务收尾 _finalize_task 在推送后立即 cleanup_task_sources，
     任务结束后内存中已取不到；收尾推送的 task_sources 事件是完整清单的
     持久化副本。
   - 报告文件：取会话目录下 mtime 最新的 .md 文件（generate_markdown 的产物）。
3. 评测固定以 approval_mode="off" 运行：无人值守跑批没有审批人，标准档
   （默认）会在 generate_markdown 前中断并一直等到超时，任务永远无法收尾。
4. --dry-run 只做 schema 校验并打印将执行的 case 清单，不导入任何 app 模块、
   不调用 LLM/API，可在无密钥环境验证，退出码恒为 0。
5. W2 增强：--cases 可重复传参按序合并（id 重复报错，loader.load_cases_multi）；
   --resume 续跑——开始前扫描 results-dir/{run_id} 下已有的 case_*.json，
   已存在（无论 status）的 case 跳过执行，summary 重新生成时把跳过的 case
   一并计入统计（status/token/时长从旧结果 JSON 读取），保证 summary 与
   目录内全部 case 一致。
6. W3 修复（收尾挂起）：W2 实测 run_deep_agent 可能在事件循环线程上同步阻塞
   （w1-009 收尾阶段），asyncio.wait_for 的软超时因取消需等待下一个 await 点
   而永远无法触发，整批被卡死。默认改为"每条 case 一个子进程 + 进程级硬超时
   （--timeout + --grace）"的编排模式：worker 进程卡死也会被编排方强杀并补写
   timeout 记录，批处理永不失速。--in-process 退回旧模式（单测/调试用）。

用法示例：
    conda run -n deepsearch python evals/run_eval.py --dry-run
    conda run -n deepsearch python evals/run_eval.py --case w1-003 --case w1-007
    conda run -n deepsearch python evals/run_eval.py --limit 3 --timeout 900
    conda run -n deepsearch python evals/run_eval.py --dry-run \
        --cases evals/golden/w1_batch.yaml --cases evals/golden/w2_batch.yaml
    conda run -n deepsearch python evals/run_eval.py --resume --run-id 20260913_101500
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 本文件位于 evals/，parents[1] 即项目根目录；
# 以 `python evals/run_eval.py` 直跑时 sys.path[0] 是 evals/，
# 先行补上项目根才能 import app / evals 包
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 延迟到 sys.path 就绪后导入本包模块：直跑 `python evals/run_eval.py` 时
# sys.path[0] 是 evals/，必须先补项目根才能定位 evals 包（上述 E402 是有意为之）
from evals.loader import (  # noqa: E402
    CaseValidationError,
    GoldenCase,
    filter_cases,
    load_cases_multi,
)

# 默认参数（与 evals/README.md 的契约说明保持一致）
DEFAULT_CASES_PATH = Path("evals/golden/w1_batch.yaml")
DEFAULT_RESULTS_DIR = Path("evals/results")
DEFAULT_RUN_ID = "{:%Y%m%d_%H%M%S}".format(datetime.datetime.now())
DEFAULT_TIMEOUT_S = 600.0

# 子进程编排下，单条 case 的硬超时 = 任务超时 + 收尾宽限。
# W2 实测：run_deep_agent 在事件循环线程上同步阻塞时（w1-009 收尾阶段挂起），
# asyncio.wait_for 的取消要到下一个 await 点才生效，600s 软超时形同虚设；
# 唯一可靠保障是"每条 case 一个子进程 + 进程级硬超时"（进程被杀，编排方补写
# timeout 记录），因此默认编排模式即子进程隔离。
DEFAULT_FINALIZE_GRACE_S = 180.0

# 评测固定使用的审批档位：关闭审批（无人值守跑批无法响应人工确认）
EVAL_APPROVAL_MODE = "off"


# ---------------------------------------------------------------------------
# 事件/产物收集（全部基于会话目录，不侵入 app/）
# ---------------------------------------------------------------------------


def _session_dir_of(session_id: str) -> Path:
    """
    计算指定会话的工作目录

    延迟导入 app.agent.main_agent：dry-run 不触发任何 app 导入，
    保证无 .env 配置（LLM_QWEN_MAX 未设置时 app.agent.llm 直接抛错）的环境
    也能完成 schema 校验
    """
    from app.agent.main_agent import project_root_path

    # 与 run_deep_agent 的会话目录规则一致：app/output/session_{session_id}
    return Path(project_root_path) / "output" / f"session_{session_id}"


def _read_events(session_dir: Path) -> list[dict[str, Any]]:
    """读取会话目录的 events.jsonl，解析失败的行跳过；文件不存在返回空列表"""
    events_file = session_dir / "events.jsonl"
    if not events_file.is_file():
        return []

    events: list[dict[str, Any]] = []
    with events_file.open("r", encoding="utf-8") as f:
        for line in f:
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


def _collect_report_md(session_dir: Path) -> tuple[Optional[str], Optional[str]]:
    """
    收集本次任务生成的报告 Markdown

    generate_markdown 是唯一报告写入入口，产物落在会话目录；同一任务可能
    生成多个 .md（如模型多次交付），取 mtime 最新的一份作为报告
    :return: (report_md 全文, report_path 绝对路径)；无 .md 产物时为 (None, None)
    """
    if not session_dir.is_dir():
        return None, None

    candidates = [p for p in session_dir.rglob("*.md") if p.is_file()]
    if not candidates:
        return None, None

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return latest.read_text(encoding="utf-8"), str(latest)
    except OSError as e:
        print(f"[run_eval] 报告文件读取失败（{latest}）：{e}")
        return None, str(latest)


def _collect_sources(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    从 task_sources 事件提取来源清单并映射到输出契约结构

    task_sources 事件由 _finalize_task 在来源清理前推送（每任务至多一条），
    取最后一条为准。source_registry 的 docs 即 RAGFlow 文档来源，映射为
    "ragflow"；"doc" 类型预留给上传附件类引用（W1 尚无登记入口）。
    """
    payload: dict[str, Any] = {}
    for event in events:
        if event.get("event") == "task_sources":
            data = event.get("data")
            if isinstance(data, dict):
                payload = data
    if not payload:
        return []

    mapped: list[dict[str, str]] = []
    for item in payload.get("web") or []:
        mapped.append(
            {
                "type": "web",
                "title": str(item.get("title") or ""),
                "url_or_ref": str(item.get("url") or ""),
            }
        )
    for item in payload.get("docs") or []:
        doc = str(item.get("doc") or "")
        page = str(item.get("page") or "")
        mapped.append(
            {
                "type": "ragflow",
                "title": doc,
                # RAGFlow 无 URL，引用以 文档名#page=页码 形式表达
                "url_or_ref": f"{doc}#page={page}" if page else doc,
            }
        )
    for sql_text in payload.get("sql") or []:
        mapped.append(
            {
                "type": "sql",
                "title": "SQL 查询记录",
                "url_or_ref": str(sql_text),
            }
        )
    return mapped


def _collect_token_usage(events: list[dict[str, Any]]) -> Optional[dict[str, int]]:
    """
    累加 token_usage 事件得到任务级 Token 用量

    每次模型调用触发一条 token_usage 事件（TokenUsageCallbackHandler 上报），
    累加即任务总量；一次事件都没取到时返回 None（如任务早期即失败）
    """
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    seen = False
    for event in events:
        if event.get("event") != "token_usage":
            continue
        data = event.get("data") or {}
        input_tokens += int(data.get("input_tokens") or 0)
        output_tokens += int(data.get("output_tokens") or 0)
        total_tokens += int(data.get("total_tokens") or 0)
        seen = True

    if not seen:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _classify_status(
    events: list[dict[str, Any]],
    timed_out: bool,
    coroutine_error: Optional[str],
    timeout_s: float,
) -> tuple[str, Optional[str]]:
    """
    依据事件流与协程结果判定单条 case 的最终状态

    :return: (status, error)；status ∈ completed / failed / timeout
    """
    if timed_out:
        return "timeout", f"任务超时：超过 {timeout_s:.0f}s 未结束，已强制取消"

    event_types = {str(event.get("event") or "") for event in events}
    if "task_result" in event_types:
        # 图执行收尾并产出最终结果；协程异常在结果已产出后发生时不影响交付
        return "completed", None
    if coroutine_error:
        return "failed", coroutine_error
    error_events = [e for e in events if e.get("event") == "error"]
    if error_events:
        # _execute_agent_run 捕获异常后只上报 error 事件（无 task_result）
        return "failed", str(
            error_events[-1].get("message") or "执行主智能发生异常"
        )
    if "task_cancelled" in event_types:
        return "failed", "任务被取消（task_cancelled 事件）"
    return "failed", "未捕获到 task_result 结束事件"


# ---------------------------------------------------------------------------
# 单条执行与批量编排
# ---------------------------------------------------------------------------


async def execute_case(
    case: GoldenCase, session_id: str, timeout_s: float
) -> dict[str, Any]:
    """
    执行单条 golden case 并产出完整的结果记录（输出契约见 evals/README.md）

    :param case: 待执行的用例
    :param session_id: 本条用例的会话 ID（eval_{run_id}_{case_id}），
        同时用作 thread_id 与会话目录名
    :param timeout_s: 单条任务超时上限（秒）
    :return: 结果记录 dict
    """
    # 延迟导入：app 模块只在真实执行时加载
    from app.agent.main_agent import run_deep_agent
    from app.api.budget import reset_task_budget

    session_dir = _session_dir_of(session_id)
    started_at = datetime.datetime.now().isoformat()
    start = time.perf_counter()

    # 跑批前重置工具预算：run_deep_agent 内部也会重置，这里按任务边界
    # 显式再清一次，保证预算状态不依赖对 run_deep_agent 内部行为的假设
    reset_task_budget(session_id)

    timed_out = False
    coroutine_error: Optional[str] = None
    try:
        await asyncio.wait_for(
            run_deep_agent(
                task_query=case.query,
                session_id=session_id,
                approval_mode=EVAL_APPROVAL_MODE,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        # py3.11+ asyncio.TimeoutError 即内建 TimeoutError 的别名；
        # wait_for 超时后已取消内部协程
        timed_out = True
    except asyncio.CancelledError:
        # 外部取消（如 Ctrl+C）：按失败收尾，但向上保留取消语义
        raise
    except Exception as e:  # 协程自身抛错：记录后仍尝试收集已有产物
        coroutine_error = f"{type(e).__name__}: {e}"

    duration_s = round(time.perf_counter() - start, 2)
    finished_at = datetime.datetime.now().isoformat()

    events = _read_events(session_dir)
    status, error = _classify_status(events, timed_out, coroutine_error, timeout_s)
    report_md, report_path = _collect_report_md(session_dir)

    return {
        "case_id": case.id,
        "query": case.query,
        "session_id": session_id,
        "report_md": report_md,
        "report_path": report_path,
        "sources": _collect_sources(events),
        "token_usage": _collect_token_usage(events),
        "duration_s": duration_s,
        "status": status,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
    }


async def run_batch(
    cases: list[GoldenCase],
    run_id: str,
    results_dir: Path,
    timeout_s: float,
) -> list[dict[str, Any]]:
    """
    串行执行一批用例并逐条落盘

    必须在同一事件循环内逐条 await：AsyncSqliteSaver 的 aiosqlite 连接在
    首次执行时绑定事件循环，逐条 asyncio.run 会因换循环导致连接失效。
    :return: 每条 case 的结果记录（按执行顺序）
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    for index, case in enumerate(cases, start=1):
        session_id = f"eval_{run_id}_{case.id}"
        print(f"\n===== [{index}/{len(cases)}] case={case.id} session={session_id} =====")
        record = await execute_case(case, session_id, timeout_s)

        case_file = results_dir / f"case_{case.id}.json"
        case_file.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"----- case={case.id} 完成：status={record['status']} "
            f"duration={record['duration_s']}s 结果={case_file}"
        )
        records.append(record)

    return records


def _p95(values: list[float]) -> float:
    """最近秩（nearest-rank）法 P95，与手工基线的保守口径一致；空列表返回 0.0"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def build_summary(
    run_id: str,
    started_at: str,
    finished_at: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    汇总单次跑批的 summary 记录

    --resume 续跑时 records 同时包含本次执行与跳过的历史记录（后者从旧
    case_*.json 原样读取），保证 summary 与结果目录内全部 case 一致。

    计数口径：cases_completed 只统计 status=completed；cases_failed 统计
    其余全部非完成态（failed + timeout），精确状态以 per_case 为准
    """
    durations = [float(r.get("duration_s") or 0.0) for r in records]
    per_case_total_tokens: list[Optional[int]] = []
    total_tokens = 0
    for record in records:
        usage = record.get("token_usage")
        if isinstance(usage, dict):
            case_total = int(usage.get("total_tokens") or 0)
            total_tokens += case_total
            per_case_total_tokens.append(case_total)
        else:
            per_case_total_tokens.append(None)

    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "cases_run": len(records),
        "cases_completed": sum(
            1 for r in records if r.get("status") == "completed"
        ),
        "cases_failed": sum(1 for r in records if r.get("status") != "completed"),
        "total_tokens": total_tokens,
        "p95_duration_s": _p95(durations),
        "per_case": [
            {
                "case_id": record.get("case_id"),
                "status": record.get("status"),
                "duration_s": record.get("duration_s"),
                "total_tokens": per_case_total_tokens[index],
            }
            for index, record in enumerate(records)
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="评测跑批：复用 run_deep_agent 执行 golden case 并按固定契约落盘",
    )
    parser.add_argument(
        "--cases",
        action="append",
        default=None,
        metavar="PATH",
        help=f"golden case YAML 路径，可重复传参按序合并、id 不可重复"
        f"（默认 {DEFAULT_CASES_PATH}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只执行前 N 条（先按 --case 过滤再截断）",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        metavar="ID",
        help="只执行指定 case id，可重复传参（如 --case w1-003 --case w1-007）",
    )
    parser.add_argument(
        "--run-id",
        default=DEFAULT_RUN_ID,
        help=f"本次跑批标识，默认时间戳 {DEFAULT_RUN_ID}；"
        "会话 ID 将取 'eval_{run_id}_{case_id}' 形式",
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"结果输出根目录（默认 {DEFAULT_RESULTS_DIR}，实际写入 {DEFAULT_RESULTS_DIR}/{{run_id}}/）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"单条 case 超时秒数（默认 {DEFAULT_TIMEOUT_S:.0f}s），超时强制取消并记为 timeout",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发数（默认 1）。>1 需 ContextVar 会话隔离才能保证正确性，"
        "W1 只保证串行，传 >1 将告警并回退串行",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做 schema 校验并打印将执行的 case 清单，不导入 app、不调用任何 LLM/API，退出码 0",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="续跑：跳过 results-dir/{run_id} 下已有 case_*.json（无论 status）的 case，"
        "只执行缺失的 case；summary 重新生成时把跳过的 case 一并计入统计"
        "（status/token/时长从旧结果 JSON 读取）",
    )
    parser.add_argument(
        "--grace",
        type=float,
        default=DEFAULT_FINALIZE_GRACE_S,
        help=f"子进程编排下收尾宽限秒数（默认 {DEFAULT_FINALIZE_GRACE_S:.0f}s），"
        "硬超时 = --timeout + --grace",
    )
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="退回旧模式：全部 case 在当前进程内串行执行（不推荐——收尾挂起会"
        "卡死整批且软超时无法自救；主要为单测与调试保留）",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,  # 内部模式：由编排方拉起，单进程内执行选中 case
    )
    args = parser.parse_args(argv)
    # --cases 未传参时回退默认值：action='append' 与标量默认值不兼容，统一在此归一为列表
    if not args.cases:
        args.cases = [str(DEFAULT_CASES_PATH)]
    return args


def _resolve_cases_path(raw: str) -> Path:
    """用例路径解析：先按 cwd，再按项目根，保证从任意目录启动都能命中默认值"""
    path = Path(raw)
    if path.is_absolute() or path.exists():
        return path
    fallback = PROJECT_ROOT / path
    return fallback if fallback.exists() else path


def _load_existing_record(case_file: Path) -> Optional[dict[str, Any]]:
    """
    --resume 时读取已存在的单条结果记录

    只要 JSON 可读且带 case_id 就视为"该 case 已有结果"（无论 status，含
    failed/timeout，避免反复重跑坏 case）；文件不存在、解析失败或缺少
    case_id 时返回 None，该 case 照常重新执行。
    """
    if not case_file.is_file():
        return None
    try:
        record = json.loads(case_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[run_eval] 旧结果文件不可读，该 case 将重新执行（{case_file}）：{e}")
        return None
    if not isinstance(record, dict) or not record.get("case_id"):
        print(f"[run_eval] 旧结果文件缺少 case_id，该 case 将重新执行（{case_file}）")
        return None
    return record


def _synthetic_timeout_record(
    case: GoldenCase, session_id: str, timeout_s: float, grace_s: float
) -> dict[str, Any]:
    """
    子进程被硬超时终止后由编排方补写的占位记录（输出契约字段与正常记录一致）

    子进程内部（事件循环挂起 / 收尾阻塞）已无法自行落盘，这份记录保证
    summary 与结果目录始终一致、后续 --resume 不会反复重试卡死 case
    """
    now = datetime.datetime.now().isoformat()
    return {
        "case_id": case.id,
        "query": case.query,
        "session_id": session_id,
        "report_md": None,
        "report_path": None,
        "sources": [],
        "token_usage": None,
        "duration_s": round(timeout_s + grace_s, 2),
        "status": "timeout",
        "error": (
            f"子进程硬超时：任务超时 {timeout_s:.0f}s + 收尾宽限 {grace_s:.0f}s 后"
            "仍未退出，进程已被编排方强制终止（详见 logs/case_"
            f"{case.id}.log）"
        ),
        "started_at": now,
        "finished_at": now,
    }


def _run_case_in_subprocess(
    case: GoldenCase,
    case_paths: list[Path],
    run_id: str,
    results_dir: Path,
    timeout_s: float,
    grace_s: float,
) -> None:
    """
    子进程编排：为单条 case 拉起独立 worker 进程并等待其退出

    worker 进程即本脚本自身（--worker 模式），在进程内执行该 case 并落盘到
    results_dir/case_{id}.json；编排方只负责硬超时兜底与记录补写。
    stdout/stderr 重定向到 results_dir/logs/case_{id}.log 便于事后排查。
    """
    results_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = results_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"case_{case.id}.log"

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--run-id",
        run_id,
        "--results-dir",
        str(results_dir.parent),
        "--timeout",
        str(timeout_s),
        "--case",
        case.id,
    ]
    for path in case_paths:
        cmd.extend(["--cases", str(path)])

    session_id = f"eval_{run_id}_{case.id}"
    case_file = results_dir / f"case_{case.id}.json"
    started = time.perf_counter()
    try:
        with log_file.open("w", encoding="utf-8") as log:
            proc = subprocess.run(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout_s + grace_s,
                check=False,
            )
        print(
            f"----- case={case.id} 完成：exit={proc.returncode} "
            f"耗时={time.perf_counter() - started:.0f}s 日志={log_file}"
        )
    except subprocess.TimeoutExpired:
        # 进程级硬超时：worker 已无力自救（正常路径下其内部软超时先触发）。
        # 但 worker 可能已完成 case 落盘、只是收尾阶段挂住未退出——此时磁盘上
        # 已有真实结果，不得用 timeout 占位记录覆盖（W3 实测 w1-009 即此形态）
        existing = _load_existing_record(case_file)
        if existing is not None:
            print(
                f"----- case={case.id} 硬超时（>{timeout_s + grace_s:.0f}s），"
                f"但磁盘已有该 case 的结果记录（status={existing.get('status')}），"
                "保留原记录不覆盖"
            )
            return
        print(
            f"----- case={case.id} 硬超时（>{timeout_s + grace_s:.0f}s），"
            f"worker 进程被强制终止，已补写 timeout 记录"
        )
        record = _synthetic_timeout_record(case, session_id, timeout_s, grace_s)
        case_file.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _run_orchestrated_batch(
    pending: list[GoldenCase],
    case_paths: list[Path],
    run_id: str,
    results_dir: Path,
    timeout_s: float,
    grace_s: float,
) -> None:
    """逐条以子进程执行 pending case（默认编排模式，见模块 docstring 的 W3 修复说明）"""
    for index, case in enumerate(pending, start=1):
        print(
            f"\n===== [{index}/{len(pending)}] case={case.id} "
            f"（子进程隔离，硬超时 {timeout_s + grace_s:.0f}s） ====="
        )
        _run_case_in_subprocess(
            case, case_paths, run_id, results_dir, timeout_s, grace_s
        )


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    try:
        # --cases 可重复传参：按传参顺序加载合并（跨文件 id 重复报错）
        cases = load_cases_multi(
            [_resolve_cases_path(path) for path in args.cases]
        )
        selected = filter_cases(cases, ids=args.case, limit=args.limit)
    except (CaseValidationError, FileNotFoundError, ValueError) as e:
        print(f"[run_eval] 用例加载失败：{e}", file=sys.stderr)
        return 2

    if args.concurrency > 1:
        print(
            f"[run_eval] 警告：--concurrency={args.concurrency} 需要 ContextVar 会话隔离"
            "才能保证正确性，W1 只保证串行，本次回退为串行执行。"
        )

    if args.dry_run:
        # dry-run 不触发任何 app 导入：execute_case 内的 app 依赖均为延迟导入
        print(
            f"[DRY-RUN] 共加载 {len(cases)} 条 case，schema 校验通过；"
            f"选中 {len(selected)} 条："
        )
        for case in selected:
            tags = ",".join(case.tags) if case.tags else "-"
            print(
                f"  {case.id}  tags={tags}  "
                f"tools={','.join(case.expected_tools)}  query={case.query[:50]}..."
            )
        print(
            f"[DRY-RUN] 预计串行执行 {len(selected)} 条，单条超时 {args.timeout:.0f}s；"
            "不调用任何 LLM/API。"
        )
        return 0

    run_id = args.run_id
    results_root = Path(args.results_dir)
    results_dir = results_root / run_id

    # --resume：扫描结果目录，已有结果（无论 status）的 case 跳过执行
    resumed: dict[str, dict[str, Any]] = {}
    if args.resume:
        for case in selected:
            existing = _load_existing_record(results_dir / f"case_{case.id}.json")
            if existing is not None:
                resumed[case.id] = existing
        pending = [case for case in selected if case.id not in resumed]
        if resumed:
            print(
                f"[run_eval] --resume：跳过已有结果 {len(resumed)} 条"
                f"（{'、'.join(resumed)}），本次执行 {len(pending)} 条"
            )
        else:
            print(
                f"[run_eval] --resume：未发现已有结果，全部 {len(selected)} 条照常执行"
            )
    else:
        pending = selected

    batch_started_at = datetime.datetime.now().isoformat()

    resolved_paths = [_resolve_cases_path(path) for path in args.cases]

    if args.worker:
        # 内部 worker 模式：当前进程内执行选中 case 并落盘（summary 由编排方统一重建）。
        # 看门狗：硬超时时刻先 dump 全线程栈到 stderr（日志文件）再退出——W2 的
        # 收尾挂起因缺少栈证据只能靠排除法定位，faulthandler 让下次复现直接
        # 看到卡在哪一行（标准库零依赖，正常完成的任务不会触发）
        import faulthandler

        faulthandler.dump_traceback_later(args.timeout + args.grace, exit=True)
        asyncio.run(run_batch(pending, run_id, results_dir, args.timeout))
        faulthandler.cancel_dump_traceback_later()
        return 0

    if args.in_process:
        # 旧模式：全部 case 在当前进程内串行执行（单测与调试用）
        executed_records = asyncio.run(
            run_batch(pending, run_id, results_dir, args.timeout)
        )
        executed_by_id = {record["case_id"]: record for record in executed_records}
    else:
        # 默认编排模式：每条 case 一个子进程 + 进程级硬超时（W3 修复收尾挂起）
        _run_orchestrated_batch(
            pending,
            resolved_paths,
            run_id,
            results_dir,
            args.timeout,
            args.grace,
        )
        executed_by_id = {}

    batch_finished_at = datetime.datetime.now().isoformat()

    # summary 必须覆盖全部选中 case：跳过的用旧记录，本次执行的优先用本次内存
    # 记录（in-process），子进程编排模式下从磁盘 case_*.json 读取（含硬超时补写
    # 的占位记录），保证 summary 与结果目录一致
    all_records: list[dict[str, Any]] = []
    for case in selected:
        if case.id in resumed:
            all_records.append(resumed[case.id])
            continue
        record = executed_by_id.get(case.id) or _load_existing_record(
            results_dir / f"case_{case.id}.json"
        )
        if record is None:
            print(
                f"[run_eval] 警告：case={case.id} 无结果记录（子进程异常退出且"
                "未落盘），summary 记为 failed"
            )
            record = {
                "case_id": case.id,
                "query": case.query,
                "session_id": f"eval_{run_id}_{case.id}",
                "status": "failed",
                "error": "子进程退出但未产出结果文件",
                "duration_s": 0.0,
                "token_usage": None,
            }
        all_records.append(record)
    summary = build_summary(run_id, batch_started_at, batch_finished_at, all_records)
    summary_file = results_dir / "summary.json"
    summary_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"\n[run_eval] 跑批结束：run_id={run_id} "
        f"completed={summary['cases_completed']}/{summary['cases_run']} "
        f"total_tokens={summary['total_tokens']} "
        f"p95_duration={summary['p95_duration_s']}s"
    )
    if resumed:
        print(f"[run_eval] 其中 {len(resumed)} 条为 --resume 跳过的历史结果")
    print(f"[run_eval] 汇总已写入：{summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
