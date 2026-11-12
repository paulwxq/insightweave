# doc 6.2 实施收尾记录

- 日期：2026-11-12（23:53 任务注入，终端降级交付）
- 对象：`docs/6.2_db_deepagent最终答案兜底问题与补齐建议.md`（收尾后工作区版本
  SHA256 `b3c271d1a2da846fcf3f6a1f2b1d9fc6919e473cb70df53b3d421faa36a08b53`）
- 分支：`feature/multi-source-knowledge-isolation`；本记录随实施一并本地提交

## 最终实现范围

**A：Runner 确定性兜底（doc 6.2 §7.1–§7.4）**
- `schemas.py`：`extract_raw_final_answer_payload()` / `has_nonblank_answer()` /
  `normalize_valid_final_answer_payload()` / `normalize_final_answer_record()`
  （外层 run_id/request_id 严格归属）+ `DbAgentFinalAnswer` 非空白正文 validator。
- `runner.py`：`_known_saved_row_counts()`（可靠行数映射，未知/非法/冲突均不计
  为可靠）；`_build_fallback_answer()`（M1 零行分型文案、M2 保留"系统兜底说明："
  前缀）；`_evaluate()` 消费归一提交视图（无效提交＝no_final_answer→failed，
  不保留 completed）；`_assemble_evidence(known_row_counts=…)`；`_finalize()`
  一次归一 + `invoke_started` 门控（前置拒绝不兜底）+ `delivery_payload` +
  `answer_source` 三值（model/system_fallback/none）；`_archive_run()` 归档 v2。
- `persistence/archive.py`：`LEGACY/ARCHIVE_SCHEMA_VERSION` 常量 +
  `read_delivery_answer()`（v1 status→submitted_status 改名、跨版本统计读顶层
  status）。
- 归档 v2：原 `answer` 按原值保留（含非法类型，缺失才 `{}`），另存
  `delivery_answer`/`answer_source`/`archive_schema_version`；条件 b 只用归一
  eligible 视图，条件 a 语义不变。

**B：主模型单层有限重试（doc 6.2 §7.5–§7.7）**
- `config.py` `ModelRetryConfig`（代码默认 enabled=False；标准 YAML 显式开启）
  + `db_deepagent.yaml model.retry`。
- `llm_factory.py`：`max_retries` 白名单 override（非负整数、OpenAI 兼容顶层
  透传、原生 dashscope 显式拒绝）。
- `middleware/model_retry.py`：`is_transient_model_error()` 白名单
  （APIConnectionError 含超时/RateLimitError/408/409/429/5xx）+
  `BudgetAwareModelRetryMiddleware`：尝试额度绑定**逻辑模型步骤**（按
  `budget.counters` 的 model_call 序号共享，摘要恢复重入消费同一额度，
  `ModelStepAttemptsExhausted` 耗尽保留 cause）；停止/时限预检优先且不耗额度；
  零项目层退避；仅同步主 Agent 调用。
- `services/run_budget.py`：`_RunEntry.extra` + `run_shared_state()`——步骤状态
  随 `close_run()` 释放；成功尝试即清 `last_error`；中间件本地字典仅为未注册
  运行回退。
- `agent.py`：`build_model()` 启用时强制客户端 `max_retries=0` 并双端核验
  （`verify_single_layer_retry_clients`，任一 client 为 None＝无法确认即拒）；
  装配顺序重试在请求审计外层，仅 enabled 时。

## 审核闭环

- **codex 三轮**（`codex_doc62_impl_review_r{1,2,3}.md`）：R1 四项（摘要恢复
  重入重置额度→步骤共享额度修复、Web 联动验收、原始 payload 矩阵、M1 文案）、
  R2 两项（步骤状态生命周期→账本条目随 close_run 释放、类型断言）全部修复；
  R3 生产代码全部通过，仅余一项 P3 测试缺陷（连续运行测试误用 run_id 上下文），
  **经用户裁定补正**（`_lc_req()` + `used==3` + 不走本地回退断言）。历史报告
  保留原文，不回写为仿佛当时已 PASS。
- **Kimi 两轮**（`kimi_doc62_impl_review_r{1,2}.md`）：R1 两项（重试不重复执行
  SQL 组合测试、观察器/双口径计数断言）已补；**R2 PASS**，无新问题。
- 两位审核者最终对生产代码一致结论：无 correctness 问题。

## 正式测试（当前代码实际复跑，2026-11-12 收尾时）

命令（测试进程内临时隔离 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` 及小写项——
继承 SOCKS 代理会因缺 socksio 使客户端构造失败；未改用户环境文件、未装依赖）：

```bash
.venv/bin/python -m pytest src/tests/db_deepagent/unit \
  src/tests/unit/services/test_llm_factory.py \
  src/tests/unit/api/v2 -q
```

结果：**1076 passed, 4 skipped**（7.4s）。改动文件 black/ruff 通过
（`runner.py`/`agent.py` 存量风格债未动）。

## 真实正常请求验证（用户执行，协调者只读核验）

- 后端 2026-11-12 23:38:52 启动（`logs/api.log`）；本请求 23:39:42–23:40:04
  正常完成（`logs/nl2sql.log`）。
- 本地运行记录
  `var/db_deepagent/sessions/s_8429dd04d44c45a0bb8b7598b97ec67a/runs/req_81c107c9642243aeb31dede7f53e30e1.json`：
  status=completed、errors=[]、warnings=[]、archived=true、elapsed_ms=21697；
  model_calls=8、total_model_calls=8、summary_model_calls=0、tool_calls=9；
  两条 SQL 成功（1 行/5 行）；**正式模型提交，未触发系统兜底或项目重试**。

**边界**：以上仅证明正常链路端到端可用（含 B 阶段装配下的真实模型调用），
不等于真实异常场景已验收。

## 剩余验证边界（如实保留）

1. **真实故障注入未执行**：真实模型持续失败后的兜底、真实上下文超限后的
   摘要恢复共享额度、预算停止/持久化故障的真实链路表现，证据来自离线正式
   回归（FakeAgent/MockTransport/真实装配组合测试），未对生产服务注入故障。
2. **仓库外归档消费者未核查**：v2 新增字段的外部读取方登记与兼容验证仍是
   实施验收项（本地检索不能证明不存在外部读者）。
3. **异步入口不承诺**：`BudgetAwareModelRetryMiddleware` 仅覆盖同步主 Agent
   模型调用（生产 Runner 为同步 invoke）；awrap_model_call 未实现。
4. **SDK 版本依赖**：`root_client/root_async_client` 校验基于
   langchain-openai==1.7.4 的公开属性（当前急切构造）；升级 SDK 须复验。
5. 历史审核探针（`codex_doc62_impl_review_r{1,2,3}_probe.py`）中的预期失败
   输出是**当时的故障复现记录**，不是生产回归失败，不应改造为假 PASS 或
   纳入常规 unittest 全量运行。

## 同步的文档

- `docs/6.1_…清单与实施建议.md`：01/05 标明已实现（含验证方式与边界），
  其余 02–13 未动。
- `docs/6.2_…兜底问题与补齐建议.md`：§1 增实施状态与收尾链接；实施前分析
  标为历史基线；§7 片段定位为设计期示例并与实现对齐（共享额度/生命周期/
  成功清 last_error/归档 v2/M1/M2）。
- `docs/v4/5_…详细设计.md` §13.3：归档 v2 由"待实施变更"改为已实施；
  读取兼容边界与外部消费者限制保留。
