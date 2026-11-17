# doc 6.3 实施审核 R2 / 最多 4 轮

日期：2026-11-13。审核者：Codex；实现者：原右下角 Claude Code。

结论：**REQUEST_CHANGES**。确认 **13 项待修问题：3 项 P1、10 项 P2**，另有 1 项非阻塞测试建议。下列多项是 R1 问题的剩余部分；不是新增产品需求，也不要求扩展 6.3 范围。

## 1. 基线与验证

- HEAD：`50067ce`，审核未提交实现，包含新增文件；没有修改业务代码或已批准方案。
- 方案：`docs/6.3_db_deepagent已登记任务终态与答案覆盖门控方案.md`。
- SHA256：`c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，与批准版本一致。
- 实施请求：[`doc63_impl_review_request_r2.md`](doc63_impl_review_request_r2.md)。
- Codex 实际复跑后端全量：**1249 passed / 4 skipped**，12.34 秒。
- 实际复跑新增前端专项：**5 passed**；这些测试是源码正则断言，不是前端行为验收，见 M1。
- `git diff --check` 通过。

后端命令（在 `nl2sql` 下）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
```

Codex 独立重放三份离线探针（退出码均为 0；打印的反例是审核发现，不是测试通过声明）：

```bash
PYTHONPATH=. .venv/bin/python /tmp/doc63_r2_lineage_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_gate_r2_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r2_lifecycle_probe.py
```

前两者使用真实工具/ToolNode、TaskLedger、reducer、共享门控或其正常组合；血缘探针使用真实 SqlPipeline、ResultStore、分析服务与内存 Store，业务数据库替换为 FakeExecDb。生命周期探针使用真实内存 Store、共享 memory guard 和临时合成结果目录；仅 dry-run，不删除真实结果。未调用真实模型、业务库或 PG，不把离线结果表述为真实跨进程验证。

## 2. 必须修正的问题

### R2-01 · P1 · 合法沿用把目标记录当作来源记录校验，无法登记完成

**证据**：`services/task_ledger.py:970–979`、`services/reuse_qualification.py:102–105,214–219`、`tools/final_answer.py:67–70`（路径均相对 `src/modules/db_deepagent/`）。

两个入口将旧的 `direct_reuse_from.evidence_ref` 与本轮新登记的目标 record 一起交给 `verify_reuse_source()`；验证器要求两者 run/evidence 相同，正常沿用必然不满足。提交装配还使用 `state.get("session_id")`，实际 `DbDeepAgentState` 没有该通道，Runner 仅 invoke messages、会话身份在 context 中。

**真实路径反例**：旧 run 查询并 completed → 新 run `reuse_evidence` 返回 ok → 用返回的新证据登记 completed，返回 `stale_task_inputs: 来源记录与引用的 run 不符`。即使批量校验传入正确 session，仍然失败；提交工具装配先得到“来源引用不属于当前会话”。

**建议**：按 direct 引用解析真正的来源原记录，再核验来源资格；新登记记录的目标身份另行核验。提交快照显式接收可信 `ctx.session_id`，不从不存在的 state 字段推断。不能通过删除身份比较来放过错误。

**验收**：增加真实 `reuse → completed → submit → Runner` 的成功正例，并覆盖同线程跨 run、跨线程和多跳来源；四入口判断一致。对应 R1-02，方案 §6.4、§7.1。

### R2-02 · P1 · 完整证据图与全部选定成果仍未核验，派生结果可绕过拒绝

**证据**：`services/reuse_qualification.py:112–114,136–159,181–188,207–212`、`services/task_completion.py:449–455,513–527`；输入消费见 `tools/result_tools.py:219–308`、`services/sql_pipeline.py:138–188`。

当前新增验证器只递归 direct，未遍历 `input_result_refs`；最终所谓“全部主成果”循环只查询 `reuse_failures`，没有逐项检查记录存在及归属。

**已实际重放的反例**：

1. 上游产生 E1、E2，只选择 E2 completed；下游声明依赖后消费未选成果 E1 分析，随后 completed 和 submit 全部成功。这是正常工具操作。
2. 沿用 E 本应被拒绝完成，但使用其 result_ref 占位符产生派生 E，选择派生 E 后 completed、submit 接受，写入最终答案；祖先错误未沿输入传播。
3. A 的源快照含 A/对应 completed 任务，却缺其结构化输入 E 原始记录，新 checkpoint 也无 E；reuse A 仍返回 ok。
4. 注入 `completed.evidence_ids=[E合法,Eghost]`，finding 只引用 E合法，最终仍 accepted=true。
5. 在请求 `[thread1,run1]` 的键下直接注入内部 `thread_id=WRONG`，`persistence/archive.py:255–261` 的 reader 仍返回记录，未核对 thread。

**静态剩余边界**：首跳直接信任可见 source_record，未读取并核对来源快照的原始证据；已有快照却缺任务时会回退 live；内部查找原始证据时取第一条，绕过 archive reader 已增加的重复拒绝。不能仅证明快照里存在一个 completed 任务与相同 evidence_id。

**建议**：使用同一个证据图核验入口，按完整身份解析节点、核对 result/task 引用、检查全部 direct/input 边，拒绝缺源、冲突、未知协议和循环。跨任务消费必须来自上游选定成果；同任务内合法探索输入按方案保留。历史来源须以完整快照和最终有效任务版本核验，已有不完整快照不得被 live 数据掩盖。统一严格 reader，包括 thread 检查与重复拒绝。逐项验证任务选定的全部成果后，再独立验证 finding 覆盖。

**验收**：上述反例逐项拒绝；合法同任务分析、已选上游成果、多跳沿用与删 checkpoint 后完整快照仍可成功。所有入口和无最终提交的收尾复用任务级核验。对应 R1-02 剩余项，方案 §6.3–6.4、§7.2–7.3、§8.2。

### R2-03 · P1 · 清理在保护根读取失败后仍继续删除，闭包未按失败保守停止

**证据**：`persistence/maintenance.py:483–489,509–548,583–608`。

`closure_problems` 只被写入 skipped，随后继续 R2；`_protected_closure()` 返回的 `_problems` 直接丢弃。该函数的 problems 从未追加错误，也未验证根快照是否存在、协议/作用域是否合法，以及 direct/input 来源是否缺失或成环。当前仍是 result_ref 与快照的关联遍历，不能代替完整来源图。

**反例**：真实内存 Store 保存仍在保留期的 run_errors，令 `evidence=[123]`。实际输出同时包含“保护根读取失败: TypeError”与该会话过期结果的 dry-run 待删项；apply=True 分支对应 `shutil.rmtree`。协议 99、缺失 direct 来源或根快照时，闭包 problems 仍为空。

**建议**：保护根或闭包核验出现问题，立即跳过该会话后续结果/PG 结果快照/孤儿运行快照删除并报告。按完整 snapshot/evidence 来源身份追踪 direct/input，验证协议、作用域、存在性及环，不能用不完整闭包降级删除。共享 guard 的修复保留。

**验收**：读取失败、坏协议、缺源、缺根、环各自用真实清理控制流验证；问题会话不得产生实删候选，其他正常会话仍可清理。对应 R1-05 未完成部分，方案 §6.5、§8.2。

### R2-04 · P2 · reducer 禁止合法终态作废，服务层成功的 Command 应用失败

**证据**：`state.py:172–178,195–201` 与 `services/task_ledger.py::_apply_void()`。

completed、failed、needs_clarification 的出边均为空，但独立 void 操作允许这些状态转为 voided。正常 apply 产生更新后交给 `merge_task_records()`，三个分支均抛 `LedgerConsistencyError`。这会阻止“作废已完成下游，再修订上游”的合法流程。

**建议**：区分普通 status 不可恢复终态与 void 合法转移，保持不可变事实检查；TaskLedger 和 reducer 使用一致规则。

**验收**：真实 TaskLedger 输出经实际 reducer 应用，覆盖三个终态到 voided；completed→pending 与同 revision 改 content 继续拒绝。对应 R1-15，方案 §5.1–5.2。

### R2-05 · P2 · 历史终止依据被按最新候选重验，且输入状态被原地改写

**证据**：`services/task_ledger.py:440–443,1206–1279`。

blocked/replacement 的循环遍历全部终态，仍要求历史目标是当前候选版本；并无条件重生成旧 replacement_snapshot。浅拷贝又使候选和输入共享嵌套 termination_context。

**反例**：

- void T1→T2@1 后正常 revise T2，仍 `invalid_task_replacement`。
- T2 因 T1@1 失败/澄清而 blocked 后 revise T1，仍 `invalid_task_dependency`。
- T1 保存替代决策时 T2 为 pending；后来只将 T2 标 failed，T1 不在返回的 task_records 中，但输入 T1 的 replacement_snapshot.status 已变为 failed。

**建议**：新决策根据最终候选校验和生成依据；旧决策仅复核固定历史身份和原决策依据，不要求目标仍当前、不得重生成。候选构造隔离嵌套对象，失败和成功计算均不得直接修改输入。

**验收**：通过正常登记/reducer 路径覆盖 replacement 后续 revise/void、blocked 来源后续 revise；前后输入深比较、旧快照不变。当前 R1-11 测试手工构造最终快照，未经过失败的登记路径。对应 R1-09/11，方案 §4.1、§5.2。

### R2-06 · P2 · 新终止快照混用任务 revision，且早于服务端原因派生

**证据**：`services/task_ledger.py:309–317,1194–1203,1246–1249`。

`_dependency_state_snapshot()` 按逻辑 ID 取当前候选后套用旧引用 revision；依赖快照生成又早于整批阻塞原因派生。

**反例**：A@1 completed，B pending 依赖 A@1；同批 revise A→@2 并终止 B，B 的快照变成 **A@1 pending**，实际历史 A@1 仍 completed。另三跳同批阻塞时，T3 保存 T2 的模型占位 reason=`PLACEHOLDER`，而非最终服务端原因。

**建议**：按固定 revision 从正确的当前候选/历史版本取事实；先完成所有新决策的服务端派生，再封存一致快照，并保持批次顺序无关。

**验收**：旧版本依赖终止与三跳同批终止，打乱操作顺序仍保存相同、真实的任务版本状态和原因。对应 R1-09 剩余项，方案 §5.2–5.3。

### R2-07 · P2 · 正常交付正文仍缺 finding 的实际结论

**证据**：`services/task_completion.py:655–688`、`runner.py:735–737`。

`build_delivery_answer_section()` 注释说展示 finding，函数却仅接收 decision 并输出“已完成：任务目标”，没有输入或遍历 finding.text。

**反例**：合法 completed，answer=`已处理`，finding.text=`销售额为123万元`，门控接受；追加正文仅为“已完成：统计销售额”，实际结论仍不在正文。

**建议**：将合法 findings 纳入共享交付构建输入，实际展示其文本及对应分项；保留原始 payload。保持实时、delivery_answer、历史和 CLI 交付正文一致。

**验收**：模型 answer 不含具体结果、结果仅在合法 finding 中的正例，正文必须含结果；失败/澄清说明继续存在。空白 finding 拒绝的修复保留。对应 R1-13，方案 §7.2、§8.3。

### R2-08 · P2 · 无最终提交时，expected_status 被错误当作任务已核验标志

**证据**：`services/task_completion.py:406–410`、`runner.py:928–935`。

normalized_submission=None 分支在主成果校验之前计算 expected_status；兜底又以 expected_status 非空判断是否可称“已完成”。

**反例**：任务 completed 选 E-a，但执行记录缺失且没有最终提交，输出 `expected_status=completed, errors=[]`，兜底仍写“已完成：统计销售额”。新回归只覆盖有结构合法提交的缺证据路径。

**建议**：任务级成果/血缘核验独立于是否存在最终提交；兜底使用明确的、逐项核验结论，不能以状态映射存在作为可信度证明。保持原运行/预算/持久化状态优先级。

**验收**：没有提交、预算停止、模型异常分别组合缺证据/坏来源/部分可信成果；不得确认未核验任务成功，真实结果和 pending 披露仍保留。对应 R1-14，方案 §7.5、§8.1。

### R2-09 · P2 · 本地分析仍不校验有效运行 guard

**证据**：`tools/result_tools.py:140–150,400–409`、`services/result_analysis.py:338–356`。

声明依赖完整检查已修复，但本地分析工具及内部服务均未检查 lease。真实 ToolNode 将 `ctx.session_guard_lease=None` 传入 analyze，仍返回 ok 并写入 A。

**建议**：在实际本地分析服务入口校验同一会话有效凭据，工具路径复用；无凭据、错误会话、释放后凭据均在读取/写入前拒绝。保留 SQL 管线已增加的服务端检查。

**验收**：不经过 Runner 直接调用内部服务不能绕行；实际分析与 cleanup 使用同一共享 guard。对应 R1-06 剩余项，方案 §6.5。

### R2-10 · P2 · 再次轮换携带仍拼出错误的直接来源 thread

**证据**：`runner.py:1544,1574–1587`。

run/evidence 已取新沿用记录，但 thread/request 仍从 ResultMeta 原生成文件补齐。沿用不生成新结果文件，文件生成线程不是中间登记线程。

**反例**：原结果在 t-probe 生成，真实 t-middle/run-mid 沿用成功包给出正确新引用；再次 `_collect_carried()` 使用生产同款 load_meta 后，却变成 `thread=t-probe, run=run-mid, evidence=新ID`，无法唯一寻址中间来源。

**建议**：保存并携带记录自身的完整可信 scope；原文件生成身份和链首展示保持各自含义，不用于补造新来源身份。

**验收**：至少两次线程轮换，使用实际 ResultMeta 和携带目录返回引用完成下一次沿用，而非人为返回新线程的 meta。对应 R1-07，方案 §6.4。

### R2-11 · P2 · 目标已核验后的沿用失败仍未绑定任务，无法引用登记失败

**证据**：`tools/guard_tools.py:623–665,686,712,745,799`。

`_ea_error(...owned=False)` 的调用均未开启 owned。目标已通过 require_task_ready_for_work 后发生的真实沿用错误，也返回 source_task_ref=None。

**反例**：合法目标沿用另一数据源结果，返回 evidence_datasource_mismatch 和 failure_ref，但 source_task_ref=null；模型按返回原样登记 `failed/tool_failed`，被参数层以 source_task_ref(dict_type) 拒绝。

**建议**：在明确的目标准入边界之后，将业务失败绑定已核验目标；未知目标/probe/准入失败保留无业务归属，不要一律绑定。按已批准规则统一相关正常错误返回的账本路径。

**验收**：正常工具错误包的 failure_ref 原样用于 failed/tool_failed 可通过；未知目标仍不能伪造任务归属。对应 R1-08 剩余项，方案 §5.1、§7.3。

### R2-12 · P2 · interrupted_tasks 因视图参数形状错误始终为空

**证据**：`runner.py:1284–1300`；`services/task_ledger.py:68–95`。

`current_task_view()` 要求任务 mapping，封存时却传入 `{"db_sub_tasks": {...}}`。异常被捕获后得到空视图，隐藏了所有中止任务。

**反例**：真实 `_seal_run_snapshot()` 保存的 tasks 有合法 pending T1，interrupted_tasks 却为 []。

**建议**：传入本 run 过滤后的任务 mapping；保留真实损坏账本的显式处理，不能用包装错误导致的空列表假装没有中止项。

**验收**：pending/in_progress、无中止项、含其他 run 同名任务分别验证封存列表。依据方案 §8.1–8.2。

### R2-13 · P2 · 最终复核可接受缺失阻塞依据的 dependency_blocked

**证据**：`services/task_completion.py:311–327`。

只遍历“已有 blocked_by”检查是否直接依赖，没有检查该集合及终止依据必填，也未按固定历史版本核验决策时根因。

**反例**：从失败阻塞任务删除 blocked_by 和 termination_context，其合法结构的 failed 提交仍 accepted=true、无错误。R1-10 的“无关目标”反例已拒绝，但空集合仍绕过。

**建议**：共享门控独立核验 required blocked_by、历史身份及已保存的决策时依据。与 R2-05 的历史语义保持一致，不改为要求历史上游仍是当前失败状态。

**验收**：依据缺失、旧版本不存在、伪造根因分别拒绝；历史依据合法而上游后来 revise/void 的旧终止记录仍可复核。依据方案 §5.2、§7.3 步骤 3/4。

## 3. 非阻塞建议

**M1 · 前端新增 5 项测试仅检查源码标记。** `frontend/src/modules/nl2sql/openQuestionsContract.test.cjs` 通过 readFileSync + assert.match 检查文字/表达式是否存在，不执行 snapshotTurn、历史映射或安全分支，不能证明刷新前后一致或问题不泄漏。建议补可执行的三态及安全投影行为测试；保留源码检查可作辅助，但报告准确区分“源码契约检查”与“行为验收”。不要求新增测试框架。

## 4. R1 闭环情况

| R1 项 | 本轮结论 |
| --- | --- |
| 01 Web 锁交接 | 原反例关闭；真实 handle 可进 Runner，释放职责及失效检查有回归。 |
| 02 来源/快照/全部主成果 | 未关闭：R2-01/02。快照 bool 版本拒绝、仅封存本 run 等局部修复有效。 |
| 03 旧 revision 占位符 | 原反例关闭；validate/execute 拒绝，新增 EXPLAIN/DB 执行均为 0。 |
| 04 memory 清理互斥 | 原反例关闭；真实共享 guard 持有时 cleanup 跳过，memory 不连接 PG。 |
| 05 保留闭包 | 正常 snapshot_ref 根已补，失败保守停止和完整来源遍历未关闭：R2-03。 |
| 06 依赖/内部 guard | 完整声明依赖已拒绝；本地分析 guard 未关闭：R2-09。 |
| 07 多跳来源身份 | 成功包 run/evidence 已补，真实文件线程仍混入携带身份：R2-10。 |
| 08 错误闭环 | 未知目标不归属、错误事实冲突等已修；真实沿用失败归属未关闭：R2-11。 |
| 09 整批顺序 | 初次两层正反序与 forward replacement 已修；历史及新决策快照仍有 R2-05/06。 |
| 10 无关 blocked_by | 原反例已拒绝；完整阻塞审计复核另见 R2-13。 |
| 11 replacement 历史 | reader 的历史版本查找已补，实际登记流程仍失败：R2-05。 |
| 12 幂等 | 原依赖乱序及 blocked 原输入重放反例关闭；旧 plan_version 仍冲突。 |
| 13 正文/空白 finding | 空白拒绝、失败原因披露已补；实际发现正文未补：R2-07。 |
| 14 兜底可信度 | pending 披露和有提交的坏成果路径已修；无提交路径未关闭：R2-08。 |
| 15 reducer | 不可变事实及倒退拒绝有效；合法 void 被误拒：R2-04。 |
| 16 空保护集 SQL | NOT IN(NULL) 原问题关闭；真实 PG DELETE 仍未验证。 |
| 17 DTO/重复 unresolved | 后端原反例关闭；前端验证边界见 M1。 |

## 5. 后续要求与轮次

本次为 **R2/4**。请 Claude 逐项判断本报告问题是否成立，成立则修复并补真实路径回归，不成立则给代码与可重放证据。修复应限于已批准契约，优先复用统一的任务/证据核验，不扩展 SQL 修正上限、自动调度或跨轮全局缓存。

完成后写 `doc63_impl_review_request_r3.md`，注明逐项结果、测试及未验证边界，暂停业务写入并显式发送 `DOC63_IMPL_R3_READY` 到原 Run。下一次是 R3，最多到 R4；R4 后仍未解决则停止自动循环交由用户裁定，不重置轮次。

真实 PG 跨进程锁/DELETE、真实模型题集与预算余量、仓库外消费者、浏览器人工验收仍未覆盖。该事实需要保留，不以单测数量代替。
