"""
SQLite 数据库查询工具模块

封装数据库查询助手使用的三个 LangChain 工具：
list_sql_tables 用于发现真实表名，get_table_data 用于预览字段和样例数据，
execute_sql_query 用于在确认结构后执行自定义查询。

SQLite 是单文件数据库，无需安装服务或 Docker：首次连接时若库文件不存在，
会自动执行 app/data/init_sqlite.sql 完成建表和教学数据导入。

三个工具统一经过 call_guard 防护层：本地 SQLite 不需要重试，但保留
tool_start/tool_end 埋点（含耗时），并对 get_table_data / execute_sql_query
启用调用预算与重复检测，避免模型反复执行完全相同的查询。
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool

from app.api import source_registry
from app.tools.call_guard import ExternalCallError, guarded_call

load_dotenv()

# 本文件位于 app/tools/，parents[2] 即项目根目录；默认库文件和建库脚本收敛在 app/data
_project_root = Path(__file__).resolve().parents[2]
_data_dir = _project_root / "app" / "data"
default_db_path = _data_dir / "deepsearch.db"
init_sql_path = _data_dir / "init_sqlite.sql"

# 优先使用环境变量指定的库文件位置；相对路径按启动目录解析后转为绝对路径
DB_PATH = Path(os.getenv("SQLITE_DB_PATH") or default_db_path).resolve()

# 本地查询很快，15 秒超时只用于兜底异常场景（如锁等待）
_DB_TIMEOUT_S = 15.0


def get_connection() -> sqlite3.Connection:
    """
    获取 SQLite 数据库连接

    所有数据库工具都通过此函数拿到连接，保证连接参数一致：
    1. 库文件不存在时，先执行 init_sqlite.sql 自动建库（仅需一次）
    2. 自动提交模式，与原 MySQL 版 autocommit=True 行为一致
    3. 逐连接开启外键约束和忙等待，让 ON DELETE CASCADE 生效并避免并发锁冲突
    :return: 已完成配置的 sqlite3.Connection
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH, autocommit=True)
        # WAL 让读写互不阻塞；该设置持久保存在库文件中，建库时设置一次即可
        conn.execute("PRAGMA journal_mode = WAL")
        with open(init_sql_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.close()

    conn = sqlite3.connect(DB_PATH, autocommit=True)
    # SQLite 默认关闭外键约束，显式开启后建表语句中的 ON DELETE CASCADE 才生效
    conn.execute("PRAGMA foreign_keys = ON")
    # 多会话并发写入时先等待持锁者释放，而不是立刻抛出 database is locked
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_readonly_connection() -> sqlite3.Connection:
    """
    获取只读数据库连接，供 execute_sql_query 使用

    mode=ro 的 URI 连接在 SQLite 层面禁止任何写操作，即使 SQL 校验被绕过
    也无法落盘变更；配合 query_only 双重保险。
    :return: 只读 sqlite3.Connection
    """
    # 首次运行库文件可能尚不存在，先走一次 get_connection 完成自动建库
    if not DB_PATH.exists():
        get_connection().close()

    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _rows_to_csv(columns: list[str], rows: list[tuple]) -> str:
    """把查询结果的列名和数据行拼成模型容易阅读的 CSV 文本"""
    results = [",".join(map(str, row)) for row in rows]
    header_str = ",".join(columns)
    data_str = "\n".join(results)
    return f"{header_str}\n{data_str}"


def _list_sql_tables_impl() -> str:
    # sqlite_master 是 SQLite 的数据字典表；sqlite_ 前缀的是内部表，需要排除
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )

        tables = cursor.fetchall()
        if not tables:
            return "没有可用的表"

        # 取每个元组的第一个元素，拼成模型容易阅读的表名列表
        table_names = [table[0] for table in tables]
        return f"可用的表有：{', '.join(table_names)}"
    finally:
        conn.close()


def _get_table_data_impl(table_name) -> str:
    # 表名先对 sqlite_master 白名单校验：它来自模型输出，直接拼接存在注入风险；
    # 校验用参数化查询，命中后再以双引号包裹标识符执行
    conn = get_readonly_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (str(table_name),),
        )
        if cursor.fetchone() is None:
            return (
                f"数据表 {table_name} 不存在。请先调用 list_sql_tables 获取真实表名，"
                "再用 get_table_data 预览。"
            )

        sql = f'SELECT * FROM "{table_name}" LIMIT 100'
        cursor.execute(sql)

        # cursor.description 保存查询结果的列元信息，没有结果集时可能为 None
        description = cursor.description
        if not description:
            return f"数据表 {table_name} 暂无数据。"

        # 只取每个列信息元组的第一个元素，也就是列名
        columns = [desc[0] for desc in description]
        rows = cursor.fetchall()
        return _rows_to_csv(columns, rows)
    finally:
        conn.close()


def _execute_sql_query_impl(query) -> str:
    conn = get_readonly_connection()
    try:
        cursor = conn.cursor()
        # 语句校验 + 只读连接双重防线，避免提示词注入诱导出 DROP/UPDATE 等破坏性 SQL
        normalized_query = query.lstrip().lstrip("(; \t").upper()
        if not normalized_query.startswith("SELECT") and not normalized_query.startswith("WITH"):
            return (
                "查询出现异常：仅允许只读查询（SELECT / WITH 开头），"
                f"拒绝执行 SQL：{query}"
            )
        cursor.execute(query)
        # 登记实际执行的 SQL，作为报告参考来源中"数据库查询记录"的依据
        source_registry.register_sql(query)

        # 非查询类 SQL 没有结果集描述，这里统一返回提示，避免工具调用直接抛错给模型
        description = cursor.description
        if not description:
            return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"
        columns = [desc[0] for desc in description]
        rows = cursor.fetchall()
        return _rows_to_csv(columns, rows)
    finally:
        conn.close()


@tool
async def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    :return: 有表：可用的表有：表1,表2,表3...
             没有表：没有可用的表
             出现异常：查询出现异常：异常信息
    """
    try:
        return await guarded_call(
            "list_sql_tables",
            {},
            _list_sql_tables_impl,
            display_name="数据库表名查询工具：list_sql_tables",
            timeout_s=_DB_TIMEOUT_S,
            budgeted=False,
        )
    except (ExternalCallError, sqlite3.Error) as e:
        return f"查询出现异常：{e}"


@tool
async def get_table_data(table_name) -> str:
    """
    查询指定表的前 100 行数据

    当前工具调用之前，应先调用 list_sql_tables 完成表名校验。
    此工具的作用：
    1. 完成单表样例数据查询
    2. 为多表查询提供表结构信息和数据格式参考
    :param table_name: 表名
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             4. 至多查询 100 条表数据
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
                1,张三,18\n -> 至多查询 100 条
    """
    try:
        return await guarded_call(
            "get_table_data",
            {"table_name": table_name},
            lambda: _get_table_data_impl(table_name),
            display_name="数据库表数据查询工具：get_table_data",
            timeout_s=_DB_TIMEOUT_S,
            budgeted=True,
        )
    except (ExternalCallError, sqlite3.Error) as e:
        return f"查询出现异常：{e}"


@tool
async def execute_sql_query(query) -> str:
    """
    执行自定义 SQL 查询

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    适合多表关联、筛选、聚合、排序等复杂查询。
    :param query: 要执行的自定义 SQL 语句
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
    """
    try:
        return await guarded_call(
            "execute_sql_query",
            {"query": query},
            lambda: _execute_sql_query_impl(query),
            display_name="数据库表数据查询工具：execute_sql_query",
            timeout_s=_DB_TIMEOUT_S,
            budgeted=True,
        )
    except (ExternalCallError, sqlite3.Error) as e:
        return f"查询出现异常：{e}"


if __name__ == "__main__":
    import asyncio

    # 本地调试入口：直接运行本文件可验证自动建库和查询链路是否可用
    print(asyncio.run(list_sql_tables.ainvoke({})))
    print(
        asyncio.run(
            execute_sql_query.ainvoke(
                {
                    "query": "SELECT dgs.generic_name, srd.region, srd.total_amount "
                    "FROM drugs dgs JOIN sales_records srd "
                    "ON dgs.drug_id = srd.drug_id LIMIT 5"
                }
            )
        )
    )
