"""
跨会话长期记忆：自研 SQLite 向量 Store（docs/upgrade-plan.md P0-3）

LangGraph 官方 Store 只有 InMemoryStore / PostgresStore，无 SQLite/向量
现成件，这里自研 BaseStore 子类：底层 SQLite 持久化 + sqlite-vec 向量
召回（Windows 预编译 wheel 可用性已于 W9 首日验证通过，v0.1.9）。

嵌入向量说明：为保持零外部依赖（不调远程 embedding API），本模块内置
确定性的本地哈希词袋嵌入（中文按单字/双字切分，英文按词，256 维），
足以支撑"同类主题召回"的语义粒度；切换真实 embedding 模型只需替换
embed_text 的实现，存储与召回链路不变。

存储文件为独立的 data/memory.db：与 checkpoints.db 分离，会话删除
（按 thread 清理 checkpoint）不会连带删除记忆——修订回流链路的验收
用例（删除会话后新会话仍召回先验）依赖这一隔离。

同一库内的 pending_revisions 表承载"报告修订暂存"（修订审批时序
案 A）：PATCH /api/reports 只写报告文件 + 修订暂存为 pending，下次
run 启动时按主题召回并引导模型经 memory_write 工具走审批后入库，
pending 表不挂 thread 生命周期。
"""

import hashlib
import json
import logging
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    PutOp,
    SearchItem,
    SearchOp,
)

logger = logging.getLogger(__name__)

# 嵌入维度：256 维哈希词袋，召回粒度是主题级而非语义级
_EMBED_DIM = 256

# 记忆元数据里的来源会话列（归属校验即 session 归属——单用户部署下
# user_id 仅作 namespace 键，不采信客户端自报身份，见 docs/memory.md）
_SOURCE_SESSION_COLUMN = "source_session_id"

_NS_SEPARATOR = "\x1f"


def _ns_to_text(namespace: tuple[str, ...]) -> str:
    return _NS_SEPARATOR.join(namespace)


def _text_to_ns(text: str) -> tuple[str, ...]:
    return tuple(text.split(_NS_SEPARATOR))


def _tokenize(text: str) -> list[str]:
    """中文按单字 + 双字 bigram 切分，英文/数字按词切分"""
    tokens: list[str] = []
    current_word: list[str] = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if current_word:
                tokens.append("".join(current_word).lower())
                current_word = []
            tokens.append(ch)
        elif ch.isalnum():
            current_word.append(ch)
        else:
            if current_word:
                tokens.append("".join(current_word).lower())
                current_word = []
    if current_word:
        tokens.append("".join(current_word).lower())
    # 双字 bigram 提高中文短语的区分度
    tokens.extend(
        text[i : i + 2] for i in range(len(text) - 1) if "\u4e00" <= text[i] <= "\u9fff"
    )
    return tokens


def embed_text(text: str, dim: int = _EMBED_DIM) -> bytes:
    """
    确定性本地嵌入：token 哈希进 dim 维桶后 L2 归一化，返回 float32 小端 BLOB

    sqlite-vec 的 vec0 表直接消费该 BLOB；同主题文本得到相近向量，
    无需外部 embedding 服务
    """
    vec = [0.0] * dim
    for token in _tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "little") % dim
        vec[index] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return struct.pack(f"<{dim}f", *vec)


class SqliteVecStore(BaseStore):
    """SQLite + sqlite-vec 的自研 Store：向量召回、namespace 隔离、修订暂存"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
        except ImportError as exc:  # pragma: no cover - 依赖已声明，防御分支
            raise RuntimeError("sqlite-vec 未安装：pip install sqlite-vec") from exc
        self._migrate()

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                embedding BLOB NOT NULL,
                source_session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (namespace, key)
            )
            """
        )
        # vec0 虚拟表：rowid 与 memories.rowid 对齐，KNN 检索后回查主表
        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                embedding float[{_EMBED_DIM}]
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                topic TEXT NOT NULL,
                diff_summary TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # BaseStore 协议：batch 是唯一必须实现的同步原语，put/get/search 等
    # 便捷方法由基类基于 batch 组合
    # ------------------------------------------------------------------

    def batch(self, ops: Sequence[Any]) -> list[Any]:
        results: list[Any] = []
        for op in ops:
            if isinstance(op, PutOp):
                results.append(self._handle_put(op))
            elif isinstance(op, GetOp):
                results.append(self._handle_get(op))
            elif isinstance(op, SearchOp):
                results.append(self._handle_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._handle_list_namespaces(op))
            else:
                results.append(None)
        self._conn.commit()
        return results

    async def abatch(self, ops: Sequence[Any]) -> list[Any]:
        return self.batch(ops)

    def _handle_put(self, op: PutOp) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if op.value is None:
            # BaseStore 约定：value 为 None 表示删除该条记忆
            self._conn.execute(
                "DELETE FROM memories_vec WHERE rowid IN "
                "(SELECT rowid FROM memories WHERE namespace=? AND key=?)",
                (_ns_to_text(op.namespace), op.key),
            )
            self._conn.execute(
                "DELETE FROM memories WHERE namespace=? AND key=?",
                (_ns_to_text(op.namespace), op.key),
            )
            return None

        source_session = ""
        meta = op.value.get("meta") or {}
        if isinstance(meta, dict):
            source_session = str(meta.get("session_id") or "")
        existing = self._conn.execute(
            "SELECT created_at FROM memories WHERE namespace=? AND key=?",
            (_ns_to_text(op.namespace), op.key),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        embedding = embed_text(op.value.get("text") or json.dumps(op.value, ensure_ascii=False))
        self._conn.execute(
            """
            INSERT INTO memories(namespace, key, value, embedding, source_session_id, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(namespace, key) DO UPDATE SET
                value=excluded.value, embedding=excluded.embedding,
                source_session_id=excluded.source_session_id, updated_at=excluded.updated_at
            """,
            (
                _ns_to_text(op.namespace),
                op.key,
                json.dumps(op.value, ensure_ascii=False),
                embedding,
                source_session,
                created_at,
                now,
            ),
        )
        if existing is None:
            row = self._conn.execute(
                "SELECT rowid FROM memories WHERE namespace=? AND key=?",
                (_ns_to_text(op.namespace), op.key),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES(?,?)",
                (row["rowid"], embedding),
            )
        else:
            row = self._conn.execute(
                "SELECT rowid FROM memories WHERE namespace=? AND key=?",
                (_ns_to_text(op.namespace), op.key),
            ).fetchone()
            self._conn.execute(
                "UPDATE memories_vec SET embedding=? WHERE rowid=?",
                (embedding, row["rowid"]),
            )
        return None

    def _handle_get(self, op: GetOp) -> Optional[Item]:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE namespace=? AND key=?",
            (_ns_to_text(op.namespace), op.key),
        ).fetchone()
        if row is None:
            return None
        return Item(
            value=json.loads(row["value"]),
            key=row["key"],
            namespace=_text_to_ns(row["namespace"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _handle_search(self, op: SearchOp) -> list[SearchItem]:
        ns_text = _ns_to_text(op.namespace_prefix or ())
        limit = op.limit or 10
        # 向量候选多取一批，给 filter 过滤留余量
        candidate = max(limit * 4, 32) if op.filter else limit + op.offset
        if op.query:
            rows = self._conn.execute(
                """
                SELECT m.*, v.distance FROM memories_vec v
                JOIN memories m ON m.rowid = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
                """,
                (embed_text(op.query), candidate),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT *, 0.0 AS distance FROM memories LIMIT ?", (candidate,)
            ).fetchall()

        items: list[SearchItem] = []
        for row in rows:
            if not row["namespace"].startswith(ns_text):
                continue
            value = json.loads(row["value"])
            if op.filter and not all(
                value.get(k) == v or (value.get("meta") or {}).get(k) == v
                for k, v in op.filter.items()
            ):
                continue
            items.append(
                SearchItem(
                    namespace=_text_to_ns(row["namespace"]),
                    key=row["key"],
                    value=value,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    score=1.0 - float(row["distance"]),
                )
            )
        return items[op.offset : op.offset + limit]

    def _handle_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = self._conn.execute("SELECT DISTINCT namespace FROM memories").fetchall()
        namespaces = [_text_to_ns(row["namespace"]) for row in rows]
        if op.prefix:
            prefix_text = _NS_SEPARATOR.join(op.prefix)
            namespaces = [ns for ns in namespaces if _ns_to_ns_text_starts(ns, prefix_text)]
        if op.suffix:
            suffix_text = _NS_SEPARATOR.join(op.suffix)
            namespaces = [
                ns for ns in namespaces if _ns_to_ns_text_ends(ns, suffix_text)
            ]
        return namespaces[: op.limit]

    # ------------------------------------------------------------------
    # 修订暂存（pending）：PATCH 只写文件 + 暂存修订，下次 run 引导
    # memory_write 审批入库。独立于 thread 生命周期，会话删除不影响
    # ------------------------------------------------------------------

    def add_pending_revision(
        self, session_id: str, filename: str, topic: str, diff_summary: str
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO pending_revisions(session_id, filename, topic, diff_summary, created_at)
            VALUES(?,?,?,?,?)
            """,
            (
                session_id,
                filename,
                topic,
                diff_summary,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_pending_revisions(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM pending_revisions WHERE status='pending'
            ORDER BY id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_pending_done(self, revision_id: int) -> None:
        self._conn.execute(
            "UPDATE pending_revisions SET status='done' WHERE id=?", (revision_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 删除（归属校验即 session 归属，见模块 docstring）
    # ------------------------------------------------------------------

    def delete_by_session(self, session_id: str) -> int:
        """删除指定会话来源的记忆与暂存修订，返回删除条数"""
        rows = self._conn.execute(
            "SELECT rowid, namespace, key FROM memories WHERE source_session_id=?",
            (session_id,),
        ).fetchall()
        for row in rows:
            self._conn.execute(
                "DELETE FROM memories_vec WHERE rowid=?", (row["rowid"],)
            )
        self._conn.execute(
            "DELETE FROM memories WHERE source_session_id=?", (session_id,)
        )
        cur = self._conn.execute(
            "DELETE FROM pending_revisions WHERE session_id=?", (session_id,)
        )
        self._conn.commit()
        return len(rows) + cur.rowcount

    def close(self) -> None:
        self._conn.close()


def _ns_to_ns_text_starts(ns: tuple[str, ...], prefix_text: str) -> bool:
    return _ns_to_text(ns).startswith(prefix_text)


def _ns_to_ns_text_ends(ns: tuple[str, ...], suffix_text: str) -> bool:
    return _ns_to_text(ns).endswith(suffix_text)


_store: Optional[SqliteVecStore] = None


def get_memory_store() -> SqliteVecStore:
    """进程级单例：记忆库落在 data/memory.db（与 checkpoints.db 分离）"""
    global _store
    if _store is None:
        db_path = Path(__file__).resolve().parents[1] / "data" / "memory.db"
        _store = SqliteVecStore(db_path)
    return _store
