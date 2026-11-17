# db_deepagent 答案展示精简与口径说明分离修改方案

> 状态：设计稿，尚未实施。依据 2026-11-15 用户截图与当前代码核对。
> 目标：对话默认展示简洁业务答案，去掉展开的技术口径和重复的成功任务说明；保留结果表格、查看 SQL，以及真实的失败、限制和澄清信息。

## 1. 用户看到的问题与代码来源

当前示例回答包含三部分：

```text
今年初第一个月（2026年1月）的订单数量为26单。

口径说明：
- 统计范围：public.orders 表
- 时间字段：order_date（date 类型）
- 时间区间：[2026-01-01,2026-02-01)
- 统计指标：COUNT(*)

- 结论 F-1（T1@1）：今年初第一个月……26单。
- 已完成：查询今年初第一个月的订单数量
```

这不是一个独立的前端“口径说明”组件，而是不同来源的文本进入同一个 `answer` 字符串：

| 内容 | 当前来源 |
| --- | --- |
| 业务结论及“口径说明” | 主 Agent 调用 `submit_final_answer` 时提交的 `answer` 正文 |
| `结论 F-1（T1@1）`、`已完成` | Runner 调用 `build_delivery_answer_section()` 后追加到正文 |
| 页面展示 | API 传递 `result.answer`；前端 `MessageList.vue` 将其交给 `AnswerCard.vue`，后者通过 `{{ answer }}` 展示 |

旧提示词及结果分析 Skill 已要求“写明口径”；doc 16 新增日期工具的 `interpretation`，但没有增加固定的“口径说明”前端栏目。doc 13 的 DEBUG 模型输出日志已记录提交的完整 JSON 参数。

## 2. 本次采用的方案

复用已有结构化字段，调整答案组织说明和 Runner 的正文追加行为。无需新增 API 字段、数据库结构或前端展示组件，也不使用正则从正文中删除段落。

本次只改变 `db_deepagent` 的回答呈现，不修改日期解析、SQL 生成与校验、任务终态判断、证据覆盖或旧 LangGraph 链路。

### 2.1 字段分工

| 字段 | 本次约定 |
| --- | --- |
| `answer` | 面向用户的简洁业务答案；必要的业务范围、限制、失败或澄清说明仍写入正文 |
| `metric_definitions` | 详细统计口径，如技术表名、字段名、聚合方法和时间边界表示；当前页面不单独展示此字段 |
| `findings` | 继续提交完整的结论、任务引用和证据引用，参与既有校验并保留在结构化响应和归档中；不再机械地将每条成功 finding 追加到正文 |
| `limitations` | 保留现有结构化限制信息；影响答案含义的限制还应在正文中简短说明 |
| `open_questions` | 保留现有问题合并和页面展示机制 |

例如模型应提交：

```json
{
  "status": "completed",
  "answer": "2026年1月的订单数量为26单。",
  "metric_definitions": [
    "统计 public.orders 表的订单记录数，聚合方式为 COUNT(*)。",
    "使用 order_date 字段，时间范围为 [2026-01-01,2026-02-01)。"
  ],
  "findings": [
    {
      "finding_id": "F-1",
      "text": "2026年1月的订单数量为26单。",
      "task_refs": [{"sub_task_id": "T1", "revision": 1}],
      "evidence_ids": ["实际工具返回的证据ID"]
    }
  ]
}
```

示例中的证据 ID 仅为说明，实施和真实运行必须继续引用实际工具返回的标识。

### 2.2 回答组织规则

- 简单问题直接回答结果，保留必要的年份、业务对象、单位等信息。
- 多问题逐项回答实质结论，用业务名称区分，不展示任务号、revision、finding ID 或 evidence ID。
- 默认不在正文展开“口径说明”段落、技术字段清单或内部区间表示；详细内容写入 `metric_definitions`。
- 用户明确询问统计方法、计算口径或技术细节时，可以在正文中回答这些内容，不把本次精简规则变成拒绝解释的限制。
- 默认年份、重新执行、样本不完整等事项若影响用户理解，仍应使用简短业务语言说明。不能因精简正文而隐藏失败、数据限制或待澄清事项。
- `answer` 本身应包含足够的业务结论，不能只写“已处理”并要求用户到 findings 中寻找结果。

## 3. 提示词和工具说明修改

### 3.1 `config/prompts.yaml`

当前结束规则中的“写明口径、限制、未完成事项”应改成明确的字段分工，避免模型继续将详细技术口径全部写入正文。

建议补充：

```text
answer 面向用户直接回答业务问题，简洁完整；多问题逐项给出实质结论。
默认不在 answer 展开技术口径清单，不展示内部表字段、任务号或证据编号。
详细统计口径写入 metric_definitions；findings 仍按既有规则完整提交。
影响结论的范围、限制、失败和待澄清事项，仍在 answer 中简短说明。
用户明确询问口径、计算方法或技术细节时，可在 answer 中解释。
```

上述要求只指导表达，不增加模型调用、语义判定器或格式拒绝规则。

### 3.2 `skills/result-analysis/SKILL.md`

将完成条件中的“写清采用口径”改为上述字段分工。保留现有证据引用、结果截断和比较口径要求；这些要求用于保证回答正确，不应因减少展示内容而删除。

### 3.3 `tools/final_answer.py`

在 `submit_final_answer()` 的工具说明中补充：`answer` 提交简洁完整的用户答案，详细口径放入 `metric_definitions`。保留全部参数、字段类型、默认值及既有校验。

无需给 `DbAgentFinalAnswer` 增加字段，也不解析或改写已经提交的 `answer`。

## 4. Runner 正文追加逻辑修改

### 4.1 当前逻辑

[`runner.py`](../src/modules/db_deepagent/runner.py) 的 `_finalize()` 在接受合法模型提交后，无条件调用：

```python
section = build_delivery_answer_section(decision)
if section:
    answer = f"{answer}\n\n{section}"
```

[`task_completion.py`](../src/modules/db_deepagent/services/task_completion.py) 的 `build_delivery_answer_section()` 会输出 finding 编号、任务版本、已完成任务、失败、待澄清及作废说明，因此即使模型答案已经简洁，页面仍有重复的成功条目。

### 4.2 建议修改

给现有函数增加一个仅关键字参数：

```python
def build_delivery_answer_section(
    decision: CompletionDecision,
    *,
    include_audit_details: bool = True,
) -> str:
    ...
```

- `include_audit_details=True`：保留现有完整输出，用于 DEBUG 诊断；默认值保留原函数行为。
- `include_audit_details=False`：跳过 finding 技术标签、completed 和 voided 计划审计条目；继续输出 failed、needs_clarification 和权威澄清问题。
- 保留现有精确去重机制，不新增自然语言相似度判断、结果匹配规则或额外 LLM 调用。

Runner 对合法模型答案使用精简模式：

```python
section = build_delivery_answer_section(decision, include_audit_details=False)
if section:
    answer = f"{answer}\n\n{section}"
```

完全成功的示例因此不会追加任何任务段落。部分成功、失败或需要澄清时，仍保留相应业务说明；原有 `open_questions` 合并逻辑不变。

### 4.3 与 doc 6.3 的关系

本次有意调整 doc 6.3 中“把所有 finding 和任务终态机械追加到交付正文”的展示规则。新的规则是：**用户正文由主 Agent 组织，完整 findings 和任务审计保留在结构化数据及后台记录中。**

任务是否完成、findings 是否覆盖全部已完成任务、证据是否合法，仍由现有代码判断；本次不删除或绕过这些校验。

相应地，正文不再通过程序保证重复展示每条 finding。模型若只提交“已处理”，既有结构校验仍可能接受；本次依靠明确的答案组织提示词与真实查询验收改善该行为，不为此增加语义门控。不能一面取消追加，一面仍声称保留了旧的正文覆盖保证。

### 4.4 系统兜底

本次不修改 `_build_fallback_answer()` 和 `_fallback_task_section()`。无有效模型答案、执行异常或预算停止时，继续展示可信的结果与未完成说明，不通过正文过滤隐藏故障。

## 5. 日志、归档与各入口

### 5.1 日志保留

- doc 13 已在 DEBUG 模型输出中记录完整提交参数；新填写的 `metric_definitions` 会随该 JSON 一并记录。
- Runner 可在 DEBUG 开启时，调用完整模式的 `build_delivery_answer_section(decision)`，记录 request_id/run_id 及完整分项段落。这份诊断文本不拼入用户正文。
- 排版前使用现有 logger 的 DEBUG 级别检查，不新增日志文件或日志服务。

### 5.2 归档与历史

保留现有归档结构：原模型提交仍保存于 `answer`，实际交付仍保存于 `delivery_answer`，其中各自的 `metric_definitions` 保留详细口径。无需增加归档版本号。

新轮次的实际交付正文通过既有流程写入 Web 历史，因此实时结果与刷新后的新历史内容一致。对外 `answer` 统一精简，CLI 读取同一结果时也不再显示正常成功任务的审计条目。

已保存的旧历史不会自动变化。本次不批量重写旧记录、不在前端按标题或横线截断旧答案。

### 5.3 前端

[`MessageList.vue`](../../frontend/src/modules/nl2sql/components/MessageList.vue) 和 [`AnswerCard.vue`](../../frontend/src/modules/nl2sql/components/AnswerCard.vue) 继续展示 `answer`，保持结果表格、查看 SQL、导出及澄清问题现有行为。

无需从 `findings` 临时拼一份页面答案，也不增加按状态选择不同答案字段的前端逻辑。详细口径不单独展示，但仍可在响应、日志与 QA 归档中查看。

## 6. 修改文件清单

| 文件 | 具体修改 |
| --- | --- |
| `src/modules/db_deepagent/config/prompts.yaml` | 明确 answer 与 metric_definitions 的分工；要求完整业务结论、默认精简技术说明 |
| `src/modules/db_deepagent/skills/result-analysis/SKILL.md` | 同步答案组织规则，保留证据、截断及业务限制要求 |
| `src/modules/db_deepagent/tools/final_answer.py` | 在工具说明中补充分工；不修改提交 schema 和校验逻辑 |
| `src/modules/db_deepagent/services/task_completion.py` | `build_delivery_answer_section()` 增加 include_audit_details 参数，精简模式仅保留失败和澄清相关说明 |
| `src/modules/db_deepagent/runner.py` | 合法模型答案采用精简模式追加；完整任务段落写 DEBUG；兜底行为不变 |
| `src/tests/db_deepagent/unit/test_doc63_r2_regressions.py` | 区分完整审计模式与精简展示模式，覆盖成功、失败、澄清及作废任务 |
| `src/tests/db_deepagent/unit/test_runner_persistence.py` | 更新正常答案被追加成功条目的旧断言，验证实际正文、结构化口径、原提交及归档一致性 |
| `src/tests/unit/api/v2/` 相关测试（按实际影响） | 验证现有字段完整传递，新历史与实时答案一致；不新增接口或字段 |

实施前查找其他依赖“正文必有 finding 标签/已完成条目”的断言并同步评估。不能只让原审计模式测试通过，就声称精简模式已验证。

无需修改前端组件、日期解析器、SQL guard、任务账本 schema 或模型工厂。doc 16 当前未提交修改保持原样，本方案不混入日期功能修整。

## 7. 验证与验收

### 7.1 离线验证

1. 完整审计模式仍能生成 findings、任务状态和引用标签；精简模式在全部成功时返回空追加段落。
2. 精简模式保留失败原因、待澄清问题，继续按既有机制去重；不展示 completed 和 voided 审计条目。
3. Runner 使用模拟提交：简洁 `answer` 加非空 `metric_definitions`，验证成功正文没有被追加 `结论 F-`、`T1@1`、`已完成` 等内部内容，findings 与口径字段仍完整。
4. 验证原提交与实际交付的归档字段正确；新 Web 历史正文与实时结果相同，详细口径仍存在于原提交和交付 payload。
5. 失败、澄清、部分成功及系统兜底路径仍保留原本需要说明的事实；既有证据覆盖和终态校验测试继续通过。
6. DEBUG 日志能保留口径提交及完整任务诊断；关闭 DEBUG 不构造额外诊断正文。
7. 使用仍含“口径说明”的模拟旧式 answer 时，确认系统不会偷偷截断正文。口径分离是提示词指导，不是代码对任意文本的绝对过滤。

### 7.2 真实查询验证

在用户安排的正常查询中，复测截图对应问题，预期对话正文为类似：

```text
2026年1月的订单数量为26单。
```

保留表格和 SQL 查看入口；详细技术口径出现在 `metric_definitions` 和 DEBUG 提交日志中。数字以真实执行结果为准，测试和实现不得硬编码26。

另选多问题或部分失败场景，确认正文仍回答成功部分，并说明未完成部分；用户明确要求口径时能正常解释。

提示词不保证模型每次都严格分离字段，离线模拟测试也不能证明真实模型已遵守表达约定。真实查询如仍出现展开的技术口径，应先检查并调整提示词/Skill 的一致性，不直接增加前端截断、硬编码匹配或新一轮答案改写模型。
