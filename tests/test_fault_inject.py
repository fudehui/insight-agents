"""
故障注入演练框架的单元测试（W3-W4，全程 mock-llm 零 token）

覆盖：ScriptedChatModel 的顺序/循环/抛错行为、指标计算纯函数、
断网场景端到端（进程内，patch checkpoint 到 tmp）、审批中重启场景
端到端（真实两段子进程，mock-llm）、WS 断连重连对账场景端到端
（进程内）、任务执行中重启场景端到端（真实两段子进程）。
"""

import json
from pathlib import Path

import requests

import pytest

from evals.fault_inject import scenarios
from evals.fault_inject.mock_llm import RESEARCH_SCRIPT, ScriptedChatModel, _FakeTavilyClient


# ---------------------------------------------------------------------------
# ScriptedChatModel / FakeTavily 单元
# ---------------------------------------------------------------------------


def test_scripted_model_cycles_through_script():
    model = ScriptedChatModel(RESEARCH_SCRIPT, cycle=True)
    for _ in range(2):  # 脚本耗尽后从头循环
        for expected in RESEARCH_SCRIPT:
            result = model._generate([])
            message = result.generations[0].message
            if expected[0] == "tool":
                assert message.tool_calls[0]["name"] == expected[1]
                assert message.tool_calls[0]["args"] == expected[2]
            else:
                assert message.content == expected[1]


def test_scripted_model_raises_on_error_step():
    model = ScriptedChatModel([("error", ConnectionError("断网"))], cycle=False)
    with pytest.raises(ConnectionError):
        model._generate([])


def test_fake_tavily_fails_first_n_then_recovers():
    client = _FakeTavilyClient(fail_first_n=1)
    # requests 的 ConnectionError：call_guard 重试白名单认的类型
    with pytest.raises(requests.exceptions.ConnectionError):
        client.search(query="x")
    result = client.search(query="x")  # 第二次恢复
    assert result["results"][0]["url"].startswith("https://")


# ---------------------------------------------------------------------------
# 指标计算纯函数
# ---------------------------------------------------------------------------


def test_event_completion_rate():
    events = [{"seq": i} for i in range(5)]
    assert scenarios.event_completion_rate(events) == 1.0
    # seq 从 1 起连续（实测 monitor 口径）→ 1.0
    assert scenarios.event_completion_rate([{"seq": i} for i in (1, 2, 3, 4, 5)]) == 1.0
    # 缺 seq=3：4/5
    assert scenarios.event_completion_rate([{"seq": i} for i in (1, 2, 4, 5)]) == 0.8
    # 重复 seq 不虚高
    assert scenarios.event_completion_rate([{"seq": 1}, {"seq": 1}, {"seq": 2}]) == 1.0
    assert scenarios.event_completion_rate([]) == 0.0


def test_tool_recovery_time_finds_fail_to_success_pair():
    events = [
        {"event": "tool_end", "message": "工具执行结束: 网络搜索工具：internet_search（failed）",
         "data": {"tool_name": "网络搜索工具：internet_search"}, "timestamp": "2026-09-21T10:00:00"},
        {"event": "tool_end", "message": "工具执行结束: 网络搜索工具：internet_search（success）",
         "data": {"tool_name": "网络搜索工具：internet_search"}, "timestamp": "2026-09-21T10:00:02.5"},
    ]
    assert scenarios.tool_recovery_time(events, "internet_search") == 2.5


def test_aggregate_metrics():
    runs = [
        {"recovery_success": True, "recovery_time_s": 1.0, "event_completion_rate": 1.0},
        {"recovery_success": False, "recovery_time_s": None, "event_completion_rate": 0.8},
        {"recovery_success": True, "recovery_time_s": 3.0, "event_completion_rate": 1.0},
    ]
    summary = scenarios.aggregate(runs)
    assert summary["runs"] == 3
    assert summary["recovery_success_rate"] == pytest.approx(0.6667)
    assert summary["recovery_time_avg_s"] == 2.0
    assert summary["recovery_time_p95_s"] == 3.0  # n<20 时 P95=最大值
    assert summary["event_completion_rate_avg"] == pytest.approx(0.9333)


def test_reconnect_backfill_rate():
    disk = [{"seq": i} for i in range(1, 7)]
    # 断连窗口（seq>3）全部可取回 → 1.0
    assert scenarios.reconnect_backfill_rate(disk, disk, cutoff_seq=3) == 1.0
    # 丢 seq=5：窗口内 3 条取回 2 条（比率按口径四舍五入到 4 位）
    partial = [e for e in disk if e["seq"] != 5]
    assert scenarios.reconnect_backfill_rate(disk, partial, cutoff_seq=3) == 0.6667
    # 窗口内无落盘事件 → None（口径：无窗口则补齐率不可判定）
    assert scenarios.reconnect_backfill_rate([{"seq": 1}], [{"seq": 1}], cutoff_seq=1) is None
    # 不带 seq 的事件（如只推 WS 不落盘的 task_delta）不参与窗口统计
    assert scenarios.reconnect_backfill_rate([{"event": "task_delta"}], [], cutoff_seq=0) is None


def test_resume_span_of():
    events = [
        {"event": "task_start", "seq": 1, "timestamp": "2026-09-24T10:00:00"},
        {"event": "tool_start", "seq": 5, "timestamp": "2026-09-24T10:01:00"},
        {"event": "task_result", "seq": 6, "timestamp": "2026-09-24T10:01:02.5"},
    ]
    # 恢复耗时 = 恢复进程首事件（seq>base_seq）→ task_result
    assert scenarios.resume_span_of(events, base_seq=4) == 2.5
    # 恢复进程未产出任何事件 → None
    assert scenarios.resume_span_of(events, base_seq=6) is None
    # 恢复进程未产出 task_result → None
    no_result = [e for e in events if e.get("event") != "task_result"]
    assert scenarios.resume_span_of(no_result, base_seq=4) is None


# ---------------------------------------------------------------------------
# 断网场景端到端（进程内，checkpoint 隔离到 tmp）
# ---------------------------------------------------------------------------


@pytest.fixture
def chaos_checkpoint(tmp_path, monkeypatch):
    """把 checkpoints.db 指到 tmp，避免演练污染真实检查点库"""
    import app.agent.main_agent as main_agent

    monkeypatch.setattr(main_agent, "project_root_path", tmp_path)
    db = tmp_path / "chaos_checkpoints.db"
    monkeypatch.setattr(main_agent, "_checkpoint_db_path", db)
    # 测试进程内可能已有别的测试初始化过 _checkpointer（绑定旧库），重置
    monkeypatch.setattr(main_agent, "_checkpointer", None)
    monkeypatch.setattr(main_agent, "_agents", {})
    yield db
    # 关闭演练创建的 aiosqlite 连接（非 daemon 线程，不关会挂住 pytest 退出）
    from evals.fault_inject.scenarios import _close_checkpoint

    _close_checkpoint()


def test_disconnect_scenario_end_to_end(tmp_path, chaos_checkpoint):
    summary = scenarios.run_disconnect_scenario(1, tmp_path / "disconnect")

    assert summary["scenario"] == "disconnect"
    assert summary["runs"] == 1
    assert summary["recovery_success_rate"] == 1.0
    assert summary["event_completion_rate_avg"] == 1.0
    run_file = tmp_path / "disconnect" / "run_1.json"
    record = json.loads(run_file.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert record["recovery_success"] is True
    # fake Tavily 第一次抛错、第二次恢复：重试真实发生
    assert record["tavily_search_calls"] >= 2


# ---------------------------------------------------------------------------
# WS 断连重连对账场景端到端（进程内，checkpoint 隔离到 tmp）
# ---------------------------------------------------------------------------


def test_ws_disconnect_scenario_end_to_end(tmp_path, chaos_checkpoint):
    summary = scenarios.run_ws_disconnect_scenario(1, tmp_path / "ws_disconnect")

    assert summary["scenario"] == "ws_disconnect"
    assert summary["runs"] == 1
    assert summary["recovery_success_rate"] == 1.0
    assert summary["event_completion_rate_avg"] == 1.0

    record = json.loads(
        (tmp_path / "ws_disconnect" / "run_1.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "completed"  # 服务端任务不受断连影响照常收尾
    assert record["recovery_success"] is True
    assert record["recovery_time_s"] is not None  # 对账调用耗时（毫秒级）
    assert record["cutoff_seq"] == 3  # 断连点 = task_start 后第 2 条
    assert record["window_events"] > 0  # 断连窗口内确有事件产生
    assert record["backfill_rate"] == 1.0  # 窗口内事件重连后 100% 补齐
    assert record["replay_consistent"] is True  # 对账结果与落盘全量一致
    assert record["replayed_events"] == record["events_count"] > 0


# ---------------------------------------------------------------------------
# 审批中重启场景端到端（真实两段子进程，mock-llm）
# ---------------------------------------------------------------------------


def test_restart_mid_approval_end_to_end(tmp_path):
    workdir = tmp_path / "restart"
    summary = scenarios.run_restart_scenario(1, workdir, timeout_s=180.0)

    assert summary["scenario"] == "restart_mid_approval"
    assert summary["runs"] == 1
    assert summary["recovery_success_rate"] == 1.0

    record = json.loads((workdir / "run_1" / "record.json").read_text(encoding="utf-8"))
    assert record["interrupted"] is True  # stage1 确实停在审批中断
    assert record["recovery_success"] is True  # stage2 新进程恢复成功
    assert record["recovery_time_s"] is not None
    assert record["event_completion_rate"] == 1.0  # 重启后事件无缺口


# ---------------------------------------------------------------------------
# 任务执行中重启场景端到端（真实两段子进程：强杀 + None input 续跑）
# ---------------------------------------------------------------------------


def test_process_restart_mid_task_end_to_end(tmp_path):
    workdir = tmp_path / "proc_restart"
    summary = scenarios.run_process_restart_scenario(1, workdir, timeout_s=180.0)

    assert summary["scenario"] == "process_restart_mid_task"
    assert summary["runs"] == 1
    assert summary["recovery_success_rate"] == 1.0
    assert summary["event_completion_rate_avg"] == 1.0

    record = json.loads((workdir / "run_1" / "record.json").read_text(encoding="utf-8"))
    assert record["killed"] is True  # stage1 确实死在任务执行中（非审批中断点）
    assert record["stage1_last_seq"] > 0  # 死亡前事件已落盘
    assert record["recovery_success"] is True  # stage4 从 checkpoint 续跑出 task_result
    assert record["recovery_time_s"] is not None
    assert record["event_completion_rate"] == 1.0  # 跨进程死亡事件无缺口

    # 全量事件 seq 从 1 起连续（stage3 死亡与 stage4 续跑两段无缝拼接）
    events = scenarios.read_events(
        workdir / "output" / "session_chaos_proc_restart_run1"
    )
    seqs = [e["seq"] for e in events if isinstance(e.get("seq"), int)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert any(e.get("event") == "task_result" for e in events)
