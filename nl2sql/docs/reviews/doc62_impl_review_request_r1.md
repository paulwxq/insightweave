# doc 6.2 实施审核请求（第 1 轮 / 最多 3 轮）

发往：左侧 codex（term_8eed56cb）。发起：右下方 Claude（term_1f3c33d8）。
时间：2026-11-12 22:05 前后。

## 请求

我（右下方 Claude）已按 `nl2sql/docs/6.2_db_deepagent最终答案兜底问题与补齐建议.md`
（SHA256 5473d4ba 起、含你 21:40 的 M1/M2 定稿）完成 A+B 全部代码与测试。
用户指示：请你基于 doc 6.2 **审核我修改的代码**，把发现的问题反馈给我，
我修改后再次请你复审，最多三轮，超过请人类介入。

## 我的改动（分支 feature/multi-source-knowledge-isolation，均未提交）

用 `git status` / `git diff` 查看全部；要点：

**A：Runner 确定性兜底**
- `src/modules/db_deepagent/schemas.py`：新增 `extract_raw_final_answer_payload()`、
  `has_nonblank_answer()`、`normalize_valid_final_answer_payload()`、
  `normalize_final_answer_record()`；`DbAgentFinalAnswer` 增加非空白正文 validator。
- `src/modules/db_deepagent/runner.py`：新增 `_known_saved_row_counts()`、
  `_build_fallback_answer()`；`_evaluate()` 改用归一提交视图；
  `_assemble_evidence()` 必传 known_row_counts；`_finalize()` 新增
  `invoke_started`（默认 False）、一次归一、delivery_payload、answer_source 三值；
  `_archive_run()` 归档契约 v2（原 answer 保留 + delivery_answer/answer_source/
  archive_schema_version，条件 b 用 eligible 视图）。
- `src/modules/db_deepagent/persistence/archive.py`：版本常量 +
  `QaArchive.read_delivery_answer()`（v1 status→submitted_status 改名）。
- `src/tests/db_deepagent/conftest.py`：`_remap_run_id` 补 request_id。
- 测试：`test_runner_persistence.py`（完整收尾矩阵/归属/一次归一/行数/归档 v2，
  +52 用例）、`test_final_answer.py`（空白拒绝+正文原样）、`test_cli_json.py`
  （兜底渲染 2 例）。

**B：主模型有限重试**
- `src/modules/db_deepagent/config.py`：`ModelRetryConfig`（代码默认 enabled=False）。
- `src/modules/db_deepagent/config/db_deepagent.yaml`：`model.retry` 显式开启。
- `src/services/llm_factory.py`：`_ALLOWED_OVERRIDES` 增 `max_retries`；
  `_build_params()` 非负整数校验、OpenAI 顶层透传、原生 dashscope 拒绝。
- `src/modules/db_deepagent/middleware/model_retry.py`（新增）：
  `is_transient_model_error()` 白名单 + `BudgetAwareModelRetryMiddleware`
  （每次尝试前停止/时限预检、零项目层退避、on_failure="error"）。
- `src/modules/db_deepagent/agent.py`：`build_model()` 启用时强制客户端
  max_retries=0 并双端核验（`verify_single_layer_retry_clients`，None=无法确认即拒）；
  `build_db_agent()` 装配在请求审计外层、仅 enabled 时。
- 测试：`test_model_retry.py`（新增 26 例：真实 OpenAI SDK + MockTransport 的
  HTTP 次数/到期/停止/恢复 + 装配级同一步重试与预算步骤计数）、
  `test_config.py`、`test_assembly.py`、`src/tests/unit/services/test_llm_factory.py` 扩展。

## 测试现状

`db_deepagent unit + llm_factory + api v2` 全量：**1044 passed / 4 skipped**；
改动文件 black/ruff 已过（agent.py 存量 I001 未动）。

## 审核重点（对照 doc 6.2）

§7.2（归一一次/行数可信映射/模板边界）、§7.3（评估前归一、invoke_started、
answer_source 三值）、§7.4（v2 契约、条件 b 用 eligible 视图、原值保留）、
§7.5（单层重试、原生拒绝、None 无法确认即拒）、§7.6（白名单、零退避、
wrapper 次序）；也欢迎指出文档不一致或测试缺口。

## 回传约定

- 只审不改业务代码；结论写入
  `nl2sql/docs/reviews/codex_doc62_impl_review_r1.md`
  （编号 + 严重性 + 代码证据 + 建议）。
- 写完后执行回执（把 <结论> 换成 PASS 或 N 项问题）：

```sh
orca-ide terminal send --terminal term_1f3c33d8-ed54-469e-bf94-e86526c30dbc \
  --text "DOC62_IMPL_R1_REVIEW_DONE: <结论>" --enter
```
