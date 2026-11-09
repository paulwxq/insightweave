-- ==============================================================================
-- NL2SQL v3 数据库迁移脚本 - Milvus Collections（现状③结构规范）
-- ==============================================================================
--
-- 说明：
-- 1. 本文件用于统一维护 Milvus Collection 的 DDL 风格定义。
-- 2. Milvus 不直接执行 SQL，本文件作为结构规范与初始化参考。
-- 3. 实际创建 Collection 时，应使用 pymilvus 的 FieldSchema / CollectionSchema。
--
-- 本版与测试环境实测结构一致（2026-10-11/12 实测，见
-- docs/upgrade/1_Milvus结构变更与访问代码适配改造方案.md §2）。
-- 旧版规范（含 db_name / 两段 table_name / embedding / update_ts）已废弃：
-- 照旧版重建 collection 会使检索 65535（字段不存在）原样复现。
--
-- 通用约定：
-- - 向量维度：1024（对应 text_embedding_v3）
-- - 向量索引：HNSW，度量 COSINE，M=16，efConstruction=200
-- - 主键：三表均为写入方提供（auto_id=False，2026-10-12 实测确认），
--   Milvus 不自动生成 id
-- - 溯源字段：source_id（数据源短 ID，db_src_N，doc 5.1 §1）/
--   publish_run_id（发布运行 UUID，doc 12.1 写侧统一命名）
-- - 层级语义：表、视图和物化视图的表级记录统一 object_type='table'；
--   列记录经 parent_id 关联所属表级对象
--
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- Collection: table_schema_embeddings
-- 用途：表/列 schema 向量检索
-- ------------------------------------------------------------------------------
CREATE TABLE table_schema_embeddings (
    entity_id VARCHAR(512) PRIMARY KEY,      -- 溯源实体唯一 id（source_id:object_id 拼接），写入方提供
    source_id VARCHAR(36) NOT NULL,          -- 溯源：数据源短 ID（db_src_N）
    publish_run_id VARCHAR(36) NOT NULL,     -- 溯源：发布运行 UUID
    object_id VARCHAR(256) NOT NULL,         -- 表=db.schema.table 三段；列=db.schema.table.column 四段
    object_type VARCHAR(64) NOT NULL,        -- 'table' / 'column'（视图/物化视图的表级记录归 'table'）
    parent_id VARCHAR(256) NOT NULL,         -- 统一表键：表记录=自身 object_id（自指）；列记录=所属表三段 id
    object_desc VARCHAR(8192) NOT NULL,      -- 语义文本（被向量化的内容），RAG 发布侧生成
    embedding FLOAT_VECTOR(1024) NOT NULL,   -- object_desc 的向量化
    time_col_hint VARCHAR(512),              -- 时间列提示（仅表级可能有值）
    table_category VARCHAR(64),              -- 'fact' / 'dim' / 'bridge'；列记录为空
    updated_at BIGINT NOT NULL
);

CREATE INDEX embedding ON table_schema_embeddings
USING HNSW
WITH (metric_type = 'COSINE', M = 16, efConstruction = 200);

-- 字段约定：
-- - entity_id: 'source_id:object_id' 拼接（主键，写入方提供，auto_id=False）
-- - parent_id ≡ Neo4j Table 节点 id 属性（三段、同值同空间），
--   因此 Cypher `MATCH (t:Table {id: $x})` 在该键空间下零改动可用
-- - 检索侧表级取键 object_id（三段表键），列级取 object_id + parent_id 两键；
--   两段式显示名由提示词边界 to_display_name() 截断产出（§4.2），不在本层存储

-- ------------------------------------------------------------------------------
-- Collection: dim_value_embeddings
-- 用途：维度值向量检索（2026-10-12 重建后实测结构）
-- ------------------------------------------------------------------------------
CREATE TABLE dim_value_embeddings (
    col_id VARCHAR(2048) PRIMARY KEY,             -- 维度值唯一 id：source_id:表三段id:列名:维度值
    source_id VARCHAR(36) NOT NULL,               -- 溯源：数据源短 ID（db_src_N）
    publish_run_id VARCHAR(36) NOT NULL,          -- 溯源：发布运行 UUID
    table_name VARCHAR(256) NOT NULL,             -- 所属表三段式 id（与 table_schema_embeddings.parent_id 同一空间）
    col_name VARCHAR(128) NOT NULL,
    col_value VARCHAR(1024) NOT NULL,             -- 维度值文本
    col_value_embedding FLOAT_VECTOR(1024) NOT NULL,  -- 向量字段（注意：不叫 embedding）
    updated_at BIGINT NOT NULL
);

CREATE INDEX col_value_embedding ON dim_value_embeddings
USING HNSW
WITH (metric_type = 'COSINE', M = 16, efConstruction = 200);

-- 字段约定：
-- - col_id 示例：'db_src_1:orders.public.categories:category_name:类别01'
-- - table_name 为三段表键（本 collection 不设 parent_id，表归属即 table_name）
-- - 无 key_col / key_value 主键信息；下游降级展示（value_matcher 降级分支）

-- ------------------------------------------------------------------------------
-- Collection: sql_example_embeddings
-- 用途：历史 SQL 示例向量检索
-- ------------------------------------------------------------------------------
CREATE TABLE sql_example_embeddings (
    example_id VARCHAR(256) PRIMARY KEY,      -- 'source_id:8位hex'，写入方提供
    source_id VARCHAR(36) NOT NULL,           -- 溯源：数据源短 ID（db_src_N）
    publish_run_id VARCHAR(36) NOT NULL,      -- 溯源：发布运行 UUID
    question_sql VARCHAR(16384) NOT NULL,     -- JSON 字符串：{"question": ..., "sql": ...}
    embedding FLOAT_VECTOR(1024) NOT NULL,    -- 向量字段（此表仍叫 embedding）
    domain VARCHAR(256) NOT NULL,             -- 业务域
    updated_at BIGINT NOT NULL
);

CREATE INDEX embedding ON sql_example_embeddings
USING HNSW
WITH (metric_type = 'COSINE', M = 16, efConstruction = 200);

-- 字段约定：
-- - example_id 示例：'db_src_1:33f802ca'
-- - 无 db_name 字段：单源阶段不做库过滤；metaweave 并入后按 source_id 过滤
--   （一个 source_id 对应一个 database，见方案 §六）
