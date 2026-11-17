# doc 6.2 实施审核请求（Kimi 第 1 轮 / 最多 3 轮）

发往：上方 Kimi pane（term_651a96d4）。发起：右下方 Claude（term_1f3c33d8）。
时间：2026-11-12 23:05 前后。用户指示：请基于 doc 6.2 审核代码，问题反馈给我，
我处理后发回复审，最多三轮，超过请人类介入。

## 请求

我已按 `nl2sql/docs/6.2_db_deepagent最终答案兜底问题与补齐建议.md`
（当前 SHA256 `cf6613f6…`）完成 A+B 全部代码与测试。请基于该文档审核
**工作区未提交的实现代码与测试**（分支 `feature/multi-source-knowledge-isolation`，
`git status` / `git diff` 查看；HEAD 为 78d24f9）。

## 改动清单（均未提交）

**A：Runner 确定性兜底**
- `src/modules/db_deepagent/schemas.py`：`extract_raw_final_answer_payload()`、
  `has_nonblank_answer()`、`normalize_valid_final_answer_payload()`、
  `normalize_final_answer_record()`（外层 run_id/request_id 严格归属）+
  `DbAgentFinalAnswer` 非空白正文 validator。
- `src/modules/db_deepagent/runner.py`：`_known_saved_row_counts()`（可靠行数
  映射）、`_build_fallback_answer()`（M1 零行文案版）、`_evaluate()` 归一提交
  视图、`_assemble_evidence(known_row_counts=…)`、`_finalize(invoke_started=
  False)` 一次归一 + delivery_payload + answer_source 三值、`_archive_run()`
  归档 v2（原 answer 保留 + delivery_answer/answer_source/archive_schema_version）。
- `src/modules/db_deepagent/persistence/archive.py`：版本常量 +
  `read_delivery_answer()`（v1 status→submitted_status 改名）。
- `src/tests/db_deepagent/conftest.py`、`test_runner_persistence.py`（完整收尾
  矩阵/归属/一次归一/行数/归档 v2/raw 原值矩阵）、`test_final_answer.py`、
  `test_cli_json.py`。

**B：主模型有限重试**
- `config.py` `ModelRetryConfig`（代码默认关）+ `db_deepagent.yaml` model.retry；
- `llm_factory.py` max_retries override；
- `middleware/model_retry.py`：白名单 + `BudgetAwareModelRetryMiddleware`
  （步骤级共享额度：按 `budget.counters` 的 model_call 步骤序号，摘要恢复重入
  消费同一额度，耗尽抛 `ModelStepAttemptsExhausted`；停止/时限预检优先；
  零项目层退避）；
- `agent.py`：build_model 强制客户端 max_retries=0 + 同步/异步 root 客户端
  双端核验；装配在请求审计外层；
- `services/run_budget.py`：`_RunEntry.extra` + `run_shared_state()`（步骤状态
  随 close_run 释放）；
- `test_model_retry.py`（MockTransport HTTP 次数 + 摘要重入组合 + 生命周期
  释放）、`test_config.py`、`test_assembly.py`、`test_llm_factory.py`、
  `src/tests/unit/api/v2/`（Web 联动 + fakes）。

## 审核重点建议

对照 doc 6.2 §7.2（归一/行数/模板边界）、§7.3（invoke_started/三值来源）、
§7.4（归档 v2 与条件 b 资格分离）、§7.5（单层重试/原生拒绝/None 即拒）、
§7.6（步骤共享额度语义、生命周期释放）、§7.7（计数口径）、§7.8（测试矩阵
覆盖度）。也欢迎指出文档不一致或测试缺口。

当前全量：`db_deepagent unit + llm_factory + api v2` 1075 passed / 4 skipped。

## 回传约定

- 只审不改业务代码与正式测试；结论写
  `nl2sql/docs/reviews/kimi_doc62_impl_review_r1.md`
  （编号 + 严重性 + 代码证据/行号 + 影响 + 建议，区分实质问题与可选增强）。
- 写完后执行回执（<结论> 换成 PASS 或 N 项问题）：

```sh
orca-ide terminal send --terminal term_1f3c33d8-ed54-469e-bf94-e86526c30dbc \
  --text "DOC62_KIMI_R1_REVIEW_DONE: <结论>" --enter
```
