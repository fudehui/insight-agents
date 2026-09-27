"""
evals/run_eval.py 的测试

不调用任何真实 LLM/API：
- --dry-run 对 w1_batch.yaml 全 10 条通过（退出码 0）；
- 输出契约：case JSON / summary JSON 的字段名与类型（用临时 results 目录 +
  monkeypatch 假 runner 验证写盘契约）；
- execute_case 的事件收集与状态分类（stub 掉 app.agent.main_agent 与
  app.api.budget 两个延迟导入的模块，端到端验证 events.jsonl 解析逻辑）；
- W2 增强：--cases 多文件合并（含 id 冲突报错）与 --resume 续跑
  （跳过已有结果、summary 把跳过项计入统计）。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import yaml

from evals import run_eval
from evals.loader import GoldenCase

# 真实批次文件
W1_BATCH_PATH = Path(__file__).resolve().parents[1] / "evals" / "golden" / "w1_batch.yaml"
W2_BATCH_PATH = Path(__file__).resolve().parents[1] / "evals" / "golden" / "w2_batch.yaml"

# 输出契约（字段名固定，供并行评分模块使用；与 evals/README.md 保持一致）
CASE_KEYS = {
    "case_id",
    "query",
    "session_id",
    "report_md",
    "report_path",
    "sources",
    "token_usage",
    "duration_s",
    "status",
    "error",
    "started_at",
    "finished_at",
}
SOURCE_KEYS = {"type", "title", "url_or_ref"}
TOKEN_KEYS = {"input_tokens", "output_tokens", "total_tokens"}
SUMMARY_KEYS = {
    "run_id",
    "started_at",
    "finished_at",
    "cases_run",
    "cases_completed",
    "cases_failed",
    "total_tokens",
    "p95_duration_s",
    "per_case",
}
PER_CASE_KEYS = {"case_id", "status", "duration_s", "total_tokens"}


def _fake_record(case_id, status="completed", token_usage=None, error=None):
    """构造一条符合输出契约的结果记录（由假 runner 返回）"""
    return {
        "case_id": case_id,
        "query": f"假任务 {case_id}",
        "session_id": f"eval_contract_run_{case_id}",
        "report_md": "# 报告\n正文",
        "report_path": f"/tmp/session_{case_id}/报告.md",
        "sources": [
            {"type": "web", "title": "示例", "url_or_ref": "https://example.com"}
        ],
        "token_usage": token_usage
        if token_usage is not None
        else {"input_tokens": 1000, "output_tokens": 234, "total_tokens": 1234},
        "duration_s": 1.5,
        "status": status,
        "error": error,
        "started_at": "2026-09-13T10:00:00",
        "finished_at": "2026-09-13T10:00:01.500000",
    }


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_passes_all_ten(tmp_path, capsys):
    exit_code = run_eval.main(
            [
            "--in-process","--dry-run", "--results-dir", str(tmp_path), "--run-id", "dry_run_test"]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "schema 校验通过" in output
    for index in range(1, 11):
        assert f"w1-{index:03d}" in output
    assert "不调用任何 LLM/API" in output
    # dry-run 不写任何结果文件
    assert not (tmp_path / "dry_run_test").exists()


def test_dry_run_respects_case_filter_and_limit(tmp_path, capsys):
    exit_code = run_eval.main(
            [
            "--in-process",
            "--dry-run",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-003",
            "--case",
            "w1-007",
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "选中 1 条" in output  # 先过滤（2 条）再截断（1 条）
    assert "w1-003" in output
    assert "w1-007" not in output


def test_dry_run_unknown_case_id_fails(tmp_path):
    exit_code = run_eval.main(
            [
            "--in-process","--dry-run", "--results-dir", str(tmp_path), "--case", "no-such-case"]
    )

    assert exit_code == 2


# ---------------------------------------------------------------------------
# 写盘契约（monkeypatch 假 runner，不触发 app 导入）
# ---------------------------------------------------------------------------


def _install_fake_runner(monkeypatch, statuses):
    """
    把 run_eval.execute_case 替换为假 runner

    :param statuses: 每条 case 依次返回的 status（或显式记录 dict）
    :return: 调用记录列表 [(case, session_id, timeout_s)]
    """
    calls = []

    async def fake_execute_case(case, session_id, timeout_s):
        calls.append((case.id, session_id, timeout_s))
        status = statuses[len(calls) - 1]
        if isinstance(status, dict):
            return status
        error = f"假失败：{status}" if status in ("failed", "timeout") else None
        return _fake_record(case.id, status=status, error=error)

    monkeypatch.setattr(run_eval, "execute_case", fake_execute_case)
    return calls


def test_case_json_contract(tmp_path, monkeypatch):
    _install_fake_runner(monkeypatch, ["completed", "completed"])

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "contract_run",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-001",
            "--case",
            "w1-003",
        ]
    )

    assert exit_code == 0
    run_dir = tmp_path / "contract_run"

    for case_id in ("w1-001", "w1-003"):
        case_file = run_dir / f"case_{case_id}.json"
        assert case_file.is_file(), f"缺少结果文件 {case_file}"
        record = json.loads(case_file.read_text(encoding="utf-8"))
        # 字段名与类型必须与契约完全一致
        assert set(record.keys()) == CASE_KEYS
        assert record["case_id"] == case_id
        assert record["status"] == "completed"
        assert record["error"] is None
        assert isinstance(record["duration_s"], (int, float))
        assert record["session_id"] == f"eval_contract_run_{case_id}"
        for source in record["sources"]:
            assert set(source.keys()) == SOURCE_KEYS
            assert source["type"] in ("web", "sql", "ragflow", "doc")
        assert set(record["token_usage"].keys()) == TOKEN_KEYS


def test_summary_json_contract(tmp_path, monkeypatch):
    _install_fake_runner(monkeypatch, ["completed", "completed"])

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "summary_run",
            "--results-dir",
            str(tmp_path),
            "--limit",
            "2",
        ]
    )

    assert exit_code == 0
    summary_file = tmp_path / "summary_run" / "summary.json"
    assert summary_file.is_file()
    summary = json.loads(summary_file.read_text(encoding="utf-8"))

    assert set(summary.keys()) == SUMMARY_KEYS
    assert summary["run_id"] == "summary_run"
    assert summary["cases_run"] == 2
    assert summary["cases_completed"] == 2
    assert summary["cases_failed"] == 0
    assert summary["total_tokens"] == 2468  # 两条假记录的 total_tokens 求和
    assert isinstance(summary["p95_duration_s"], (int, float))
    assert summary["started_at"] and summary["finished_at"]

    assert len(summary["per_case"]) == 2
    for item in summary["per_case"]:
        assert set(item.keys()) == PER_CASE_KEYS
        assert item["status"] == "completed"
        assert item["total_tokens"] == 1234


def test_summary_counts_failures_and_timeouts(tmp_path, monkeypatch):
    """failed 与 timeout 都计入 cases_failed，精确状态保留在 per_case"""
    _install_fake_runner(monkeypatch, ["completed", "failed", "timeout"])

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "mixed_run",
            "--results-dir",
            str(tmp_path),
            "--limit",
            "3",
        ]
    )

    assert exit_code == 0
    summary = json.loads(
        (tmp_path / "mixed_run" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["cases_run"] == 3
    assert summary["cases_completed"] == 1
    assert summary["cases_failed"] == 2
    assert [item["status"] for item in summary["per_case"]] == [
        "completed",
        "failed",
        "timeout",
    ]


def test_fake_runner_receives_eval_session_ids(tmp_path, monkeypatch):
    """会话 ID 必须遵循 eval_{run_id}_{case_id} 约定"""
    calls = _install_fake_runner(monkeypatch, ["completed"])

    run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "sess_run",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-002",
        ]
    )

    assert calls == [("w1-002", "eval_sess_run_w1-002", 600.0)]


# ---------------------------------------------------------------------------
# execute_case 端到端（stub 延迟导入的 app 模块，验证事件收集与状态分类）
# ---------------------------------------------------------------------------


def _install_app_stubs(monkeypatch, project_root: Path, behavior: dict):
    """
    用假模块替换 execute_case 延迟导入的两个 app 模块

    - app.agent.main_agent：project_root_path 指向临时目录（会话目录随之落位），
      run_deep_agent 按 behavior 写入模拟事件流与报告文件
    - app.api.budget：reset_task_budget 只做调用记录
    两个包的 __init__.py 均为空文件，替换父包导入不会引入真实依赖
    """
    captured = {"resets": [], "calls": []}

    async def fake_run_deep_agent(task_query, session_id, approval_mode=None):
        captured["calls"].append(
            {
                "task_query": task_query,
                "session_id": session_id,
                "approval_mode": approval_mode,
            }
        )
        if behavior.get("sleep_forever"):
            await asyncio.sleep(3600)
            return

        session_dir = project_root / "output" / f"session_{session_id}"
        if behavior.get("no_events"):
            # 模拟早期失败：run_deep_agent 正常返回但未写任何事件与产物
            return
        session_dir.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "type": "monitor_event",
                "event": "task_start",
                "message": "研究任务已启动",
                "data": {"query": task_query},
            },
            {
                "type": "monitor_event",
                "event": "token_usage",
                "message": "Token 用量",
                "data": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
            },
            {
                "type": "monitor_event",
                "event": "token_usage",
                "message": "Token 用量",
                "data": {"input_tokens": 200, "output_tokens": 70, "total_tokens": 270},
            },
        ]
        if behavior.get("with_sources"):
            events.append(
                {
                    "type": "monitor_event",
                    "event": "task_sources",
                    "message": "本次任务共引用 3 个来源",
                    "data": {
                        "web": [
                            {"title": "Tavily 官网", "url": "https://tavily.com"}
                        ],
                        "docs": [{"doc": "投资展望.pdf", "page": "3"}],
                        "sql": ["SELECT COUNT(*) FROM inventory"],
                    },
                }
            )
        if behavior.get("error_only"):
            events.append(
                {
                    "type": "monitor_event",
                    "event": "error",
                    "message": "执行主智能发生异常信息：模拟异常",
                    "data": {},
                }
            )
        if behavior.get("with_result", True):
            events.append(
                {
                    "type": "monitor_event",
                    "event": "task_result",
                    "message": "任务执行完成",
                    "data": {"result": "最终回答"},
                }
            )
        (session_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
            encoding="utf-8",
        )
        if behavior.get("with_report"):
            (session_dir / "分析报告.md").write_text(
                "# 分析报告\n正文内容", encoding="utf-8"
            )

    fake_main_agent = types.ModuleType("app.agent.main_agent")
    fake_main_agent.project_root_path = project_root
    fake_main_agent.run_deep_agent = fake_run_deep_agent

    fake_budget = types.ModuleType("app.api.budget")
    fake_budget.reset_task_budget = lambda thread_id: captured["resets"].append(thread_id)

    monkeypatch.setitem(sys.modules, "app.agent.main_agent", fake_main_agent)
    monkeypatch.setitem(sys.modules, "app.api.budget", fake_budget)
    return captured


def _golden_case(case_id="w1-000"):
    return GoldenCase(
        id=case_id,
        query="测试任务：查询库存并生成报告",
        expected_points=["要点一", "要点二", "要点三"],
        expected_tools=["execute_sql_query"],
        expected_source_types=["sql"],
    )


def test_execute_case_completed_collects_all(tmp_path, monkeypatch):
    behavior = {"with_sources": True, "with_report": True, "with_result": True}
    captured = _install_app_stubs(monkeypatch, tmp_path, behavior)

    record = asyncio.run(
        run_eval.execute_case(_golden_case("w1-000"), "eval_stub_w1-000", 5.0)
    )

    # 契约字段完整，且收集结果与模拟事件流一致
    assert set(record.keys()) == CASE_KEYS
    assert record["status"] == "completed"
    assert record["error"] is None
    assert record["token_usage"] == {
        "input_tokens": 300,
        "output_tokens": 100,
        "total_tokens": 400,
    }
    assert record["sources"] == [
        {"type": "web", "title": "Tavily 官网", "url_or_ref": "https://tavily.com"},
        {
            "type": "ragflow",
            "title": "投资展望.pdf",
            "url_or_ref": "投资展望.pdf#page=3",
        },
        {
            "type": "sql",
            "title": "SQL 查询记录",
            "url_or_ref": "SELECT COUNT(*) FROM inventory",
        },
    ]
    assert record["report_md"] == "# 分析报告\n正文内容"
    assert record["report_path"] is not None and record["report_path"].endswith("分析报告.md")
    assert record["session_id"] == "eval_stub_w1-000"
    assert record["duration_s"] >= 0
    assert record["started_at"] and record["finished_at"]

    # 执行参数：approval_mode 固定关闭审批；预算在跑批前被重置
    assert captured["calls"] == [
        {
            "task_query": "测试任务：查询库存并生成报告",
            "session_id": "eval_stub_w1-000",
            "approval_mode": "off",
        }
    ]
    assert captured["resets"] == ["eval_stub_w1-000"]


def test_execute_case_timeout(tmp_path, monkeypatch):
    _install_app_stubs(monkeypatch, tmp_path, {"sleep_forever": True})

    record = asyncio.run(
        run_eval.execute_case(_golden_case("w1-001"), "eval_stub_w1-001", 0.05)
    )

    assert record["status"] == "timeout"
    assert "超时" in record["error"]


def test_execute_case_error_event_maps_to_failed(tmp_path, monkeypatch):
    """只有 error 事件、无 task_result 时判定为 failed 并携带错误信息"""
    _install_app_stubs(
        monkeypatch, tmp_path, {"error_only": True, "with_result": False}
    )

    record = asyncio.run(
        run_eval.execute_case(_golden_case("w1-002"), "eval_stub_w1-002", 5.0)
    )

    assert record["status"] == "failed"
    assert "模拟异常" in record["error"]


def test_execute_case_missing_session_maps_to_failed(tmp_path, monkeypatch):
    """run_deep_agent 正常返回但没写任何事件（早期失败）时判定为 failed"""
    _install_app_stubs(monkeypatch, tmp_path, {"no_events": True})

    record = asyncio.run(
        run_eval.execute_case(_golden_case("w1-003"), "eval_stub_w1-003", 5.0)
    )

    assert record["status"] == "failed"
    assert "未捕获到 task_result" in record["error"]
    assert record["report_md"] is None and record["report_path"] is None
    assert record["sources"] == []
    assert record["token_usage"] is None


def test_build_summary_p95_nearest_rank():
    base = {
        "case_id": "c",
        "status": "completed",
        "token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }

    def make(duration_s):
        record = dict(base)
        record["duration_s"] = duration_s
        return record

    # 最近秩法：ceil(0.95 * n) 位
    assert run_eval.build_summary("r", "s", "f", [make(d) for d in [1, 2, 3, 4]])[
        "p95_duration_s"
    ] == 4.0
    assert run_eval.build_summary(
        "r", "s", "f", [make(d) for d in range(1, 11)]
    )["p95_duration_s"] == 10.0
    assert run_eval.build_summary("r", "s", "f", [])["p95_duration_s"] == 0.0


def test_build_summary_handles_missing_token_usage():
    records = [
        {
            "case_id": "c1",
            "status": "completed",
            "duration_s": 2.0,
            "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        },
        {
            "case_id": "c2",
            "status": "timeout",
            "duration_s": 4.0,
            "token_usage": None,
        },
    ]

    summary = run_eval.build_summary("r", "s", "f", records)

    assert summary["total_tokens"] == 15  # None 视为 0
    assert summary["per_case"][1]["total_tokens"] is None
    assert summary["p95_duration_s"] == 4.0


# ---------------------------------------------------------------------------
# W2 增强：--cases 多文件合并
# ---------------------------------------------------------------------------


def test_dry_run_multi_cases_w1_plus_w2(tmp_path, capsys):
    """全量 30 条 = w1 + w2 两个文件合并加载，dry-run 通过且逐条列出"""
    exit_code = run_eval.main(
            [
            "--in-process",
            "--dry-run",
            "--cases",
            str(W1_BATCH_PATH),
            "--cases",
            str(W2_BATCH_PATH),
            "--results-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "共加载 30 条 case，schema 校验通过" in output
    assert "选中 30 条" in output
    for index in range(1, 11):
        assert f"w1-{index:03d}" in output
    for index in range(1, 21):
        assert f"w2-{index:03d}" in output


def test_multi_cases_files_executed_in_merge_order(tmp_path, monkeypatch):
    """合并后的执行顺序与传参顺序一致（w1 在前、w2 在后）"""
    calls = _install_fake_runner(monkeypatch, ["completed", "completed"])

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--cases",
            str(W2_BATCH_PATH),
            "--run-id",
            "merge_run",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-001",
            "--case",
            "w2-001",
        ]
    )

    assert exit_code == 0
    assert [call[0] for call in calls] == ["w1-001", "w2-001"]


def test_multi_cases_duplicate_id_fails(tmp_path):
    """跨文件 id 重复：加载失败，退出码 2"""
    case = {
        "id": "dup-001",
        "query": "测试问题",
        "expected_points": ["要点一", "要点二", "要点三"],
        "expected_tools": ["internet_search"],
        "expected_source_types": ["web"],
    }
    file_a = tmp_path / "a.yaml"
    file_a.write_text(
        yaml.safe_dump({"cases": [case]}, allow_unicode=True), encoding="utf-8"
    )
    file_b = tmp_path / "b.yaml"
    file_b.write_text(
        yaml.safe_dump({"cases": [dict(case)]}, allow_unicode=True), encoding="utf-8"
    )

    exit_code = run_eval.main(
            [
            "--in-process","--dry-run", "--cases", str(file_a), "--cases", str(file_b)]
    )

    assert exit_code == 2


# ---------------------------------------------------------------------------
# W2 增强：--resume 续跑
# ---------------------------------------------------------------------------


def test_resume_skips_existing_and_summary_includes_them(tmp_path, monkeypatch):
    """已有 case_*.json（无论 status）跳过执行；summary 把跳过项计入统计"""
    calls = _install_fake_runner(monkeypatch, ["completed", "completed", "failed"])

    # 第一次：只跑前两条，写入固定 run-id 的结果目录
    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "resume_run",
            "--results-dir",
            str(tmp_path),
            "--limit",
            "2",
        ]
    )
    assert exit_code == 0
    assert [call[0] for call in calls] == ["w1-001", "w1-002"]

    # 第二次：同 run-id 续跑三条（前两条已有结果应跳过，只执行 w1-003）
    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "resume_run",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-001",
            "--case",
            "w1-002",
            "--case",
            "w1-003",
            "--resume",
        ]
    )
    assert exit_code == 0
    # 只新增一次执行调用：w1-001/w1-002 从旧结果跳过
    assert [call[0] for call in calls] == ["w1-001", "w1-002", "w1-003"]

    summary = json.loads(
        (tmp_path / "resume_run" / "summary.json").read_text(encoding="utf-8")
    )
    # summary 覆盖全部选中 case：跳过的 2 条 + 本次执行的 1 条
    assert summary["cases_run"] == 3
    assert summary["cases_completed"] == 2  # 跳过条目按旧 JSON 的 status=completed 计
    assert summary["cases_failed"] == 1  # 本次执行的 w1-003 失败
    assert [item["case_id"] for item in summary["per_case"]] == [
        "w1-001",
        "w1-002",
        "w1-003",
    ]
    assert [item["status"] for item in summary["per_case"]] == [
        "completed",
        "completed",
        "failed",
    ]
    # 跳过条目的 token 从旧结果 JSON 读取
    assert summary["total_tokens"] == 1234 * 3


def test_resume_skips_timeout_record_too(tmp_path, monkeypatch):
    """timeout 状态的旧结果同样跳过，并按 timeout 计入 summary 统计"""
    calls = _install_fake_runner(monkeypatch, ["completed"])
    run_dir = tmp_path / "resume_run2"
    run_dir.mkdir(parents=True)
    # 手工放置一条 timeout 旧记录（token_usage 为 null）
    timeout_record = dict(_fake_record("w1-004"), status="timeout", token_usage=None,
                          duration_s=12.34, error="任务超时：超过 600s 未结束，已强制取消")
    (run_dir / "case_w1-004.json").write_text(
        json.dumps(timeout_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "resume_run2",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-004",
            "--resume",
        ]
    )

    assert exit_code == 0
    assert calls == []  # 旧结果存在，未执行任何 case
    summary = json.loads(
        (run_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["cases_run"] == 1
    assert summary["cases_completed"] == 0
    assert summary["cases_failed"] == 1  # timeout 计入 failed 口径
    assert summary["per_case"][0]["duration_s"] == 12.34  # 时长沿用旧记录
    assert summary["per_case"][0]["total_tokens"] is None
    assert summary["total_tokens"] == 0


def test_resume_without_existing_results_runs_all(tmp_path, monkeypatch, capsys):
    """结果目录为空时 --resume 等价于普通全量执行"""
    calls = _install_fake_runner(monkeypatch, ["completed", "completed"])

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "resume_fresh",
            "--results-dir",
            str(tmp_path),
            "--limit",
            "2",
            "--resume",
        ]
    )

    assert exit_code == 0
    assert [call[0] for call in calls] == ["w1-001", "w1-002"]
    output = capsys.readouterr().out
    assert "未发现已有结果" in output
    summary = json.loads(
        (tmp_path / "resume_fresh" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["cases_run"] == 2


def test_resume_corrupt_json_reruns(tmp_path, monkeypatch):
    """旧结果文件损坏（非法 JSON）时视为不存在，该 case 重新执行"""
    calls = _install_fake_runner(monkeypatch, ["completed"])
    run_dir = tmp_path / "resume_run3"
    run_dir.mkdir(parents=True)
    (run_dir / "case_w1-005.json").write_text("{not-valid-json", encoding="utf-8")

    exit_code = run_eval.main(
            [
            "--in-process",
            "--cases",
            str(W1_BATCH_PATH),
            "--run-id",
            "resume_run3",
            "--results-dir",
            str(tmp_path),
            "--case",
            "w1-005",
            "--resume",
        ]
    )

    assert exit_code == 0
    assert [call[0] for call in calls] == ["w1-005"]
    record = json.loads(
        (run_dir / "case_w1-005.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "completed"  # 已被新结果覆盖


# ---------------------------------------------------------------------------
# W3 子进程编排（默认模式）：硬超时兜底与磁盘读取 summary
# ---------------------------------------------------------------------------


def test_subprocess_orchestration_reads_records_from_disk(tmp_path, capsys):
    """编排模式：worker 写盘的 case 记录被编排方从磁盘读回并计入 summary"""
    import subprocess as sp

    def fake_run(cmd, **kwargs):
        # 模拟 worker：从命令行参数解析 case id，按正常契约写盘
        cid = cmd[cmd.index("--case") + 1]
        run_dir = Path(kwargs["cwd"]) if "cwd" in kwargs else Path.cwd()
        # 编排方未传 cwd，用 --results-dir 定位
        rdir = Path(cmd[cmd.index("--results-dir") + 1])
        case_file = rdir / cid_run(cmd) / f"case_{cid}.json"
        case_file.parent.mkdir(parents=True, exist_ok=True)
        case_file.write_text(
            json.dumps(
                {
                    "case_id": cid,
                    "query": "q",
                    "session_id": f"s-{cid}",
                    "status": "completed",
                    "duration_s": 1.0,
                    "token_usage": {"total_tokens": 100},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return sp.CompletedProcess(cmd, 0)

    def cid_run(cmd):
        return cmd[cmd.index("--run-id") + 1]

    import unittest.mock as mock

    with mock.patch.object(run_eval.subprocess, "run", side_effect=fake_run):
        exit_code = run_eval.main(
            [
                "--cases",
                str(W1_BATCH_PATH),
                "--run-id",
                "orch_run",
                "--results-dir",
                str(tmp_path),
                "--case",
                "w1-001",
                "--case",
                "w1-002",
            ]
        )

    assert exit_code == 0
    summary = json.loads(
        (tmp_path / "orch_run" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["cases_run"] == 2
    assert summary["cases_completed"] == 2
    assert summary["total_tokens"] == 200
    assert [c["case_id"] for c in summary["per_case"]] == ["w1-001", "w1-002"]


def test_subprocess_hard_timeout_writes_synthetic_record(tmp_path):
    """worker 卡死被强杀：编排方补写 timeout 记录且批处理继续、summary 一致"""
    import subprocess as sp
    from unittest import mock

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[cmd.index("--case") + 1])
        if len(calls) == 1:
            raise sp.TimeoutExpired(cmd, 999)
        # 第二条正常写盘
        rdir = Path(cmd[cmd.index("--results-dir") + 1])
        cid = cmd[cmd.index("--case") + 1]
        case_file = rdir / cmd[cmd.index("--run-id") + 1] / f"case_{cid}.json"
        case_file.parent.mkdir(parents=True, exist_ok=True)
        case_file.write_text(
            json.dumps(
                {
                    "case_id": cid,
                    "query": "q",
                    "session_id": f"s-{cid}",
                    "status": "completed",
                    "duration_s": 2.0,
                    "token_usage": None,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return sp.CompletedProcess(cmd, 0)

    with mock.patch.object(run_eval.subprocess, "run", side_effect=fake_run):
        exit_code = run_eval.main(
            [
                "--cases",
                str(W1_BATCH_PATH),
                "--run-id",
                "hard_timeout_run",
                "--results-dir",
                str(tmp_path),
                "--case",
                "w1-003",
                "--case",
                "w1-004",
                "--grace",
                "5",
            ]
        )

    assert exit_code == 0
    run_dir = tmp_path / "hard_timeout_run"
    timed_out = json.loads(
        (run_dir / "case_w1-003.json").read_text(encoding="utf-8")
    )
    assert timed_out["status"] == "timeout"
    assert "硬超时" in timed_out["error"]
    assert timed_out["token_usage"] is None
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["cases_run"] == 2
    assert summary["cases_completed"] == 1
    assert summary["cases_failed"] == 1
    by_id = {c["case_id"]: c for c in summary["per_case"]}
    assert by_id["w1-003"]["status"] == "timeout"
    assert by_id["w1-004"]["status"] == "completed"


def test_subprocess_hard_timeout_preserves_existing_record(tmp_path):
    """W3 实测回归：worker 已落盘完成但收尾挂起被硬超时终止时，
    不得用 timeout 占位记录覆盖磁盘上的真实结果"""
    import subprocess as sp
    from unittest import mock

    run_dir = tmp_path / "keep_run"
    run_dir.mkdir(parents=True)
    # 预置磁盘已有 completed 记录（模拟 worker 已落盘、进程未退出）
    (run_dir / "case_w1-003.json").write_text(
        json.dumps(
            {
                "case_id": "w1-003",
                "query": "q",
                "session_id": "s-w1-003",
                "status": "completed",
                "duration_s": 378.69,
                "token_usage": {"total_tokens": 578632},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_run(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd, 999)

    with mock.patch.object(run_eval.subprocess, "run", side_effect=fake_run):
        exit_code = run_eval.main(
            [
                "--cases",
                str(W1_BATCH_PATH),
                "--run-id",
                "keep_run",
                "--results-dir",
                str(tmp_path),
                "--case",
                "w1-003",
                "--grace",
                "5",
            ]
        )

    assert exit_code == 0
    record = json.loads((run_dir / "case_w1-003.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"  # 保留，未被 timeout 覆盖
    assert record["token_usage"]["total_tokens"] == 578632
