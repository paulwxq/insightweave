"""doc 8.1 §3.3/§3.5/§7 的表结构契约：逻辑删除列、条件约束、删除任务表。

SQL 建表脚本与 ORM 必须同步声明；本文件只做静态契约核对，不连库。
"""

from pathlib import Path

from sqlalchemy import Index

from backend.persistence.models import (
    CollectionJobInfo,
    DbSourceInfo,
    PublishJobInfo,
    ResourceDeletionTask,
    WorkspaceInfo,
)

SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "rebuild_platform_schema.sql"
)
SQL = SQL_PATH.read_text(encoding="utf-8")


def _table_block(table: str) -> str:
    """截取 CREATE TABLE ... 到下一个 ); 的建表块，断言限定在表内。"""
    start = SQL.index(f"CREATE TABLE public.{table} (")
    end = SQL.index(");", start)
    return SQL[start:end]


def _orm_index(model, name):
    return next(
        (
            item
            for item in model.__table_args__
            if isinstance(item, Index) and item.name == name
        ),
        None,
    )


def test_four_main_tables_declare_deleted_at_in_sql_and_orm():
    for table in (
        "db_source_info",
        "workspace_info",
        "collection_job_info",
        "publish_job_info",
    ):
        assert "deleted_at timestamptz," in _table_block(table), table
    assert DbSourceInfo.__table__.c.deleted_at.nullable is True
    assert WorkspaceInfo.__table__.c.deleted_at.nullable is True
    assert CollectionJobInfo.__table__.c.deleted_at.nullable is True
    assert PublishJobInfo.__table__.c.deleted_at.nullable is True


def test_deletion_state_on_source_and_workspace_with_checks():
    for table in ("db_source_info", "workspace_info"):
        block = _table_block(table)
        assert "deletion_state text NOT NULL DEFAULT 'active'" in block, table
        assert f"{table}_deletion_state_check" in SQL, table
        assert f"{table}_deletion_state_time_check" in SQL, table
    # 时间一致性 CHECK 的两个分支都必须出现（§3.3）。
    assert "deleted_at IS NULL AND deletion_state IN (" in SQL
    assert "deleted_at IS NOT NULL AND deletion_state = 'deleted'" in SQL

    assert (
        WorkspaceInfo.__table__.c.deletion_state.nullable is False
    )
    assert (
        DbSourceInfo.__table__.c.deletion_state.nullable is False
    )


def test_source_connection_fields_conditional_check():
    """§3.5：连接字段可空 + 双向条件约束（有效源必须完整、已删源必须清空）。"""
    block = _table_block("db_source_info")
    for fragment in (
        "db_source_info_connection_presence_check",
        "host IS NOT NULL",
        "host IS NULL",
        "password_ciphertext IS NULL",
        "selected_schemas IS NULL",
        "is_active = false",
    ):
        assert fragment in SQL, fragment
    # db_source_info 建表列改为可空（不再强制 NOT NULL）。
    assert "host text NOT NULL" not in block
    assert "password_ciphertext text NOT NULL" not in block
    assert "selected_schemas text[] NOT NULL" not in block
    for column in ("host", "port", "db_name", "username", "password_ciphertext"):
        assert DbSourceInfo.__table__.c[column].nullable is True, column
    assert DbSourceInfo.__table__.c.selected_schemas.nullable is True


def test_name_unique_constraints_replaced_by_partial_indexes():
    """§3.4：移除三条全量唯一约束，替换为 deleted_at IS NULL 部分唯一索引。"""
    for name in (
        "workspace_info_source_name_key",
        "collection_job_info_workspace_name_key",
        "publish_job_info_workspace_name_key",
    ):
        assert name not in SQL, name
    for idx in (
        "workspace_info_active_name_idx",
        "collection_job_info_active_name_idx",
        "publish_job_info_active_name_idx",
    ):
        assert f"CREATE UNIQUE INDEX {idx}" in SQL, idx
        assert "WHERE deleted_at IS NULL" in SQL
        model = {
            "workspace_info_active_name_idx": WorkspaceInfo,
            "collection_job_info_active_name_idx": CollectionJobInfo,
            "publish_job_info_active_name_idx": PublishJobInfo,
        }[idx]
        orm_index = _orm_index(model, idx)
        assert orm_index is not None and orm_index.unique is True
        assert orm_index.dialect_kwargs["postgresql_where"] is not None


def test_resource_deletion_task_table_and_mutex_index():
    """§7：任务表字段、queued_at 时间契约与同源 pending/running 部分唯一索引。"""
    assert "CREATE TABLE public.resource_deletion_task" in SQL
    for fragment in (
        "task_id uuid PRIMARY KEY",
        "action text NOT NULL",
        "delete_source",
        "delete_workspace",
        "clear_source_knowledge",
        "target_id text NOT NULL",
        "source_id text NOT NULL",
        "workspace_id text",
        "status text NOT NULL DEFAULT 'pending'",
        "step_results jsonb",
        "created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "queued_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "resource_deletion_task_one_active_source_idx",
        "WHERE status IN ('pending', 'running')",
    ):
        assert fragment in SQL, fragment

    table = ResourceDeletionTask.__table__
    assert table.c.task_id.primary_key
    assert table.c.action.nullable is False
    assert table.c.target_id.nullable is False
    assert table.c.source_id.nullable is False
    assert table.c.workspace_id.nullable is True
    assert table.c.status.nullable is False
    assert table.c.queued_at.nullable is False
    assert table.c.queued_at.server_default is not None
    mutex = _orm_index(ResourceDeletionTask, "resource_deletion_task_one_active_source_idx")
    assert mutex is not None and mutex.unique is True
