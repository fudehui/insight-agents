"""
FastAPI 接口层与项目闭环入口

负责承接前端的任务提交、任务取消、文件上传/下载、输出文件列表查询和
WebSocket 长连接。HTTP 接口只做轻量调度，真正的 DeepAgents 执行放到后台
任务中；执行进度、工具调用和最终结果由 monitor 按 thread_id 推送给前端。
"""

import asyncio
import datetime
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional

import uvicorn
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent.main_agent import (
    clear_task_runtime_state,
    get_main_agent,
    get_pending_approval,
    resume_deep_agent,
    run_deep_agent,
)
from app.api.budget import cleanup_task_budget
from app.api.monitor import manager
from app.api.source_registry import cleanup_task_sources


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动时绑定当前事件循环到 WebSocket 管理器，确保后台 Agent 任务可以把
    monitor 事件投递回 FastAPI 所在的 loop。
    """
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    print(f"[Server] WebSocket Manager bound to loop: {id(loop)}")
    yield


# 当前文件位于 app/api/server.py，运行时目录统一收敛到 app 目录
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent

# 访问令牌鉴权：未配置 APP_ACCESS_TOKEN 时不启用（本地开发零负担）；
# 配置后所有 REST 接口与 WebSocket 都需要携带令牌
_access_token = (os.getenv("APP_ACCESS_TOKEN") or "").strip()


async def verify_access(
    request: Request,
    x_access_token: Optional[str] = Header(default=None, alias="X-Access-Token"),
    access_token: Optional[str] = None,
) -> None:
    """
    校验访问令牌：X-Access-Token 请求头优先，access_token 查询参数兜底

    查询参数兜底是给下载链接（<a href>）和预览 iframe 用的——浏览器原生
    请求带不了自定义 header。/api/health 豁免：存活探针不含任何数据，
    Docker 健康检查也不方便携带令牌
    """
    if not _access_token or request.url.path == "/api/health":
        return
    provided = x_access_token or access_token
    if not provided or not hmac.compare_digest(
        provided.encode(), _access_token.encode()
    ):
        raise HTTPException(
            status_code=401, detail="访问令牌缺失或不正确，请输入访问令牌后重试"
        )


app = FastAPI(title="DeepAgents API", lifespan=lifespan)

# HTTP 路由统一挂到带鉴权 dependency 的 router 上。
# 不能用 app 级全局依赖：FastAPI 会把它套到 WebSocket 路由，而 Request
# 参数无法注入 WS 路由（运行时 TypeError）；WebSocket 单独留在 app 并
# 在握手处自行校验令牌
api_router = APIRouter(dependencies=[Depends(verify_access)])

# 保存 thread_id -> 后台 Agent 任务，用于同一会话任务替换和主动取消
active_tasks: dict[str, asyncio.Task] = {}

# output 保存每个会话最终工作区，前端只允许从这里浏览和下载生成文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# updated 暂存用户上传文件，run_deep_agent 启动时会复制到对应 output/session_xxx
updated_dir = project_root / "updated"
updated_dir.mkdir(exist_ok=True)

# 前后端分别本地启动，跨域只放行本地 Vite 开发端口，不再使用通配符
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api_router.get("/api/health")
async def health():
    """
    存活探针 (Health Check)。

    只确认进程与路由可用，不触碰数据库与外部服务；Docker 编排的健康检查
    用它替代原先借用的 /api/sessions，语义清晰且不受会话数量影响。
    """
    return {"status": "ok"}


class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str
    thread_id: Optional[str] = None
    # 审批档位（off/standard/strict）；未传时使用服务端缺省
    # （HITL_APPROVAL_TOOLS 自定义清单或标准档）
    approval_mode: Optional[str] = None


def _forget_task(thread_id: str, task: asyncio.Task) -> None:
    """
    清理已结束任务的登记关系。

    done_callback 触发时，active_tasks 中可能已经被新任务替换；只有仍是同一个
    task 时才删除，避免误清理同 thread_id 下刚启动的新任务。
    """
    if active_tasks.get(thread_id) is task:
        active_tasks.pop(thread_id, None)


@api_router.post("/api/task")
async def run_task(request: TaskRequest):
    """
    启动一次 DeepAgents 后台任务。

    HTTP 请求只负责创建后台协程并立即返回，后续执行轨迹、子智能体调用和最终
    答案都会由 monitor 通过 `/ws/{thread_id}` 推送给同一会话的前端。
    """
    thread_id = request.thread_id or str(uuid.uuid4())

    # 同一个 thread_id 只保留一个活跃任务，新任务会先取消旧任务，避免并发写同一会话目录
    old_task = active_tasks.get(thread_id)
    if old_task and not old_task.done():
        old_task.cancel()

    # create_task 把长耗时 Agent 执行交给事件循环，接口本身不用等待最终结果
    task = asyncio.create_task(
        run_deep_agent(request.query, thread_id, request.approval_mode)
    )
    active_tasks[thread_id] = task
    task.add_done_callback(lambda finished_task: _forget_task(thread_id, finished_task))

    return {"status": "started", "thread_id": thread_id}


@api_router.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str):
    """
    取消指定 thread_id 对应的后台 Agent 任务。

    注意：取消会向 asyncio.Task 注入 CancelledError。若底层第三方工具正在执行不可中断
    的同步阻塞调用，任务可能需要等该调用返回后才会真正结束。
    """
    task = active_tasks.get(thread_id)
    if not task or task.done():
        active_tasks.pop(thread_id, None)
        # 任务暂停在待审批态：没有活跃协程可取消，此时以全部拒绝恢复执行，
        # 模型收到拒绝结果后自行收尾出 task_result，避免中断检查点悬挂、
        # 前端永远停在"等待确认"
        pending_actions = await get_pending_approval(thread_id)
        if pending_actions:
            task = asyncio.create_task(
                resume_deep_agent(
                    thread_id,
                    [{"type": "reject"} for _ in pending_actions],
                )
            )
            active_tasks[thread_id] = task
            task.add_done_callback(
                lambda finished_task: _forget_task(thread_id, finished_task)
            )
            return {
                "status": "cancelled",
                "thread_id": thread_id,
                "message": "任务正在等待审批，已按全部拒绝处理并由模型收尾",
            }
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    # 先发出取消信号，再短暂等待协程响应；若底层阻塞中，则返回 cancelling 给前端继续展示状态
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id}
    except asyncio.TimeoutError:
        return {"status": "cancelling", "thread_id": thread_id}
    except Exception as e:
        _forget_task(thread_id, task)
        return {"status": "cancelled", "thread_id": thread_id, "message": str(e)}

    _forget_task(thread_id, task)
    return {"status": "cancelled", "thread_id": thread_id}


class ApprovalDecision(BaseModel):
    """单个待审动作的人工决策；v1 支持 approve/reject，edit 预留扩展"""

    type: Literal["approve", "reject"]


class ApprovalRequest(BaseModel):
    """审批提交：decisions 顺序与 approval_required 事件的 actions 一一对应"""

    decisions: List[ApprovalDecision]


@api_router.post("/api/task/{thread_id}/approval")
async def submit_approval(thread_id: str, request: ApprovalRequest):
    """
    提交人工审批决策并恢复被中断的任务 (Approval Submit)。

    待审动作在后端重启后可从检查点重建；决策数量必须与待审动作数量一致，
    第 i 个决策作用于第 i 个待审动作
    """
    running = active_tasks.get(thread_id)
    if running and not running.done():
        raise HTTPException(
            status_code=409, detail="任务正在执行中，当前没有等待审批的操作"
        )

    pending_actions = await get_pending_approval(thread_id)
    if not pending_actions:
        raise HTTPException(status_code=409, detail="当前没有等待审批的任务")

    if len(request.decisions) != len(pending_actions):
        raise HTTPException(
            status_code=400,
            detail=(
                f"需要 {len(pending_actions)} 个决策"
                f"（对应 {len(pending_actions)} 个待审动作），"
                f"收到 {len(request.decisions)} 个"
            ),
        )

    active_tasks.pop(thread_id, None)
    task = asyncio.create_task(
        resume_deep_agent(
            thread_id, [decision.model_dump() for decision in request.decisions]
        )
    )
    active_tasks[thread_id] = task
    task.add_done_callback(lambda finished_task: _forget_task(thread_id, finished_task))

    return {"status": "resumed", "thread_id": thread_id}


@api_router.delete("/api/sessions/{thread_id}")
async def delete_session(thread_id: str):
    """
    删除指定历史会话 (Session Delete)。

    若该会话仍有后台任务在执行，先取消并等待其退出，再删除会话目录；
    output 产物目录与 updated 上传暂存目录一并清理，避免任务结束后
    重新写入事件导致删除不彻底。
    """
    # thread_id 会拼进目录名，只放行安全字符，防止路径穿越
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", thread_id):
        raise HTTPException(status_code=400, detail="非法的会话 ID")

    # 运行中的任务先取消：否则任务收尾时会重建会话目录或继续追加事件
    task = active_tasks.get(thread_id)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            # 取消超时（底层同步调用阻塞中）也继续删除目录，任务退出后发现
            # 目录已消失只会让文件写入报错，不会影响其他会话
            pass
        active_tasks.pop(thread_id, None)

    # 会话记忆与目录一同清理：checkpoints 已落盘，不复位的话前端复用同一
    # thread_id 重建会话时，删除前轮次的对话上下文会"复活"
    agent = await get_main_agent()
    checkpointer = getattr(agent, "checkpointer", None)
    if checkpointer is not None and hasattr(checkpointer, "adelete_thread"):
        try:
            await checkpointer.adelete_thread(thread_id)
        except Exception as e:
            # 记忆清理失败不阻断目录删除，但要在日志中可见
            print(f"[ERROR] 清理会话 checkpoint 失败: {e}")

    # 进程内的预算与来源登记同步清理，避免删除会话后残留孤儿数据；
    # 待审登记与文件基线（审批暂停中的会话）一并清理
    cleanup_task_budget(thread_id)
    cleanup_task_sources(thread_id)
    clear_task_runtime_state(thread_id)

    removed = False
    for base_dir in (output_dir, updated_dir):
        session_dir = (base_dir / f"session_{thread_id}").resolve()
        if not session_dir.is_relative_to(base_dir.resolve()):
            raise HTTPException(status_code=400, detail="非法的会话 ID")
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
            removed = True

    if not removed:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {"status": "deleted", "thread_id": thread_id}


# 上传限制：类型与 read_file_content 支持的格式对齐，大小/数量上限防磁盘占满
_UPLOAD_ALLOWED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".xlsx", ".xls", ".csv"}
_UPLOAD_MAX_FILE_SIZE = 50 * 1024 * 1024
_UPLOAD_MAX_FILES = 10


@api_router.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), thread_id: str = Form(...)):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件。
    2. 保存到 `updated/session_{thread_id}` 目录。
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    # 上传文件先按会话隔离保存，避免不同任务读取到彼此的附件
    target_dir = updated_dir / f"session_{thread_id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir_resolved = target_dir.resolve()

    if len(files) > _UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {_UPLOAD_MAX_FILES} 个文件",
        )

    saved_files = []
    for file in files:
        if not file.filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")

        # 扩展名白名单与 read_file_content 的解析能力对齐，
        # 同时拦下可执行文件与脚本类内容进入会话目录
        extension = Path(file.filename).suffix.lower()
        if extension not in _UPLOAD_ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"不支持的文件类型: {file.filename}"
                    f"（允许：{'、'.join(sorted(_UPLOAD_ALLOWED_EXTENSIONS))}）"
                ),
            )

        # file.filename 由客户端控制，resolve 后必须仍在上传目录内，防止 ../ 路径穿越
        file_path = (target_dir / file.filename).resolve()
        if not file_path.is_relative_to(target_dir_resolved):
            raise HTTPException(status_code=400, detail=f"非法文件名: {file.filename}")

        # 流式复制并累计大小：既避免大文件一次性读入内存，也能在超限时
        # 立即中止并清理半成品文件，而不是等整个文件写完
        try:
            with file_path.open("wb") as buffer:
                written = 0
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > _UPLOAD_MAX_FILE_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"文件过大: {file.filename}"
                                f"（单文件上限 {_UPLOAD_MAX_FILE_SIZE // (1024 * 1024)}MB）"
                            ),
                        )
                    buffer.write(chunk)
        except HTTPException:
            file_path.unlink(missing_ok=True)
            raise
        saved_files.append(file.filename)

    return {"status": "uploaded", "files": saved_files}


@api_router.get("/api/download")
async def download_file(path: str):
    """
    文件下载接口 (File Download)。

    目标：
    1. 根据绝对路径下载文件。
    2. 严格的安全检查，防止越权访问。

    Args:
        path (str): 文件的绝对路径 (通常从 list_files 接口获取)。
    """
    # resolve 后再做 is_relative_to，防止 `../` 之类的路径穿越到 output 之外；
    # 错误统一走 HTTPException（400/403/404），前端 requestJson 直接解析 detail
    abs_path = _resolve_within_output(path)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # FileResponse 会以流式响应返回文件内容，并让浏览器使用原文件名下载
    return FileResponse(abs_path, filename=abs_path.name)


# 预览接口放行的类型：浏览器可直接内联渲染且无脚本执行风险；
# SVG/HTML 可内嵌脚本，即便会话产物由后端生成也不放行内联
_INLINE_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _resolve_within_output(path: str) -> Path:
    """
    把客户端传入的路径解析并校验在 output 目录内，越界直接抛 HTTPException

    下载、预览、文件列表三个接口共用同一条安全边界
    """
    try:
        abs_path = Path(path).resolve()
        output_abs = output_dir.resolve()

        if not abs_path.is_relative_to(output_abs):
            raise HTTPException(
                status_code=403, detail="拒绝访问: 只能访问输出目录下的文件"
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="无效的路径参数")
    return abs_path


@api_router.get("/api/files/content")
async def get_file_content(path: str):
    """
    应用内预览接口 (Inline File Content)。

    与 /api/download 的差别只在 Content-Disposition：下载接口强制 attachment
    （浏览器直接保存），本接口强制 inline，Markdown 文本可被 fetch 读取、
    PDF 与图片可直接塞进 iframe/img 渲染。路径安全边界与下载接口一致。
    """
    abs_path = _resolve_within_output(path)

    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type = _INLINE_CONTENT_TYPES.get(abs_path.suffix.lower())
    if media_type is None:
        raise HTTPException(
            status_code=415,
            detail=f"该文件类型不支持应用内预览: {abs_path.suffix or '(无后缀)'}",
        )

    return FileResponse(
        abs_path,
        media_type=media_type,
        filename=abs_path.name,
        content_disposition_type="inline",
    )


@api_router.get("/api/sessions")
async def list_sessions():
    """
    历史会话列表接口 (Session List)。

    目标：
    1. 枚举 output/ 下的 session_* 目录，作为可恢复的历史会话。
    2. 返回每个会话的摘要（标题、更新时间、文件数、事件数），供前端侧边栏渲染。
    """
    sessions = []
    for session_dir in output_dir.glob("session_*"):
        if not session_dir.is_dir():
            continue

        thread_id = session_dir.name[len("session_") :]
        events_file = session_dir / "events.jsonl"

        # 标题取第一条 task_start 的原始问题；无事件记录时回退为空
        title = ""
        event_count = 0
        if events_file.exists():
            try:
                with events_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        event_count += 1
                        if not title:
                            try:
                                payload = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if payload.get("event") == "task_start":
                                query = payload.get("data", {}).get("query", "")
                                title = str(query)[:60]
            except OSError as e:
                print(f"[ERROR] 读取事件文件失败: {e}")

        # 目录 mtime 加上产物文件 mtime 取最大值，反映会话最近一次活动时间
        latest_mtime = session_dir.stat().st_mtime
        file_count = 0
        for file_path in session_dir.rglob("*"):
            if file_path.is_file():
                # 事件日志是会话内部数据，不计入对外展示的文件数
                if file_path.name == "events.jsonl":
                    continue
                file_count += 1
                latest_mtime = max(latest_mtime, file_path.stat().st_mtime)

        sessions.append(
            {
                "thread_id": thread_id,
                "title": title,
                "mtime": latest_mtime,
                "file_count": file_count,
                "event_count": event_count,
            }
        )

    # 最近使用的会话排在前面
    sessions.sort(key=lambda item: item.get("mtime", 0), reverse=True)
    return {"sessions": sessions}


@api_router.get("/api/sessions/{thread_id}/events")
async def get_session_events(thread_id: str):
    """
    历史会话事件回放接口 (Session Event Replay)。

    目标：
    1. 返回指定会话落盘的 events.jsonl 全量事件，供前端重启后重建对话。
    """
    # thread_id 会拼进会话目录名，只放行安全字符，防止路径穿越
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", thread_id):
        raise HTTPException(status_code=400, detail="非法的会话 ID")

    events_file = (output_dir / f"session_{thread_id}" / "events.jsonl").resolve()
    if not events_file.is_relative_to(output_dir.resolve()):
        raise HTTPException(status_code=400, detail="非法的会话 ID")

    if not events_file.exists():
        return {"events": []}

    events = []
    try:
        with events_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取事件文件失败: {e}")

    return {"events": events}


@api_router.post("/api/files/reveal")
async def reveal_in_folder(path: str):
    """
    在操作系统的文件管理器中打开并选中指定产物文件。

    浏览器沙箱无法直接调起本地资源管理器，只能由后端进程代为执行；
    路径边界校验与下载接口保持一致：仅允许 output 目录内的文件。
    """
    abs_path = _resolve_within_output(path)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    try:
        if sys.platform == "win32":
            # /select 让资源管理器打开所在目录并选中该文件
            subprocess.Popen(["explorer", "/select,", str(abs_path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(abs_path)])
        else:
            subprocess.Popen(["xdg-open", str(abs_path.parent)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开文件管理器失败: {e}")

    return {"status": "revealed", "path": str(abs_path)}


@api_router.get("/api/files")
async def list_files(path: str):
    """
    文件列表查询接口 (File Explorer)。

    目标：
    1. 列出指定目录下的所有生成文件。
    2. 提供文件元数据（大小、修改时间、下载所需路径）。
    3. 严格的安全检查，防止路径遍历攻击。

    Args:
        path (str): 目标目录的绝对路径 (必须在 output 目录下)。
    """
    # 和下载接口保持同一条安全边界：前端只能查看 output 目录内部内容
    abs_path = _resolve_within_output(path)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="目录不存在")

    files = []
    try:
        # 递归返回文件元数据，前端据此渲染文件列表并发起下载请求
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                # events.jsonl 是会话事件日志，不是任务产物，不进入文件列表
                if file_path.name == "events.jsonl":
                    continue
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": str(file_path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except OSError as e:
        raise HTTPException(status_code=500, detail=f"遍历文件失败: {e}")

    # 最新生成的文件排在前面，方便用户优先看到本次任务产物
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"files": files}


# HTTP 路由统一注册；WebSocket 单独挂在 app 上（不带鉴权 dependency，
# Request 无法注入 WS 路由），握手处在 endpoint 内自行校验令牌
app.include_router(api_router)


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    连接建立后，ConnectionManager 会用 thread_id 保存 WebSocket。monitor 后续
    发送事件时只需要按 thread_id 查找连接，就能把进度推给对应页面。循环中的
    receive_text 用于接收前端心跳，避免连接空闲断开。
    """
    print(f"会话向我们发起了请求，要求建立连接：{thread_id} 对应：{websocket}")

    # 令牌校验先于注册：accept 后立即以策略码关闭，前端能从 close code
    # 1008 识别"令牌无效"并弹窗补录
    if _access_token:
        provided_token = websocket.query_params.get("access_token")
        if not provided_token or not hmac.compare_digest(
            provided_token.encode(), _access_token.encode()
        ):
            await websocket.accept()
            await websocket.close(code=1008, reason="访问令牌缺失或不正确")
            return

    # 连接建立后立即按 thread_id 注册，monitor 后续才能把事件定向推给当前页面
    await manager.connect(websocket, thread_id)

    # 前端重启重连时不会再收到当时的 session_created，这里对已存在的会话补发一次，
    # 让前端立即恢复 sessionPath 并能列出历史产物文件
    existing_session_dir = (output_dir / f"session_{thread_id}").resolve()
    if existing_session_dir.is_dir() and existing_session_dir.is_relative_to(
        output_dir.resolve()
    ):
        await websocket.send_json(
            {
                "type": "monitor_event",
                "event": "session_created",
                "message": f"工作目录已恢复: {existing_session_dir}",
                "data": {"path": str(existing_session_dir).replace("\\", "/")},
                "timestamp": datetime.datetime.now().isoformat(),
            }
        )

    try:
        while True:
            # 前端通常发送 ping 心跳；服务端回复 pong，顺便维持连接活跃
            data = await websocket.receive_text()
            await websocket.send_json(
                {"type": "pong", "message": f"服务端已收到: {data}"}
            )

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, thread_id)
        print(f"[WebSocket] 客户端已断开: {thread_id}")

    except Exception as e:
        print(f"[WebSocket] 连接异常: {e}")
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    # 默认只监听本机回环，避免无鉴权服务暴露到局域网；需要外部访问时显式传 --host
    uvicorn.run("app.api.server:app", host="127.0.0.1", port=8000, reload=True)
