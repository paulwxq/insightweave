# 表列 Schema 向量检索参数改造方案

> 状态：设计稿，待审核；本文件不代表代码已实施。依据为当前代码。下文的 `K_table`、`K_column`、`K_backfill` 和对应阈值均指运行时从 YAML 读取的值，不固定业务取值。

## 一、目标与范围

对 `table_schema_embeddings` 的表级检索、列级检索和 `role="column"` 维度文本回填分别配置数量上限与相似度阈值，三条路径互不共用业务控制参数。其中后两条路径都调用 `search_columns()`，但由调用方传入各自的参数。Milvus 搜索各自直接使用配置的 `top_k` 作为 `limit`，再过滤相似度；允许实际命中数少于 `limit`，包括零命中。保持与当前历史 SQL 案例检索相同的“先限制候选数量、再按分数过滤”语义。

本期只改表、列 Schema 检索及其配置接线、相关测试。`sql_example_embeddings` 使用的 `sql_embedding_top_k` / `sql_similarity_threshold`，以及 `dim_value_embeddings` 的检索参数和实现，均不在本次改造范围。原有 `topk_tables`、`topk_columns` 分别改名为 `table_schema_top_k`、`column_schema_top_k`；另给 column 维度回填新增独立的 `column_backfill_top_k`。三个数量参数的初始 YAML 值均为当前相关检索使用的 `10`，不因拆分调整初始检索规模。

## 二、当前代码核对

| 路径 | 调用点 | 当前数量 | 当前相似度 | Milvus 表达式 |
|---|---|---|---|---|
| 表语义检索 | `retriever.py:155-159` | `topk_tables` | 共用 `similarity_threshold` | `object_type == "table"` |
| 列语义检索 | `retriever.py:179-183` | `topk_columns` | 共用 `similarity_threshold` | `object_type == "column"` |
| column 维度文本回填 | `retriever.py:834-891`，每个去重后的 `role="column"` 文本各调用一次 `search_columns()` | **每次** `topk_columns` | 共用 `similarity_threshold` | `object_type == "column"` |

参数读取位于 `src/tools/schema_retrieval/retriever.py:37-44`，YAML 定义位于 `src/modules/sql_generation/config/sql_generation_subgraph.yaml:30-34`。`MilvusSearchAdapter.search_tables()`（`milvus_adapter.py:119-186`）和 `search_columns()`（`:188-239`）均请求 `limit=top_k * 2`，使用原始 `hit.distance` 比较阈值，累计 `top_k` 条合格结果后 `break`。`metric_type: COSINE`、HNSW `ef: 100` 来自同一 YAML 的 `milvus_search_params` 配置块（`:103-106`）。仓库当前 `src/configs/config.yaml` 配置 `vector_database.active: milvus`；工厂仍支持 pgvector。

本方案的行号以当前工作区为准；实施前按函数名和配置键复核定位，代码变动后不要把旧行号当作契约。

## 三、目标契约与参数传递

在 `schema_retrieval` 段使用以下六个配置键；三项阈值均以现有 `0.45` 作为初始 YAML 值，避免仅因拆分参数改变当前默认分数门槛，后续可独立调节：

| YAML 参数 | 作用 | retriever 传递路径 |
|---|---|---|
| `table_schema_top_k` | 单次表向量查询的候选上限 `K_table`，替代 `topk_tables` | `search_tables(top_k=...)` |
| `table_schema_similarity_threshold` | 表候选的最低相似度 `T_table` | `search_tables(similarity_threshold=...)` |
| `column_schema_top_k` | 主列级检索的单次候选上限 `K_column`，替代该路径的 `topk_columns` | `retrieve()` 中的 `search_columns(top_k=...)` |
| `column_schema_similarity_threshold` | 主列级检索的最低相似度 `T_column` | `retrieve()` 中的 `search_columns(similarity_threshold=...)` |
| `column_backfill_top_k` | column 维度回填的单次候选上限 `K_backfill`；对**每个去重后的** `role="column"` 文本独立使用一次 | `_retrieve_column_dimension_hits()` 中的 `search_columns(top_k=...)` |
| `column_backfill_similarity_threshold` | column 维度回填的最低相似度 `T_backfill` | `_retrieve_column_dimension_hits()` 中的 `search_columns(similarity_threshold=...)` |

YAML 中要直接写清三个数量参数各自的查询路径与对象，建议使用下列注释。这里的“返回”指 Milvus 向量查询**在相似度阈值过滤前**返回的候选；过滤后的条数可能更少。三组参数都只控制 `table_schema_embeddings` 中相应的表或列记录，不控制历史 SQL 或维度值 collection：

```yaml
schema_retrieval:
  # table_schema_embeddings：表记录（object_type="table"）单次向量查询最多返回的候选数；Milvus limit 使用此值
  table_schema_top_k: 10
  # table_schema_embeddings：上述表候选在返回后须达到的最低相似度，决定哪些表命中参与候选表集合
  table_schema_similarity_threshold: 0.45
  # table_schema_embeddings：主列级检索查询列记录（object_type="column"）时，
  # 单次最多返回的候选数；Milvus limit 使用此值，不控制 column 维度回填
  column_schema_top_k: 10
  # table_schema_embeddings：主列级检索返回的列候选须达到的最低相似度
  column_schema_similarity_threshold: 0.45
  # table_schema_embeddings：column 维度回填按每个去重后的 role="column" 文本查询列记录，
  # 每次查询最多返回的候选数；Milvus limit 使用此值，不控制主列级检索
  column_backfill_top_k: 10
  # table_schema_embeddings：column 维度回填每次查询返回的列候选须达到的最低相似度
  column_backfill_similarity_threshold: 0.45
```

删除三个旧配置键 `schema_retrieval.topk_tables`、`topk_columns`、`similarity_threshold`，不从旧键隐式回退，避免旧配置继续生效而掩盖迁移遗漏。适配器接口中的通用形参 `top_k` 与 `similarity_threshold` 无需改名：表调用传 `K_table` / `T_table`，主列调用传 `K_column` / `T_column`，column 维度回填调用传 `K_backfill` / `T_backfill`。两条列路径仍共用 `search_columns()` 实现，但不共用传入值；pgvector 适配器也通过现有形参接收各次调用的数值，不需为配置拆分改动其 SQL。代码在新键缺项时可沿用当前 `retrieval_config.get(..., 10)`、`retrieval_config.get(..., 0.45)` 的默认策略；YAML 显式配置六项并给出注释。`schema_retrieval.sql_embedding_top_k`、`sql_similarity_threshold` 是独立的历史 SQL 参数，不受本次改名影响。

单次 Milvus 表查询的处理为：`object_type="table"` 限定对象类型 → `limit=K_table` 最多返回 `K_table` 个相似候选 → Python 保留原始 COSINE 分数 `>= T_table` 的命中。主列级检索对应 `object_type="column"`、`limit=K_column`、分数 `>= T_column`；column 维度回填的**每次**列查询对应 `object_type="column"`、`limit=K_backfill`、分数 `>= T_backfill`。三条路径分别返回筛选后的列表，删除“凑够 top_k 后退出”的 `break`；单次结果数满足 `0 ≤ 返回数 ≤ Milvus 实际候选数 ≤ 对应 K`。阈值筛掉的记录不补位，这是预期行为。保留现有的字段映射和分数规范化；阈值判断仍在规范化之前进行。

Milvus 的结果是向量搜索的排序结果，但若实际索引是近似索引，改变 `limit` 可能改变本次找到的候选；不能承诺新旧两次查询逐条完全一致，也不能把 `2×K` 视为“过滤后补足 K 条”的保证。验收以传入的 `limit`、实际返回数量及阈值为准。pgvector 路径的 `PGClient.search_semantic_tables()` / `search_semantic_columns()` 已在 SQL 中先按阈值过滤、按向量距离排序，再 `LIMIT top_k`（`src/services/db/pg_client.py:53-70`、`:96-113`）；内部步骤不改，对外仍是最多 K 条且每条达标。

## 四、文件与代码改动

1. **`src/modules/sql_generation/config/sql_generation_subgraph.yaml`**：把 `topk_tables: 10`、`topk_columns: 10` 分别替换为 `table_schema_top_k: 10`、`column_schema_top_k: 10`，新增 `column_backfill_top_k: 10`；把 `similarity_threshold: 0.45` 替换成 `table_schema_similarity_threshold: 0.45`、`column_schema_similarity_threshold: 0.45` 和 `column_backfill_similarity_threshold: 0.45`。按 §三给六项参数写明用途，尤其要在三个 Top-K 参数旁明确标注它们分别限制 `table_schema_embeddings` 表级检索、主列级检索、column 维度回填的**单次查询**候选返回数。主列与回填的注释须明确两组参数互不共用，不得只写含糊的“列 Top-K”。改正只写 `system.sem_object_vec` 的过时口径；pgvector 路径仍使用 `system.sem_object_vec`。注释明示“Milvus `limit=K` 后再过滤，合格命中可能少于 K”。当前 YAML 的 `ef: 100` 行（`:106`）已补“应 >= 上述各 top_k”注释；实施时保留该注释即可，不需再改代码、增加参数或调整 `ef` 值。
2. **`src/tools/schema_retrieval/retriever.py`**：在 `__init__()` 从六个新键读取数量和阈值，内部属性相应改为 `self.table_schema_top_k`、`self.column_schema_top_k`、`self.column_backfill_top_k`、`self.table_schema_similarity_threshold`、`self.column_schema_similarity_threshold`、`self.column_backfill_similarity_threshold`，删除旧的 `self.topk_tables`、`self.topk_columns`、`self.similarity_threshold`；更新 `retrieve()` 的参数 DEBUG 日志，使三条路径的数量与阈值可区分，并更新表、主列两处调用（`:151-182`）；`_retrieve_column_dimension_hits()` 的 `search_columns()`（`:878-882`）独立传入回填数量和回填阈值。避免误用主列参数控制回填。
3. **`src/services/vector_adapter/milvus_adapter.py`**：在 `search_tables()` 与 `search_columns()` 中分别将 `limit=top_k * 2` 改为 `limit=top_k`，日志中的 `limit` 随实际请求同步；保留对象类型表达式、`output_fields`、原始分数阈值判断和返回形状；删除两个 `if len(matches) >= top_k: break`。`search_dim_values()` 自身仍可能使用 `2×top_k`，它访问的是另一 collection，本期不触碰。
4. **测试**：更新 `tests/unit/vector_adapter/test_milvus_adapter.py`，用真实适配器 + mock `Collection.search()` 分别断言表、列请求 `limit=K`、表达式与阈值筛选，覆盖候选中部分低于阈值、全部低于阈值、边界分数等于阈值。更新 `src/tests/unit/tools/test_column_dimension_retrieval.py` 的配置 fixture，断言 column 回填调用传入 `column_backfill_top_k` / `column_backfill_similarity_threshold`；更新 `tests/unit/tools/test_retriever_wiring.py` 绕过 `__init__` 的实例桩字段；同步修改 `src/tests/integration/sql_generation_subgraph/test_subgraph.py:62-63` 的旧数量配置键。`src/tests/unit/subgraph/test_question_parsing.py:416` 也构造 `SchemaRetriever`，但其 `_MINIMAL_RETRIEVER_CONFIG`（`:389-397`）没有数量或相似度键，按本方案保留的新键默认值读取逻辑，该测试**无需改动**。增加 retriever 接线测试，故意给表级、主列级、column 回填设置**彼此不同**的数量与阈值，断言三处调用分别使用本路径的参数；若有多个 `role="column"` 文本，断言每次回填调用均使用回填参数，且不把 `column_backfill_top_k` 误当作全部回填命中的总上限。只调整受本次改造影响的旧测试数据与断言。

`BaseVectorSearchAdapter`、`PgVectorSearchAdapter` 与 `PGClient` 的接口继续使用通用 `similarity_threshold` 形参；配置拆分由调用方负责，不额外添加适配器参数。`sql_generation.py` 的 prompt 组装没有新数量截断或新相似度判断，不需因本次改造修改。

## 五、检索命中与 prompt 的边界：其他实际影响因素

这里需要修正一个容易误读的说法：**“向量命中通过阈值”表示该命中可参与候选表集合，不保证每条表或列命中都作为独立条目出现在 prompt。** 当前 `schema_context` 只带表卡片、表分类、JOIN 计划等结果；列命中的 `object_id` 不直接写入 prompt。列命中通过 `parent_id` 带入父表，多个同表列会去重（`retriever.py:462-545`）。`fetch_table_cards()` 按候选表及 JOIN 计划涉及表做精确查询（`:67-94`、`:272-274`），表卡片 formatter 才生成“表结构”段（`sql_generation.py:255-256`、`:408-414`）。若精确查询未取到某表，prompt 里也不会凭向量命中自动生成该表卡片。

除本期三组数量 / 阈值参数外，发现以下**实际生效**的相关配置和路径：

| 项目 | 对表列命中或 prompt 的影响 | 是否为额外的表/列向量相似度或数量过滤 |
|---|---|---|
| `schema_retrieval.milvus_search_params.metric_type`、`params.ef` | 决定 Milvus 向量检索度量与搜索参数；`ef` 会影响近似检索的召回和耗时，可能改变进入阈值判断的候选。表、列和其他 Milvus 向量搜索共用此配置。 | 否；不是另一道业务阈值 / prompt 截断 |
| `schema_retrieval.table_category_mapping` | 将命中的父表归入 fact / dimension / bridge，影响候选分组和 JOIN base 选择；桥接表不能作为 base，但可以参与 JOIN 并获取卡片。 | 否；不按分数删掉命中 |
| `schema_retrieval.similarity_gap_threshold` | 在存在事实表和维度表时，影响是否走“单维度表”优化（`retriever.py:719-740`），从而影响 JOIN 计划及其带入的表卡片。 | 否；不是表/列候选的二次阈值 |
| `schema_retrieval.join_max_hops`、`join_strategy` | 影响 Neo4j JOIN 路径；路径上的中间表也可能进入“表结构”段，即使它没有通过本次表/列向量阈值。 | 否；不是向量检索筛选 |
| `schema_retrieval.dim_index_topk`、`dim_value_min_score` | 控制**另一 collection** `dim_value_embeddings` 的命中；维度值的 `dim_table` 也能补入候选表（`retriever.py:414-429`、`:540-555`），因此表/列阈值不能作为 prompt 表卡片的唯一准入条件。 | 否；不作用于 `table_schema_embeddings` |

另有固定的代码规则：表、列检索分别以 `object_type` 限定记录类型；重复父表去重；JOIN 规划只选一个 base（`retriever.py:640`）；JOIN formatter 目前最多渲染 3 个计划（`src/tools/schema_retrieval/join_planner.py:96`）。这些不是新增 YAML 参数。当前 YAML 虽写有 `sql_generation.prompt.max_table_cards: 10`、`max_join_plans: 3` 和 `include_schema_hints`，在当前 Python 代码中没有读取它们的引用，**不能据此认定表卡片会按 `max_table_cards` 截断**；`max_join_plans` 也没有驱动前述写死的 `[:3]`。本期仅如实记录，不借这次改造顺手添加一层 prompt 过滤。

因此，这六项是**表/列向量候选筛选**的直接业务控制参数；最终 prompt 的表集合还会受维度值、分类、JOIN 路径和精确卡片查询影响。不能把“通过阈值的表/列命中数”等同于“prompt 中的表卡片数”，也不能保证表/列都零命中时 prompt 一定没有表结构。

## 六、验收标准

1. **三路接线测试**：在测试内构造三组彼此不同的数量与阈值（不写回正式 YAML），断言 `retrieve()` 的表调用使用 `table_schema_top_k` / 表阈值，主列调用使用 `column_schema_top_k` / 主列阈值，每个 column 维度文本回填调用使用 `column_backfill_top_k` / 回填阈值。取值必须可区分，否则传串参数也可能通过断言。

2. **正式 YAML 与旧键检查**：从实际 YAML 加载 `schema_retrieval`，断言六个新键都存在，三个 Top-K 的初始值均为 `10`，三个相似度阈值的初始值均为 `0.45`，且旧的 `topk_tables`、`topk_columns`、`similarity_threshold` 均不存在。人工核对 YAML 三个数量参数旁的注释均明确写出 `table_schema_embeddings`、对应的检索路径、`object_type` 及“过滤前的单次查询候选数”，两条列路径的注释不能互相混淆。实施后执行以下旧键检查；**两条命令均应无输出**（`rg` 无匹配时退出码为 1），不要把源码中适配器通用形参 `similarity_threshold` 或独立的 `sql_similarity_threshold` 误判为旧配置键。历史 SQL 的独立数量及阈值保持原状。

   ```bash
   rg -n 'topk_tables|topk_columns' src tests
   rg -n '^  similarity_threshold:' src/modules/sql_generation/config/sql_generation_subgraph.yaml
   ```

3. mock 表、列 Milvus 搜索分别验证 `limit` 恰为当次 `top_k`，`expr` 仍对应对象类型；给出有序候选分数如 `[0.9, 0.5, 0.4]`、阈值 `0.5`，返回前两条；阈值高于全部分数时返回空。断言没有 `2×K` 请求，也没有依赖过滤后补足 K 的行为。
4. 通过真实 retriever → mock 向量适配器的链路验证：列命中仍经父表进入候选表，重复父表去重；维度值和 JOIN 中间表允许按现有逻辑补入；最终 prompt 的“表结构”条目以实际 `table_cards` 为准，不用向量命中条数代替其数量。
5. 运行受影响的表列适配器、retriever 与 prompt 单测，执行 Ruff 和 `git diff --check`。不以真实服务恰好返回固定条数作为通用验收条件。

本方案待审核通过后再实施；本轮只更新设计文档。
