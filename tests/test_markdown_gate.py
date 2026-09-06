"""
Markdown 生成质量闸门的单元测试

覆盖：缺来源拒绝写入、带来源正常写入、RAGFlow 文档来源章节检查、
两次拒绝后自动附注兜底、无外部来源任务不受影响
"""

from unittest import mock

import pytest

from app.api import source_registry
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.tools import markdown_tools
from app.tools.markdown_tools import generate_markdown


@pytest.fixture
def gate_env(tmp_path):
    """独立的会话目录 + 任务上下文；屏蔽 monitor 落盘避免污染 output 目录"""
    dir_token = set_session_context(str(tmp_path))
    thread_token = set_thread_context("gate_test_session")
    source_registry.reset_task_sources("gate_test_session")

    with mock.patch.object(markdown_tools.monitor, "report_tool"):
        yield tmp_path

    # reset_session_context 的第二个参数即 thread token，一次完成两项恢复
    reset_session_context(dir_token, thread_token)


def test_gate_rejects_report_without_web_link(gate_env):
    source_registry.register_web_sources([{"title": "报告A", "url": "https://e.com/a"}])

    result = generate_markdown.invoke(
        {"content": "# 行业分析\n正文没有任何链接", "filename": "r.md"}
    )

    assert "缺少来源引用" in result
    assert "https://e.com/a" in result  # 来源清单原样递给模型
    assert not (gate_env / "r.md").exists()  # 文件未写入


def test_gate_passes_report_with_web_link(gate_env):
    source_registry.register_web_sources([{"title": "报告A", "url": "https://e.com/a"}])

    result = generate_markdown.invoke(
        {
            "content": "# 行业分析\n见 [报告A](https://e.com/a)\n\n【参考来源】\n1. [报告A](https://e.com/a)",
            "filename": "r.md",
        }
    )

    assert "已成功生成" in result
    assert (gate_env / "r.md").exists()


def test_gate_requires_source_section_for_docs(gate_env):
    source_registry.register_doc_source("白皮书.pdf", "3")

    result = generate_markdown.invoke(
        {"content": "# 分析\n只有文字没有来源章节", "filename": "r.md"}
    )

    assert "缺少来源引用" in result
    assert "RAGFlow 文档来源标注" in result
    assert not (gate_env / "r.md").exists()


def test_gate_fallback_appends_sources_after_two_rejections(gate_env):
    source_registry.register_web_sources([{"title": "报告A", "url": "https://e.com/a"}])

    first = generate_markdown.invoke(
        {"content": "无链接正文第一版", "filename": "r.md"}
    )
    assert "缺少来源引用" in first
    assert not (gate_env / "r.md").exists()

    second = generate_markdown.invoke(
        {"content": "无链接正文第二版", "filename": "r.md"}
    )
    assert "自动附注" in second
    written = (gate_env / "r.md").read_text(encoding="utf-8")
    assert "## 参考来源（系统自动附注）" in written
    assert "[报告A](https://e.com/a)" in written


def test_gate_not_triggered_without_external_sources(gate_env):
    # 纯本地任务（无网络/知识库来源）不受闸门影响
    result = generate_markdown.invoke(
        {"content": "# 笔记\n普通内容，无需引用", "filename": "r.md"}
    )

    assert "已成功生成" in result
    assert (gate_env / "r.md").exists()


def test_monitor_logs_preview_not_full_content(gate_env):
    # 修复验证：埋点只记录长度和预览，不再写入整份 content
    with mock.patch.object(markdown_tools.monitor, "report_tool") as report_tool:
        generate_markdown.invoke(
            {"content": "x" * 5000, "filename": "r.md"},
        )

    args = report_tool.call_args[0][1]
    assert args["内容长度(字符)"] == 5000
    assert len(args["内容预览"]) == 100
