# db_deepagent 非取数回答直接提交修改方案

> 状态：实施稿（待评审）。
> 依据：doc 18（问题分析稿，经三轮外部审核通过）及其审核中收敛的修改方向；
> 代码基线为分支 `feature/multi-source-knowledge-isolation`、HEAD `6a98f4d` 工作区。
> 目标：为"回答不需要数据库结果"的问题（纯解释、时间常识、查询前澄清）留出
> 直接提交出口；已登记数据任务的全部校验原样保留。

## 1. 目标与非目标

### 1.1 目标

- 主 Agent 自主判断是否需要取数：
  - **需要取数**：沿用现有任务登记、SQL 校验/执行、成果覆盖全流程，行为不变；
  - **无需取数**：可调用 `resolve_time_range` 等辅助工具后**直接**单独调用
    `submit_final_answer` 提交（`findings=[]`，不登记任务）。
- 修复连带缺陷：零任务时 `needs_clarification`（查询前澄清）同样可提交。
- 直接回答合法归档（QA，而非 run_errors）。
- 本案例（"请问大前天是哪一天"）的目标流程：

```text
resolve_time_range("大前天") → [2026-11-12, 2026-11-13)
submit_final_answer(status=completed, answer="大前天是 2026年11月12日。", findings=[])
→ 用户看到答案；db_execute=0；无任务、无追加审计段
```

### 1.2 非目标（明确不做）

- 不改已登记数据任务的任何校验（登记证据绑定、成果核验、覆盖、status 映射）。
- 不改 SQL guard、时间解析器、reuse_evidence、任务账本 schema。
- 不新增工具、子 Agent、分类器或语义判定模型。
- 展示精简（正文追加审计段、口径说明分离）归 doc 17，本方案不处理
  **数据查询案例**的展示；直接回答案例的追加段自然为空（§3.4），不依赖
  doc 17。
- 不为"模型把数据问题误判为无需取数"增加程序拦截（§12 风险坦白）。

## 2. 方案总览

改动集中在四处，共享门控分支是核心：

| # | 位置 | 改动 |
| --- | --- | --- |
| 1 | `services/task_completion.py` | `evaluate_submission` 新增"零任务直接提交"分支（共享入口，提交工具与 Runner 自动一致） |
| 2 | `config/prompts.yaml` + `skills/query-planning/SKILL.md` | 登记指令从"任何问题"收窄为"需要取数的工作"；明示直接回答路径与防跳查约束 |
| 3 | `middleware/final_answer_gate.py` | 收卷提醒去掉无条件的"并引用已有 evidence_id" |
| 4 | `runner.py` | `_archive_run` 归档条件补直接回答（零执行零沿用的合法答案写 QA） |

## 3. 共享门控新分支（task_completion.py）

### 3.1 触发条件

`evaluate_submission` 在**步骤 2（任务视图检查）之前**插入分支：

```text
若 snapshot.tasks 为空（本 run 从未登记任何任务，含作废记录）
   且 normalized_submission is not None：
    走直接提交分支
否则：
    走现有全部步骤（步骤 2..N 原样）
```

`normalized_submission=None`（Runner 无有效提交的收尾判定）**不进入**
直接分支：现有 None 守卫位于步骤 6 之后（`task_completion.py:668`），
零任务 + None 会照常走步骤 2 拒绝后由 Runner 兜底——保持现状，不加
新机制。另：完成规则语义变化，`TASK_COMPLETION_RULES_VERSION`
（`task_completion.py:19`，当前 1）**递增为 2**，供日志与归档区分
新旧规则。

判定依据已核实：提交工具与 Runner 装配快照用的 `current_task_view` 默认
`include_voided=True`（`task_ledger.py:70-72`），因此：

- **从未登记** → `snapshot.tasks == {}` → 直接分支；
- **登记后全部作废** → `tasks` 非空但 `non_void` 为空 → 现有步骤 2 照常
  `task_not_registered`（**v1 有意保留**，见 §12.2；防止顺手放开数据任务
  的失败语义）；
- **存在任何未作废任务** → 现有全流程。

### 3.2 直接分支的允许形态

| 项 | 约定 |
| --- | --- |
| 允许的 status | `completed`（解释型答案）、`needs_clarification`（查询前澄清） |
| 澄清问题承载 | 提交 schema（`schemas.py:394` `DbAgentFinalAnswer`，`extra="forbid"`）**没有** `clarification_question` 字段——该字段属于任务记录；零任务直接澄清分支中，澄清问题写入 `answer` 正文并列入 `open_questions`（此为该分支内的例外语义；已有任务路径的 `open_questions` 仍只放非阻塞补充问题，不变） |
| `findings` | 必须为空（解释内容写在 `answer` 正文） |
| `unresolved_task_refs` | 必须为空（无任务可引用） |
| 其余结构 | 沿用既有参数层校验：`answer` 非空白、单独调用、不重复提交 |
| 违反上述空约束 | 拒绝，错误码 `direct_answer_task_refs_not_allowed`（可修复：清空 findings/任务引用后重提） |

`failed` 不开放：零任务的失败语义不明，"无法回答"用 `needs_clarification`
或 `completed`（正文说明无法确定的原因）表达。

### 3.3 分支返回值

- `accepted=True`，`expected_status=提交的 status`（无任务映射，自洽）；
- `finding_summaries=[]`、`coverage_map={}`、`counts={}`；
- `plan_version` 照实传递。

### 3.4 交付正文与 K19 一致性

- Runner 收尾复评同一入口（K19），直接分支结论自动一致；
- 直接回答的 `decision` 无 findings、无任务、无失败/澄清任务 →
  `build_delivery_answer_section(decision)` 输出为空 → **交付正文无追加
  段**。本案例类的"结论 F-1（T2@1）/已作废/已完成"尾部自然消失（doc 18
  §4 痛点 2/3 在直接回答场景顺带解决；数据查询场景仍归 doc 17）。

## 4. 提示词与 Skill 修改

### 4.1 `config/prompts.yaml`（"业务任务登记"节，约 :37）

原文：

> 回答任何问题前先用 record_sub_tasks 登记本轮业务任务（简单问题也
> 登记一项）；任务 = 需要交付的业务问题或必要中间结果，不是工具步骤。

替换为：

> 需要取数、结果分析或沿用数据成果的问题，先用 record_sub_tasks 登记
> 本轮业务任务；任务 = 需要交付的业务问题或必要中间结果，不是工具步骤。
> 纯解释类问题（日期常识、术语/口径说明等，答案不依赖库内数据）与需要
> 向用户澄清的问题，无需登记任务：收集必要信息（可调用时间解析等辅助
> 工具）后，单独调用 submit_final_answer 直接回答，findings 留空。

并在证据规则附近补一句（防跳查；措辞兼容 reuse_evidence 沿用与先澄清）：

> 库内数据结论必须依据实际查询、结果计算或受控沿用的有效成果；信息
> 不足时可以先澄清，不得直接猜测数据数值。

同时将主提示词中三处"全部任务"表述限定到已登记任务路径，避免与直接
回答冲突（`config/prompts.yaml`）：

- `:48` "全部任务进入终态后才能提交" → "已登记任务时，全部任务进入
  终态后才能提交"；
- `:77` "证据足够、全部任务终态后单独调用 submit_final_answer 提交最终
  答案" → 同步加"已登记任务时"限定；
- `:84` "open_questions 只放非阻塞补充问题，不放阻塞事项" → 补例外
  "零任务直接澄清时，open_questions 承载澄清问题"。

> 注：提示词修改是本方案的核心组成部分而非附属——若只放开程序出口而
> 不改"回答任何问题前先登记"指令，提示词仍会推动模型为日期类问题登记
> 无法 completed 收尾的任务（doc 18 §5.6）。

### 4.2 `skills/query-planning/SKILL.md`

同步上述边界：规划指引改为"需要取数的工作才登记任务"；混合问题（如
"大前天是哪天、那天有多少订单"）只为数据部分登记任务，日期部分直接在
answer 中解释。

## 5. 收卷提醒文本（middleware/final_answer_gate.py）

`final_answer_gate.py:84` 原文：

> 请调用 submit_final_answer 单独提交最终答案，并引用已有 evidence_id。

替换为（无条件短语改为按需）：

> 请调用 submit_final_answer 单独提交最终答案；已登记数据任务时在
> findings 中引用对应 evidence_id。

纯解释答案不再被提醒推向补造证据引用。待处理任务摘要逻辑不变。

## 6. 归档条件（runner.py）

`_archive_run`（`runner.py:1218-1275`）现状：

```python
has_real_execution = <本 run 存在非沿用的 query/pushdown 执行>
condition_b = <合法提交为 completed/partial 且 findings 引用了沿用证据>
if has_real_execution or condition_b:
    write_qa(...)      # QA 归档
else:
    write_run_errors(...)
```

新增第三条件（复用 `_archive_run` 已接收的 `decision` 参数，经其任务摘要
判定零任务——`decision.outcome_summaries` 由全部任务记录生成，"从未登记"
时为空列表，"仅作废"时非空，与 §3.1 分支口径一致）：

```python
condition_c = (
    decision is not None
    and decision.accepted
    and not decision.outcome_summaries   # 零任务直接回答
)
if has_real_execution or condition_b or condition_c:
    write_qa(...)
```

不改归档结构、不新增存储；直接回答的 `evidence` 为空列表、`stats` 为
正常的 RunStats 对象（非空列表），照常写入。

## 7. submit 工具说明与拒绝提示（tools/final_answer.py）

- 工具 docstring 补充："无需取数的问题可不登记任务直接提交，findings 为空，
  澄清问题写入 answer 与 open_questions；已登记数据任务时按既有规则提交
  findings 与证据引用。"
- 两个拒绝码的 hint 分别措辞（不得互相承诺对方的恢复路径）：
  - `direct_answer_task_refs_not_allowed`（零任务但携带 findings/任务
    引用）："清空 findings 与任务引用后直接重提；澄清问题写入 answer 与
    open_questions。"
  - `task_not_registered`（含"登记后全部作废"形态）："已作废全部任务时
    当前不支持切换为零任务直接回答；数据问题请重新登记真实任务，无需
    补做无关 SQL。"
- 提交 schema、参数校验、错误信封结构不变（不新增字段）。

## 8. 明确不改清单

| 组件 | 状态 |
| --- | --- |
| 已登记数据任务全链路（登记绑定/成果核验/覆盖/status） | 原样，既有测试全部保持通过 |
| SQL guard | 原样（含 doc 18 §7.5 带时刻边界实测，另行安排） |
| 时间解析器（doc 16） | 原样 |
| reuse_evidence / 证据编号体系 | 原样（2026-11-13 已有真实沿用） |
| 任务账本 schema / 依赖图 | 原样（doc 18 开放问题的其他项不在本方案） |
| 展示精简（数据查询案例） | doc 17 |
| 系统兜底（崩溃/无答案/预算停止） | 原样 |

## 9. 修改文件清单

| 文件 | 修改 |
| --- | --- |
| `src/modules/db_deepagent/services/task_completion.py` | `evaluate_submission` 新增零任务直接分支（§3） |
| `src/modules/db_deepagent/config/prompts.yaml` | 登记指令收窄 + 防跳查约束（§4.1） |
| `src/modules/db_deepagent/skills/query-planning/SKILL.md` | 同步登记边界（§4.2） |
| `src/modules/db_deepagent/middleware/final_answer_gate.py` | 提醒文本（§5） |
| `src/modules/db_deepagent/runner.py` | 归档条件 `condition_c`（§6） |
| `src/modules/db_deepagent/schemas.py` | 仅注释同步：`DbAgentFinalAnswer` 的 `open_questions` 注释补零任务直接澄清例外（不改字段与校验结构） |
| `src/modules/db_deepagent/tools/final_answer.py` | 工具说明 + 拒绝 hint（§7） |
| `src/tests/db_deepagent/unit/`（见 §10） | 新增/更新测试 |

## 10. 测试计划（TDD：先写测试，看红再实现）

### 10.1 `task_completion` 直接分支（新增用例）

1. 零任务 + completed + findings=[] → accepted，expected_status=completed，
   finding_summaries 空，coverage_map 空，TASK_COMPLETION_RULES_VERSION=2；
2. 零任务 + needs_clarification（澄清问题在 `answer` 正文 +
   `open_questions`）→ accepted；
2a. 零任务提交携带 `clarification_question` 字段 → 参数层
    `extra_forbidden`（schema 回归，`extra="forbid"`）；
3. 零任务 + completed + findings 非空 → 拒
   `direct_answer_task_refs_not_allowed`；
4. 零任务 + unresolved_task_refs 非空 → 同上拒；
5. 零任务 + failed → 拒（不开放状态）；
5a. 零任务 + 无有效提交（`normalized_submission=None`）→ accepted=False，
    维持现有未提交收尾行为（Runner 兜底），不读取提交字段；
6. 仅作废任务（账本含 voided 记录）→ 维持 `task_not_registered`（v1
   边界回归保护）；
7. 存在未作废任务 → 既有全流程行为不变（由既有测试回归覆盖）。

### 10.2 submit 工具联通

8. 零任务直接提交返回 command（非错误信封），FinalAnswerRecord 登记成功
   （answer_source 由 Runner 生成，不在工具层断言）；
9. 两个拒绝码（`direct_answer_task_refs_not_allowed` /
   `task_not_registered`）的 hint 分别与 §7 文本一致。

### 10.3 Runner 收尾与归档

10. 真实 Runner 收尾路径：零任务、零执行、直接回答 accepted →
    `write_qa` 被调用、`write_run_errors` 未被调用，answer_source=model；
11. 直接回答的交付正文与 answer 一致，无"结论 F-"/"已完成"/"已作废"
    追加段（`build_delivery_answer_section` 输出空）；
12. 数据查询正常路径归档行为不变（回归）；
12a. API 历史回读：直接回答经既有历史接口回读，正文与实时交付一致
    （QA 归档验证不替代历史回放验证，使用现有离线测试设施）。

### 10.4 middleware / 提示词

13. 收卷提醒文本不再含无条件"并引用已有 evidence_id"；
14. prompts.yaml / SKILL.md 含"无需登记任务直接回答"、防跳查句与
    :48/:77/:84 三处限定（文本断言，防漂移）。

### 10.5 回归基线

全量 `pytest src/tests/db_deepagent`：当前基线 **1139 passed / 4 skipped**
不回退；预计净增约 17 个用例。

## 11. 验收标准

### 11.1 离线（单测）

§10 全部通过；`ruff check` / `black --check` 通过。

### 11.2 真实场景（WebUI，日志核对）

| # | 场景 | 预期 |
| --- | --- | --- |
| 1 | "请问大前天是哪一天" | 直接回答日期；db_execute=0；无任务登记/作废；无追加审计段 |
| 2 | 需澄清的模糊问题（如"最近的订单情况"不给口径） | 零任务提交澄清问题，正常展示 |
| 3 | "9月份到现在的订单总数" | 原路径不变：登记→查询→成果→交卷，一次通过 |
| 4 | 混合："大前天是哪一天？那天有多少订单？" | 日期在 answer 直接解释；订单数量来自查询证据（findings 仅数据部分） |
| 5 | 场景 1/2 的历史 | 刷新后 QA 归档正文与实时一致（不再进 run_errors） |
| 6 | 反例观察（不阻断）：纯数据问题若被模型判为无需取数 | 裸答案、无表格无 SQL；记录出现频率，作为提示词调优输入 |

## 12. 风险、边界与回退

### 12.1 判断权交给模型（已知松开的一角）

"是否需要取数"由主 Agent 语义判断，程序只验证已登记数据任务的成果，
**不能**证明零任务答案的每句话非编造。缓解：提示词要求"库内数据结论
必须依据实际查询、结果计算或受控沿用的有效成果"（§4.1 同款措辞）；
误判产物（无表格无 SQL 的裸答案）用户可辨识；不为此增加关键词
分类器或审核模型（doc 18 三轮审核一致方向）。

### 12.2 "登记后全部作废"仍是死角（v1 有意保留）

直接分支仅覆盖"从未登记"。模型若先登记再作废（如本案原路径），仍会
`task_not_registered`，且**当前不支持切换为零任务直答**（§7 该拒绝码的
hint 如实说明，不承诺"清空 findings 可恢复"）。缓解：§4 提示词从源头
减少登记冲动。若真实使用中该形态高频出现，再评估是否放开"全部作废"
提交。

### 12.3 提示词遵从度

模型可能仍习惯性登记任务（旧指令惯性）。验收场景 1/2 若不达预期，先检查
提示词/Skill 修改是否生效与一致，不加程序强制。

### 12.4 回退

本方案无 schema、存储、接口变更；回退 = revert 六个文件的改动，数据任务
路径自始至终未动。
