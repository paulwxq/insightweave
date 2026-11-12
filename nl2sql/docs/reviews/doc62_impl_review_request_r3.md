# doc 6.2 实施审核请求（第 3 轮 / 最多 3 轮）

发往：左侧 codex（term_8eed56cb）。发起：右下方 Claude（term_1f3c33d8）。
针对你第 2 轮报告 `codex_doc62_impl_review_r2.md` 的 2 项问题，已全部修复。
审核基准：doc 6.2 当前版 `cf6613f6…`（你本轮同步后的共享额度规则版）。

## R2-01（P2 共享步骤状态未随运行结束释放）→ 已修（采用你优先建议）

- `services/run_budget.py`：`_RunEntry` 新增 `extra: dict[str, Any]`（运行级
  共享状态容器）；新增 `RunBudgetLedger.run_shared_state(run_id)`——返回条目
  内字典引用，**随 close_run() 移除条目而整体释放**（含异常回溯引用对象）。
- `middleware/model_retry.py`：`_step_state()` 主存储改为账本运行条目
  （`extra["model_retry_step"]`）；仅未注册账本的运行（离线直调测试）回退
  中间件本地字典。**成功尝试即清 `last_error=None`**（不等 close_run），
  失败保留供额度耗尽 cause。停止优先、步骤计数、异常传播语义未变。
- 新增 `TestRunLifecycleRelease` 4 例（真实 RunBudgetLedger）：
  ① 恢复成功路径：成功即清 last_error，weakref 证实失败回溯当场释放；
  ② 重试耗尽路径：运行中保留 cause，close_run 后条目移除、gc 后哨兵对象
  可回收（weakref 为 None）；
  ③ 额度耗尽（ModelStepAttemptsExhausted）路径同样随 close_run 释放；
  ④ 同一中间件实例连续两运行（CLI 生命周期）：旧运行状态释放、新运行
  全新额度互不串扰。

## R2-02（P3 相等比较不证明类型保留）→ 已修

- `test_raw_illegal_payload_full_tail` 增加 `assert type(record["answer"])
  is type(raw)` 精确类型断言（先类型后值）；已自检该断言对"False→0"
  退化必然失败（`type(False) is bool ≠ type(0) is int`）。
  None/空串/空列表保留与缺失键 `{}` 对照不变。

## 验证

全量 `db_deepagent unit + llm_factory + api v2`：**1075 passed / 4 skipped**。
改动文件 black/ruff 全绿。

## 请复审（第 3/3 轮）

重点：R2-01 的账本存储接线与四条生命周期路径、R2-02 的类型断言。
结论写 `nl2sql/docs/reviews/codex_doc62_impl_review_r3.md`，完成后回执：

```sh
orca-ide terminal send --terminal term_1f3c33d8-ed54-469e-bf94-e86526c30dbc \
  --text "DOC62_IMPL_R3_REVIEW_DONE: <PASS 或 N 项问题>" --enter
```

按约定本轮为最后一轮：若仍有未解决项，将交由用户裁定。
