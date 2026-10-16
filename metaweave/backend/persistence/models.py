"""平台库表模型：数据源、作业步骤状态、已发布数据库画像。

平台表由管理员按设计文档的 SQL 手工创建（doc 3 §2、doc 9 §2–§3）；
应用启动不建表、不改表。
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
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DbSourceInfo(Base):
    __tablename__ = "db_source_info"
    __table_args__ = (
        CheckConstraint("db_type = 'postgresql'"),
        CheckConstraint("port BETWEEN 1 AND 65535"),
        CheckConstraint(
            "cardinality(selected_schemas) > 0 "
            "AND array_position(selected_schemas, NULL) IS NULL"
        ),
        {"schema": "public"},
    )

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    db_type: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    sslmode: Mapped[str | None] = mapped_column(Text, nullable=True)
    db_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_schemas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
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


class JobInfo(Base):
    """doc 9 §2：一个 job_id 下一个步骤的最近一次执行状态。

    主键为 (job_id, step_name)；不定义外键，source_id 归属由应用校验。
    """

    __tablename__ = "job_info"
    __table_args__ = (
        CheckConstraint("length(btrim(step_name)) > 0"),
        CheckConstraint("last_status IN ('running', 'success', 'failed')"),
        {"schema": "public"},
    )

    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    step_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    last_status: Mapped[str] = mapped_column(Text, nullable=False)
    run_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    db_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DbProfile(Base):
    """每个数据源的当前数据库画像（doc 16 §3.1：主键 source_id，每源一行）。

    publish_id 是这一行最后一次成功写入来自哪次发布的普通字段，
    不是内容历史主键；重复发布整行替换，不保留内容版本。
    """

    __tablename__ = "db_profile"
    __table_args__ = (
        CheckConstraint("port BETWEEN 1 AND 65535"),
        CheckConstraint("size_bytes >= 0"),
        {"schema": "public"},
    )

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
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
    """一个数据源中一个数据库对象的一种当前定义文本（doc 16 §3.2）。

    主键为 (source_id, schema_name, object_name, define_type)；DDL 与未来
    的 MD 各占一行，互不覆盖。本阶段只写 define_type='ddl'。job_id 与
    publish_id 是最后成功发布这一行的来源，不是内容历史主键。
    """

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

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    define_type: Mapped[str] = mapped_column(Text, primary_key=True)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    define_text: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PublishInfo(Base):
    """doc 12 §2：一次发布尝试中某个步骤的结果。

    主键为 (publish_id, step_name)。不定义外键。
    is_current_publish 由部分唯一索引保证每源每步骤至多一行。
    """

    __tablename__ = "publish_info"
    __table_args__ = (
        CheckConstraint("length(btrim(step_name)) > 0"),
        CheckConstraint("status IN ('success', 'failed')"),
        Index(
            "publish_info_current_idx",
            "source_id",
            "step_name",
            unique=True,
            postgresql_where=text("is_current_publish"),
        ),
        Index(
            "publish_info_source_created_idx",
            "source_id",
            text("created_at DESC"),
        ),
        Index("publish_info_job_idx", "job_id"),
        {"schema": "public"},
    )

    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    step_name: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    is_current_publish: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class TableProfile(Base):
    """每个数据源中一个数据库对象的当前 JSON 画像（doc 19.2 §6.2）。

    主键为 (source_id, schema_name, object_name)；publish_id/job_id 是当前
    行最后一次成功发布的来源。json_sample_records 仅整体保留 JSON 的
    sample_records 节点供未来用途，页面样例数据不读该列。
    """

    __tablename__ = "table_profile"
    __table_args__ = ({"schema": "public"},)

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)

    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    metadata_version: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON 生成时间是带时区的 UTC 时间点（doc 19.3 §2.1），列名不再标记 local
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
    """每对象、每字段一行的当前 JSON 画像（doc 19.2 §6.2）。

    字段行只额外复制 publish_id 版本标识，插入时必须与所属对象行相同；
    某字段参与的约束/索引/候选列组不在此表持久化，读取时由对象行明细
    按字段名投影（doc 19.2 §6.3）。
    """

    __tablename__ = "table_column_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "schema_name", "object_name"],
            [
                # 三段式 schema 限定名：两段式在带 schema 的 metadata 里解析不到目标表
                "public.table_profile.source_id",
                "public.table_profile.schema_name",
                "public.table_profile.object_name",
            ],
            ondelete="CASCADE",
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

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    object_name: Mapped[str] = mapped_column(Text, primary_key=True)
    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
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
    """每个数据源中一个表/对象的一条 Domain 成员关系（doc 21.2 §3）。

    主键为 (source_id, schema_name, table_name, domain_name)：一张表可属于
    多个 domain，整份替换写入；publish_id/job_id 是当前行最后一次成功发布
    与产生 YAML 的采集作业。不建外键，删源清理见 100 备忘录第 4 条。
    """

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

    source_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    schema_name: Mapped[str] = mapped_column(Text, primary_key=True)
    table_name: Mapped[str] = mapped_column(Text, primary_key=True)
    domain_name: Mapped[str] = mapped_column(Text, primary_key=True)
    domain_desc: Mapped[str] = mapped_column(Text, nullable=False)
    db_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    publish_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
