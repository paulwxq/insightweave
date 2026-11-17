# db_deepagent 主 Agent 北京时间上下文注入方案

> 状态：讨论定稿备忘，尚未实施。
> 日期：2026-11-16。
> 依据：当前工作区代码及用户对时间背景注入、时间工具职责的讨论。
> 本文只记录修改方案，不代表代码已经修改或验证通过。

## 1. 已确认的决策

在已有的 `RequestAuditMiddleware.wrap_model_call()` 中，向每次主 Agent 模型请求的系统消息追加本轮北京时间背景。

- 不新建中间件，不改中间件装配顺序，不新增工具或模型调用。
- 使用 `ctx.now`，与现有时间解析工具保持相同的权威时钟。
- 主 Agent 通过注入内容知道今天的日期；自然语言时间范围仍由 `resolve_time_range` 解释、计算。
- 时间工具暂不增加 `reference_now` 返回字段：自动注入后，该字段不再是本需求的必要条件。
- 调整“需要当前日期基准时必须调用工具”的提示词；保留时间表达解析和时间计算复核的工具要求。

## 2. 当前代码与问题

| 位置 | 当前行为 |
| --- | --- |
| `context.py`：`DbAgentContext.now` | 带时区的运行级参考时间 |
| `runner.py`：创建 `DbAgentContext` | 每轮请求取一次时钟，优先使用测试配置的 `fixed_now` |
| `services/time_resolver.py`：`TimeResolver.resolve()` | 将 `ctx.now` 转为 `Asia/Shanghai`，通过 `reference_now` 提供给 Flash |
| `tools/guard_tools.py`：`resolve_time_range` | 返回区间、时区、解释和 SQL 提示，未返回 `reference_now` |
| `middleware/request_audit.py` | 每次主模型请求均能读取 `request.runtime.context`，但尚未注入时间背景 |

因此 Flash 有可信参考时间，主 Agent 却可能只能从 `interpretation` 的措辞或历史对话间接猜测日期。不能依赖解释文本是否恰好提到“今天几号”。

## 3. 时钟语义

```python
reference_now = ctx.now.astimezone(ZoneInfo("Asia/Shanghai"))
```

这里不是每次调用模型时重新读取操作系统时钟，而是把**本轮请求开始时定格的服务端时间**转换为北京时间；配置 `fixed_now` 时使用该固定参考时间。

- 同一 run 的全部主模型调用、重试及时间工具调用使用相同的参考时钟。
- 下一轮用户请求创建新的上下文，自动取得新的参考时间。
- 即使一轮执行跨午夜，也不在中途改变“今天”的基准，避免主 Agent 与时间工具相互矛盾。
- 历史轮次的日期不能覆盖本轮系统注入的参考时间。
- 不修改 Runner 的时钟创建逻辑，不增加第二套时钟配置。

## 4. 注入位置与内容

修改 `RequestAuditMiddleware.wrap_model_call()`：读取 `ctx` 后，先创建带时间背景的新请求，再进行现有 Token 估算、工具集合核对、审计及 `handler(request)` 调用。

```text
读取 runtime.context
→ 从 ctx.now 生成北京时间背景
→ 创建包含该背景的新 system_message / request
→ 原有请求审计与工具核对
→ 调用主模型
```

注入文本模板：

```text
## 本轮北京时间背景
本轮参考日期：{reference_now.date().isoformat()}
本轮参考时间：{reference_now.isoformat()}
时区：Asia/Shanghai（北京时间，UTC+08:00）。
以上是本轮请求开始时的参考时间；本轮“今天”以此为准。
历史对话中的参考时间不代表本轮日期。
自然语言时间范围解析、SQL 时间边界生成及时间计算结论复核仍使用 resolve_time_range。
```

本方案的背景注入只针对主 Agent 请求。Flash 时间解析器继续通过原有输入接收 `reference_now`；不扩展摘要模型的输入职责。

### 4.1 核心实现建议

可以在 `request_audit.py` 内增加一个私有辅助函数 `_with_reference_time()`，供现有 `wrap_model_call()` 调用，不增加新类、新文件或配置开关。

以下为核心代码示意，实施时补齐现有文件中的类型与导入：

```python
from zoneinfo import ZoneInfo

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage


_REFERENCE_TIME_HEADER = "## 本轮北京时间背景\n"


def _with_reference_time(
    request: ModelRequest[DbAgentContext],
) -> ModelRequest[DbAgentContext]:
    reference_now = request.runtime.context.now.astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    clock_text = (
        f"\n\n{_REFERENCE_TIME_HEADER}"
        f"本轮参考日期：{reference_now.date().isoformat()}\n"
        f"本轮参考时间：{reference_now.isoformat()}\n"
        "时区：Asia/Shanghai（北京时间，UTC+08:00）。\n"
        "以上是本轮请求开始时的参考时间；本轮‘今天’以此为准。\n"
        "历史对话中的参考时间不代表本轮日期。\n"
        "自然语言时间范围解析、SQL 时间边界生成及时间计算结论复核"
        "仍使用 resolve_time_range。"
    )
    clock_block = {"type": "text", "text": clock_text}
    original = request.system_message
    if original is None:
        system_message = SystemMessage(content=[clock_block])
    else:
        blocks = (
            [{"type": "text", "text": original.content}]
            if isinstance(original.content, str)
            else list(original.content)
        )
        # 仅替换本方案保留标题开头的独立文本块，保留其他内容块。
        blocks = [
            block
            for block in blocks
            if not (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].lstrip().startswith(_REFERENCE_TIME_HEADER)
            )
        ]
        system_message = original.model_copy(
            update={"content": [*blocks, clock_block]}
        )
    return request.override(system_message=system_message)
```

现有方法内的接入位置固定如下：在 `ctx = ...` 后立即注入，随后才执行现有序号生成、Token 估算、审计及模型调用；不得移到估算之后。

```python
def wrap_model_call(self, request, handler):
    ctx = request.runtime.context
    request = _with_reference_time(request)
    # 以下继续原有审计、估算、工具集合核对和 handler(request)。
```

约定：

- 除本方案自己的时间背景块外，保留原系统消息全部内容块及消息元数据，不用 `.text` 提取后重建整段系统提示词。
- 使用公开的 `request.override()`，不原地修改共享请求或系统消息。
- 不写入 `state.messages`，不形成每轮不断累积的日期消息。
- 不把模型参数、工具集合或普通对话消息替换掉。
- 每次对原请求创建副本；重复处理同一原请求或已经注入过的返回请求，时间背景均只保留一块，并使用当前 `ctx.now` 更新。两种重复处理场景都应测试。
- `## 本轮北京时间背景` 为本方案独立注入块的保留标题；识别时先去掉前导空白，仅匹配文本块开头，不在整段原提示词中搜索并截断内容。发现多个旧时间块时统一移除，再追加当前块。
- 不调用 SDK 私有辅助函数，不改变原有重试、预算及工具核对逻辑。

原示例依靠创建副本，已经能够避免“重复处理同一个原请求”时累积；上述替换逻辑额外覆盖“把已注入的返回请求再次交给包装器”的情况，不意味着当前重试链已经发生重复注入。

## 5. 提示词调整

修改 `config/prompts.yaml` 的“时间”节。当前表述要求：

> 时间结论被质疑或需要当前日期基准时，必须重新调用 resolve_time_range 核实。

上述引用仅为摘要。实施时须完整替换该条 YAML 规则及其所有续行：从“时间结论被质疑或需要当前日期基准时”开始，至“说明此前有误。”结束。不能仅按摘要做前缀替换，也不能只替换第一物理行，否则会遗留旧规则的半句残尾。相邻的其他时间规则保持原意。

改为区分“知道参考日期”和“解析、复核时间计算”：

> 当前日期与时间以系统注入的本轮北京时间为准，无需为获取今天日期单独调用工具，不得从历史对话或模型记忆猜测当前日期。
>
> 需要解析自然语言时间表达、生成 SQL 查询时间边界，或时间计算结论被质疑时，调用 resolve_time_range；source_text 逐字引用用户原话中的时间表达，不自行计算查询边界。
>
> 复核时间计算时使用本轮参考时间；复核结果与此前结论冲突时如实说明差异，仅在确认此前计算错误时说明此前有误。不能仅因两轮参考日期不同，就把相对日期变化认定为此前计算错误。

现有规则继续保留：

- SQL 时间边界来自 `resolve_time_range`，或经受控沿用的已确认证据。
- 有限区间使用 `>= start AND < end`，单边区间不擅自补齐另一端。
- 不通过 SQL `CURRENT_DATE`、`now()` 另取时钟。
- 裸“当前／目前／现在”不自动解释成“今天全天”的查询范围。

例如，用户问“今天是几号”可直接读取注入日期；用户问“大前天是哪天”“上个月的区间”或“截至今天中午12点”，仍使用时间工具。

## 6. 时间工具与日志

### 6.1 时间工具保持不变

`resolve_time_range` 内部继续使用 `ctx.now`，返回的 `start`、`end`、`timezone`、`interpretation` 和 `sql_hint` 不变。

工具返回中增加 `reference_now` 与本方案不冲突，但本次不增加：主 Agent 在调用工具之前已经能得到权威参考日期，无需重复提供，也不调整时间证据 schema。

### 6.2 沿用完整上下文 DEBUG 日志

doc 13 的 `ModelCallObserver` 在模型调用回调中记录实际收到的上下文。时间背景加入模型请求后，应自然进入现有 `MODEL_INPUT_BEGIN/END` 的 DEBUG 日志块。

不新增日志文件，不另行打印完整提示词；原有 Token 估算应在注入后执行，将新增背景计入现有统计。审计文件继续保持原有摘要记录职责。

## 7. 修改文件清单

| 文件 | 修改内容 |
| --- | --- |
| `src/modules/db_deepagent/middleware/request_audit.py` | 增加 `_with_reference_time()`；在 `wrap_model_call()` 的估算与模型调用前接入；同步职责说明 |
| `src/modules/db_deepagent/config/prompts.yaml` | 移除“仅为获取今天日期也必须调用工具”的要求，明确注入时间与时间解析工具各自职责 |
| `src/tests/db_deepagent/unit/test_budget_middleware.py` | 集中更新请求测试替身，覆盖 None / str / list 三种系统消息形态、两类重复处理、原请求不变及原有审计行为 |
| `src/tests/db_deepagent/unit/test_doc19_direct_answer.py` | 同步 `test_prompts_time_challenge_requires_reresolve`：验证 §5 的新三段关键语义、旧规则无残留及其他时间规则保留 |
| `src/tests/db_deepagent/unit/test_model_observer.py` 或现有装配测试 | 复用现有假模型验证真实主模型输入及 DEBUG 日志含时间背景；不调用付费模型 |

当前审计测试用 `SimpleNamespace` 构造请求，没有 `override()`。实施时改用真实 `ModelRequest` 或具有同等公开行为的测试替身，不在生产代码中增加兼容这种旧测试替身的回退逻辑。

三种系统消息形态、请求替身更新及幂等用例均放在 `test_budget_middleware.py`，不分散到多个测试文件。提示词回归继续放在 `test_doc19_direct_answer.py`；该文件现有测试断言了将被替换的“时间结论被质疑或需要当前日期基准时”等旧文本，必须与提示词修改同步。新断言覆盖：系统注入时间为权威基准且无需为获取今天日期调用工具、解析或复核时间计算仍调用工具、参考日期变化不等于此前计算错误。真实装配及完整日志验证仍放在相应测试中。

`agent.py`、中间件导出与装配顺序、Runner、时间解析器、时间工具、任务账本和前端均无需修改。

## 8. 验证要点

优先在既有测试中覆盖，不搭建新的测试框架：

1. UTC 时间转换为北京时间后跨日正确；固定时间配置的结果与工具参考时钟一致。
2. 在 `test_budget_middleware.py` 集中验证系统消息为空、字符串内容及结构化内容块时均可注入，非时间块内容与消息元数据保留。
3. 同一原请求重复处理，以及已注入请求再次处理，均只保留一个时间块；已有多个旧时间块时也归一为一个。相同 run 使用同一时间，新 run 能替换旧块并刷新日期；原请求及历史 messages 不变。原提示词其他位置出现该标题文本时不被截断或删除。
4. 原有工具集合核对、审计返回值与调用流程正常；验证注入发生在 Token 估算及 handler 调用之前，两者使用同一份注入后的请求。
5. 通过现有装配链和假模型确认主模型收到时间背景，DEBUG 完整上下文日志能看到该段。
6. `test_doc19_direct_answer.py` 中的提示词回归断言随 §5 更新，保留禁止猜测当前日期与要求时间计算复核的意图。先修改断言确认旧提示词无法通过，再完整替换规则并确认测试通过；除验证新内容存在外，还应断言旧规则开头及仅属于旧规则的续行片段已移除，并验证相邻时间规则保留。仅断言新内容存在，不能证明旧规则无残留；不要求删除新旧规则共同使用的术语（如 `source_text`）。

实施后再做人工验证：直接问今天日期不必调用时间工具；询问相对日期或查询时间边界时仍调用工具；后续追问和质疑时不再从历史解释文本猜测“今天”。注入可信背景可以减少日期猜测，但不承诺消除全部模型错误。

## 9. 范围与回退

这是一处请求背景补充，不改变 Agent 的任务规划权，不新增执行门控，不增加模型调用，不扩展 doc 17/19 的任务与答案规则。

回退时同步撤销中间件注入及依赖它的提示词调整，并恢复对应测试；无需迁移数据、修改工具参数或调整存储。
