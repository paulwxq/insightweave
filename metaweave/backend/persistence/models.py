"""平台库表模型：数据源、工作区、两类作业与运行、已发布内容。

doc 6.1 §1–§9、§13–§18 的十五张目标表与 §12 视图对应的 ORM 定义；
平台表由 `scripts/rebuild_platform_schema.py` 按 `db/rebuild_platform_schema.sql`
显式一次性重建（先统一改名旧表，再创建新对象），应用启动不建表、不改表。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    ForeignKey,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 短 ID 主键由数据库序列默认值生成（`'db_src_' || nextval(...)` 等），
# 插入时省略该列，ORM 通过 INSERT .. RETURNING 取回生成值。
_SEQUENCE_DEFAULTS = {
    "db_source_info": "'db_src_' || nextval('public.db_source_info_id_seq')",
    "workspace_info": "'ws_' || nextval('public.workspace_info_id_seq')",
    "collection_job_info": (
        "'coll_job_' || nextval('public.collection_job_info_id_seq')"
    ),
    "publish_job_info": "'pub_job_' || nextval('public.publish_job_info_id_seq')",
}


class Base(DeclarativeBase):
    pass


class DbSourceInfo(Base):
    """数据库数据源登记（doc 6.1 §13）：`source_id` 为 text 短 ID。"""

    __tablename__ = "db_source_info"
    __table_args__ = (
        CheckConstraint(
            "source_id ~ '^db_src_[1-9][0-9]*$'", name="db_source_info_id_format"
        ),
        CheckConstraint(
            "length(btrim(display_name)) > 0", name="db_source_info_name_nonblank"
        ),
        CheckConstraint("db_type = 'postgresql'", name="db_source_info_type_check"),
        CheckConstraint("length(btrim(host)) > 0", name="db_source_info_host_nonblank"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="db_source_info_port_check"),
        CheckConstraint(
            "length(btrim(db_name)) > 0", name="db_source_info_db_name_nonblank"
        ),
        CheckConstraint(
            "length(btrim(username)) > 0", name="db_source_info_username_nonblank"
        ),
        CheckConstraint(
            "sslmode IS NULL OR sslmode IN ("
            "'disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'"
            ")",
            name="db_source_info_sslmode_check",
        ),
        CheckConstraint(
            "cardinality(selected_schemas) > 0 "
            "AND array_position(selected_schemas, NULL) IS NULL",
            name="db_source_info_schemas_nonempty",
        ),
        CheckConstraint(
            "deletion_state IN ('active', 'deleting', 'delete_failed', 'deleted')",
            name="db_source_info_deletion_state_check",
        ),
        CheckConstraint(
            "(deleted_at IS NULL AND deletion_state IN "
            "('active', 'deleting', 'delete_failed')) "
            "OR (deleted_at IS NOT NULL AND deletion_state = 'deleted')",
            name="db_source_info_deletion_state_time_check",
        ),
        CheckConstraint(
            "(deleted_at IS NULL AND host IS NOT NULL AND port IS NOT NULL "
            "AND db_name IS NOT NULL AND username IS NOT NULL "
            "AND password_ciphertext IS NOT NULL AND selected_schemas IS NOT NULL) "
            "OR (deleted_at IS NOT NULL AND host IS NULL AND port IS NULL "
            "AND db_name IS NULL AND username IS NULL "
            "AND password_ciphertext IS NULL AND selected_schemas IS NULL "
            "AND sslmode IS NULL AND db_version IS NULL AND description IS NULL "
            "AND is_active = false AND deletion_state = 'deleted')",
            name="db_source_info_connection_presence_check",
        ),
        Index(
            "db_source_info_connection_idx",
            "host",
            "port",
            "db_name",
            "username",
        ),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        server_default=text(_SEQUENCE_DEFAULTS["db_source_info"]),
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    db_type: Mapped[str] = mapped_column(Text, nullable=False)
    # doc 8.1 §3.5：连接字段仅对有效源强制非空（条件约束），
    # 删除完成后按字段清单置 NULL，保留最小登记。
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    db_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    sslmode: Mapped[str | None] = mapped_column(Text, nullable=True)
    db_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_schemas: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )


class WorkspaceInfo(Base):
    """工作区（doc 6.1 §1）：文件产物的隔离与共享边界，归属一个数据源。"""

    __tablename__ = "workspace_info"
    __table_args__ = (
        CheckConstraint(
            "workspace_id ~ '^ws_[1-9][0-9]*$'", name="workspace_info_id_format"
        ),
        CheckConstraint(
            "length(btrim(source_id)) > 0", name="workspace_info_source_nonblank"
        ),
        CheckConstraint(
            "length(btrim(workspace_name)) > 0", name="workspace_info_name_nonblank"
        ),
        CheckConstraint(
            "deletion_state IN ('active', 'deleting', 'delete_failed', 'deleted')",
            name="workspace_info_deletion_state_check",
        ),
        CheckConstraint(
            "(deleted_at IS NULL AND deletion_state IN "
            "('active', 'deleting', 'delete_failed')) "
            "OR (deleted_at IS NOT NULL AND deletion_state = 'deleted')",
            name="workspace_info_deletion_state_time_check",
        ),
        Index(
            "workspace_info_active_name_idx",
            "source_id",
            "workspace_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "public"},
    )

    workspace_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        server_default=text(_SEQUENCE_DEFAULTS["workspace_info"]),
    )
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_name: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deletion_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )


class CollectionJobInfo(Base):
    """采集作业基本信息（doc 6.1 §2）：执行配置保存在 revision 表。"""

    __tablename__ = "collection_job_info"
    __table_args__ = (
        CheckConstraint(
            "collection_job_id ~ '^coll_job_[1-9][0-9]*$'",
            name="collection_job_info_id_format",
        ),
        CheckConstraint(
            "length(btrim(job_name)) > 0", name="collection_job_info_name_nonblank"
        ),
        Index(
            "collection_job_info_active_name_idx",
            "workspace_id",
            "job_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "public"},
    )

    collection_job_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        server_default=text(_SEQUENCE_DEFAULTS["collection_job_info"]),
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("public.workspace_info.workspace_id"), nullable=False
    )
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


COLLECTION_STEP_WHITELIST = (
    "db",
    "ddl",
    "json",
    "md",
    "domain",
    "dim",
    "rel",
    "sql",
)

PUBLISH_STEP_WHITELIST = (
    "db",
    "ddl",
    "json",
    "domain",
    "table_schema",
    "dim_value",
    "rel",
    "sql",
)


class CollectionJobRevision(Base):
    """采集作业不可变配置版本（doc 6.1 §3）：联合主键 (job, revision_no)。"""

    __tablename__ = "collection_job_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="collection_job_revision_no_positive"),
        CheckConstraint(
            "cardinality(steps) > 0", name="collection_job_revision_steps_nonempty"
        ),
        CheckConstraint(
            "array_position(steps, NULL) IS NULL",
            name="collection_job_revision_steps_no_null",
        ),
        CheckConstraint(
            "steps <@ ARRAY['db', 'ddl', 'json', 'md', "
            "'domain', 'dim', 'rel', 'sql']::text[]",
            name="collection_job_revision_steps_allowed",
        ),
        CheckConstraint(
            "step_config IS NULL OR jsonb_typeof(step_config) = 'object'",
            name="collection_job_revision_config_object",
        ),
        {"schema": "public"},
    )

    collection_job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("public.collection_job_info.collection_job_id"),
        primary_key=True,
    )
    revision_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    steps: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    step_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class CollectionRunInfo(Base):
    """一次采集执行（doc 6.1 §4）：运行 UUID + 复合外键到配置版本。"""

    __tablename__ = "collection_run_info"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_job_id", "revision_no"],
            [
                "public.collection_job_revision.collection_job_id",
                "public.collection_job_revision.revision_no",
            ],
            name="collection_run_info_revision_fk",
        ),
        CheckConstraint(
            "trigger_type IN ('manual', 'scheduled')",
            name="collection_run_info_trigger_type_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'partial_success', 'failed')",
            name="collection_run_info_status_check",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="collection_run_info_time_order_check",
        ),
        Index(
            "collection_run_info_job_created_idx",
            "collection_job_id",
            text("created_at DESC"),
        ),
        {"schema": "public"},
    )

    collection_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True
    )
    collection_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    run_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CollectionRunStep(Base):
    """一次采集运行中每个已选步骤的记录（doc 6.1 §5）。"""

    __tablename__ = "collection_run_step"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection_run_id"],
            ["public.collection_run_info.collection_run_id"],
        ),
        CheckConstraint(
            "step_name IN ('db', 'ddl', 'json', 'md', "
            "'domain', 'dim', 'rel', 'sql')",
            name="collection_run_step_name_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'blocked')",
            name="collection_run_step_status_check",
        ),
        CheckConstraint(
            "step_config IS NULL OR jsonb_typeof(step_config) = 'object'",
            name="collection_run_step_config_object_check",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="collection_run_step_time_order_check",
        ),
        {"schema": "public"},
    )

    collection_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True
    )
    step_name: Mapped[str] = mapped_column(Text, primary_key=True)
    step_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PublishJobInfo(Base):
    """发布作业基本信息（doc 6.1 §6）。"""

    __tablename__ = "publish_job_info"
    __table_args__ = (
        CheckConstraint(
            "publish_job_id ~ '^pub_job_[1-9][0-9]*$'",
            name="publish_job_info_id_format",
        ),
        CheckConstraint(
            "length(btrim(job_name)) > 0", name="publish_job_info_name_nonblank"
        ),
        Index(
            "publish_job_info_active_name_idx",
            "workspace_id",
            "job_name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        {"schema": "public"},
    )

    publish_job_id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        server_default=text(_SEQUENCE_DEFAULTS["publish_job_info"]),
    )
    workspace_id: Mapped[str] = mapped_column(
        Text, ForeignKey("public.workspace_info.workspace_id"), nullable=False
    )
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PublishJobRevision(Base):
    """发布作业不可变配置版本（doc 6.1 §7）。"""

    __tablename__ = "publish_job_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="publish_job_revision_no_positive"),
        CheckConstraint(
            "cardinality(steps) > 0", name="publish_job_revision_steps_nonempty"
        ),
        CheckConstraint(
            "array_position(steps, NULL) IS NULL",
            name="publish_job_revision_steps_no_null",
        ),
        CheckConstraint(
            "steps <@ ARRAY['db', 'ddl', 'json', 'domain', "
            "'table_schema', 'dim_value', 'rel', 'sql']::text[]",
            name="publish_job_revision_steps_allowed",
        ),
        CheckConstraint(
            "step_config IS NULL OR jsonb_typeof(step_config) = 'object'",
            name="publish_job_revision_config_object",
        ),
        {"schema": "public"},
    )

    publish_job_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("public.publish_job_info.publish_job_id"),
        primary_key=True,
    )
    revision_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    steps: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    step_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PublishRunInfo(Base):
    """一次发布执行（doc 6.1 §8）。"""

    __tablename__ = "publish_run_info"
    __table_args__ = (
        ForeignKeyConstraint(
            ["publish_job_id", "revision_no"],
            [
                "public.publish_job_revision.publish_job_id",
                "public.publish_job_revision.revision_no",
            ],
            name="publish_run_info_revision_fk",
        ),
        CheckConstraint(
            "trigger_type IN ('manual', 'scheduled')",
            name="publish_run_info_trigger_type_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'partial_success', 'failed')",
            name="publish_run_info_status_check",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="publish_run_info_time_order_check",
        ),
        Index(
            "publish_run_info_job_created_idx",
            "publish_job_id",
            text("created_at DESC"),
        ),
        {"schema": "public"},
    )

    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    publish_job_id: Mapped[str] = mapped_column(Text, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    run_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PublishRunStep(Base):
    """一次发布运行中每个已选步骤的记录（doc 6.1 §9）。"""

    __tablename__ = "publish_run_step"
    __table_args__ = (
        ForeignKeyConstraint(
            ["publish_run_id"],
            ["public.publish_run_info.publish_run_id"],
        ),
        CheckConstraint(
            "step_name IN ('db', 'ddl', 'json', 'domain', "
            "'table_schema', 'dim_value', 'rel', 'sql')",
            name="publish_run_step_name_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'success', 'failed', 'blocked')",
            name="publish_run_step_status_check",
        ),
        CheckConstraint(
            "step_config IS NULL OR jsonb_typeof(step_config) = 'object'",
            name="publish_run_step_config_object_check",
        ),
        CheckConstraint(
            "started_at IS NULL OR finished_at IS NULL OR finished_at >= started_at",
            name="publish_run_step_time_order_check",
        ),
        {"schema": "public"},
    )

    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    step_name: Mapped[str] = mapped_column(Text, primary_key=True)
    step_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    target_manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DbProfile(Base):
    """每源一行的当前数据库画像（doc 6.1 §14）；发布来源为 publish_run_id。"""

    __tablename__ = "db_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["publish_run_id"],
            ["public.publish_run_info.publish_run_id"],
            name="db_profile_publish_run_fk",
        ),
        CheckConstraint("port BETWEEN 1 AND 65535", name="db_profile_port_check"),
        CheckConstraint("size_bytes >= 0", name="db_profile_size_check"),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    db_type: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    server_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    server_version_full: Mapped[str | None] = mapped_column(Text, nullable=True)
    os_platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size_scope: Mapped[str] = mapped_column(Text, nullable=False)
    record_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    selected_schemas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    schemas: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    ordinary_table_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partitioned_table_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    partition_child_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    view_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    materialized_view_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_count: Mapped[int] = mapped_column(BigInteger, nullable=False)


class TableDefinition(Base):
    """每源、每对象、每定义类型一行的当前定义文本（doc 6.1 §15）。"""

    __tablename__ = "table_definition"
    __table_args__ = (
        CheckConstraint(
            "define_type IN ('ddl', 'md')", name="table_definition_type_ck"
        ),
        CheckConstraint(
            "object_type IN ('table', 'view', 'materialized_view')",
            name="table_definition_object_type_ck",
        ),
        CheckConstraint(
            "length(btrim(schema_name)) > 0 AND length(btrim(object_name)) > 0 "
            "AND length(btrim(db_name)) > 0 AND length(btrim(define_text)) > 0",
            name="table_definition_names_ck",
        ),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    define_type: Mapped[str] = mapped_column(Text, primary_key=True)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    define_text: Mapped[str] = mapped_column(Text, nullable=False)
    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class TableProfile(Base):
    """每源、每对象一行的当前 JSON 画像（doc 6.1 §16）。"""

    __tablename__ = "table_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["publish_run_id"],
            ["public.publish_run_info.publish_run_id"],
            name="table_profile_publish_run_fk",
        ),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    metadata_version: Mapped[str] = mapped_column(Text, nullable=False)
    generated_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    object_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_comment_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    has_table_profile: Mapped[bool] = mapped_column(Boolean, nullable=False)
    table_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_confidence: Mapped[float | None] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    classification_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_basis: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    rule_table_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_confidence: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    rule_inference_basis: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    primary_key_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_key_columns: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), nullable=True
    )
    foreign_key_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    index_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    constraint_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    foreign_keys: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    unique_constraints: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    indexes: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    unique_column_sets: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    json_sample_records: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TableColumnProfile(Base):
    """每源、每对象、每字段一行的当前字段画像（doc 6.1 §17）。"""

    __tablename__ = "table_column_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "schema_name", "object_name"],
            [
                "public.table_profile.source_id",
                "public.table_profile.schema_name",
                "public.table_profile.object_name",
            ],
            ondelete="CASCADE",
            name="table_column_profile_object_fk",
        ),
        ForeignKeyConstraint(
            ["publish_run_id"],
            ["public.publish_run_info.publish_run_id"],
            name="table_column_profile_publish_run_fk",
        ),
        Index(
            "table_column_profile_object_order_idx",
            "source_id",
            "schema_name",
            "object_name",
            "ordinal_position",
            unique=True,
        ),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    column_name: Mapped[str] = mapped_column(Text, primary_key=True)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    column_default: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_confidence: Mapped[float | None] = mapped_column(
        Numeric(8, 6), nullable=True
    )
    semantic_basis: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    null_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    null_rate: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    unique_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uniqueness: Mapped[float | None] = mapped_column(Numeric(8, 6), nullable=True)
    sample_min: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_max: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_distribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class TableDomain(Base):
    """每源表与业务领域的成员关系（doc 6.1 §18）。"""

    __tablename__ = "table_domain"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(schema_name)) > 0 AND length(btrim(table_name)) > 0 "
            "AND length(btrim(domain_name)) > 0 AND length(btrim(domain_desc)) > 0 "
            "AND length(btrim(db_name)) > 0",
            name="table_domain_names_not_blank",
        ),
        {"schema": "public"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    domain_name: Mapped[str] = mapped_column(Text, primary_key=True)
    domain_desc: Mapped[str] = mapped_column(Text, nullable=False)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    publish_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class ResourceDeletionTask(Base):
    """数据源/工作区删除与源级知识清理任务（doc 8.1 §7）。

    同源互斥由部分唯一索引保证（pending/running 每源至多一条）；
    queued_at 由数据库默认值生成并随每次重试显式刷新。
    """

    __tablename__ = "resource_deletion_task"
    __table_args__ = (
        CheckConstraint(
            "action IN ('delete_source', 'delete_workspace', 'clear_source_knowledge')",
            name="resource_deletion_task_action_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'failed', 'success')",
            name="resource_deletion_task_status_check",
        ),
        CheckConstraint(
            "action != 'delete_workspace' OR workspace_id IS NOT NULL",
            name="resource_deletion_task_workspace_required_check",
        ),
        Index(
            "resource_deletion_task_one_active_source_idx",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        {"schema": "public"},
    )

    task_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    step_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
