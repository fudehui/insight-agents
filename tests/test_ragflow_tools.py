"""
RAGFlow 工具改造的单元测试

覆盖三块：证据链格式化、流式回答与引用解析、临时会话泄漏修复。
全部用 Mock 模拟 SDK 客户端，不发起真实网络请求
"""

from unittest import mock

import pytest

from app.tools import ragflow_tools


# ---------- 证据链格式化 ----------


def test_format_with_full_reference():
    reference = {
        "chunks": [
            {
                "document_name": "白皮书.pdf",
                "positions": [[3, 10, 20, 30, 40]],
                "similarity": 0.87,
                "content": "第一段证据内容",
            },
            {
                "document_name": "研报.docx",
                "positions": [],
                "similarity": 0.5,
                "content": "第二段证据内容",
            },
        ]
    }

    text = ragflow_tools.format_answer_with_references("结论如下", reference)

    assert text.startswith("结论如下")
    assert "【引用来源】" in text
    assert "《白皮书.pdf》第3页" in text
    assert "相似度 0.87" in text
    assert "《研报.docx》" in text  # 无页码时不显示页码部分
    assert "第页" not in text


def test_format_without_reference_marks_unverified():
    text = ragflow_tools.format_answer_with_references("结论", None)
    assert "未返回引用信息" in text
    assert "谨慎引用" in text


def test_format_with_empty_chunks_marks_unverified():
    text = ragflow_tools.format_answer_with_references("结论", {"chunks": []})
    assert "未返回引用信息" in text


def test_format_truncates_many_chunks():
    chunks = [{"document_name": f"文档{i}.pdf", "content": "证据"} for i in range(8)]

    text = ragflow_tools.format_answer_with_references("结论", {"chunks": chunks})

    assert text.count("《文档") == 5
    assert "另有 3 个引用片段未展示" in text


def test_format_truncates_long_snippet():
    chunks = [{"document_name": "文档.pdf", "content": "长" * 500}]

    text = ragflow_tools.format_answer_with_references("结论", {"chunks": chunks})

    assert "长" * 101 not in text  # 片段被截断到 100 字符
    assert "..." in text


# ---------- 流式解析：回答拼接 + 引用捕获 ----------


def _stream_client(lines):
    """构造模拟的 RAGFlow 客户端：list_chats 返回单个助手，post 返回指定 SSE 流"""
    fake_chat = mock.Mock()
    fake_chat.id = "chat-1"
    fake_session = mock.Mock()
    fake_session.id = "session-1"
    fake_chat.create_session.return_value = fake_session

    fake_response = mock.Mock()
    fake_response.iter_lines.return_value = iter(lines)

    fake_client = mock.Mock()
    fake_client.list_chats.return_value = [fake_chat]
    fake_client.post.return_value = fake_response
    return fake_client, fake_chat


def test_ask_once_parses_answer_and_reference():
    # 累积式流式片段：每次 answer 是"截至当前的完整答案"
    lines = [
        'data:{"data": {"answer": "部分回答", '
        '"reference": {"chunks": [{"document_name": "d.pdf", "content": "证据"}]}}}',
        'data:{"data": {"answer": "部分回答，补充完整", '
        '"reference": {"chunks": [{"document_name": "d.pdf", "content": "证据"}]}}}',
        "data:[DONE]",
    ]
    fake_client, fake_chat = _stream_client(lines)

    with mock.patch.object(ragflow_tools, "ragflow_client", fake_client):
        result = ragflow_tools._ask_once("电商助手", "测试问题")

    assert result.startswith("部分回答，补充完整")
    assert "【引用来源】" in result
    assert "《d.pdf》" in result


def test_ask_once_handles_incremental_stream():
    # 增量式流式片段：answer 只包含新增内容，需要拼接
    lines = [
        'data:{"data": {"answer": "第一段"}}',
        'data:{"data": {"answer": "，第二段"}}',
        "data:[DONE]",
    ]
    fake_client, _ = _stream_client(lines)

    with mock.patch.object(ragflow_tools, "ragflow_client", fake_client):
        result = ragflow_tools._ask_once("电商助手", "测试问题")

    assert result.startswith("第一段，第二段")
    assert "未返回引用信息" in result


# ---------- 会话泄漏修复：异常路径也必须删除临时会话 ----------


def test_ask_once_deletes_session_on_stream_error():
    fake_chat = mock.Mock()
    fake_chat.id = "chat-1"
    fake_session = mock.Mock()
    fake_session.id = "session-1"
    fake_chat.create_session.return_value = fake_session

    fake_client = mock.Mock()
    fake_client.list_chats.return_value = [fake_chat]
    fake_response = mock.Mock()
    fake_response.iter_lines.side_effect = RuntimeError("stream broken")
    fake_client.post.return_value = fake_response

    with mock.patch.object(ragflow_tools, "ragflow_client", fake_client):
        with pytest.raises(RuntimeError):
            ragflow_tools._ask_once("电商助手", "测试问题")

    # 改造前流式中途抛异常会跳过 delete_sessions，造成 RAGFlow 侧会话泄漏
    fake_chat.delete_sessions.assert_called_once_with(ids=["session-1"])


def test_ask_once_deletes_session_on_success():
    lines = ['data:{"data": {"answer": "回答"}}', "data:[DONE]"]
    fake_client, fake_chat = _stream_client(lines)

    with mock.patch.object(ragflow_tools, "ragflow_client", fake_client):
        ragflow_tools._ask_once("电商助手", "测试问题")

    fake_chat.delete_sessions.assert_called_once()


def test_session_cleanup_failure_does_not_mask_result():
    # 清理本身失败时只记日志，不能影响提问结果
    lines = ['data:{"data": {"answer": "正常回答"}}', "data:[DONE]"]
    fake_client, fake_chat = _stream_client(lines)
    fake_chat.delete_sessions.side_effect = Exception("cleanup failed")

    with mock.patch.object(ragflow_tools, "ragflow_client", fake_client):
        result = ragflow_tools._ask_once("电商助手", "测试问题")

    assert result.startswith("正常回答")
