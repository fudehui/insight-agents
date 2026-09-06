"""
文件路径解析工具

负责把模型或工具返回的虚拟路径、上传文件路径和相对路径统一转换为本地绝对路径
后续文件读取、Markdown 生成和 PDF 转换工具都可以复用这里的解析规则
"""

import os
from pathlib import Path
from typing import Optional


def resolve_path(filename: str, session_dir: Optional[str] = None) -> str:
    """
    解析文件路径，并强制限制在允许的目录范围内

    允许的根目录只有两类：
    1. 当前会话目录 session_dir（及其子路径）
    2. 项目根目录下的 updated/（用户上传文件的真实存放位置）

    任何解析后越出上述范围的路径（绝对路径、../ 穿越等）都会抛出 ValueError，
    由调用方（各文件工具）捕获后向模型返回错误提示。

    :param filename: 模型、工具或用户传入的文件名/路径
    :param session_dir: 当前任务的会话目录
    :return: 解析后的绝对路径
    :raises ValueError: 路径越出允许范围
    """
    path = Path(filename)
    path_str = filename.replace("\\", "/")

    # 大模型常返回 /workspace、/mnt/data 这类沙箱路径，本地项目需要先剥离虚拟前缀
    for prefix in ["/workspace", "/mnt/data", "/home/user"]:
        if path_str.startswith(prefix):
            cleaned = path_str[len(prefix) :].lstrip("/")
            path = Path(cleaned)
            path_str = str(path).replace("\\", "/")
            break

    # updated/ 用于存放用户上传文件，按项目根目录下的真实上传路径解析；
    # 解析后必须仍位于 updated/ 内，防止 updated/../.. 形式的穿越
    if "updated/" in path_str:
        idx = path_str.find("updated/")
        relative_part = path_str[idx:]
        candidate = Path(relative_part).resolve()
        allowed_updated_root = _project_root() / "updated"
        if not candidate.is_relative_to(allowed_updated_root):
            raise ValueError(f"路径 '{filename}' 越出上传目录范围，已拒绝解析")
        return str(candidate)

    if not session_dir:
        # 无会话上下文时退化为仅允许项目根目录内的相对路径
        candidate = (
            (_project_root() / path).resolve()
            if not path.is_absolute()
            else path.resolve()
        )
        if not candidate.is_relative_to(_project_root()):
            raise ValueError(f"路径 '{filename}' 越出项目目录范围，已拒绝解析")
        return str(candidate)

    session_path = Path(session_dir).resolve()
    session_name = session_path.name
    is_unix_abs = path_str.startswith("/")

    if path.is_absolute() or (os.name == "nt" and is_unix_abs):
        # Windows 下 "/xxx" 没有盘符，按会话目录内的相对路径处理
        if os.name == "nt" and is_unix_abs and not path.drive:
            full_path = session_path / path_str.lstrip("/")
        else:
            full_path = path.resolve()

        # 绝对路径只有落在会话目录内才放行，其余一律拒绝
        if not full_path.is_relative_to(session_path):
            raise ValueError(f"路径 '{filename}' 越出会话目录范围，已拒绝解析")
        return _fix_nested_session_path(full_path, session_path, session_name)

    parts = path.parts

    # 避免模型把 session 名或 output 前缀重复拼到当前会话目录里
    if session_name in parts:
        return str(session_path / path.name)

    if parts and parts[0] == "output":
        return str(session_path / path.name)

    # 相对路径（含 ../ 穿越）统一以会话目录为基准解析后再校验边界
    candidate = (session_path / path).resolve()
    if not candidate.is_relative_to(session_path):
        raise ValueError(f"路径 '{filename}' 越出会话目录范围，已拒绝解析")
    return str(candidate)


def _project_root() -> Path:
    """返回项目根目录（app/utils/path_utils.py 的上两级）。"""
    return Path(__file__).resolve().parents[2]


def _fix_nested_session_path(
    full_path: Path,
    session_path: Path,
    session_name: str,
) -> str:
    """
    修正 session_xxx/session_xxx/file.md 这类重复嵌套路径
    """
    parts = full_path.parts
    for index in range(len(parts) - 1):
        if parts[index] == session_name and parts[index + 1] == session_name:
            return str(session_path / full_path.name)
    return str(full_path)
