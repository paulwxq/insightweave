# doc 6.3 实施审核 R3 / 最多 4 轮

日期：2026-11-13。审核者：Codex；本轮实现由用户授权的新工具接手，通过用户人工传递结果，不操作原 Orca pane。

**结论：REQUEST_CHANGES。确认 5 组待修问题：1 项 P1、4 项 P2。** 多数 R2 原反例已关闭；剩余主要是有输入血缘的合法沿用、阻塞决策历史语义，以及清理闭包的根集合。

按用户本轮裁定，已实现且验证成立的复杂能力保留。本次没有发现必须因 DeepAgents API 限制而放弃的能力；下列问题均有具体修法和有限、可执行的验收条件。暂不建议削减批准方案，也不以“已经开发较久”作为拒绝理由。

## 1. 基线、范围与实际验证

- HEAD：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`，审核对象为全部未提交实现及新增文件。
- 批准方案 SHA256：`c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，复核未变。
- 实施请求：[`doc63_impl_review_request_r3.md`](doc63_impl_review_request_r3.md)。
- 审核期间只读业务代码；没有修改实现、测试或批准方案，没有真实资源删除、真实模型或数据库调用。

Codex 实际执行：

| 验证 | 结果 |
| --- | --- |
| db_deepagent unit + llm_factory + API v2 全量 | **1299 passed / 4 skipped**，12.29 秒 |
| 前端全部现有 nl2sql `.test.cjs` / `.test.ts` | **57 passed / 0 failed**，包含 6 项新增行为测试 |
| `vue-tsc --noEmit` | 退出 0，无错误 |
| `git diff --check` | 通过 |
| 四份 R3 离线正反例探针 | 均完成，退出 0；发现内容见下文，不以脚本退出码代表业务通过 |

后端命令（在 `nl2sql` 目录）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
```

前端命令（在 `frontend` 目录）：

```bash
node_modules/.bin/vue-tsc --noEmit
node --test $(rg --files src/modules/nl2sql -g '*.test.cjs' -g '*.test.ts' | sort)
```

Codex 已独立重放以下 `/tmp` 探针（在 `nl2sql` 下）：

```bash
PYTHONPATH=. .venv/bin/python /tmp/doc63_r3_lineage_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_gate_r3_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r3_lifecycle_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r3_closed_paths_probe.py
```

探针使用真实 ToolNode、业务工具、TaskLedger/reducer、管线、分析/结果服务、内存 Store 和临时目录；业务数据库为 FakeExecDb。生命周期仅 dry-run，不删除真实文件。损坏历史来源用例明确是故障注入，其余正常操作路径不依赖构造伪造最终答案来复现。

## 2. 待修问题及具体建议

### R3-01 · P1 · 合法分析成果沿用后，历史输入被按新目标任务重新解释

**位置**：`src/modules/db_deepagent/services/reuse_qualification.py:345–397`；`tools/guard_tools.py:856–873`；`tools/result_tools.py:277–320`。

**正常路径反例**：旧 run 的 T1 查询 E → 同任务分析 A → completed 选择 A；新 run 登记 T1 → reuse A 返回 ok → 登记 completed 被拒：

```text
VALID_A_REUSE
reuse=ok
completed=stale_task_inputs:
  来源 新A 的跨任务输入未在其声明依赖中
consume_reused_a=stale_task_inputs:
  输入血缘不可核验（中间引用缺失或成环）
```

沿用记录保留了源 A 的原始 `input_result_refs`，这是方案 §6.4 的要求；新图核验随后却用新目标 task/run 作为 consumer，要求目标声明对历史 run 输入的本轮依赖。再次分析沿用 A 时，旧 `_validate_input_lineage()` 仍只在本轮裸 evidence_id 字典中找历史输入，形成另一处误拒。当前成功正例主要覆盖没有 inputs 的 E 沿用链，未覆盖这一类正常 A。

**建议修法**：

1. 明确沿用记录是对原证据的受控包装。目标身份在本轮核验；继承输入在原证据所属 consumer scope 中核验。
2. 校验继承的输入事实与权威来源一致，再递归原来源图；不删除血缘，也不要求将历史 run 加入本轮 DAG。对于带 inputs 的 SQL 成果沿用采用同一规则。
3. 分析入口复用完整身份的解析/核验逻辑，收敛裸 ID 的另一套递归实现，保持上游选定成果与同任务探索的既有区分。

**验收**：真实 `query → A → completed → reuse A → completed → submit → Runner`；继续消费沿用 A 做分析；同线程跨 run、跨线程完整快照和多跳；同时保留缺源、伪造继承输入及来源失效拒绝。不能仅让 reuse 工具返回 ok。

**是否建议放弃**：否。无 inputs 的完整沿用路径已实测成功；问题是原来源与目标作用域混用，修正规则和入口即可，不是 SDK 不支持。

### R3-02 · P2 · SQL 占位符未在副作用前核验选定成果及完整祖先

**位置**：`src/modules/db_deepagent/services/sql_pipeline.py:116–189`，尤其 `138–170`；最终拒绝发生在 `services/reuse_qualification.py:388–390`。

**正常路径反例**：上游 T1 产生 E1、E2，只选择 E2 completed；下游 T2 声明依赖 T1，SQL 占位符消费未选中的 E1。实际：

```text
UNSELECTED_PLACEHOLDER_SIDE_EFFECT
validate=ok, execute=ok
new_explains=1, new_executes=1
completed=stale_task_inputs: 跨任务输入不是上游选定成果
```

resolver 只检查所属版本及声明依赖，未在查库前检查选定成果/完整祖先。最终门控拒绝是有效防线，但不能撤销已执行的 EXPLAIN、业务查询、预算消耗和保存结果。违反方案 §6.1、§6.3 的消费前置条件。

**建议修法**：对解析出的完整 lineage 在 EXPLAIN/授权及实际执行之前调用共享输入资格规则。跨任务必须是当前上游选定成果；同任务探索输入仍可合法使用；全部祖先必须可核验。保留 completed 和提交的防御复核。

**验收**：未选成果及失效祖先在 validate/execute 都拒绝，新增 EXPLAIN/DB 执行/授权/结果均为 0；选定上游成果和合法同任务输入继续成功。

**是否建议放弃**：否。入口和血缘已经存在，缺的是副作用前接入同一规则，不需要新增调度机制。

### R3-03 · P2 · 畸形或旧协议血缘被当成“没有边”，历史坏来源仍可提交成功

**位置**：`src/modules/db_deepagent/services/reuse_qualification.py:53–93`，尤其 `61–63,76–80`。

`evidence_edges()` 对非 Mapping 输入、缺 evidence_ref 的 input/direct 直接跳过。下列三类故障注入的已封存来源均经真实工具完成 `reuse → completed → submit`，最终被接受：

```json
{"input_result_refs":["res_old_string_protocol"]}
{"input_result_refs":[{"result_ref":"res_missing_input"}]}
{"direct_reuse_from":{"source_task_ref":{"sub_task_id":"MISSING","revision":1}}}
```

这不是声称正常工具主动生成这些结构，而是方案 §6.3、§6.4、§8.2 明确要求的旧协议/损坏来源拒绝。当前解析器把失败变成零条边，图看起来合法；清理也共用此解析器。

**建议修法**：严格验证血缘容器、每条边以及该类边必需的 evidence/task/result 引用。正常 `[]` 和合法的无 direct 来源可以为空；存在但畸形的结构必须返回受控资格错误。读取/最终门控应拒绝，清理应记录问题并保守停止，不让新异常穿透 Runner 破坏 6.2 兜底。

**验收**：以上三例及错误容器类型、缺任务引用明确拒绝；正常空输入、合法完整边仍成功；通过清理控制流验证解析失败不会降级删除。

**是否建议放弃**：否。是共享解析函数的局部严格性修正，不需要兼容或重建旧协议。

### R3-04 · P2 · 阻塞决策仍未形成一致的历史契约：合法操作误拒，坏依据又可漏过

**位置**：`src/modules/db_deepagent/services/task_ledger.py:340–382`；`services/task_completion.py:154–183,375–441`。

同属 R2-05/06/13 的历史决策规则剩余部分，合并报告，避免按每种表现再拆工作项。

**正常路径反例 A**：T1 needs_clarification → T2 failed/dependency_blocked(T1@1) → void T1。TaskLedger 与实际 reducer 全部成功，T2 决策上下文前后相同，最终 failed 却拒绝“阻塞根因与固定历史依据不符”。原因是最终规则读取了同 revision 已变为 voided 的记录，与旧快照比较。**固定 revision 不等于状态不可变化。**

**正常路径反例 B**：T1@1 澄清、T2 因其 blocked；随后 revise T1@2 并 failed，原因 NEW_ROOT；新增 T3 依赖 T2@1，登记 T3 blocked。登记成功却保存 T1@2/NEW_ROOT，T2 原决策仍是 T1@1/澄清，最终拒绝。生成端仍按逻辑 ID 取最新 candidate，复核端按固定版本，二者不一致。

**防御反例 C/D**：从正常登记状态将 `dependency_snapshots=[]`、`decision_plan_version="forged"`，仍 accepted；或者给旧 blocked_by 追加另一个后来才失败的声明依赖，虽未被原决策依据覆盖，也 accepted。当前只检查 dependency_snapshots 键存在，只追第一个根。

**建议修法**：

1. 生成与复核共用决策时引用规则。新决策读取最终候选；遇到已封存 blocked 节点，读取并核验其固定决策依据，不沿逻辑 ID 追到新版。
2. 历史复核以不可变的决策事实为准。对于同 revision 后来 void，可在转换时保留原终态事实/转换审计，或使用已有服务端封存的终止依据建立可复核链；不把旧 NC/failed 重新与当前 voided status/reason 强制相等，也不接受未经检查的模型自报快照。
3. 校验决策版本为合法且不超当前计划的整数；逐项检查依赖快照的身份、唯一性、状态与 blocked_by 的覆盖关系。每个声称的阻塞来源在当时都有 failed/NC 的依据，不能只验证首个 root。

**验收**：A/B 正常路径接受，C/D 拒绝；旧决策深比较不变，正反序批量终止结果一致，来源后续 revise/void 不反向改写历史。使用真实 TaskLedger 输出与 reducer，不能只手工拼最终快照证明正确。

**是否建议放弃**：当前否。reducer 合法 void、深复制及固定 revision 依赖快照的原反例已修好，剩余集中在决策时事实与当前状态的区分。上述契约可具体实现并测试。如果 R4 仍无法同时做到合法历史接受和坏依据拒绝，再由用户裁定是否限制“已终止计划后继续修订/扩展阻塞链”；收紧会限制动态重规划，不能静默删掉。

### R3-05 · P2 · 没有保留根的过期证据链仍自发进入保护集合，无法回收

**位置**：`src/modules/db_deepagent/persistence/maintenance.py:585–635`，尤其 `611–620,624–627`。

全图 `_walk()` 遍历每条原始证据，并无条件将其来源结果加入 protected；随后 result_ref→snapshot 逆边又把相关快照保全。于是图只要有 input/direct 关系，就可能在没有任何 QA/run_errors/携带根时保护自己。

**反例**：400 天前的同一 run 内 query E→analysis A，无 QA/run_errors/携带根，结果也已超过保留期；实际根集合均为空，但 protected 有两个结果、orphan 为空，实际清理 dry-run 无候选。两个 run 的 direct 沿用链也有同样问题。

```text
orphan_input_chain_permanently_protected
root_refs=[], root_snapshots=[]
protected=[resA,resB], orphan_snapshots=[]
result_delete_candidates=[]
```

**建议修法**：分离“图是否合法”的检查与“哪些资源受保护”的标记。检查阶段不向 protected 写入；保护集合只从最终保留根出发，沿必要来源关系扩展。不要让全图扫描或资源相同本身创造新的保留根。保留目前根/图读取失败时停止该会话清理的修复。

**验收**：无根 input/direct 链可正常成为到期候选；有保留根时递归来源和快照全部保护；移除最后根后可回收；未知协议/缺源/环仍报告并按约定保守停止。通过实际清理控制流验证，不能只检查 problems 为空。

**是否建议放弃**：否。核心是有根图遍历的起点与标记规则，可局部调整，无需放弃来源保护。

## 3. R2 各项闭环情况

| R2 项 | 本轮判断 |
| --- | --- |
| 01 合法沿用/可信会话 | 无 inputs 的 E 完整路径已独立验证成功；有 inputs 的 A 仍有 R3-01。可信 ctx 身份传递已补。 |
| 02 完整图/全部成果 | ghost、原缺源、wrong thread 等原反例已修；跨任务分析未选成果拒绝有效。剩余 R3-01/02/03。 |
| 03 清理失败停止 | 原读取失败路径已正确停止且零删除候选；无根链回收回归见 R3-05。 |
| 04 终态 void | 原问题关闭；三个终态到 voided 经实际 reducer 成功，非法覆盖仍拒绝。 |
| 05 历史与输入污染 | 深复制和旧快照不改写已修；合法后续 revise 登记通过。历史阻塞同 revision void 的语义仍见 R3-04。 |
| 06 新终止快照 | 旧 revision 取错状态、三跳保存占位原因的原反例关闭；历史阻塞链后续扩展见 R3-04。 |
| 07 finding 正文 | 原问题关闭；实际 finding 文本进入确定性正文，失败/澄清说明保留。 |
| 08 无提交兜底可信度 | 原反例关闭；缺成果不再被确认完成，逐项 verified 集合生效。测试边界见 M1。 |
| 09 内部分析 guard | 已独立验证工具无 lease 拒绝、不写 A，内部服务也拒绝；有效凭据正例通过。 |
| 10 携带 thread | 已独立验证实际 ResultMeta 下 origin/listing 保留新记录 t-middle/run-mid 身份。 |
| 11 沿用失败归属 | 已独立验证 failure_ref 带目标身份，原样登记 failed/tool_failed 成功。 |
| 12 interrupted_tasks | 原反例关闭；pending/in_progress 及跨 run 过滤通过。 |
| 13 阻塞审计 | 原“完全缺字段/首根伪造”拒绝已补；其余合法历史及逐项依据复核见 R3-04。 |
| M1 前端真实行为 | 采纳有效，新增测试实际执行 snapshotTurn 与编译后的 MessageList，已复跑 6 项行为测试及全部 57 项前端测试。 |

## 4. 非阻塞建议与验证边界

**M1：异常类型参数化并未实际注入异常。** `test_doc63_r2_regressions.py:1696–1718` 的 stop 三种参数执行同一纯规则/摘要路径，stop 只参与末尾枚举断言。因此该测试证明“无提交时缺成果不被当成功”，不能单独证明预算停止和模型异常的 Runner 集成路径。建议分别注入真实 budget snapshot / FakeAgent.invoke 异常，断言原状态优先级、正文、归档。既有 Runner 回归仍已通过，此处不另列为新增阻塞功能。

真实 PG 跨进程锁与 DELETE、真实模型题集/预算余量、仓库外消费者及浏览器人工验收未执行。前端 Vue 渲染行为测试是有效证据，但不等于浏览器验收。上述验证缺口不被伪装成实现错误，也不能被 1299 项离线测试替代。

## 5. 下一步与范围裁定建议

建议保留已经通过的任务门控、合法终态作废、普通证据沿用、可信会话/guard、失败引用、确定性正文与前端历史问题列表；本轮不要求拆除这些能力或再改产品范围。

剩余 5 组均有明确根因和可执行修法，建议针对性完成后提交 **R4/4**。优先完成 R3-01 和 R3-04 的共享语义，再收口 SQL 输入与严格边解析、根集合清理；避免每个入口再增加一套独立遍历规则。

实现者应逐项核实，修复则给回归，不接受则给具体反证，写 `doc63_impl_review_request_r4.md`，暂停业务写入，由用户人工通知 Codex。R4 为本次最后一轮；若仍有未解决的正确性问题或需要改变保证范围，停止自动循环，明确列出剩余问题、修复障碍、缩减功能的具体代价，交由用户决定，不进入 R5。
