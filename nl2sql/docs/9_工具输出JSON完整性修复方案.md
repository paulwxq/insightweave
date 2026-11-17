# 工具输出 JSON 完整性修复方案

> 状态：已实施并通过双重代码审核（2026-11-11）：Kimi 第 2 轮通过，Codex 第 1 轮通过；独立离线回归 662 passed、28 skipped。真实模型受控验收已通过（§6.6：真实 LLM＋模拟业务数据）；开发/审核当时的验收边界见 §6.3，审核记录见 §6.5。
> 实施 Agent：按 `executing-plans` 技能逐步实施；沿用用户指定的 Orca 委派与审核方式，不自行创建额外 Agent。

**目的（Goal）：** 修复统一返回层因字符截断或列表裁剪造成的 JSON 结构损坏、列与行错位及证据映射丢失；不扩展为全部数据库类型的编码修复。

**实现方式（Architecture）：** 删除统一的字符级压缩与截断，对现有已可序列化为标准 JSON 的载荷完整输出多行文本；超过现有 SDK 阈值时由 `FilesystemMiddleware` 自动转存。业务层继续决定查询保存量、预览行数和分页大小，不再由通用返回包函数擅自删字段或删行。

**技术范围（Tech Stack）：** 当前 DeepAgents 0.7.34 / LangChain 1.4.9 / LangGraph 1.2.20、现有文件后端与 pytest；不升级依赖。

**依据（Spec）：** [doc 4 §4.1](4_DeepAgents实现简化与旧链保留价值分析.md)、[doc 7](7_工具大输出SDK转存接入方案.md) 及本文 §1 的离线预检。本文调整[旧详细设计 §7.1](v4/5_db_deepagent详细设计.md)（当前第 787 行）“业务工具输出永远不会触发 SDK 大结果转存”的规则；K9 条目本身说明的是 `artifacts_root` 路径机制，不是禁止转存规则。不能同时执行两套相反约定。

## 1. 已验证的问题与方案选择

| 证据 | 结论 |
| --- | --- |
| `_envelope.finalize_output()` 最终返回 `text[:max_chars]`；真实 `read_result`、`execute_sql` 复现 `JSONDecodeError` | 缺陷位于工具消息层，不是 PostgreSQL 或结果存储损坏 |
| 原函数超限时将 `columns` 裁为 5 项，未同步裁行内单元格 | 即便 JSON 合法，行列对应关系也可能错误 |
| `_merge_warnings()` 在限长处理后重新序列化 | 提示可能并不进返回包，或并回后再次超过原上限 |
| 离线分页原型删行却沿用旧 `next_offset`，120 行漏读 10 行 | 结构内删行需要新的分页算法，不能简单替代文本截断 |
| `reuse_evidence.mapping` 是模型取得新证据 ID 的返回入口 | 保留前几项不等于保留证据；账本完整也不能弥补模型看不到映射 |
| 严格限长回退原型仍存在低预算返回超限、丢语义字段等边界 | 不采用这套原型作为生产实现 |

**选择：保留上述范围内的完整 JSON，复用现有 SDK 转存。** 不新增通用缩减规则、最小预算校验、错误回退包、消息层分页或自定义转存中间件。不因返回文本较长把已执行成功的 SQL 改报为执行失败，也不要求模型重复执行 SQL 来取回丢失的引用。

**既有编码边界，本次不修：** 普通 `json.dumps` 会输出 `NaN/Infinity/-Infinity`，Python 默认 `json.loads` 又接受这些非标准常量；工具预览的 `_encode_value()` 为浅层编码，嵌套 `[Decimal("1.5")]` 等值仍可能在序列化时抛 `TypeError`。结果文件的 v2 编码支持数组，不等于工具预览已经支持全部嵌套类型。本文不承诺修复这些输入，也不通过单加 `allow_nan=False`、`default=str` 或静默改值掩盖它们；递归编码与非有限值表示需另行定义契约。本次新增成功样本使用标准 JSON 可表达的载荷，并用 `json.loads(text, parse_constant=reject_constant)` 验收；`reject_constant` 对传入常量直接抛 `ValueError`，避免默认解析器产生假阳性。该严格解析仅用于测试，不改变运行时编码策略。

协调者用临时进程替换序列化函数，未改项目文件，补充验证了推荐路线：

- `read_result` 正文 103,409 字符，120 行完整；SDK 转存文件与正文一致，回读位于第 102,999 字符处的标记，窗口输出 918 字符。
- 沿原分页位置读取，120 行无遗漏、无重复，无需新分页算法。
- `execute_sql` 正文 121,460 字符；转存后的 `Command` 保留 `db_executions`，消息引用与记录中的 `evidence_id/result_ref` 一致，原始结果仍可读取。

这些是离线工具与 SDK wrapper 验证，不是正式代码已修复，也不是所有工具的完整生产图验收。预检脚本、报告和日志位于 `/tmp/insightweave-doc9-preflight-hrpm3lqg/`：`report.md`（rev2）、`real_tools_offline.py`、`paging_probe.py`、`coordinator_complete_json_probe.py` 及各自日志。临时目录不是正式测试资产，实施时把必要断言写入下述项目测试文件，不复制原型作为产品代码。

## 2. 全局约束（Global Constraints）

- 项目未上线，直接删除失效配置和内部旧接口；不保留兼容参数、双分支或历史数据迁移。
- 删除 `results.max_tool_output_chars`，不是把它从硬上限悄悄解释成软上限。
- 所有调用公共序列化函数的返回包，包括 `ok_envelope/error_envelope`，统一使用 `indent=2`。这是有意的格式统一：接受小包增加空白字符，换取单一序列化入口和大包可按行回读；不增加“大小包各用一种格式”的分支。额外体积取决于结构，不采用固定增幅估计；错误包也可能包含超长 message/data，不能假定永远不会转存。
- 保持 `results.preview_rows=20`、`results.read_max_rows=200` 及其他业务行数、检索数量、数据库保存上限不变。
- 保持 `filesystem.tool_token_limit_before_evict=20000`。当前 SDK 在消息正文 **超过 80000 字符**时转存；这是字符近似，不是精确 token 计数。
- 12000～80000 字符的返回可以直接进入上下文；相较现状可能增加 token 用量和摘要频率，本文不宣称更省钱或更快。
- 保持 `Command` 的账本更新、授权 SQL、来源标识、证据接受规则、最终提交协议、Runner、API 和 Vue 行为不变。
- 不把 SDK 转存文件当作新的业务证据源：文件是同一工具返回的另一种读取形式；引用资格仍由本运行账本校验。

**回读边界：** 多行 JSON 必须使用 `indent=2`。`read_file` 按行分页，带状态头/窗口提示，不支持行内字符偏移；紧凑单行 JSON 会使大输出中部无法回读。当前配置下，读取显示文本（含状态头/提示）长度 **≥80000 字符**时，`read_file` 会按字符裁剪并附截断提示；这与工具正文 **>80000 字符**才转存的边界不同。不能指望一次整读拿到完整大文件，应分窗口读取。缩进仍不能拆分一个超过读取预算的字符串，超长单格全文的字符级读取不在本次范围；不能宣称任何尺寸的单格都可通过 `read_file` 读全。常规结果仍优先使用 `read_result(columns=..., offset=..., limit=...)` 或 `analyze_result`，而不是反复整读转存文件。

**替换消息预览：** SDK 的转存提示除路径外，还显示原文头 5 行和尾 5 行，预览中的每行最多显示前 1000 字符，并标明省略/裁剪。当前 `execute_sql/analyze_result` 的缩进布局把 `evidence_id/result_ref` 放在头 5 行内，可直接看到；完整 `mapping`、中部数据及其他返回形态不保证出现在预览里。先看预览，信息不足再读文件，不重新排列字段来迎合预览模板，也不把预览当完整 JSON 解析。`reuse_evidence` 预览之外的映射项须从文件读取；大型 mapping 单元测试证明返回内容完整，不等于已证明模型逐项读回。

**摘要与存储边界：** 当前摘要触发阈值为 96000 tokens。摘要后，早期转存提示可能不再直接出现在模型上下文中；SDK 成功归档被摘要消息时，会在摘要消息中提供 `/artifacts/conversation_history/` 历史文件路径，可从历史中再查原转存路径，并非必然无法回读。历史写入失败或摘要未保留有关引用时，找回细节可能更困难；不保证模型一定主动追溯。本次不增加引用索引、摘要恢复协议或 artifacts 自动清理机制，文件增长与会话存储容量作为既有运维边界记录。`grep` 对超长行仅显示约 2000 字符的命中窗口，也不能替代超长单格全文读取。

## 3. 修改文件清单

路径均相对于 `nl2sql/`。`finalize_output` 共 10 个原调用点、9 个工具；`probe_dimension_values` 有正常与空结果两个调用点。共享 `error_envelope()` 还影响第 10 个工具 `submit_final_answer` 的错误 JSON 布局；其检查、成功回执和门控不变，纳入聚焦回归，不能声称该工具所有返回文本都未变化。

| 文件 | 修改位置与职责 |
| --- | --- |
| `src/modules/db_deepagent/tools/_envelope.py` | 删除 `finalize_output()`；新增完整 JSON 序列化函数；现有 `ok_envelope/error_envelope` 共用它 |
| `src/modules/db_deepagent/tools/schema_tools.py` | `search_schema/plan_joins` 改用完整序列化；删除 `_merge_warnings` 及不再使用的导入 |
| `src/modules/db_deepagent/tools/sql_tools.py` | `validate_sql/execute_sql` 改用完整序列化；保留原 `Command` 更新 |
| `src/modules/db_deepagent/tools/result_tools.py` | `read_result/analyze_result` 改用完整序列化；不删行、不裁列、不改游标 |
| `src/modules/db_deepagent/tools/guard_tools.py` | `resolve_time_range/probe_dimension_values/reuse_evidence` 改用完整序列化；保留全部原始警告与证据映射 |
| `src/modules/db_deepagent/config.py` | `ResultsConfig` 删除 `max_tool_output_chars` 字段 |
| `src/modules/db_deepagent/config/db_deepagent.yaml` | 删除该配置及旧硬截断说明；说明业务行数限制与 SDK 转存各自职责 |
| `src/modules/db_deepagent/config/prompts.yaml` | 增加工具返回文件路径时的按需读取说明，不修改业务守卫规则 |
| `src/tests/db_deepagent/unit/test_envelope.py` | 将旧截断断言替换为 JSON 完整性与不修改入参断言 |
| `src/tests/db_deepagent/unit/test_config.py` | 核对配置字段删除、真实 YAML 可加载 |
| `src/tests/db_deepagent/unit/test_schema_tools.py` | 覆盖检索列表、JOIN 全目标结果及警告完整性 |
| `src/tests/db_deepagent/unit/test_sql_tools.py` | 覆盖大预览、大校验错误与账本更新 |
| `src/tests/db_deepagent/unit/test_result_tools.py` | 覆盖大页、行列对应、原分页连续性与类型编码 |
| `src/tests/db_deepagent/unit/test_guard_tools.py` | 覆盖完整证据映射、取值、时间边界及空结果警告 |
| `src/tests/db_deepagent/unit/test_reuse_ea.py` | 在现有执行/分析证据沿用用例旁补大型 E/A mapping，逐项核对消息新 ID 与账本一致 |
| `src/tests/db_deepagent/unit/test_final_answer.py` | 聚焦回归提交工具的错误 JSON 语义、接受记录及门控前提；现有覆盖充分时无需为缩进单独新增测试 |
| `src/tests/db_deepagent/unit/test_tool_output_offload.py` | 复用现有生产后端/权限假件，补结果类工具转存、回读及 `Command` 验证 |
| `docs/2_DeepAgents_0.7.34新特性与项目收益分析.md` | 实施完成后同步 §5.3 的其他工具状态；真实模型证据按工具与受控范围分别表述 |
| `docs/4_DeepAgents实现简化与旧链保留价值分析.md` | 实施完成后更新 §4.1 状态与新契约；不提前标记完成 |
| 本文 | 记录实施、审核、命令与真实验证边界 |

`inspect_tables` 已按 doc 7 返回完整缩进 JSON，本次不重写。`ResultStore/SqlPipeline`、SDK 装配及阈值无需改动。`docs/v4`、doc 7 与升级探针是历史版本记录，不逐份改写成当前实现；尤其旧探针依赖已删除函数时，应按其历史基线使用，不新增兼容壳让它继续调用旧语义。

## 4. 按顺序实施

### 步骤 1：固定缺陷与行为不变量

- [x] 在 `test_envelope.py` 写入原始字段完整、不修改入参的断言；旧函数下先检查 `json.loads` 失败或字段被裁的 RED，不能只检查长度。
- [x] 在 `test_result_tools.py/test_sql_tools.py` 使用现有假服务和 `invoke_tool_node`，分别复现大页、大预览；同时核对账本和 `result_ref` 原始结果完整。
- [x] 为 `test_result_tools.py` 增加独立行列错位 RED：真实 `read_result` 返回 140 列、2 行，每列名为 `f"c{i:03d}_" + "x" * 50`（55 字符），每行 140 个带位置标记的值。确保旧函数首次序列化 **>12000**，裁为 5 列后 **≤12000 且仍可严格解析**。RED 必须失败于列数/对应值断言，而不是 `JSONDecodeError`；修改后逐项比较原列顺序、行宽及位置值。仅 helper 形态的已复现样本为 14969 → 3089 字符，真实工具含分页元数据，长度应在测试内实际测量。
- [x] 在 `test_guard_tools.py` 补时间/维度沿用映射，在 `test_reuse_ea.py` 补执行/分析（E/A）大型 mapping；逐项核对新 ID 与账本。`plan_joins` 全目标结果用例写入 `test_schema_tools.py`。这些用例使用真实构造器，不仅用同形字典代替。
- [x] 修改现有 `test_distinct_truncated_warning`，精确断言 `payload["warnings"] == ["该列取值多于 limit（2），只有列出的值有证据"]`，不能只用 `any(...)`；当前重复添加路径应 RED。正常命中和空结果路径均断言 `warnings == []`。另由序列化单测锁定多条 warnings 的原顺序，不引入全局 `set` 去重或排序。
- [x] 保存 RED 命令和失败原因。模型调用、真实业务库与包升级均不参与这些测试。

### 步骤 2：删除截断管线，统一序列化

在 `_envelope.py` 定义唯一的新接口：

```python
def serialize_tool_output(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
```

此函数只负责序列化与布局，不递归修复上游值类型；“完整”不扩大为对 §1 既有编码边界的保证。

- [x] 删除 `finalize_output(payload, max_chars)`，不保留旧签名或别名。
- [x] `ok_envelope()`、`error_envelope()` 保留参数与返回结构，组装字典后调用 `serialize_tool_output()`；只统一文本布局，不改变错误类型、重试属性或最终答案提交规则。
- [x] 删除 `schema_tools._merge_warnings()`；新函数不修改 payload，不需要 `deepcopy`，也不再返回单独的截断警告列表。
- [x] 原业务 warnings 必须在序列化前组装完成。`probe_dimension_values` 的 warnings 已在 payload 中，移除旧“再次并回 warnings”的操作，避免人为重复添加；原警告顺序与内容按工具当前语义保留。

### 步骤 3：逐个替换 10 个调用点，删除失效配置

- [x] `search_schema/plan_joins/validate_sql/execute_sql/read_result/analyze_result/resolve_time_range/reuse_evidence` 尾部统一为 `content = serialize_tool_output(payload)`；保留各自 string 或 `tool_command(..., updates=...)` 的返回方式。
- [x] `probe_dimension_values` 两条路径都用新函数，删除 `[0]` 取元组内容和后置 `_merge_warnings`。
- [x] `read_result` 原样返回 `page.rows/page.columns/page.offset/page.next_offset`；不新增消息层删行、游标修正或空页回退逻辑。
- [x] `execute_sql/analyze_result` 保留业务预览行数和 `preview_truncated/source_truncated` 的原计算；保留 `error` 对象、完整 `error_codes`、证据引用和所有账本更新。
- [x] 保留现有上游局部限制：检索 SQL 样例 1500 字符、EXPLAIN/展示用 executed_sql 3000 字符等。本方案的“完整”指进入统一返回包时的内容，不恢复这些上游已限制的数据，也不改变 SQL 授权逐字匹配规则。
- [x] 同一组改动中删除 `ResultsConfig.max_tool_output_chars` 与 YAML 配置，更新全部生产引用与测试。无需为被删除字段设置默认值或迁移逻辑；不要在真实 YAML 与模型不一致的状态停留。
- [x] 将步骤 1 测试转为 GREEN；旧测试中“必须裁为五项/必须短于 300 字符”的断言应替换，不保留互相矛盾的验收标准。

### 步骤 4：补充使用说明与 SDK 集成测试

- [x] 在 `prompts.yaml` 的“结果”规则中加入：工具返回 `/artifacts/large_tool_results/` 路径时，先看附带预览，信息不足再用 `grep/read_file` 查找并分窗口读取，再使用返回中的真实引用；文件路径本身不是 `result_ref/evidence_id`。分析结果被转存时，先从预览或文件取得 `result_ref/evidence_id` 再继续分页、分析或提交；跨运行沿用仍遵循原 `reuse_evidence` 规则。结果分页和计算继续用业务工具，不能把已保存部分冒充总体。
- [x] 在 `test_tool_output_offload.py` 复用现有生产 backend、permissions、FilesystemMiddleware 假件，对真实 `read_result` 与 `execute_sql` 大输出分别验证正文、转存文件和读取窗口。
- [x] `analyze_result` 不单独复制完整图转存用例：SDK 对同类 `Command + ToolMessage` 的转存与状态保留机制由上述 `execute_sql` 代表性验证；在 `test_result_tools.py` 用真实分析工具的大预览断言不删行、不裁列，并核对 `db_analyses`、消息中的引用与结果存储一致。这不等于单独验证了分析下推大输出的完整图路径。大错误包由错误信封保字段单测与通用 SDK 大消息/Command 用例分层覆盖，本次不另增一套大错误包全图测试。
- [x] 至少增加一个完整生产图离线业务闭环用例：局部动态假模型执行 `validate_sql → execute_sql → 从替换消息提取路径 → read_file → submit_final_answer`。目标业务数值或中部标记必须位于头尾预览之外，提交内容必须由实际 `read_file` 窗口解析所得；只调用一次 `read_file` 但不使用内容不能通过。引用可从实际预览取得，并与本 run 账本核对，不预先写死路径或证据 ID。明确该用例证明“读回后部数据，并用合法引用提交”，**不证明从文件恢复了引用**，不额外增加大型 mapping 的全图恢复链路。保存实际模型请求，断言替换消息不含目标数据、模型请求未直接包含原始完整正文；最终答案通过门控。保留漏提交提醒可能追加模型轮次的脚本余量。
- [x] 原 doc 7 的阈值边界、同步/异步 `Command`、写入失败及 `inspect_tables` 回归继续通过；不重写 SDK 转存实现。

### 步骤 5：验证、审核与记录

工作目录 `nl2sql/`，使用 `.venv/bin/python`；必要时移除本次命令的 SOCKS 代理变量，不修改用户环境。

```bash
env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest src/tests/db_deepagent/unit/test_envelope.py src/tests/db_deepagent/unit/test_config.py src/tests/db_deepagent/unit/test_schema_tools.py src/tests/db_deepagent/unit/test_sql_tools.py src/tests/db_deepagent/unit/test_result_tools.py src/tests/db_deepagent/unit/test_guard_tools.py src/tests/db_deepagent/unit/test_reuse_ea.py src/tests/db_deepagent/unit/test_final_answer.py src/tests/db_deepagent/unit/test_tool_output_offload.py -q
env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest src/tests/db_deepagent -q
```

- [x] 对修改的 Python 文件运行 Black/Ruff，并执行 `git diff --check`；区分既有 lint/仓库测试收集错误与本次新增失败，不宣称全仓库已全绿。
- [x] 按用户指定方式进行代码审核，核实问题后修改；本方案不授权自动提交、推送、重启或真实付费调用。
- [x] 更新 doc 2 §5.3、doc 4 §4.1 与本文实施记录：同步修改“其他工具保持原状、未全局修复”和“建议 1 仍未实施”，协调原“试点已实施”段落与扩展后的状态；本次仅修复本文定义的截断/裁列问题，不把既有编码边界也写成已修复。沿用真实模型证据分级：doc 7 已验证 `inspect_tables → grep → read_file` 的真实模型+模拟结构受控回读；不等于所有自然问题主动回读。**本次新增结果类大输出与最终提交完整链路仍无真实模型验收**（实施当时状态；其后已按用户授权完成并记录于 §6.6），离线通过后也不能替代此结论。后续真实查询记录工具正文字符数、是否转存、模型/摘要调用次数及 tokens，性能收益须有对照才能判断。

## 5. 审核重点（Review Focus）与验收断言

| 场景 | 必须断言 | 测试归属 |
| --- | --- | --- |
| 旧阈值 11999/12000/12001；中文、引号与换行 | 对本次成功样本，用拒绝非标准常量的严格解析器验证；不按阈值删字段/删尾；解码后字典等于原 payload；入参未变 | `test_envelope.py` |
| 极宽行、单格 20000 字符、Decimal/date 编码 | 原行、列数与类型信封完整；不把数值或超长文本改写成截短标记 | `test_result_tools.py` |
| 旧裁列后仍为合法 JSON | 独立 RED 确认旧输出可严格解析但 5 列对应 140 个单元格；修复后原列序、行宽、对应值全部一致 | `test_result_tools.py` |
| 连续分页至末页 | 按原 `next_offset` 拼回所有已保存行，无跳行/重复；末页仍为 `None`；源截断标志原样保留 | `test_result_tools.py` |
| 超长校验错误与 probe 空结果 | `status/error_type/retryable/message/hint/error_codes/warnings` 均保留；错误不变成功，空结果不变失败 | `test_sql_tools.py/test_guard_tools.py` |
| probe 三条路径警告 | distinct 超限时 warnings 精确等于步骤 1 的单元素列表；正常命中与空结果均为 `[]`；多条序列化警告保持原顺序 | `test_guard_tools.py/test_envelope.py` |
| 大 evidence mapping、JOIN 多目标、维度值 | 完整映射/目标状态/取值与边界可见；不只断言第一个 ID 存在；E/A mapping 逐项核对账本 ID | `test_guard_tools.py/test_reuse_ea.py/test_schema_tools.py` |
| SDK 阈值与多行回读 | 工具正文 80000 不转存、80001 转存；文件与原正文一致；读取显示文本 ≥80000 会被裁剪，回读窗口须小于此预算；读取位于原文第 80000 字符后的标记，不直接解析带状态头的显示文本 | `test_tool_output_offload.py` |
| 返回 `Command` 的工具转存 | 非消息状态更新完整、消息身份保留；账本与原始结果完整；完整图使用实际回读的后部数据及预览中的合法引用提交，原始完整正文未直接进入模型请求 | `test_tool_output_offload.py` |
| 分析结果大预览 | 真实 `analyze_result` 不额外删行/裁列，`db_analyses`、结果存储与返回引用一致；同类 Command 的 SDK 转存由 execute_sql 代表性验证，不重复完整图 | `test_result_tools.py/test_tool_output_offload.py` |
| 共享信封影响最终提交工具 | 错误 JSON 布局可变，错误类型与重试属性、未接受时不写正式答案、成功回执与登记语义不变 | `test_final_answer.py` |
| 删除配置 | 真实 YAML 可加载；运行时无旧字段/函数引用；严格配置模型对重新加入的旧字段报 `extra_forbidden` | `test_config.py` |

## 6. 实施记录

**2026-11-11 实施（Claude Code，Orca worker task_3ea92222cc7f；基线 HEAD `32f329d`）。离线测试全绿；待 Kimi/Codex 双重代码审核，未经审核不得视为最终通过。**

### 6.1 修改文件

生产代码（§3 清单全量，无范围外文件）：

- `src/modules/db_deepagent/tools/_envelope.py`：删除 `finalize_output()`；新增 `serialize_tool_output(payload) = json.dumps(payload, ensure_ascii=False, indent=2)`；`ok_envelope`/`error_envelope` 组装字典后共用它（参数与返回结构不变，仅文本布局统一为 indent=2）。
- `src/modules/db_deepagent/tools/schema_tools.py`：删除 `_merge_warnings()`；`search_schema`/`plan_joins` 尾部改为 `serialize_tool_output(payload)`；`inspect_tables` 未改。
- `src/modules/db_deepagent/tools/sql_tools.py`：`validate_sql`/`execute_sql` 两个调用点替换；保留 `Command` 更新、`_EXPLAIN_PLAN_MAX_CHARS=3000`、`_EXECUTED_SQL_MAX_CHARS=3000` 与授权逐字校验。
- `src/modules/db_deepagent/tools/result_tools.py`：`read_result`/`analyze_result` 调用点替换；`page.rows/columns/offset/next_offset` 原样返回，预览行数与 `preview_truncated/source_truncated` 计算不变。
- `src/modules/db_deepagent/tools/guard_tools.py`：`resolve_time_range`/`probe_dimension_values`（正常与空结果两路径）/`reuse_evidence` 调用点替换；删除 `[0]` 取元组与后置并回 warnings（消除 distinct 警告重复添加）；证据映射/账本写入不变。
- `src/modules/db_deepagent/config.py`：`ResultsConfig` 删除 `max_tool_output_chars`（无默认值/迁移）。
- `src/modules/db_deepagent/config/db_deepagent.yaml`：删除该配置项，注释改为业务行数限制与 SDK 转存各自职责；`filesystem.tool_token_limit_before_evict=20000`、`preview_rows=20`、`read_max_rows=200`、`max_saved_result_rows=10000` 不变。
- `src/modules/db_deepagent/config/prompts.yaml`：§结果 增加两条转存路径使用说明（先看预览、grep/read_file 分窗口读取、路径≠result_ref/evidence_id、跨运行沿用仍走 reuse_evidence）；业务守卫规则未改。

测试（§3 清单 9 个文件 + 状态文档）：

- `test_envelope.py`：删除 `TestFinalizeOutput`（旧截断断言），新增 `TestSerializeToolOutput`（严格解析 roundtrip/入参不变/11999-12001 阈值不裁/warnings 原顺序/大错误包字段完整/indent 布局）；信封测试改用 `parse_constant` 拒绝 NaN/Infinity 的严格解析。
- `test_config.py`：新增字段删除断言（模型与真实 YAML 一致）与重新加回旧字段报 `extra_forbidden`。
- `test_result_tools.py`：新增宽表行列错位 RED（140 列×2 行，实测完整载荷 >12000、旧实现裁 5 列合法 JSON 错位）、单格 20000 字符硬截断 RED、连续分页无跳/重（含 source_truncated 两态）、analyze_result 本地 top_n 大预览（40 行×700 字符、账本/ResultStore/引用一致）。
- `test_sql_tools.py`：新增 execute_sql 大预览（20 行×760 字符）完整性与账本/ResultStore 一致；校验错误字段完整性；顺带修复该文件一处基线 Black 格式问题。
- `test_guard_tools.py`：`test_distinct_truncated_warning` 改为精确单元素断言（旧实现重复添加路径 RED）；新增 exact 命中与空结果 `warnings == []`；新增时间/维度混合沿用映射完整性（逐项对账本）。
- `test_reuse_ea.py`：新增 10+10 大型 E/A mapping（入参上限 20），逐项核对全部新 ID/result_ref 与账本，warnings 精确两条。
- `test_schema_tools.py`：新增 search_schema 大命中（9 表+9 列 >12000）不裁列表 RED；plan_joins 8 目标混合状态全量结果（含桥接边、无路径与错误 warning）。
- `test_final_answer.py`：新增共享错误信封布局回归（indent=2 多行、错误语义/重试属性/未接受不登记不变）。
- `test_tool_output_offload.py`：新增真实 `read_result`（200 行×500 字符）转存逐字相等与按窗口回读；真实 `execute_sql`（20 行×4600 字符）转存 + Command 账本 + serialize 回环一致性；完整生产图假模型闭环 `validate_sql → execute_sql → 提取路径 → read_file → submit_final_answer`（提交内容使用头尾预览之外的实际回读标记；引用来自预览头 5 行的 evidence_id 并与账本核对；模型请求证据：完整原文与未裁剪单元格从未直接进入任何一轮请求）。
- 文档：doc 2 §5.3、doc 4 §4.1 状态更新（保留原文与既有未提交内容，追加实施状态与覆盖界限）；本文 §4 复选框全部勾选并写本节。

### 6.2 命令与结果（`nl2sql/`，`env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest`）

| 命令 | 结果 |
| --- | --- |
| 9 个聚焦文件（基线） | 110 passed（`logs/baseline-focused.log`） |
| 9 个聚焦文件（RED） | 12 failed + 1 collection error（`serialize_tool_output` 未定义/字段仍在/裁列/硬截断/警告重复/无转存路径，逐项符合 §5 预期；`logs/red-final.log`） |
| 9 个聚焦文件（GREEN） | 132 passed（`logs/green-focused.log`） |
| `src/tests/db_deepagent -q` 基线 → 实施后 | 637 passed, 28 skipped → 659 passed, 28 skipped（`logs/baseline-db-deepagent.log` / `logs/green-final-db-deepagent.log`） |
| Black（15 个改动 Python 文件，apply） | 全部通过（基线仅 test_sql_tools.py 一处格式问题，已随本次修改修复） |
| Ruff（同 15 文件） | 与基线逐条比对：全部为基线既有项（UP045/UP017/I001/FLY002/RUF022/BLE001/RUF059 等），无新增 |
| `git diff --check` | 干净（exit 0） |

既有基线问题未扩修：全仓库 collect 错误 `src/tests/unit/tools/test_join_planner.py`（`build_multi_base_join_plans` ImportError）为基线状态，与本任务无关，未触碰；lint 基线项保留原样。

### 6.3 验收边界（未变的事实）

- 本轮全部为离线假件（FakeExecDb/FakeMilvus/ScriptedChatModel/生产 backend+permissions+FilesystemMiddleware），无真实 LLM、真实业务库、服务重启、git 提交/推送、依赖安装。
- 完整生产图闭环用例证明"读回后部数据并用合法引用提交"，不证明从文件恢复了引用（引用取自预览头 5 行的 `evidence_id`，与账本核对一致）。
- 结果类大输出与最终提交链路仍无真实模型验收（本节为开发/审核当时状态；其后 §6.6 已补充真实模型受控验收通过——真实 LLM＋模拟业务数据，仍非真实业务大输出）；doc 7 的真实模型+模拟结构受控回读仅覆盖 `inspect_tables → grep → read_file`，该证据保留不扩大表述。
- §1 既有编码边界（NaN/Infinity 非标准常量、嵌套非 JSON 类型的递归编码）未修：未加 `allow_nan=False`/`default=str`；`parse_constant` 严格解析仅存在于测试。多行 JSON 的超长单行单元格仍受 `read_file` 显示文本 ≥80000 裁剪约束，需分窗口读取。
- 12000～80000 字符返回直接进入上下文的 token/费用影响未测量，不宣称更省钱或更快。

### 6.4 Kimi 第 1 轮审核修复（2026-11-11）

verdict `requires_changes`（报告 `reviews/kimi-r1.md`），两项均为测试证据缺口，生产代码未改：

- **K1-1（采纳）**：`_ResultChainModel` 的 read_file 分支改为从窗口实际解析提交值（模块级 `_WINDOW_VALUE_RE` 定向正则，未引入通用解析框架）；解析失败时如实提交"未取得"，不回退预置 marker（marker 字段仅供外层断言）。新增 `TestResultChainModelWindowParsing`：含标记窗口提交窗口值、**不同窗口值提交该值而非预置 marker**（因果对照）、**缺标记窗口不提交预置值**（Kimi 复现场景反向检查）。完整图用例增加 `_submitted_window_value == marker` 断言；消费/合法引用/门控/模型请求正文未直接进入的既有断言保留。
- **K1-2（采纳）**：`_tools_node_with_file_mw` 增加可选 `captured_raw`——在 SDK 转存前截获同一次工具调用的原始 ToolMessage.content；execute_sql 转存测试改为断言**转存文件与同一次调用原正文逐字相等**（替换原 serialize 自回环替代证据），未引入第二次执行；账本与 ResultStore 断言保留。doc2/doc4 的"逐字回读"表述由此补齐为真实断言，无需收窄。

验证：9 聚焦文件 135 passed（+3 窗口解析用例）、`src/tests/db_deepagent` 662 passed/28 skipped、Black/Ruff/git diff --check 干净（`logs/kimi-r1-fix-*.log`）；`test_tool_output_offload.py` 新 hash `01b4b679364127971777383a95acdc036e50db6a`，其余 19 个文件 hash 与 §6.1 交付一致；doc6 未改。修复报告：`/tmp/insightweave-doc9-implementation-vixwi_hq/claude-kimi-r1-fix-report.md`。待 Kimi 第 2 轮审核。

### 6.5 双重代码审核结论（2026-11-11）

- **Kimi：第 2 轮通过。** 第 1 轮两项测试证据缺口已修复；复审确认窗口值的因果对照与同次调用原正文逐字比较有效，未发现新增可操作问题。报告：`/tmp/insightweave-doc9-implementation-vixwi_hq/reviews/kimi-r2.md`。
- **Codex：第 1 轮独立审核通过。** 核对本文与生产/测试差异、完整返回、警告、业务行数和分页、证据账本及 SDK 转存链路，未发现需要返工的问题。报告：`/tmp/insightweave-doc9-implementation-vixwi_hq/reviews/codex-r1.md`。
- **独立验证：** `env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest src/tests/db_deepagent -q` → **662 passed, 28 skipped**；15 个修改 Python 文件的 Black `--check` 和 `git diff --check` 均通过。既有 Ruff 项及全仓 `test_join_planner` 收集错误未扩修，不代表全仓测试全绿。
- 两阶段均未超过 3 轮，无需用户介入裁定。Orca 对自定义 pane 的后续编排返回 `agent_unconfigured`，核实无未决派发后复用原 pane 终端降级；Kimi 直接反馈受 `consumer_fenced` 限制，由协调者转交，不重复派发。
- 本结论限于本文代码修改与离线验收（审核当时状态；其后 §6.6 已补充真实模型受控验收通过）；§6.3 的超长单行、类型编码和费用/性能等其余边界继续有效。未推送或重启服务。

### 6.6 真实模型受控验收（2026-11-11，用户授权）

**结论：passed（一次通过，无重试）。** 新增 opt-in 集成测试 `src/tests/db_deepagent/integration/test_model_result_tool_offload.py`（默认 skip，`--run-llm` 显式运行）：生产 `build_db_agent`（真实主 LLM `qwen3_8_max`、真实校验执行管线、ResultStore、Command 账本、SDK backend/permissions/filesystem 中间件、最终答案门控、生产提示词含 §4 步骤 4 的转存指引）＋临时工作区，仅业务数据源为受控假件（public.orders 固定 20 行）。

- **场景：** 目标值 `NOTE12-<随机 hex>`（短标量）位于 execute 正文（96179 字符、200 行）第 125 行中部，不在头尾 5 行预览，不在问题文本；任务要求全量取回并在结果正文中核对 id=12 的 note 原文后引用 evidence_id 提交。随机值、路径、行号、引用 ID 均未写入问题或脚本化工具调用。
- **实际工具序列（模型自决）：** `inspect_tables → validate_sql → execute_sql（96179 字符被转存，替换消息仅 929 字符）→ read_file(0,50)（未命中）→ grep（未给出内容）→ read_file(50,100)（55434 字符窗口命中）→ submit_final_answer`。模型首次窗口与 grep 未命中后自行改读后续窗口，回读参数完全自定。
- **证据（`logs/real-model-evidence/`）：** 目标值在前 6 轮模型输入中均不可见，仅在第 7 轮经 read_file 窗口进入；完整 96179 字符正文从未直接进入任何模型请求（最大工具消息 55434 字符）；转存文件与同次调用原正文逐字相等（快照 `offload-file-snapshot.json`，git hash `774c28d9…`，解码 96179 字符一致）；最终答案引用目标原文与 `E-13f356af`，与本 run `db_executions` 账本及 ResultStore（row 12 note 后端真值）一致，门控通过。
- **观测：** 7 次模型调用（上限 12）、摘要 0 次、23.5 秒、输入 93438 / 输出 1121 tokens、usage 完整、无异常；预算经生产 RunBudgetLedger 注册控制。
- **边界：** 本验收为**真实 LLM＋模拟业务数据**的受控回读，不是真实业务大输出样本；不证明所有自然问题都会主动回读、不证明费用/性能优化，也不扩大到大型 mapping 逐项恢复或 analyze 大输出完整图（§6.3 其余边界不变）。离线准备脚本与命令证据：`real-model-prep.py`、`logs/real-model-prep.log`、`logs/real-model-run1.log`、`logs/real-model-post-regression.log`；回归 `src/tests/db_deepagent` 662 passed / 29 skipped（新增 1 个默认 skip 的 opt-in 用例）。转存文件原始路径位于 pytest 临时目录（临时资源），关键证据已快照至审计目录。
