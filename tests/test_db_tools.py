"""
数据库表数据查询工具的单元测试

get_table_data 的表名来自模型输出，修复后先对 sqlite_master 白名单校验
（参数化查询），命中后以双引号包裹标识符执行，且统一走只读连接。
测试在临时库文件上自动建库（复用 app/data/init_sqlite.sql 教学数据）
"""

import asyncio

from app.tools import db_tools


def _invoke(table_name: str) -> str:
    """db_tools 的工具是 async 定义，统一经 asyncio 同步调用"""
    return asyncio.run(db_tools.get_table_data.ainvoke({"table_name": table_name}))


def _init_tmp_db(tmp_path, monkeypatch):
    """把 DB_PATH 指到临时文件，首次连接时自动执行建库脚本"""
    db_path = tmp_path / "test_deepsearch.db"
    monkeypatch.setattr(db_tools, "DB_PATH", db_path)
    db_tools.get_connection().close()
    return db_path


def test_get_table_data_returns_csv_for_real_table(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    result = _invoke("drugs")

    # 返回 CSV：第一行是列名，后续是数据行
    lines = result.splitlines()
    assert lines[0].startswith("drug_id,")
    assert len(lines) > 1


def test_get_table_data_rejects_missing_table_with_guidance(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    result = _invoke("no_such_table")

    assert "不存在" in result
    assert "list_sql_tables" in result


def test_get_table_data_blocks_injection_payloads(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    payloads = [
        "drugs; DROP TABLE drugs",
        'drugs" UNION SELECT sql FROM sqlite_master --',
        "drugs --",
    ]
    for payload in payloads:
        result = _invoke(payload)
        # 白名单未命中统一返回引导提示，而不是把 payload 拼进 SQL 执行
        assert "不存在" in result, f"payload 未被拦截: {payload}"

    # 注入尝试后库仍然完好：真实表照常可查，DROP 未生效
    result = _invoke("drugs")
    assert result.startswith("drug_id,")


def test_get_table_data_accepts_quoted_but_existing_table(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    # 白名单命中后走双引号包裹的执行路径：普通表名照常返回数据
    result = _invoke("sales_records")
    assert result.startswith("sale_id,")


def _invoke_sql(query: str) -> str:
    """execute_sql_query 同为 async 定义，统一经 asyncio 同步调用"""
    return asyncio.run(db_tools.execute_sql_query.ainvoke({"query": query}))


def test_execute_sql_query_accepts_leading_comments(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    # 模型习惯在 SQL 前写注释说明，注释行剥离后是合法 SELECT，应正常返回数据
    result = _invoke_sql("-- 布洛芬库存总量查询\nSELECT COUNT(*) AS n FROM drugs")

    # 单列 CSV：首行为列名，次行为计数（drugs 表 50 行）
    assert result == "n\n50"

    # 多行注释 + 空行交错，剥离后仍是合法 SELECT，同样放行
    result = _invoke_sql("-- 第一条注释\n\n-- 第二条注释\nSELECT COUNT(*) AS n FROM drugs")
    assert result == "n\n50"


def test_execute_sql_query_still_rejects_writes_after_comments(tmp_path, monkeypatch):
    _init_tmp_db(tmp_path, monkeypatch)

    # 注释剥离不得放松只读防线：注释后跟写语句仍被拒
    result = _invoke_sql("-- 看似注释\nDROP TABLE drugs")

    assert "仅允许只读查询" in result

    # 纯注释（剥离后为空）不构成合法 SELECT，同样拒绝
    result = _invoke_sql("-- 只有注释没有语句")

    assert "仅允许只读查询" in result


def test_strip_leading_comments_normalization():
    # 校验归一化辅助函数本身：空串、无注释、连续注释的行为
    assert db_tools._strip_leading_comments("  \n -- a\n-- b\nSELECT 1") == "SELECT 1"
    assert db_tools._strip_leading_comments("SELECT 1") == "SELECT 1"
    assert db_tools._strip_leading_comments("") == ""
    assert db_tools._strip_leading_comments("-- 只有一行") == ""
