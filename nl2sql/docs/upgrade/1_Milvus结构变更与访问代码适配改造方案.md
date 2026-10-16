# 1_Milvus 结构变更与访问代码适配改造方案

> **日期**: 2026-10-11（方案 2026-10-12 定稿）
> **范围**: Milvus 向量库访问代码的适配改造及旧过滤辅助代码清理（`src/services/vector_adapter/milvus_adapter.py`）+ 契约注释同步（`base.py`、`retriever.py`）+ retriever 卡片补全扩展（`retriever.py:244-250`，§4.4）+ LLM 提示词边界的表名截断（新增共享模块 `src/tools/schema_retrieval/table_names.py`；`sql_generation.py` 的 formatter 与内联规则、`join_planner.py`、`value_matcher.py`，§4.2）+ Milvus 结构规范重写（`migrations/milvus.sql`，§4.5）。**pgvector_adapter 不在本次范围**（模式休眠，见 §6）
> **状态**: 方案已定稿（parent_id 统一表键），待实施
> **触发问题**: 运行时检索报 `MilvusException: (code=65535, message=field table_name not exist)`。精确失败链：根因失败点在 Schema 检索节点，内部标记 `error_type=schema_retrieval_failed`（`schema_retrieval.py:73`，返回 `schema_context=None`）；终端呈现的 `generation_failed` 是 SQL 生成节点对空 schema_context 的**连带降级**（空结果判断见 `sql_generation.py:540`，`error_type` 赋值见 `sql_generation.py:544`）—— 排障时勿被终端错误码引向 SQL 生成侧

---



## 一、背景与根因

Milvus 中的 RAG collection 结构已由 RAG 发布侧完成升级（新增溯源字段，结构回归 pgvector 原始模型），而仓库内的 Milvus 访问代码仍按 docs/82 那一版（含 `table_name`、`db_name` 字段）的契约编写。运行时 `search` / `query` 请求引用了 collection 中不存在的字段，被 Milvus 服务端整体拒绝（code 65535）。

**根因不是数据问题，是代码与结构的契约脱节。** 代码的匹配逻辑从未改变，改变的是数据中的"钥匙格式"（参见 4.0 节 ID 空间讨论）。

### 三代结构演进对照（帮助理解，非本文档重点）


|      | ① pgvector 原始 (`migrations/pgvector.sql` 的 `system.sem_object_vec`) | ② Milvus 旧规范（`migrations/milvus.sql` + docs/82） | ③ 当前线上 Milvus（RAG 侧升级后）                                                                                                           |
| ---- | ------------------------------------------------------------------- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| 唯一标识 | `object_id`（两/三段）                                                   | `object_id`（三/四段 db.schema.table[.column]）      | `object_id`（三/四段）同②                                                                                                               |
| 父引用  | `parent_id` 自动生成列（两段式）                                              | 改名为 `table_name`（schema.table）                  | `parent_id` 显式存储，= 父表 object_id（三段式）                                                                                              |
| 语义文本 | `text_raw`                                                          | `object_desc`                                   | `object_desc`                                                                                                                     |
| 业务表名 | 无                                                                   | `table_name`（schema.table 两段式）                  | parent_id相当于table_name，当object_type为table是，object_id与parent_id相同。当object_type为column时，object_id则是四段结构，而parent_id则代表当前column所属的表名。 |
| 库隔离  | 无                                                                   | `db_name`                                       | 无                                                                                                                                 |
| 溯源   | 无                                                                   | 无                                               | `entity_id` / `source_id` / `publish_id`                                                                                          |


> 注：相对②规范，当前结构③**有增、有删、有改名** —— 增：溯源三件套（entity_id/source_id/publish_id）；删：`db_name`、`table_name`；改名：dim_value 的 `embedding`→`col_value_embedding`、`update_ts`→`updated_at`（2026-10-12 已统一）、`id BIGINT AUTO_ID`→`col_id VARCHAR`。**本方案只按现状③适配，不追溯上游升级动作**。结构③本质上是回归①的设计（parent_id 层级 + object_desc 语义文本），外加溯源字段。
>
> 另外，本文的范围只设计对milvus的改造，不涉及对pgvector相关查询的改造。

---



## 二、当前 Milvus 结构（测试环境，2026-10-11/12 实测）

本节连接信息与具体表名仅用于说明测试环境的核验结果和标识格式，不限定生产环境的表名、表数量或业务场景。实现以字段契约和实际检索记录为准。

数据库：`nl2sql` @ 172.29.128.1:19530（认证关闭，`MILVUS_USER`/`MILVUS_PASSWORD` 留空）。
共 3 个 collection，向量维度均为 **1024**（RAG 发布侧使用 `qwen3.7-text-embedding`；查询侧模型已同步，见 config.yaml embedding_profiles）。

### 2.1 table_schema_embeddings（表/列 Schema 向量检索）


| 字段               | 类型                 | 说明                                                                                                                                       |
| ---------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `entity_id`      | VARCHAR(512)       | 溯源：实体唯一 id（`source_id:object_id` 拼接）                                                                                                     |
| `source_id`      | VARCHAR(36)        | 溯源：RAG 数据集 UUID                                                                                                                          |
| `publish_id`     | VARCHAR(36)        | 溯源：发布批次 UUID                                                                                                                             |
| `object_id`      | VARCHAR(256)       | 唯一标识。表=`db.schema.table` 三段；列=`db.schema.table.column` 四段                                                                                |
| `object_type`    | VARCHAR(64)        | `table` / `column`；表、视图和物化视图的表级记录统一归类为 `table` |
| `parent_id`      | VARCHAR(256)       | **统一表键（本方案核心）**。表记录=自身 object_id（自指）；列记录=所属表的 object_id（恒三段）                                                                             |
| `object_desc`    | VARCHAR(8192)      | **语义文本**（被向量的内容）。表级为结构化描述（含中文语义名、字段清单、示例值）；列级为当前字段的语义注释                                                                                  |
| `embedding`      | FLOAT_VECTOR(1024) | 向量字段，是对object_desc的向量化内容。                                                                                                                |
| `time_col_hint`  | VARCHAR(512)       | 时间列提示；非空时由 `_format_time_columns` 渲染，具体列名取自实际记录 |
| `table_category` | VARCHAR(64)        | 表分类：`fact` / `dim` / `bridge`；未识别的非空分类（如 `unknown`）由 `_classify_table_category` 告警并默认归入维度表；列记录为空 |
| `updated_at`     | INT64              | 更新时间戳                                                                                                                                    |


**通用处理规则**：表、视图和物化视图的表级记录统一使用 `object_type="table"`；列记录通过 `parent_id` 关联所属表级对象。检索与卡片补全根据实际记录处理，不依赖固定表名或表数量。

**测试环境示例**（说明标识格式与字段含义）：

```
object_id   = 'orders.public.categories'              object_type='table'  parent_id=自身（='orders.public.categories'）  table_category='dim'
object_id   = 'orders.public.categories.category_id'  object_type='column' parent_id='orders.public.categories'
object_desc(表) = '# public.categories [table]（商品类别维度表）## 字段列表：- category_id (integer) - 类别ID [示例: 1, 2]...'
object_desc(列) = '类别名称'
```

> `object_desc` 来源于 RAG 发布侧元数据，不以业务库存在 COMMENT 为前提；COMMENT 可以作为上游生成语义描述的素材。本项目直接使用发布后的 `object_desc`。



### 2.2 dim_value_embeddings（维度值向量检索，2026-10-12 重建后实测）


| 字段                         | 类型                 | 说明                                                                                                |
| -------------------------- | ------------------ | ------------------------------------------------------------------------------------------------- |
| `col_id`                   | VARCHAR(2048)      | 维度值唯一 id，构成：`source_id:表三段id:列名:维度值`（实测 `b4a87275-…:orders.public.categories:category_name:类别01`） |
| `source_id` / `publish_id` | VARCHAR(36)        | 溯源                                                                                                |
| `table_name`               | VARCHAR(256)       | 所属表的三段式 id（实测 `orders.public.categories`，与 table_schema_embeddings 的 parent_id 同一空间）              |
| `col_name`                 | VARCHAR(128)       | 列名                                                                                                |
| `col_value`                | VARCHAR(1024)      | 维度值文本                                                                                             |
| `col_value_embedding`      | FLOAT_VECTOR(1024) | **向量字段（注意：不叫 embedding，已由 RAG 侧确认改名）**                                                            |
| `updated_at`                | INT64              | 更新时间戳                                                                                 |




### 2.3 sql_example_embeddings（历史 SQL 示例检索）


| 字段                         | 类型                 | 说明                                                     |
| -------------------------- | ------------------ | ------------------------------------------------------ |
| `example_id`               | VARCHAR(256)       | 示例唯一 id，构成：`source_id:8位hex`（实测 `b4a87275-…:33f802ca`） |
| `source_id` / `publish_id` | VARCHAR(36)        | 溯源                                                     |
| `question_sql`             | VARCHAR(16384)     | JSON：`{"question": ..., "sql": ...}`                   |
| `embedding`                | FLOAT_VECTOR(1024) | 向量字段（此表仍叫 embedding）                                   |
| `domain`                   | VARCHAR(256)       | 业务域                                                    |
| `updated_at`               | INT64              | 更新时间戳                                                  |




### 2.4 关联事实：Neo4j 图库的钥匙空间（实测）

Neo4j `Table` 节点属性（新发布管道写入）：`id = 'orders.public.categories'`（三段，与 Milvus 的 `parent_id` **同值同空间**）、`full_name = 'public.categories'`（两段）、`name = 'categories'`（裸名）、`database = 'orders'`、`schema = 'public'`，另有 `source_id`/`publish_id` 溯源。

**关键结论：**`Milvus 的 parent_id ≡ Neo4j 的 id 属性`**。** 因此现有 `MATCH (t:Table {id: $x})` Cypher 在三段键下**零改动可用**。

---



## 三、当前代码需要的字段（差距清单）

代码：`src/services/vector_adapter/milvus_adapter.py`（`MilvusSearchAdapter`）。

本文行号均对应实施前的当前代码；修改后可能漂移，定位时优先使用函数名或测试名，行号作为辅助。


| #   | 方法                       | 位置                          | 引用的失效字段                                 | 问题类型                                  |
| --- | ------------------------ | --------------------------- | --------------------------------------- | ------------------------------------- |
| 1   | `search_tables`          | `milvus_adapter.py:151`     | `output_fields` 含 `table_name`          | 字段不存在 → 65535（本次崩溃点）                  |
| 2   | `search_columns`         | `milvus_adapter.py:203`     | `output_fields` 含 `table_name`          | 同上                                    |
| 3   | `search_dim_values`      | `milvus_adapter.py:251`     | `expr` 过滤 `db_name`                     | 字段不存在 → 65535                         |
| 4   | `search_dim_values`      | `milvus_adapter.py:257`     | `anns_field="embedding"`                | 向量字段已改名 `col_value_embedding` → 65535 |
| 5   | `search_similar_sqls`    | `milvus_adapter.py:301`     | `expr` 过滤 `db_name`                     | 字段不存在 → 65535                         |
| 6   | `fetch_table_cards`      | `milvus_adapter.py:362,368` | `expr` 与 `output_fields` 用 `table_name` | 字段不存在 → 65535                         |
| 7   | `fetch_table_categories` | `milvus_adapter.py:401,406` | 同上                                      | 字段不存在 → 65535                         |


---



## 四、改造方案（定稿：parent_id 统一表键）

**核心原则：全链路内部统一使用三段式** `parent_id` **作为表键（与 Neo4j** `id` **同空间，零转换），只在 LLM 提示词边界做一次截断（剥掉第一段 db 名）得到两段式显示名。**

### 4.0 ID 空间统一的设计决策（内部键空间的选择）

同一张表在不同环节的标识格式：


| 环节                             | 格式      | categories 的值              |
| ------------------------------ | ------- | -------------------------- |
| Milvus `object_id`             | 三/四段    | `orders.public.categories` |
| **Milvus** `parent_id`**（表键）** | **恒三段** | `orders.public.categories` |
| Neo4j `id` 属性                  | 三段      | `orders.public.categories` |
| Neo4j `full_name` 属性           | 两段      | `public.categories`        |
| LLM 提示词 / SQL（契约）              | 两段      | `public.categories`        |


候选过的两个方案：


|                  | 备选方案（已否决）：adapter 出两段 + neo4j_client 改匹配 `full_name` | **定稿方案（采纳，本文档）**：内部三段 parent_id + LLM 边界截断 |
| ---------------- | ---------------------------------------------------- | ------------------------------------------ |
| Neo4j 侧改动        | 改 3 处 Cypher（`{full_name, database}` 匹配）             | **零改动**（现有 `{id: $x}` 直接匹配）                |
| 多库隔离             | 靠 `database` 属性过滤                                    | **三段键自带 db 名，结构性隔离**                       |
| 翻译发生处            | neo4j_client（2段→匹配属性）                                | LLM formatter（三段→两段，唯一一处）                  |
| metaweave 多库接入成本 | 中                                                    | 最低                                         |


两者都是"只在一处翻译"，分歧仅在把哪段空间当内部标准。**采纳定稿方案**：三段 id 天然全局唯一，与 Neo4j 现有匹配逻辑及数据完全对齐，多库未来成本最低。

**统一表键定义**（parent_id 语义，RAG 发布侧确认）：


| object_type | parent_id 含义     | 段数    |
| ----------- | ---------------- | ----- |
| `table`     | 自身 object_id（自指） | 3     |
| `column`    | 所属表的 object_id   | 3（恒定） |


→ **任何记录取** `parent_id` **即得表键，无需按 object_type 分支判断，无需任何剥段派生。**

### 4.1 逐方法改造与配套清理（8 项）


| #   | 方法                       | 改造内容                                                                                                                                                                                                                                                                                                                                                          |
| --- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `search_tables`          | `output_fields` → `["object_id","object_type","table_category","time_col_hint"]`；返回 dict **删除** `table_name` **这个 key**（retriever 经 `table_name or object_id` 回退到 `object_id` 三段表键）。表记录 `parent_id ≡ object_id`，无需单独返回；**返回 dict 保留 `grain_hint=None`**（Milvus 无此字段，由适配器补齐；retriever 日志在 :138 读取），并保留 `object_type="table"`、`similarity`、`time_col_hint`、`table_category` |
| 2   | `search_columns`         | `output_fields` → `["object_id","parent_id","object_type"]`。**两键目的必须分开声明**：`parent_id` 承担表归属（业务逻辑，retriever 经 `table_name or parent_id` 回退取到）；`object_id` **仅用于追溯** —— 日志中定位具体命中的是哪一行（四段 id 含列名，parent_id 无法区分同表的两列），同时是 `retriever.py:861` 硬下标 `[m['object_id'] ...]` 的必需键（缺键即 KeyError）。**不得因返回 object_id 而改变 parent_id 的表归属逻辑**，object_id 不参与归并、不进提示词；**返回 dict 明确移除 `table_name` 与 `table_category`**，不再保留恒空的分类键；列命中父表的分类由 retriever 的 `table_category_map[parent_id]` 提供（:448），不读取列返回记录的 `table_category`。最终返回键为 `object_id`、`parent_id`、`object_type="column"`、`similarity`、`grain_hint=None` |
| 3   | `search_dim_values`      | 删除 `db_name` expr 过滤（当前单数据源），**删除处须留代码注释：**`# 将来与metaweave合并后，使用source_id过滤`；`anns_field` → `"col_value_embedding"`；返回的 `dim_table`（数据即三段）与表键同一空间；**`output_fields=["table_name","col_name","col_value"]` 保持不变**：`table_name` 仅从 Schema collection（`table_schema_embeddings`）中删除，维度值 collection（`dim_value_embeddings`）仍保留该字段，返回映射 `dim_table ← table_name` 保持不变。误删该输出字段会使当前读取逻辑得到 `dim_table=None`，导致命中无法归属到表；误改为该 collection 中不存在的字段（如 `parent_id`）才会触发字段不存在错误 |
| 4   | `search_similar_sqls`    | 删除 `db_name` expr 过滤，**删除处须留代码注释（同上）**；同步移除该方法 docstring（:297）中的 `db_name` 过滤描述；其余不变 |
| 5   | `fetch_table_cards`      | `expr` → `object_id in [表键列表] and object_type == "table"`（表记录 `object_id ≡ parent_id ≡` 表键）；`output_fields` → `["object_id","object_desc","time_col_hint","table_category"]`；cards key = `object_id`（三段表键，与入参同空间）；返回 dict **保留** `object_id` **字段** —— 追溯用途（同 §4.1-2 的两键分工原则），`text_raw`←`object_desc` 映射不变                                                   |
| 6   | `fetch_table_categories` | 与 `fetch_table_cards` 统一：`expr` → `object_id in [表键列表] and object_type == "table"`；`output_fields` → `["object_id","table_category"]`；返回映射 = `row["object_id"]（三段表键）→ row["table_category"]`。表记录 `object_id ≡ parent_id`，统一用 object_id 精确查询和取键；保留 object_type 过滤与仅收录非空分类的行为，替换原 `row.get("table_name")` 取键 |
| 7   | 契约注释同步（`base.py`、`retriever.py`，正式改造项） | 抽象基类是两个适配器的共同契约说明书，三处旧契约必须同步：`search_tables` 返回注释（`base.py:40-41`，删 `table_name` 行，表标识=三段）、`search_columns` 返回注释（:64-65，所属表取 `parent_id`）、`fetch_table_cards`/`fetch_table_categories` 入参注释（:125/:144，"schema.table 格式"→三段表键）；**卡片返回字段注释修正**：base.py :127-133 现写 `display_name`/`description`（实际从未返回），实际返回的是 `text_raw`（←`object_desc` 映射）——一并修正；`grain_hint` 相关注释一并复核。**retriever 返回说明同步**：`_retrieve_column_dimension_hits()` 的 docstring（`retriever.py:820-822`）改为说明返回适配器列命中并追加 `source_dimension_text`；当前 Milvus 形态包含 `object_id`、`parent_id`、`object_type`、`similarity`、`grain_hint=None`，所属表由 `parent_id` 表示，不含 `table_name` 与 `table_category`，父表分类通过 `table_category_map` 获取 |
| 8   | 旧 `db_name` 过滤辅助代码清理（正式改造项） | 随 §4.1-3/§4.1-4 删除两处 `_get_business_db_name()` 调用（`milvus_adapter.py:250,300`）及仅为旧过滤服务的 `business_db_name`、`search_expr` 局部计算，请求不再引用旧过滤表达式；删除此后无调用的 `_get_business_db_name()`（:119-123）及仅供该方法使用的 `get_config` import（:10）。保留两处约定的未来 `source_id` 过滤注释；本项随适配改造交付，不留待 §六清理。配套测试 fixture 清理见 §5.6 |


说明：

- **adapter 返回 dict 的键名必须与 retriever 的读取表达式对齐**（本节曾遗漏，经外部评审纠正）：
  - 表侧 retriever 只认 `t.get("table_name") or t.get("object_id")`（`retriever.py:135,409,492`），**不读 parent_id** → 表记录必须带 `object_id` 键，`table_name` 键删除（触发 `or` 回退）
  - 列侧 retriever 读 `c.get("object_id")`（`:159`，缺键返回 None）与硬下标 `m["object_id"]`（`:861`，缺键即 KeyError），并通过 `c.get("table_name") or c.get("parent_id")`（`:160,423,425,444`）获取所属表 → 列记录必须带 `object_id` + `parent_id` 两键，其中 :861 是必须保留 `object_id` 键的直接运行依据
  - 上述清单仅列运行链路中的读取点；无调用点的 `_collect_table_names()` 内 :332、:338 不作为契约依据，该函数的清理见 §六
  - "键的值"（三段表键空间）与"键的名字"（dict key 名）是两个独立契约，缺一不可
- 下游 `retriever.py` 的取值逻辑在新返回结构下经 `or` 回退自然工作，**现有取值逻辑无需修改**；本期仍须同步 §4.1-7 的返回说明 docstring，并实施 §4.4 的卡片补全扩展
- `fetch_table_cards` / `fetch_table_categories` 的入参（来自 retriever 的候选表集合）本就是三段表键，expr 直接 `in` 匹配，**不再需要拼装** `db.public.{table}`。两个方法均按表记录的 `object_id` 过滤并作为返回映射的 key；这是表级精确查询的统一约定，列记录的表归属仍由 `parent_id` 承担
- `search_columns` 返回的列记录两键各司其职：`parent_id` = 表归属（业务键，归并候选表的唯一依据）；`object_id` = 命中行的**追溯身份**（四段含列名，仅供日志定位具体是哪一行，不参与归并）。列的语义文本经 `object_desc` 向量检索命中后，`object_id` 从不进入提示词（已验证：`retrieve()` 的 schema_context 出口 `retriever.py:289-311` 仅有 6 个 key——join_plans/table_cards/similar_sqls/dim_value_hits/table_categories/metadata，逐字段无列 id；`_collect_and_classify_tables` 返回中的 `column_dim_summary` 注释明确"仅日志用，不进入 schema_context"，见 `retriever.py:540`）



### 4.2 LLM 提示词边界截断（唯一翻译点）

表键进提示词前剥掉第一段 db 名，得到两段式显示名：

```
'orders.public.categories'  →  'public.categories'   （保留表自身真实 schema，非写死 public）
```

**共享函数落点（本期新增模块）**：`to_display_name()` 定义在 `src/tools/schema_retrieval/table_names.py`，由 `sql_generation.py`、`join_planner.py`、`value_matcher.py` 共同导入。该模块只包含表名转换的纯函数，不依赖 Agent 或数据库客户端，也不反向导入 `sql_generation.py`，保持 `modules → tools` 的依赖方向，避免循环导入与重复实现。

**截断工具函数**（所有 formatter 复用，段数感知防误剥）：

```python
def to_display_name(s: str) -> str:
    parts = s.split(".")
    return ".".join(parts[1:]) if len(parts) >= 3 else s
```

**表名进 prompt 的出口清单（5 处，已逐一核实验证）**：


| #   | formatter                             | 位置                                                 | 渲染形态                                                        | 截断对象                                                                         |
| --- | ------------------------------------- | -------------------------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------- |
| 1   | `_format_table_cards`                 | `sql_generation.py:407-413`                        | `- **{卡片key}** {object_desc}`                               | 卡片 key                                                                       |
| 2   | `_format_table_categories`            | `sql_generation.py:372-405`                        | `- {分类}: 表1, 表2`（遍历 table_categories 的 key）                 | 分类列表中的表名                                                                     |
| 3   | `_format_time_columns`                | `sql_generation.py:415-422`                        | `- {卡片key}.{时间列}`（仅 time_col_hint 非空时）                      | 行首表名                                                                         |
| 4   | `format_join_plan_for_prompt`         | `join_planner.py:96`（base）、`:105-106`（边的源表与目标表）；调用点 `sql_generation.py:197` | `Base #{n}: **{plan["base"]}**` 及边 `{src} --> {dst} ON ...` | **base 和边都要截** —— 边的表名取自 Neo4j 节点 `id` 属性（`neo4j_client.py:174-175`，三段），非边属性 |
| 5   | `format_dim_value_matches_for_prompt` | `value_matcher.py:117`（带 key_col 分支）、`:125`（降级分支）；调用点 `sql_generation.py:200` | `{dim_table}.{key_col}='…'` / `{dim_table}.{dim_col}` | dim_table                                                                    |


**排除项**（不渲染表名，勿误改）：`_format_dimension_filters`（`sql_generation.py:345-362`）经 `build_optimized_filters` 只输出 `value=…`/`column=…`。

**配套：提示词规则 2 澄清** —— ⚠️ 运行时落点是 `sql_generation.py` 的 `_build_prompt()` 内联规则（约 :229），**不是** `generator_prompt.txt`（实测：该 txt 仅被 `sql_generation_subgraph.yaml:122` 的 `template_path` 指向，无任何代码读取，属死资产；改 txt 不生效）：

运行时规则 2 原文：`2.所有表必须包含 schema 前缀（例如 public.table）。` —— 本体未写死 public，在其后加一句澄清：

> 表名的 schema 以检索结果提供的为准（不一定是 public）；截断后的两段名已含表自身真实 schema，直接使用即可。

`generator_prompt.txt` 可同步更新保持一致，但不作为运行时改造落点。

**完备性风险与后果（修订）**：漏截一处的失败是**非确定性**的 —— 提示词同时出现三段名与两段名时，LLM 可能照规则 2 自行纠正前缀（不报错但不可控），也可能原样写入 SQL 报 `cross-database references`。不能依赖"必然响亮失败"，必须靠出口清单完备 + 验证计划第 3 步的专项断言兜底。

**上游文本契约（截断覆盖范围的边界）**：上述出口只覆盖**结构化表键**。提示词中还有三类文本原样进入：`object_desc`（表卡片描述）、历史 SQL（`question_sql.sql`）、JOIN ON 模板文本。实测样本均为两段式（object_desc 头部 `# public.categories [table]`、历史 SQL `FROM public.screenings`），说明发布侧遵守"两段 = 展示名"的契约 —— 但这是上游约定而非本项目可强制执行的规则；本文的截断与断言只保证结构化键无三段泄漏，**不据此宣称任意完整 prompt 绝无三段名**。

### 4.3 设计决策记录


| 决策                     | 结论                                                                                                    | 依据                                                                                                                                                                                |
| ---------------------- | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `table_name`（旧字段）      | 不再输出 —— 该字段在现状③已不存在（§2.1 实测）；两段式显示名由 §4.2 截断产出，语义文本载体是 `object_desc`                                  |                                                                                                                                                                                   |
| `object_desc` 来源       | RAG 发布管道生成（非业务库 COMMENT） | RAG 发布侧提供 object_desc，检索侧不依赖业务库 COMMENT |
| `db_name` 过滤           | 删除，不做替代                                                                                               | 当前单数据源（orders，全表同属一个 `source_id`）；metaweave 并入后按查询绑定的 `source_id` 过滤（一个 source_id 对应一个 database，见 §6）                                                                                                        |
| 内部键空间                  | 三段 `parent_id`（定稿方案）                                                                                  | 与 Neo4j `id` 同空间零转换；多库结构性隔离；见 4.0                                                                                                                                                 |
| 两段式 `schema.table` 的地位 | 从"全链路契约"降级为"LLM 显示格式"                                                                                 | docs/82 防止 db 名进 SQL 的初衷不变，只是翻译点移到提示词边界                                                                                                                                           |
| Neo4j 侧                | **零改动**（限定：指访问代码无需因 ID 格式变化而改）                                                                        | `parent_id ≡ id`（2.4 节实测）；实测 `check_table_exists("orders.public.orders") → True`、`check_table_exists("public.orders") → False` —— 当前图内 `id` 为三段，两段查不到（docstring 中两段示例为历史遗留，不代表现状） |
| 列标识                    | 零处理                                                                                                   | 四段 `object_id` 不进提示词；列语义走 `object_desc`，标识归并由 `parent_id` 承担                                                                                                                      |
| 公共契约 vs 后端标识空间         | **键名契约以 Milvus 现状为准**（表检索必备 `object_id`；列检索必备 `object_id` 与 `parent_id`；`table_name` 移除）；标识空间是各后端私有属性，不做跨后端统一，也不在本文承诺 pgvector 侧行为 | pgvector 模式休眠中（`active: milvus`），orders 库无 `system` schema（已验证），不在范围                                                                                                              |




### 4.4 配套改造项：retriever 卡片补全扩展（既有缺口，JOIN 打通后必然暴露）

**缺口描述**（`retriever.py`：`all_table_ids` 构造在 :244-249，`fetch_table_cards()` 调用在 :250）：`fetch_table_cards` 的入参只含候选事实表+维度表，**桥接表被排除、JOIN 计划新增的中间表不在内**。实测 JOIN 路径 orders → products → categories：若向量检索只命中 orders 与 categories，JOIN 计划含 products，提示词"表结构"段却没有 products 的卡片 —— 与运行时内联规则 5（"只允许使用'表结构'中列出的字段，禁止使用任何未列出的字段"）直接冲突，LLM 将无字段可写或自行推断。

**性质**：既有缺口（与本次结构变更无关，旧数据下同样存在）；但本次打通 JOIN 链路后该路径必然被走到，**纳入本次改造与验收**。

**改造**（`retriever.py`：替换 :244-249 的 `all_table_ids` 构造，沿用 :250 的 `fetch_table_cards()` 调用；时序可行：JOIN 计划已在步骤 4 `:211` 取回，卡片获取在其后的步骤 5）：

```python
join_table_ids = []
for plan in join_plans:
    if plan.get("base"):
        join_table_ids.append(plan["base"])
    for edge in plan.get("edges", []):
        join_table_ids.append(edge["src_table"])
        join_table_ids.append(edge["dst_table"])
all_table_ids = list(dict.fromkeys(
    candidate_set["candidate_fact_tables"] +
    candidate_set["candidate_dim_tables"] +
    candidate_set["candidate_bridge_tables"] +   # 桥接表补入（原本被排除）
    join_table_ids                                # JOIN base 与边两端表，按计划遍历顺序追加
))
```

`join_plans` 变量在步骤 4 已就绪，直接引用；全程 list 收集 + `dict.fromkeys()` 去重，顺序确定（候选表保持原序，JOIN 表按计划遍历顺序以首次出现位次追加），不引入 set 的哈希序不确定性。

### 4.5 Milvus 结构规范重写（migrations/milvus.sql，本期交付物）

**背景**：`src/services/db/migrations/milvus.sql` 仍描述②规范（含 db_name/table_name/embedding/update_ts），与现状③脱节。它无任何代码引用（已核实：全仓无 `create_collection`，插入按 `collection.schema.fields` 动态拼），但它是仓库唯一的 Milvus 结构规范 —— 照它重建 collection 会使 65535 原样复现。

**交付物**：按 §2 实测现状重写该文件 DDL，包含：三张表的字段清单与类型、向量索引（HNSW / COSINE / M=16 / efConstruction=200）、`parent_id` 层级语义（表记录自指、列记录指向所属表）、以及**主键规范 —— 三表主键分别为 `entity_id` / `col_id` / `example_id`，均为写入方提供（`auto_id=False`，2026-10-12 实测确认）**，重写规范必须如实标注。

---



## 五、验证计划

1. **集成冒烟验证**：临时测试脚本（不入正式测试树），直连 Milvus 依次调用 6 个方法，断言：
  - 不抛 65535
  - **键名契约**：`search_tables` 返回 dict 含 `object_id` 键、不含 `table_name`，且 `grain_hint is None`；`search_columns` 返回 dict 同时含 `object_id` 与 `parent_id`，不含 `table_name` 与 `table_category`，且 `grain_hint is None`（模拟 retriever 的 `t.get(...) or t.get(...)` 取值链不为 None）
  - `search_tables` / `search_columns` 返回的表键为三段（`orders.public.categories` 形态）
  - `fetch_table_cards(['orders.public.categories'])` 命中并返回 `text_raw`（即 `object_desc`）
  - **维度表键契约**：以当前已发布的维度值为输入调用 `search_dim_values`，断言返回结果非空（避免空结果使校验无效），且每条命中的 `dim_table` 均为非空三段式 `db.schema.table`（恰好三段，各段非空），与当前数据源数据库及对应表级记录的 `object_id` 一致；该字段是 retriever 回填候选表的来源（:397），不得在适配器中提前转换为两段显示名
  - `search_dim_values` / `search_similar_sqls` 正常返回
2. **JOIN 链路（两级断言）**：
  - **契约级**：`neo4j_client.plan_join_paths(base_tables=["orders.public.orders"], target_tables=["orders.public.categories"], max_hops=5)` 返回非空且 edges 非空，且路径为预期表序列（orders → products → categories）；`check_table_exists("orders.public.orders")` 为 True
  - **链路级**：`SchemaRetriever.retrieve(...)` 输出的 `join_plans[0]["edges"]` 非空 —— 只有链路级能守住"候选表键段数与图内 `Table.id` 一致"这一本方案唯一可能静默失败的契约
  - ⚠️ 生产链路为 `retriever.py:211` `_retrieve_join_plans` → `:762` `_plan_join_paths_for_base` → `:782` `plan_join_paths`（一次一个 base）；**不可用** `build_join_plans` **作验证载体**（全仓零调用点的死代码，见第六节）
3. **提示词截断验证（固定输入优先，确定性覆盖）**：
  - **formatter 单元断言**（固定输入，逐出口）：直接调用 5 个 formatter，传入含三段表键（如 `orders.public.orders`）的构造数据，断言输出中表名均为两段。另须覆盖：两段输入幂等（`sales.orders` 原样返回）、**非 public schema 三段输入不被误剥**（`orders.sales.orders` 截后为 `sales.orders`，保留非 public schema，而非 `orders`）、维度值两个展示分支（带 `key_col/key_value` 的 PgVector 形态 / 降级展示的 Milvus 形态）的 `dim_table` 均被截断。**当前 Milvus 运行链路不进入带主键信息的分支；该分支的固定输入断言仅作兼容回归保护**
  - **组合断言**：构造字段齐全的 schema_context（cards/categories/time_columns/join_plans/dim_hits 全配齐三段键），调用 `_build_prompt()`，断言完整输出不含 `orders.public.`
  - **真实查询降级为集成验收补充**：真实召回、阈值与解析结果决定分支是否进入（如时间列非空的表未必被召回），不能作为出口覆盖的保证 —— 只用于验证端到端串接
4. **中间表卡片补全（§4.4 专项，固定输入）**：固定候选表 = `[orders.public.orders, orders.public.categories]`（products **不在**向量候选中）、固定 `join_plans` = 实测 2 跳路径（orders → products → categories），断言：① `fetch_table_cards()` 的调用参数包含 `orders.public.products`；② prompt"表结构"段包含 products 的字段说明 —— 守住"JOIN 计划引用的表必有字段说明"这一规则 5 的前提。**不依赖真实查询恰好形成该召回**（真实召回是运行时变量）。**测试落位**：本用例与 formatter 断言进入正式测试树（`tests/unit/`，随 §4.4 实现同步交付）；§5.1 直连 Milvus 的临时脚本属集成冒烟验证，不入正式树
5. **端到端**：重启后端，通过前端或 `POST /api/v1/query` 发起真实查询，确认 Schema 检索 → SQL 生成 → 执行 → Summarizer 全链路通过
6. **回归与基线（三步，按顺序执行）**：
  **第 1 步：先立基线**。基准命令（两个测试树都跑——`pytest.ini` 的 `testpaths = src/tests` 使裸 `pytest` 只跑 src/tests，显式路径只跑根树，各漏一半）：
   ```bash
uv run pytest tests/unit/ src/tests/ -v --continue-on-collection-errors
```

`--continue-on-collection-errors` 用于跳过一个既有收集错误：`src/tests/unit/tools/test_join_planner.py:6` 导入了不存在的 `build_multi_base_join_plans`（join_planner.py 无此函数），属**既有技术债，与本次改造无关**，另行处理（见第六节）。
   **第 2 步：必改断言清单**（`tests/unit/vector_adapter/test_milvus_adapter.py` —— 这些断言的新形态就是 §4.1 契约的单元测试表达，属**交付物的一部分**，实施时重写而非视为"被破坏"）：

  | 测试（当前行号） | 旧断言                                               | 必改原因                                                               |
  | --- | ------------------------------------------------- | ------------------------------------------------------------------ |
  | `test_search_tables_returns_time_col_hint`（:257） | `results[0]["table_name"] == "public.fact_sales"` | search_tables 不再输出 `table_name` 键（§4.1-1）                          |
  | `test_search_columns_grain_hint_none`（:287） | `results[0]["table_name"] == "public.table1"`     | search_columns 同上（§4.1-2）                                          |
  | `test_search_dim_values_missing_key_fields`（:318） | `expr == 'db_name == "test_db"'`                  | `db_name` 过滤删除（§4.1-3）                                             |
  | `test_fetch_table_cards_json_serialization`（:344） | `'["public.table1", ...]' in expr`                | fetch_table_cards 改 `object_id in [表键列表] and object_type == "table"`（§4.1-5）                   |
  | `test_fetch_table_categories_json_serialization`（:366） | `'["public.dim_store"]' in expr`                  | fetch_table_categories 改 `object_id in [表键列表] and object_type == "table"`，查询字段和返回取键均为 `object_id`（§4.1-6） |
  | `test_search_similar_sqls_returns_matches`（:401） | `expr == 'db_name == "test_db"'`                  | 同 318（§4.1-4）                                                      |
  | `test_search_dim_values_with_embedding`（:419） | `expr == 'db_name == "test_db"'`                  | 同 318（§4.1-3）                                                      |

   配套新增或补充断言：`search_columns` 返回结果不含 `table_category` 与 `table_name`，表/列检索均保留 `grain_hint=None`；两个表级精确查询使用 `object_id` 过滤、查询并取键。Mock 字段应与请求的 `output_fields` 一致。

   **旧配置 fixture 清理（随 §4.1-8 交付）**：删除 `tests/unit/vector_adapter/test_milvus_adapter.py:67-80` 的 `mock_global_config` fixture（包含其 `@pytest.fixture` 装饰器）；它在 :70 patch `milvus_adapter.get_config`，删除该 import 后继续 patch 会在 fixture 初始化时抛出 AttributeError。同步删除以下 5 个用例签名中的 `mock_global_config` 参数：`test_search_dim_values_missing_key_fields`、`test_search_similar_sqls_returns_matches`、`test_search_dim_values_with_embedding`、`test_search_dim_values_with_min_score`、`test_search_dim_values_default_min_score`。保留这些用例对字段映射、embedding 调用及分数过滤的验证；本项只清理已失效的配置依赖。

   另：相关用例的 mock `entity` 字段按新 `output_fields` 重写（旧 mock 含 `table_name`，新实现从 `object_id`/`parent_id` 取值，不改 mock 会取到 None）。其中 `:225`（`test_fetch_table_cards_field_mapping`，卡片 `object_id`）与 `:564`（`test_similarity_threshold_filtering`，`search_tables()` 表侧 `object_id`）两条断言无需改动 —— object_id 在两处返回中均保留（追溯用途，§4.1-1/§4.1-5），对应 mock 须含 object_id 值。
   **第 3 步：回归判定**。期望结果 = 除按新契约重写的 `test_milvus_adapter.py` 外零失败；`src/tests/` 侧对照第 1 步基线判读（基线中已红的保持红，不新增红）。

---



## 六、后续事项（非本期范围）

- **死代码清理**：`build_join_plans`（`join_planner.py:6`）全仓无调用点（仅 `retriever.py:10` 一行死导入），建议删除，避免误导后续维护者以为它在生产链路中使用；`tests/`、`scripts/` 均无引用，删除安全
- **陈旧测试修复**：`src/tests/unit/tools/test_join_planner.py:6` 导入不存在的 `build_multi_base_join_plans`（收集期 ImportError，导致整轮 pytest 中断），与 `build_join_plans` 死代码属同一片重构废墟 —— 或指回现有函数、或删除该批用例，与本次改造分开处理
- **死代码清理（补充）**：`_collect_table_names`（`retriever.py:313-342`）全仓零调用点，且函数体正是旧契约表达式（`table_name or object_id`）—— 与 `build_join_plans` 一并删除；如需保留语义，压缩为 `_collect_and_classify_tables` 上方一行注释说明表标识来源
- **pgvector_adapter 范围边界**：本次不修改 `pgvector_adapter.py`。Milvus 改造后其返回 dict 仍带 `table_name` 键（旧形状），retriever 对三个键名任一兼容、且该模式休眠（`active: milvus`），无功能影响。将来启用 pgvector 模式需另行适配（建 `system.sem_object_vec` 灌数、标识空间与 Neo4j 对齐），不属本文
- **pgvector 维度值实现的后续退役**：当前链路未启用该实现，现有环境也不具备运行条件，建议后续退役。依据为：① `config.yaml:69` 为 `active: milvus`，工厂仅在 `active: pgvector` 时实例化 `PgVectorSearchAdapter`（`factory.py:64-66`），当前配置下检索链路不会调用 `PGClient.search_dim_values()`；② 仓库内未发现 `system.dim_value_index` 的 loader/ETL 或其他写入方，仅保留建表 DDL；当前配置连接的业务库经只读查询确认无 `system` schema，`dim_value_index` 与 `sem_object_vec` 均不存在。上述事实说明当前未使用，不能单独视为已正式废弃的证明，也不能由仓库内无写入方推断外部发布流程。单独调用 `PGClient.search_dim_values()` 时，缺表异常会触发仍查询同一张表的 fallback（`pg_client.py:319-356`），两次异常均打印错误，最终返回 `[]`，上层可能将其视为无匹配；直接切换整个系统到 pgvector，还可能在维度值检索前因缺少 `sem_object_vec` 而失败。后续清理该维度值实现（`pg_client.py:268-356`）时应同步处理适配器调用与相关测试；`PGClient` 仍承担 SQL 执行和 EXPLAIN 校验，不属于退役范围。
- **维度值匹配功能与提示词清理**：维度值匹配功能仍在当前 Milvus 链路中使用：问题解析允许产出 `role='value'`（`question_parsing.py:173`），`retriever._retrieve_dim_value_hits()`（`:869`，调用适配器于 `:902`）经 Milvus 向量检索取得命中，回填候选表并进入提示词。因此 §4.1-3 与 §4.2 出口 5 仍是本期必改项。当前 Milvus 返回结果不含 `key_col/key_value`，正常运行链路不会进入 `value_matcher.py:115-121` 的主键展示分支，但不能将该分支认定为对所有输入永远不可达。`value_matcher.py:131-134` 的“使用建议”在存在可展示命中时仍会追加，包括无主键信息的 Milvus 命中，属于仍在运行的提示内容；后续建议按主键信息是否存在调整措辞，避免在仅有文本命中时笼统建议使用主键条件。内联规则 8（`sql_generation.py:235`）已以“给出了明确的主键等值条件”为前提，可保留；主键能力并不依赖 trigram 实现。
- **历史 embedding 探针处置**：建议后续删除 `src/tests/integration/test_embedding_similarity.py`。其模块 docstring（`:4`）说明它服务于 `dim_value_index` 文本相似度与向量相似度差异的核对；实际测试（`:56-83`）仅调用 embedding、计算并打印两组余弦相似度，断言数量与值域，没有查询 trigram，也没有断言两种匹配方式的差异或业务匹配质量，作为历史选型探针的自动回归价值有限，无需为了保留文件而改写。§5.1 的临时 Milvus 冒烟脚本会经 `search_dim_values()` 内部的 `embed_query()` 覆盖真实 embedding 调用与检索串接，但不完整覆盖该探针的批量调用和本地余弦计算，因此删除理由是原选型目标不再服务当前链路，而非声称测试已被完全重复覆盖。
- **将来恢复维度值到主键条件的能力**：建议由 RAG 发布侧在 `dim_value_embeddings` 发布 `key_col/key_value` 等必要主键信息，并同步调整 collection 结构、适配器 `output_fields`、返回映射及测试，约定字段类型、适用范围与缺失时的展示行为；仅发布侧填充字段不足以让下游读取该能力，无需以恢复 trigram 链路为前提。此项属于后续设计，不改变本期无主键信息的 Milvus 返回契约。
- **死提示词模板资产**：`src/prompts/sql_generation/generator_prompt.txt` 及其 `template_path` 配置项（`sql_generation_subgraph.yaml:122`）未被任何运行时代码读取（运行时提示词内联在 `sql_generation.py` 的 `_build_prompt()`）—— 可选同步更新保持一致，或后续清理；切勿误将其当作提示词修改的落点
- **metaweave 多库接入的既定设计（source_id 绑定）**：三个 collection 均已含 `source_id`，一个 source_id 对应一个 database；并入 metaweave 后查询时从 source_id 读取连接信息，并将 source_id 自动绑定到 Milvus 查询过滤 —— 单库查询由 source_id 控制，**不做跨库查询**（当前单源阶段暂不过滤，行为等价）。当前开发阶段，不涉及source_id的筛选。
