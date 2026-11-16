# doc 19 / doc 17 实施审核 R1

总体结论：**PASS**。本次范围内未发现 P1/P2 级功能缺陷；直接提交、归档和正文精简符合方案的主要边界，存在以下三项 P3 建议，其中一处格式检查实际未通过。

## 审核范围

- 基线：`feature/multi-source-knowledge-isolation`，HEAD `6a98f4d`，审核当前未提交工作区。
- 已完整阅读审核任务书、doc 19、doc 17 和背景 doc 18，逐项核对任务书列出的业务文件与五个测试文件。
- doc 16 时间解析实现、观察器及相关配置/测试不作为本轮审查对象；`query-planning/SKILL.md` 中时间工具参数更新亦按 doc 16 增量排除。全量离线回归执行到相关测试，不代表重新审查 doc 16。
- 本轮仅新增本报告，未修改业务代码、测试或配置，未提交。

## 编号问题清单

### C1-01 / P3：直答分支的 counts 未按 doc 19 §3.3 返回空字典

- **位置**：`src/modules/db_deepagent/services/task_completion.py:393`；相关初始化在同文件 `:356`。
- **证据**：函数在进入直答分支前已计算各状态计数，接受直答时直接返回。对零任务、合法 completed 提交的独立调用，观察到 `decision.counts == {"completed": 0, "failed": 0, "needs_clarification": 0, "voided": 0, "dependency_blocked": 0}`，而 doc 19 §3.3 明确约定 `counts={}`。新增直答成功测试检查了 findings、coverage、outcomes 和 errors，未检查 counts。
- **影响**：返回对象与方案约定有轻微偏差；当前未发现 counts 的业务消费点，不影响门控放行、正文或归档条件，因此不升级为功能阻断。
- **最小建议**：在合法直答返回前设 `decision.counts = {}`，并在既有直答成功测试增加一条空字典断言，无需调整已登记任务路径。

### C1-02 / P3：已承诺的历史回读及非空口径字段验证未形成对应回归用例

- **位置**：`src/tests/db_deepagent/unit/test_doc19_direct_answer.py:276`、`:305`；`src/tests/db_deepagent/unit/test_runner_persistence.py:1120`。
- **证据**：新增 Runner 直答测试止于 FakeStore QA payload，没有走 Web 收尾/历史接口；数据正文精简测试使用 `_state()`，其 `metric_definitions` 为空。doc 19 §10.3 第 12a 项明确要求 API 历史回读，doc 17 §7.1 第 3/4 项要求以非空口径验证响应、原提交和交付归档字段。既有 API 测试通过，只能证明其既有覆盖；本次没有新增对应联动断言。
- **影响**：不构成当前功能失败。本轮独立补测已验证零任务 completed/needs_clarification 的真实 Runner→收尾钩子→历史 HTTP 路由，以及非空 metric_definitions 的保存均正确；但这些临时探针未成为仓库的持续回归保护，不能称新增 24 项已覆盖 doc 19 §10 全部条目。
- **最小建议**：将现有正文精简测试的口径设为非空并断言 `result`、QA `answer`、QA `delivery_answer` 三处一致；复用现有 Web fake 设施增加一项参数化历史回读测试，覆盖零任务成功与澄清。无需新增接口或测试框架。

### C1-03 / P3：本次修改的一条断言未通过 Black

- **位置**：`src/tests/db_deepagent/unit/test_final_answer.py:164`。
- **证据**：对本轮十个 Python 文件运行 `black --check`，结果为 `1 file would be reformatted, 9 files would be left unchanged`，退出码 1。`black --check --diff` 显示唯一变化是将本次新增的三行 `direct_answer_task_refs_not_allowed` 断言收为一行；与存量中文 noqa / BLE001 无关。
- **影响**：不影响测试语义或运行行为，但 doc 19 §11.1 的 Black 通过条件尚未满足。
- **最小建议**：仅格式化该断言，或对该单文件运行项目 Black；无需改动断言内容。

## 关键核对结论

| 审核项 | 结论与依据 |
| --- | --- |
| 零任务直接分支 | `task_completion.py:379` 同时要求任务集合为空及提交非 None；仅接受 completed / needs_clarification，findings 和 unresolved 非空时拒绝。failed / partial / no_data 继续落到任务视图拒绝；规则版本已升为 2。 |
| None、仅作废、已有任务 | None 不读取提交字段、不被接受；仅作废时 tasks 非空，仍 task_not_registered；任何未作废任务继续既有终态、证据、覆盖、status 路径。未发现顺手放宽已登记任务校验。 |
| 工具层 | 参数 schema、非空正文、单独调用、重复提交保护保留；两个拒绝码 hint 正确分流。schema 仅改注释，clarification_question 仍为禁止额外字段。 |
| condition_c | `_task_outcome_summaries()` 对传入 tasks 的每条记录生成摘要，包含 voided；因此空摘要与门控的空 tasks 口径一致。`runner.py:1255` 还要求 decision.accepted，仅作废/无提交不会经 condition_c 写 QA。 |
| 有执行但被拒 | 原 condition_a 仍可将实际执行轮次归档 QA，这不等于答案获准交付；独立探针观察到缺 finding 覆盖时交付 system_fallback，业务状态 failed。不可把“写 QA”误判为 condition_c 放宽门控。 |
| 精简与完整模式 | 默认完整模式保留 finding 标签、completed、voided；精简模式仅跳过这些内容，failed / needs_clarification / authoritative_questions 仍输出，精确去重仍生效。 |
| Runner / DEBUG | 合法交付调用 `include_audit_details=False`；完整段落只在 `isEnabledFor(DEBUG)` 内构造并写日志。关闭 DEBUG 的独立 spy 验证只调用一次精简构造；完整诊断不拼进正文。兜底函数未改。 |
| 正文和结构化字段 | 原模型 answer 不被解析/裁剪；含旧式“口径说明”的模拟正文原样保留。非空 metric_definitions 在结果、原提交和 delivery payload 中一致，findings 保留。 |
| 提示词和 Skill | 登记范围收窄、混合问题只登记数据部分、防猜数据约束、已登记任务限定、零任务 open_questions 例外及 answer/metric_definitions 分工均存在。 |
| 四处既有断言更新 | task_completion / final_answer 的新断言对应合法直答及引用空约束；runner_persistence 去掉成功尾段要求符合 doc 17；doc63_r3 将 finding 从正文检查转为 QA 结构化检查符合 doc 17 §4.3 的有意调整。后者接受“已处理”是方案明确接受的语义保证变化，不是本次应恢复的旧行为。 |

## 本轮独立验证证据

以下命令均在 `nl2sql/` 执行，使用项目 `.venv`，设置 `PYTHONDONTWRITEBYTECODE=1`，未使用 `--run-db` / `--run-llm`，未调用真实模型或真实数据库。未执行 shell `source .env`；项目原有配置加载器在部分测试/探针构造配置时自动读取 `.env`，不据此开启 live 用例。

1. 新增专项测试：

   ```sh
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent/unit/test_doc19_direct_answer.py -q -o addopts= -p no:cacheprovider
   ```

   **24 passed，退出码 0**。

2. db_deepagent 全量离线回归：

   ```sh
   env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent -q -o addopts= -p no:cacheprovider --tb=short
   ```

   **1164 passed，36 skipped，1 warning，退出码 0**。warning 为既有 langchain-community 弃用提示。首轮保留终端代理环境时为 1154 passed / 36 skipped / 6 failed / 4 errors，失败均在 HTTP 客户端构造处报 SOCKS 代理所需 `socksio` 缺失；只移除该测试进程的代理变量后通过，没有安装依赖或修改配置，也没有放开真实调用门控。

3. API 回归：

   ```sh
   PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/unit/api -q -o addopts= -p no:cacheprovider
   ```

   **185 passed，退出码 0**。

4. 额外 Python heredoc 离线探针（复用项目 FakeAgent/FakeStore、assembly_services 与 Web fake 设施，使用临时工作区，未写入仓库测试）：

   | 输入/链路 | 实际结果 |
   | --- | --- |
   | 空任务合法提交，plan_version=7 | accepted，plan_version 保留；counts 为五项全零字典，证实 C1-01。 |
   | 零任务 needs_clarification，正文及 open_questions 有问题 | 状态/正文/问题保留，QA answer_source=model，evidence=[]，stats 为正常对象；QaArchive.read_delivery_answer 回读正文一致。 |
   | 仅 voided 任务，无执行 | 拒绝模型交付，归档 run_errors，未写 QA。 |
   | 有真实形态的 fake 执行记录，但 completed 任务没有 finding 覆盖 | status=failed，QA answer_source=system_fallback；现有 condition_a 没有使模型答案获准。 |
   | 正常数据提交，非空 metric_definitions、正文仍含旧式口径段 | 正文原样；口径在 result/QA answer/QA delivery_answer 三处一致，findings 保留。 |
   | DEBUG 关闭时 spy 段落构造函数 | 仅一次调用，参数为 include_audit_details=False；不构造完整诊断。 |
   | 真实 Runner 零任务 completed 与 needs_clarification→真实 Web finalize hook→GET /api/v2/sessions/{sid}/turns | 两种状态均 history_saved=true；历史正文、open_questions 与实时一致，evidence=[]。HTTP TestClient 使用允许的 localhost 主机。 |
   | 精简段落输入重复失败摘要 | 相同失败条目仅输出一次。 |

5. 静态检查：新测试文件 `ruff check --no-cache` **通过**；本轮十个 Python 文件 `black --check` 为 **9 个通过、1 个待格式化**，详见 C1-03；`git diff --check` **通过**。未将这部分结果扩大为全仓 Ruff/Black 通过声明。

## 未覆盖的边界

- 未进行真实模型语义验收，不能证明模型在日期常识、混合问题或模糊查询中一定选择正确登记路径，也不能证明每次均将技术口径放进 metric_definitions。
- 未实测真实 WebUI 的表格/SQL/导出与页面视觉效果；历史验证为 fake 后端上的真实 Runner、收尾钩子和 HTTP 路由。
- 未调用真实数据库，未复验带时刻 SQL guard、真实沿用证据、真实 PostgreSQL 持久化/并发锁；相关 live 用例跳过属本次边界。
- 零任务直接答案的内容真实性由模型及提示词约束、“登记后全部作废”仍不能直答，以及模型只写“已处理”不再靠成功审计尾段补全，均为方案明确接受的边界，不作为本轮缺陷要求追加机制。
