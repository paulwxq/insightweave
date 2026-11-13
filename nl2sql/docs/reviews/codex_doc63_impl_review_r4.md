# doc 6.3 实施审核 R4

日期：2026-11-13。审核者：Codex；实施者：用户授权的 CodeBuddy / hy4 接手会话。通过用户人工传递反馈，不使用原 Orca pane。

**结论：REQUEST_CHANGES。确认 3 项需要修复的问题：1 项 P1、2 项 P2；另有 1 项非阻塞性能建议。** 上轮五组主要反例均已有有效修复，真实分析沿用链可以完成查询、继续分析、提交、Runner 交付及归档。

用户在本轮开始后明确要求：不要因第四轮或投入较多而保守放弃，按事实判断，能够给出有力修法就继续完成。故本报告不因轮次自动建议停止或删功能。三项问题均有具体、有限的修复路径，**建议继续修完，不建议放弃现有能力**。

## 1. 基线和实际验证

- HEAD：`50067ce2bc7dbd273c42f81f31fce0eab255ebf5`，分支 `feature/multi-source-knowledge-isolation`，审核全部未提交实现和新增文件。
- 批准方案 SHA256：`c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，读取核对未变。
- 实施请求：[`doc63_impl_review_request_r4.md`](doc63_impl_review_request_r4.md)。
- 只读业务代码与测试；本轮仅写审核文档、协调记录及 `/tmp` 探针，没有部署、重启、真实模型/数据库调用或真实文件删除。

Codex 实际复跑结果：

| 检查 | 结果 |
| --- | --- |
| db_deepagent unit + llm_factory + API v2 全量 | **1335 passed / 4 skipped**，12.46 秒 |
| 前端 nl2sql 全部 `.test.cjs` / `.test.ts` | **57 passed / 0 failed** |
| `vue-tsc --noEmit` | 退出 0，无错误 |
| `git diff --check` | 通过 |
| 独立离线反例与成功路径 | 见下列命令及各项实际输出 |

后端（`nl2sql` 目录）：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy \
  .venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 \
  -q --disable-warnings
```

前端（`frontend` 目录）：

```bash
node_modules/.bin/vue-tsc --noEmit
node --test $(rg --files src/modules/nl2sql -g '*.test.cjs' -g '*.test.ts' | sort)
```

Codex 独立重放的本轮探针（`nl2sql` 目录）：

```bash
PYTHONPATH=. .venv/bin/python /tmp/doc63_r4_runner_persistence_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_gate_r4_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r4_lineage_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r4_lifecycle_probe.py
PYTHONPATH=. .venv/bin/python /tmp/doc63_r4_graph_dedup_probe.py
```

均完成、退出 0；其中打印异常/拒绝的脚本是在捕获并报告反例，不以退出码代表业务成功。使用真实 ToolNode、服务、QaArchive、TaskLedger/reducer、Runner.run 及内存 Store；业务 DB/模型为替身，工作区为临时目录。生命周期探针只删除合成内存快照，并用异常钩子禁止文件实删。

## 2. 必须修复的问题

### R4-01 · P1 · 来源快照读取故障穿透 Runner 收尾，6.2 兜底不再返回

**位置**：`src/modules/db_deepagent/runner.py:696–704,902–911`；`services/reuse_qualification.py:187–194`。该 I/O 调用位于 `_finalize()` 现有 P3 归档异常处理之前；snapshot() 仅将 ValueError 转为 SourceUnqualified。

**复现**：真实 `DbAgentRunner.run()`，注入正常 FakeAgent 状态和真实 QaArchive 的 Store，只令 `run_snapshots` namespace 的 GET 抛 `psycopg.OperationalError`；P0 会话/线程读取正常，模型替身已调用，最终 checkpoint 已读取。普通查询也会查当前 run 的来源快照，无须沿用即可触发。

```text
run_snapshot_read_failure
returned=false
exception=OperationalError
invoke_count=1, get_state_count=2
```

实际没有返回 DbAgentResult，因此没有 `persistence_error` 交付和系统兜底正文。原有 Runner 许多用例未给 services 注入 store，会绕过这段新 I/O；新增 M1 已覆盖模型异常/预算停止，但未覆盖这类来源读取故障。

**建议修法**：

1. 在 Runner 收尾装配/门控 I/O 外增加受控异常边界。按现有 `is_persistence_error()` 分类，写入 persistence 失败记录，令提交无交付资格、无已核验完成标记，继续走既有 `_evaluate()` 的持久化优先级和系统兜底。
2. 保留已取得的证据；返回 `status=persistence_error`、`archived=False`，跳过对同一失败归档后端的再次正常归档。不为完成判定重试模型。
3. SourceUnqualified/确定性的协议损坏继续按业务门控拒绝处理；真实 Store/连接故障不能吞成普通 `task_completion_rejected`。
4. 同步检查 `tools/final_answer.py::build_completion_snapshot()` 的广义 except：已识别持久化异常不应伪装成 `task_ledger_corrupt`，应进入运行层既有持久化分类。

**验收**：只在 snapshot GET 注入 OperationalError/PoolTimeout，普通查询和真实沿用分别执行实际 Runner.run；均返回正确持久化状态、可展示兜底及已有证据，异常不穿透，primary 为 persistence。确定性坏来源仍为受控门控失败。既有模型异常/预算停止优先级保持。

**是否需要放弃功能**：否。缺少的是新增 I/O 与既有收尾状态机之间的异常边界；有明确接入位置，无 SDK 能力障碍。依据方案 §7.5、§8.1 及 6.2 既有保证。

### R4-02 · P2 · 多个独立上游同时阻塞：登记接受，提交与 Runner 却拒绝

**位置**：`src/modules/db_deepagent/services/task_ledger.py:1295–1302`；`services/task_completion.py:280–312`。

**正常路径复现**：T1、T2 分别 failed；T3 声明依赖二者，并登记 `blocked_by=[T1@1,T2@1]`。TaskLedger 输出经实际 reducer 成功应用，但 submit ToolNode 拒绝、不写最终记录，Runner 共享判断也拒绝：

```text
ledger_and_reducer_accepted=true
submit_record_written=false
runner_accepted=false
invalid_task_dependency:
  blocked_by T2@1 的根因与已封存 blocked_by_root 不符
```

即使两项失败 reason 完全相同，仍因根任务身份不同而拒绝。原因是生成端只保存首项主要根因，复核却要求每一项的根因都等于这一项。

R4 实施请求的“边界 7”主动披露了该限制，这有助于审核，但“所有 blocked_by 必须共享同一根”**不是已批准契约**。方案 §4.1 的 blocked_by 是导致阻塞的直接依赖版本列表，§5.2 要求登记时各依赖确已 failed/needs_clarification，并未限制独立根数量。单一摘要字段也不意味着其余真实阻塞原因必须相同。

**建议修法**：

1. 逐项核验所有 blocked_by 的固定身份、决策时依赖快照和可推导根因，继续拒绝后来追加的伪造来源。
2. 保留 `blocked_by_root` 为首项的主要展示摘要，并验证它等于首项根因；其余合法项各自验证，不强制等于首项。首版无需新增多根展示协议。
3. 最终候选和决策快照生成后，登记端复用同一校验，避免再次写入“生成端接受、复核端无法接受”的终态。

**验收**：两个独立 failed、failed+needs_clarification、同根多分支均经真实登记/reducer→submit→Runner 接受；C/D 伪造快照、未覆盖的追加来源继续拒绝；单根与历史 revise/void 正例保持。

**是否需要放弃功能**：否。移除多余的“所有根相同”限制即可，保留逐项事实检查，不必新增一套阻塞机制或重做接口。

### R4-03 · P2 · 孤儿快照按各自年龄删除，制造缺源后永久阻断后续清理

**位置**：`src/modules/db_deepagent/persistence/maintenance.py:779–803`（逐快照删除）；`593–642`（下轮全图核验）；`714–730`（发现缺源时跳过该会话全部资源删除）。

**跨清理周期复现**：使用合法配置 `archive_retention_days=7`、`result_retention_days=30`（配置只要求正数，无二者大小限制）。source 快照 8 天、reuse 快照 1 天，来源在沿用时仍处结果有效期；来源 QA 已过期，新运行可因 P3 归档失败仅留下快照，没有保留根。

第一次实际清理删除已到期 source，保留未到期 reuse。8 天后 reuse 也到期，却因自己的 source 被前次清理删掉而报告缺源，停止整个会话回收，reuse 此后一直残留。

```text
first_actions: 孤儿快照 source 已清理
remaining_after_first=[reuse]
second_actions: 来源证据 source:E-source 缺失
remaining_after_all_expired=[reuse]
```

这不是要求保留无根链至永久；恰恰是清理自身制造了无法再回收的状态。R3-05 原“无根即自发保护”已经修复，但新增的逐项 TTL 回收与全图完整性检查仍需协调。

**建议修法**：从现有来源边构造孤儿快照的删除依赖。先计算到期候选；若一个暂不删除的孤儿仍引用某个到期来源，则延迟删除该来源，递归收敛。相关孤儿全部满足删除条件时按依赖一起回收。保证“本轮保留的快照不会指向本轮刚删除的来源”。不延长业务沿用 TTL，也不取消根/图读取失败时的保守停止。

**验收**：至少两个连续清理周期：旧 source + 新 dependent 首次不产生悬空引用，全部到期后均可回收；input/direct 多跳分别覆盖；有根保护与移除最后根场景保持；坏协议/缺源/环仍明确报告。使用内存 Store 的实际 snapshot delete，文件用临时隔离目录或禁止实删钩子。

**是否需要放弃功能**：否。已有图和年龄信息足以规划依赖一致的删除集合，无需重做持久化体系。依据方案 §8.2 的来源完整性与过期孤儿可回收要求。

## 3. 非阻塞性能建议 M1：共享祖先仍重复遍历，建议加 verifier 内缓存

`EvidenceGraphVerifier.verify_node()` 目前只有 visiting 环检测，没有已完成节点缓存。真实工具构造同任务 compare DAG，每个新分析消费前两项成果；仅核验最后一项，实测：

| 独立节点数 | verify_node 调用次数 | 本机本次耗时 |
| --- | --- | --- |
| 10 | 109 | 0.001 秒 |
| 14 | 753 | 0.010 秒 |
| 18 | 5167 | 0.070 秒 |

全部合法，无资格错误。当前小规模耗时尚低，因此本报告列为**非阻塞建议**，不把未运行的大图耗时当成已发生的故障；但重复次数增长和方案 §6.3 的“去重+环检测”值得补齐。默认 40 模型/80 工具预算也允许比 18 节点更深的图。

建议每个 verifier 按完整身份及核验角色缓存已完成节点；每条边仍核对 expected task/result 约束，保留 visiting 环检测。缓存局限本次不可变视图，不跨 run 全局共享；不能因 input 角色验证过就跳过更严格 source 资格检查。增加共享祖先访问次数及 source/input 角色转换回归即可，不需要性能框架。

## 4. R3 原项与已完成能力

| 原项 | R4 复核结论 |
| --- | --- |
| R3-01 合法 A 沿用 | 主要原反例关闭。独立探针真实 query→A→completed→reuse A→completed→下游继续分析→completed→submit→Runner.run 成功，status=completed、archived=true、写 QA，正文有 findings。 |
| R3-02 占位符前置 | 原反例关闭。未选成果、祖先缺源均在 EXPLAIN/DB 前拒绝；四类副作用增量为 0。缺源用例保持上游 completed，排除 pending 依赖造成的假阳性。 |
| R3-03 严格边解析 | 原三类畸形来源受控拒绝、零沿用写入；清理共享严格解析及删除 guard 保持。新 I/O 异常分类另见 R4-01。 |
| R3-04 历史阻塞 | 原 A/B 合法历史路径接受，原 C/D 坏依据拒绝；pre_void_terminal 和固定版本追根有效。多个独立根正常路径仍见 R4-02。 |
| R3-05 无根链 | 原反例关闭：无根链 protected=set()，快照为孤儿，可进入清理候选。跨周期回收顺序见 R4-03。 |
| M1 Runner 异常测试 | 新模型异常、预算停止用例实际注入并通过；不再把只改变字符串参数当作异常路径验证。快照读取故障尚缺回归，见 R4-01。 |

真实 PG 跨进程锁/DELETE、真实模型题集和预算余量、仓库外消费者、浏览器人工验收仍未覆盖；本报告保留这些边界，不把离线成功等同于上线验收完成，也不以未执行外部验证断言实现错误。

## 5. 公正评价与后续建议

这轮修复有实质效果：核心沿用链、原历史决策反例、副作用前置、严格血缘及无根标记均已验证成立。没有证据表明上述能力在 DeepAgents 中无法实现。建议保留当前结构，针对 R4-01、R4-02、R4-03 做局部修复；M1 可顺带处理，但不应为它引入新的通用框架。

用户在本轮已明确调整此前“第四轮后停止”的推进意见：质量和可执行修法优先，不能仅因轮次或 token 投入放弃。本报告因此建议继续修复，而非自动终止；仍保持真实轮次编号，不清零。接手工具逐项核实、修复并补回归后，写下一次复审请求并暂停业务写入，由用户人工传递；Codex 再按实际证据判断。若之后确实遇到技术不可行或收益不足，再具体说明障碍、可选方案和功能代价，由用户裁定。
