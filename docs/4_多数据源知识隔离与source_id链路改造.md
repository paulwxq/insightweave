# 4. 多数据源知识隔离与 source_id 链路改造

记录及决策更新日期：2026-10-17。状态：**已确认直接实现 B 多源共存、增加 entity_id、Column 增加 source_id、发布按源删除；尚未实施**。

本文记录多数据源场景下 Milvus 与 Neo4j 的按源隔离现状、直接实现 B 多源共存的设计方向，以及 source_id 从前端到检索的传递方案。用户已确认不实施 A 单源过渡方案，即使当前只有一个源也直接按 B 改造。本文不提供最终 DDL、完整接口定义或开发排期；事务实现边界在 §4.5 作为建议说明，不代表已经修改配置或代码。

关联文档：[0_项目整合讨论与共识.md](0_项目整合讨论与共识.md)、[2_NL2SQL_Vue接入设计.md](2_NL2SQL_Vue接入设计.md)、[3_SQL查询结果文件存储问题与改造思路.md](3_SQL查询结果文件存储问题与改造思路.md)。

## 1. 结论摘要

- **source_id 是唯一锚点**：`source_id`（`public.db_source_info` 主键，UUID）是数据源的唯一标识。db_name 不唯一、也不打算设置为唯一——客户不同数据库中 db_name 相同很常见；登记侧无需保证 db_name 一致性。用户选源时界面提供 source_id、db_type、display_name、host、port、db_name 等完整候选信息，不会让用户只凭 db_name 做选择；展示文案可只用 db_name 或 display_name，取值始终绑定 source_id。
- **Milvus：写入侧已按源隔离，缺查询过滤**。三类集合的主键均含 source_id 前缀，不同源（包括同名库）的数据可共存、互不覆盖；缺口在 NL2SQL 检索适配器尚未按 source_id 过滤，改造点小、方向已定。
- **Neo4j：当前是单源在线模型，目标直接采用 B 多源共存**。当前发布为全清重载，节点合并键不含 source_id。目标为 Table/Column 增加 entity_id，以独立 source_id 标记归属；普通发布只替换当前源，不清空其他源。A 单源过渡方案已排除，不纳入开发计划。
- **source_id 必须从前端带过去**：Vue 选定数据源后，创建会话与提问请求都携带 source_id，会话建立时即与 source_id 绑定；后端在检索、图查询、连接获取、归档全链路按该 source_id 执行，才能在多源情况下完成正确查询。

### 1.1 模块职责与 Vue 开发的关系

**本文涉及 MetaWeave 后端和 NL2SQL 后端的多源能力改造，不是仅修改 Vue 页面。** NL2SQL 的 Vue 查询页面尚未开发；本文为后续 Vue 接入补齐后端前提，页面及 Web API 接入设计仍由 doc 2 承接。

| 范围 | 职责与改造内容 | 对应章节 |
| --- | --- | --- |
| MetaWeave 后端 | 生成按源独立的图节点和关系，迁移约束，按源发布、删除与核验 | §4.2–§4.6、§4.8 |
| NL2SQL 后端（新的 db_deepagent） | Milvus 查询过滤、Neo4j 实体定位与寻路，以及 source_id 向检索、图查询和业务库连接的传递 | §3、§4.7、§5 |
| 统一 Vue 中已有的 MetaWeave 页面 | 沿用采集、关系编辑与发布入口，保持接口兼容并做回归验证 | §1.2 |
| 统一 Vue 中的 NL2SQL 查询页面（待开发） | 选择数据源，创建会话与提问时携带 source_id，展示查询结果 | 本文 §5.1 记录传参要求；具体页面和接入见 doc 2 |

Vue 传入 source_id 不会自动改变后端查询。当前 NL2SQL 后端已有的 Milvus 检索和 Neo4j 寻路代码缺少源限定，因此即使页面正确传参，后端不改仍会有串源风险。这些后端改造可以先于 Vue 页面开发，通过脚本或服务层测试验证；不要求先开发页面。本范围只对接新的 db_deepagent，不扩展为旧父图的 Vue 接入。

### 1.2 现有 MetaWeave Vue 的兼容边界

本次不将现有 MetaWeave Vue 的功能改造列为必做项。现有 [采集、关系编辑与发布客户端](../frontend/src/api/dataSources.ts) 已在请求路径中携带 source_id/job_id；新增 entity_id、节点及关系归属、按源删除和事务处理由后端完成，Vue 不构造实体键或参与图迁移。

后端改造须保持现有接口路径、请求和响应结构兼容，保留关系编辑使用的 relationship_id 及业务字段，并通过现有预检 reason、发布结果 message 或错误响应 message 返回可读原因。当前 [发布页面](../frontend/src/views/PublishView.vue) 可以沿用既有通道展示结果与错误；迁移后重新发布 rel 也使用该页面，不新增迁移页面或维护开关。

这里“兼容”的口径是：接口路径、既有必填请求及既有响应字段的含义和类型保持不变，允许新增旧客户端可以忽略的可选信息字段；不能借新增字段改变旧字段的统计范围或成功/失败含义。message 是供用户阅读的说明，可补充必要原因，不作为前端解析计数的协议。

本期不为展示按源计数新增发布响应字段或 Vue 组件。普通 rel 发布的预期/实际按源计数和核验结论写入后端日志，并关联 source_id、publish_id；一次性迁移的核验结果按 §4.8.1 写入报告。核验失败仍须通过现有失败状态和可读 message 返回，不能只记日志后返回成功。

联调验收应回归数据源切换、采集作业选择、关系编辑、单独重新发布 rel、发布成功/失败展示，以及键空间不支持时的明确错误提示；同一页面的 dim_value/sql 发布仅补充入口、提交及结果展示的冒烟验证，不重测其生成算法或扩大写侧改造。若实际实现破坏上述接口兼容或现有页面无法展示必要信息，应先报告具体差异并调整设计，不能预先扩大为前端重构。NL2SQL Vue 查询页面仍按 doc 2 单独开发。

## 2. 现状核对

### 2.1 平台登记（PostgreSQL `public.db_source_info`）

- source_id 为 UUID 主键，唯一性由登记侧保证；`(host, port, db_name, username)` 精确查重加 `confirm_duplicate` 软覆盖，不构成 db_name 唯一约束。
- 登记信息含 db_type（约束为 postgresql）、host、port、db_name、username、sslmode、selected_schemas、password_ciphertext（经 `SOURCE_CREDENTIAL_KEY` 加解密）等。依据：[登记模型](../metaweave/backend/persistence/models.py)、[登记仓储](../metaweave/backend/data_sources/repository.py)、[密码加解密](../metaweave/backend/security/source_password.py)。

### 2.2 Milvus：写入已隔离，查询未过滤

三类共用集合的主键均由 MetaWeave 写入时拼接 source_id 前缀，未使用分区键：

| 集合 | 主键 | 拼接位置 |
| --- | --- | --- |
| table_schema | `entity_id = source_id:object_id` | [build_entity_id](../metaweave/metaweave/core/table_schema/models.py)（无 source_id 时退化为裸 object_id，当前发布链路总会提供 source_id） |
| dim_value | `col_id = source_id:table_name:col_name:col_value` | [维度加载](../metaweave/metaweave/core/loaders/dim_value_loader.py) |
| sql_example | `example_id` 前缀 source_id | [示例加载](../metaweave/metaweave/core/sql_rag/loader.py) |

因此不同源、乃至不同服务器同名库的数据在 Milvus 中天然共存，写入互不覆盖。

缺口在查询侧：NL2SQL 的检索适配器 [milvus_retrieval.py](../nl2sql/src/modules/db_deepagent/services/milvus_retrieval.py) 当前不带任何 source_id 过滤——表检索仅按 `object_type == "table"`，维度值与 SQL 示例检索不传 expr。多源数据共存后，检索会命中其他源的表、维度和示例。

### 2.3 Neo4j：单源在线模型

四个事实叠加，决定了当前图库同一时刻只能正确服务一个源：

1. **发布为全清重载**：`load(clean=True)` 先执行 `_clean_database()` 清空整库再写入（[cql_loader.py](../metaweave/metaweave/core/loaders/cql_loader.py)）。任一源发布后，图中只剩该源的数据。
2. **节点合并键不含源**：Table 节点 `MERGE (n:Table {full_name})`，full_name 仅为两段 `schema.table`（[reader.py](../metaweave/metaweave/core/cql_generator/reader.py) 构造、[writer.py](../metaweave/metaweave/core/cql_generator/writer.py) 合并）；三段 `db.schema.table` 只是 `id` 属性，不是身份。Column 节点同样按 `schema.table.column` 合并。这意味着不仅同名库会撞，**任何两个源只要存在相同的 schema+表名（例如都有 `public.orders`），在共存场景下就会合并为同一个节点**。
3. **边身份不含源**：JOIN_ON 的 `relationship_id = rel_ + MD5(schema, table, 列对应对, salt)[:12]`，不含库名与 source_id（[repository.py](../metaweave/metaweave/core/relationships/repository.py)）；HAS_COLUMN 两端按 full_name 匹配。
4. **Table.source_id 属性已写入但未启用**：发布时给 Table 节点 SET source_id/publish_id（Column 与边不注入，metaweave 文档 23.2 的 D3 决策；「多源互斥靠单源发布纪律」在其 §8 挂账）。NL2SQL 的 [join_graph.py](../nl2sql/src/modules/db_deepagent/services/join_graph.py) 按 `Table {id: 三段键}` 匹配并走 JOIN_ON 寻路，不读 Column 节点（列信息来自业务库实时 inspect）。

## 3. Milvus 改造（方向已确认）

**Milvus 知识查询必须绑定当前会话的 source_id。** 现有主键前缀避免不同源写入互相覆盖，但不会让查询自动隔离。此次按源过滤不要求更换现有集合主键或增加分区键。

### 3.1 全部六个入口统一过滤

在 [milvus_retrieval.py](../nl2sql/src/modules/db_deepagent/services/milvus_retrieval.py) 的以下入口中，将 `source_id == "<服务端会话绑定的 source_id>"` 与原有查询条件组合：

| 方法 | 用途 | 必须保留并组合的条件 |
| --- | --- | --- |
| search_tables | 表向量检索 | object_type 为 table |
| search_columns | 列向量检索 | object_type 为 column |
| search_dim_values | 维度值向量检索 | 原检索条件及阈值 |
| search_similar_sqls | SQL 示例向量检索 | 原检索条件及阈值 |
| fetch_table_cards | 按对象标识回查表卡片 | object_id 范围、object_type 为 table |
| fetch_table_categories | 按对象标识回查表分类 | object_id 范围、object_type 为 table |

精确回查也必须过滤，不能只在最初的向量检索中过滤。同名 object_id 的字典整理只能发生在已限定来源的结果集上。

### 3.2 source_id 的可信来源与失败行为

- 创建会话时，后端验证 source_id 并持久化绑定；后续检索从该绑定解析来源，不把模型填写的工具参数或未经验证的请求字段当作查询范围。
- 前端续聊携带的 source_id 仅用于一致性检查，不能覆盖原会话绑定；不一致明确拒绝。
- source_id 缺失、非法或会话绑定无法解析时拒绝查询，**禁止回退为不带源过滤的查询**。
- source_id 使用规范化 UUID，并按 Milvus 表达式规则构造过滤；不能用 db_name、display_name 或 `pg:` 连接指纹替代平台 source_id。
- 若服务实例构造时绑定来源，该绑定必须不可被其他并发请求修改；切源不能修改全局配置或共享可变过滤条件。

### 3.3 并发连接与验收

集合和现有主键方案保留。Web 服务生命周期采用 §4.7 的每轮执行独立构造、结束释放策略。当前固定 alias 且 close 会 disconnect，必须改为每个执行实例独立 alias，不能让一个请求关闭另一请求正在使用的连接；不能仅因不改分区键就认定连接管理无需调整。具体 Web 装配由 doc 2 §4.3 承接，回填项见 §6。

验收覆盖全部六个入口：两个源存在同名库、同名表时，A 会话的向量检索和精确回查都只返回 A 的知识；缺失来源被拒绝；指定源无知识时返回空/未就绪，不返回其他源数据；并发 A/B 请求不互相修改过滤条件或断开连接。

实库核验时目前只有一个真实登记源，三个集合共读取到 33 条结构、163 条维度值和 12 条 SQL 示例，均属于该源。实际向量对照中，当前源过滤返回正常结果，不存在的测试 source_id 返回空。上述单源对照不代替双源同名对象的正式验收，也不需要为此次文档更新写入测试数据。

## 4. Neo4j：直接实现 B 多源共存

### 4.1 已确认的实施方向

B 是正式目标，不是先完成 A 后才能进入的第二阶段。MetaWeave 负责生成和发布按源独立的子图，NL2SQL 按同一 source_id 检索和寻路；两端完成后再做多源验收。

单独添加 source_id 属性不足以隔离节点，必须同时改变 MERGE 身份和现有唯一约束。本次采用新增 entity_id 的方式，保留原业务 id 和 full_name，避免把带源 UUID 的实体键直接用作 SQL 表名。

### 4.2 Table 与 Column 的属性和 MERGE 身份

**Column 必须新增独立的 `source_id` 属性，与 entity_id 同批实现。** entity_id 负责区分实体，source_id 负责源归属、过滤、删除和统计，不要求每次从 entity_id 字符串解析来源。

| 对象 | 保留属性 | 源归属 | 新实体身份 |
| --- | --- | --- | --- |
| Table | `id = db.schema.table`、`full_name = schema.table` | source_id；沿用 publish_id | `entity_id = source_id:id` |
| Column | `id = db.schema.table.column`、`full_name = schema.table.column` | **新增 source_id**；同批增加 publish_id | `entity_id = source_id:id` |

writer 中的“合并”指 Cypher MERGE：匹配已有实体则复用，没有则创建；不是把两个数据源的节点合成一个。目标按实体键执行：

```cypher
MERGE (t:Table {entity_id: row.entity_id})
SET t.source_id = row.source_id
```

Column 同样按 entity_id MERGE。其他业务属性仍由 SET 写入。必须校验实体键的源前缀与 source_id 一致。

实体键拼接采用同一归一契约：source_id 使用规范化 UUID；Table 的逻辑 id 必须与 NL2SQL `catalog.normalize_table_id(...).key` 一致，即由当前源的数据库名前缀、schema、table 构成三段键；Column 再追加列名。MetaWeave 生成、一次性回填和 NL2SQL 查询应遵守同一格式，不能一侧使用裸 `schema.table`、另一侧使用 `db.schema.table`，也不能自行统一大小写。两模块分别实现并用同一组输入输出样例验证即可，无需增加跨模块依赖。

本期对键空间采用“数据库名前缀单独校验，schema/表/列保留普通标识符范围”的小范围扩展：

| 部分 | 支持范围与归一规则 |
| --- | --- |
| db 前缀 | 使用选定源登记的 db_name，作为不透明的逻辑前缀；按下述统一判定式校验，允许内部普通空格、连字符、数字开头，原样保留大小写和内部字符 |
| schema、table、column | 本期范围为 `[A-Za-z_][A-Za-z0-9_]*`；保留实际名称大小写，不做转小写、字符替换或带引号名称的重新解释 |
| 两段表输入 | 将 `schema.table` 补为当前源的 `db_name.schema.table` |
| 三段表输入 | 首段必须与当前源登记的 db_name 完全相同，后两段按上述范围校验 |

数据库名前缀的统一判定式为：`name 是字符串 ∧ name 非空 ∧ name == name.strip() ∧ name 不含 "." ∧ 每个字符的 Unicode 类别均不为 Cc`。这里 Cc 指控制字符；内部普通空格允许，首尾空白和制表符等控制字符拒绝，不通过自动 strip 修正登记库名。MetaWeave 发布预检、迁移预检、NL2SQL 归一及验收使用同一判定式。共同样例包括 `my orders`、`my-orders`、`2026orders` 通过；空串、首尾空格、含点号或控制字符的名称拒绝。

当前 NL2SQL 对三段全部使用普通标识符正则，MetaWeave reader 则原样构造 id，存在不一致。实现时需调整 `normalize_table_id` 对首段的校验，并在 MetaWeave 相关知识发布预检、迁移预检和 NL2SQL 运行配置构造时校验各自涉及的键；不支持的对象须明确报出名称和规则，在删除/写入知识之前拒绝本次发布（整源粒度，以当前源本次发布的完整对象集合为单位），不能仅拒绝或静默跳过个别非法对象，也不改变名称。此边界不限制平台登记和元数据采集的一般能力，但相关对象不在本期 NL2SQL 知识接入范围内；扩展任意带引号标识符的解析不纳入此次改造。

例如，登记 db_name 为 `my-orders` 时，输入 `public.orders` 应得到 `my-orders.public.orders`，再拼接为 `<source_id>:my-orders.public.orders`；`2026orders` 同样可用。验收覆盖这两类前缀、两段/三段输入一致性、错误源前缀，以及含点号的库名或含连字符的 schema/表名被明确拒绝。当前工具层会捕获 `InvalidTableIdError` 并返回明确的非法标识错误，而 `table_not_in_graph` 表示合法键在当前源图中不存在，两者不能混为一类。

HAS_COLUMN 和 JOIN_ON 的端点使用实体键匹配，两端必须属于同一个 source_id。本文采用以下关系写入方式，完整与分步 writer 保持一致：

| 关系 | MERGE 身份 | 匹配或新建后写入的归属属性 |
| --- | --- | --- |
| HAS_COLUMN | 按 entity_id 定位 Table、Column 后，按两个端点和关系类型 MERGE，不把 source_id 放进 MERGE 属性模式 | SET source_id、publish_id |
| JOIN_ON | 按 entity_id 定位两个 Table 后，保留端点之间按 relationship_id MERGE 的方式 | 在现有业务属性 SET 中增加 source_id、publish_id |

source_id、publish_id 均由本次发布的服务端上下文提供；publish_id 表示该节点或关系对应的图内容发布编号，不是关系身份的一部分，也不单独证明 PostgreSQL 发布记录已提交。旧图回填时沿用所属 Table 的有效发布归属，无法确认或端点归属冲突时中止迁移，不伪造新发布编号。

实体端点和禁止跨源边约束负责关系隔离；边上的 source_id 便于直接过滤、统计、删除范围检查和审计，不替代端点校验。普通按源删除仍按 §4.4 删除节点并带走关联边，不额外引入一套边删除流程。原 relationship_id 保留为端点之间的业务标识，无需新增边的全局唯一约束或重写关系发现/评分算法；已存在边的非空来源与端点冲突时应报错，不能通过 SET 静默掩盖。

### 4.3 唯一约束和现有单源数据迁移

实库已核实存在四个全局唯一约束：Table.id、Table.full_name、Column.id、Column.full_name。它们不允许不同源保存同名对象，必须迁移为与新实体身份匹配的约束，目标为 Table.entity_id 和 Column.entity_id 分别唯一；旧 id/full_name 保留但不再全局唯一。

迁移与生成脚本必须同步处理以下事项：

- 一次性迁移必须显式删除旧四个约束：`table_id`、`table_full_name`、`column_id`、`column_full_name`。仅新增 entity_id 约束不会解除旧约束对同名对象的限制。
- 新约束建议命名为 `table_entity_id`、`column_entity_id`，分别约束 Table.entity_id、Column.entity_id。`CREATE CONSTRAINT ... IF NOT EXISTS` 不会修改已有约束：同名约束存在时，即使属性定义不同，也可能跳过创建。正确删除旧约束后复用旧名在技术上可行，但本方案推荐新名以明确语义。参见 [Neo4j 约束创建说明](https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/#create-a-constraint-if-not-exist)（本轮已核对页面及实际 HTML 锚点）。
- [writer.py](../metaweave/metaweave/core/cql_generator/writer.py) 的 `_write_constraints` 分步脚本和 `_write_import_all` 完整脚本必须同批更新，仅生成新的实体键唯一约束，避免普通发布重新创建旧的 id/full_name 全局唯一约束。迁移后核对实际约束定义，不能只凭脚本未报错判断成功。

实库为 8 个 Table、25 个 Column、7 条 JOIN_ON，属于一个来源。补充只读核验确认：25 个 Column 均有唯一所属 Table，可由该表回填 source_id；7 条 JOIN_ON 的端点来源一致。迁移可据此回填实体键、列和关系来源；迁移时仍须再次验证归属，不按 db_name 猜测 source_id。

**在默认保留旧图的迁移路线下，必须先完成旧 Column 的 source_id 回填，再启用按源删除。** 否则旧列没有来源，删除表后会遗留无法按源识别的孤儿列。约束迁移和数据回填属于一次性维护，不混入每次普通发布，也不在本次文档更新中执行。维护式全清后重建的替代路径及其代价见 §4.8。

### 4.4 按源删除约束（已确认）

1. 普通数据源发布不再调用全库 `MATCH (n) DETACH DELETE n`。全库清空只属于专门维护操作，不属于普通发布。
2. 删除目标必须是后端已验证的本次 source_id；source_id 缺失、空白或非法时拒绝发布，不退回全库清空或按库名删除。
3. 删除范围只包含该 source_id 的 Table、Column 及其关联关系，其他源的节点和关系必须保留。
4. Table、Column、HAS_COLUMN、JOIN_ON 均不得跨源共享或建边。若发现跨源边，应拒绝替换并报告归属异常，避免 DETACH DELETE 顺带删除其他源的关系。
5. 孤儿清理同样限定当前源，不执行全图孤儿清理；列归属缺失应先迁移修复。
6. 替换后按当前 source_id 核验 Table、Column、JOIN_ON、HAS_COLUMN 数量与归属，不用全图数量验收某一个源。

下面仅说明删除的目标范围，不是本次已执行的命令，也不是包含全部检查的最终加载实现：

```cypher
MATCH (n)
WHERE (n:Table OR n:Column) AND n.source_id = $source_id
DETACH DELETE n
```

DETACH DELETE 同时删除目标节点的关联边，因此上述“禁止跨源边”是执行前提。按源删除和 entity_id 身份是配套约束，不能只改删除语句而继续按全局 full_name 合并。

### 4.5 transaction_mode 与删除的区别

**transaction_mode 不决定删除什么，也不决定是否按 source_id 删除。** 删除范围由删除查询决定；transaction_mode 是现有加载器控制后续 CQL 导入如何提交的配置，本次没有新增或修改它。

当前 [metadata_config.yaml](../metaweave/configs/metadata_config.yaml) 的 `loaders.cql_loader.options.transaction_mode` 为 `by_section`，加载器提供 by_section 和 single 两个分支。其当前执行顺序为：

```text
clean=True：先清空整库并单独提交
                    ↓
transaction_mode 决定后续导入方式
  by_section：分章节/语句导入，分次提交
  single：拼接多条顶层语句，作为一次查询提交（当前实现不可用）
                    ↓
加载结束后再做数量核验
```

所以即使改为 single，当前代码的删除也不会自动加入导入事务；写入失败时，已经单独提交的删除不能回滚。将删除范围改为 source_id 只解决“删除哪些数据”，仍需另行决定“删除与新数据写入何时一起提交”。

此外，现有 `_load_single_transaction` 将各章节拼接成字符串，再通过 `execute_write_transaction` 内的一次 `tx.run()` 提交。生成内容包含分号分隔的多条顶层语句，Neo4j 不接受这种单次查询。审核时对实际 Neo4j 仅执行只读对照：一次提交 `RETURN 1 AS a; RETURN 2 AS b` 返回 `Expected exactly one statement per query but got: 2`；同一个事务内分别调用两次 `tx.run()` 则成功。因此不能直接切换 `transaction_mode: single` 来实现 B 的原子替换。

**建议的 B 发布事务边界**为：

```text
开始一个 Neo4j 数据事务
  → 删除当前源旧子图
  → 写入当前源新 Table、Column 和关系
  → 按源核验数量与归属
全部成功：一起提交
任一步失败：一起回滚，当前源旧子图保留
```

该建议需要改加载器的事务组织，不能只改 YAML，也不需要用户通过 by_section/single 决定删除范围。应在同一个数据事务回调中分别执行删除、各项写入和核验语句，消费结果并检查核验结果后再提交；可以逐条或分批执行，但不能在章节之间提前提交，也不能把多条顶层语句拼成一次 `tx.run()`。一次性约束迁移与这类发布数据事务分别处理。

Neo4j 与 PostgreSQL publish_info 也不是同一事务。发布服务必须区分图事务回滚与图已提交后发布记录失败，不能继续沿用“调用过 load 就撤销旧 current”的全清假设。具体跨库失败处理和最终配置整理仍待详细设计。

### 4.6 MetaWeave 改动与验收范围

- CQL models/reader/generator/writer：贯通 source_id、entity_id 和端点身份，完整/分步脚本保持一致。
- CQL loader：移除普通发布全清路径，按源替换、按源统计，并实现选定的数据事务边界。
- 发布服务：向加载器传入可信来源，调整发布成功/失败及 current 记录处理。
- 一次性迁移：回填现有节点和关系、替换旧约束，保留逻辑表键。
- 测试：双源同名库/表/列共存；重新发布 B 不改变 A；B 发布失败回滚；非法来源不删除；来源缺失/跨源边拒绝；按源数量核验。
- 并发发布验收：A、B 同时发布各自源的同名库/表/列，最终子图内容、数量和发布归属均正确；A 提交而 B 失败回滚时，A 的新图和 B 的旧图均保留。现有平台库锁按 source_id 锁登记行，只串行化同一源发布，不能据此假定不同源的 Neo4j 事务不会交错。允许数据库正常锁等待，但不得出现互相删除、节点合并或发布状态串源。

双源验收前置准备：在隔离的验收平台库、Milvus 和 Neo4j 测试库中登记两个不同 UUID 的测试源，准备同名 db/schema/table/column，以及可区分的知识和查询结果、仅 B 存在的表和关系。通过 MetaWeave 为两源生成各自归属正确且可发布的作业产物，保留用于失败回滚对照的旧子图，并安排可控的图事务内失败注入。目前单源实库不足以完成这些用例，不能用重复查询同一源代替；此次文档更新不创建第二源或写入测试数据。

在本次 writer/loader 改造涉及的写入路径中，可顺手采用 `UNWIND $rows AS row` 参数化传入数据，减少内联 JSON 的引号修补。此项属于局部实现建议，不作为独立前置交付，不扩展为全仓 CQL 参数化重构。

### 4.7 NL2SQL 图查询改动与验收范围

**B 的交付范围必须包含 NL2SQL 图查询侧，不能只修改 MetaWeave。** 保留逻辑 id 后，不同源的 Table 可以拥有相同的 `db.schema.table`。当前 [join_graph.py](../nl2sql/src/modules/db_deepagent/services/join_graph.py) 只按 id 定位节点，会在多源共存时产生错误匹配。

按 §5 建立服务端会话绑定后，图查询服务使用该 source_id 与归一后的逻辑表 id 构造 `entity_id = source_id:id`；必须使用 `normalize_table_id` 返回的 `TableKey.key`，不能直接拼接模型输入的原始表名（归一契约见 §4.2）。统一调整以下三个入口：

| 方法 | 必须完成的改动 |
| --- | --- |
| check_table_exists | 按 Table.entity_id 检查存在性，不能因其他源存在同名表就返回 true |
| _find_path_apoc_dijkstra | 起点和终点均按当前源的 entity_id 定位 |
| _find_path_shortest | 起点和终点均按当前源的 entity_id 定位 |

source_id 的注入点固定在服务端构造会话服务的入口：会话启动层读取并校验持久化的会话绑定，将 source_id 传入 `build_services`，由其构造来源不可变的 JoinGraphService 和 Milvus 检索服务，再将 services 注入 Runner；Runner 每轮再次核对请求、会话记录与服务绑定一致，不在已有服务上切换来源。不同来源不能共享可变的服务绑定。[schema_tools.py](../nl2sql/src/modules/db_deepagent/tools/schema_tools.py) 的 `plan_joins` 通过工具构造时闭包持有的 services 调用 `find_join_path`，后者内部调用 `check_table_exists`，两者均使用同一服务绑定。模型只提供表名和现有规划参数，**source_id 不作为模型可填写的工具参数**，也不从模型提供的实体键解析来源。

按源运行配置必须在 `build_services` 之前构造：`business_db.database`、`expected_database`、`table_key_prefix` 均取已验证的选定源登记 db_name，并与真实连接的 `current_database()` 核对；不得沿用 YAML 当前固定的 `orders` 前缀。`selected_schemas` 同时限制允许访问的 schema。此项由 doc 2 §5.3 的源配置装配承接，并按 §4.2 的键空间规则校验；只传 source_id 而不更新 table_key_prefix 不算完成改造。

Web 首版采用**每轮提问执行独立构造 DbAgentServices，执行结束释放**，不做按源服务缓存：读取会话绑定后构造，Runner 执行及归档结束后在 finally 中 close；流式请求应覆盖整个后台执行生命周期，不能在返回流对象时提前释放，也不能在客户端断开而后台仍使用服务时先行关闭。构造部分失败也须释放已创建资源。Milvus alias 必须包含本轮唯一执行标识，同源并发请求也不能共用；close 只释放本实例的连接与驱动。预算等可变执行状态不跨请求共享，会话连续性由已有 Store/checkpoint 保持，历史列表等只读接口不构造整套查询服务。该方案接受每轮建立连接的开销，后续有性能证据再单独优化；CLI chat 可以在同一固定来源会话中复用，退出时释放。具体 Web 装配及异常、取消清理由 doc 2 §4.3 承接。

CLI 的查询会话也必须在构造服务前显式接收并验证平台 source_id，使用相同的服务绑定，不自动选择唯一源或回退为全图查询。[cli.py](../nl2sql/src/modules/db_deepagent/cli.py) 另有直接查询全库 Table/JOIN_ON 数量的 doctor 诊断：全库统计可以保留为明确标注的运维信息，但不能用它判断指定源知识已就绪，也不能把它的结果用作会话寻路数据。

可选辅助交付：为 doctor 增加 `--source-id`，校验登记来源后只读统计该源的 Milvus 三集合记录和 Neo4j Table、Column、HAS_COLUMN、JOIN_ON 数量，沿用现有 JSON 输出方式。该检查不生成 Embedding、不调用模型或执行业务 SQL；来源非法或依赖不可用须明确失败。数量为零时区分未发布、空集合与查询失败，不用“非零”替代完整就绪验收。无 source-id 时的全局诊断仍标注为运维统计。此项是 doc 2 §9.1 自动化检查的辅助工具，不阻塞隔离改造，也不代替 §3.3、§4.6 和本节的双源行为验收。

来源缺失或绑定无效时拒绝图查询，不回退为仅按 id 匹配。寻路依赖 §4.4 的禁止跨源边约束；验收时确认路径节点和关系均属于当前源。返回给后续 SQL 流程的表标识继续使用原逻辑 id，不把带 UUID 的 entity_id 当作 SQL 表名，也无需改造关系发现/评分算法。

验收至少覆盖：双源同名表时，两种寻路方式都只使用当前源子图；某表仅存在于 B 源时，A 会话的存在性检查返回 false；当前源无路径时不能返回其他源内部的路径；工具参数模式不包含 source_id，模型不能覆盖服务端绑定。错误不仅可能表现为跨源路径，即使没有跨源边，旧查询也可能选中其他源内部的完整路径。

增加“知识前缀不进入 SQL 表引用”验收：对 db_name 为 `my-orders` 的源，知识键 `my-orders.public.orders` 应转换为 SQL 表引用 `public.orders`（或作用域内可正确解析的裸表名）；通过校验并执行的 SQL 不得将 db 前缀、source_id 或 entity_id 作为表的限定段。按表引用结构核验，不以全文是否出现相同字符串判断，避免误判注释、字面量或恰好同名的表。

正式多源验收以 MetaWeave 发布与 NL2SQL 查询的 B 完整链路为准，不以单源守卫代替。所有代码、配置和数据库迁移尚未执行。

### 4.8 代码改造完成后的迁移与重新发布

**代码改造和一次性迁移完成后，需要重新发布已有数据源的 `rel`，用新代码重新生成 CQL 并加载 Neo4j。** 这是本次落地的操作步骤，用于更新图库并验证新的发布链路；不能直接加载改造前生成的旧 CQL。即使完整回填可使现有节点兼容新身份，本次仍安排重新发布，以验收实际生成、替换和发布状态处理过程。

本方案默认采用“保留旧图并回填 → 迁移约束 → 按源事务重新发布”。回填的价值是让旧图兼容新身份和按源删除，并在首次新发布失败、图事务回滚时完整保留旧源子图。当前只有一个源，也存在更简单的“维护式全清并独立提交 → 迁移约束 → 新代码重新发布”路径，可跳过旧节点回填；但新发布失败后旧图已不存在，会留下空图，且仍须替换旧约束。该替代路径仅在确认图库无其他需保留数据、可接受停机和失败后恢复的维护场景使用，不作为本次默认执行路线，不能由执行者自行省略回填。

迁移停发是硬前提，不能只靠公告或低峰约定。此次采用一次性停机维护，不新增持久维护开关：停止全部 MetaWeave 后端实例及独立图发布/加载进程，确认在途发布已结束且维护期间无人启动 CLI 写图，再执行迁移；依赖该图的 NL2SQL 查询入口同步停用。仅关闭浏览器或前端按钮不能阻止 API/CLI 发布。否则误发布会删除已有 source_id 的旧 Table，却遗留尚未回填 source_id 的旧 Column。

在现有采集产物完整、来源归属明确且业务库结构没有变化的前提下，执行顺序为：

1. 进入上述停机维护窗口，确认发布进程退出、无在途图写入；按 §4.8.1 的顺序先停 Neo4j、完成 dump，再启动 Neo4j，确认备份成功后才可执行迁移脚本。部署修改后的代码，暂不启动普通发布和查询服务。
2. 按 §4.3 完成旧节点和关系的归属、实体键回填，删除旧四个唯一约束并建立新实体键唯一约束；核验迁移结果。旧 Column 的 source_id 必须先补齐，才能按源替换，避免遗留无来源的列。
3. 迁移核验通过后启动新版 MetaWeave 后端，在发布页面选择该数据源已有的成功采集作业，仅重新发布 `rel`。现有 [关系发布链路](../metaweave/backend/publishing/publish_service.py) 会读取该作业的 JSON、Rel 和 Domain 产物，生成 CQL 后加载 Neo4j；改造后须保留这一入口，并采用新的按源事务替换。通过 Web 发布不需要另行手动生成 CQL。
4. 核验当前源 Table、Column、HAS_COLUMN、JOIN_ON 的身份、归属和数量，以及其他源的数据未受影响；NL2SQL 后端改造完成后，再验证 Milvus 过滤、表存在性和两种寻路方式，然后恢复查询入口。迁移失败时保持停机，修复或恢复备份后重新核验，不能带着部分迁移状态开放发布。

这里的“重新发布 rel”是将已有关系产物重新生成图并写入 Neo4j，**不等于重新执行 rel 关系发现与评分**。DDL、JSON、MD、Domain、维度配置及 SQL 示例通常可以复用；只有产物缺失、失效或业务结构变化时，才补跑相应采集步骤。当前 Milvus 数据已有正确 source_id，本次查询过滤改造通常无需重新发布三类向量知识或重新生成 Embedding。

#### 4.8.1 一次性迁移的执行载体、备份与留档

本设计采用待开发的一次性脚本 `metaweave/scripts/migrate_neo4j_source_identity.py`，不新增维护子命令或通用迁移框架，也不以手工粘贴 Cypher 作为默认执行方式。脚本默认只读预检，显式 `--apply` 才执行迁移；本次只确定设计，不创建该脚本。

- **配置与连接**：从 MetaWeave 现有配置加载 `loaders.cql_loader.neo4j`，复用 `services.db.neo4j_connection.Neo4jConnectionManager`。通过其 session 在同一个数据事务中分别执行归属与实体键回填及核验；约束 DDL 单独分阶段处理，不把当前仅支持一条查询的 `execute_write_transaction` 当成多语句接口。脚本不写平台库、Milvus，不触发重新发布。
- **预检与执行**：记录节点、边、约束和来源分布；检查 Column 唯一所属 Table、边端点归属、已有实体键冲突及逻辑键归一一致性。apply 时再次预检，回填事务成功后再替换约束并复核；正确的已有值保留，冲突停止，不盲目覆盖。某阶段失败则报告失败阶段，保持应用停机，不能把部分成功视为迁移完成。
- **核验留档**：每次执行在 `metaweave/output/maintenance/neo4j_source_identity/<执行编号>/report.json` 保存模式、时间、目标数据库、备份归档位置、前后按源计数、约束定义、各项检查和阶段结果；控制台输出摘要和报告位置。失败也尽可能留档并返回非零退出码，不记录密码或完整连接串。报告与备份由维护执行者留存，属于运行产物，不提交 Git。
- **图库备份**：在 Neo4j 实际宿主机或拥有其数据卷的维护容器中，使用与服务版本匹配的 `neo4j-admin database dump` 离线备份目标图库，记录 dump 位置、工具版本、退出状态与文件校验值。停止应用写入不等于数据库离线：先停止 Neo4j 实例，执行 dump，成功后启动 Neo4j，应用入口仍保持停用，再运行迁移脚本。备份工具不能通过远程 Bolt 连接完成此操作；恢复也使用相应版本的 `neo4j-admin database load`，迁移脚本不自行停止数据库或恢复归档。参见 [Neo4j 离线备份说明](https://neo4j.com/docs/operations-manual/current/backup-restore/offline-backup/)。

迁移与 §4.5 loader 发布使用相同的事务写法：session 的事务回调内多次 `tx.run()`，消费结果、核验后统一提交。两者可以复用写法和事务测试样例，不要求抽象通用框架，也不规定迁移脚本必须早于 loader 实现；两项都完成并验证后才能执行正式迁移和重发布。脚本本身可供后续维护验证参考，但此前只读 RETURN 对照仅证明单次查询的语句限制，不代替真实迁移或发布回滚测试。

## 5. source_id 全链路传递（方向已确认）

多源情况下完成正确查询的前提是：**前端把 source_id 参数带过去，且全链路绑定**。

### 5.1 前端选源与请求参数

- 选源列表复用 MetaWeave 现有接口（doc 2 §5.1 已确认复用 `/api/data-sources` 列表与详情）；列表项展示 db_name 或 display_name，实际取值绑定 source_id。
- NL2SQL 的 v2 请求（doc 2 §4.2，全部尚未实现）需携带 source_id：创建会话与提交问题时都必须传；提问请求形如 `{session_id, question, source_id}`。具体字段名以 doc 2 回填为准（见 §6）。

### 5.2 会话与 source_id 绑定

- 会话创建即绑定 source_id：[会话记录](../nl2sql/src/modules/db_deepagent/persistence/archive.py) 的 `session_record` 当前没有 source_id/归属字段，需增加并在 `upsert_session` 整体覆盖时保留该值。
- [Runner](../nl2sql/src/modules/db_deepagent/runner.py) 的 `run()` 增加 source_id 参数，贯穿检索与图查询；thread_id 格式（`dbda:<session_id>:<seq>`）不必改，checkpoint/线程经会话间接归属到源。
- 续聊请求校验 source_id 与会话记录一致，不一致明确拒绝（防止用 A 源的会话发 B 源的问题导致串源）。建议切换数据源即新建会话（待确认，见 §7）。

### 5.3 执行与存储归属

- 业务库连接按 source_id 从 `db_source_info` 取登记信息并解密（doc 2 的 D6 决策，解密只在后端），运行配置来自选定数据源（doc 2 §5.3）。
- 注意区分两个标识：`source_id` 是平台登记 UUID；`datasource_id` 是 BusinessDbService 的连接指纹（`pg:` + 连接参数摘要），用于结果元数据。两者不是一回事。建议结果 meta.json 在连接指纹之外补充 source_id 归属字段，便于按源统计与清理；该增补应与 doc 3 的结果元数据设计合并考虑，不单独实施。
- 结果读取已有按 session 的访问校验；source 维度经会话绑定间接获得，无需在 read_rows 再加一层。

## 6. 与 doc 2 / doc 3 的关系与回填点

- 本文档细化 doc 2 §5.4「知识检索隔离是前置交付」的落地方式，不改变其 D0–D3、D6 决策。
- B 方向已确认，doc 2 回填待安排：§4.2 的 v2 API 补 source_id 字段；§4.3 承接本文 §4.7 每轮独立服务、唯一 Milvus alias、流式执行与异常清理的装配要求；§5.3 明确 database/expected_database/table_key_prefix 按源注入与本文 §4.2 键空间校验；§5.4 增加指向本文档的引用；§9.1 验收补「双源同名库不串源」、键空间边界、双源测试环境准备及可选 doctor 按源诊断。回填前 doc 2 保持原样。
- doc 3 的结果存储改造与本改造相互独立、可并行；涉及结果元数据增补 source_id 时合并设计。

## 7. 待确认事项

1. B 已确认直接实施；Table/Column entity_id、Column.source_id 和按源删除已确认。需继续明确实现排期及事务边界的最终方案。
2. Neo4j 图已提交、PostgreSQL 发布记录提交失败时的处理细节仍待明确。关系字段写入机制见 §4.2；迁移顺序和停机要求见 §4.8，一次性脚本、连接复用、备份和报告载体见 §4.8.1；Web 服务生命周期采用 §4.7 的首版方案，由 doc 2 §4.3 承接装配。这些已补充的设计随本文档整体审核，审核通过前不开发。
3. 切换数据源是否强制新建会话（建议：是，且旧会话在界面标注所属源）。
4. display_name 唯一性曾提及考虑，尚未决定；它只影响界面辨识度，不影响本方案的隔离机制。
5. doc 2 的 source_id 字段回填时机。

## 8. 本次范围

本文档已记录直接实施 B、两个后端的改造边界、发布与迁移、服务生命周期和验收准备。本轮明确数据库名前缀的统一判定式、内部空格与首尾空白的边界、非法对象阻止本次整源发布的粒度，并补充知识前缀不进入 SQL 表引用的验收。文档整体审核通过前不编写实现代码。未创建迁移脚本或实现 doctor 参数，未修改业务代码、配置、数据库、Milvus/Neo4j 数据，未修改 doc 2/3，未执行 Git 暂存或提交。
