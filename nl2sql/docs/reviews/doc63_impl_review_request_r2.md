# doc 6.3 实施复审请求（R2 / 最多 4 轮）

- 日期：2026-11-13
- 实施者：右下 Claude（term_b59a7d6d）；审核：左侧 Codex（term_03e470db）
- R1 报告：`codex_doc63_impl_review_r1.md`（17 项：5 P1、11 P2、1 P3）
- 方案 SHA256 不变：`c393d2ea…`（本轮未改文档）；代码仍为未提交工作区 diff
- 变更规模（相对 R1 diff 再增量）：核心 12 文件 + 新增
  `services/reuse_qualification.py` 与回归 `test_doc63_r1_regressions.py`（37 例）

## 逐项处理结论

| 项 | 处理 | 要点 |
| --- | --- | --- |
| R1-01 P1 Web 锁交接 | **修复** | `session_guard._lock_is_held` 统一接受 `SessionAdvisoryLock.locked` 与 `SessionLockHandle.held`；构造与校验共用。回归：真实 SessionLockHandle→真实 Runner→FakeAgent 进入 invoke、释放职责仍归外层、已释放包装不能再铸凭据 |
| R1-02 P1 来源资格/快照 | **修复** | 新增 `services/reuse_qualification.py`：来源须为原 run 最终有效 completed 版本且证据已选入成果，递归核验 direct_reuse_from（去重+环检测），快照优先、线程账本次之、缺源拒绝；接入 reuse 工具、completed 登记（`_validate_outcome_evidence`）、提交门控与 Runner（`CompletionSnapshot.reuse_failures`，选定成果逐项核验）。快照基础：写入/读取均核验内部 scope 与寻址一致、版本 `type is int and ==1`、read_task/read_evidence 多条匹配即拒绝、封存只含本 run 任务（同线程两轮同名任务按 run 身份区分）、保存并校验 `ledger_protocol_version`。Runner 装配 store 注入（CLI/Web），装配见 `services/__init__.py store` 字段 |
| R1-03 P1 同任务旧 revision 占位符 | **修复** | 提取共享 `resolve_placeholder_lineage`：validate 在 EXPLAIN/授权副作用前调用，execute 复用同解析器；同任务来源必须 `source_rev == 当前 revision`。回归断言拒绝时无授权、无 DB 执行、无 EXPLAIN |
| R1-04 P1 memory 清理互斥 | **修复** | `cleanup(memory_in_process=True)` 走 `_cleanup_memory_mode`：不连 PG、逐会话 `acquire_memory_lease`（取不到跳过全部清理）、锁内重扫 metas、归档+R2 照常、R3 跳过。回归：Runner 持锁→跳过并报告、释放后可清、memory 路径零 psycopg.connect |
| R1-05 P1 闭包忽略 snapshot_ref | **修复** | `_retained_snapshot_roots` 收集保留期 QA/run_errors 的公开引用 **与 snapshot_ref 键**；`_protected_closure` 重写为双向图 BFS（ref↔快照），不再用集合相交近似；锁内重读 metas；坏元数据/时间不可解析报告并跳过。回归：无公开 evidence 的 run_errors 经 snapshot_ref 保护其结果文件 |
| R1-06 P2 全依赖准入/guard | **修复** | analyze 与 E/A reuse 改用 `require_task_ready_for_work`（声明依赖全部 completed，与 SQL 同一规则）；`require_session_lease` 在 pipeline validate/execute 服务侧校验（占位符读取亦在窗口内）。回归：T4 声明 T1(completed)+T2(pending) 只输入 T1 → 拒绝 |
| R1-07 P2 携带清单身份 | **修复** | `_collect_carried` 的 origin 一律为记录自身完整身份（含 meta 补 thread_id），链首 reused_from 只随 record 作展示；execute/analyze 成功包新增 `source_evidence_ref`（新记录完整引用，execute 置段尾保 result_ref 在转存预览头 5 行）；reuse mapping 的 `source_evidence_ref` 改指新记录、`origin_evidence_ref` 保留直接来源。回归：多跳（沿用→轮换）清单可解析 |
| R1-08 P2 错误闭环 | **修复** | ①`_ADMISSION_ERROR_TYPES` 白名单：准入子码以原码记录 → 未知 T9@99 无 source_task_ref；②E/A 沿用失败统一 `_ea_error` Command 工厂（含 failure_ref；准入类不归属）；③`_validate_failure_refs` 与最终规则均逐项比较 ref 内 source_task_ref/tool_call_id/error_type 与权威记录（伪造 T99 拒绝）；④reducer 拒绝空壳（tool/error_type/message 非空白、retryable 布尔），事实比较改为全字段（结构化 details 差异拒绝） |
| R1-09 P2 先合并后核验 | **修复** | blocked_by 目标资格与 replacement 存在性校验移入 `_validate_candidate`（候选构造完成后统一执行）；服务端 reason/blocked_by_root/dependency_snapshots/replacement_snapshot 从最终候选生成并只回填本批修改记录。回归：正反序同批等价接受 |
| R1-10 P2 无关 blocked_by | **修复** | 登记侧：blocked_by ⊆ 该任务声明直接依赖（固定版本）且目标在最终候选 failed/NC；终局侧：`evaluate_submission` 防御复核同样规则。回归：无关 T3 拒绝（两侧） |
| R1-11 P2 replacement 历史 | **修复** | `CompletionSnapshot.all_task_records` 携带全部版本 {(run,task,rev)}；复核按完整历史身份 + `replacement_snapshot` 身份一致（缺失/不符拒绝），不要求目标仍是最新/未作废。回归：T2@1 被引用后 revise@2 终止，合法 failed 提交通过 |
| R1-12 P2 幂等口径 | **修复** | add/revise 的 `creation_operation` 保存排序规范化依赖（乱序重放 no-op）；status 保存 `status_operation`（完整规范化输入）、void 保存 `void_operation`，重放比较输入而非派生 reason。回归：依赖乱序重放 no-op、blocked 原参数重放 no-op；旧 expected_plan_version 仍拒绝（裁定不变） |
| R1-13 P2 确定性分项正文 | **修复** | `build_delivery_answer_section`（成功 finding/失败原因/澄清/作废，strip 去重保序）；合法交付（model 来源）正文=模型原文+分项段，交付/归档 delivery_answer/CLI 一致，原始 payload 保真；FindingItem.text 空白在参数层拒绝。回归：合法 partial 正文含 T2 原因；空白 finding 拒绝 |
| R1-14 P2 兜底分级 | **修复** | `_fallback_task_section`：completed 声明仅当门控走到状态映射（`expected_status` 已算）才显示"已完成"，否则"已登记完成（未核验）"；pending/in_progress 一律披露"未完成：…（pending）"。回归：坏 completed 不称已完成；中止运行披露 pending 任务 |
| R1-15 P2 reducer 覆盖 | **修复** | `task_record_update_allowed`：不可变字段（content/dependencies/creation_operation 等）同版本不可改写；status 仅沿 §5.1 方向转移（无出边的终态只能整条相等重放）；`merge_task_records` 拒绝非法同版本覆盖。回归：completed 回退 pending、同 revision 改 content 均拒绝；合法转移通过 |
| R1-16 P2 NOT IN (NULL) | **修复** | 保护集为空时省略排除子句（非空时参数绑定不变）。回归：捕获 SQL 断言无 `NOT IN (NULL)` 且空集仍删除 2 行 |
| R1-17 P3 DTO/去重 | **修复** | `WebTurn` DTO 增 `open_questions: Optional[list[str]] = None`（public_turn 校验通过）；`unresolved_task_refs` 重复在 schema 层拒绝 + 最终规则多重集合精确比较（set 化恰好相等的重复也拒绝）；前端新增 `openQuestionsContract.test.cjs`（5 例：类型契约、snapshotTurn 一致、历史 null 语义、非空即展示、标题） |

**全部 17 项接受并修复；无不成立项。**

## 方案对应表更新

R1 修复后新增的闭环层：`reuse_qualification`（来源最终资格，四入口共享）→
R1-02 契约 4；`resolve_placeholder_lineage` 副作用前核验 → 契约 3；
`_cleanup_memory_mode`/图闭包 → 契约 9；`task_record_update_allowed` →
契约 1（专用 reducer 拒绝非法版本变化）；`build_delivery_answer_section`
→ 契约 8；`status_operation`/排序规范化 → 契约 2。其余十条对应关系见
R1 请求报告，未变化。

## 验证结果

```bash
cd /opt/projects/insightweave/nl2sql
# 后端全量（含 R1 回归 37 例；测试进程内隔离代理变量）
.venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py src/tests/unit/api/v2 -q
# → 1249 passed, 4 skipped

# R1 反例回归单跑
.venv/bin/python -m pytest src/tests/db_deepagent/unit/test_doc63_r1_regressions.py -q
# → 37 passed

# 前端
cd ../frontend
node_modules/.bin/vue-tsc --noEmit   # → 0 错误
node --test $(find src/modules/nl2sql -name '*.test.cjs' -o -name '*.test.ts' | grep nl2sql)
# → 51 pass（含新增 openQuestionsContract 5 例）
```

格式：新增/重构文件 black 通过；ruff 新文件 0 问题（存量债未动）。

## 未验证边界（与 R1 相同，如实保留）

1. 真实 PG 跨进程互斥/真实 PG DELETE 仍未连库执行（FakeConn/内存验证）；
2. 真实模型题集与预算余量验收未执行（不发起付费调用）；
3. 仓库外归档消费者未核查；浏览器人工验收未执行。

## 需重点复核位置

- `services/reuse_qualification.py`（来源资格递归、快照优先降级次序、
  `_find_evidence_record` 快照在而缺证据即缺源不回退）
- `services/task_ledger.py` `_validate_candidate`（blocked_by/replacement
  整批复核与终止依据回填；`status_operation` 幂等口径）
- `persistence/maintenance.py` `_cleanup_memory_mode`/`_protected_closure`
  （图遍历、锁内重扫、memory 边界）
- `state.py` `task_record_update_allowed`（合法转移判定完整性）
- `tools/guard_tools.py` `_ea_error`（归属条件）与 E/A 双模式准入
- `runner.py` 交付分项拼接与兜底分级的交界（`should_fallback` 谓词）

## git diff 基线

- HEAD=`50067ce`；R2 在 R1 工作区上继续（未提交）；已批准文档修订不在审核范围。
