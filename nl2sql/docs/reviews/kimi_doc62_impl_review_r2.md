# doc 6.2 实施复审：Kimi 第 2/3 轮

**结论：PASS。** 第 1 轮的 K1、K2 均已关闭，卫生项清理达到本轮约定范围；未发现新的生产代码或正式测试问题。

审核范围：当前工作区未提交的 doc 6.2 A+B 实现和测试，重点复核新增完整 Runner + 真实 Agent 装配组合测试。只审未改业务代码或正式测试。

## K1 复核：重试恢复不重复执行 SQL——通过

新增 [test_sql_once_and_observer_counts_across_retry()](../../src/tests/db_deepagent/unit/test_model_retry.py:1097) 使用真实 `build_db_agent()` 中间件装配、真实 `DbAgentRunner` P0—P4 收尾、`SqlPipeline` 和 FakeExecDb，脚本顺序为：

1. validate_sql；
2. execute_sql；
3. 后续模型步骤首次抛 APIConnectionError，同一步重试后 submit_final_answer。

测试直接断言：

- `business_db.executed` 长度为 1，且 SQL 原文一致（[第 1166—1169 行](../../src/tests/db_deepagent/unit/test_model_retry.py:1166)）；
- 返回证据恰为 1 条，未因重试重复（第 1170 行）；
- 模型实际尝试 4 次，证明确实经过了失败和恢复，而非未触发重试的假日程（第 1174 行）；
- 正常模型答案、no_data 状态和 QA/model 来源保持正确（第 1171—1181 行）。

这满足 K1 的验收意图：模型请求恢复发生在已有 SQL 结果之后，且不重跑 Agent、SQL 或执行证据。

## K2 复核：观察器与双口径计数——通过

同一测试经过完整 Runner，因此使用生产 Runner 的 ModelCallObserver 回调注入和统计收尾，而非仅检查模型替身计数。

断言包括：

- `result.stats.model_calls == 3`：validate、execute、提交三个逻辑模型步骤，步骤内重试不重复预占；
- `result.stats.total_model_calls == 4`：观察器计入三个成功工具步骤和一次失败后的实际重试；
- `result.stats.summary_model_calls == 0`：摘要调用不串入该场景；
- 独立的 `model.attempts == 4` 与观察器计数互相印证。

证据见 [test_model_retry.py:1173](../../src/tests/db_deepagent/unit/test_model_retry.py:1173)。这固定了 doc 6.2 §7.7 的“步骤数”和“实际尝试数”两种口径，关闭 K2。

审计文件由同一 Runner/Observer 路径生成；本轮没有将“隐式经过注入路径”扩大表述为逐条审计内容断言。

## 卫生项复核

对 `test_model_retry.py` 执行：

```bash
.venv/bin/ruff check --select F821,F841,F401 \
  src/tests/db_deepagent/unit/test_model_retry.py
```

结果：**All checks passed**。第 1 轮指出的未使用变量、未使用导入和闭包捕获告警已清理；GC/weakref 测试仍在完整回归中通过。

## 实际验证

新增组合测试单独执行：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit/test_model_retry.py::TestRetryNoSqlReexecution::test_sql_once_and_observer_counts_across_retry -q
```

结果：**1 passed**。

完整相关范围：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py \
  src/tests/unit/api/v2 -q
```

结果：**1076 passed，4 skipped**。

`git diff --check` 亦通过。未访问真实模型或数据库；异步入口、强杀进程、硬 deadline 和仓库外归档消费者仍按文档既定边界，不在本轮 PASS 的外推范围内。
