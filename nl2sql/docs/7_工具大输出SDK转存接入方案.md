# 工具大输出 SDK 转存接入方案

> 状态：**已实施（2026-11-11）**，实施记录见文末。以下为实施前设计（含三轮审核修订），保留原文。

**目的（Goal）：** 以 `inspect_tables` 为试点，用 SDK 自动转存替代该工具的整体输出压缩/硬截断，保留工具输出，减少直接进入模型上下文的长内容。

**实现方式（Architecture）：** `inspect_tables` 返回合法 JSON；已有 `FilesystemMiddleware` 将超阈值内容写入会话文件，模型接收预览及路径，再通过 `read_file` 按需读取。

**技术范围（Tech Stack）：** 已安装的 DeepAgents 0.7.34、现有 CompositeBackend/FilesystemBackend、pytest；不升级依赖。

**依据（Spec）：** [doc 2 §5.3](2_DeepAgents_0.7.34新特性与项目收益分析.md)、[doc 4 §4.1](4_DeepAgents实现简化与旧链保留价值分析.md)。转存是 SDK 已有能力，不是 0.7.34 新增功能。

## 1. 范围与固定决策

- 只接入 `inspect_tables`。它返回字符串、不写业务账本，先验证转存和回读，再评估其他工具。
- 项目未上线，直接修改现有实现；不增加旧行为开关、兼容路径或历史数据迁移。
- 保持 `filesystem.tool_token_limit_before_evict: 20000`。本机 SDK 按 `4 × 配置值` 的字符近似判断，即正文 **超过 80000 字符**时转存，不是精确 tokenizer 计数。
- `results.max_tool_output_chars: 12000` 继续约束其他原有调用点；本次仅让 `inspect_tables` 不再使用它。12000～80000 字符的该工具输出会直接进入上下文，这是本次明确接受的行为变化。
- 保持表数量上限 10、实时列选择、逐表错误隔离、warnings 和工具参数不变。
- `TableCatalog.describe()` 的卡片正文仍限制为 2000 字符，`card_truncated` 保留。完整转存指完整的工具返回值，不恢复上游已裁掉的正文。
- 保持 `ResultStore`、结果分页、SQL 执行与校验、最终答案证据门控、摘要器和 UI/API 不变。

**边界：** 本次使试点工具绕开非法 JSON 硬截断；其他工具仍使用 `_envelope.finalize_output()`，其缺陷未在本方案中全局修复。原缺陷探针和 `test_envelope.py` 不改成“全部问题已修复”。

## 2. 按顺序修改

以下路径均相对 `nl2sql/`。

### 步骤 1：先补试点工具测试，确认目标 RED

**修改：** [src/tests/db_deepagent/unit/test_schema_tools.py](../src/tests/db_deepagent/unit/test_schema_tools.py)，在 `TestInspectTables` 增加：

| 用例 | 输入与断言 |
| --- | --- |
| `test_large_description_keeps_all_columns` | 假业务库返回大量列，使工具正文超过 12000 字符；经现有 ToolNode 辅助调用，断言 JSON 可解析、全部列及末列标记保留 |
| `test_six_table_descriptions_not_reduced_to_five` | 假 catalog 为 6 个合法表返回足够大的说明，使旧实现进入列表裁剪；断言返回 6 表、顺序与逐表 status/warnings 不变 |

继续保留好表描述、非法表隔离、无卡片告警三类已有测试。假件局部放在测试内，不改全局 fixture，不请求模型或真实数据库。

- [x] 运行目标测试，确认失败来自旧工具压缩/截断，而非假表键非法、依赖缺失或 fixture 配置错误。

### 步骤 2：让 `inspect_tables` 直接序列化完整工具结果

**修改：** [src/modules/db_deepagent/tools/schema_tools.py](../src/modules/db_deepagent/tools/schema_tools.py) → `make_inspect_tables_tool()` 内的 `inspect_tables()` 尾部。

替换该函数的 `finalize_output(...)` 和 `_merge_warnings(...)` 调用为：

```python
return json.dumps(
    {
        "status": "ok",
        "data": {"tables": results},
        "warnings": sorted(set(all_warnings)),
    },
    ensure_ascii=False,
    indent=2,
)
```

**本试点必须保留 `indent=2`，不得改回单行紧凑 JSON。** `read_file` 的 `offset/limit` 是原始文件的行号/行数，没有行内字符偏移参数；它自身还会将过长的显示结果裁到约 80000 字符以内（提示语与状态头也占预算）。若完整 JSON 是一条超过该预算的长行，重复调整行号无法读到该行后段；SDK 单行预览也只显示前 1000 字符。多行序列化是本方案按行回读的必要条件，`indent=2` 是本次固定采用的格式。

缩进不能拆开单个超长字符串；目前卡片正文的 2000 字符上限保留。步骤 4/5 必须用位于原文第 80000 字符之后的中部/末列标记验证回读，防止只验证到首段。保留当前逐表处理代码和错误语义，不手写文件、不新增返回包类型。文件中其他工具仍使用两个辅助函数，因此其导入和实现保留。

- [x] 步骤 1 测试转为 GREEN，原 `TestInspectTables` 用例继续通过。

### 步骤 3：核对已有转存接线，只修改配置说明

**修改：** [src/modules/db_deepagent/config/db_deepagent.yaml](../src/modules/db_deepagent/config/db_deepagent.yaml) 的两处注释，配置值保持：

```yaml
results:
  # read_result / analyze_result / search_schema / plan_joins / guard / SQL 等仍使用此字符上限。
  # inspect_tables 改由 SDK 转存管理，不使用此字符上限。
  max_tool_output_chars: 12000

filesystem:
  # SDK 近似转存阈值；当前版本按 4 字符/token 判断，20000 对应 80000 字符。
  tool_token_limit_before_evict: 20000
```

片段只展示相关字段，不替换整个 YAML 段。删除“保证全部业务工具不触发 SDK 转存”的过时注释。

**核对但不修改：** [src/modules/db_deepagent/agent.py](../src/modules/db_deepagent/agent.py) 的 `build_backend()`、`build_permissions()`、`build_filesystem_middleware()`：

- 同一个 backend 已传给文件中间件及主 Agent，`artifacts_root="/artifacts"`。
- SDK 转存路径为 `/artifacts/large_tool_results/<清理后的 tool_call_id>`；实际文件在 `<workspace>/sessions/<session_id>/artifacts/large_tool_results/`。
- 模型已有 `/artifacts/**` 读取权限和 `read_file`，无需新增工具或写权限。SDK 内部写文件与模型可调用写工具是两件事。
- 不新增自定义转存中间件，不调用 SDK 私有转存函数作为生产实现。

### 步骤 4：增加 SDK 转存与回读验证

**新增：** `src/tests/db_deepagent/unit/test_tool_output_offload.py`。

测试使用真实 SDK、生产 `build_backend/build_permissions/build_filesystem_middleware` 与临时会话目录。以公开 `wrap_tool_call()` / `awrap_tool_call()` 驱动工具调用；handler 返回实际 `inspect_tables` 生成的 `ToolMessage`。不以直接写文件代替 SDK 转存，不 mock 掉被测中间件。

| 用例 | 必须断言 |
| --- | --- |
| `test_small_inspect_output_stays_inline` | 使用生产阈值，短输出原样返回、没有转存文件 |
| `test_threshold_boundary` | 用 `"x" * N` 构造 ToolMessage 正文：80000 字符不转存、80001 字符转存；边界测量的是消息文本，不要求合成文本是 JSON |
| `test_large_inspect_output_saved_without_loss` | 真实试点工具的序列化输出超过 80000 字符；转存文件原文与 handler 输入完全相同，JSON 可解析、所有表列保留；替换消息含路径且明显短于原文，tool_call_id/status 保留 |
| `test_saved_output_readable_with_existing_permissions` | 用生产中间件注册的 `read_file` 经 ToolNode 调用，按 `offset/limit` 分段读取首部及末列附近；确认正文中部/末尾标记可读，不只检查路径存在 |
| `test_command_updates_survive_offload` | 合成一个含长 ToolMessage 的 `Command`，附 `db_dim_evidence` 和 `db_executions` 哨兵更新；经同一公开 wrapper 后，账本更新值不变、消息保留调用身份并发生转存。此项验证 SDK 语义，不代表其他业务工具已接入 |
| `test_write_error_preserves_original_message` | 让 backend 的写入返回 `WriteResult(error=...)`；SDK 应保留原始长消息，不伪造文件引用、不返回残缺 JSON。另用抛出 `OSError` 的写入假件验证异常不被伪装为成功 |
| `test_async_offload_and_command_updates` | 用 `asyncio.run()` 驱动公开异步 wrapper，核对转存、回读与账本保留；不新增 pytest 插件 |

`read_file` 文本输出带 `@@ ... @@` 状态头/窗口提示（含行范围），正文不添加逐行行号；超限时还可能带截断说明或行内截断标记。状态头等 SDK 附加文本会使显示结果不再是原始 JSON，因此不直接对显示文本调用 `json.loads()`；JSON 完整性在原始转存文件上验证。回读测试用多行结构和合适的读取窗口，避免一次把整个大文件塞回上下文。

**大输出假件规模：** 可从 10 个合法表 × 每表 100 个实时列开始，列名约 50 个合法字符，字段使用生产实际返回的 `name/pg_type/nullable`。生产代码不输出实时列注释，不靠虚构的 `comment` 字段凑长度；假业务库须为这些表实际返回列，不能只填卡片（卡片会先裁到 2000 字符）。序列化后显式断言 `len(raw_content) > 80000`，并确认回读标记的原文字位置在 80000 之后，再调用中间件；不以表数/列数代替长度断言。步骤 5 复用同一规模。

**失败处理决策：** 接受 SDK 现有行为：显式写入错误保留原消息，抛出的异常按现有运行异常路径处理。本次不另写降级/重试框架；保留原消息不等于满足上下文预算，写入失败时不得宣称转存成功或内容已缩减。

### 步骤 5：验证完整生产装配下的模型可见内容

**仍在新增文件中增加：** `test_production_graph_reads_offloaded_inspect_output`。

- 使用 `assembly_services`、临时 workspace、`InMemorySaver` 和生产 `build_db_agent()`；主模型使用测试内自定义的、按消息内容分支的局部假模型，摘要器沿用现有 fixture 假件。现有 `ScriptedChatModel` 只支持静态顺序响应，可继承后重写 `_generate()`，不能直接填固定 responses 冒充动态路径提取；不改全局 conftest。
- 假 catalog 返回超过生产转存阈值的多行表列说明；脚本模型先请求 `inspect_tables`，下一轮从实际 ToolMessage 提取返回路径，再请求 `read_file` 读取含末列标记的窗口。
- 记录脚本模型收到的消息：确认先看到预览/路径，随后看到文件中的目标列；没有整段原始大输出直接进入该轮模型请求。
- 用标准 `DbAgentContext` 和 metadata 驱动图；调用完成后检查工具预算正常扣减、15 工具装配契约不变。假模型在回读后返回普通文本，并在收到 `dbda_kind=final_answer_reminder` 的提醒后再返回一轮普通文本；保留生产 `final_answer_reminders: 1`，不因提醒重复发起 inspect/read 调用。此测试验证图路径，不伪造 SQL 证据来强行得到业务 `completed`。
- [x] 先确认旧实现因预截断而无法完成大输出转存/末列回读，再运行修改后测试确认通过。

### 步骤 6：回归及文档同步

在 `nl2sql/` 执行：

```bash
env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_schema_tools.py \
  src/tests/db_deepagent/unit/test_tool_output_offload.py \
  src/tests/db_deepagent/unit/test_backend_permissions.py \
  src/tests/db_deepagent/unit/test_assembly.py -q

env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit src/tests/unit/api/v2 \
  tests/unit/db_deepagent/test_independent_summarizer.py -q

.venv/bin/python -m black --check \
  src/modules/db_deepagent/tools/schema_tools.py \
  src/tests/db_deepagent/unit/test_schema_tools.py \
  src/tests/db_deepagent/unit/test_tool_output_offload.py
.venv/bin/python -m ruff check \
  src/modules/db_deepagent/tools/schema_tools.py \
  src/tests/db_deepagent/unit/test_schema_tools.py \
  src/tests/db_deepagent/unit/test_tool_output_offload.py
git diff --check
```

记录实际退出码及通过/跳过数，既有 lint 问题与新增问题分开。离线验证不调用真实模型/数据库；本方案不把真实模型一定会主动回读、成本改善或全部工具已修复作为已验证结论。

**实施后运行核对：** 选一次真实 `inspect_tables` 大输出运行，记录请求/工具调用 ID、表数与列数、缩进序列化后的实际字符数、是否转存，以及该轮 `summary_model_calls` 和用量记录。转存时从文件统计原文长度，未转存时从该调用的完整 ToolMessage 统计；诊断读取不新增生产中间件或观测框架。没有真实大输出样本时明确记为未验证，不用假件数字替代。

12000～80000 字符的直接返回可能在常见的多表结构查询中出现，`indent=2` 也会增加字符数；不把“10 表 × 30～50 列约 50k～90k 字符”作为通用实测结论。以 40000 字符为例，SDK 的近似规则相当于 10000 tokens，实际模型 token 数仍取用量记录；连续调用可能使上下文更快达到摘要阈值。单次运行只能记录是否发生摘要，是否“提前”须有对照后判断。这些记录用于决定后续是否调整 20000 的转存阈值，本次不改数值。

**预览解读：** 缩进 JSON 的头尾各 5 行可能主要是外层结构、标识或收尾括号，不能据此认定模型已获得完整表列信息。验收记录注明“沿用 SDK 默认预览，回读依赖路径和提示语”，记录实际是否回读；不为改进预览模板新增自定义转存中间件。

**最后修改：** [doc 2 §5.3](2_DeepAgents_0.7.34新特性与项目收益分析.md)、[doc 4 §4.1](4_DeepAgents实现简化与旧链保留价值分析.md) 和本文状态，注明试点范围、实际结果及其他工具尚未处理的硬截断问题。

## 3. 验收标准与审核重点

五项均需有上文测试证据：

1. **内容完整性：** 超宽列结构和 6 表输出不再被试点工具裁掉；文件内容与转存前工具正文一致。2000 字符卡片上游规则仍明确可见。
2. **模型上下文：** 超过生产阈值后实际看到预览/路径，能够在现有权限下回读所需段落；SDK 替换消息本来就是文本，不要求它仍是 JSON。
3. **业务状态：** ToolMessage 身份及 Command 账本更新保留；数据库结果仍走 ResultStore，模型未获得新增写权限。
4. **失败行为：** 写入失败不会产生假的成功引用；无法保证缩减时按实际行为报告，不用硬切字符串兜底。
5. **范围控制：** 其他工具与 API 回归无新增失败。只证明 `inspect_tables` 试点成立，再决定是否扩大，不将本次接入与全局截断 bug 修复混为一项成果。

## 实施记录（2026-11-11）

实施由用户授权的 Claude Code 会话按本方案 TDD 执行（Orca worker task_0e973bb459b5），经协调者 Codex 两轮代码审核通过。改动仅限 §2 授权范围 7 个文件中的代码/测试 4 个 + 文档 3 个；未动 TableCatalog、其他工具的 `finalize_output`、旧探针与旧测试、任何预算值与生产装配代码。完整命令输出：`/tmp/insightweave-doc7-implementation-qttfgnls/logs/`。

### 两类目标 RED（均确认，非环境错误）

1. **工具层**（`s1-red.log`，exit 1）：两个新用例失败于旧 `finalize_output` 行为——`JSONDecodeError: Unterminated string … (char 11980/12000)`，即整体截断恰在第 12000 字符处截断 JSON；不是假表键非法或 fixture 错误。
2. **SDK 层/完整图**（`s4-red.log`，exit 1，旧生产代码下运行新测试文件）：3 个依赖新行为的用例失败——大输出保存与完整图 2 例失败于 `assert len(raw) > 80000` 长度断言（旧实现输出恰为 12000），权限回读 1 例失败于 `raw.index(_MARKER)` 抛 `ValueError: substring not found`（旧整体截断丢失标记）；同场 5 个纯 SDK 语义用例（80000/80001 边界、小输出直返、Command 账本、写失败、异步）通过，证明失败均源于旧工具预截断而非测试环境。

### GREEN 与验证（cwd＝`nl2sql/`，测试进程排除 SOCKS 代理变量）

| 项 | 结果 | 日志 |
| --- | --- | --- |
| 步骤 2 应用后 test_schema_tools.py | **20 passed**（原 18 + 新 2），exit 0 | s2-green-schema-tools.log |
| test_tool_output_offload.py（8 用例） | **8 passed**，exit 0 | s4-green-offload-v3.log |
| 步骤 6 聚焦（4 文件） | **44 passed**（任务基线 34 + 本次新增 10；schema_tools 18→20、offload 新增 8），exit 0 | s6-focused-final.log |
| 步骤 6 离线回归 | **818 passed、4 skipped、0 failed**（doc 5.1 基线 808+4，+10 新用例），exit 0 | s6-regression-final.log |
| Black（3 个变更 Python 文件） | 终态 **3 files unchanged**，exit 0 | s6-black-final.log |
| Ruff（同 3 文件） | 5 项全部为协调者基线文件既有问题（schema_tools.py 的 UP045×3/FLY002/RUF022，经 `--stdin-filename` 对基线副本核对同集合），**零新增**（新文件自查出的 I001×1 已当即修复） | s6-ruff-final.log |
| git diff --check | 通过 | — |

实施中两处测试内修正（非方案偏离）：完整图用例的服务栈改为按 `assembly_services` 字段合并替换检索三类假件（首版缺 budget/pipeline 等步骤 3 服务导致 `RunBudgetMiddleware` 取到 None）；按生产 Runner 语义先 `budget.open_run(...)` 再驱动图（未注册运行的预占放行不计数，属账本既有设计）。

### 关键证据（离线，无真实模型/数据库）

- 大输出假件：10 表 × 每表 100 实时列（`name/pg_type/nullable`，50 字符列名，无 comment 凑长度）；序列化后 **len(raw) > 80000 显式断言**，末表末列标记 `ZQ_END_MARKER_COLUMN_ZQ` 原文位置 > 80000（测试内逐次断言）。
- 边界：80000 字符原样直返且无文件；80001 字符触发转存，文件内容与消息文本逐字相等。
- 回读：经生产中间件注册的 `read_file`（create_deep_agent 装配的工具节点）按 `offset/limit` 分段读取；首部窗口含 `@@ … @@` 状态头，标记窗口（标记行前 10 行起 30 行）读到末列标记且 < 80000 字符；JSON 完整性在原始转存文件验证。
- 失败语义：`WriteResult(error=...)` → 原消息原样保留、无文件引用；`write` 抛 `OSError` → 异常传播（`pytest.raises` 捕获），未伪装成功。
- 完整生产图：动态假模型（继承 ScriptedChatModel 重写 `_generate`，按消息分支）先调 `inspect_tables` → 从实际替换消息提取 `/artifacts/large_tool_results/...` 路径 → `read_file` 读到标记 → 普通文本收尾 → 响应一次 `dbda_kind=final_answer_reminder` 后再普通文本结束；模型可见消息逐轮记录：inspect 轮消息含路径且 < 80000 字符（整段原文未进入模型请求），read 轮含标记；工具调用序列恰为 inspect/read、工具预算计数为 2、提醒计数为 1，提醒后的 AI 回复无工具调用；stop_reason None、15 工具契约不变。

### 实施后运行核对：无真实样本，记为**待验证**

现有真实会话审计（`var/db_deepagent/sessions/*/audit.jsonl`）含 `model_request`（工具清单、approx_tokens）与 `model_attempt`（call_kind/ok/elapsed_ms/input_tokens/output_tokens）事件，本地真实运行 JSON 也有 `summary_model_calls`/`token_usage_complete` 等统计；但**审计不保留 `inspect_tables` 完整返回正文**，无法重建其实际字符数，且旧代码下真实输出恒 ≤ 12000 字符、本次尚无新实现的真实大输出样本（会话目录无任何 `large_tool_results/`）。因此该类样本的"实际字符数/是否转存/摘要影响"本轮未验证，不用假件数字替代。运行中后端未重启，仍持旧配置；待重启后自然产生样本再核对。

### 未验证边界

- 离线假模型按路径回读成功，**不证明真实模型一定会主动回读**；预览沿用 SDK 默认（头尾各 5 行，对缩进 JSON 主要是结构/括号信息），回读依赖路径与提示语。
- 无费用/延迟/上下文收益结论；12000～80000 字符直返的实际上下文影响与摘要提前触发未实测。
- 其他工具的硬截断与 JSON 中部截断缺陷未修复（本方案明确不在范围）；`test_envelope.py` 与旧探针未改。
- 完整默认测试集仍因 `src/tests/unit/tools/test_join_planner.py` 既有导入错误无法收集（与本方案无关）。

### 方案偏离

无。步骤 2 代码块按原文逐字落地（曾误加模块级辅助函数，随即改回函数内联形式以符合"仅改 inspect_tables() 尾部"的授权范围）；其余按 §2–§6 执行。

### 第 1 轮协调者审核（C1，2026-11-11）

协调者复核未发现运行时缺陷（独立 44 passed、Black/Ruff/YAML 核对通过）；提出 2 项，均确认成立并采纳：

- **C1-1 测试补强**：完整图用例收紧为——最终状态 AIMessage 工具调用序列恰为 `["inspect_tables", "read_file"]`、预算 `tool_call == 2`、`final_answer_reminders == 1`、提醒 HumanMessage 之后的 AI 回复存在且全部不带工具调用（原 `>= 2` 弱断言已替换，防重复调用被掩盖）。收紧后该用例 1 passed、聚焦 44 passed、Black 3 文件 unchanged、Ruff 仍为基线 5 项零新增。
- **C1-2 记录修正**：RED 描述改为"2 例长度断言 + 1 例 ValueError（旧截断丢标记）"；聚焦基线口径统一为"任务基线 34 + 本次 10 = 44"（36 为步骤 2 已加 2 例后的中间值）；"审计只含请求级 approx_tokens"的绝对化表述改为"审计含 model_attempt 用途/耗时/用量与真实运行统计，缺的是 inspect 完整正文与新实现真实大输出样本"。原始日志未改动。

详见 review-c1-response.md 与实施报告更新节。

### 第 2 轮协调者审核（C2，通过）

两项反馈均已落实，无未决问题。C1 后仅新增转存测试文件和本文发生修正，生产代码未变化。协调者独立复跑步骤 6 聚焦命令：**44 passed**，exit 0；Black 3 文件通过；Ruff 现状与任务基线均为 5 项，零新增；YAML 配置值与任务基线完全一致，`git diff --check` 通过。已核对原始 **818 passed/4 skipped** 回归证据，本轮未无理由重复全量回归或真实请求。报告：`/tmp/insightweave-doc7-implementation-qttfgnls/review-c2.md`。

代码审核共两轮，通过；未提交、推送或重启服务。真实大输出运行及真实模型主动回读仍待验证，本次通过不扩大到其他工具的截断问题。

### 用户重启后的本地运行验收（2026-11-11 01:08）

用户于 01:07:41 重启后端，通过 Vue 查询“请问系统中共有多少个订单？”。协调者核对日志、运行记录、接口及只读 PostgreSQL 检查点：

- 请求 `req_2cbc588d4f4a407d90618fe803bac616` 为 `completed`，耗时 **7702 ms**，得到 **500 个订单**。SQL 校验、执行、最终答案及归档成功，errors/warnings/failures 均为空；本次启动后的后端文件日志未见 WARNING/ERROR。
- 本次 `inspect_tables` 调用 `call_ebc11437ac9b422a996a7719` 成功；检查点中的实际返回为 **1596 字符、53 行**，可解析且与 `json.dumps(..., ensure_ascii=False, indent=2)` 完全相同，确认新实现已被重启后的服务使用。
- 实际正文低于 80000 字符，因此直接返回，未生成转存文件；本轮模型调用 5 次、摘要调用 0 次，用量记录完整。该样本仅验证真实小输出路径，不是大输出转存/真实模型回读验收。
- 健康、历史轮次、结果读取接口均为 HTTP 200；历史保存本次问答及 SQL/结果引用，结果接口返回 `total_orders=500`。

本地正常查询验收通过。证据副本位于审计目录 `logs/post-restart-web-query-api.json`、`logs/post-restart-inspect-output.json`。前文“未重启/无真实样本”描述对应开发审核阶段；现已取得真实小输出样本，大输出样本仍待验证。

### 真实模型 + 模拟大输出受控验收（2026-11-11，通过）

用户授权后，新增 [真实模型回读测试](../src/tests/db_deepagent/integration/test_model_tool_output_offload.py)。使用生产 `inspect_tables`、文件后端、权限和当前 Max 模型；仅表结构数据为模拟数据。目标列名带随机后缀，位于正文中部，不出现在问题或头尾预览中；不向模型预先提供文件路径、行号或完整目标列名。任务明确要求读取字段段落后回答。

实际结果：

- **1 passed，9.10 秒，exit 0**。工具返回 **166842 字符**，目标字段位于第 **91863** 字符处；SDK 保存的文件原文与工具返回逐字一致。
- 实际模型为 `qwen3.8-max`，4 次调用，用途观测无异常、用量完整。合计输入/输出 11537/286 tokens，摘要调用 0 次；不据此推导生产成本收益。
- 模型收到的 `inspect_tables` 替换消息为 **845 字符**，含预览和路径；没有目标字段，也没有原始完整大输出。
- 模型实际执行 `inspect_tables → grep → read_file`；`grep` 定位后，它自行选择 `offset=2845, limit=15`，读到字段 `lookup_probe_bcca639ebb3d` 与 `pg_type=numeric`，最后准确返回两项。`read_file` 返回为 510 字符，含状态头及原文窗口。
- 文件位于临时目录，未修改真实数据库、生产代码或配置。此结果证明：在该受控任务下，真实模型可以使用 SDK 转存路径读取中部信息；不等于已有真实业务大输出样本，也不保证所有自然问题都会主动回读。

显式运行命令（cwd=`nl2sql/`，会调用真实模型）：

```bash
env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest \
  src/tests/db_deepagent/integration/test_model_tool_output_offload.py \
  --run-llm -q
```

默认不加 `--run-llm`：1 skipped。新文件 Black/Ruff 通过。另运行完整默认测试集，仍在收集阶段被既有 `src/tests/unit/tools/test_join_planner.py` 导入错误阻断（缺少 `build_multi_base_join_plans`，exit 2）；未因此修改无关代码。

本次完整日志、JUnit、模型收到的工具消息、工具调用参数及转存文件保存在 `/tmp/insightweave-doc7-real-offload-1o1gbodo/`，汇总为 `result.json`。真实业务大输出的字符分布、实际摘要影响和成本/性能对照仍未验证。
