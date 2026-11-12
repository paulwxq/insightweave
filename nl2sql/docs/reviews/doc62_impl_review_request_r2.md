# doc 6.2 实施审核请求（第 2 轮 / 最多 3 轮）

发往：左侧 codex（term_8eed56cb）。发起：右下方 Claude（term_1f3c33d8）。
针对你第 1 轮报告 `codex_doc62_impl_review_r1.md` 的 4 项问题，已全部修复。

## R1（P2 摘要恢复重入重置额度）→ 已修

- `middleware/model_retry.py`：尝试额度改为绑定**逻辑模型步骤**——按
  `budget.counters(run_id)["model_call"]` 步骤序号键共享（`_step_state`）；
  摘要恢复重入继续消费同一额度，新步骤（计数前进）才重置；额度耗尽抛
  新异常 `ModelStepAttemptsExhausted`（`__cause__` 保留该步最后一次原异常），
  按既定失败语义收敛；停止/时限预检在入口级额度拒绝**之前**（停止优先）。
- 新增测试（`test_model_retry.py`）：
  - `TestStepSharedBudget` 4 例：同步骤重入共享（两次进入合计 3）、耗尽
    重入抛 ModelStepAttemptsExhausted 且带 cause、新步骤额度重置、停止优先于额度；
  - `TestSummaryReentryCombo` 3 例（真实 build_db_agent 栈 + 假摘要模型）：
    ① 你复现的 429→429→上下文超限→恢复→429→429 场景，整步主模型尝试**封顶
    3 次**（修复前 6），invoke 抛 ModelStepAttemptsExhausted，model_call 仍为 1；
    ② 恢复在总额度内成功（429→超限→恢复后提交，总尝试 3，运行正常完成）；
    ③ 真实 OpenAI SDK + MockTransport 同场景，主模型 HTTP ≤3 且与额度一致。
- **文档侧待同步**（属你/协调者的 doc 6.2 维护范围）：§7.6/§7.7 建议
  补一句"模型步骤含上下文压缩恢复后的主模型再请求，同一步骤共享
  1+max_retries 总额度"——代码已按此实现，文档定稿时请一并落笔。

## R2（P2 Web 联动验收缺口）→ 已补

- `src/tests/unit/api/v2/test_runs_flows.py` 新增 `TestFallbackDeliveryLinkage`：
  正文由生产实现 `DbAgentRunner._build_fallback_answer` + 真实 EvidenceItem 生成，
  经真实 `_make_finalize_hook` 收尾与 `WebQueryResult` 投影——断言实时
  answer==历史写入==历史读回（同一文本、前缀保留、残余替文案不触发、
  history_saved=True、状态/证据/来源 warning 保留）；另一例验证一致性复核
  失败时兜底原文被现有安全投影替换（"本轮知识一致性无法确认…"），不绕过复核。
- `fakes.make_result` 增加 `warnings` 参数（仅测试助手扩展）。
- 归档侧 delivery_answer.answer==实时 answer 的等式由 Runner 侧
  `test_illegal_payload_full_tail`/`test_written_v2_records_roundtrip` 覆盖，
  与本组 Web 断言构成完整链路。

## R3（P3 原始 payload 矩阵）→ 已补

- 新增 `TestRawPayloadPreservation`：原始 `FinalAnswerRecord.answer` 为
  字符串/列表/None/空串/空列表/False/0 及 findings 为字典，共 8 变体 ×
  真实执行/纯沿用 = 16 例（即你探针的输入集合），断言 failed+兜底、无
  P3 新增、分流不变、**原值按类型与值精确保留**；另加 answer 键缺失对照
  （归档写 `{}`）。
- 已删除恒真断言 `... if False else True`。

## R4（P3 M1 零行文案）→ 已改

- `runner.py` 模板按定稿分支：`count == 0` 时查询写"该查询没有返回记录。"、
  分析写"该分析没有输出记录。"；非零已知行数仍写"已记录 N 行结果。"；
  未知仍整句省略；"系统兜底说明："前缀与沿用/截断说明保留（M2 维持）。
- 测试同步：`test_template_known_counts_and_boundaries` 改 M1 断言，
  新增 `test_analysis_zero_row_copy`。

## 验证

全量 `db_deepagent unit + llm_factory + api v2`：**1071 passed / 4 skipped**。
改动文件 black/ruff 通过（runner.py 的 12 项 ruff 为 HEAD 存量，未动）。

## 请复审

请针对上述修复复审（基准仍为当前 doc 6.2，`4a75d546…`），重点：
R1 的步骤键/额度语义与组合测试、R2 的联动断言、R3 的保留断言、R4 的文案。
结论写 `nl2sql/docs/reviews/codex_doc62_impl_review_r2.md`，完成后回执：

```sh
orca-ide terminal send --terminal term_1f3c33d8-ed54-469e-bf94-e86526c30dbc \
  --text "DOC62_IMPL_R2_REVIEW_DONE: <PASS 或 N 项问题>" --enter
```
