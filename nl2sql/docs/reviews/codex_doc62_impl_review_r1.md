# doc 6.2 实施审核：第 1 轮 / 最多 3 轮

结论：**需修改后复审，共 4 项**。其中 1 项为已复现的次数约束问题，2 项为验收覆盖缺口，1 项为低优先级文案差异。未发现新的 A 阶段归一、原值保留及归档资格计算错误；不将测试缺口描述为已经发生的生产故障。

审核范围：分支 `feature/multi-source-knowledge-isolation`，HEAD `78d24f9`，本轮请求列出的未提交业务文件、测试以及新增 model_retry.py。只审不改业务代码。

审核基准是工作区当前 doc 6.2，SHA256：`4a75d546626eb02103cf38c09cc1675f2034626cacffa78795446eb0eb43e82c`。请求中列出的 `5473d4ba` 与当前文件不同；本轮采用用户最近定稿的当前内容，包括 M1/M2，不把旧哈希当作本版验收依据。

## R1 · P2 / 中：摘要恢复重入会重置同一模型步骤的尝试额度

**代码证据**：

- [model_retry.py](../../src/modules/db_deepagent/middleware/model_retry.py:86)：每次进入 wrap_model_call() 都创建新的 ModelRetryMiddleware(max_retries=2)，没有跨同一步重入共享的尝试计数。
- [agent.py](../../src/modules/db_deepagent/agent.py:564)：保留 SummarizationMiddleware；[装配处](../../src/modules/db_deepagent/agent.py:631) 将其与新重试中间件一同交给 create_deep_agent。
- 固定 SDK 的 `deepagents/graph.py:216` `_apply_custom_middleware()` 将同名摘要实例替换到原有槽位，新重试实例插在 core 后；因此真实调用链中摘要在重试外层。
- 固定 SDK 的 `deepagents/middleware/summarization.py:1687` 捕获上下文超限，压缩后再次调用下游 handler；该调用重新进入项目重试包装器，但不是新的 before_model 步骤。
- 现有 [HTTP 次数测试](../../src/tests/db_deepagent/unit/test_model_retry.py:180) 只直接调用重试实例；装配测试只覆盖普通暂时错误恢复，未组合上下文恢复路径。

**已复现**：真实 build_db_agent 栈、一次模型步骤，主模型依次返回：

```text
429 → 429 → 400(context_length_exceeded)
→ 外层摘要压缩恢复 → 429 → 429 → 成功提交
```

主模型 OpenAI SDK 设置 max_retries=0，MockTransport 实测 **6 次主模型 HTTP 请求**；运行计数仍为 `model_call=1, tool_call=1, db_execute=0`。独立假摘要模型调用不计入这 6 次。它不是 SDK 内部重试叠加，也不是重新执行 Agent/SQL。

这超出了 doc 6.2 §7.7 的“每步骤最多三次主模型 HTTP 尝试”及其全轮理论上限。当前 doc §7.6 的示例也没有考虑摘要恢复重入，因此这里同时是**方案遗漏**，不是实现者误抄代码片段。每次尝试的停止/时限预检仍然存在，本轮没有发现它绕过该预检或重复 SQL。

**建议**：保留既有摘要恢复，但将主模型尝试额度绑定到逻辑模型步骤，而不是一次 wrap_model_call() 进入。例如按 run_id + 已预占的模型步骤序号共享计数，摘要恢复重入继续消费同一额度；新步骤才重置，运行关闭时清理。不要通过重复 reserve("model_call") 把同一步算成新步骤。额度耗尽应按既定失败语义收敛并保留错误，不伪造有效提交。同步明确文档中“步骤”包括摘要恢复后的主模型再请求。

必须补真实装配 + SDK MockTransport 的上述组合测试，验证整步最多三次；另验证“首次上下文超限后恢复成功”仍可在总额度内工作、新步骤额度独立、到期/其他停止仍优先。若决定让上下文恢复拥有独立额度，则需先修订文档的上限契约并获用户认可，不能继续宣称当前总上限为三次。

## R2 · P2 / 中：尚缺文档明确要求的 Web 实时与历史兜底联动验收

**证据**：

- 本轮未修改 `src/tests/unit/api/v2/`；检索该目录没有系统兜底、system_fallback 或 delivery_answer 的新场景。
- [test_runs_flows.py](../../src/tests/unit/api/v2/test_runs_flows.py:630) 的现有 fallback 测试输入 answer=""，只验证 Web 层原有占位文案，不覆盖 Runner 新生成的非空兜底。
- [实时投影](../../src/api/v2/dto.py:307) 与 [历史写入](../../src/api/v2/runs.py:450) 是不同路径；API 原有用例全绿，不能证明新增兜底同时走通二者。doc 6.2 §7.8 明确要求这一组检查。

**建议**：复用现有 Web fixture，至少用一个由 Runner 实际生成的兜底结果经过正常收尾与 WebQueryResult 投影，断言实时 answer、归档 delivery_answer.answer、写入历史以及历史读取的文本相同，来源前缀保留，原失败状态/证据保留且 history_saved 符合回读。再用同一兜底结果覆盖来源复核失败，验证仍采用现有安全投影，不把兜底原文/证据绕过复核返回。无需为此新增业务接口或改变 DTO。

这是验收缺口，本轮没有复现兜底在 Web 中丢失的功能错误。

## R3 · P3 / 低：非法提交矩阵尚未覆盖完整原始 payload 的类型与假值保留边界

**代码证据**：

- [ILLEGAL_PAYLOADS](../../src/tests/db_deepagent/unit/test_runner_persistence.py:943) 的所有条目仍为字典；answer_none 等是字典内部的正文值，不是 FinalAnswerRecord.answer 原始 payload 本身为 None/空串/列表。
- [_state()](../../src/tests/db_deepagent/unit/test_runner_persistence.py:77) 使用 `if answer` 构造记录，直接传 None、空串、空列表会把记录删除，无法检验“键存在但原值非法”的保留契约。
- [test_illegal_payload_full_tail](../../src/tests/db_deepagent/unit/test_runner_persistence.py:1009) 还有一个 `... if False else True` 恒真断言，不提供任何验证；真正有效的状态和来源断言在相邻位置。

**建议**：直接构造 `db_final_answers[run_id]["answer"]` 已存在的状态，补原始 payload 为字符串、列表、None、空串、空列表、False、0，以及 findings 为字典的场景，分别走真实执行/纯沿用；验证 failed + 兜底、不新增 P3、正确分流、原值类型和值精确保留。缺失 answer 键另作对照，才应写 `{}`。可复用审核探针中的输入集合；删除恒真断言。无需因此改变当前正确的原值提取实现。

审核者临时补跑的 8 类输入 × 2 条分流共 **16 个真实 Runner 场景已通过**，说明当前实现能处理这些数据；建议将这些保障移入正式回归，防止未来重新引入 `or {}` 或无条件遍历原值。

## R4 · P3 / 低：M1 零行文案仍为旧版，测试也固定了旧文案

**代码证据**：

- [runner.py](../../src/modules/db_deepagent/runner.py:817) 对所有已知行数统一输出“已记录 N 行结果。”，没有查询零行/分析零行分支。
- [test_template_known_counts_and_boundaries](../../src/tests/db_deepagent/unit/test_runner_persistence.py:1325) 仍断言“已记录 0 行结果”。
- 当前 doc 6.2 §7.2.3 已定稿为查询零行“该查询没有返回记录。”、分析零行“该分析没有输出记录。”；M2 则明确保留“系统兜底说明：”。M2 实现符合要求。

**建议**：补 count==0 的结果类型分支并调整测试，至少覆盖查询零行、分析零行、未知行数不进入零行分支；保留前缀及沿用/截断说明。纯文案差异，不影响当前兜底核心逻辑。

## 已核实的符合项

- 结构与非空白正文共用模型规则，外层 run_id/request_id 严格校验；收尾只生成一次归一视图，评估、交付、归档复用。
- 行数映射只在 _finalize() 计算一次；未知和非法行数在外部兼容投影为零，但模板读取可信映射而非投影默认零。
- invoke_started 正确透传；前置拒绝不生成兜底；model/system_fallback/none 三值来源逻辑符合方案。
- 归档 v2 保存原 answer，另外保存 delivery_answer/source/version；条件 b 只读取归一视图，条件 a 保持真实执行语义；v1 状态改名与 v2 浅投影符合契约。
- 单层重试的主模型构造覆盖、原生拒绝、双端客户端核验、白名单及零退避符合方案；R1 是外层恢复带来的额度重置，不能用“SDK 重试已经为零”排除。
- P3 部分写入后状态变化仍保留文档已声明的限制，没有因本次审查额外要求事务或历史迁移。

## 实际运行的验证

1. 先运行改动相关用例及 API v2：454 passed、6 failed。6 个失败全部是当前进程继承 SOCKS 代理但缺少 socksio 可选依赖，发生在客户端构造阶段。
2. 仅在测试子进程隔离 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 及小写对应项后，按实施者相同范围重跑：**1044 passed、4 skipped（6.83 秒）**。未安装依赖、未改环境文件。

```python
import os
import pytest
for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(name, None)
raise SystemExit(pytest.main([
    "src/tests/db_deepagent/unit",
    "src/tests/unit/services/test_llm_factory.py",
    "src/tests/unit/api/v2", "-q", "--disable-warnings", "--tb=short",
]))
```

3. [审核探针](codex_doc62_impl_review_r1_probe.py)：在生产装配中先复现同一步 6 次模型尝试，再用真实 OpenAI SDK + MockTransport 确认 6 次主 HTTP、客户端内部重试零，最后补跑 16 个完整原始 payload 场景。探针只在本进程替换模型/服务，不改业务文件；它记录修复前行为，不是修复后应继续返回 6 的验收标准。

```bash
# 从 nl2sql/ 执行
PYTHONPATH=. .venv/bin/python docs/reviews/codex_doc62_impl_review_r1_probe.py
```

关键输出：

```json
{"primary_attempts": 6, "budget": {"model_call": 1, "tool_call": 1, "db_execute": 0}}
{"sdk_internal_max_retries": 0, "actual_primary_http_requests": 6, "budget": {"model_call": 1, "tool_call": 1, "db_execute": 0}}
{"raw_payload_probe_cases": 16, "passed": true}
```

这些测试没有访问真实模型或数据库。本轮未重做实施者的格式化操作，也未修改业务或正式测试文件。已知仓库外归档消费者的登记/兼容核查仍需实施者提供证据；本地测试不能证明外部消费者已经兼容。

## 回传与后续

本轮修订请继续由原 Claude pane 处理，完成后按第 2/3 轮重新请求审核，不创建重复实现任务。建议优先修正 R1，再补 R2/R3 并处理 R4。达到三轮上限仍有未解决项时交由用户介入，不自行延长轮次。
