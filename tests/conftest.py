"""
pytest 全局配置：让测试可以从项目根目录导入 app 包
"""

import functools
import subprocess
import sys
from pathlib import Path

import pytest

# tests/ 的上级即项目根目录，加入 sys.path 后测试可直接 import app.*
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# Docker 沙箱安全回归（P0-2）：conftest 级 skipif
# tests/test_sandbox_escape.py 的逃逸验证需要本机 Docker（docker CLI 可用），
# docker info 失败时整文件跳过，避免无 Docker 环境下误报失败。
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _docker_cli_available() -> bool:
    """探测 docker CLI 是否可用（docker info 退出码 0），进程内只探测一次"""
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def pytest_collection_modifyitems(config, items):
    """docker info 失败 -> 沙箱逃逸验证文件整体 skip（conftest 级 skipif）"""
    # 仅当逃逸验证文件被收集时才探测 docker，其余会话零开销
    if not any(Path(str(item.fspath)).name == "test_sandbox_escape.py" for item in items):
        return
    if _docker_cli_available():
        return
    skip_docker = pytest.mark.skip(
        reason="需要本机 Docker（docker info 失败），跳过沙箱逃逸真实验证"
    )
    for item in items:
        if Path(str(item.fspath)).name == "test_sandbox_escape.py":
            item.add_marker(skip_docker)


def pytest_configure(config):
    """注册 slow 标记：真实 Docker 容器执行的慢速用例（需本机 Docker）"""
    config.addinivalue_line(
        "markers", "slow: 慢速测试（真实 Docker 容器执行），需要本机 Docker"
    )
