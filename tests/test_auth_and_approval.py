"""
访问令牌鉴权与人工审批端点的接口测试

覆盖：令牌缺失/错误拒绝、header 与 query 两种携带方式、health 豁免、
未配置令牌时全放行、WebSocket 握手令牌校验、审批端点的无待审/决策数校验
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.server as server
from app.agent import main_agent as main_agent_module


@pytest.fixture
def client():
    """未启用鉴权的默认客户端"""
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def secured_client(monkeypatch):
    """启用访问令牌（APP_ACCESS_TOKEN=secret-token）的客户端"""
    monkeypatch.setattr(server, "_access_token", "secret-token")
    with TestClient(server.app) as test_client:
        yield test_client


class TestAccessTokenAuth:
    def test_health_exempt_from_auth(self, secured_client):
        # 存活探针豁免：Docker 健康检查不带令牌也要可用
        response = secured_client.get("/api/health")
        assert response.status_code == 200

    def test_missing_token_rejected(self, secured_client):
        assert secured_client.get("/api/sessions").status_code == 401

    def test_header_token_accepted(self, secured_client):
        response = secured_client.get(
            "/api/sessions", headers={"X-Access-Token": "secret-token"}
        )
        assert response.status_code == 200

    def test_query_token_accepted(self, secured_client):
        # 下载/预览这类浏览器原生请求带不了 header，走查询参数
        response = secured_client.get("/api/sessions?access_token=secret-token")
        assert response.status_code == 200

    def test_wrong_token_rejected(self, secured_client):
        response = secured_client.get(
            "/api/sessions", headers={"X-Access-Token": "wrong-token"}
        )
        assert response.status_code == 401

    def test_without_token_config_all_requests_allowed(self, client):
        # 未配置 APP_ACCESS_TOKEN 时保持本地开发零负担的现状
        assert client.get("/api/sessions").status_code == 200

    def test_ws_wrong_token_closed(self, secured_client):
        # 后端 accept 后立即以 1008 关闭：读取消息时抛 WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with secured_client.websocket_connect(
                "/ws/t-auth?access_token=bad"
            ) as websocket:
                websocket.receive_text()

    def test_ws_correct_token_receives_pong(self, secured_client):
        with secured_client.websocket_connect(
            "/ws/t-auth-ok?access_token=secret-token"
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json()["type"] == "pong"


class TestApprovalEndpoint:
    def test_without_pending_approval_conflict(self, client):
        response = client.post(
            "/api/task/no-such-thread-xyz/approval",
            json={"decisions": [{"type": "approve"}]},
        )
        assert response.status_code == 409

    def test_decision_count_mismatch_rejected(self, client, monkeypatch):
        monkeypatch.setitem(
            main_agent_module._pending_approvals,
            "t-pending",
            {
                "actions": [
                    {
                        "name": "generate_markdown",
                        "args": {},
                        "allowed_decisions": ["approve", "reject"],
                    }
                ]
            },
        )
        response = client.post(
            "/api/task/t-pending/approval",
            json={"decisions": [{"type": "approve"}, {"type": "reject"}]},
        )
        assert response.status_code == 400

    def test_valid_decisions_spawn_resume(self, client, monkeypatch):
        monkeypatch.setitem(
            main_agent_module._pending_approvals,
            "t-resume",
            {
                "actions": [
                    {
                        "name": "generate_markdown",
                        "args": {},
                        "allowed_decisions": ["approve", "reject"],
                    },
                    {
                        "name": "convert_md_to_pdf",
                        "args": {},
                        "allowed_decisions": ["approve", "reject"],
                    },
                ]
            },
        )
        captured = {}

        async def fake_resume(thread_id, decisions):
            captured["args"] = (thread_id, decisions)

        monkeypatch.setattr(server, "resume_deep_agent", fake_resume)
        response = client.post(
            "/api/task/t-resume/approval",
            json={"decisions": [{"type": "approve"}, {"type": "reject"}]},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "resumed", "thread_id": "t-resume"}
        # 恢复任务经 create_task 调度，与响应返回存在时序竞争，轮询等待其执行
        import time

        deadline = time.time() + 2
        while "args" not in captured and time.time() < deadline:
            time.sleep(0.01)
        assert captured["args"] == (
            "t-resume",
            [{"type": "approve"}, {"type": "reject"}],
        )
