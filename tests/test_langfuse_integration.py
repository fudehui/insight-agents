"""
Langfuse 可选观测接入的单元测试（W4）

不依赖真实 Langfuse 服务：覆盖未配置密钥零开销旁路、配置假密钥时 handler
进入 callbacks、langfuse 不可用时降级为 None + warning。
"""

import json
import logging
from unittest import mock

from app.agent import main_agent
from app.observability.langfuse_callback import build_langfuse_handler


def test_no_config_returns_none(monkeypatch):
    """三项密钥任一缺失 → 不加载 langfuse、返回 None（零开销旁路）"""
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(var, raising=False)
    # 屏蔽真实 import：即便环境意外装了包也不应触达
    with mock.patch.dict("sys.modules", {"langfuse": None, "langfuse.langchain": None}):
        assert build_langfuse_handler() is None


def test_configured_returns_handler_and_mounts(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")

    handler = build_langfuse_handler()

    # 真实构造（本地无服务也会成功——CallbackHandler 构造不联网）
    assert handler is not None
    # 挂进 RunnableConfig：未配置时只有用量 handler，配置后追加 langfuse
    config = main_agent._build_agent_config("langfuse_test_thread")
    names = [type(c).__name__ for c in config["callbacks"]]
    assert "TokenUsageCallbackHandler" in names
    assert "LangchainCallbackHandler" in names  # langfuse 4.x 的类名


def test_import_failure_degrades_to_none(monkeypatch, caplog):
    """langfuse 包缺失/构造异常 → None + warning，绝不阻塞业务"""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")

    import builtins

    real_import = builtins.__import__

    def boom(name, *args, **kwargs):
        if name.startswith("langfuse"):
            raise ImportError("模拟 langfuse 未安装")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=boom):
        with caplog.at_level(logging.WARNING):
            assert build_langfuse_handler() is None

    assert any("构造失败" in r.message for r in caplog.records)


def test_config_json_contract_unchanged():
    """挂载 Langfuse 后 RunnableConfig 的既有契约不变（thread_id / recursion_limit）"""
    config = main_agent._build_agent_config("contract_thread")
    assert config["configurable"]["thread_id"] == "contract_thread"
    assert isinstance(config["recursion_limit"], int)
    json.dumps({"thread": config["configurable"]["thread_id"]})  # 可序列化
