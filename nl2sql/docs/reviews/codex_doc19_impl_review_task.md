# Codex 审核任务书：doc 19 + doc 17 实施（第 1 轮）

你是本仓库的代码审核方。请审核**未提交工作区**中 doc 19 与 doc 17 的实施，
把发现的问题与建议写入报告文件，不要修改业务代码、测试或配置，不要提交。

## 范围与边界

- 分支 `feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` + 工作区未提交改动。
- **注意**：工作区还包含 doc 16（时间解析 LLM 重写）的未提交改动，该部分
  已经四轮审核收口（`docs/reviews/codex_doc16_impl_review_r1-r4.md`，R4 PASS）。
  本次只审 **doc 19/17 的增量**；doc 16 文件（time_resolver*.py、guard_tools.py、
  model_observer.py、agent.py、config.py、db_deepagent.yaml、conftest.py、
  test_time_resolver*.py、test_guard_tools.py、test_model_observer.py、
  test_tool_output_offload.py 等）**不在审核范围**，除非发现与本次改动的交叉影响。
- 禁止调用真实模型与真实数据库（不要 source .env 跑 live 用例）。可使用
  项目 `.venv` 运行离线测试，例如：
  `.venv/bin/python -m pytest src/tests/db_deepagent -q -o addopts= -p no:cacheprovider`
  或定向文件。设置 `PYTHONDONTWRITEBYTECODE=1`。
- 实施方宣称：新增 `test_doc19_direct_answer.py` 24 项全过；全量
  **1164 passed / 36 skipped / 0 failed**（未 source .env，live 门控跳过属预期）；
  `src/tests/unit/api` 185 passed；ruff/black 对本次新改文件通过（存量
  中文 noqa 失效与 BLE001 基线不在本次范围）。

## 依据文档（先读）

1. `docs/19_db_deepagent非取数回答直接提交修改方案.md`（实施稿，经两轮
   外部审核修订后定稿）
2. `docs/17_db_deepagent答案展示精简与口径说明分离修改方案.md`
3. 背景：`docs/18_db_deepagent时间常识问答被门控拒绝问题分析.md`

## 本次实施改动的文件

| 文件 | 改动内容（对照 doc 19/17 条目） |
| --- | --- |
| `src/modules/db_deepagent/services/task_completion.py` | §3 零任务直接分支（步骤 1.5，含 None 保护与 failed/partial/no_data 不开放）；`TASK_COMPLETION_RULES_VERSION` 1→2；`build_delivery_answer_section` 增加 `include_audit_details` 精简模式（doc 17 §4.2） |
| `src/modules/db_deepagent/tools/final_answer.py` | §7 工具 docstring（直接回答路径 + 口径分工）；拒绝 hint 按错误码分流（task_not_registered / direct_answer_task_refs_not_allowed） |
| `src/modules/db_deepagent/middleware/final_answer_gate.py` | §5 收卷提醒文本去掉无条件"并引用已有 evidence_id" |
| `src/modules/db_deepagent/runner.py` | §6 `_archive_run` 新增 condition_c（零任务合法提交写 QA）；交付正文用精简模式追加；DEBUG 记录完整分项诊断（不进交付正文） |
| `src/modules/db_deepagent/schemas.py` | 仅注释：open_questions 零任务直接澄清例外（不改字段/校验） |
| `src/modules/db_deepagent/config/prompts.yaml` | §4.1 登记指令收窄 + 三处"已登记任务"限定 + 防跳查句 + doc 17 answer/metric_definitions 分工 |
| `src/modules/db_deepagent/skills/query-planning/SKILL.md` | §4.2 同步登记边界（含混合问题） |
| `src/modules/db_deepagent/skills/result-analysis/SKILL.md` | doc 17 §3.2 完成条件字段分工 |
| `src/tests/db_deepagent/unit/test_doc19_direct_answer.py` | 新增 24 项（doc 19 §10 全部 + doc 17 精简模式 + 文本同步断言） |
| `test_task_completion.py` / `test_final_answer.py` / `test_runner_persistence.py` / `test_doc63_r3_regressions.py` | 既有断言按新规范更新（4 处，均有注释说明依据） |

## 审核重点

1. 对照 doc 19 §3/§4/§5/§6/§7 与 doc 17 §3/§4 逐条核对实现是否相符、
   有无遗漏或超范围改动。
2. 直接分支的边界：None 提交、仅作废、failed/partial/no_data、携带
   findings/unresolved、voided-only 的行为是否与文档一致。
3. 归档 condition_c 的判定口径（`decision.outcome_summaries` 为空）是否与
   门控直接分支口径一致；会不会误放行"有任务但全 voided"或"有执行记录
   但被拒"的轮次。
4. 精简模式是否保留了失败/澄清条目、完整模式默认行为是否未变；DEBUG
   完整诊断是否不会拼进交付正文。
5. 测试断言更新的合理性（4 处旧断言改动是否掩盖了行为回归）。
6. **不要过度设计**：问题按 P1（必须修）/P2（应修）/P3（建议）分级，
   建议给最小修改方案；本项目未上线，无向下兼容负担，不引入灰度开关
   或环境变量配置面。

## 产出

写入 `docs/reviews/codex_doc19_impl_review_r1.md`：

- 总体结论：PASS / REQUEST_CHANGES（+一句话）
- 编号问题清单（C1-xx：级别、位置 file:line、证据、影响、最小修改建议）
- 本轮独立验证证据（跑了什么、结果）
- 明确说明未覆盖的边界（如真实模型语义、带时刻 guard 等）

完成后结束回合即可，Orca 会回传。
