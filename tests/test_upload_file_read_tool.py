"""
文件读取工具截断与分段读取的单元测试

read_file_content 对文本类内容（.md/.txt/.docx/.pdf 及未知后缀兜底）设置了
单次返回上限：超限截断并附续读提示，模型可用 offset 参数继续读取。
测试用 monkeypatch 把上限调小，避免真正写入超大文件
"""

from app.api.context import reset_session_context, set_session_context
from app.tools import upload_file_read_tool
from app.tools.upload_file_read_tool import read_file_content

_TRUNCATION_MARK = "文件内容未完"


def _read(filename: str, offset: int = 0) -> str:
    """在临时会话目录上下文中调用工具；无 thread 上下文时监控只打印不落盘"""
    return read_file_content.invoke({"filename": filename, "offset": offset})


def test_small_file_returns_full_content(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_file_read_tool, "_MAX_TEXT_CHARS", 10)
    token = set_session_context(str(tmp_path))
    try:
        (tmp_path / "小文件.md").write_text("完整内容", encoding="utf-8")
        result = _read("小文件.md")
    finally:
        reset_session_context(token)

    assert result == "完整内容"
    assert _TRUNCATION_MARK not in result


def test_large_file_is_truncated_with_resume_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_file_read_tool, "_MAX_TEXT_CHARS", 10)
    token = set_session_context(str(tmp_path))
    try:
        (tmp_path / "大文件.md").write_text("0123456789ABCDEF", encoding="utf-8")
        result = _read("大文件.md")
    finally:
        reset_session_context(token)

    # 前上限个字符原样返回，结尾提示总长度与下一次的 offset
    assert result.startswith("0123456789")
    assert _TRUNCATION_MARK in result
    assert "共 16 个字符" in result
    assert "offset=10" in result


def test_offset_resumes_from_previous_position(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_file_read_tool, "_MAX_TEXT_CHARS", 10)
    token = set_session_context(str(tmp_path))
    try:
        (tmp_path / "大文件.md").write_text("0123456789ABCDEF", encoding="utf-8")
        first = _read("大文件.md")
        second = _read("大文件.md", offset=10)
    finally:
        reset_session_context(token)

    # 第二段从 offset 10 开始返回剩余内容，且不再附带续读提示
    assert first.endswith("offset=10") or "offset=10" in first
    assert second.startswith("（从第 10 个字符继续）")
    assert "ABCDEF" in second
    assert _TRUNCATION_MARK not in second


def test_offset_beyond_end_returns_out_of_range_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_file_read_tool, "_MAX_TEXT_CHARS", 10)
    token = set_session_context(str(tmp_path))
    try:
        (tmp_path / "大文件.md").write_text("0123456789ABCDEF", encoding="utf-8")
        result = _read("大文件.md", offset=999)
    finally:
        reset_session_context(token)

    assert "超出文件末尾" in result
