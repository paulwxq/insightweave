# doc 16 实施审查，第 1 轮

总体结论：**REQUEST_CHANGES**。

审查基线：`feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` 及当前工作区；依据 `nl2sql/docs/16_db_deepagent时间解析LLM重写方案.md`。本轮只创建本报告，未修改业务代码、测试或配置，未提交，未调用真实模型或业务库。

## 编号问题清单

### C1-01｜高 / P1｜服务容器成功构造路径必然 NameError

- **位置**：`nl2sql/src/modules/db_deepagent/services/__init__.py:205`。
- **证据**：函数形参是 `cfg`，新增接线却是 `time_resolver=build_time_resolver(config)`；该作用域及模块均没有 `config` 变量。将数据库、向量库、图服务等依赖全部替换为假对象，执行真实 `build_services(cfg, source_id="db_src_1", verify_embedding=False)`，得到 `NameError: name 'config' is not defined`，日期解析器构造函数尚未被调用。对实施涉及的七个 Python 业务文件执行 Ruff F821 检查，也只报出此处未定义名称。
- **影响**：正常 CLI/Web 服务启动在容器返回前失败，任何查询都无法进入新时间解析链路。现有 `assembly_services` 直接实例化容器，不能检出此接线错误；既有真实 `build_services` 单测只覆盖提前拒绝或中途失败。
- **最小修改建议**：将参数改为 `build_time_resolver(cfg)`；增加一个全部外部依赖为假的 `build_services` 成功构造用例，断言日期构造器收到同一个 `cfg`，并核对返回容器的解析器实例。

### C1-02｜高 / P1｜边界格式校验过宽，非法模型输出可越过信封或形成错误 SQL

- **位置**：`nl2sql/src/modules/db_deepagent/services/time_resolver.py:260`、`:263`。
- **证据**：`datetime.fromisoformat()` 接受的格式远多于 doc 16 §3.2 规定的 `YYYY-MM-DD` / `YYYY-MM-DDTHH:MM:SS`（无偏移）。以合法 `status="ok"`、`grain="day"`、`interpretation="mock"` 包装以下假响应，调用真实 `TimeResolver.resolve()`，本轮复现如下：

  | start | end | 实际结果 |
  | --- | --- | --- |
  | `2026-11-13T00:00:00+08:00` | `2026-11-14T00:00:00` | 抛出 `TypeError: can't compare offset-naive and offset-aware datetimes` |
  | `2026-11-13T00:00:00+00:00` | `2026-11-14T00:00:00+00:00` | 返回成功证据，仍标为 `Asia/Shanghai` |
  | null | `2026-11-14T00:00:00Z` | 返回成功证据 |
  | `20261113` | `20261114` | 返回成功证据 |
  | `2026-11-13'00:00:00` | `2026-11-14T00:00:00` | 返回成功证据 |

- **影响**：第一例没有归入要求的 `time_parse_invalid_output`，直接逃出解析器；其余例可被工具落账。最后一例中的引号还会原样进入 `_sql_hint`，形成非法 SQL。这是模型输出验形缺陷，与 doc 已声明挂起的时刻 guard 匹配问题不同。
- **最小修改建议**：先限定为上述两种无偏移的完整格式，再用日期解析校验真实日期与大小关系；不对偏移做转换，也不在外围增加宽泛异常兜底。为混合偏移、双端偏移、单边偏移、紧凑日期及异常分隔符补参数化反例，至少经 ToolNode 验证非法输出返回对应错误信封且不更新 `db_time_evidence`，并保留合法日期/日期时间混用与澄清双 null 的成功行为。

### C1-03｜中 / P2｜规划技能仍教主 Agent 发送已删除的参数

- **位置**：`nl2sql/src/modules/db_deepagent/skills/query-planning/SKILL.md:37`。
- **证据**：该行仍要求单边需求用 `resolve_time_range(kind="open_range", ...)`。新工具只允许 `source_text/context`；本轮通过真实 ToolNode 发送 `{"source_text":"前天","kind":"open_range"}`，返回 `invalid_argument: kind(extra_forbidden)`。虽然第 50 行示例已更新，第 37 行仍会作为技能内容进入主 Agent 上下文。
- **影响**：用户问“截至某日／从某日起”时，主 Agent 按技能调用即产生无效参数错误，与 doc 16 §8 的残留旧参数清理要求不符。
- **最小修改建议**：该行改为逐字传入对应单边原文，例如 `resolve_time_range(source_text="截至今天中午12点")`，继续保留“不补另一端”的语义约束。

### C1-04｜中 / P2｜日期 DEBUG 日志遗漏实际 System 输入

- **位置**：`nl2sql/src/modules/db_deepagent/services/time_resolver.py:131`。
- **证据**：唯一的日期 DEBUG 记录只写 `human_payload` 与 `raw`，没有 `self._system_prompt`，且位于调用成功之后。用含唯一标记的 System 提示词调用并捕获日志参数，确认日志不含该标记；模型超时路径也在到达该记录前返回。observer 的完整上下文日志明确只处理 `agent`，不会为 `time_resolution` 补记 System。
- **影响**：不满足 doc 16 §7、§9.2.6 对实际 System/Human 输入的日志验收；提示词文件后来改变时，仅凭运行日志无法还原该次日期调用的实际指令。
- **最小修改建议**：调用前记录实际 System/Human，调用后记录响应，沿用 request_id/run_id；如新增专门排版，在 `isEnabledFor(DEBUG)` 判断内进行。补 DEBUG 开启时含两种输入与响应、调用失败仍有输入、关闭时不执行专门排版的日志测试，无需新增日志框架。

### C1-05｜中 / P2｜新旧对照用例把“最近三周”固化成 22 天，且未完成约定的对照输出

- **位置**：`nl2sql/src/tests/db_deepagent/unit/test_time_resolver_compare.py:43`、`:23`、`:93`。
- **证据**：固定今天为 `2026-10-13` 时，当前 mock 是 `[2026-09-22, 2026-10-14)`，实际相差 **22 天**。doc 16 规定含今天共 21 天，正确下界应是 `2026-09-23`。测试把同一个 `mock_bounds` 同时作为响应和断言，故错误数据仍能通过。此外仅有四题，没有覆盖 §9.1 的小时窗口、单边、context 年份、无效日期、澄清等题例；文件没有输出对照表，文件头说明 mock 不能代替 §9.3 要求的逐行来源标记。
- **影响**：测试夹具与正式口径相悖；现有四个绿色对照用例仅说明指定 JSON 能被接收，不能作为完整题集验收。
- **最小修改建议**：修正该下界，断言窗口为 21 天；按 §9.1 补齐固定时钟题例及所需 context，利用现有参数化测试打印简洁对照行，分别标明旧版规则结果和新版 `mock` 来源。不需要接真实 flash，也不应据 mock 声称模型语义已验收。

### C1-06｜中 / P2｜原生 DashScope 分支静默丢弃必填超时配置

- **位置**：`nl2sql/src/modules/db_deepagent/services/time_resolver.py:295`。
- **证据**：配置允许引用已有原生 `dashscope` profile；当 `overrides={}`、`request_timeout_seconds=15` 时，该分支不向工厂传递 `timeout`。本轮替换配置读取与工厂、调用真实 `build_time_resolver()`，记录到工厂覆盖参数为 `{}`。现有工厂本身明确拒绝原生 DashScope 的 timeout，新增分支却绕过了这一拒绝。当前默认 `dashscope_openai` 配置不受此项影响。
- **影响**：非默认但合法的配置表面要求 15 秒，实际不生效，与 doc 16 §6 的显式映射约定不符。
- **最小修改建议**：统一映射 timeout，让已有工厂拒绝不支持的 provider；或在该分支明确报出指向 `time_resolution` 的配置错误。无需支持新的超时机制、兼容层或配置开关。增加该 provider 的离线拒绝测试。

## §9.2 测试覆盖核对

| 清单项 | 当前证据与缺口 |
| --- | --- |
| 1：仅原文调用、Human 输入、来源错误 | 已有 ToolNode 仅原文调用和来源错误；输入字段有断言。北京时间测试原时钟已是 `+08:00`，没有验证从其他时区跨日转换。 |
| 2：七种结果、失败不记账、澄清双 null | 服务结果码覆盖主要分支，双 null 澄清用例有效；大部分失败只测服务结果，未通过 ToolNode 断言错误信封及账本无更新。C1-02 的输出格式反例缺失。 |
| 3：ToolNode 账本与 grain、可见参数装配 | 成功 ToolNode 核对了 ID/run_id 和返回边界；未逐项比较返回值与账本，未断言非默认 grain 原样落账。装配 schema 测试存在且通过，但不覆盖 C1-01 的真实服务容器成功构造。 |
| 4：覆盖参数、timeout、缺配置段 | 工厂替身核对 timeout/max_retries/response_format；没有断言 enable_thinking/temperature 的最终工厂参数，也未验证真实客户端 timeout。默认 flash profile 确实提供 temperature=0、enable_thinking=false。缺 profile 测试不等同于删除整个 time_resolution 段的错误定位测试，后者未见。 |
| 5：observer 用途、计数、日志隔离 | 新测试直接给假模型手工设置 metadata 后调用，验证了总数和无 anomaly；未走工具→解析器回调转发，未带 usage 验证 Token 汇总。现有 doc 13 日志测试只覆盖主 Agent/摘要，未专测日期调用隔离。本轮额外离线调用真实解析器加 observer，得到 total=1、summary=0、anomalies=[]。 |
| 6：DEBUG 输入输出与关闭行为 | 未见日期服务日志测试；实际 System 遗漏见 C1-04。 |
| 7：date 粒度 guard 回归 | 既有 guard 测试通过，但使用手工证据；尚无新工具返回证据直接供 guard 校验的联通用例。应复用现有假数据库，补一条日粒度 ToolNode→账本→guard 用例即可。 |

澄清先于边界校验、grain 枚举校验而不反推、ctx.now 转北京时间、sql_hint 原格式、旧解析器退役及旧工厂直接绑定、TimeEvidence/reuse_evidence 字段衔接均未发现额外阻断问题；不建议改造已挂起的 guard。

## 本轮验证与边界

- `test_time_resolver_llm.py`、`test_time_resolver_compare.py`、`test_guard_tools.py`、`test_model_observer.py`：**54 passed**。
- `test_assembly.py`、`test_config.py`、`test_sql_guard.py`、旧解析器 `test_time_resolver.py`：**144 passed**。首次运行因本机 SOCKS 代理环境缺少 socksio 而有 6 项客户端构造失败；仅在第二次测试进程移除 HTTP/HTTPS/ALL_PROXY（含小写）后全部通过，未修改环境文件或安装依赖。
- 上述 pytest 均设置 `PYTHONDONTWRITEBYTECODE=1`、禁用 cacheprovider；Ruff 使用 `--no-cache --select F821`，检出 C1-01。全假依赖复现了服务装配 NameError、输出格式问题、旧技能参数拒绝、日志遗漏和超时参数丢弃。
- 未重跑所述 1115/288 项全量回归；已有回归通过不能代替本报告指出的路径覆盖。本任务禁止真实调用，故 §9.3 的 flash 语义及带时刻 SQL/guard 实测仍待后续授权环境验收，本轮不将其表述为已通过。
