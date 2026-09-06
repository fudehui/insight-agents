"""
审批档位路由的单元测试

覆盖：档位归一化（合法/未传/未知回落服务端缺省）、按档位的 agent 实例
缓存（同档复用、跨档独立、全部共享检查点）、任务请求的档位透传
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agent import main_agent as main_agent_module
from app.agent.main_agent import (
    APPROVAL_MODE_PRESETS,
    _resolve_approval_mode,
    get_main_agent,
)
from app.api import server as server_module


@pytest.fixture
def client():
    with TestClient(server_module.app) as test_client:
        yield test_client


class TestResolveApprovalMode:
    def test_valid_modes_pass_through(self):
        assert _resolve_approval_mode("off") == "off"
        assert _resolve_approval_mode("standard") == "standard"
        assert _resolve_approval_mode("strict") == "strict"

    def test_missing_mode_falls_back_to_server_default(self, monkeypatch):
        monkeypatch.delenv("HITL_APPROVAL_TOOLS", raising=False)
        # env 未设置时缺省为标准档
        assert _resolve_approval_mode(None) == "server_default"

    def test_unknown_mode_falls_back_to_server_default(self):
        assert _resolve_approval_mode("yolo") == "server_default"

    def test_server_default_uses_env_custom_list(self, monkeypatch):
        monkeypatch.setenv("HITL_APPROVAL_TOOLS", "internet_search")
        interrupt_on = main_agent_module._interrupt_on_for("server_default")
        assert interrupt_on == {"internet_search": True}

    def test_presets_cover_three_modes(self):
        assert APPROVAL_MODE_PRESETS["off"] == {}
        assert "generate_markdown" in APPROVAL_MODE_PRESETS["standard"]
        strict = APPROVAL_MODE_PRESETS["strict"]
        assert {"generate_markdown", "execute_sql_query", "internet_search"} <= set(
            strict
        )


class TestAgentCacheByMode:
    def test_agent_cache_by_mode(self):
        # 全部在同一个事件循环内执行：aiosqlite 连接绑定创建时的循环，
        # 生产环境单事件循环运行，这里保持一致
        async def scenario():
            standard_first = await get_main_agent("standard")
            standard_second = await get_main_agent("standard")
            strict = await get_main_agent("strict")
            off = await get_main_agent("off")
            return standard_first, standard_second, strict, off

        try:
            standard_first, standard_second, strict, off = asyncio.run(scenario())
            # 同档位复用同一实例
            assert standard_first is standard_second
            # 跨档位实例独立
            assert standard_first is not strict
            assert standard_first is not off
            assert strict is not off
            # 所有实例共享同一检查点（切换档位不影响待审批任务恢复）
            assert main_agent_module._checkpointer is not None
        finally:
            # 关闭 aiosqlite 连接线程并清空模块缓存：连接线程是非守护线程，
            # 不关闭的话 asyncio.run 结束后解释器会一直等它退出（表现为测试
            # 全部通过但进程挂死）；缓存的实例绑定已关闭的连接也会影响
            # 同进程内后续用例，一并复位
            if main_agent_module._checkpointer is not None:
                asyncio.run(main_agent_module._checkpointer.conn.close())
            main_agent_module._agents.clear()
            main_agent_module._checkpointer = None


class TestTaskRequestModePassThrough:
    def test_task_endpoint_forwards_mode(self, client, monkeypatch):
        captured = {}

        async def fake_run(query, thread_id, approval_mode=None):
            captured["args"] = (query, thread_id, approval_mode)

        monkeypatch.setattr(server_module, "run_deep_agent", fake_run)
        response = client.post(
            "/api/task",
            json={
                "query": "测试任务",
                "thread_id": "t-mode",
                "approval_mode": "strict",
            },
        )
        assert response.status_code == 200
        assert captured["args"] == ("测试任务", "t-mode", "strict")

    def test_task_endpoint_without_mode_passes_none(self, client, monkeypatch):
        captured = {}

        async def fake_run(query, thread_id, approval_mode=None):
            captured["args"] = (query, thread_id, approval_mode)

        monkeypatch.setattr(server_module, "run_deep_agent", fake_run)
        response = client.post("/api/task", json={"query": "测试任务"})
        assert response.status_code == 200
        assert captured["args"][2] is None
