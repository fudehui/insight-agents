"""
大模型初始化模块

负责从 .env 中读取模型配置，并创建项目统一复用的模型对象
后续主智能体和子智能体都从这里导入 model，避免在多个文件里重复加载环境变量
"""

import os

from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

# find_dotenv 会从当前目录向上查找 .env，适合脚本和 Web 服务从不同入口启动的场景
load_dotenv(find_dotenv())

_model_name = os.getenv("LLM_QWEN_MAX")
if not _model_name:
    raise RuntimeError(
        "未配置模型名：请在项目 .env 中设置 LLM_QWEN_MAX（如 LLM_QWEN_MAX=qwen-max），"
        "否则主智能体与子智能体无法初始化。"
    )

# 使用 OpenAI 兼容协议接入模型；具体模型名由 .env 中的 LLM_QWEN_MAX 控制。
# 默认 HTTP 超时长达 600 秒，模型一次挂起就会拖住整个任务，这里显式收敛；
# 超时与重试次数均可通过环境变量按需调整
model = init_chat_model(
    model=_model_name,
    model_provider="openai",
    timeout=float(os.getenv("LLM_TIMEOUT_S", "300")),
    max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
)
