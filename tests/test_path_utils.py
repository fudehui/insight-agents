"""
路径解析工具（app/utils/path_utils.py）的单元测试

覆盖：沙箱虚拟前缀剥离、updated/ 上传目录解析与越界拒绝、无会话目录
时的项目根约束、会话目录内的相对/绝对路径解析、重复 session 名与
output 前缀的收敛修正、路径穿越拒绝
"""

import os
from pathlib import Path

import pytest

from app.utils import path_utils
from app.utils.path_utils import resolve_path


# ---------------------------------------------------------------------------
# 沙箱虚拟前缀剥离
# ---------------------------------------------------------------------------


def test_strips_sandbox_virtual_prefixes(tmp_path):
    """大模型常见的 /workspace 等沙箱前缀应剥离后落到会话目录内"""
    session = str(tmp_path)
    assert resolve_path("/workspace/report.md", session) == str(
        (tmp_path / "report.md").resolve()
    )
    assert resolve_path("/mnt/data/data.csv", session) == str(
        (tmp_path / "data.csv").resolve()
    )
    assert resolve_path("/home/user/notes/a.md", session) == str(
        (tmp_path / "notes" / "a.md").resolve()
    )


# ---------------------------------------------------------------------------
# updated/ 上传目录
# ---------------------------------------------------------------------------


def test_updated_paths_resolve_to_project_upload_dir():
    """updated/ 前缀统一按项目根目录下的上传目录解析"""
    result = resolve_path("updated/上传文件.md")
    expected = (path_utils._project_root() / "updated" / "上传文件.md").resolve()
    assert result == str(expected)


def test_updated_prefix_found_mid_path():
    """路径中间出现 updated/ 时从该处截取解析"""
    result = resolve_path("some/prefix/updated/a.md")
    expected = (path_utils._project_root() / "updated" / "a.md").resolve()
    assert result == str(expected)


def test_updated_escape_rejected():
    """updated/../.. 形式的穿越必须被拒绝"""
    with pytest.raises(ValueError, match="越出上传目录范围"):
        resolve_path("updated/../secret.md")


# ---------------------------------------------------------------------------
# 无会话目录：以项目根为边界
# ---------------------------------------------------------------------------


def test_without_session_dir_relative_path_resolves_to_project_root():
    """无会话上下文时，相对路径以项目根目录为基准"""
    result = resolve_path("README.md")
    expected = (path_utils._project_root() / "README.md").resolve()
    assert result == str(expected)


def test_without_session_dir_absolute_inside_project_allowed():
    """无会话上下文时，项目根内的绝对路径放行"""
    app_file = Path(path_utils.__file__).resolve()
    assert resolve_path(str(app_file)) == str(app_file)


def test_without_session_dir_outside_project_rejected(tmp_path):
    with pytest.raises(ValueError, match="越出项目目录范围"):
        resolve_path(str(tmp_path / "outside.md"))


def test_without_session_dir_parent_escape_rejected():
    with pytest.raises(ValueError, match="越出项目目录范围"):
        resolve_path("../outside.md")


# ---------------------------------------------------------------------------
# 会话目录内的解析
# ---------------------------------------------------------------------------


def test_relative_path_resolves_inside_session(tmp_path):
    result = resolve_path("报告.md", str(tmp_path))
    assert result == str((tmp_path / "报告.md").resolve())


def test_duplicated_session_name_is_collapsed(tmp_path):
    """模型把 session 目录名重复拼进路径时收敛为会话目录下一层"""
    session_name = tmp_path.name
    result = resolve_path(f"{session_name}/报告.md", str(tmp_path))
    assert result == str((tmp_path / "报告.md").resolve())


def test_output_prefix_is_collapsed(tmp_path):
    """模型习惯性添加的 output/ 前缀收敛为会话目录下一层"""
    result = resolve_path("output/报告.md", str(tmp_path))
    assert result == str((tmp_path / "报告.md").resolve())


def test_absolute_path_inside_session_allowed(tmp_path):
    target = tmp_path / "sub" / "a.md"
    result = resolve_path(str(target), str(tmp_path))
    assert result == str(target.resolve())


def test_absolute_path_outside_session_rejected(tmp_path):
    with pytest.raises(ValueError, match="越出会话目录范围"):
        resolve_path(str(tmp_path.parent / "outside.md"), str(tmp_path))


def test_relative_escape_rejected(tmp_path):
    with pytest.raises(ValueError, match="越出会话目录范围"):
        resolve_path("../outside.md", str(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 将 /xxx 视为会话内相对路径")
def test_unix_absolute_path_without_drive_treated_as_session_relative(tmp_path):
    """Windows 下 "/xxx" 没有盘符，按会话目录内的相对路径处理"""
    result = resolve_path("/报告.md", str(tmp_path))
    assert result == str((tmp_path / "报告.md").resolve())


def test_nested_duplicated_session_dir_is_fixed(tmp_path):
    """session_xxx/session_xxx/file.md 这类重复嵌套收敛为 session_xxx/file.md"""
    session_name = tmp_path.name
    nested = tmp_path / session_name / "a.md"
    result = resolve_path(str(nested), str(tmp_path))
    assert result == str((tmp_path / "a.md").resolve())


def test_project_root_contains_the_module_itself():
    """_project_root 返回的目录应包含 path_utils 自身"""
    root = path_utils._project_root()
    assert (root / "app" / "utils" / "path_utils.py").is_file()
