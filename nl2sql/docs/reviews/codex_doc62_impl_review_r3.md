# doc 6.2 实施复审：第 3/3 轮（最终轮）

**结论：生产功能修复通过复审，仍留 1 项 P3 测试问题，由用户裁定。** 前两轮的业务/资源问题和类型断言问题均已关闭，没有复现新的生产 correctness 问题。不能把下面这个测试的误用上下文当作生产生命周期仍然错误。

**已达用户约定的三轮上限。Claude 请仅回传本报告并等待用户裁定；不要自动发起第 4 轮，也不要将“全部待修项为零”作为本轮结论。** 用户可决定补正这一测试或接受当前实现和验证范围。

基准：当前 doc 6.2 SHA256 `cf6613f61ddc17555e31d43f618b1164c02c92c79b50160a9ff5a2a54e03e084`；读取 `doc62_impl_review_request_r3.md` 并核对当前未提交修改。仍只审不改业务或正式测试；本轮新增审核报告与离线探针，没有改写 6.2 主文档。

## 已关闭的复审项

| 项目 | 结论与证据 |
| --- | --- |
| R2-01：运行状态与异常回溯释放 | 通过。RunBudgetLedger._RunEntry.extra 持有已注册运行的步骤状态，close_run() 移除条目；中间件不将这些状态写入自身回退字典。成功尝试即清 last_error，失败保留至本运行结束。独立补跑“同一中间件、两个均已注册的运行”，第一运行失败耗尽并关闭、第二运行三次内恢复，状态独立、旧异常对象可 GC、没有使用本地回退。 |
| R2-02：类型断言 | 通过。正式测试先断言 type(record["answer"]) is type(raw)，再比较值。重新注入 False→0 归档退化后，两条 payload_false 测试都在精确类型断言处失败；未注入的全量测试通过，证明新增断言能够识别原先漏检的退化。 |
| 第 1 轮 R1 次数上限 | 未回退。普通重试、上下文恢复共享总额、新步骤重置、停止优先及 SDK MockTransport 组合测试继续通过；没有再次出现同一步 6 次请求。 |
| 第 1 轮 R2/R3/R4 | Web 联动、完整原值矩阵与缺失键对照、查询/分析零行文案和保留兜底前缀的用例继续通过。 |

生产接线证据：

- [run_budget.py](../../src/modules/db_deepagent/services/run_budget.py:69)：运行条目新增 extra；[共享状态访问](../../src/modules/db_deepagent/services/run_budget.py:154) 返回该条目字典，未注册运行返回 None；close_run() 移除整个条目，返回快照不携带 extra。
- [model_retry.py](../../src/modules/db_deepagent/middleware/model_retry.py:82)：已注册运行取账本状态，回退字典只用于未注册运行；成功 handler 返回后清空 last_error。
- [Runner finally](../../src/modules/db_deepagent/runner.py:409)：生产调用开始前已 open_run，成功/异常均经 finally close_run，不因失败跳过释放。
- [类型断言](../../src/tests/db_deepagent/unit/test_runner_persistence.py:1504)：False 和 0 不能再仅靠 Python 的值相等混过契约检查。

## R3-01 · P3 / 低：连续两运行测试的第一请求没有使用已登记的运行 ID

**代码证据**：

- [test_sequential_runs_independent_states()](../../src/tests/db_deepagent/unit/test_model_retry.py:1044) 由 _ledger_mw() 打开的是 `run-lc`，另打开 `run-lc2`。
- [第一轮调用](../../src/tests/db_deepagent/unit/test_model_retry.py:1058) 却传 `_Req()`；该类的上下文在 [第 118 行](../../src/tests/db_deepagent/unit/test_model_retry.py:118) 固定为 `run-retry`。
- 因此第一轮走未注册运行的 `_local_steps` 回退，之后 close_run("run-lc") 关闭的是没有实际消费尝试额度的另一条运行记录。后面的“旧运行已释放”断言即使通过，也没有验证第一轮真实状态已释放。
- 已有 [_lc_req()](../../src/tests/db_deepagent/unit/test_model_retry.py:926) 正是该测试需要的 `run-lc` 请求，可以直接复用。

**动态确认**：仅在审核进程包装中间件构造器、运行此正式测试，再观察其实例：测试通过，但本地回退仍留 `run-retry`。

```json
{"submitted_sequential_test_exit_code": 0,
 "leftover_unregistered_run_ids": ["run-retry"]}
```

这说明测试走错分支，不表示已注册生产运行仍然泄漏。其余三条生命周期测试确实使用 _lc_req()；审核者对两个正确注册运行的独立探针也已通过。

**建议修正（本轮未应用，交用户裁定）**：将第 1058 行的 `_Req()` 改成 `_lc_req()`；在 close_run("run-lc") 前断言运行条目确实已记录该步骤的 used，避免以后再次用错误上下文通过测试。第一轮注释“401 一次失败”也应改为实际的连接异常重试耗尽，因为 _payload_error() 抛的是 APIConnectionError。

```python
with pytest.raises(Exception):
    mw.wrap_model_call(_lc_req(), self._payload_error(self._Payload()))
assert budget.run_shared_state("run-lc")[_STATE_KEY]["used"] == 3
budget.close_run("run-lc")
```

用户可仅补正此正式测试，不需要为本问题调整生产实现或新增功能。此项尚未改写，因此报告保留为 1 项 P3，不把独立探针通过等同于该正式测试已修复。

## 实际验证与边界

1. 按前两轮相同范围，在测试进程临时隔离代理变量后复跑：**1075 passed、4 skipped（7.03 秒）**。生产代码和正式测试没有故障注入。
2. [本轮离线探针](codex_doc62_impl_review_r3_probe.py) 使用真实 RunBudgetLedger 和同一重试中间件实例；两个请求分别绑定各自已注册的运行，并各自预占模型步骤，验证完整额度、成功清异常、失败/成功关闭后的状态和对象回收：

```json
{"two_registered_runs_independent": true,
 "tracebacks_released": true,
 "local_fallback_unused": true}
```

3. 同探针独立观察正式连续运行测试，得到上面的 run-retry 回退残留证据。
4. 重跑第二轮的刻意 False→0 变异：payload_false 的真实执行和纯沿用两例均在 `type(...) is type(...)` 断言处失败，pytest 退出码为 1。**这两次失败是审核者刻意注入错误后的预期结果，不是正常实现的测试失败。** 替换在进程内恢复，没有写回业务或正式测试文件。
5. `git diff --check` 通过；未重新运行自动格式化，也未替实施者提交或合并代码。没有访问真实模型或数据库。

从 `nl2sql/` 复现探针：

```bash
PYTHONPATH=. .venv/bin/python docs/reviews/codex_doc62_impl_review_r3_probe.py
```

本轮覆盖的是当前同步 Runner 路径及文档声明的失败收尾边界。异步入口、强杀进程、硬 deadline、仓库外归档消费者兼容等仍按既定范围处理，本报告不把本地回归通过外推为这些能力已获验证。

自动审核循环到此结束。剩余 R3-01 交用户裁定，不创建新任务、不继续第 4 轮。
