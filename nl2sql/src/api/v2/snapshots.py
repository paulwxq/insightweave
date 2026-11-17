"""结果快照表读写（docs/3.1）。

写：Web 收尾钩子把展示证据的结果前 ``thread.snapshot_max_rows`` 行写入
``result_snapshots``；读：results API 在结果目录缺失时按
``session_id + result_ref`` 回退。执行/读取路径（ResultStore、Runner、工具）
不感知本模块。
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from src.modules.db_deepagent.services.result_store import ResultStore
from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.api.v2.snapshots")

SNAPSHOT_TABLE = "result_snapshots"


class SnapshotTableMissing(Exception):
    """快照表未建（部署态）；读取方按“无快照”处理。"""


class SnapshotStore:
    """快照表的写入与按会话读取。"""

    def __init__(self, pool: Any, *, max_rows: int = 0):
        self._pool = pool
        self._max_rows = max_rows

    # ------------------------------------------------------------ 表/读

    def table_exists(self) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT to_regclass(%s) AS reg", (SNAPSHOT_TABLE,)
            ).fetchone()
        return bool(row and row.get("reg"))

    def fetch(self, session_id: str, result_ref: str) -> Optional[dict]:
        """返回快照行（dict）；表未建抛 SnapshotTableMissing，连接失败原样抛。"""
        if not self.table_exists():
            raise SnapshotTableMissing()
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT result_ref, session_id, request_id, thread_id,
                       saved_row_count, rows_snapshot, snapshot_row_count, meta_jsonb
                FROM result_snapshots
                WHERE session_id = %s AND result_ref = %s
                """,
                (session_id, result_ref),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ 写

    def write(
        self,
        *,
        session_id: str,
        request_id: str,
        evidence: list[dict],
        results: ResultStore,
    ) -> None:
        """逐条写入本轮展示证据；单条失败 WARN 后继续，不抛出。"""
        if self._pool is None:
            return
        with self._pool.connection() as conn:
            for item in evidence or []:
                ref = item.get("result_ref") if isinstance(item, dict) else None
                if not ref:
                    continue
                try:
                    meta = results.load_meta(ref, session_id=session_id)
                    page = results.read_rows_cells(
                        ref,
                        session_id=session_id,
                        offset=0,
                        limit=self._max_rows,
                    )
                    conn.execute(
                        """
                        INSERT INTO result_snapshots (
                            result_ref, session_id, request_id, thread_id,
                            saved_row_count, rows_snapshot, snapshot_row_count,
                            meta_jsonb
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (result_ref) DO UPDATE
                        SET last_referenced_at = now()
                        """,
                        (
                            ref,
                            meta.session_id,
                            # request_id 记结果自身来源（复用/补录不更新）
                            meta.request_id,
                            meta.thread_id,
                            meta.saved_row_count,
                            Jsonb(page.rows),
                            len(page.rows),
                            Jsonb(meta.to_json()),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001（单条隔离，失败不阻断本轮）
                    logger.warning(
                        "快照写入失败 session=%s request=%s ref=%s: %s: %s",
                        session_id,
                        request_id,
                        ref,
                        type(exc).__name__,
                        exc,
                    )
