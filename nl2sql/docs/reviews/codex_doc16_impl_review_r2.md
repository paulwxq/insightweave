# doc 16 实施复审，第 2 / 4 轮

总体结论：**REQUEST_CHANGES**。

基线仍为分支 `feature/multi-source-knowledge-isolation`、HEAD `6a98f4d` 及当前工作区。第一轮两个 P1 业务缺陷已修复；本轮仍有两个测试验收问题，其中 C1-05 的题集覆盖尚未完成。本轮只创建本报告，未修改业务代码、测试或配置，未提交，未调用真实模型或真实数据库。

## 第一轮问题复核

| 编号 | 复核结果 |
| --- | --- |
| C1-01 | **业务修复通过**：`build_time_resolver(cfg)` 已接对。本轮将全部外部服务替换为假对象，执行真实 `build_services` 和真实日期解析器构造，确认配置对象、解析器类型及 profile 正确。新增测试的隔离问题见 C2-01。 |
| C1-02 | **通过**：格式白名单先于日期解析，六类非法格式被拒；真实 ToolNode 验证错误码及账本无更新，合法日期/日期时间混用及澄清双 null 用例仍通过。 |
| C1-03 | **通过**：规划技能第 37 行已改为逐字传 `source_text`，未见残留旧 `kind="open_range"` 调用示例。 |
| C1-04 | **通过**：实际 System/Human 在调用前记录，原始响应在调用后记录，两处都有 DEBUG 判断；正常 DEBUG 开关测试通过，失败路径也已能保留调用输入。 |
| C1-05 | **部分通过，未关闭**：21 天下界与窗口长度断言已修正，输出行已标明新版 mock；仍未补齐 §9.1 题集，见 C2-02。 |
| C1-06 | **通过**：timeout 无条件映射，原生 DashScope 经真实工厂明确拒绝；测试已剥离 max_retries，断言确实命中 timeout。 |

日粒度解析器→真实 guard 的新增用例已通过；它直接使用解析器返回的证据，尚未经过 ToolNode 账本，但不影响此次对 guard 接收新证据格式的验证。

## 编号问题清单

### C2-01｜中 / P2｜新增“离线”装配单测仍连接真实业务库和检索设施

- **位置**：`nl2sql/src/tests/db_deepagent/unit/test_time_resolver_llm.py:240`、`:241`。
- **证据**：该测试只 monkeypatch `llm_factory.get_llm`，随后直接使用真实配置调用 `build_services()`，没有替换 `BusinessDbService`、Milvus 或 Neo4j 等外部依赖，也没有使用隔离工作目录。实际构造路径在 `services/__init__.py:147` 调用 `business_db.open()`；`services/business_db.py:181` 随即打开真实连接池并查询数据库。`verify_embedding=False` 仅跳过 embedding 一致性探针，随后 `DbMilvusRetrieval` 构造仍会连接 Milvus。
- **本轮安全复现**：直接调用该测试方法，在外部将 `BusinessDbService` 替换为一个 `open()` 立即抛哨兵异常的对象，得到 `OfflineBlocked: REAL business_db.open reached by new unit test; network prevented`。该哨兵阻断发生在任何数据库连接之前；本轮没有直接运行此测试的未隔离版本。
- **影响**：默认 unit 测试会依赖真实数据库、Milvus 和 Neo4j 的可用性；数据库在线时还会查询真实设施。成功返回的容器没有 `close()`，也会遗留连接资源。因此它不能作为所称“离线”装配回归证据，且不能按本任务边界直接执行。
- **最小修改建议**：在此测试内沿用现有 monkeypatch，把外部连接服务与会写目录的 ResultStore 换成假对象，保留真实 `build_services`、`build_time_resolver`，继续用假 `get_llm`；工作目录使用 `tmp_path`。成功断言配置接线与解析器/profile 后，在 finally 中关闭容器。无需新增隔离框架或全局 autouse。

### C2-02｜中 / P2｜C1-05 的完整对照题集要求仍未落实

- **位置**：`nl2sql/src/tests/db_deepagent/unit/test_time_resolver_compare.py:25`、`:105`。
- **证据**：当前 `_CASES` 仍只有六行，且全部注入 `status="ok"` 响应；新调用一律传 `context="年份 2026（如需）"`。doc 16 §9.1 有十四题，§9.3 明确要求对该题集运行新旧对照；第一轮 C1-05 也已要求补齐。现有对照仍缺“上个月”“过去72小时”“从大前天起”“上周一到周五”、context 为 2025 年的裸月、无 context 的默认年份、无效日期，以及两类澄清题。另一个文件存在通用无效日期/澄清单测，不能替代本文件承诺的新旧同题对照。
- **影响**：修正 21 天数据和增加两题解决了原有错误，但没有完成约定的对照验收；尤其没有区分“裸月随 context”与“默认 reference_now 年”，也没有展示新版错误/澄清状态的对照结果。
- **最小修改建议**：扩充现有参数化数据即可：逐题提供可选 context、mock 响应及预期状态，覆盖 §9.1 的题面；可在局部替换为文档固定时钟，不改变公共 fixture。输出行按实际结果标识 ok/invalid_output/needs_clarification 对应信封码及 mock 来源，无需接真实 flash 或新建测试框架。

## 测试边界与非阻断说明

§9.2 的主要正常/错误路径已有覆盖，输出格式与 guard 接收新证据的关键缺口本轮已补。第一轮列出的其他覆盖缺口仍不能宣称全部完成，例如非北京时间跨日转换、缺整个 time_resolution 段的定位、observer 经工具转发并携带 Token usage 的验证；本轮未据此增加新的业务缺陷。

【讨论】日志测试可以更精确：`TestDebugLogging.test_debug_logs_system_human_response` 用 `2026-11-13` 判断“响应已记”，但 INFO 摘要本身也含该日期，删除模型输出 DEBUG 后该断言仍可能通过。建议直接断言“时间解析模型输出”记录含完整假响应，顺手补一例超时仍保留输入；当前业务日志实现已正确，此建议不单独阻断。

## 本轮验证证据

- 使用项目 `.venv`，运行 `test_time_resolver_llm.py`、`test_time_resolver_compare.py`、`test_guard_tools.py`、`test_model_observer.py`、`test_assembly.py`、`test_config.py`、`test_sql_guard.py`、旧解析器 `test_time_resolver.py`，以 `-k 'not TestBuildServicesWiring'` 排除 C2-01：**212 passed / 1 deselected**。
- 测试进程设置 `PYTHONDONTWRITEBYTECODE=1`、禁用 cacheprovider，并移除 HTTP/HTTPS/ALL_PROXY（含小写）以避免首轮已确认的 socksio 环境问题；没有修改环境文件。
- 对实施涉及的七个业务 Python 文件运行 `ruff check --no-cache --select F821`：**All checks passed**。全假外部依赖下，真实 `build_services` 成功构造与真实日期解析器/profile 接线另行验证通过；C2-01 用前述哨兵验证，未发起真实连接。
- 未重跑所述 1130 项全量回归；本轮发现其中新增单测需要外部设施，因此不能将该全量数字复述为“全离线通过”。真实 flash 语义与带时刻 SQL/guard 实测仍待授权环境验收，本轮保持该边界。
