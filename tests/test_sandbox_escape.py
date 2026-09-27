"""
沙箱逃逸真实验证（docs/upgrade-plan.md P0-2 安全回归，**需要本机 Docker**）

与 evals/golden/sandbox_security.yaml 的固定安全回归口径一一对应：
网络隔离 / 超时 kill / fork bomb / 资源限额 / 文件系统边界 / events.jsonl
审计日志不可达（终评补充）。本文件与并行建设的 huiyan-sandbox 自建镜像
**完全解耦**：直接用官方 python:3.12-slim 跑容器（避免镜像名冲突），
但安全加固参数与计划中的沙箱命令模板逐项一致（见 HARDENED_FLAGS）。

包含两组测试：
- TestSandboxEscapeDocker：逐类真实跑容器验证逃逸行为，标记 slow；
  docker info 失败时由 tests/conftest.py 的 conftest 级 skipif 整文件跳过，
  官方镜像缺失且 pull 失败时由 sandbox_image fixture 跳过整组。
  每个用例的 subprocess 调用自带 timeout 保护 + 容器强删兜底，
  全组总时长控制在约 2 分钟内（实测约 30s）。
- TestGoldenSchema：免 docker 的纯单测——sandbox_security.yaml 的 schema
  校验（load_cases 加载 6 条逃逸 + 3 条端到端、tags/expected_points
  结构对），不依赖 Docker，任何环境都执行。

本机实测经验（已回写 YAML 用例口径）：bytes(n)/bytearray(n) 走
calloc + mmap 惰性零页分配，不实际触页则不计入 cgroup 内存——内存炸弹
必须写入非零数据才能真正触发 512m 限额。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from evals.loader import load_cases

# ---------------------------------------------------------------------------
# 常量与辅助
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SECURITY_YAML = PROJECT_ROOT / "evals" / "golden" / "sandbox_security.yaml"

# 官方基础镜像：不依赖自建 huiyan-sandbox（并行建设中，内含 pandas/matplotlib），
# 只验证安全 flag 的隔离行为，二者加固模板完全一致
SANDBOX_IMAGE = "python:3.12-slim"

# 与 docs/upgrade-plan.md P0-2 沙箱命令模板逐项一致（镜像名除外）
HARDENED_FLAGS = [
    "--rm",
    "--network=none",
    "--memory=512m",
    "--memory-swap=512m",
    "--cpus=1.0",
    "--pids-limit=64",
    "--read-only",
    "--tmpfs",
    "/tmp:rw,size=64m",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "-u",
    "65534:65534",
]

_run_seq = 0  # 容器名序号：保证唯一，超时兜底 rm -f 用


def run_sandbox(
    code: str | None,
    *,
    extra_flags: list[str] | None = None,
    command: list[str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """
    按加固模板跑一次容器并收集结果

    :param code: 传给 `python -c` 的代码；与 command 二选一
    :param extra_flags: 追加参数（如 -v 挂载）
    :param command: 自定义容器命令（如 timeout 包装），给出时忽略 code
    :param timeout: subprocess 硬超时（秒）——每个用例的时长保护
    :return: subprocess.CompletedProcess（utf-8 解码，容错替换）
    """
    global _run_seq
    _run_seq += 1
    name = f"sandbox-escape-test-{_run_seq}"
    cmd = ["docker", "run", *HARDENED_FLAGS, "--name", name, *(extra_flags or []), SANDBOX_IMAGE]
    cmd += list(command) if command else ["python", "-c", code]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # 兜底：容器可能仍在运行，强制删除（--rm 场景下 rm -f 即可回收）
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        raise


@pytest.fixture(scope="session")
def sandbox_image():
    """确保官方镜像可用：本地缺失时尝试 pull 一次，失败则整组跳过"""
    probe = subprocess.run(
        ["docker", "image", "inspect", SANDBOX_IMAGE], capture_output=True, timeout=30
    )
    if probe.returncode != 0:
        pull = subprocess.run(
            ["docker", "pull", SANDBOX_IMAGE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        if pull.returncode != 0:
            pytest.skip(f"镜像 {SANDBOX_IMAGE} 不可用且 pull 失败：{(pull.stderr or '')[-200:]}")
    return SANDBOX_IMAGE


# ---------------------------------------------------------------------------
# Docker 逐类逃逸验证（slow：真实容器执行，需本机 Docker）
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestSandboxEscapeDocker:
    """逐类真实验证（与 sandbox_security.yaml 的 6 类逃逸一一对应）"""

    def test_network_isolation(self, sandbox_image):
        """第 1 类 escape-network：--network=none 下 socket 连外网必须失败"""
        code = textwrap.dedent(
            """
            import socket
            outcomes = []
            for host, port in (("example.com", 80), ("8.8.8.8", 53)):
                try:
                    socket.create_connection((host, port), timeout=5)
                    outcomes.append(f"{host}:{port}=CONNECTED")
                except OSError as exc:
                    outcomes.append(f"{host}:{port}=BLOCKED({type(exc).__name__})")
            print("NET_RESULT", ";".join(outcomes))
            """
        )
        proc = run_sandbox(code, timeout=60)
        # 容器应正常退出（逃逸被拦截 != 容器崩溃）
        assert proc.returncode == 0, f"容器异常退出：{proc.stderr[-300:]}"
        assert "CONNECTED" not in proc.stdout, f"外网连接竟然成功：{proc.stdout}"
        # 两个目标（域名 + IP）都必须以 OSError 失败
        assert proc.stdout.count("BLOCKED(") == 2, f"网络探测结果不完整：{proc.stdout}"

    def test_timeout_kill(self, sandbox_image):
        """第 2 类 escape-timeout：容器内 timeout 包装必须杀掉死循环（退出码 124）"""
        command = [
            "timeout", "-k", "5", "3",
            "python", "-c", "print('LOOP_STARTED', flush=True)\nwhile True: pass",
        ]
        started = time.monotonic()
        proc = run_sandbox(None, command=command, timeout=60)
        elapsed = time.monotonic() - started
        assert "LOOP_STARTED" in proc.stdout, "死循环代码未真正开始执行"
        # 退出码 124 = timeout 命令按 3s 软超时终止目标进程（与加固模板同构）
        assert proc.returncode == 124, (
            f"预期 timeout 杀死死循环（退出码 124），实际 {proc.returncode}：{proc.stderr[-200:]}"
        )
        # 容器必须在限时内自行退出，不能挂死（3s 超时 + 5s -k 余量 + 启动开销）
        assert elapsed < 30, f"超时终止耗时 {elapsed:.1f}s，超出预期"

    def test_pids_limit_forkbomb(self, sandbox_image):
        """第 3 类 escape-forkbomb：pids-limit=64 下 fork 循环必须被快速拦截"""
        code = textwrap.dedent(
            """
            import os, time
            count = 0
            try:
                for _ in range(500):
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(10)  # 子进程驻留，撑满 pids 上限
                        os._exit(0)
                    count += 1
            except OSError as exc:
                print("FORK_BLOCKED", type(exc).__name__)
            print("FORK_COUNT", count)
            """
        )
        started = time.monotonic()
        proc = run_sandbox(code, timeout=60)
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, f"容器异常退出：{proc.stderr[-300:]}"
        # fork 必须以 OSError（EAGAIN 类）被拦截，而不是完成 500 次
        assert "FORK_BLOCKED" in proc.stdout, f"fork bomb 未被拦截：{proc.stdout}"
        count = int(proc.stdout.split("FORK_COUNT")[1].split()[0])
        # pids-limit=64 是容器内全部进程的硬上限（父进程 + 子进程计入）
        assert count <= 64, f"成功 fork 数 {count} 超过 pids-limit=64"
        # 进程数不失控：容器在限定时间内自行退出（子进程随容器停止被回收）
        assert elapsed < 45, f"fork bomb 用例耗时 {elapsed:.1f}s，进程疑似堆积"

    def test_memory_limit(self, sandbox_image):
        """第 4 类 escape-resource：512m 下写满内存页必须 MemoryError 或 OOM kill"""
        code = textwrap.dedent(
            """
            chunks = []
            try:
                for _ in range(64):
                    # 必须写非零数据：bytes(n)/bytearray(n) 走 calloc 惰性零页，
                    # 不触页不计入 cgroup 内存（本机实测 8GB 都分配得下）
                    chunks.append(b"A" * (32 * 1024 * 1024))
                print("MEMORY_UNLIMITED")
            except MemoryError:
                print("MEMORY_LIMITED")
            """
        )
        proc = run_sandbox(code, timeout=90)
        oom_killed = proc.returncode in (137, -9)  # 128+SIGKILL
        memory_error = "MEMORY_LIMITED" in proc.stdout or "MemoryError" in proc.stderr
        # 计划口径：MemoryError 或 OOM kill 均属预期拦截形态
        assert oom_killed or memory_error, (
            f"内存限额未拦截（rc={proc.returncode}）：{proc.stdout[-200:]}{proc.stderr[-200:]}"
        )
        assert "MEMORY_UNLIMITED" not in proc.stdout, "2GB 分配竟全部成功，512m 限额未生效"

    def test_filesystem_and_uid_smoke(self, sandbox_image):
        """第 5 类 escape-filesystem：只读 rootfs 拒绝越权写；/work 冒烟双向可读"""
        tmp = Path(tempfile.mkdtemp(prefix="sandbox_escape_"))
        try:
            # 宿主 -> 容器方向的金丝雀（UID 冒烟反向）
            (tmp / "in.txt").write_text("HOST_CANARY", encoding="utf-8")
            mount = f"{tmp.as_posix()}:/work:rw"
            code = textwrap.dedent(
                """
                import pathlib
                results = []
                # 1) 只读 rootfs：/etc /root /usr 一律拒绝
                for target in ("/etc/escape_test", "/root/escape_test", "/usr/escape_test"):
                    try:
                        pathlib.Path(target).write_text("x")
                        results.append(f"{target}=WRITE_OK")
                    except OSError as exc:
                        results.append(f"{target}=DENIED({type(exc).__name__})")
                # 2) /work 挂载目录：加固模板下的唯一可写出口
                try:
                    pathlib.Path("/work/out.txt").write_text("sandbox-ok")
                    results.append("WORK_WRITE_OK")
                except OSError as exc:
                    results.append(f"WORK_WRITE_FAIL({type(exc).__name__})")
                # 3) UID 冒烟反向：读宿主写入的金丝雀
                try:
                    results.append(f"READ_HOST={pathlib.Path('/work/in.txt').read_text()}")
                except OSError as exc:
                    results.append(f"READ_HOST_FAIL({type(exc).__name__})")
                # 4) tmpfs /tmp 可写（加固模板的预期设计）
                try:
                    pathlib.Path("/tmp/ok.txt").write_text("t")
                    results.append("TMPFS_WRITE_OK")
                except OSError as exc:
                    results.append(f"TMPFS_WRITE_FAIL({type(exc).__name__})")
                print("FS_RESULT", ";".join(results))
                """
            )
            proc = run_sandbox(code, extra_flags=["-v", mount], timeout=60)
            assert proc.returncode == 0, f"容器异常退出：{proc.stderr[-300:]}"
            out = proc.stdout
            # 越权写全部被拒（read-only 或 Permission denied）
            for target in ("/etc/escape_test", "/root/escape_test", "/usr/escape_test"):
                assert f"{target}=WRITE_OK" not in out, f"越权写 {target} 竟然成功"
                assert f"{target}=DENIED(" in out, f"{target} 探测结果缺失：{out}"
            # /work 可写 + 宿主金丝雀可读（UID 错位不破坏链路）
            assert "WORK_WRITE_OK" in out, f"/work 挂载不可写（UID 错位？）：{out}"
            assert "READ_HOST=HOST_CANARY" in out, f"宿主金丝雀读取失败：{out}"
            assert "TMPFS_WRITE_OK" in out, "tmpfs /tmp 应可写（加固模板设计）"
            # 容器 -> 宿主方向（UID 冒烟正向）：沙箱产物宿主可读
            produced = tmp / "out.txt"
            assert produced.is_file(), "沙箱写入 /work/out.txt 未落到宿主"
            assert produced.read_text(encoding="utf-8") == "sandbox-ok"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_events_jsonl_unreachable(self, sandbox_image):
        """第 6 类 escape-audit：未挂载即不可达——容器内看不到会话审计路径"""
        code = textwrap.dedent(
            """
            import os
            print("WORK_EXISTS", os.path.exists("/work"))
            print("OUTPUT_PATH_EXISTS", os.path.exists("/app/output"))
            try:
                open("/app/output/events.jsonl").read()
                print("EVENTS_REACHABLE")
            except OSError:
                print("EVENTS_UNREACHABLE")
            mounts = open("/proc/mounts").read()
            # 按挂载点字段精确匹配会话路径，避免无关挂载含 "output" 子串造成误报
            mount_points = [ln.split()[1] for ln in mounts.splitlines() if ln.strip()]
            session_hit = any(
                mp in ("/work", "/app/output") or mp.startswith(("/work/", "/app/output/"))
                for mp in mount_points
            )
            print("MOUNT_HAS_OUTPUT", session_hit)
            """
        )
        proc = run_sandbox(code, timeout=60)
        assert proc.returncode == 0, f"容器异常退出：{proc.stderr[-300:]}"
        assert "WORK_EXISTS False" in proc.stdout, "容器内不应存在 /work（未挂载）"
        assert "OUTPUT_PATH_EXISTS False" in proc.stdout, "真实 app/output 路径不应出现在容器内"
        assert "EVENTS_UNREACHABLE" in proc.stdout, "events.jsonl 竟然可达（存在越权挂载？）"
        assert "MOUNT_HAS_OUTPUT False" in proc.stdout, "/proc/mounts 中出现会话目录挂载"


# ---------------------------------------------------------------------------
# 免 docker 的纯单测：golden YAML schema 校验（任何环境都执行）
# ---------------------------------------------------------------------------


class TestGoldenSchema:
    """sandbox_security.yaml 的 schema 与结构校验（不依赖 Docker）"""

    def test_load_all_cases(self):
        """load_cases 加载 9 条（6 条逃逸 + 3 条端到端）全通过，id 唯一"""
        cases = load_cases(SANDBOX_SECURITY_YAML)
        assert len(cases) == 9
        ids = [c.id for c in cases]
        assert len(set(ids)) == len(ids)
        assert ids[:6] == [f"sandbox-{i:03d}" for i in range(1, 7)]
        assert ids[6] == "sandbox-e2e-001"
        assert ids[7] == "sandbox-e2e-002"
        assert ids[8] == "echarts-e2e-001"

    def test_six_escape_categories(self):
        """6 条逃逸用例 tags 标齐 6 个逃逸类别，且都是 run_code 行为判定口径"""
        cases = {c.id: c for c in load_cases(SANDBOX_SECURITY_YAML)}
        expected_tags = {
            "sandbox-001": ["escape-network"],
            "sandbox-002": ["escape-timeout"],
            "sandbox-003": ["escape-forkbomb"],
            "sandbox-004": ["escape-resource"],
            "sandbox-005": ["escape-filesystem"],
            "sandbox-006": ["escape-audit"],
        }
        for cid, tags in expected_tags.items():
            case = cases[cid]
            assert case.tags == tags, f"{cid} 的逃逸类别标签错误：{case.tags}"
            assert "run_code" in case.expected_tools, f"{cid} 未以 run_code 为载体"
            # expected_points：3-5 条非空行为判定（loader 已校验条数，这里查语义）
            assert all(p.strip() for p in case.expected_points)
            behaviors = ("拦截", "失败", "不可达", "拒绝", "终止", "OOM", "MemoryError", "未")
            assert any(b in p for p in case.expected_points for b in behaviors), (
                f"{cid} 的 expected_points 缺少行为判定口径"
            )

    def test_e2e_acceptance_case(self):
        """端到端验收 case：查库 -> 沙箱算环比 -> 生成图表 -> 出报告 的工具链"""
        cases = {c.id: c for c in load_cases(SANDBOX_SECURITY_YAML)}
        e2e = cases["sandbox-e2e-001"]
        assert e2e.expected_tools == ["execute_sql_query", "run_code", "generate_markdown"]
        assert e2e.expected_source_types == ["sql"]
        assert "matplotlib" in e2e.query, "图表生成是 P0-2 差异化目标，query 应要求画图"
        assert "环比" in e2e.query or "同比" in e2e.query
