# Codex 审核任务书：doc 20 实施（第 1 轮）

你是本仓库的代码审核方。请审核**未提交工作区**中 doc 20（主 Agent 北京时间
上下文注入）的实施，把发现的问题与建议写入报告，不要修改业务代码、测试或
配置，不要提交。

## 范围与边界

- 分支 `feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` + 工作区未提交改动。
- **工作区还包含两批已收口的未提交改动，不在本次范围**：
  - doc 16（时间解析 LLM 重写）：已四轮审核收口（codex_doc16_impl_review_r1-r4.md）；
  - doc 19 + doc 17（非取数直接提交 + 展示精简）：已两轮审核收口
    （codex_doc19_impl_review_r1/r2.md）。
  - 除非发现与 doc 20 改动的交叉影响，不要重复审它们。
- 禁止调用真实模型与真实数据库。可用项目 `.venv` 跑离线测试，例如：
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest src/tests/db_deepagent -q -o addopts= -p no:cacheprovider`
  （如遇 SOCKS 代理报错，仅移除该测试进程的代理环境变量。）
- 实施方宣称：全量 **1362 passed / 36 skipped / 0 failed**（未 source .env，
  live 门控跳过属预期）；api 套件 187 passed；改动文件 ruff/black 通过。

## 依据文档（先读）

1. `docs/20_db_deepagent主Agent北京时间上下文注入方案.md`（实施稿，经三轮
   评审修订定稿——重点看 §4.1 实现约定、§5 替换纪律、§7 文件清单、§8 验证要点）

## 本次实施改动的文件

| 文件 | 改动（对照 doc 20） |
| --- | --- |
| `src/modules/db_deepagent/middleware/request_audit.py` | 新增 `_REFERENCE_TIME_HEADER` 常量与 `_with_reference_time()`（§4.1：ctx.now→Asia/Shanghai、保留标题识别、替换式幂等、request.override 纯函数）；`wrap_model_call` 在 `ctx = ...` 后立即注入（§4：估算与 handler 用注入后请求）；模块 docstring 补职责说明。ruff --fix 顺带把 typing.Callable 归位 collections.abc |
| `src/modules/db_deepagent/config/prompts.yaml` | §5：旧规则整条（4 物理行）替换为新三段（注入基准/工具复核/复核纪律）；相邻时间规则未动 |
| `src/tests/db_deepagent/unit/test_budget_middleware.py` | §7：请求替身升级为真实 `ModelRequest`（`_model_request` 帮助函数）；新增 `TestReferenceTimeInjection` 10 项：None/str/list 三形态、跨午夜北京转换、两类重包幂等、多旧块归一、句中标题不误删、原请求不变、中间件集成（handler 收到注入后请求且原请求未变）；既有审计测试改用新替身 |
| `src/tests/db_deepagent/unit/test_doc19_direct_answer.py` | §8.6：`test_prompts_time_challenge_requires_reresolve` 重写——新三段关键语义存在、旧规则独有片段无残留（归一化续行后断言，防换行假绿）、相邻规则保留 |
| `src/tests/db_deepagent/unit/test_model_observer.py` | §8.5：新增 `TestReferenceTimeLogging`——含时间背景块的消息经 `on_chat_model_start` 进入 doc 13 DEBUG 完整上下文日志 |

## 审核重点

1. 对照 doc 20 §4/§4.1/§5 逐条核对实现；特别核对 §5:156 的替换纪律是否
   被遵守（整条替换、无半句残尾、相邻规则原样）。
2. `_with_reference_time` 的幂等语义：重复处理同一原请求 / 重包已注入请求 /
   多旧块 / 句中标题出现，是否与 §4 约定一致；`request.override` 用法是否
   为公开 API 且不改原对象。
3. 注入位置：是否确实在 Token 估算与 handler 之前（§8.4），工具集合核对
   的行为是否未被改变。
4. 测试替身升级是否引入了对 SDK 私有行为的依赖；`ModelRequest` 构造用法
   是否公开合法。
5. 提示词断言的归一化策略（`re.sub(r"\n[ \t]+", "", raw)`）是否会掩盖
   真实问题（例如把不该拼接的行拼起来导致假绿）。
6. **不要过度设计**：问题按 P1/P2/P3 分级，建议给最小修改；未上线项目无
   向下兼容负担，不引入灰度开关或新配置面。

## 产出

写入 `docs/reviews/codex_doc20_impl_review_r1.md`：

- 总体结论：PASS / REQUEST_CHANGES（+一句话）
- 编号问题清单（C1-xx：级别、位置 file:line、证据、影响、最小修改建议）
- 本轮独立验证证据（跑了什么、结果）
- 未覆盖边界说明

完成后结束回合即可，Orca 会回传。
