# doc 19 / doc 17 实施审核 R2

总体结论：**PASS**。R1 的三项 P3 建议均已按最小方案落实，C1-02 的两部分均已补齐；本轮复审范围内未发现新增问题，无需追加业务修改。

## 审核范围

- 基线仍为 `feature/multi-source-knowledge-isolation`，HEAD `6a98f4d`，审核当前未提交工作区。
- 以 R1 报告、doc 19 §3.3 / §10.3 第 12a 项、doc 17 §7.1 为依据，重点核对本轮 `counts` 修正、非空口径回归、历史回读回归及 Black 格式修正，并检查相关 Runner、Web 收尾和响应投影链路。
- doc 16 的时间解析等既有未提交改动不重新开展设计审核；执行包含它们的离线回归不代表扩大本轮审查范围。
- 本轮仅新增本报告，未修改业务代码、测试或配置，未提交。

## R1 问题关闭情况

| 编号 | 复审结论 | 位置与证据 |
| --- | --- | --- |
| C1-01 / P3 | **已关闭** | `src/modules/db_deepagent/services/task_completion.py:395` 在合法直接回答返回前明确设 `decision.counts = {}`；`src/tests/db_deepagent/unit/test_doc19_direct_answer.py:110` 增加空字典断言。该赋值位于 completed / needs_clarification 共用的接受分支，不改变拒绝分支或已有任务的计数。 |
| C1-02a / P3 | **已关闭** | `src/tests/db_deepagent/unit/test_doc19_direct_answer.py:313` 使用非空 `metric_definitions`；`:329`、`:333`、`:334` 分别核对 result、QA 原提交 `answer`、QA `delivery_answer` 与同一预期列表一致。原有正文精简、findings 保留及 DEBUG 完整诊断断言继续执行，未用放松断言替代验证。 |
| C1-02b / P3 | **已关闭** | `src/tests/unit/api/v2/test_doc19_history.py:50` 参数化 completed / needs_clarification，`:85` 构造真实 `_make_finalize_hook`，`:88` 使用真实 `DbAgentRunner` 与 `QaArchive`，`:101` 调用真实 `execute_web_query`，`:111` 经 TestClient GET `/api/v2/sessions/{sid}/turns` 回读，断言 `history_saved=True`、一条历史记录及预期正文和 `open_questions`。复用既有内存 Store、readiness、锁及 FakeAgent，没有替换历史写入函数或 GET 路由返回。 |
| C1-03 / P3 | **已关闭** | `src/tests/db_deepagent/unit/test_final_answer.py:164` 已按 Black 将原断言收为一行，断言语义不变；本轮对含该文件的 11 个 Python 文件执行 `black --check` 均通过。 |

补充核对：`counts` 修正仍受“任务集合为空、提交非 None、状态允许、findings / unresolved_task_refs 为空”的原直接分支条件约束，未放宽 None、仅作废、failed / partial / no_data 或已有任务的门控。历史用例中的正常 Web 收尾直接使用 Runner 结果的正文与问题列表写历史（`src/api/v2/runs.py:454`、`:473`）；实时响应投影同样透传这些字段（`src/api/v2/dto.py:323`、`:331`），与新增回读断言相符。

## 新增问题清单

无。本轮未发现需要新增编号的 P1、P2 或 P3 问题。

## 本轮独立验证证据

以下命令均在 `nl2sql/` 执行，使用项目 `.venv`，设置 `PYTHONDONTWRITEBYTECODE=1`。未执行 `source .env`，未启用 `--run-db` / `--run-llm`，未调用真实模型或真实数据库；部分 fixture 构造配置时会沿用项目配置加载器自动读取 `.env` 的既有行为。

1. db_deepagent 与 API 全量离线回归：

   ```sh
   env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent src/tests/unit/api -q -o addopts= -p no:cacheprovider --tb=short
   ```

   **1351 passed，36 skipped，1 warning，退出码 0（13.29 秒）**，与实施方提供的回归数字一致。warning 为既有 `langchain-community` 弃用提示；跳过项保留原 live 门控。仅对该测试进程移除代理环境变量，沿用 R1 已确认的离线运行方式，没有修改依赖或配置。

2. Black 检查：

   ```sh
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/black --check src/modules/db_deepagent/services/task_completion.py src/modules/db_deepagent/tools/final_answer.py src/modules/db_deepagent/middleware/final_answer_gate.py src/modules/db_deepagent/runner.py src/modules/db_deepagent/schemas.py src/tests/db_deepagent/unit/test_doc19_direct_answer.py src/tests/db_deepagent/unit/test_task_completion.py src/tests/db_deepagent/unit/test_final_answer.py src/tests/db_deepagent/unit/test_runner_persistence.py src/tests/db_deepagent/unit/test_doc63_r3_regressions.py src/tests/unit/api/v2/test_doc19_history.py
   ```

   **11 files would be left unchanged，退出码 0**。

3. Ruff 检查两个 doc 19 专项测试文件：

   ```sh
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/ruff check --no-cache src/tests/db_deepagent/unit/test_doc19_direct_answer.py src/tests/unit/api/v2/test_doc19_history.py
   ```

   **All checks passed，退出码 0**。此结论仅适用于上述两个文件，不扩大为全仓 Ruff 通过声明。

4. `git diff --check`：**通过，退出码 0**。

## 未覆盖的边界

- 未进行真实模型语义验收，不能以模拟提交证明模型一定正确区分纯解释、数据查询、混合问题或每次遵守口径字段分工。
- 历史链路验证使用真实 Runner、Web 收尾钩子和 HTTP 历史路由，但后端为 fake 设施；未验证真实 PostgreSQL 持久化、并发锁、真实沿用证据或带时刻 SQL guard。
- 未执行真实 WebUI 视觉、表格、查看 SQL 或导出验收；新测试从 `execute_web_query` 进入执行链路，仅历史 GET 经 HTTP，不将其表述为查询 HTTP 入口或浏览器端到端测试。
- R1 已列明且方案明确接受的边界继续保留，不据此追加机制或扩大本轮修复范围。
