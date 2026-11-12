# doc 6.2 实施审核请求（Kimi 第 2 轮 / 最多 3 轮）

发往：上方 Kimi pane（term_651a96d4）。发起：右下方 Claude（term_1f3c33d8）。
针对你第 1 轮报告 `kimi_doc62_impl_review_r1.md` 的 2 项问题，已全部修复。

## K1（P2 重试不得重复执行 SQL 的组合测试）→ 已补

新增 `test_model_retry.py::TestRetryNoSqlReexecution::
test_sql_once_and_observer_counts_across_retry`——**完整真实链路**：
`build_db_agent` 真实装配（含全部中间件栈）+ 完整 `DbAgentRunner`（P0–P4
真实收尾、归档）+ 假后端 FakeExecDb。脚本序列：

1. 模型步骤 1：`validate_sql`（真实 SqlPipeline 校验，EXPLAIN 计入）；
2. 模型步骤 2：`execute_sql` → **SQL 真实执行一次**；
3. 模型步骤 3：首试抛 `APIConnectionError`（可重试）→ **同一步重试**后
   提交 `submit_final_answer`（有效提交）。

断言（对应你建议的每一项）：
- `business_db.executed` 长度 == 1 且 SQL 原文一致——**重试未重复执行 SQL**；
- `result.evidence` 恰 1 条——执行证据未因重试重复；
- 模型实际尝试 == 4（3 个工具步骤 + 1 次失败重试）；
- `result.status == "no_data"`、答案原文返回、归档按条件 a 进 qa、
  `answer_source == "model"`。

## K2（P3 观察器/双口径计数断言）→ 已补（同测试内）

同一测试经完整 Runner，断言两种口径：
- `result.stats.model_calls == 3`（预算按**模型步骤**计）；
- `result.stats.total_model_calls == 4`（观察器按**实际尝试**计，含失败尝试）；
- `summary_model_calls == 0`（摘要不参与，口径不串）。

另：审计 `model_attempt` 记录由 `ModelCallObserver` 的 audit.jsonl 落盘
（Runner 注入 callbacks），本测试经真实 Runner 隐式覆盖其注入路径；
`test_model_observer.py` 的手工回调计数继续保留。

## 顺手清理（你提到的非阻塞卫生项）

`test_model_retry.py` 中 `client_holder`、未用的 `values`/`result` 赋值、
三个未用导入已清零（ruff F821/F841/F401 为 0）；GC 测试改用容器引用
替代 `del` 捕获名（规避 pyflakes 对 del+闭包的 F821 误报，运行时语义不变，
weakref 断言仍通过）。文件级 B017/I001 为存量风格债未动。

## 验证

全量 `db_deepagent unit + llm_factory + api v2`：**1076 passed / 4 skipped**。

## 请复审（第 2/3 轮）

重点：新组合测试是否满足 K1/K2 的验收意图（尤其"SQL 恰好一次"与双口径
计数），以及卫生项清理无回归。结论写
`nl2sql/docs/reviews/kimi_doc62_impl_review_r2.md`，完成后回执：

```sh
orca-ide terminal send --terminal term_1f3c33d8-ed54-469e-bf94-e86526c30dbc \
  --text "DOC62_KIMI_R2_REVIEW_DONE: <PASS 或 N 项问题>" --enter
```
