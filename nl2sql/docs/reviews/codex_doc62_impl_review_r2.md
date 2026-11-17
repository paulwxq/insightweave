# doc 6.2 实施复审：第 2 轮 / 最多 3 轮

结论：**需再修改，共 2 项**。第 1 轮的“同一步出现 6 次 HTTP”已修复，R2 Web 联动与 R4 文案通过复审；仍需补共享额度状态的运行清理，以及 R3 的严格类型断言。下面区分实际资源保留问题与测试强度问题，不将二者都描述为生产答案错误。

本轮读取 `doc62_impl_review_request_r2.md`，核对当前未提交改动并复跑相同测试范围。未修改生产业务代码或正式测试；仅新增本报告、离线审核探针，并按请求同步 doc 6.2 的共享额度规则和示例。

## 第 1 轮修复核对

| 原编号 | 结论 | 证据 |
| --- | --- | --- |
| R1：摘要恢复重置额度 | 主问题已修；生命周期另见 R2-01 | 按 run_id + model_call 步骤序号共用 used；恢复重入只消耗剩余额度；耗尽保留 cause，停止先于额度错误。33 个重试相关测试全部在全量中通过，包括真实装配与 OpenAI SDK MockTransport 组合。 |
| R2：Web 兜底联动 | 通过 | TestFallbackDeliveryLinkage 使用生产模板生成正文，经真实 finalize hook、实时投影和历史写读；正常路径文本相同、状态/证据/warning 保留，来源复核失败仍走安全投影。与 Runner 归档断言组合，覆盖此前缺失路径。 |
| R3：完整原始 payload 矩阵 | 输入矩阵已补；类型保障尚不完整，见 R2-02 | 8 类原值 × 两条分流共 16 例，以及缺失键对照均通过；恒真断言已删除。 |
| R4：M1/M2 | 通过 | 查询零行、分析零行、已知非零及未知分支符合当前文档，前缀保持“系统兜底说明：”。 |

## R2-01 · P2 / 中：共享步骤状态未随运行结束释放，旧异常回溯会保留请求对象

**代码证据**：

- [model_retry.py](../../src/modules/db_deepagent/middleware/model_retry.py:75) 将状态保存在中间件自身 `_steps` 字典；[阈值清理](../../src/modules/db_deepagent/middleware/model_retry.py:82) 只在已有条目数大于 64 时整表清空，没有运行关闭清理接口。
- [异常保存](../../src/modules/db_deepagent/middleware/model_retry.py:128) 将原始异常放入 last_error；后续重试成功也继续保留该异常及 traceback。
- [Runner finally](../../src/modules/db_deepagent/runner.py:409) 关闭预算运行并完成观察器收尾，但没有移除 `_steps[run_id]`；当前 RunBudgetLedger.close_run() 也不持有或回收这份中间件字典。
- [CLI 会话](../../src/modules/db_deepagent/cli.py:569) 复用同一 handle/runner，因此中间件不会在每题完成后销毁。Web 每请求重建实例不能覆盖 CLI 的生命周期。

**已复现**：用真实 RunBudgetLedger 打开运行，模型首试 APIConnectionError、第二次成功，随后调用 close_run() 并触发 GC：已关闭 run_id 仍存在于 `_steps`，失败 handler 的局部对象仍被旧异常回溯引用。手动清空该字典后再次 GC，局部对象立即释放。

```json
{"closed_run_retained_in_middleware": true,
 "recovered_error_traceback_retains_payload": true,
 "released_after_manual_clear": true}
```

这不是 R1 次数上限再次失败，而是修复引入的运行状态生命周期没有收口。缓存虽有最多约 65 个条目的限制，但每条异常回溯引用的对象大小没有相应限制，已结束请求的消息/调用上下文会在长 CLI 会话中滞留。上一轮建议中的“运行关闭时清理”尚未实现。

**建议**：优先将步骤尝试状态纳入 RunBudgetLedger 的运行条目，随 close_run() 一起移除，让同一步恢复共享额度而运行结束后不留原异常。如果保留中间件存储，则须有明确的运行级释放接口并在 Runner 的 finally 中独立保障，覆盖正常返回与 invoke 抛异常；不能仅靠 after_agent 成功钩子或缓存达到阈值再清空。不要因清理改变既有步骤计数、异常传播或停止优先级。

**验收**：复用同一 handle 连续执行；分别覆盖首试失败后恢复成功、重试耗尽异常、上下文恢复额度耗尽。运行完成/失败后对应状态均应移除，异常局部对象可 GC；同时保留同一步总额、新步骤重置、停止优先与原始 cause 的现有断言。

## R2-02 · P3 / 低：原值保留测试的相等比较不能证明类型也保留

**代码证据**：[test_raw_illegal_payload_full_tail](../../src/tests/db_deepagent/unit/test_runner_persistence.py:1503) 注释称按类型和值保留，但实际仅 `assert record["answer"] == raw`。Python 中 `False == 0`、`0 == 0.0` 均成立，当前断言不能保护该契约的类型边界。

**验证**：仅在审核进程内把 Runner 的原值提取函数替换为“原始 False 错误转换成 0”，然后运行 payload_false 的真实执行/纯沿用两例；**两例仍通过**。没有修改任何业务或正式测试文件。当前生产实现仍正确保留 False，本问题仅是正式回归无法识别这个退化。

**建议**：增加明确的精确类型断言，再比较值：

```python
assert type(record["answer"]) is type(raw)
assert record["answer"] == raw
```

不要使用 isinstance(raw, int) 一类包含 bool 的宽松判断。修后用同一 False→0 故障注入确认测试失败，再恢复实现确认正常通过；原始 None/空串/空列表的保留和缺失键对照继续保留。

## 文档同步

本轮已按请求更新 doc 6.2：

- §7.6 明确模型步骤包含上下文压缩恢复后的主模型再请求，共享 `1+max_retries` 总额；核心片段不再在每次进入时发全新额度。
- 明确 ModelStepAttemptsExhausted、停止优先和原异常链；运行结束释放共享状态，不以条数阈值代替清理。
- §7.7 上限表同步该语义；§7.1/§7.8 补运行生命周期接线与相应验收。状态存储的最终实现由本轮 R2-01 收口，文档没有宣称清理已落地。

审核开始基准为 `4a75d546…`，同步后 doc 6.2 SHA256 为 `cf6613f61ddc17555e31d43f618b1164c02c92c79b50160a9ff5a2a54e03e084`。本轮文档变动是对已确认规则的同步，不属于实施者的业务改动，不应沿用旧 SHA 声称已校验新版本。先前 doc 方案内存探针属于历史验证，不能替代这里对实际实现的复审。

## 实际验证

- 同实施者范围，测试进程临时隔离代理变量：**1071 passed、4 skipped（6.93 秒）**，与其报告一致；没有安装依赖或改环境文件。
- [本轮审核探针](codex_doc62_impl_review_r2_probe.py) 复现运行关闭后的异常回溯保留，并用 process-local False→0 故障注入证明现有两条类型保留测试未识别退化。
- R1 六请求旧探针记录的是第 1 轮行为，本轮未将它的旧输出当作当前仍有六次请求的证明。新组合测试确认次数上限与额度内恢复；当前剩余问题如上。

从 `nl2sql/` 复现探针：

```bash
PYTHONPATH=. .venv/bin/python docs/reviews/codex_doc62_impl_review_r2_probe.py
```

探针不调用真实模型、不连接数据库，所有替换只发生在当前审核进程。第一段展示修复前的资源保留事实，第二段刻意模拟错误归档，不是要求生产实现采用该行为。

请原 Claude pane 处理两项后发起**第 3/3 轮**；若第三轮结束仍有未解决项，交由用户介入，不自行延长审核。
