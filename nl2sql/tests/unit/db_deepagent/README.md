# 独立摘要器离线对照实验

运行目录为 `nl2sql/`，使用现有虚拟环境，无需 API Key 或数据库连接：

```bash
.venv/bin/python -m pytest tests/unit/db_deepagent/test_independent_summarizer.py -q
```

该目录已加入 `pytest.ini` 的 `testpaths`，普通 pytest 运行也会收集这些用例。
未将整个历史 `tests/` 目录加入默认收集，避免混入需要外部服务的手工诊断脚本。

## 验证范围

测试使用当前安装的 DeepAgents SDK、真实摘要中间件、Agent 图和内存检查点。
仅模型响应使用本地替身；文件历史存入 pytest 的临时目录。

| 场景 | 核对内容 |
| --- | --- |
| 短会话，现有/候选写法 | 不触发摘要，不增加摘要调用 |
| 相同模型配置下的现有/候选写法 | 摘要输入相同；主模型收到摘要和相同近期消息；用途标记与调用统计完整 |
| 主模型和摘要模型的窗口不同 | 分数触发阈值以主模型 profile 为依据 |
| 长历史，取消裁剪/4000 字符预算对照 | `trim_tokens_to_summarize=None` 将早期条件送入摘要器；裁剪对照能暴露条件被排除 |
| 保留边界落入工具响应 | 主模型收到完整的工具调用/响应对 |
| 检查点冷读 | 新图实例能读取摘要事件；现有 `read_summary_for_handoff()` 能消费；归档原文可读 |
| 自定义摘要函数失败 | 错误向外传播，主模型没有在缺少摘要的情况下继续运行 |
| 异步调用 | 独立摘要器被调用，主模型/摘要回调仍能分别统计 |

“现有写法”指将带摘要用途标记的主模型副本传给中间件的 `model`；
“候选写法”指 `model=主模型, summarizer=带摘要用途标记的副本`。
测试未修改生产 `build_db_agent()`。测试图不包含数据库业务工具、业务预算和最终答案门控，
不能代替迁移后的完整业务装配与线程轮换验收。

## 证据边界

- 使用可重复的**字符计数器**驱动 SDK 阈值，不模拟供应商 tokenizer；
  4000 在这些用例中为字符预算，不能推断真实生产 96000/24000 tokens 的容量。
- 模型始终返回固定摘要。断言验证消息进入/离开摘要链路的情况，
  **不证明真实模型保留了业务语义**。
- 用量响应为固定模拟数字，仅验证观测器接线；不提供真实成本或延迟结论。
- 失败用例覆盖自定义函数异常传播，不代表已验证模型重试、上下文溢出恢复或网络异常。
- 检查点使用内存保存器，不代表 PostgreSQL 持久化已验收。

在决定更换摘要模型前，应使用同一批业务对话，对现有写法、同模型候选写法和替代模型进行真实对照：

1. 保留时间区间、时区、统计口径、用户后续修改、有效证据引用和未解决的 SQL 错误；
   检查是否错误地把失败或未执行操作写成已经完成。
2. 将摘要交回主 Agent，检查后续生成的 SQL 是否符合最新条件。
3. 记录实际输入/输出 tokens、摘要耗时、失败及重试次数，再按模型价格计算成本。
4. 使用接近实际触发阈值的历史，验证替代摘要模型能否接收完整输入，
   并核对线程交接、业务预算及数据库检查点。

## 本次运行记录（2026-11-10）

- 新增用例：10 个全部通过；Black 与 Ruff 检查通过。
- 新增用例与现有 `src/tests/db_deepagent/unit` 一起运行：620 passed、4 skipped。
  此运行临时排除了进程中的 SOCKS 代理环境变量；原环境下一个现有模型装配测试
  因未安装 SOCKS 支持依赖而失败。没有修改依赖或用户的代理配置。

```bash
env -u all_proxy -u ALL_PROXY .venv/bin/python -m pytest \
  tests/unit/db_deepagent/test_independent_summarizer.py \
  src/tests/db_deepagent/unit -q
```

完整默认测试集未能完成收集：旧文件 `src/tests/unit/tools/test_join_planner.py`
导入不存在的 `build_multi_base_join_plans`。单独收集该旧文件也复现相同错误；
该测试及对应业务源文件本次均未修改。因此这里没有全项目测试通过的结论。

## 生产接入实施记录（2026-11-10，doc 5）

生产 `build_db_agent()` 已显式传入 `model=主模型, summarizer=主模型副本`（保留副本
`dbda_call_kind=summarization` metadata、阈值 96000/24000、`trim_tokens_to_summarize=None`）。
本目录 10 个离线对照实验按计划复用、未修改。

- 实施顺序（TDD）：先加强 `src/tests/db_deepagent/unit/test_summary_context.py` 的装配
  实参测试并在旧生产写法下确认 RED（生产构造记录缺 `summarizer`，退出码 1），应用
  `agent.py` 接线后 GREEN；再新增完整生产图的 observer 分类测试
  （`test_production_summary_calls_keep_observer_classification`）并调整
  `_invoke_with_context()` 支持注入 observer。
- 聚焦测试（4 文件）：**39 passed**（此前基线 38 + 新增 1）。
- 与 `src/tests/db_deepagent/unit` 合并回归：**621 passed、4 skipped**（基线 620+4，零新增失败）。
- 真实模型脚本 `test_model_summary_window.py` 已同步新接线；离线核对 1 collected、
  默认执行 1 skipped（`requires_llm`），本轮未运行 `--run-llm`。
- Black：3 个修改的 Python 文件全部通过；Ruff：`test_summary_context.py` 零问题，
  `agent.py`（9 项）与 `test_model_summary_window.py`（1 项）的 I001/UP037 为 HEAD
  既有基线，本次未新增、未顺带修复。
- `git diff --check` 通过。完整默认测试集仍受 `test_join_planner.py` 既有导入错误
  阻断（与此前记录一致，未修改）。

无真实质量/成本结论；96000 tokens 附近窗口与 PostgreSQL 持久化验收不在本次范围。

## 后续真实模型低阈值冒烟（2026-11-10）

生产接入审核完成后，用户授权协调者实际运行
`src/tests/db_deepagent/integration/test_model_summary_window.py --run-llm`。
使用当前 `qwen3_8_max` 配置，结果为 **1 passed**，总耗时 15.37 秒；
观测到 3 次模型调用尝试，其中 1 次摘要调用，用量完整性断言通过。
全部调用输入/输出 tokens 合计为 7827/338，内存检查点中检测到摘要事件。

这是 1200/300 tokens 测试阈值下的真实接口冒烟；生产 96000/24000 配置保持原值。
未验证完整数据库业务图、SQL 正确性、检查点冷启动或 PostgreSQL，
也没有质量/成本/延迟改善的对照结论。原实施记录中的“未运行 --run-llm”
指此前离线实施阶段；本节记录其后的实际调用。

详细命令、结果与证据路径见 [doc 5 的真实模型冒烟记录](../../../docs/5_独立摘要器接入修改方案.md)。
