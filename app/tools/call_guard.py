"""
外部调用统一防护层

为工具调用提供四项能力：超时、有限重试、错误分类、tool_start/tool_end 埋点。
tavily 和 ragflow 的同步客户端都通过 asyncio.to_thread 放入线程池执行，
再用 wait_for 施加超时——这样 Agent 的取消信号能立即生效，而不用像过去那样
等同步 HTTP 调用自然返回。

错误分两类处理：
- 可重试：网络超时、连接失败、429、临时性 5xx —— 按指数退避 + 随机抖动重试
- 不可重试：参数错误、鉴权失败、资源不存在 —— 立即失败，避免浪费调用

注意：wait_for 超时后线程池里的线程无法真正终止，会继续在后台跑完。
因此 max_retries 必须与 timeout_s 一起收敛（例如 2 次重试 × 15 秒超时，
最坏约 45 秒 + 退避），防止孤儿线程累积。
"""

import asyncio
import random
import time
from typing import Any, Callable, Optional

import requests

from app.api.budget import consume_tool_quota
from app.api.monitor import monitor

# 这些状态码代表外部服务临时异常，值得重试；其余（4xx 参数/权限类）不值得
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# 事件摘要的最大长度，避免大结果把 events.jsonl 和 WebSocket 撑爆
_SUMMARY_MAX_LEN = 200


class ExternalCallError(Exception):
    """
    外部调用最终失败（重试耗尽或不可重试）

    工具捕获后转为面向模型的错误文本；error_type 保留分类，
    供前端和监控系统区分超时、网络、服务端错误等失败原因
    """

    def __init__(self, message: str, error_type: str, attempts: int):
        super().__init__(message)
        self.error_type = error_type
        self.attempts = attempts


def _status_code_of(exc: BaseException) -> Optional[int]:
    """从异常对象上尽力提取 HTTP 状态码（requests.HTTPError 挂在 response 上）"""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_retryable(exc: BaseException) -> bool:
    """
    判断异常是否值得重试

    覆盖三类来源：
    1. requests 网络层异常（超时、连接失败、连接被重置）
    2. tavily 自定义 TimeoutError——它与内建 TimeoutError 同名但没有继承关系，
       按模块名识别，避免对 tavily 包产生硬编码 import 依赖
    3. 携带可重试状态码的 HTTPError（tavily 对非约定错误走 raise_for_status）
    ragflow SDK 对非 2xx 一律 raise Exception(message)，无法从类型上区分，
    一律视为不可重试——它的提问操作不幂等，宁可少试不可重复提交
    """
    # wait_for 的超时（Python 3.11+ 中 asyncio.TimeoutError 即内建 TimeoutError）
    if isinstance(exc, asyncio.TimeoutError):
        return True

    if isinstance(
        exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    ):
        return True

    if type(exc).__name__ == "TimeoutError" and type(exc).__module__.startswith(
        "tavily"
    ):
        return True

    status = _status_code_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUS_CODES

    return False


def _classify(exc: BaseException) -> str:
    """把异常归入粗粒度错误类型，写入 tool_end 事件便于监控区分"""
    if isinstance(exc, asyncio.TimeoutError) or (
        type(exc).__name__ == "TimeoutError"
        and type(exc).__module__.startswith("tavily")
    ):
        return "timeout"
    if isinstance(
        exc,
        (requests.exceptions.Timeout, requests.exceptions.ConnectionError),
    ):
        return "network"
    status = _status_code_of(exc)
    if status is not None:
        return f"http_{status}"
    return "non_retryable"


def _summarize(result: Any) -> str:
    """生成结果摘要：转字符串后截断，只进事件日志不进模型上下文"""
    text = str(result).replace("\n", " ").strip()
    if len(text) > _SUMMARY_MAX_LEN:
        return text[:_SUMMARY_MAX_LEN] + "..."
    return text


async def guarded_call(
    tool_name: str,
    args: Optional[dict],
    sync_fn: Callable[[], Any],
    *,
    display_name: Optional[str] = None,
    timeout_s: float = 15.0,
    max_retries: int = 0,
    backoff_base_s: float = 1.0,
    budgeted: bool = False,
) -> Any:
    """
    执行一次受防护的工具调用

    :param tool_name: 工具注册名，用于预算限额和去重指纹（与 budget.py 限额表对齐）
    :param args: 调用参数，用于埋点展示和预算/去重指纹
    :param sync_fn: 无参可调用对象（闭包捕获真实参数后执行同步外部请求）
    :param display_name: 事件日志中的显示名（保持前端原有中文展示）；缺省用注册名
    :param timeout_s: 单次尝试的超时上限
    :param max_retries: 额外重试次数，总尝试次数 = 1 + max_retries
    :param backoff_base_s: 退避基数，第 n 次重试等待 base * 2^(n-1) * 抖动
    :param budgeted: 是否启用调用预算与重复检测
    :return: 成功时返回 sync_fn 的原始返回值；被预算拦截时返回面向模型的拦截说明
    :raises ExternalCallError: 重试耗尽或不可重试错误
    """
    display = display_name or tool_name

    # 预算与去重检查先于一切执行：被拦截的调用不产生 tool_start，也不消耗重试
    if budgeted:
        allowed, reason = consume_tool_quota(tool_name, args)
        if not allowed:
            monitor.report_budget_exceeded(display, reason)
            monitor.report_tool_end(
                display,
                duration_ms=0,
                status="blocked",
                summary=reason,
            )
            return reason

    monitor.report_tool(display, args)
    started = time.perf_counter()

    attempts = 0
    last_error: Optional[ExternalCallError] = None

    while attempts <= max_retries:
        attempts += 1
        try:
            # to_thread 让同步客户端在线程池中执行，wait_for 保证超时和取消可生效
            result = await asyncio.wait_for(
                asyncio.to_thread(sync_fn), timeout=timeout_s
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            monitor.report_tool_end(
                display,
                duration_ms=duration_ms,
                status="success",
                retries=attempts - 1,
                summary=_summarize(result),
            )
            return result
        except asyncio.CancelledError:
            # 用户取消任务必须原样上抛给 LangGraph，绝不吞掉
            raise
        except Exception as exc:
            error_type = _classify(exc)
            if error_type == "timeout":
                message = f"{display} 调用超时（单次上限 {timeout_s} 秒）"
            else:
                message = f"{display} 调用失败：{type(exc).__name__}: {exc}"
            last_error = ExternalCallError(message, error_type, attempts)

            if not is_retryable(exc) or attempts > max_retries:
                break

            # 指数退避 + 随机抖动，避免多个任务同步重试打垮外部服务
            delay = backoff_base_s * (2 ** (attempts - 1)) * (0.5 + random.random())
            await asyncio.sleep(delay)

    duration_ms = int((time.perf_counter() - started) * 1000)
    monitor.report_tool_end(
        display,
        duration_ms=duration_ms,
        status="failed",
        retries=attempts - 1,
        error_type=last_error.error_type if last_error else "unknown",
        summary=str(last_error) if last_error else "",
    )
    raise last_error
