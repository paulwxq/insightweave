# doc 6.2 实施审核：Kimi 第 1/3 轮

**结论：发现 2 项测试覆盖问题；未发现新的生产代码 correctness 阻断。** A 阶段归一、归属校验、可信行数、兜底和归档 v2 实现与文档一致；B 阶段单层重试、摘要恢复共享步骤额度及运行状态释放的实现与现有测试一致。以下两项均是文档明确要求、但当前正式测试没有直接验证的回归保障缺口，不表示已经复现生产功能错误。

审核范围：当前工作区未提交的 doc 6.2 A+B 实现和测试；只审未改业务代码或正式测试。

## K1 · P2 / 中：缺少“SQL 已成功，后续模型重试不得重复执行 SQL”的组合测试

**代码与测试证据**：

- doc 6.2 §5 要求“重试模型请求，不重跑整轮 Agent”，§7.8 的 `test_model_retry.py` 明确要求验证“SQL 不重复”，§8 也要求模型持续失败时“不重复执行 SQL”（文档第 182、849、869 行）。
- [test_same_step_retry_recovers_within_agent()](../../src/tests/db_deepagent/unit/test_model_retry.py:387) 的脚本只有“首次模型失败 → 重试后直接 submit_final_answer”；[模型脚本](../../src/tests/db_deepagent/unit/test_model_retry.py:367) 也只产生异常、普通文本或提交调用。
- 摘要恢复组合测试同样只覆盖主模型尝试和提交（[test_model_retry.py:638](../../src/tests/db_deepagent/unit/test_model_retry.py:638)），没有先完成 validate/execute，再让后续模型步骤失败并恢复。
- 当前 `test_model_retry.py` 没有 execute_sql、SqlPipeline、db_execute 或业务库执行次数断言。Runner 的 `SqlThenModelFail` 故障注入覆盖的是无重试恢复的最终兜底，不能替代“重试恢复 + 已有 SQL”的组合路径。

**影响**：

当前中间件只包装 `wrap_model_call()`，装配位置也未显示会重跑 Agent 或 SQL，因此本项不是对当前实现存在重复执行 bug 的指控。但如果后续把重试移到错误层级、改为重跑节点/整轮，现有测试仍可能全绿，无法守住本项最核心的副作用约束。

**建议**：

增加一个真实图装配的脚本序列：

1. 完成 validate_sql 和 execute_sql，并确认执行账本/假业务库计数为 1；
2. 后续模型步骤首次抛可重试异常，同一步重试后提交有效答案；
3. 断言主模型实际尝试 2 次、模型步骤预算只增加 1 次、Agent 没有重新 invoke、SQL/业务库执行仍恰好 1 次、执行证据没有重复。

该测试可同时覆盖 K2 的观察器断言。

## K2 · P3 / 低：没有直接验证重试尝试进入 ModelCallObserver / RunStats 计数

**代码与测试证据**：

- doc 6.2 §7.7 规定 `RunStats.total_model_calls` 是观察器看到的主模型及摘要模型尝试总数；§7.8 要求重试测试验证“回调计数”（文档第 816、825、849 行）。
- [test_model_retry.py](../../src/tests/db_deepagent/unit/test_model_retry.py) 没有使用 `ModelCallObserver`，也没有 `total_model_calls`、`callbacks` 或 `model_attempt` 断言。
- 当前测试使用模型替身的 `attempts` 和 HTTP MockTransport 计数，例如 [第 456—463 行](../../src/tests/db_deepagent/unit/test_model_retry.py:456)，能证明模型/HTTP 次数，但不能证明 Runner 注入的回调在每次重试中都生成独立、正确的观测记录。
- [test_model_observer.py](../../src/tests/db_deepagent/unit/test_model_observer.py) 以手工回调验证通用计数，没有覆盖真实重试中间件链。

**影响**：

当前生产代码通过 Runner 的 `config.callbacks` 注入观察器，结构上支持记录每次实际模型尝试；但若回调传播或重试包装在未来升级中变化，`RunStats.total_model_calls`、审计 `model_attempt` 和 token 完整性可能出现回归而现有测试不失败。

**建议**：

在真实 Agent 重试恢复测试中注入 `ModelCallObserver`，首次失败、第二次成功后可断言：

- `snapshot.total_model_calls == 2`，`summary_model_calls == 0`；
- 审计含一条失败和一条成功的 `model_attempt`；
- 若经完整 Runner 验证，则 `result.stats.total_model_calls == 2`，同时 `result.stats.model_calls == 1`，固定“步骤数”和“尝试数”的不同口径。

## 已核对通过的重点

- `normalize_final_answer_record()` 严格核对外层 run_id/request_id，Runner 单次收尾只归一一次；评估、交付和归档复用同一视图（[schemas.py:253](../../src/modules/db_deepagent/schemas.py:253)、[runner.py:599](../../src/modules/db_deepagent/runner.py:599)）。
- 原始 answer 与 eligible/normalized 视图分离，非法 payload/findings 不参与条件 b 遍历；原始空值和非法类型有完整归档矩阵（[runner.py:931](../../src/modules/db_deepagent/runner.py:931)、[test_runner_persistence.py:1461](../../src/tests/db_deepagent/unit/test_runner_persistence.py:1461)）。
- 行数映射只计算一次，未知行数不通过 EvidenceItem 的兼容零值进入模板；零行、沿用、截断和五条上限均有测试（[runner.py:760](../../src/modules/db_deepagent/runner.py:760)）。
- 归档 v2 保留原 answer，另存 delivery_answer/answer_source；v1 status 改名为 submitted_status，避免与 Runner 状态混用（[archive.py:84](../../src/modules/db_deepagent/persistence/archive.py:84)）。
- 主模型启用项目重试时强制同步/异步客户端 max_retries=0；代码默认关闭，旧原生 DashScope 配置不会仅因升级而拒绝启动（[agent.py:171](../../src/modules/db_deepagent/agent.py:171)、[config.py:35](../../src/modules/db_deepagent/config.py:35)）。
- 同一逻辑步骤在摘要恢复重入时共享尝试额度；已注册运行状态存于账本条目并随 close_run 释放，成功时及时清除 last_error（[model_retry.py:58](../../src/modules/db_deepagent/middleware/model_retry.py:58)、[run_budget.py:154](../../src/modules/db_deepagent/services/run_budget.py:154)）。此前连续运行测试的上下文问题也已修正为已登记的 run-lc。

## 实际验证

从 `nl2sql/` 执行：

```bash
.venv/bin/python -m pytest \
  src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py \
  src/tests/unit/api/v2 -q
```

结果：**1075 passed，4 skipped**。

```bash
git diff --check -- nl2sql/src \
  nl2sql/docs/6.2_db_deepagent最终答案兜底问题与补齐建议.md
```

结果：通过。

补充执行了变更文件的 Ruff 检查，未通过；输出含既存规则债务，也含新测试文件中的未使用变量/导入和泛化异常断言，例如 [test_model_retry.py:261](../../src/tests/db_deepagent/unit/test_model_retry.py:261) 的 `client_holder`、第 451 行的 `values`。这些不影响上述功能测试结论，可作为非阻塞代码卫生项清理，不应把全部 71 项都归因于本次生产改动。

## 审核边界

本轮未访问真实模型或数据库，未验证异步入口、强杀进程、硬 deadline 或仓库外归档消费者。结论覆盖当前同步 Runner、真实图装配、MockTransport 和上述单元/API 测试范围。
