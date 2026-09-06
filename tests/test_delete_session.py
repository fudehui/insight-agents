"""
会话删除接口的单元测试

用 FastAPI TestClient 验证：目录删除、上传暂存清理、非法 ID 拦截、
会话不存在时的 404，以及运行中任务的先取消后删除
"""

from unittest import mock

from fastapi.testclient import TestClient

from app.api import server


def _fake_get_main_agent(agent):
    """替代 get_main_agent 的假工厂：返回一个返回假 agent 的异步工厂"""

    async def _factory():
        return agent

    return _factory


def _make_session(tmp_path, thread_id="del123", with_upload=True):
    """在临时目录中构造一个已存在会话的 output/updated 目录结构"""
    output_dir = tmp_path / "output"
    updated_dir = tmp_path / "updated"
    output_dir.mkdir(exist_ok=True)
    updated_dir.mkdir(exist_ok=True)

    session_dir = output_dir / f"session_{thread_id}"
    session_dir.mkdir()
    (session_dir / "report.md").write_text("报告", encoding="utf-8")
    (session_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")

    if with_upload:
        upload_dir = updated_dir / f"session_{thread_id}"
        upload_dir.mkdir()
        (upload_dir / "附件.txt").write_text("附件", encoding="utf-8")

    return output_dir, updated_dir, session_dir


def test_delete_session_removes_output_and_upload_dirs(tmp_path, monkeypatch):
    output_dir, updated_dir, session_dir = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    client = TestClient(server.app)
    response = client.delete("/api/sessions/del123")

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert not session_dir.exists()
    assert not (updated_dir / "session_del123").exists()


def test_delete_session_rejects_path_traversal(tmp_path, monkeypatch):
    output_dir, updated_dir, _ = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    client = TestClient(server.app)
    # %2e%2e 解码后是 ".."，直达路由并被字符集校验拦截，返回 400
    response = client.delete("/api/sessions/%2e%2e")
    assert response.status_code == 400

    # 含编码斜杠的 ID 在路由层就被拒绝（404），同样不会进入删除逻辑
    response = client.delete("/api/sessions/abc%2fdef")
    assert response.status_code == 404

    assert (output_dir / "session_del123").exists()


def test_delete_session_returns_404_when_missing(tmp_path, monkeypatch):
    output_dir, updated_dir, _ = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    client = TestClient(server.app)
    response = client.delete("/api/sessions/no_such_session")

    assert response.status_code == 404


def test_delete_session_cancels_running_task_first(tmp_path, monkeypatch):
    output_dir, updated_dir, session_dir = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    # 用假任务对象验证接口行为：运行中的任务先被 cancel，再删除目录
    fake_task = mock.Mock()
    fake_task.done.return_value = False

    async def fake_wait_for(target, timeout):
        return None

    monkeypatch.setattr(server.asyncio, "wait_for", fake_wait_for)
    server.active_tasks["del123"] = fake_task

    try:
        client = TestClient(server.app)
        response = client.delete("/api/sessions/del123")

        assert response.status_code == 200
        fake_task.cancel.assert_called_once()
        assert "del123" not in server.active_tasks
        assert not session_dir.exists()
    finally:
        server.active_tasks.pop("del123", None)


def test_delete_session_purges_checkpoint_memory(tmp_path, monkeypatch):
    output_dir, updated_dir, _ = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    # 假 agent 验证：删除会话目录的同时按 thread_id 清理落盘的会话记忆
    fake_agent = mock.Mock()
    monkeypatch.setattr(server, "get_main_agent", _fake_get_main_agent(fake_agent))

    client = TestClient(server.app)
    response = client.delete("/api/sessions/del123")

    assert response.status_code == 200
    fake_agent.checkpointer.adelete_thread.assert_called_once_with("del123")


def test_delete_session_skips_purge_without_checkpointer(tmp_path, monkeypatch):
    output_dir, updated_dir, session_dir = _make_session(tmp_path)
    monkeypatch.setattr(server, "output_dir", output_dir)
    monkeypatch.setattr(server, "updated_dir", updated_dir)

    # checkpointer 缺失（如退回内存 saver 的旧实现）时不应报错，目录删除照常完成
    monkeypatch.setattr(server, "get_main_agent", _fake_get_main_agent(object()))

    client = TestClient(server.app)
    response = client.delete("/api/sessions/del123")

    assert response.status_code == 200
    assert not session_dir.exists()
