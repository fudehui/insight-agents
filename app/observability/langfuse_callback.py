"""
Langfuse 全链路 tracing 接入（P0-1 W4，降格口径：接入即通，不做打磨）

设计原则（升级计划 P1 表格的"可选观测组件"约束）：
- 零配置零开销：LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
  三者任一未配置时不 import langfuse（该依赖较重，不拖累无观测需求的部署）；
- 永不弄挂业务：import / 构造的任何异常捕获后打 warning 并返回 None——
  观测组件的可用性永远低于任务执行本身；
- 回调随 RunnableConfig 穿透子智能体（与 TokenUsageCallbackHandler 同通道），
  主 + 子全部模型调用都会上报 Langfuse trace。
"""

import logging
import os

logger = logging.getLogger(__name__)


def build_langfuse_handler():
    """
    构造 Langfuse 的 LangChain CallbackHandler；未配置密钥或构造失败返回 None

    :return: langfuse.langchain.CallbackHandler 实例或 None
    """
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST")
    if not (public_key and secret_key and host):
        return None
    try:
        # 延迟导入：未配置密钥的部署完全不加载 langfuse
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as e:  # noqa: BLE001 观测组件绝不阻塞任务执行
        logger.warning("Langfuse CallbackHandler 构造失败，本次任务不启用 tracing：%s", e)
        return None
