"""
沙箱代码执行工具的单元测试与真实冒烟

mock 子进程验证 docker run 命令的安全加固模板（网络隔离/资源限额/非 root/
只读 rootfs/去能力）逐项拼装正确、输出 256KB 硬截断、产物收口 sandbox/
子目录、磁盘不足拒绝、超时错误信息可引导模型；
末尾两条真实冒烟（@pytest.mark.slow + docker 缺失时跳过）验证端到端链路
与 WSL2 UID 写入/宿主可读（docs/upgrade-plan.md P0-2 硬要求）
"""

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import budget
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
)
from app.tools import sandbox_tools


def _invoke(args: dict) -> str:
    """run_code 是 async 定义，统一经 asyncio 同步调用"""
    return asyncio.run(sandbox_tools.run_code.ainvoke(args))


def _ok_process(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def session_env(tmp_path):
    """设置会话目录上下文，结束时报复上下文，避免串台"""
    token = set_session_context(str(tmp_path))
    try:
        yield tmp_path
    finally:
        reset_session_context(token)


@pytest.fixture
def no_docker_call(monkeypatch):
    """默认桩：任何 docker 子进程调用都记录参数并返回成功，便于断言命令拼装"""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _ok_process(stdout=b"demo output")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 命令拼装：安全加固模板逐项断言
# ---------------------------------------------------------------------------


def test_build_docker_command_contains_all_security_flags(tmp_path):
    cmd = sandbox_tools.build_docker_command(tmp_path, "demo.py")

    # 基础形态：一次性容器 + 网络隔离 + 资源限额（内存/CPU/进程数）
    assert cmd[1:3] == ["run", "--rm"]
    assert "--network=none" in cmd
    assert "--memory=512m" in cmd
    assert "--memory-swap=512m" in cmd
    assert "--cpus=1.0" in cmd
    assert "--pids-limit=64" in cmd
    # 只读 rootfs + 64MB tmpfs
    assert "--read-only" in cmd
    assert "--tmpfs" in cmd and cmd[cmd.index("--tmpfs") + 1] == "/tmp:rw,size=64m"
    # 去 Linux 能力 + 禁止提权
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges" in cmd
    # 非 root（nobody）
    assert "-u" in cmd and cmd[cmd.index("-u") + 1] == "65534:65534"
    # 会话目录挂载（Windows 路径转正斜杠）+ 工作目录收口 sandbox/ 子目录
    assert "-v" in cmd
    assert cmd[cmd.index("-v") + 1] == f"{tmp_path.as_posix()}:/work:rw"
    assert "--workdir" in cmd and cmd[cmd.index("--workdir") + 1] == "/work/sandbox"
    # 容器内 timeout 60 秒（-k 5 强杀宽限）+ python -B（不写 .pyc）
    assert "timeout" in cmd
    assert cmd[cmd.index("timeout") : cmd.index("timeout") + 4] == [
        "timeout",
        "-k",
        "5",
        "60",
    ]
    assert "python" in cmd and "-B" in cmd
    assert cmd[-1] == "/work/sandbox/demo.py"
    # 禁止向容器传入任何环境变量/密钥：不允许出现 -e/--env
    assert "-e" not in cmd
    assert "--env" not in cmd
    # 不允许提权参数
    assert "--privileged" not in cmd
    assert "--userns" not in cmd


def test_sandbox_image_env_override_supports_digest(monkeypatch, tmp_path):
    # 未设置环境变量时用默认镜像名
    assert sandbox_tools.sandbox_image() == "huiyan-sandbox:latest"
    cmd = sandbox_tools.build_docker_command(tmp_path, "a.py")
    assert "huiyan-sandbox:latest" in cmd

    # digest 锁定形态：SANDBOX_IMAGE 可覆盖为 @sha256:... （生产推荐）
    digest_ref = "huiyan-sandbox@sha256:" + "ab" * 32
    monkeypatch.setenv("SANDBOX_IMAGE", digest_ref)
    assert sandbox_tools.sandbox_image() == digest_ref
    cmd = sandbox_tools.build_docker_command(tmp_path, "a.py")
    assert digest_ref in cmd


# ---------------------------------------------------------------------------
# 写入路径：resolve_path 约束 + 产物收口 sandbox/ 子目录
# ---------------------------------------------------------------------------


def test_run_code_writes_script_into_sandbox_subdir(tmp_path, session_env, monkeypatch):
    def fake_run(cmd, **kwargs):
        return _ok_process(stdout=b"hello sandbox")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "print('hello sandbox')", "filename": "hello"})

    # 脚本必须落在 session_dir/sandbox/ 子目录且补齐 .py 后缀
    script = tmp_path / "sandbox" / "hello.py"
    assert script.is_file()
    assert script.read_text(encoding="utf-8") == "print('hello sandbox')"
    # 目录下不出现越出 sandbox/ 的散落脚本
    assert [p.name for p in tmp_path.iterdir()] == ["sandbox"]
    # stdout 回传给模型
    assert "hello sandbox" in result


def test_run_code_flattens_traversal_filename(tmp_path, session_env, no_docker_call):
    # 模型传入带目录成分的文件名：只取基础名，压平进 sandbox/，不允许落盘到他处
    result = _invoke({"code": "print(1)", "filename": "../escape.py"})

    assert (tmp_path / "sandbox" / "escape.py").is_file()
    assert not (tmp_path / "escape.py").exists()
    assert "沙箱脚本执行成功" in result


def test_run_code_enforces_resolve_path_constraint(tmp_path, session_env, monkeypatch):
    # 路径解析层如果拒绝（越界/非法路径），工具返回写入失败而不是继续执行
    def refusing_resolve_path(filename, session_dir=None):
        raise ValueError(f"路径 '{filename}' 越出会话目录范围，已拒绝解析")

    monkeypatch.setattr(sandbox_tools, "resolve_path", refusing_resolve_path)

    result = _invoke({"code": "print(1)"})

    assert "脚本写入失败" in result
    # 拒绝发生在建目录/写脚本之前
    assert not (tmp_path / "sandbox").exists()


def test_run_code_default_filename_is_deterministic_per_code(
    tmp_path, session_env, no_docker_call
):
    # 相同代码 -> 相同默认文件名（内容哈希），保证预算层去重指纹稳定
    _invoke({"code": "print('same code')"})
    _invoke({"code": "print('same code')"})

    scripts = sorted(p.name for p in (tmp_path / "sandbox").glob("*.py"))
    assert len(scripts) == 1
    assert scripts[0].startswith("sandbox_") and scripts[0].endswith(".py")


# ---------------------------------------------------------------------------
# 输出与产物
# ---------------------------------------------------------------------------


def test_run_code_truncates_output_to_256kb(tmp_path, session_env, monkeypatch):
    big_stdout = b"x" * (300 * 1024)
    big_stderr = b"e" * (300 * 1024)

    def fake_run(cmd, **kwargs):
        return _ok_process(stdout=big_stdout, stderr=big_stderr)

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "print('x' * 400000)"})

    assert "已硬截断" in result
    # stdout 与 stderr 各自截断到 256KB，不再放大模型上下文
    stdout_section = result.split("[stdout]")[1].split("[stderr]")[0]
    stderr_section = result.split("[stderr]")[1]
    assert len(stdout_section) < 300 * 1024
    assert len(stderr_section) < 300 * 1024
    # _truncate_output 本身的边界行为
    assert sandbox_tools._truncate_output(b"a" * 100) == "a" * 100


def test_run_code_lists_new_artifacts_in_sandbox(tmp_path, session_env, monkeypatch):
    def fake_run(cmd, **kwargs):
        # 模拟容器内脚本把图表写到挂载卷的 sandbox/ 子目录
        (tmp_path / "sandbox" / "chart.png").write_bytes(b"\x89PNG fake")
        return _ok_process(stdout=b"chart saved")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "plt.savefig('chart.png')"})

    assert "chart.png" in result
    assert "产物文件" in result
    assert (tmp_path / "sandbox" / "chart.png").is_file()


def test_run_code_flattens_nested_artifact_dirs(tmp_path, session_env, monkeypatch):
    """模型照搬宿主 output/<session_id>/ 约定时，产物仍提升收口到 sandbox/ 根"""
    def fake_run(cmd, **kwargs):
        nested = tmp_path / "sandbox" / "output" / "session_x"
        nested.mkdir(parents=True)
        (nested / "chart.png").write_bytes(b"\x89PNG fake")
        return _ok_process(stdout=b"saved to output/session_x/chart.png")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "plt.savefig('output/session_x/chart.png')"})

    assert "chart.png" in result
    # 产物提升到 sandbox/ 根，嵌套空目录清理干净
    assert (tmp_path / "sandbox" / "chart.png").is_file()
    assert not (tmp_path / "sandbox" / "output").exists()
    # 返回给模型的产物列表是提升后的收口路径，不出现嵌套写法（stdout 是模型
    # 自己的 print，管不到）
    assert "- output/session_x/chart.png" not in result


def test_flatten_keeps_existing_files_in_nested_dirs(tmp_path):
    """提升只动本次新增产物，模型有意保留的既有嵌套文件不迁移"""
    old_dir = tmp_path / "dataset"
    old_dir.mkdir()
    (old_dir / "kept.csv").write_text("a,b")

    flattened = sandbox_tools._flatten_sandbox_artifacts(
        tmp_path, ["dataset/new_chart.png"]
    )

    assert flattened == ["dataset/new_chart.png"]
    assert (old_dir / "kept.csv").is_file()


# ---------------------------------------------------------------------------
# 失败路径：磁盘防护 / 超时 / docker 缺失 / 非零退出
# ---------------------------------------------------------------------------


def test_run_code_rejects_when_disk_space_low(tmp_path, session_env, monkeypatch):
    # 剩余空间 100MB < 500MB 阈值；disk_usage 只用到 free 字段
    fake_usage = SimpleNamespace(total=10**9, used=9 * 10**8, free=100 * 1024 * 1024)
    monkeypatch.setattr(sandbox_tools.shutil, "disk_usage", lambda _p: fake_usage)

    def must_not_run(cmd, **kwargs):
        raise AssertionError("磁盘不足时不应发起 docker 调用")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", must_not_run)

    result = _invoke({"code": "print(1)"})

    assert "磁盘剩余空间不足" in result
    assert "500MB" in result
    # 拒绝发生在写脚本之前
    assert not (tmp_path / "sandbox").exists()


def test_run_code_host_timeout_returns_guidance(tmp_path, session_env, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=90)

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "while True: pass"})

    assert "超时" in result
    assert "死循环或过长计算" in result


def test_run_code_container_timeout_exit_code_returns_guidance(
    tmp_path, session_env, monkeypatch
):
    # 容器内 timeout 触发：退出码 124（TERM 超时）/137（-k KILL）
    def fake_run(cmd, **kwargs):
        return _ok_process(returncode=124, stderr=b"Terminated")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "while True: pass"})

    assert "脚本超时被强制终止" in result
    assert "124" in result
    assert "死循环" in result


def test_run_code_docker_missing_returns_friendly_error(
    tmp_path, session_env, monkeypatch
):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("[WinError 2] 系统找不到指定的文件。")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "print(1)"})

    assert "docker" in result
    assert "未找到" in result


def test_run_code_nonzero_exit_returns_stderr_and_guidance(
    tmp_path, session_env, monkeypatch
):
    def fake_run(cmd, **kwargs):
        return _ok_process(
            returncode=1,
            stderr=b"Traceback (most recent call last):\nValueError: bad input",
        )

    monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

    result = _invoke({"code": "raise ValueError('bad input')"})

    assert "脚本执行失败（退出码 1）" in result
    assert "ValueError: bad input" in result


def test_run_code_requires_session_context(monkeypatch):
    # 无会话上下文时直接返回说明，不发起 docker 调用
    def must_not_run(cmd, **kwargs):
        raise AssertionError("无会话目录时不应发起 docker 调用")

    monkeypatch.setattr(sandbox_tools.subprocess, "run", must_not_run)

    result = _invoke({"code": "print(1)"})

    assert "未找到会话工作目录" in result


# ---------------------------------------------------------------------------
# 治理接线：预算/去重（guarded_call budgeted=True + budget.py 限额）
# ---------------------------------------------------------------------------


def test_run_code_budget_dedup_and_limit(tmp_path, monkeypatch):
    session_token = set_session_context(str(tmp_path))
    thread_token = set_thread_context("test-sandbox-budget")
    budget.reset_task_budget("test-sandbox-budget")
    try:
        def fake_run(cmd, **kwargs):
            return _ok_process(stdout=b"ok")

        monkeypatch.setattr(sandbox_tools.subprocess, "run", fake_run)

        # 相同代码重复调用被去重闸门拦截（指纹只含 code+filename）
        first = _invoke({"code": "print('dup')", "description": "第一次"})
        assert "沙箱脚本执行成功" in first
        second = _invoke({"code": "print('dup')", "description": "换种说法再调"})
        assert "检测到重复调用" in second

        # 限额耗尽后拒绝：DEFAULT_TOOL_LIMITS["run_code"] = 5，已用 1 次
        for i in range(4):
            _invoke({"code": f"print('variant {i}')"})
        exhausted = _invoke({"code": "print('one too many')"})

        assert "调用预算已耗尽" in exhausted
    finally:
        budget.cleanup_task_budget("test-sandbox-budget")
        reset_session_context(session_token, thread_token)


# ---------------------------------------------------------------------------
# 真实冒烟（需要本机 Docker，标记 slow：CI 与日常全量可跳过）
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.OSType}}"],
        capture_output=True,
        timeout=60,
    )
    return probe.returncode == 0 and b"linux" in probe.stdout


@pytest.mark.slow
@pytest.mark.skipif(not _docker_available(), reason="本机 Docker 不可用")
def test_sandbox_end_to_end_uid_write_host_readable(tmp_path):
    """
    端到端冒烟：pandas 计算均值 + matplotlib 存图

    覆盖升级计划 P0-2 的 WSL2 UID 硬要求：沙箱以 uid 65534 写 /work 产物，
    宿主侧必须可读（PNG 魔数校验）
    """
    token = set_session_context(str(tmp_path))
    try:
        code = (
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "import pandas as pd\n"
            "df = pd.DataFrame({'month': [1, 2, 3], 'sales': [100, 200, 300]})\n"
            "print('mean =', df['sales'].mean())\n"
            "plt.plot(df['month'], df['sales'], marker='o')\n"
            "plt.savefig('smoke_chart.png', dpi=120)\n"
            "print('chart saved')\n"
        )
        result = _invoke({"code": code, "filename": "smoke.py"})
    finally:
        reset_session_context(token)

    assert "沙箱脚本执行成功" in result
    assert "mean = 200.0" in result
    assert "smoke_chart.png" in result

    # UID 写入 -> 宿主可读：PNG 魔数可从宿主侧完整读出
    chart = tmp_path / "sandbox" / "smoke_chart.png"
    assert chart.is_file()
    magic = chart.read_bytes()[:8]
    assert magic == b"\x89PNG\r\n\x1a\n"
    assert chart.stat().st_size > 0


@pytest.mark.slow
@pytest.mark.skipif(not _docker_available(), reason="本机 Docker 不可用")
def test_sandbox_network_isolation(tmp_path):
    """
    逃逸用例冒烟（网络隔离）：--network=none 下 socket 连接必须失败
    """
    token = set_session_context(str(tmp_path))
    try:
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('example.com', 443), 3)\n"
            "    print('NETWORK: CONNECTED (BAD)')\n"
            "except Exception as e:\n"
            "    print('NETWORK: BLOCKED ->', type(e).__name__)\n"
        )
        result = _invoke({"code": code, "filename": "net_probe.py"})
    finally:
        reset_session_context(token)

    assert "沙箱脚本执行成功" in result
    assert "NETWORK: BLOCKED" in result
    assert "CONNECTED (BAD)" not in result


def test_normalize_cjk_font_names():
    """
    字体名归一化：模型惯用的 SimHei/微软雅黑等别名替换为沙箱内置 Noto Sans CJK SC，
    只替换字符串字面量，非字体上下文不受影响（Windows 镜像无中文字体问题的修复）
    """
    normalize = sandbox_tools._normalize_cjk_font_names

    code = (
        "import matplotlib\n"
        "matplotlib.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']\n"
        'matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei"]\n'
        "print('SimHei 不可用提示')\n"
    )
    normalized = normalize(code)
    assert "'Noto Sans CJK SC'" in normalized
    assert '"Noto Sans CJK SC"' in normalized
    assert "SimHei', 'DejaVu Sans'" not in normalized
    # 单引号字符串内的中文明文不被误改（只替换整段字体名字面量）
    assert "print('SimHei 不可用提示')" in normalized

    # 双引号字体名（保留原引号风格）、无字体代码均安全
    assert normalize('x = "Arial Unicode MS"') == 'x = "Noto Sans CJK SC"'

    assert normalize("x = 123") == "x = 123"
