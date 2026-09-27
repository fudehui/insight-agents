"""记忆 API（PATCH /api/reports、DELETE /api/memories）与注入验收链路测试

P0-3 验收口径：修改报告结论 → pending 暂存 → memory_write 审批入库 →
删除会话不影响记忆库 → 新会话同主题任务先验注入生效。
"""

import asyncio
import hashlib
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.memory import store as store_module
from app.memory.prior import build_memory_injection
from app.tools import memory_tools


@pytest.fixture()
def mem_store(tmp_path):
    """把进程级单例指向临时库：API 与 memory_write 共用同一替身"""
    s = store_module.SqliteVecStore(tmp_path / "memory.db")
    saved = store_module._store
    store_module._store = s
    yield s
    store_module._store = saved
    s.close()


@pytest.fixture()
def client():
    with TestClient(server_module.app) as test_client:
        yield test_client


@pytest.fixture()
def report_file(tmp_path, monkeypatch):
    """把 output 目录指向临时区并预置一份报告（resolve 校验跟随 output_dir）"""
    out_dir = Path(tempfile.mkdtemp(prefix="mem_out_"))
    monkeypatch.setattr(server_module, "output_dir", out_dir)
    session_dir = out_dir / "session_eval-s1"
    session_dir.mkdir()
    report = session_dir / "销售分析报告.md"
    content = "# 销售分析报告\n\n12月销售额为零。\n"
    report.write_text(content, encoding="utf-8", newline="\n")
    return report


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha(path) -> str:
    """与前端/服务端一致的乐观锁口径：对磁盘原始字节做 sha256"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# PATCH /api/reports
# ---------------------------------------------------------------------------


def test_patch_requires_token(report_file, monkeypatch):
    """必须挂 api_router 继承 verify_access：配置令牌后无凭据 401"""
    monkeypatch.setattr(server_module, "_access_token", "tok")
    client = TestClient(server_module.app)
    resp = client.patch(
        "/api/reports",
        json={
            "session_id": "eval-s1",
            "filename": "销售分析报告.md",
            "base_sha256": _file_sha(report_file),
            "content": "# 销售分析报告\n\n修订后的结论。\n",
        },
    )
    assert resp.status_code == 401


def test_patch_saves_file_and_pends_revision(report_file, mem_store, monkeypatch):
    monkeypatch.setattr(server_module, "_access_token", "")
    client = TestClient(server_module.app)
    base_sha = _file_sha(report_file)
    new_content = "# 销售分析报告\n\n12月因促销回升 5%。\n"

    resp = client.patch(
        "/api/reports",
        json={
            "session_id": "eval-s1",
            "filename": "销售分析报告.md",
            "base_sha256": base_sha,
            "content": new_content,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["new_sha256"] == _sha(new_content)
    assert report_file.read_text(encoding="utf-8") == new_content
    pending = mem_store.list_pending_revisions()
    assert len(pending) == 1
    assert pending[0]["topic"] == "销售分析报告"


def test_patch_optimistic_lock_conflict(report_file, mem_store, monkeypatch):
    """基线版本号过期返回 409（agent 收尾重写与用户编辑互踩的防护）"""
    monkeypatch.setattr(server_module, "_access_token", "")
    client = TestClient(server_module.app)

    resp = client.patch(
        "/api/reports",
        json={
            "session_id": "eval-s1",
            "filename": "销售分析报告.md",
            "base_sha256": "stale-hash",
            "content": "# 新内容\n",
        },
    )
    assert resp.status_code == 409


def test_patch_rejects_path_traversal(monkeypatch):
    """路径守卫复用 _resolve_within_output：越界路径拒绝"""
    monkeypatch.setattr(server_module, "_access_token", "")
    client = TestClient(server_module.app)

    resp = client.patch(
        "/api/reports",
        json={
            "session_id": "../..",
            "filename": "app/data/deepsearch.db",
            "base_sha256": "x",
            "content": "y",
        },
    )
    assert resp.status_code in (403, 404)


def test_patch_rejects_non_markdown(tmp_path, monkeypatch, mem_store):
    """PDF/图片是导出产物，只开放 Markdown 编辑（编辑降级约定）"""
    monkeypatch.setattr(server_module, "_access_token", "")
    out_dir = Path(tempfile.mkdtemp())
    monkeypatch.setattr(server_module, "output_dir", out_dir)
    pdf = out_dir / "session_eval-s1"
    pdf.mkdir()
    (pdf / "报告.pdf").write_bytes(b"%PDF-1.4 fake")
    client = TestClient(server_module.app)

    resp = client.patch(
        "/api/reports",
        json={
            "session_id": "eval-s1",
            "filename": "报告.pdf",
            "base_sha256": "x",
            "content": "y",
        },
    )
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# memory_write 工具（审批由 APPROVAL_MODE_PRESETS 拦截，此处测写入语义）
# ---------------------------------------------------------------------------


def test_memory_write_roundtrip_and_pending_close(mem_store):
    """修订入库后关单；敏感内容被拒"""
    async def scenario():
        ok = await memory_tools.memory_write.ainvoke(
            {
                "kind": "revision",
                "content": "用户修订：12月销售额结论改为因促销回升 5%",
                "source_session_id": "eval-s1",
            }
        )
        secret = await memory_tools.memory_write.ainvoke(
            {"kind": "preference", "content": "我的手机号 13812345678"}
        )
        return ok, secret

    ok, secret = asyncio.run(scenario())
    assert "已写入长期记忆" in ok
    assert "敏感信息" in secret

    hits = mem_store.search(
        ("memories", "local_user"), query="12月销售额 促销 回升", limit=3
    )
    assert any("回升 5%" in i.value["text"] for i in hits)
    # revision 类写入后该会话的 pending 关单
    assert mem_store.list_pending_revisions() == []


def test_memory_write_rejects_unknown_kind(mem_store):
    result = asyncio.run(
        memory_tools.memory_write.ainvoke({"kind": "hack", "content": "x"})
    )
    assert "kind 必须是" in result


# ---------------------------------------------------------------------------
# P0-3 验收链路：修订 → 入库 → 先验注入生效（会话删除不影响独立记忆库）
# ---------------------------------------------------------------------------


def test_revision_injection_acceptance_flow(mem_store):
    """1) pending 召回注入；2) memory_write 后转记忆召回；3) 指令句式被包裹"""
    mem_store.add_pending_revision(
        session_id="s1",
        filename="销售分析报告.md",
        topic="销售分析",
        diff_summary="- 12月销售额结论改为因促销回升 5%",
    )

    # 下次 run 启动：先验含修订摘要与 memory_write 引导
    injection = build_memory_injection("做一份 2025 年销售分析报告")
    assert "<user_revision_prior>" in injection
    assert "回升 5%" in injection
    assert "memory_write" in injection

    # 模型发起 memory_write（本应被审批档拦截——APPROVAL_MODE_PRESETS 覆盖）
    from app.agent.main_agent import APPROVAL_MODE_PRESETS

    assert APPROVAL_MODE_PRESETS["standard"].get("memory_write") is True
    assert APPROVAL_MODE_PRESETS["strict"].get("memory_write") is True
    asyncio.run(
        memory_tools.memory_write.ainvoke(
            {
                "kind": "revision",
                "content": "12月销售额结论改为因促销回升 5%（用户修订）",
                "source_session_id": "s1",
            }
        )
    )

    # 入库后 pending 关单；同主题新会话不再有修订引导，但记忆召回生效
    assert mem_store.list_pending_revisions() == []
    injection_after = build_memory_injection("做一份 2025 年销售分析报告")
    assert "回升 5%" in injection_after
    assert "<user_revision_prior>" in injection_after


def test_no_memory_no_injection(mem_store):
    assert build_memory_injection("全新任务") == ""
