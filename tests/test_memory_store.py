"""自研 SqliteVecStore（P0-3）的存储、召回、pending 暂存与删除测试"""

import pytest

from app.memory import store as store_module


@pytest.fixture()
def mem_store(tmp_path):
    """每个用例独立临时库，测试后关闭连接"""
    s = store_module.SqliteVecStore(tmp_path / "memory.db")
    yield s
    s.close()


def test_put_get_roundtrip(mem_store):
    import asyncio

    async def scenario():
        await mem_store.aput(
            ("memories", "local_user", "preference"),
            "pref-1",
            {"text": "结论放正文最前", "meta": {"kind": "preference"}},
        )
        return await mem_store.aget(("memories", "local_user", "preference"), "pref-1")

    item = asyncio.run(scenario())
    assert item is not None
    assert item.value["text"] == "结论放正文最前"
    assert item.namespace == ("memories", "local_user", "preference")


def test_vector_search_recalls_same_topic(mem_store):
    import asyncio

    async def scenario():
        await mem_store.aput(
            ("memories", "local_user", "finding"),
            "f1",
            {"text": "奥司他韦 2025 年销售额 70 万元，11 月占全年七成", "meta": {}},
        )
        await mem_store.aput(
            ("memories", "local_user", "finding"),
            "f2",
            {"text": "库存批次 MY-251120-C 占比 63.83%，集中在天津一号库", "meta": {}},
        )

    asyncio.run(scenario())
    hits = mem_store.search(
        ("memories", "local_user"), query="奥司他韦 2025 年的销售情况", limit=2
    )
    assert len(hits) == 2
    # 同主题（奥司他韦/销售）的向量距离应小于库存主题
    scores = {h.key: h.score for h in hits}
    assert scores["f1"] > scores["f2"]


def test_namespace_isolation(mem_store):
    import asyncio

    async def scenario():
        await mem_store.aput(("memories", "user_a", "finding"), "k", {"text": "甲的记忆"})
        await mem_store.aput(("memories", "user_b", "finding"), "k", {"text": "乙的记忆"})
        hits_a = await mem_store.asearch(("memories", "user_a"), query="记忆", limit=10)
        return hits_a

    hits = asyncio.run(scenario())
    assert len(hits) == 1
    assert hits[0].value["text"] == "甲的记忆"


def test_put_none_deletes(mem_store):
    import asyncio

    async def scenario():
        ns, key = ("memories", "local_user", "revision"), "r1"
        await mem_store.aput(ns, key, {"text": "待删除"})
        assert await mem_store.aget(ns, key) is not None
        await mem_store.aput(ns, key, None)  # BaseStore 约定：None 即删除
        return await mem_store.aget(ns, key)

    assert asyncio.run(scenario()) is None


def test_pending_revision_lifecycle(mem_store):
    """PATCH 暂存 → 下次 run 召回 → memory_write 入库后关单"""
    rid = mem_store.add_pending_revision(
        session_id="s1",
        filename="报告.md",
        topic="销售分析",
        diff_summary="- 12月结论改为回升 5%",
    )
    pending = mem_store.list_pending_revisions()
    assert len(pending) == 1 and pending[0]["topic"] == "销售分析"

    mem_store.mark_pending_done(rid)
    assert mem_store.list_pending_revisions() == []


def test_delete_by_session_covers_memories_and_pending(mem_store):
    import asyncio

    async def scenario():
        await mem_store.aput(
            ("memories", "local_user", "revision"),
            "r1",
            {"text": "修订内容", "meta": {"kind": "revision", "session_id": "s1"}},
        )
        await mem_store.aput(
            ("memories", "local_user", "revision"),
            "r2",
            {"text": "别的会话的修订", "meta": {"kind": "revision", "session_id": "s2"}},
        )
        return mem_store.delete_by_session("s1")

    deleted = asyncio.run(scenario())
    assert deleted >= 1  # s1 的记忆 + pending（若有）
    remaining = mem_store.search(("memories", "local_user"), query="修订", limit=10)
    assert {i.value["text"] for i in remaining} == {"别的会话的修订"}


def test_singleton_points_at_project_data_dir():
    """记忆库独立于 checkpoints（P0-3 验收：删除会话不连带删记忆）"""
    s = store_module.get_memory_store()
    assert s.db_path.name == "memory.db"
    assert "data" in str(s.db_path)
