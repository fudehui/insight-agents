"""
generate_markdown 非闸门分支的补充测试（W3 覆盖率补测）

覆盖：.md 后缀自动补全、path 参数合成子目录、resolve_path 越界拒绝、
子目录自动创建、文件写入异常转错误提示。闸门逻辑见 test_markdown_gate.py。
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
def md_env(tmp_path):
    dir_token = set_session_context(str(tmp_path))
    thread_token = set_thread_context("md_extra_session")
    source_registry.reset_task_sources("md_extra_session")

    with mock.patch.object(markdown_tools.monitor, "report_tool"):
        yield tmp_path

    reset_session_context(dir_token, thread_token)


def test_filename_without_suffix_gets_md_appended(md_env):
    result = generate_markdown.invoke(
        {"content": "# 笔记", "filename": "r"}
    )

    assert "已成功生成" in result
    assert (md_env / "r.md").exists()


def test_path_param_composes_subdirectory(md_env):
    result = generate_markdown.invoke(
        {"content": "# 笔记", "filename": "r.md", "path": "reports"}
    )

    assert "已成功生成" in result
    assert (md_env / "reports" / "r.md").exists()


def test_dot_path_uses_session_root(md_env):
    # path="." 是模型常见的"当前目录"写法，应等价于不传
    result = generate_markdown.invoke(
        {"content": "# 笔记", "filename": "r.md", "path": "."}
    )

    assert "已成功生成" in result
    assert (md_env / "r.md").exists()


def test_path_escape_rejected_by_resolve_path(md_env):
    result = generate_markdown.invoke(
        {"content": "# 笔记", "filename": "../escape.md"}
    )

    assert "错误：" in result
    assert not (md_env.parent / "escape.md").exists()


def test_write_failure_returns_error_message(md_env, monkeypatch):
    def boom(self, *args, **kwargs):
        raise OSError("磁盘已满")

    monkeypatch.setattr("pathlib.Path.write_text", boom)

    result = generate_markdown.invoke(
        {"content": "# 笔记", "filename": "r.md"}
    )

    assert "生成Markdown文件失败" in result
    assert "磁盘已满" in result
