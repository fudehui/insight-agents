"""
任务来源清单事件（溯源信息推送）的单元测试

验证：report_task_sources 输出 task_sources 事件并落盘 events.jsonl，
事件数据与 source_registry 登记内容一致
"""

import json

from app.api.context import set_thread_context
from app.api.monitor import monitor
from app.api.source_registry import (
    get_task_sources,
    register_web_sources,
    reset_task_sources,
)


def test_report_task_sources_emits_and_persists(tmp_path, monkeypatch):
    thread_id = "test_task_sources_001"
    reset_task_sources(thread_id)

    # 走真实的 ContextVar 机制：register_web_sources 和 _emit 都按 thread_id 取上下文
    set_thread_context(thread_id)
    register_web_sources([{"title": "示例标题", "url": "https://example.com/a"}])

    # 落盘目录指向 tmp_path，避免测试写入真实 output/
    import app.api.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "_output_dir", tmp_path)
    (tmp_path / f"session_{thread_id}").mkdir()

    monitor.report_task_sources(get_task_sources())

    events_file = tmp_path / f"session_{thread_id}" / "events.jsonl"
    assert events_file.exists()

    payloads = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    task_sources = [p for p in payloads if p.get("event") == "task_sources"]
    assert len(task_sources) == 1

    data = task_sources[0]["data"]
    assert data["web"] == [{"title": "示例标题", "url": "https://example.com/a"}]
    assert data["docs"] == []
    assert data["sql"] == []
    assert "共引用 1 个来源" in task_sources[0]["message"]


def test_empty_sources_message_has_zero_counts(tmp_path, monkeypatch):
    thread_id = "test_task_sources_002"
    reset_task_sources(thread_id)

    set_thread_context(thread_id)

    import app.api.monitor as monitor_module

    monkeypatch.setattr(monitor_module, "_output_dir", tmp_path)
    (tmp_path / f"session_{thread_id}").mkdir()

    # 全空来源也允许推送（main_agent 侧已做空判断，这里只验证事件本身可渲染）
    monitor.report_task_sources(get_task_sources())

    events_file = tmp_path / f"session_{thread_id}" / "events.jsonl"
    payloads = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
    ]
    task_sources = [p for p in payloads if p.get("event") == "task_sources"]
    assert len(task_sources) == 1
    assert "0 个来源" in task_sources[0]["message"]
