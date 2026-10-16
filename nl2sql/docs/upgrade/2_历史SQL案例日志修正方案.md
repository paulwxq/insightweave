# 2_历史 SQL 案例日志修正与检索简化方案

- 状态：v4 已实施；单元测试、固定输入验证与静态检查通过，真实服务端到端验证未执行
- 日期：2026-10-12
- 版本：v4。v1 的新增 prompt 相似度过滤已撤销；v3 已实施 Milvus 历史 SQL 两步筛选及日志修正；本版已删除 `sql_generation.prompt.max_similar_sqls`，取消 prompt 阶段的再次数量截断。
- 范围：保留已完成的 v1 回退、Milvus 两步筛选及 DEBUG 候选日志；补充删除 prompt 数量参数、调整 formatter 和数量摘要、更新相关验证。**历史 SQL 全链路仅保留原有数量与相似度两个参数，不新增配置项**。

---

## 一、背景

**原始问题（真实缺陷，本方案日志修正部分的目标）**：`sql_generation_node` 的日志

```python
qlog.info(f"使用 {len(similar_sqls)} 个历史 SQL 案例")
```

打印的是**检索返回数量**（经 `sql_similarity_threshold=0.6` 过滤后），而 `_format_similar_sqls` 内部还会按 `max_similar_sqls=2` 截断——截断无任何日志。2026-10-12 实测（q_be442260）日志称"使用 3 个"，实际 LLM 只看到 2 个，观测失真。

**v3 已修复的候选日志缺口**：原 `MilvusSearchAdapter.search_similar_sqls()` 没有打印 Milvus 原始返回的全部候选；`retriever.py:296` 的 DEBUG“检索到 N 个相似 SQL 案例”记录的是适配器过滤后返回数。v3 已按 §3.3 补充筛选前的全部候选明细及筛选后数量，最终案例明细通过完整 prompt 观察。

**v1 弯路记录（防止重蹈）**：v1 针对该问题提出"数量上限 + 新增相似度门槛 `similar_sql_min_similarity` 双层过滤"，新增配置、`select_similar_sqls` 助手函数及 6 个测试用例，并已实施。评审讨论确认属过度设计，理由：

1. `schema_context["similar_sqls"]` 的唯一功能性消费者就是 prompt 组装（全库 grep 证实；`retriever.py:961` 的 `similar_sql_count` 仅为遥测）。检索阈值 0.6 与 prompt 侧阈值无各自独立的职责边界；
2. 当 prompt 侧阈值 ≤ 检索侧阈值时，prompt 侧相似度过滤没有额外作用；当它高于检索侧阈值时，确实会进一步过滤案例，但原有 `sql_similarity_threshold` 已能直接设置所需的相似度门槛，当前没有独立的两层过滤需求。因此已撤销新增的 prompt 侧阈值，仅在检索阶段按相似度过滤；
3. v3 为保留原有行为，仍使用 `max_similar_sqls` 截取 prompt 案例并按切片长度计数。再次审核确认，这个数量上限与检索侧 `sql_embedding_top_k` 职责重复，v4 将其删除。

**v4 补充认定**：历史 SQL 检索结果唯一的功能性用途是进入 LLM prompt，日志和统计不构成保留独立展示数量上限的需求。因此删除 `max_similar_sqls`，通过检索阈值过滤的全部案例按原顺序进入 prompt，入 prompt 数量等于检索返回数量。v3 的“保留 prompt 数量上限”决策由本版替代。

## 二、已完成改造与 v4 补充清单

以下 v1 回退及 v3 检索改造已经完成，仅保留为实施记录；执行 v4 时不再重复整文件回退，以免覆盖已完成的改造。

| 文件 | v1 改动 | 回退操作 |
|---|---|---|
| `src/modules/sql_generation/config/sql_generation_subgraph.yaml` | `prompt:` 段新增 1 行 `similar_sql_min_similarity: 0.70` | `git checkout` 还原（该文件 v1 仅此 1 行改动） |
| `src/modules/sql_generation/subgraph/nodes/sql_generation.py` | ① 新增 `select_similar_sqls` 方法；② `_format_similar_sqls` 改为调用它；③ 节点日志段两分支改造。另有 black 波及既有行的格式噪音（diff 14 个 hunk 中约 11 个为纯格式） | `git checkout` 整文件还原，再按 §三 重做日志段（整文件还原同时消除格式噪音） |
| `tests/unit/modules/test_sql_generation_prompt.py` | 新增 `_select_stub` 辅助函数 + `TestSelectSimilarSqls` 类 6 个用例 | `git checkout` 还原为原有 9 个用例 |

上述三项为 v1 回退。回退后另外实施以下检索简化及 §3.2 的日志修正：

| 文件 | v3 改造前 | 已完成改动 |
|---|---|---|
| `src/services/vector_adapter/milvus_adapter.py` | `search_similar_sqls()` 使用 `limit=top_k * 2`，按相似度过滤后再累计到 `top_k` 条停止，未打印候选明细与过滤数量 | 按 §3.1 改为两步筛选；按 §3.3 增加 DEBUG 原始候选明细及过滤后数量 |
| `tests/unit/vector_adapter/test_milvus_adapter.py` | 已有历史 SQL 返回字段验证 | 随实现补充请求数量、相似度边界及 DEBUG 日志断言；配置传递及日志验证见 §5.2 |

**v4 实施改动**（在 v3 代码基础上定点修改，现已完成）：

| 文件 | 修改方法 |
|---|---|
| `src/modules/sql_generation/config/sql_generation_subgraph.yaml` | 删除 `sql_generation.prompt.max_similar_sqls` 一行；保留 `schema_retrieval.sql_embedding_top_k` 和 `schema_retrieval.sql_similarity_threshold`，两者继续从 YAML 读取 |
| `src/modules/sql_generation/subgraph/nodes/sql_generation.py` | `_format_similar_sqls()` 删除展示数量读取与切片，遍历全部 `similar_sqls`；节点数量摘要改为 §3.2 的直接计数，删除切片计数及同步配置注释 |
| `tests/unit/modules/test_sql_generation_prompt.py` | 保留原有 9 例，补充全部历史案例进入 prompt 及零命中隐藏整节的回归验证，具体输入及断言见 §3.5；不再要求该文件与 HEAD 零差异 |
| `/tmp/nl2sql_sql_history_plan_validation.py`（临时文件，不入库） | 删除 prompt 数量参数注入、切片预期和负数上限场景；删除 YAML、prompt 测试与 HEAD 完全一致的旧断言，改验配置内容、全部案例展示及 `A=M`，详见 §3.5 |

**文件数量与实际引用核对**：v4 的仓库实现改动为 2 个文件（YAML、`sql_generation.py`），正式测试改动为 1 个文件，另外更新 1 个临时验证脚本。v4 实施前，运行配置与源代码中，`max_similar_sqls` 恰有三处引用：YAML 定义一处、formatter 读取一处、节点日志读取一处；三处均在上述两个实现文件内，现已全部删除，运行代码与配置中无遗留引用。

`retriever.py`、Milvus 适配器、pgvector 适配器和 `PGClient` 的检索实现不需要为 v4 再次修改。

## 三、实施方案

### 3.1 Milvus 历史 SQL 检索：三步改为两步

**位置**：`src/services/vector_adapter/milvus_adapter.py` 的 `search_similar_sqls()`。本项只涉及历史 SQL 检索。

v3 改造前流程为：请求最多 `top_k × 2` 条候选 → Python 按相似度阈值过滤 → 合格结果累计到 `top_k` 条停止。已简化为：

1. **数量限制**：向 Milvus 请求 `limit=top_k`，由向量搜索返回最多 `top_k` 条相似候选。
2. **相似度过滤**：Python 保留 `similarity_raw >= similarity_threshold` 的记录，直接返回；合格记录可以少于 `top_k` 条。

**配置来源**：沿用 `src/modules/sql_generation/config/sql_generation_subgraph.yaml` 中的既有参数，配置值经 `SchemaRetriever.retrieve()` 传入适配器，不在适配器内重新设定业务阈值。

| 最终保留的 YAML 参数 | 符号 | 用途与传递路径 |
|---|---|---|
| `schema_retrieval.sql_embedding_top_k` | `N` | retriever 读取为 `sql_topk`，传给 `search_similar_sqls(top_k=sql_topk)`，直接用于 Milvus 的 `limit` |
| `schema_retrieval.sql_similarity_threshold` | `T` | retriever 读取为 `sql_threshold`，传给 `similarity_threshold`，用于 Python 相似度过滤 |

`retriever.py:285-294` 已有上述两项的读取与传递逻辑，继续沿用。`N` 和 `T` 均取自当次加载的 YAML 配置，不在本方案中固定为某个业务值。v1 新增的 `similar_sql_min_similarity` 已删除；v4 另删除原有 `max_similar_sqls` 配置及其全部读取、默认值和切片逻辑，不新增替代参数，也不为删除该参数而改写其余配置值。

**参数作用范围**：这两个参数仅控制历史 SQL 案例检索。在当前 Milvus 路径中对应 `sql_example_embeddings`，不涉及 `table_schema_embeddings` 或 `dim_value_embeddings` 的查询与结果处理；pgvector 历史 SQL 路径同样复用这两个参数。

**关键代码变化**（示意，结果解析及字段映射沿用原实现）：

```python
results = collection.search(
    data=[embedding],
    anns_field="embedding",
    param=self._get_search_params(),
    limit=top_k,
    expr=None,
    output_fields=["example_id", "question_sql", "domain"],
)

matches = []
for hit in results[0]:
    similarity_raw = float(hit.distance)
    if similarity_raw < similarity_threshold:
        continue
    # 沿用原有结果解析、分数规范化与 matches.append(...)。

# 删除原有 if len(matches) >= top_k: break；limit 已限制候选数量。
return matches
```

**简化依据与边界**：当前使用 COSINE 度量，Milvus 向量 `search()` 按相似度从高到低返回结果。[Milvus 官方说明](https://milvus.io/docs/single-vector-search.md) 对于同一个有序候选序列，仅按最低相似度过滤时，可分两种情况：

- 若前 `top_k` 条中出现低于阈值的记录，由分数单调不增可知，其后所有候选也低于阈值；扩大 `limit` 不会增加合格记录。
- 若前 `top_k` 条全部合格，旧代码也会在累计 `top_k` 条时停止，不会使用后续候选。候选总数不足 `top_k` 时，两种流程均过滤并返回全部实际候选中的合格记录。

因此，在同一有序候选序列上，两步与三步筛选结果等价，无需两倍召回及第二次数量截断。HNSW 属于近似搜索，不承诺不同 `limit` 的两次查询候选完全一致；本方案的契约是返回最多 `top_k` 条且每条达到配置的相似度阈值。

**其他后端范围**：当前 `src/configs/config.yaml` 的 `vector_database.active` 为 `milvus`。本期不修改 `PgVectorSearchAdapter` 或 `PGClient.search_similar_sqls()`；该路径已在 SQL 中按相似度阈值过滤、按向量距离排序并以 `LIMIT top_k` 限制数量。两种后端均保持“最多返回 `top_k` 条、每条达到相似度阈值”的对外契约，不要求内部筛选步骤或具体召回结果相同。

v4 实施后的完整流程为：**从 YAML 读取数量 `N` 和相似度阈值 `T` → Milvus 按 `limit=N` 返回最多 `N` 条候选 → Python 在这批候选中保留原始相似度 ≥ `T` 的记录 → 仅通过相似度过滤的案例全部进入 prompt**。相似度过滤仍使用原始分数判断，再沿用原有分数规范化逻辑。

**行为变化**：v4 取消 prompt 阶段的再次数量截断。Milvus 返回的候选必须再经过相似度阈值 `T` 过滤，不能因满足数量上限就直接进入 prompt；最终案例数可能少于 `N`，也可能为 0。数量和相似度门槛均由上述 YAML 参数控制。

### 3.2 日志修正

**位置**：`src/modules/sql_generation/subgraph/nodes/sql_generation.py`，`sql_generation_node` 内读取 `similar_sqls` 处。v4 替换当前 v3 的切片计数及同步配置注释。

```python
# v4：检索返回的全部案例进入 prompt
sql_count = len(similar_sqls)
qlog.info(
    f"历史 SQL 案例: 检索={sql_count}, 入prompt={sql_count}"
)
```

说明：

- **检索=A**：通过检索侧相似度阈值 `T` 过滤后的返回数，与 `retriever.py` DEBUG 日志的适配器返回数量同源；不同于 §3.3 中 Milvus 原始返回数 `C`；
- **入prompt=M**：formatter 遍历全部 `similar_sqls`，因此 `M=A=len(similar_sqls)`；节点不再读取 prompt 数量配置，也不再进行切片计数；
- 零命中仅记录数量 0，不新增原因推断；相似度过滤后的空列表既可能源于无候选，也可能源于候选全部低于阈值，具体情况通过已有 DEBUG 候选及过滤摘要观察；
- YAML 删除 `max_similar_sqls`，保留数量参数 `sql_embedding_top_k` 与相似度参数 `sql_similarity_threshold`，不新增参数或助手函数。

### 3.3 DEBUG 候选明细与过滤数量

**位置**：`MilvusSearchAdapter.search_similar_sqls()`，沿用该模块已有的 `logger` 和日志级别配置。

1. **筛选前打印所有候选**：在 `collection.search()` 返回之后、Python 相似度过滤之前，使用 `logger.debug()` 记录请求的 `limit=top_k` 和实际返回数量，再按 Milvus 原有返回顺序打印 `results[0]` 中每一条记录。每条包含序号、原始相似度分数、`example_id`、`domain` 及完整 `question_sql`。这些字段覆盖本次请求的 `output_fields` 及检索分数；不额外查询向量字段。实际返回不足 `limit` 时打印全部实际结果，空结果时记录返回数 0。
2. **筛选后只打印数量**：完成相似度过滤后，使用 `logger.debug()` 记录实际使用的 `similarity_threshold` 和保留记录数 `len(matches)`；不再单独打印 `matches` 的案例明细。
3. **入 prompt 数沿用 §3.2**：最终展示全部通过检索阈值过滤的案例，使用已有的 INFO 摘要记录；完整 prompt 由既有 DEBUG 日志输出，不另增一份最终案例明细。日志级别为 DEBUG 时，INFO 摘要也可见。

**数量口径**：配置的候选数量上限为 `N`，Milvus 实际返回候选数为 `C`，通过相似度阈值 `T` 过滤后的适配器返回数为 `A`，最终入 prompt 数为 `M`。v4 取消 prompt 截断后，`M=A`，且 `0≤A≤C≤N`。`M` 取决于实际候选及相似度过滤结果，不保证等于配置的数量上限 `N`。

日志形式示例（以下 `limit=3`、阈值 `0.6` 仅为演示数据，实际从 YAML 读取；筛选后及最终入 prompt 均只记录数量）：

```text
DEBUG 历史 SQL 候选: limit=3, 返回=3
DEBUG 历史 SQL 候选 #1: 相似度=0.72, example_id=case_1, domain=示例域, question_sql={"question":"示例问题1","sql":"SELECT 1"}
DEBUG 历史 SQL 候选 #2: 相似度=0.62, example_id=case_2, domain=示例域, question_sql={"question":"示例问题2","sql":"SELECT 2"}
DEBUG 历史 SQL 候选 #3: 相似度=0.55, example_id=case_3, domain=示例域, question_sql={"question":"示例问题3","sql":"SELECT 3"}
DEBUG 历史 SQL 相似度过滤: 阈值=0.6, 保留=2
INFO  历史 SQL 案例: 检索=2, 入prompt=2
```

原始分数使用原值输出，避免仅保留两位小数造成阈值边界难以判断。候选日志必须位于过滤之前，包含低于阈值的记录；不截断条数或案例文本。`question_sql` 保留原始载荷，不为日志额外解析 JSON，确保异常格式的候选也能如实观察。详细记录仅以 DEBUG 输出，不新增日志开关或业务参数。候选明细条数最多为 `top_k`，日志量随候选数量及文本长度增长；调大 `sql_embedding_top_k` 会相应增加 DEBUG 输出量，这是完整打印候选的已知代价。

### 3.4 删除 prompt 数量参数及再次截断（v4 新增）

下面的行号对应 v4 实施前审核时的代码，仅用于定位；实施时以配置路径、函数名及表达式为准。

| 文件与位置 | 当前内容 | v4 修改内容 |
|---|---|---|
| `sql_generation_subgraph.yaml:127`，`sql_generation.prompt` 段 | `max_similar_sqls: 2` 及其行尾注释 | 删除整行；保留其余配置，不新增替代数量参数 |
| `sql_generation.py:430-431`，`_format_similar_sqls()` | “从配置读取展示数量”注释及 `max_display = self.config.get(...).get("max_similar_sqls", 2)` | 删除该注释、赋值及因此多余的空白行，不保留默认上限 2 |
| `sql_generation.py:434`，同一 formatter | `enumerate(similar_sqls[:max_display], 1)` | 改为 `enumerate(similar_sqls, 1)`，原顺序遍历全部输入案例 |
| `sql_generation.py:552-555`，`sql_generation_node` | 同步配置注释、`max_display` 读取、`prompt_count` 切片计数及旧摘要 | 用 §3.2 的 `sql_count = len(similar_sqls)` 与数量摘要整体替换；不再访问 prompt 数量配置 |
| `sql_generation.py:277-284`，`_build_prompt()` | `if similar_sqls:` 包围历史案例标题及正文 | 代码保持原样，仅通过测试确认：空列表隐藏整节，非空列表展示 formatter 返回的全部案例 |

`_format_similar_sqls()` 修改后的完整方法如下，案例顺序、序号及问题、SQL、相似度渲染沿用原实现：

```python
def _format_similar_sqls(self, similar_sqls: List[Dict]) -> str:
    """格式化历史 SQL 相似案例"""
    if not similar_sqls:
        return "（无相似案例）"

    lines = []
    for idx, hit in enumerate(similar_sqls, 1):
        sim = hit.get("similarity", 0.0)
        question = hit.get("question", "")
        sql_text = hit.get("sql", "")

        header = f"{idx}. 相似度：{sim:.2f}"
        lines.append(header)
        if question:
            lines.append(f"   查询：{question}")
        if sql_text:
            lines.append(f"   SQL：{sql_text}")

    return "\n".join(lines)
```

删除后全仓检查源代码、运行配置和测试中的 `max_similar_sqls`、`max_display`、`prompt_count` 相关引用，确认历史 SQL 链路无遗留读取或硬编码的展示上限。`scripts/nl2sql_father_cli.py` 的 `max_display` 用于展示 SQL 执行结果，`_format_dependencies()` 的 `max_display_rows` 用于展示依赖结果，均不属于历史 SQL 案例配置，不随本次删除。设计文档中的历史说明和删除要求可保留。该 formatter 与节点为后端共用，因此 pgvector 返回的合格历史案例也会全部进入 prompt，但不修改其查询实现。

### 3.5 测试文件与临时脚本的具体修改（v4 新增）

**正式测试文件**：`tests/unit/modules/test_sql_generation_prompt.py`。

- 保留原有 9 个测试及 `_self_stub()`；新增用例仍直接执行真实 formatter 或 `_build_prompt()`，不调用外部 LLM。
- 新增“全部案例渲染”参数化用例，分别输入 3 条和 5 条各有独立问题、SQL 的案例，断言序号数量、问题、SQL、顺序均与输入一致；输入包含 `0.62` 分数，确保 formatter 不再增加相似度过滤。该用例输入代表检索侧已通过阈值过滤的返回值，不在 formatter 内再次读取或应用 `T`。
- 新增“空列表隐藏整节”用例，执行 `_build_prompt(..., similar_sqls=[])`，断言 prompt 不含“历史成功SQL案例（参考）”标题，也不包含 formatter 的“（无相似案例）”占位正文。
- 新增案例不注入 `max_similar_sqls`，不把预期数量写为 `min(返回数, 2)`，也不按另一个上限切片。

**临时验证脚本**：`/tmp/nl2sql_sql_history_plan_validation.py`；若临时文件已不存在，按 §5.2 重建等价脚本，不将其加入仓库。

- 将脚本的 `generate(context, max_display=2)` 改为 `generate(context)`，删除向配置注入 `max_similar_sqls` 的语句，检查加载的配置不含该键。
- 将 `expected_count = len(cases[:max_display])` 改为 `len(cases)`；所有案例均应在真实 LLM 调用收到的 prompt 中出现并保持原顺序。删除检查 `cases[max_display:]` 未展示的旧断言，不再保留任何预期案例切片。
- 删除 `generate(direct, max_display=-1)` 等负数展示上限场景；改为 §5.2 中改变 YAML 数量 `N` 和阈值 `T` 的串接场景。
- 删除脚本中 YAML、正式 prompt 测试文件与 `git show HEAD:...` 字节完全相等的旧断言。改查 YAML 不含两个已删除的 prompt 参数、仍保留两个检索参数；正式测试是否通过由 pytest 验证，变更范围按 §5.1 的 diff 检查确认。
- 保留真实 retriever → Milvus 适配器及真实生成节点调用，按 §5.2 重新验证日志与实际 prompt；旧脚本通过记录不代表 v4 通过。

## 四、设计决策记录

1. **不保留筛选助手函数**：v1 的 `select_similar_sqls` 已撤销；v4 继续在节点中直接计数、formatter 中直接遍历，不为简单计数新增助手函数。
2. **prompt 不再独立过滤**：相似度仅由原有 `sql_similarity_threshold` 控制，数量仅由原有 `sql_embedding_top_k` 控制；删除 `max_similar_sqls`，prompt 不再进行相似度或数量筛选。
3. **合格案例全部进入 prompt**：仅展示本次 Milvus 候选中原始相似度达到 YAML 阈值 `T` 的记录，不再受新增 prompt 门槛或第二次数量截断影响。日志中的检索返回数与入 prompt 数应相等。
4. **阈值取值调优**：不在删除重复参数的范围内；需要调整时直接修改原有 YAML 参数，不为某个具体取值增加代码分支。
5. **历史 SQL 全链路仅保留两个参数**：数量上限 `N` 与相似度阈值 `T` 均从 YAML 读取，采用 `limit=N` 后在返回候选中按相似度 ≥ `T` 过滤，formatter 只展示全部通过过滤的结果。方案不将数量固定为 3，也不将相似度阈值固定为 0.6。
6. **候选可观察性**：按 §3.3 在 DEBUG 完整记录 Milvus 原始候选及过滤后数量，最终案例内容复用完整 prompt 日志；分别统计原始返回、相似度过滤后返回及入 prompt 数量。

## 五、v4 验证计划与 v3 历史记录

§5.1、§5.2 的 v4 验证已完成，结果见 §5.5；§5.3 的真实服务端到端验证为补充验收，本次未执行。§5.4 单独保留 v3 历史记录。

### 5.1 单元测试与静态检查

- [x] `PYTHONPATH=. uv run pytest tests/unit/modules/test_sql_generation_prompt.py -v`：原有 9 例及本期新增回归用例全过；新增用例至少直接验证 3 条合格案例全部渲染（含 `0.62` 案例），以及空列表隐藏整个历史案例章节；
- [x] `git status` / `git diff` 确认：v4 的 YAML 差异仅删除 `max_similar_sqls`；`sql_generation.py` 仅在 v3 基础上修改 formatter 遍历和数量摘要；prompt 测试仅新增对应回归用例，既有适配器实现及其测试不因 v4 改动；
- [x] 检查运行配置与源代码，不再存在 `max_similar_sqls`、`similar_sql_min_similarity` 的读取或默认值，也没有新的 prompt 数量参数、硬编码截断或筛选助手；
- [x] `PYTHONPATH=. uv run pytest tests/unit/vector_adapter/test_milvus_adapter.py -v` 通过，保证已完成的检索两步筛选及 DEBUG 日志行为保持；
- [x] `uv run ruff check src/modules/sql_generation/subgraph/nodes/sql_generation.py src/services/vector_adapter/milvus_adapter.py tests/unit/modules/test_sql_generation_prompt.py tests/unit/vector_adapter/test_milvus_adapter.py` 通过；
- [x] `git diff --check` 通过，并复核改动区域的缩进、换行与周边代码风格一致。

**格式检查基线**：评审时确认，回退目标 HEAD 中原有的 `sql_generation.py` 本身不能通过整文件 Black 检查（显式使用 `--target-version py312` 也会要求重新格式化）。v4 仅调整 formatter 与数量摘要等本期改动区域的格式；整文件 Black 结果作为既有基线记录，不作为本期通过条件。整文件格式化另行处理，避免重新引入 §二 要消除的格式噪音。

### 5.2 固定输入验证（覆盖检索简化与日志缺陷）

完成 v4 修改后，更新临时验证脚本并执行以下检查，不将该脚本加入正式测试树，不新增筛选函数。§5.1 的 prompt 单元测试用于回归，不能代替真实节点与配置传递验证。v3 脚本中的切片计数及负数展示上限验证随参数删除而移除。

**检索配置与两步筛选验证**：加载真实 YAML，在脚本中复制配置并将原有 `sql_embedding_top_k`、`sql_similarity_threshold` 分别覆盖为非默认值 `5`、`0.65`（不写回 YAML），通过真实的 retriever → Milvus 适配器调用链验证传递，隔离其他外部依赖并 mock `Collection.search()`：

本节的具体数量和分数仅为固定输入测试数据，不作为运行配置的固定值；必须改变 `N`、`T` 分别验证数量限制和相似度过滤确实受 YAML 控制。

- [x] Milvus 请求的 `limit` 为 `5`，而非 `10`；提供按相似度降序的 5 条候选，分数为 `0.90`、`0.70`、`0.65`、`0.64`、`0.50`，返回结果依次保留前三条，验证配置门槛及等于阈值时保留的边界；
- [x] 再改变脚本中的原有配置值，确认请求数量与 Python 过滤门槛随之改变；全部候选低于阈值或 Milvus 返回空列表时，返回 `[]`；
- [x] 不再存在两倍召回或检索返回后的数量截断，也不读取任何 prompt 侧的历史 SQL 相似度或数量参数；已有适配器单测继续覆盖请求数量与相似度边界。

**DEBUG 候选日志验证**：在上述 5 条候选场景中捕获适配器的 DEBUG 日志，断言候选明细恰有 5 条，顺序与返回结果一致，包含 `0.64`、`0.50` 两条被过滤的记录，且字段内容完整；过滤摘要记录阈值 `0.65`、保留数 `3`，不另打印过滤后的案例明细。另覆盖实际返回不足 limit、空结果，以及仅启用 INFO 时不输出 DEBUG 候选明细。

**日志与 prompt 数量验证**：

脚本构造非空的 `schema_context`，其中 `similar_sqls` 按下表赋值；配置不含 `max_similar_sqls` 或 `similar_sql_min_similarity`。执行完整、真实的 `sql_generation_node`：mock `load_subgraph_config` 返回上述配置，mock `get_llm` 返回包含 `llm`、`provider`、`model` 的元数据对象，并让该模拟 LLM 的 `invoke()` 返回固定响应，避免访问外部 LLM。保留真实的 Agent 构造、`generate()`、`_build_prompt()` 和 `_format_similar_sqls()` 调用路径，捕获 INFO 日志和实际传给模拟 LLM 的 prompt；不能仅在脚本中重算预期数量而跳过被测代码。

| 场景 | 固定输入 | 日志断言 | 实际 prompt 断言 |
|---|---|---|---|
| 全部返回案例展示 | 3 条案例，相似度依次为 `0.72`、`0.62`、`0.61`，问题与 SQL 各不相同 | `历史 SQL 案例: 检索=3, 入prompt=3` | 历史案例段恰有 3 条，按原顺序全部展示；`0.62` 案例及第三条均正常展示，无缺省上限 2 |
| 零命中 | `similar_sqls=[]` | `历史 SQL 案例: 检索=0, 入prompt=0`，无新增门槛的原因附注 | 不追加“历史成功SQL案例（参考）”段，案例数为 0 |

- [x] 两种场景均通过，且每次日志的检索返回数、入 prompt 数及实际 prompt 中历史案例段的案例数三者相等。
- [x] 串接检索与 prompt 的固定场景中，设 `N=5`，实际返回 `C=5` 条候选，经相似度阈值过滤后保留 `A=3` 条，最终 `M=3` 条进入 prompt。
- [x] 设 `N=5`，实际返回 5 条且全部达到配置的相似度阈值，验证 `C=A=M=5`，排除 formatter 残留上限 2 或 3；保持这批候选不变并提高阈值，使部分或全部被排除，验证 prompt 案例数相应减少或为 0。
- [x] 再将原有数量配置改为 `N=2`，验证 Milvus 请求 `limit=2`，并对这最多 2 条候选继续执行相似度过滤；最终展示数为通过过滤的记录数，可能是 0、1 或 2。
- [x] 对 pgvector 路径使用其既有返回形状构造 3 条合格历史案例，经过同一真实生成节点后全部进入 prompt；不要求为该验证访问真实 PostgreSQL。

### 5.3 手工端到端验证（串接验收补充）

可重放问题"请共有多少个客户"，核对 `logs/nl2sql.log` 中同一次 SQL 生成的 INFO 日志与 DEBUG“完整 LLM 提示词”：

- [ ] 日志的“检索=A”与当次 `similar_sqls` 返回数量一致，“入prompt=M”与实际历史案例段的案例数一致，且 `A=M≤sql_embedding_top_k`；
- [ ] DEBUG 候选明细覆盖当次 Milvus 原始返回的全部记录（含低于阈值的记录），过滤摘要中的保留数与 INFO 摘要的检索返回数一致；过滤后的最终案例内容通过完整 prompt 日志核对。
- [ ] SQL 生成及执行正常，结果符合问题的业务语义；不要求 LLM 生成完全相同的 SQL 文本；
- [ ] 保留原有检索阈值与数量配置，确认已移除 prompt 相似度及数量参数和相关过滤、切片代码，按 §3.1 执行两步检索筛选，并展示全部合格结果。

2026-10-12 的“检索 3 条、展示 2 条，相似度为 0.72 与 0.62”仅作为 v3 之前的历史复现参考，不再作为 v4 的预期行为。真实召回随输入与数据变化，通用验收不要求固定召回数量、固定相似度或重复旧 SQL 结果；确定性的数量及低分案例保留检查由 §5.2 完成。

### 5.4 v3 已完成的实施与验证记录（2026-10-12，历史基线）

以下记录描述 v3 当时的验证结果，其中 prompt 仍保留数量截断；v4 的验证结果单独记录于 §5.5。

- v1 新增的 YAML 参数、筛选助手及 6 个测试已撤销；YAML 与原 prompt 测试文件均与 HEAD 一致。`sql_generation.py` 相对 HEAD 仅修改数量摘要日志，适配器仅修改历史 SQL 方法及所需的 `logging` 导入。
- 本地仅有 `.venv`，未发现指南指定的 `.venv-wsl`，因此使用现有 `.venv/bin/python`（Python 3.12）运行验证。
- `PYTHONPATH=. .venv/bin/python -m pytest tests/unit/modules/test_sql_generation_prompt.py tests/unit/vector_adapter/test_milvus_adapter.py -q`：**44 例通过**（原 prompt 9 例、Milvus 适配器 35 例，含本期新增 11 例）。
- `PYTHONPATH=. .venv/bin/python /tmp/nl2sql_sql_history_plan_validation.py`：旧版 §5.2 的真实 retriever → 适配器参数传递、非默认 YAML 参数、相似度边界、完整候选日志、真实生成节点及 prompt 数量检查全部通过；当时确认 `N=5、A=3、M=2`、`0.62` 案例保留、零命中隐藏整节标题，并补验负数展示上限时计数与切片一致。该脚本为临时验证，不加入正式测试树；v4 需按新版 §5.2 修改断言后重新验证。
- 两个改动源文件及适配器测试的 Ruff 检查通过；`git diff --check` 通过。未对整文件执行格式化，保留 §5.1 所述格式基线。
- §5.3 为补充验收，本次未连接真实 Milvus、数据库或 LLM 服务执行端到端验证；相关复选框保持未勾选。

### 5.5 v4 实施与验证记录（2026-10-12）

- 已删除 YAML 中的 `sql_generation.prompt.max_similar_sqls`、formatter 中的配置读取与再次切片、节点日志中的配置读取与切片计数。正式代码及运行配置不再引用该参数，也未引入替代参数或筛选函数。
- 数量 `N` 和相似度阈值 `T` 继续来自原有 YAML 参数。与本次实施前快照比较，YAML 仅删除目标键，其余配置内容完全一致；Milvus 适配器及其测试文件的内容未改动。
- 使用现有 `.venv/bin/python` 执行 `PYTHONPATH=. .venv/bin/python -m pytest tests/unit/modules/test_sql_generation_prompt.py tests/unit/vector_adapter/test_milvus_adapter.py -q`：**47 例通过**，包括 prompt 12 例（原有 9 例及新增 3 例）和适配器 35 例。
- 更新并执行 `/tmp/nl2sql_sql_history_plan_validation.py`：真实 retriever → Milvus 适配器 → 真实生成节点调用路径验证通过，外部服务均使用 mock。配置 `N=5` 时，同一批候选在不同阈值下分别有 5、3、1、0 条进入 prompt；配置 `N=2` 时也验证了过滤后分别为 2、1、0 条。相似度边界、完整 DEBUG 候选、日志与实际 prompt 数量一致、pgvector 返回形状兼容及零命中隐藏整节均通过。
- 两个源文件及两个相关测试文件的 Ruff 检查、`git diff --check` 均通过；未执行整文件格式化，保留既有格式基线。
- 本次未连接真实 Milvus、PostgreSQL 或 LLM 执行 §5.3 的端到端验证，相关复选框保持未勾选。

## 六、后续事项（非本期范围）

- `sql_similarity_threshold` 与 `sql_embedding_top_k` 的取值调优：积累实测后另行议题；调大数量上限后，实际进入 prompt 的案例数仍取决于召回结果及相似度过滤，不保证同步增加；
- 若未来确需"高质量案例门槛"，优先评估直接上调检索阈值（yaml 一行），而非在 prompt 侧新增过滤层。
