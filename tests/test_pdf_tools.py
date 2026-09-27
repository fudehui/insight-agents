"""
Markdown 转 PDF 工具（app/tools/pdf_tools.py）的单元测试

覆盖：会话目录内正常转换（缺省/显式 PDF 文件名、自动补 .md 后缀）、
源文件不存在、路径越界异常的统一包装。monitor 埋点与上下文按现有测试
约定隔离，避免污染 output 目录
"""

from unittest import mock

import pytest

from app.api.context import reset_session_context, set_session_context
from app.tools import pdf_tools


@pytest.fixture
def session_env(tmp_path):
    """独立的会话目录 + 屏蔽 monitor 埋点，避免测试输出污染事件日志"""
    token = set_session_context(str(tmp_path))
    with mock.patch.object(pdf_tools.monitor, "report_tool"):
        yield tmp_path
    reset_session_context(token)


def test_tool_converts_session_md_to_default_pdf(session_env):
    """未指定 PDF 文件名时与源 Markdown 同目录同名"""
    (session_env / "报告.md").write_text("# 标题\n\n正文内容", encoding="utf-8")

    result = pdf_tools.convert_md_to_pdf.invoke({"md_filename": "报告.md"})

    pdf_path = session_env / "报告.pdf"
    assert result.startswith("成功转换")
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
    assert pdf_path.read_bytes().startswith(b"%PDF")


def test_tool_appends_md_suffix_automatically(session_env):
    """md_filename 缺少 .md 后缀时自动补全"""
    (session_env / "笔记.md").write_text("# 笔记", encoding="utf-8")

    result = pdf_tools.convert_md_to_pdf.invoke({"md_filename": "笔记"})

    assert result.startswith("成功转换")
    assert (session_env / "笔记.pdf").exists()


def test_tool_uses_explicit_pdf_filename(session_env):
    """显式指定 PDF 文件名（含子目录）时输出到对应位置"""
    (session_env / "源.md").write_text("# 源", encoding="utf-8")

    result = pdf_tools.convert_md_to_pdf.invoke(
        {"md_filename": "源.md", "pdf_filename": "输出/结果.pdf"}
    )

    assert result.startswith("成功转换")
    assert (session_env / "输出" / "结果.pdf").exists()


def test_tool_returns_error_for_missing_md_file(session_env):
    """源文件不存在时返回错误说明，且不产出 PDF"""
    result = pdf_tools.convert_md_to_pdf.invoke({"md_filename": "不存在.md"})

    assert result.startswith("错误：文件不存在")
    assert not (session_env / "不存在.pdf").exists()


def test_tool_wraps_path_resolution_failure(session_env):
    """路径越出会话目录时 resolve_path 抛 ValueError，统一包装为失败说明"""
    result = pdf_tools.convert_md_to_pdf.invoke({"md_filename": "../outside.md"})

    assert result.startswith("转换失败")
