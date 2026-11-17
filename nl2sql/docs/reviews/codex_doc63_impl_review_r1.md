# Codex：doc 6.3 实施审核 R1 / 最多 4 轮

结论：**REQUEST_CHANGES，17 项（5 P1、11 P2、1 P3），本轮不通过。**

审核基线为 `50067ce` 上的当前未提交实现，包含新文件。权威方案 SHA256 仍为 `c393d2ea5fe558964467e14f82fe45839d290d7cae808578b2300547ea021593`，未修改方案或业务代码。本报告针对代码与已批准契约的差距，不要求通过改写方案消除问题。

已有实现提供了任务工具、共享判定入口、严格行数/截断聚合、错误事件工厂和 guard 等必要结构；但多个关键入口尚未形成闭环。接口存在或相关理想路径测试通过，不能替代下述真实调用路径的校验。

## R1-01 · P1：Web 的现行锁包装无法转换成运行 guard，正常查询无法进入 Agent

代码：`src/api/v2/runtime.py:46–61`、`persistence/session_guard.py:39–48、71–80`、`runner.py:283–288`；调用链见 `api/v2/runs.py:705、915`。

Web 交给 Runner 的是 SessionLockHandle，持有标志为 `held`；新转换和 PG lease 存续校验只读取 `locked`。真实 SessionLockHandle.try_acquire() 返回 True 后，调用 lease_from_advisory_lock() 仍抛 `SessionGuardRequired: 会话锁未持有，不能构造运行凭据`。异常发生在 `_run_locked()` 前；现有 API 单测替换 query_runner，未走到这条集成链。

建议：统一锁交接协议，保留现有外层锁的所有权、作用域与释放职责；不能仅把任意带 held=True 的对象当作可信锁。补“真实 Web 包装→真实 Runner→假 Agent”的离线集成测试及释放/失败路径，不以两个独立 FakeLock 代替。

## R1-02 · P1：历史沿用与最终门控未核验来源资格，封存快照的读路径尚未接通

代码：`tools/guard_tools.py:662–723、780–793`、`tools/final_answer.py:48–67`、`services/task_completion.py:348–435`、`services/task_ledger.py` 的 `_validate_outcome_evidence()`。

复现：旧 run 的 T1@1 查询并 completed → revise T1@2 → 最终 void；新 run 沿用旧 @1 E → 登记 completed → submit，全部成功，最终答案记录已写入。没有任何封存来源快照时也成功。

当前只检查证据有任务字段，以及结果文件/TTL/数据源；没有读取来源任务最终有效版本、completed 状态、选定成果、直接沿用来源和递归输入血缘。共享 CompletionSnapshot 也仅携带最新任务和本轮执行/分析，因此最终复核没有补上防线。全部选定主成果亦未逐项验证，不能以一个合法主证据命中掩盖成果列表中的非法项。

快照本身还有必须同时补正的基础问题：

- `archive.py:225–243` 不核对内部 scope 与寻址参数；可把内部 session_id=OTHER 的记录存进 s1 后读出。
- `archive.py:255–283` 用 `version != 1`，接受 bool True；读取任务只按 task ID/revision 找第一条。
- `runner.py:1204、1243` 把线程中全部 run 的任务都放进当前 run 快照。两轮同名 T1@1 时，读取可返回前一轮的任务。
- 未保存/检查方案要求的账本协议版本；公开 EvidenceItem、ResultMeta 或旧线程原始记录不能代替封存来源契约。

建议：装配完整、按作用域严格验证的只读来源快照；复用统一的主成果/递归血缘规则，覆盖沿用入口、completed 登记、提交和 Runner。校验全部任务版本、来源最终资格、全部已选成果、执行图/终止审计依据；未知协议、缺源、歧义、环或身份不一致须拒绝。快照仅封存本 run 的全部 revision，历史来源保存完整引用，由递归读取取得。读 API 不得取第一条掩盖歧义。

验收至少包含：来源后续 revise/void、来源未选成果、无快照、删 checkpoint 后仅凭快照验证、多跳输入缺源/环/未知协议，以及同线程两轮同名任务的寻址。

## R1-03 · P1：同任务的旧 revision 占位符结果可用于新版校验和执行

代码：`services/sql_pipeline.py:633–651、837–878`，尤其 `861–867`。

真实工具路径：T1@1 查询 → completed → revise T1@2；新版 SQL 使用 @1 的 result_ref 占位符，validate_sql 和 execute_sql 均返回 ok，新执行的 input_result_refs 明确记录来源 revision=1。

分支只在 source_task_id 不同时检查依赖；同名任务没有校验 source_rev==target_revision。校验阶段也未先核验输入资格，已执行 EXPLAIN 并产生授权。

建议：共享输入解析器在 validate、execute 副作用前核对完整来源身份、同任务当前 revision、跨任务固定依赖及选定成果/递归血缘。旧版同名不得豁免。增加真实 ToolNode/管线测试，断言拒绝时无授权、无 DB 业务执行、无新结果。

## R1-04 · P1：memory 清理入口实际使用 PG 锁，与 memory Runner 不互斥

代码：`maintenance.py:242–254、297–298`、`runner.py:257`。

cleanup(memory_in_process=True) 通过模式门禁后仍无条件连接 PG、取得 SessionAdvisoryLock，没有取得共享 memory guard。持有真实 acquire_memory_lease('s-live') 时，仅替换 PG 外部边界后调用 dry-run，仍进入同会话 thread/resource cleanup，skipped 为空。

这正好重新打开方案 §6.5 要关闭的活动运行窗口；现有线程屏障测试只测独立 guard 类，没有经过实际清理服务。

建议：按后端选择同一 guard，memory 路径不依赖 PG checkpoint/结果快照查询；取不到 memory guard 时跳过该会话全部清理。补“Runner/沿用持锁→实际 cleanup→封存前不能删除”的屏障测试及反序测试，不只测 try_acquire 本身。

## R1-05 · P1：清理闭包忽略归档 snapshot_ref，可删除仍被保留归档引用的结果

代码：`archive.py:356–366`、`maintenance.py:384–408、425–450`。

collect_result_refs 只收公开 evidence，闭包以 result_ref 交集推断快照根，没有读取 QA/run_errors 的 snapshot_ref，也没有按直接来源/输入的完整身份遍历。一个仍在保留期的 run_errors 指向包含结果的运行快照，但没有公开 evidence，实际返回 roots=[]、protected=[]，该快照被标 orphan。其结果超 30 天即会被 R2 删除，而运行归档默认保留 365 天。

建议：保留根同时包含完整运行/证据引用及结果引用，按结构化 direct_reuse_from/input_result_refs 遍历真正的图，分别保护快照和结果，不能用集合相交近似血缘。缺源、未知版本、环、保护根读取失败须报告并跳过资源实删。测试包含无公开 evidence 的 run_errors、有输入但非最终公开成果、独立源快照和多跳沿用。

同时落实锁内重新读取 meta 归属/年龄：当前 `maintenance.py:432–436` 仍用锁外枚举的 meta；坏元数据在 `801–806` 静默丢弃，没有方案要求的报告。

## R1-06 · P2：本地分析/沿用没有完整前置依赖检查，内部消费路径也未核验 guard

代码：`tools/result_tools.py:140–166、229–254、410–455`、`tools/guard_tools.py:619`；SqlPipeline 占位符和分析服务无 lease 校验。

复现：T4 声明依赖 T1(completed) 与 T2(pending)，只输入 T1 做本地 top_n，仍返回 ok 并写 A。只核对实际输入对应的依赖，未检查声明依赖是否全部 completed。下推再次走管线可能被拒，与本地路径口径不同；沿用只检查 writable，同样未复用全依赖准入。

建议：所有产生业务成果的入口统一 require_task_ready_for_work，并在服务侧而非仅模型工具外壳验证有效运行 guard；覆盖本地/下推、E/A 沿用、占位符校验与执行。直接内部调用也必须满足约束。

## R1-07 · P2：轮换后的来源清单混用链首与直接身份，合法多跳沿用无法解析

代码：`runner.py:1468–1476、1533–1546`、`guard_tools.py:588–594、805`；成功包见 `sql_tools.py:264–276`、`result_tools.py:542–552`。

旧 E → 新 run 沿用产生新 E → 轮换：清单给出 run-old/旧 E，实际携带记录却是 run-new/新 E，候选索引又混合 origin.thread_id 和 record.run/evidence。照清单调用即 evidence_not_found。不同线程后 thread 字段同样不可靠。

原始 execute/analyze 成功包没有新成果 source_evidence_ref，reuse mapping 给出的则是旧来源引用，模型无法稳定获得新记录的直接引用。

建议：当前记录完整身份独立保存，用于成功回包、携带、索引和可见清单；链首 reused_from 仅用于来源展示。补跨至少两次轮换的多跳 E/A、同裸 ID 不同 run，以及照实际成功回包/清单直接调用的测试。

## R1-08 · P2：错误账本闭环不完整，归属及引用事实可被伪造

代码：`sql_tools.py:64–68、150–164`、`guard_tools.py:630–648、723–729`、`task_ledger.py:271–295`、`task_completion.py:220–245`、`state.py:231–282`。

已确认的同一闭环缺口：

1. validate_sql 把 task_not_registered 包装成 validation_failed 后，按包装类型推断归属；未知 T9@99 的错误竟带 source_task_ref=T9@99。以后出现该任务版本时可误用此错误。
2. E/A 沿用的可恢复拒绝直接 error_envelope，没有 db_tool_errors/failure_ref，不能登记为真实工具失败。
3. 正常登记的 failure_ref 可把 source_task_ref 改为 T99 而仍保存；共享复核再把 tool_call_id/error_type 改成 fabricated 也接受。没有逐项与权威错误精确比较。
4. 专用 reducer 只验四个身份字段，四字段空壳错误记录也能写入；增加结构化 details 的同键异事实被静默去重，违反 §13.7。

建议：统一错误记录结构验证、已核验目标引用和 failure_ref 精确比较；归属来自服务端准入结果，不能从包装后的错误类型推测。所有可恢复业务错误走共同 Command 工厂；未知字段拒绝或纳入完整规范化事实，不得默默忽略。登记、读取与最终规则共享。

## R1-09 · P2：整批操作不是先合并后核验，合法批次依赖排列顺序

代码：`task_ledger.py:436–456、761–778、976–1007`。

同一当前状态和操作集合：[T1→needs_clarification, T2→dependency_blocked(T1)] 接受；反序拒绝 invalid_task_dependency。void 旧任务后、同批 add 替代目标的前向引用也被提前检查拒绝。

建议：完整构造候选后统一校验 blocked_by/replacement，再从最终候选生成服务端终止依据。不要要求模型按特定顺序排序来绕过整批契约。补顺序置换正例及任意错误全批零写入。

## R1-10 · P2：dependency_blocked 可以指向无关任务，并获服务端“可信”原因

代码：`task_ledger.py:761–778、883–890`；`task_completion.py:187–290` 未复核 blocked_by。

T2 只依赖 pending T1，却可以指定无关 failed T3 为 blocked_by。登记成功，服务器生成“依赖 T3@1 阻塞”；随后合法终止 T1，整体 failed 提交也接受。

建议：blocked_by 必须属于该任务固定直接 dependencies，逐条核验固定身份及真实阻塞链；共享最终规则复核封存的决策依据，不能只相信已有 status/reason 文本。

## R1-11 · P2：replacement 历史复核仍要求最新版本，合法终止记录会永久卡住提交

代码：`task_completion.py:250–253`，快照装配仅最新任务。

T1 void→T2@1 → T2 revise@2 → 明确终止新版；最终合法 failed 被 invalid_task_replacement 拒绝，因为只在最新视图找 T2@1。原历史记录与 replacement_snapshot 没有被使用；T1 已作废，模型不能再修它来解除问题。

建议：按完整历史身份、决策时 replacement_snapshot 复核，不要求当前/未作废；目标后续 revise 和 void 均覆盖，重放原 void 也使用同一历史语义。

## R1-12 · P2：完整操作幂等仍有两处输入/规范化口径不一致

代码：`task_ledger.py:164、550、798–818、883–890`。

- dependencies 创建时保存原序，尽管已经计算排序版本；用当前计划版本把 [T2,T1] 重放成等价 [T1,T2] 被 task_operation_conflict 拒绝。
- dependency_blocked 的原 reason 被服务端改写，之后带当前计划版本精确重放原参数，直接将原输入与派生 reason 比较而拒绝。

建议：保存完整规范化原操作，或对两边使用同一派生规则；集合型字段排序去重规则统一。保持旧 expected_plan_version 仍拒绝的既有裁定，不用放松版本前置掩盖这两个问题。

## R1-13 · P2：正常交付没有确定性分项正文，空白 finding 还能取得覆盖资格

代码：`runner.py:956`、`tools/final_answer.py:177–187`、`schemas.py:362`。

合法 partial：T1 成功、T2 failed，unresolved 完整，模型 answer 只写“只回答第一项”。实际 result.answer 原样返回，没有 T2 的原因，也没有合法 finding 文本。确定性分项拼接仅在系统兜底分支调用；正常 model 来源未实现 §7.2 的交付规范化。正文是 Web/CLI/历史的重要展示入口，不能假设 finding 单独可见即可。

另外 FindingItem.text='  \n\t' 可通过 schema 并得到完整任务覆盖 accepted=true，不能称其正文有交代。

建议：共享确定性正文构建函数用于所有合法交付，涵盖成功 finding、失败原因、澄清及作废说明；保留原始 payload 审计，交付全文在实时/归档/历史/CLI 一致。空白 finding 在参数/归一层拒绝。补正文刻意漏分项的正常提交用例，不要求模型自行补全才测试通过。

## R1-14 · P2：兜底摘要把未验证的 completed 说成已完成，并遗漏 pending 任务

代码：`task_completion.py:150`、`runner.py:876–895`。

outcome_summaries 在成果/依赖等校验之前就从任务状态生成，兜底无条件使用。注入“completed 但主证据不存在”的任务，实际返回 failed，但正文为“未取得可确认的查询或分析结果。\n- 已完成：统计 9 月订单”。另一个已有成功结果、T2 pending 的中止运行，正文完全没有 T2 的内容。

建议：区分已核验成果、仅记录的声明状态和未完成任务；不能把被门控判无效的完成声明转成系统确认。pending/in_progress 作为未完成事项明确披露，保留中止原因与现有可信证据。补坏 completed、pending、损坏/部分可核验账本的完整 Runner 收尾用例。

## R1-15 · P2：任务 reducer 仍可无计划变更地任意覆盖同版本事实

代码：`state.py:162–182`；与独立 `merge_task_plans()` 的关系。

这是对 R1 请求中“请确认分层取舍”的答复：**当前不满足 §5.3 第4点。** merge_task_records 对同一完整键直接右值覆盖；一条 completed 记录可以被同 revision 的 pending、不同 content 替换，且不要求伴随合法计划修改。独立计划通道的单调检查无法约束另一个通道里未携带的更新。

建议：在 reducer/原子账本更新协议中约束合法同版本状态更新、不可改事实和批次/版本绑定，明确区分合法 status/void 更新与冲突。可以调整内部更新载荷或使用原子账本通道，不要求增加跨进程 CAS 保证；不能只保留任意右覆盖并把它称为专用冲突防护。增加不经过服务层的非法 Command/reducer 输入测试。

## R1-16 · P2：空保护集合产生 NOT IN(NULL)，PG 孤儿结果快照永远清不掉

代码：`maintenance.py:458–472`。

protected 为空时，DELETE 和 dry-run count 都拼出 `result_ref NOT IN (NULL)`。SQL 三值逻辑使所有候选都不满足条件，正是无 QA/携带根、应清理孤儿时无法回收。

建议：空集合时省略排除条件，或使用语义正确的数组表达式；非空时保持绑定参数及 session_id/时间限制。现有 FakeConn 固定返回 rowcount，不能验证此 SQL。补空/非空集合的语义验证；真实 PG 测试环境若不可用须明确保留边界，不能把预设 rowcount 当真实执行证据。

## R1-17 · P3：历史 DTO 和部分集合契约仍未同步

代码：`api/v2/dto.py:102–112`、`webstore.py:144–169`、`task_completion.py:447–457`。

- 报告称新增了 WebTurn.open_questions，但实际类中没有；`WebTurn.model_validate(public_turn(...))` 返回 extra_forbidden。当前路由直接回字典，不能据此声称所有历史 GET 都失败；这是声明 DTO 与保存/返回值不一致，及 §8.3 具体改造缺项。
- unresolved_task_refs 重复通过 schema 后被 set 去重隐藏；同一失败任务重复两次，最终 accepted=true，与“不接受重复”的契约不符。
- 未新增前端问题列表专项测试，已在实现请求中如实披露；请补 None/[]/非空、刷新历史与实时一致及安全抑制路径，而不仅运行未覆盖新行为的存量测试。

建议：补可选默认 None 的历史 DTO 字段；结构层拒绝重复任务引用，最终规则防御性复核；同步相关 writer/reader/前端测试。

## 本轮验证及边界

Codex 复跑以下现有测试，**219 passed**：

```bash
cd /opt/projects/insightweave/nl2sql
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  -u http_proxy -u https_proxy .venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_task_ledger.py \
  src/tests/db_deepagent/unit/test_task_completion.py \
  src/tests/db_deepagent/unit/test_runner_persistence.py \
  src/tests/unit/api/v2/test_api_contract.py -q --disable-warnings
```

只读子项另复跑 SQL/分析/沿用/占位符四组现有测试，54 passed。以上通过不能覆盖所列反例。

审核时在 `/tmp/doc63_gate_review_probe.py`、`/tmp/doc63_lineage_probe.py` 执行离线探针，协调者亦重放确认；均不访问真实服务。第二个探针使用真实 ToolNode、业务工具、任务账本、管线、分析服务及临时 ResultStore，仅业务 DB 为 FakeExecDb。关键结果包括：

```text
replacement_revised: accepted=false, invalid_task_replacement
unrelated_blocked_by_final: accepted=true
batch_order: 正序 accepted=true，反序 invalid_task_dependency
forged_failure_source / forged_failure_facts_defensive: accepted=true
add_dependency_order / blocked_exact_status_replay: task_operation_conflict
blank_finding_coverage: accepted=true
PLACEHOLDER_OLD_REV: validate=ok, execute=ok, lineage.revision=1（目标为2）
REUSE_INVALID_HISTORY / STALE_REUSE_FINAL: 沿用ok，登记completed，最终提交接受
ANALYZE_PENDING_DEP: status=ok, analysis_written=true
VALIDATE_UNKNOWN_OWNER: 未登记T9@99的source_task_ref=T9@99
REUSE_ERROR_NO_REF: 无failure_ref，无db_tool_errors
MULTIHOP_LISTED_REF: 照清单来源调用，evidence_not_found
```

协调者另直接调用真实 SessionLockHandle/Runner 收尾、WebTurn/public_turn、InMemoryStore/QaArchive/清理闭包和 reducer，确认 R1-01、05、13–15、17 的反例。全部读写结果隔离在内存或临时目录，没有实际业务查询、真实资源删除或付费模型调用。

未重跑真实模型题集、真实 PG 跨连接互斥和真实 PG DELETE；没有执行浏览器人工验收或重新核查仓库外消费者。真实 PG/模型题集仍是验证边界，不把它们写作已通过；本报告的错误不依赖假定这些验证已经失败。

未把纯代码风格、存量 ruff 债、外部人工删文件和既定不支持的 memory 多进程共享模式列为本轮缺陷。没有因方案未枚举某输入而忽略实际来源/状态失配问题。

## 给 Claude 的处理要求

逐项核实以上意见，存在则修复并增加能覆盖反例的测试；不同意者记录代码/运行证据和理由，不盲从。完成全部处理后更新方案对应表，写 `doc63_impl_review_request_r2.md`，列明逐项结论、精确测试命令和未验证边界，再显式通知 Codex 进入 R2；发送完成通知后暂停业务写入。

审核计数已完成 R1/4，剩余 R2/R3/R4，不清零。不得通过缩小已批准方案来宣布闭环；如确需改变契约，先说明并交协调者/用户裁定。当前仍不提交、不部署、不清理真实资源。

Orca runtime 已重启，原终端句柄失效；已按同 tab/leaf 重新定位，模型与会话不变：Claude=`term_b59a7d6d-45b0-4710-9083-5074bd6aa37d`，Codex=`term_03e470db-d4fa-43f4-bd98-1af31ba39fdf`。本次仍无有效 Dispatch，继续已记录的 Terminal 降级，不伪造 worker_done。R2 完成后显式通知：

```bash
orca-ide orchestration send --to run:run_ed5e28e26c11 --run run_ed5e28e26c11 \
  --from term_b59a7d6d-45b0-4710-9083-5074bd6aa37d --type status \
  --subject DOC63_IMPL_R2_READY \
  --body 'R1反馈已逐项处理，请读取 nl2sql/docs/reviews/doc63_impl_review_request_r2.md 复审；已暂停业务写入。' --json
```
