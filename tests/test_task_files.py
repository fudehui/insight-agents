"""
任务文件快照与 diff 逻辑的单元测试

验证：任务结束后能准确找出新增和被覆写的文件，events.jsonl 不计入
"""

from app.agent.main_agent import _diff_task_files, _snapshot_session_files


def test_diff_finds_new_and_modified_files(tmp_path):
    (tmp_path / "old_report.md").write_text("旧文件", encoding="utf-8")
    baseline = _snapshot_session_files(tmp_path)
    assert set(baseline) == {"old_report.md"}

    # 模拟本次任务：修改旧文件 + 新建两个文件 + 事件日志持续追加
    (tmp_path / "old_report.md").write_text("覆写后的内容变长了", encoding="utf-8")
    (tmp_path / "new_report.md").write_text("新报告", encoding="utf-8")
    (tmp_path / "new_report.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "events.jsonl").write_text('{"event": "tool_start"}\n', encoding="utf-8")

    # Windows 文件时间戳按系统时钟跳变（约 15.6ms 一跳），毫秒内连续写入可能
    # 得到相同 mtime，导致"覆写"无法通过 mtime 差异识别；显式错开时间戳保证确定性
    import os

    st = (tmp_path / "old_report.md").stat()
    os.utime(tmp_path / "old_report.md", (st.st_atime + 10, st.st_mtime + 10))

    generated = _diff_task_files(tmp_path, baseline)
    names = {item["name"] for item in generated}

    # events.jsonl 由 monitor 追加产生，不属于任务产物
    assert names == {"old_report.md", "new_report.md", "new_report.pdf"}
    # 每个条目结构与 /api/files 一致，前端可直接渲染
    item = next(i for i in generated if i["name"] == "new_report.md")
    assert item["type"] == "file"
    assert item["size"] > 0
    assert str(tmp_path) in item["path"]


def test_diff_empty_when_nothing_changed(tmp_path):
    (tmp_path / "a.md").write_text("内容", encoding="utf-8")
    baseline = _snapshot_session_files(tmp_path)

    assert _diff_task_files(tmp_path, baseline) == []


def test_diff_sorted_by_mtime_desc(tmp_path):
    import os

    first = tmp_path / "1.md"
    second = tmp_path / "2.md"
    first.write_text("a", encoding="utf-8")
    os.utime(first, (1000, 1000))
    second.write_text("b", encoding="utf-8")
    os.utime(second, (2000, 2000))

    generated = _diff_task_files(tmp_path, {})
    assert [item["name"] for item in generated] == ["2.md", "1.md"]
