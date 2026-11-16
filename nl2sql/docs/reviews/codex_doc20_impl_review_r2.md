# doc 20 实施审核 R2

- 审核日期：2026-11-16。
- 审核基线：`feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` 加当前未提交工作区。
- 范围：复审 R1 两项 P3 的持久回归补齐及相关交叉影响；不重复审核已收口的 doc 16、doc 19/17。
- 总体结论：**PASS**。C1-01、C1-02 均已关闭，本轮范围内未发现新的 P1/P2/P3 问题，无需继续修改。

## R1 问题闭环

| 编号 | 结论 | 复审证据 |
| --- | --- | --- |
| C1-01 / P3 | **已关闭** | `src/tests/db_deepagent/unit/test_model_observer.py:502` 新增真实装配回归：复用 `assembly_services`，仅替换主模型构造，调用真实 `build_db_agent()` 并使用 `InMemorySaver`；经真实图与中间件调用 `ScriptedChatModel`，将 `ModelCallObserver` 作为真实模型回调接入。`:551` 固定 `ctx.now=2026-11-16T18:30:00+00:00`，`:570–576` 断言 `MODEL_INPUT_BEGIN`、时间背景标题、北京次日日期 `2026-11-17`、假模型调用游标为 1，以及 `total_model_calls == 1`。没有手工向输入系统消息塞入日期，也没有直接调用观察器伪造装配链。 |
| C1-02 / P3 | **已关闭** | `src/tests/db_deepagent/unit/test_budget_middleware.py:286` 新增 `test_same_original_processed_twice_stable`，`:292–293` 两次分别向 `_with_reference_time()` 传入同一 `original`；`:296–299` 检查各自只有一个时间块、时间块文本一致、结果请求彼此独立且首次结果不是原请求、原系统消息内容仍为“基础提示”。`:278` 原有重包返回值用例继续保留，`:301` 刷新已注入请求时间的用例也保留，两种重复处理路径均有持久覆盖。 |

补充核对：`model_observer.py:209–240` 的 `_log_model_input()` 直接格式化真实回调收到的消息，将正文与 `MODEL_INPUT_BEGIN/END` 合并为一条 DEBUG 记录；新测试捕获的目标日期由装配链注入并进入该输入日志。`assembly_services` 对摘要模型、时间解析器和后端使用现有假件，主模型由本用例替换，因此该用例不调用真实模型或数据库。脚本固定提交答案仅用于结束图执行，不作为自然语言时间计算正确性的验收证据。

测试数量更正已核实：AST 统计当前 `TestReferenceTimeInjection` 为 **10** 项，其中新增同一原请求独立处理用例 1 项；R1 实际为 **9** 项。R1 任务书原称 10 项属于实施方数量误报，不能作为此前已有该覆盖的证据；保留原任务书与 R1 报告作为历史记录，本报告记载更正。

## 本轮独立验证证据

以下命令工作目录均为 `nl2sql/`。未使用 `--run-db` / `--run-llm`，未手工 source `.env`；配置加载器仍可能按项目既有行为读取 `.env`。仅在测试子进程中移除代理变量，未改环境配置文件。

### 1. db_deepagent 与 API 回归

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent src/tests/unit/api -q -o addopts= -p no:cacheprovider --tb=short
```

实测结果：**1364 passed / 36 skipped / 0 failed**，退出码 **0**，耗时 **14.06 秒**，与实施方本轮申报一致。另有 1 项既有 `langchain-community` 弃用警告，来自 `test_harness_profile.py::test_request_timeout_applied`。这里的全量指上述两个目录，不表示整个仓库所有测试。

### 2. 静态检查

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m ruff check --no-cache src/modules/db_deepagent/middleware/request_audit.py src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_model_observer.py src/tests/db_deepagent/unit/test_doc19_direct_answer.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m black --check src/modules/db_deepagent/middleware/request_audit.py src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_model_observer.py src/tests/db_deepagent/unit/test_doc19_direct_answer.py
git diff --check -- src/modules/db_deepagent/middleware/request_audit.py src/modules/db_deepagent/config/prompts.yaml src/tests/db_deepagent/unit/test_budget_middleware.py src/tests/db_deepagent/unit/test_model_observer.py
```

结果分别为 **All checks passed!**、**4 files would be left unchanged**、无差异格式错误，退出码均为 **0**。AST 独立计数 `TestReferenceTimeInjection` 为 **10** 项，包含新增用例及保留的重包用例。

## 验证边界与修改范围

- 没有运行真实模型、真实数据库或其他外部服务验收；live 用例按默认门控跳过。假模型证明注入数据流、真实回调日志及单次模型调用，不证明真实模型对“今天日期无需调工具”或“相对日期仍应调时间工具”的服从性。
- 本轮未修改业务代码、测试、配置来重演 red/green 或故障注入；结论来自当前源码核对和上述实际回归结果。R1 已验证的重试、跨 run 等行为不在本轮重复扩展测试范围。
- 本轮只新增本审核报告；业务代码、测试和配置的审核前后文件哈希核对无变化，未提交。
