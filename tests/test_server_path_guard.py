"""
server.py 文件路径安全边界（_resolve_within_output）的单元测试

下载、应用内预览、文件列表三个接口共用这条校验：
- 只放行 output 目录内的路径，越界返回 403
- 解析异常的无效路径返回 400
"""

import pytest
from fastapi import HTTPException

import app.api.server as server
from app.api.server import _resolve_within_output


def test_path_inside_output_is_allowed():
    target = server.output_dir / "session_guard" / "报告.pdf"
    assert _resolve_within_output(str(target)) == target.resolve()


def test_output_dir_itself_is_allowed():
    """output 目录本身视为边界内（is_relative_to 含相等）"""
    assert _resolve_within_output(str(server.output_dir)) == server.output_dir.resolve()


def test_path_outside_output_rejected_with_403(tmp_path):
    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_output(str(tmp_path / "secret.md"))
    assert exc_info.value.status_code == 403


def test_parent_traversal_outside_output_rejected_with_403():
    """../ 穿越后落在 output 之外，同样拒绝"""
    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_output(str(server.output_dir / ".." / "secret.md"))
    assert exc_info.value.status_code == 403


def test_invalid_path_rejected_with_400():
    """路径含 NUL 字节时 resolve 抛 ValueError，应归一为 400 无效路径"""
    with pytest.raises(HTTPException) as exc_info:
        _resolve_within_output("a\x00b")
    assert exc_info.value.status_code == 400
