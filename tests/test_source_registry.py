"""
任务级来源登记表的单元测试

覆盖登记、去重、重置、无上下文降级和清单渲染
"""

import pytest

from app.api import source_registry
from app.api.context import reset_thread_context, set_thread_context


@pytest.fixture
def thread_context():
    thread_id = "src_test_session"
    token = set_thread_context(thread_id)
    source_registry.reset_task_sources(thread_id)
    yield thread_id
    reset_thread_context(token)


def test_web_sources_dedup_by_url(thread_context):
    source_registry.register_web_sources([{"title": "A", "url": "https://e.com/1"}])
    # 相同 URL 不同标题视为重复；缺 url 的条目忽略
    source_registry.register_web_sources(
        [{"title": "A-again", "url": "https://e.com/1"}, {"title": "无链接"}]
    )

    sources = source_registry.get_task_sources()
    assert len(sources["web"]) == 1
    assert sources["web"][0]["title"] == "A"


def test_doc_sources_dedup_by_doc_and_page(thread_context):
    source_registry.register_doc_source("白皮书.pdf", "3")
    source_registry.register_doc_source("白皮书.pdf", "3")
    source_registry.register_doc_source("白皮书.pdf", "5")
    source_registry.register_doc_source("", "1")  # 空文档名忽略

    sources = source_registry.get_task_sources()
    assert len(sources["docs"]) == 2


def test_sql_dedup_by_exact_text(thread_context):
    source_registry.register_sql("SELECT 1")
    source_registry.register_sql("SELECT 1")
    source_registry.register_sql("SELECT 2")

    assert source_registry.get_task_sources()["sql"] == ["SELECT 1", "SELECT 2"]


def test_reset_clears_all(thread_context):
    source_registry.register_web_sources([{"title": "A", "url": "https://e.com/1"}])
    source_registry.count_gate_rejection()
    source_registry.reset_task_sources(thread_context)

    sources = source_registry.get_task_sources()
    assert sources["web"] == []
    assert source_registry.count_gate_rejection() == 1  # 重置后重新从 1 计数


def test_no_thread_context_is_noop(monkeypatch):
    monkeypatch.setattr(source_registry, "get_thread_context", lambda: None)
    source_registry.register_web_sources([{"title": "A", "url": "https://e.com/1"}])
    source_registry.register_doc_source("d.pdf")
    source_registry.register_sql("SELECT 1")

    assert source_registry.get_task_sources() == {"web": [], "docs": [], "sql": []}


def test_manifest_renders_all_sections(thread_context):
    source_registry.register_web_sources(
        [
            {"title": "行业报告", "url": "https://e.com/r"},
            {"title": "", "url": "https://e.com/x"},
        ]
    )
    source_registry.register_doc_source("白皮书.pdf", "3")
    source_registry.register_doc_source("研报.docx", "")
    source_registry.register_sql("SELECT * FROM drugs LIMIT 5")

    manifest = source_registry.format_source_manifest()

    assert "网络来源：" in manifest
    assert "1. [行业报告](https://e.com/r)" in manifest
    assert "2. [无标题](https://e.com/x)" in manifest
    assert "RAGFlow 文档来源：" in manifest
    assert "1. 《白皮书.pdf》第3页" in manifest
    assert "2. 《研报.docx》" in manifest  # 无页码不带页码后缀
    assert "数据库查询记录：" in manifest
    assert "SELECT * FROM drugs LIMIT 5" in manifest


def test_manifest_empty_when_no_sources(thread_context):
    assert source_registry.format_source_manifest() == "（本次任务没有登记到外部来源）"
