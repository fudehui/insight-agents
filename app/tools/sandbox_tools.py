"""
Docker 沙箱代码执行工具模块

提供 run_code 工具：把模型生成的 Python 代码写入当前会话目录的 sandbox/ 子目录，
再用加固版 docker run 在一次性容器中执行。威胁模型（docs/upgrade-plan.md P0-2）：
LLM 生成代码视为不可信输入——检索网页/文档被投毒即可诱导恶意代码，因此执行环境
按"零信任"加固：网络隔离、内存/CPU/进程数限额、只读 rootfs、去能力、非 root 运行。

安全约束逐条对应升级计划模板：
1. --network=none            容器内无法访问任何网络（含内网）
2. --memory=512m --memory-swap=512m  内存与交换锁死 512MB
3. --cpus=1.0 --pids-limit=64        CPU 与进程数限额（fork bomb 拦截）
4. --read-only --tmpfs /tmp:rw,size=64m  只读 rootfs，仅 /tmp 可写
5. --cap-drop=ALL --security-opt=no-new-privileges   去全部 Linux 能力
6. -u 65534:65534            非 root（nobody）运行
7. -v <session_dir>:/work:rw 挂载会话目录；--workdir 收口到 sandbox/ 子目录，
                             容器内相对路径产物（png/csv 等）全部落在
                             session_dir/sandbox/ 下，经现有 _diff_task_files
                             机制进入产物面板
8. 容器内 timeout -k 5 60 + 宿主 subprocess 双层超时
9. stdout/stderr 256KB 硬截断；禁止向容器传入任何环境变量/密钥（不用 -e）
10. 磁盘防护：run 前检查会话目录所在卷剩余空间（低于 500MB 拒绝执行）

实现约束（docs/evaluation-metric-template.md）：不新增 Python 依赖，
仅用 subprocess 调 docker CLI + 标准库。外层 guarded_call 防护
（超时/重试/埋点/预算），并在 app/api/budget.py 注册调用限额。

镜像：默认 huiyan-sandbox:latest（Dockerfile.sandbox 自建，预装
matplotlib + pandas）。生产环境建议 digest 锁定，用环境变量覆盖：
    SANDBOX_IMAGE=huiyan-sandbox@sha256:<digest>
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.tools import tool

from app.api.context import get_session_context
from app.tools.call_guard import guarded_call
from app.utils.path_utils import resolve_path

# 默认沙箱镜像：由项目根目录的 Dockerfile.sandbox 构建（见文件头构建命令）。
# 支持环境变量 SANDBOX_IMAGE 覆盖为 huiyan-sandbox@sha256:<digest> 形式做 digest 锁定
_DEFAULT_SANDBOX_IMAGE = "huiyan-sandbox:latest"

# 沙箱代码统一写在会话目录的 sandbox/ 子目录（产物收口，W3 评审要求），
# rglob 扫描会把该子目录下的图表/CSV 等送进 _diff_task_files 产物面板
_SANDBOX_SUBDIR = "sandbox"

# 容器内挂载点与脚本工作目录：/work 映射整个会话目录，脚本 cwd 收口到 /work/sandbox
_CONTAINER_WORK_MOUNT = "/work"

# 容器内 timeout 参数：60 秒软超时后发 TERM，-k 5 再等 5 秒强制 KILL
_CONTAINER_TIMEOUT_S = 60
_CONTAINER_KILL_GRACE_S = 5

# 宿主侧 subprocess 超时兜底：正常情况下容器内 timeout 会先生效，
# 这里防 docker CLI/daemon 卡死导致调用永久挂起
_HOST_SUBPROCESS_TIMEOUT_S = 90.0

# guarded_call 超时要大于宿主侧 subprocess 超时，让"脚本超时"的引导信息
# 由本工具给出，而不是被 guard 层的通用超时文案覆盖
_GUARD_TIMEOUT_S = 95.0

# stdout/stderr 硬截断上限（256KB），防止大输出撑爆模型上下文与 events.jsonl
_OUTPUT_LIMIT_BYTES = 256 * 1024

# 磁盘防护阈值：会话目录所在卷剩余空间低于 500MB 时拒绝执行。
# --storage-opt 对 bind mount 无效，只能在宿主侧 run 前检查（残余风险见升级计划）
_MIN_FREE_BYTES = 500 * 1024 * 1024

# 非 root 运行身份：Debian 的 nobody 用户
_SANDBOX_UID_GID = "65534:65534"

# 沙箱内置 CJK 字体（fonts-noto-cjk 提供）与模型常用但镜像内不存在的字体别名。
# 归一化只作用于字符串字面量（见 _normalize_cjk_font_names）
_SANDBOX_CJK_FONT = "Noto Sans CJK SC"
_CJK_FONT_ALIAS_RE = re.compile(
    r"(['\"])(?:SimHei|Microsoft YaHei|SimSun|Heiti SC|STHeiti|Arial Unicode MS)\1"
)


def sandbox_image() -> str:
    """
    取当前沙箱镜像名

    SANDBOX_IMAGE 环境变量支持 huiyan-sandbox@sha256:<digest> 形式的
    digest 锁定（生产推荐），未设置时回退默认镜像名
    """
    return os.getenv("SANDBOX_IMAGE") or _DEFAULT_SANDBOX_IMAGE


def _docker_binary() -> str:
    """docker CLI 路径；环境变量可覆盖，便于测试注入假 docker 二进制"""
    return os.getenv("SANDBOX_DOCKER_BIN", "docker")


def build_docker_command(session_dir: Path, script_filename: str) -> list[str]:
    """
    按 docs/upgrade-plan.md P0-2 的安全加固模板拼装 docker run 命令

    返回参数列表（不经 shell），避免会话路径中的特殊字符被二次解释。
    :param session_dir: 宿主侧会话目录（bind mount 源）
    :param script_filename: sandbox/ 子目录下的脚本文件名
    :return: docker run 参数列表
    """
    # Windows 路径改用正斜杠，Docker Desktop 两种写法都接受
    mount_source = str(session_dir).replace("\\", "/")
    return [
        _docker_binary(),
        "run",
        "--rm",
        # 1. 网络隔离：容器内无法访问任何网络
        "--network=none",
        # 2. 内存与交换分区合计锁死 512MB
        "--memory=512m",
        "--memory-swap=512m",
        # 3. CPU 限 1 核 + 进程数限 64（fork bomb 拦截）
        "--cpus=1.0",
        "--pids-limit=64",
        # 4. 只读 rootfs，仅 /tmp 提供 64MB 可写 tmpfs
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        # 5. 去全部 Linux 能力 + 禁止提权
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # 6. 非 root（nobody）运行
        "-u",
        _SANDBOX_UID_GID,
        # 容器内脚本 cwd 收口到挂载卷的 sandbox/ 子目录：
        # 相对路径保存的图表/CSV 全部落在 session_dir/sandbox/ 下
        "--workdir",
        f"{_CONTAINER_WORK_MOUNT}/{_SANDBOX_SUBDIR}",
        # 7. 会话目录整体挂为 /work（rw：产物要落盘）
        "-v",
        f"{mount_source}:{_CONTAINER_WORK_MOUNT}:rw",
        sandbox_image(),
        # 8. 容器内软超时 60 秒，超时 TERM 后 5 秒强制 KILL
        "timeout",
        "-k",
        str(_CONTAINER_KILL_GRACE_S),
        str(_CONTAINER_TIMEOUT_S),
        # -B 禁止写 .pyc：rootfs 只读，也避免 __pycache__ 混进产物面板
        "python",
        "-B",
        f"{_CONTAINER_WORK_MOUNT}/{_SANDBOX_SUBDIR}/{script_filename}",
    ]


def _truncate_output(data: bytes) -> str:
    """
    解码并硬截断容器输出（256KB 上限）

    先按字节截断再解码：截断点可能切开多字节中文，用 errors=replace 容错
    """
    truncated = len(data) > _OUTPUT_LIMIT_BYTES
    text = data[:_OUTPUT_LIMIT_BYTES].decode("utf-8", errors="replace")
    if truncated:
        text += f"\n...[输出超过 256KB 上限，已硬截断（原始 {len(data)} 字节）]"
    return text


def _normalize_cjk_font_names(code: str) -> str:
    """
    把模型惯用但镜像内不存在的 CJK 字体名归一化为沙箱预装字体

    镜像只内置 Noto Sans CJK（见 Dockerfile.sandbox），模型凭习惯写的
    SimHei/Microsoft YaHei 等字体名在容器内必然缺字形，matplotlib 会把
    中文渲染成豆腐块并触发逐字符告警——在标准审批档位下每次重试都要再过
    一次人工审批，成本高。只在字符串字面量位置做等价替换（保留原引号风格），
    不改动其他代码。
    """
    return _CJK_FONT_ALIAS_RE.sub(
        lambda m: f"{m.group(1)}{_SANDBOX_CJK_FONT}{m.group(1)}", code
    )


def _resolve_script_path(filename: Optional[str], code: str, session_dir: Path) -> Path:
    """
    解析脚本落盘路径，统一收口到 session_dir/sandbox/ 子目录

    - filename 只取基础文件名（子目录/穿越成分直接剥掉），缺省按代码内容
      哈希命名：相同代码得到相同文件名，预算层的重复调用检测才能命中
    - 再经 resolve_path 复用现有会话路径约束做双保险（拒绝绝对路径与 ../ 穿越）
    """
    name = Path(filename or "").name if filename else ""
    if not name:
        code_digest = hashlib.md5(code.encode("utf-8")).hexdigest()[:8]
        name = f"sandbox_{code_digest}.py"
    if not name.endswith(".py"):
        name = f"{name}.py"
    resolved = Path(resolve_path(f"{_SANDBOX_SUBDIR}/{name}", str(session_dir)))
    return resolved


def _snapshot_sandbox_files(sandbox_dir: Path) -> dict[str, float]:
    """记录执行前 sandbox/ 下已有文件的相对路径与修改时间，用于执行后对比新增产物"""
    snapshot: dict[str, float] = {}
    if not sandbox_dir.exists():
        return snapshot
    for file_path in sandbox_dir.rglob("*"):
        if file_path.is_file():
            snapshot[file_path.relative_to(sandbox_dir).as_posix()] = (
                file_path.stat().st_mtime
            )
    return snapshot


def _diff_sandbox_files(sandbox_dir: Path, before: dict[str, float]) -> list[str]:
    """对比执行前快照，返回本次新生成或更新的产物相对路径（最新在前）"""
    generated: list[str] = []
    for file_path in sandbox_dir.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(sandbox_dir).as_posix()
        mtime = file_path.stat().st_mtime
        if relative not in before or before[relative] != mtime:
            generated.append(relative)
    generated.sort(reverse=True)
    return generated


def _flatten_sandbox_artifacts(sandbox_dir: Path, generated: list[str]) -> list[str]:
    """
    把模型写进 sandbox/ 嵌套子目录的产物提升到 sandbox/ 根

    模型常把宿主侧 output/<session_id>/ 的落盘约定照搬进容器代码（如
    savefig("output/<session_id>/chart.png")），产物会嵌套两层目录。
    产物收口约定是 sandbox/ 根，这里在宿主侧统一提升，保证产物面板路径
    一致；只处理本次新增/更新的文件，不触碰既有文件。
    """
    flattened: list[str] = []
    for relative in generated:
        source = sandbox_dir / relative
        if "/" not in relative or not source.is_file():
            flattened.append(relative)
            continue
        target = sandbox_dir / Path(relative).name
        try:
            os.replace(source, target)
            flattened.append(target.name)
        except OSError:
            # 提升失败（文件占用等）不阻断任务，保留原路径供产物面板兜底
            flattened.append(relative)
    # 清理提升后残留的空目录（rmdir 只能删空目录，非空静默跳过）
    for dir_path in sorted(
        (p for p in sandbox_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            dir_path.rmdir()
        except OSError:
            pass
    return flattened


def _disk_check_message(session_dir: Path) -> Optional[str]:
    """
    宿主侧磁盘防护：会话目录所在卷剩余空间低于阈值时返回拒绝说明

    shutil.disk_usage 在 Windows 上落到会话目录所在盘符，Docker Desktop 的
    bind mount 经 VM 转发，宿主盘剩余空间是可获得的最佳近似口径
    """
    try:
        free_bytes = shutil.disk_usage(session_dir).free
    except OSError:
        # 卷信息拿不到时放行，交由后续资源限额兜底；不能因此阻断正常任务
        return None
    if free_bytes < _MIN_FREE_BYTES:
        free_mb = free_bytes // (1024 * 1024)
        return (
            f"错误：磁盘剩余空间不足（当前约 {free_mb}MB，低于 500MB 安全阈值），"
            "已拒绝执行沙箱任务。请先清理磁盘空间后重试。"
        )
    return None


def _format_result(
    exit_code: int, stdout: str, stderr: str, artifacts: list[str]
) -> str:
    """
    把执行结果拼装成面向模型的返回文本

    退出码语义：0 成功；124/137（及 128+信号）是容器内 timeout 超时/强杀，
    错误信息要引导模型检查死循环或过长计算，而不是简单报错
    """
    if exit_code == 0:
        header = "沙箱脚本执行成功。"
    elif exit_code in (124, 137) or exit_code > 128:
        header = (
            f"错误：脚本超时被强制终止（退出码 {exit_code}）。"
            "请检查代码是否存在死循环或过长计算，"
            f"单次执行上限为 {_CONTAINER_TIMEOUT_S} 秒，"
            "可拆分为多次小步骤执行。"
        )
    else:
        header = (
            f"错误：脚本执行失败（退出码 {exit_code}）。"
            "请根据下方 stderr 中的报错信息修正代码后重试。"
        )

    sections = [header]
    if stdout.strip():
        sections.append(f"[stdout]\n{stdout}")
    if stderr.strip():
        sections.append(f"[stderr]\n{stderr}")
    if artifacts:
        sections.append(
            "[本次生成的产物文件]\n- " + "\n- ".join(artifacts)
            + "\n以上文件保存在会话目录 sandbox/ 子目录下，已进入任务产物面板。"
        )
    return "\n".join(sections)


def _run_code_impl(code: str, filename: Optional[str]) -> str:
    """
    run_code 的同步实现：写脚本 -> 磁盘检查 -> docker run -> 收集输出

    所有可预期失败（无会话目录、磁盘不足、超时、docker 缺失、非零退出）
    都返回面向模型的说明文本，而不是把异常抛给上层
    """
    session_dir_str = get_session_context()
    if not session_dir_str:
        return (
            "错误：未找到会话工作目录，沙箱执行必须在任务会话内进行。"
            "请确认当前处于正常任务流程中。"
        )
    session_dir = Path(session_dir_str)

    # 磁盘防护先于一切执行：bind mount 卷写满会危及整个会话数据
    disk_guard = _disk_check_message(session_dir)
    if disk_guard:
        return disk_guard

    sandbox_dir = session_dir / _SANDBOX_SUBDIR
    try:
        # 字体名归一化放在哈希命名之前：归一化后的代码才是真正执行的代码
        code = _normalize_cjk_font_names(code)
        script_path = _resolve_script_path(filename, code, session_dir)
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        # UTF-8 写入，容器内 PYTHONUTF8 已在镜像层设置
        script_path.write_text(code, encoding="utf-8")
    except (ValueError, OSError) as e:
        return f"错误：脚本写入失败：{e}"

    before = _snapshot_sandbox_files(sandbox_dir)
    cmd = build_docker_command(session_dir, script_path.name)

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_HOST_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # 宿主侧兜底超时：正常应由容器内 timeout 先生效，走到这里说明
        # docker CLI/daemon 层面卡住了
        return (
            "错误：沙箱执行超时（宿主侧 90 秒上限，容器内 60 秒软超时未正常生效）。"
            "请检查脚本是否有死循环或过长计算；若反复出现请联系运维检查 Docker 状态。"
        )
    except FileNotFoundError:
        return (
            "错误：未找到 docker 命令，沙箱执行依赖本机 Docker 环境。"
            "请确认 Docker 已安装并处于运行状态。"
        )

    stdout = _truncate_output(completed.stdout or b"")
    stderr = _truncate_output(completed.stderr or b"")
    artifacts = _diff_sandbox_files(sandbox_dir, before)
    artifacts = _flatten_sandbox_artifacts(sandbox_dir, artifacts)
    return _format_result(completed.returncode, stdout, stderr, artifacts)


@tool
async def run_code(
    code: Annotated[str, "要执行的 Python 脚本完整源码"],
    filename: Annotated[
        Optional[str], "脚本文件名（可选，默认按代码内容自动命名）"
    ] = None,
    description: Annotated[
        Optional[str], "本次执行的简短说明（可选，用于执行记录展示）"
    ] = None,
) -> str:
    """
    在 Docker 沙箱中执行一段 Python 代码，适合定量分析与图表生成

    执行环境：
    - 预装 pandas、matplotlib（Agg 后端），无网络访问，单次最长执行 60 秒
    - 代码先写入当前会话目录 sandbox/ 子目录，再在容器内执行
    - 图表/CSV 等产物用相对文件名直接保存（如 plt.savefig("chart.png")），
      不要在代码里自建 output/ 等子目录；文件会统一收口到会话目录
      sandbox/ 根并自动进入任务产物面板
    - stdout/stderr 会返回（超长自动截断），请用 print 输出关键计算结果

    代码要求：
    - 禁止访问网络（容器无网络权限，相关代码会直接失败）
    - 禁止读写工作目录之外的路径
    - 单次脚本只做一件事，复杂分析拆成多次执行

    :param code: Python 脚本源码全文
    :param filename: 可选脚本文件名（只取文件名部分，自动补 .py 后缀）
    :param description: 可选说明文字
    :return: 执行结果（退出码、stdout/stderr、产物清单）或错误引导信息
    """
    # 预算/去重指纹只用 code 与 filename：description 语气变化不应绕过重复检测
    return await guarded_call(
        "run_code",
        {"code": code, "filename": filename or ""},
        lambda: _run_code_impl(code, filename),
        display_name="沙箱代码执行工具",
        timeout_s=_GUARD_TIMEOUT_S,
        max_retries=1,
        budgeted=True,
    )


if __name__ == "__main__":
    import asyncio

    from app.api.context import set_session_context, set_thread_context

    # 本地调试入口：手工指定会话目录验证沙箱链路（需本机 Docker 已启动）
    debug_dir = Path(__file__).resolve().parents[2] / "output" / "sandbox_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    session_token = set_session_context(str(debug_dir))
    thread_token = set_thread_context("sandbox_debug")
    try:
        demo_code = (
            "import pandas as pd\n"
            "df = pd.DataFrame({'v': [1, 2, 3, 4]})\n"
            "print('均值 =', df['v'].mean())\n"
        )
        print(asyncio.run(run_code.ainvoke({"code": demo_code})))
    finally:
        from app.api.context import reset_session_context

        reset_session_context(session_token, thread_token)
