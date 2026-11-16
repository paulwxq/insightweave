# doc 20 实施审核 R1

- 审核日期：2026-11-16。
- 审核基线：`feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` 加当前未提交工作区。
- 范围：任务书列出的 5 个文件中与 doc 20 有关的改动；doc 16、doc 19/17 已收口部分不重复审核。
- 总体结论：**PASS**。未发现阻断交付的 P1/P2 问题；实现符合方案，以下两项 P3 是现有回归测试的覆盖缺口，本轮已用独立离线探针确认对应运行行为正确。

## 编号问题清单

### C1-01 · P3：时间日志用例未覆盖方案要求的实际装配链

- **位置**：`src/tests/db_deepagent/unit/test_model_observer.py:479`。
- **证据**：用例手工构造含日期的 `SystemMessage`，直接调用 `observer.on_chat_model_start()`；未经过 `_with_reference_time()`、`RequestAuditMiddleware`、`build_db_agent()` 或假模型。已有 `test_real_chain_scripted_model` 只验证普通系统消息经模型回调进入日志，也未把时间注入与装配链连起来。
- **影响**：它证明“给观察器日期文本就能记录”，不能防止未来装配路径丢失时间块的回归，与 doc 20 §8.5 的验证要求存在差距；本轮独立装配探针已通过，因此不认定当前功能失效。
- **最小修改建议**：在现有装配/观察器测试中补一项 `build_db_agent + 假模型 + ModelCallObserver` 用例，断言假模型收到 `ctx.now` 对应的时间块，且该日期出现在 `MODEL_INPUT_BEGIN/END` 内。复用现有 fixtures，不新建测试框架，不改生产代码。

### C1-02 · P3：所谓“原请求重复处理”用例实际仍在重包返回请求

- **位置**：`src/tests/db_deepagent/unit/test_budget_middleware.py:278`。
- **证据**：`test_rewrap_original_keeps_single_block` 执行 `once = _with_reference_time(original)` 后，第二次传的是 `once`，不是再次传 `original`；后续刷新时间用例也属于重包已注入请求。实际该类只有 **9** 项测试，与任务书宣称的 10 项不一致，未独立覆盖 doc 20 §4.1 明列的另一种重复处理场景。
- **影响**：当前只对“重包返回值”有明确幂等断言，不能完整锁定“同一原对象可重复处理且始终不变”的约定；本轮独立探针已确认现实现符合此约定。
- **最小修改建议**：增加一项或扩展现有用例，分别执行两次 `_with_reference_time(original)`，检查两个结果均只有一个时间块、内容一致，并核对原系统消息/普通消息不变。保留现有重包返回值用例；无需增加兼容逻辑或配置。

## 逐项核对结论

1. **时间与位置**：`request_audit.py:52` 仅使用 `ctx.now.astimezone(Asia/Shanghai)`，无第二次读取系统时钟；`:134` 在读取 ctx 后立即注入，早于序号、Token 估算、工具集合核对和 handler。`:137` 与 `:168` 使用注入后的局部请求变量。
2. **替换纪律与纯函数**：`:68–85` 保留非时间块，用原消息 `model_copy` 保留元数据，再调用公开 `request.override(system_message=...)`。只匹配 `type=text` 且去前导空白后以完整保留标题开头的独立块；多个旧块归一，句中标题不误删；没有修改 `state.messages`、工具集合、模型参数或普通消息。
3. **SDK 用法**：实际安装版本为 `deepagents==0.7.34`、`langchain==1.4.9`、`langchain-core==1.7.6`、`langgraph==1.2.20`。已核对安装包 `langchain/agents/middleware/types.py`：`ModelRequest` 在 `__all__` 导出，构造参数包含 `model/messages/system_message/tools/runtime`；`override()` 有公开文档并通过 dataclass replace 返回新实例。测试中的 `SimpleNamespace` 只占位不执行的模型/工具/runtime，未调用 SDK 私有辅助函数，不构成本次兼容性问题。
4. **提示词完整性**：`prompts.yaml:27–35` 为方案 §5 新三段；未发现旧规则开头或独有续行残留。时间节其余内容与 HEAD 对应原文一致，包括受控沿用、双端/单端区间、禁止 SQL 实时时钟及裸“当前”不自动成为范围。
5. **归一化断言**：`test_doc19_direct_answer.py:465` 的正则会拼接所有缩进行，并非 YAML 语法校验；对当前 `system_prompt: |` 中的中文物理续行，它能覆盖跨行旧片段，当前检查未发现因此假绿的真实错误。本轮另外用 `yaml.safe_load` 取得实际 `agent.system_prompt`，限定“时间”节后核对新语义及旧片段，结果通过；不建议为此增加复杂解析框架。
6. **职责边界**：RequestAudit 原有工具名核对、审计返回与预算停止分支保持原逻辑；本次没有增添模型/数据库调用，也不要求调整中间件装配顺序或时间工具 schema。

## 本轮独立验证证据

以下命令工作目录均为 `nl2sql/`，未使用 `--run-db` / `--run-llm`，未手工 source `.env`，未调用真实模型或真实数据库。

### 1. db_deepagent 套件

首次按任务书命令运行：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent -q -o addopts= -p no:cacheprovider
```

结果：`1165 passed / 36 skipped / 6 failed / 4 errors`，失败/错误均来自模型客户端构造读取 SOCKS 代理时缺少 `socksio`，尚未调用模型。按任务书仅移除该测试进程代理后重跑：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent -q -o addopts= -p no:cacheprovider
```

结果：**1175 passed / 36 skipped / 0 failed**，退出码 0，12.64 秒，另有 1 项既有 langchain-community 弃用警告。实施方所述 1362 与本次 db_deepagent 1175 加其所述 api 187 数量相符；本轮未独立复跑 api，不把 1362 当成本轮实测结果。

### 2. 独立运行时探针（内存执行，无仓库代码/测试变更）

通过 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'` 执行，复用现有 `assembly_services`、`db_agent_context`、`ScriptedChatModel`、`_DebugCapture`，以 `pytest.MonkeyPatch.context()` 临时替换模型构造，并使用临时目录与 `InMemorySaver`：

- 首次假模型调用抛 `APIConnectionError`，同一步重试返回新的 `submit_final_answer` 调用；下一 run 继续使用同一 checkpoint 线程、不同 ctx.now。
- 两个参考时间分别为 `2026-11-16T18:30:00+00:00` 与 `2026-11-17T18:30:00+00:00`；假模型与 DEBUG 日志分别收到北京日期 `2026-11-17` 与 `2026-11-18`。
- 共 2 个 run、3 次假模型尝试，每次模型输入均只有一个时间块；同 run 重试的时间块相同，两轮均成功登记最终答案。
- 估算函数收到的系统消息对象就是注入请求的系统消息；其时间块完整到达假模型。SDK 下游 SkillsMiddleware 会追加自己的内容块，因此只比较时间块，不把“估算时与最终模型的全部系统内容完全相同”作为本方案要求。
- checkpoint 的普通 `messages` 没有持久化系统时间消息。
- 另构造含文本块、字符串项、id/name/additional_kwargs/response_metadata 的系统消息，对同一原请求调用两次辅助函数；核对结果一致、原消息不变、元数据保留，且模型、runtime、messages、state、tools、model_settings、tool_choice、response_format 字段未替换。

最终探针退出码 **0**，输出：

```text
Real build_db_agent + fake model + retry + next run + DEBUG log + estimator request identity + no state clock persistence: PASS (3 attempts, 2 runs)
Repeated same original request + mixed list + metadata + request fields preserved: PASS
```

探针调试中排除了两项探针自身问题：最初复用了同一条 AIMessage/工具调用 ID，导致第二轮无法正常提交；随后过严地比较了 SDK SkillsMiddleware 追加内容前后的整条系统消息。改为每次生成新响应、比较目标时间块后通过，上述过程不构成生产缺陷。配置加载器会自动读取项目 `.env`，但探针主模型/摘要模型/时间解析器及后端均使用现有假件。

### 3. 提示词及静态检查

- `yaml.safe_load` 后提取实际时间节，独立检查注入权威基准、无需单独取今天日期、原话 source_text、工具复核、本轮参考时钟、两轮日期差异、单边区间及裸“当前”等语义，旧独有片段无残留：**PASS**。
- AST 统计 `TestReferenceTimeInjection` 的测试函数定义：**9**。
- `.venv/bin/python -m ruff check` 本次 4 个 Python 改动文件：**All checks passed!**
- `.venv/bin/python -m black --check` 同上：**4 files would be left unchanged.**
- `git diff --check` 本次已跟踪的 4 个改动文件（中间件、提示词、预算测试、观察器测试）：退出码 **0**。`test_doc19_direct_answer.py` 为已有未跟踪文件，已纳入上述 ruff/black 与 pytest 验证。

## 未覆盖边界与修改范围

- 未进行真实模型的自然语言服从性验收，例如“今天几号”是否必定不调工具、用户质疑相对日期时是否每次都复核；假模型只能证明数据流和门控执行，不能证明模型策略。
- 未调用真实数据库/外部服务，live 用例按默认门控跳过；未复审 doc 16、doc 19/17 已收口业务行为，未独立复跑 api 套件。
- 未通过修改磁盘文件重演实施方先改提示词断言的 red/green 顺序；该历史顺序无法从当前工作区直接证实。
- 本轮只新增此审核报告，未修改业务代码、测试或配置，未提交。两项 P3 可在现有用例内最小补齐，无需业务重构、灰度开关或新配置面。
