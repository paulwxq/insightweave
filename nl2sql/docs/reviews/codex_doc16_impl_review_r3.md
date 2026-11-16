# doc 16 实施复审，第 3 / 4 轮

总体结论：**APPROVE_WITH_NITS（离线实施复审通过，保留非阻断建议）**。

第二轮的 **C2-01、C2-02 均关闭**，连带关闭 C1-05；本轮未发现新的 P1/P2 阻断项。日志讨论的原始误判问题已经解决，但“完整假响应精确断言”和“finally 关闭容器”两处描述比实际代码更强，具体差异见下文，不影响本轮通过。

审查基线：`feature/multi-source-knowledge-isolation`，HEAD `6a98f4d` 及当前工作区。本轮仅创建本报告，没有修改业务代码、测试或配置，没有提交，没有调用真实模型或真实数据库；仍保留真实 flash 与带时刻 SQL/guard 的授权环境验收边界。

## 第二轮问题复核

### C2-01｜关闭：装配测试已隔离外部设施

- `test_time_resolver_llm.py:260` 起确实替换了 `BusinessDbService`、`DbEmbeddingClient`、`DbMilvusRetrieval`、`JoinGraphService`、`_neo4j_manager`、`ResultStore`，假连接对象的 `open/close` 均无操作；配置中的 workspace root 改用 `tmp_path`。
- 保留真实 `build_services`、真实 `build_time_resolver` 和真实提示词加载，仅将 `get_llm` 替换为假模型；成功断言解析器类型、profile 和 timeout 接线。
- 本轮在测试外层预置一个构造即抛异常的 `BusinessDbService` 哨兵，再调用该测试：测试内部正确覆盖哨兵并通过。另对定向回归进程拦截 `socket.socket.connect/connect_ex` 和 `socket.create_connection`，包含该装配测试的 222 项回归全部通过；独立哨兵/对照检查的外部 socket 连接尝试为 0。
- `finally` 实际只调用 `services.business_db.close()`，并非 `services.close()`。由于所有连接资源均为无操作假对象，原问题中的真实连接与资源泄漏风险已消除，此差异不再维持 P2，见 C3-02。

### C2-02 / C1-05｜关闭：十四题新旧同题对照已补齐

- `_CASES` 已覆盖 §9.1 全部十四个题面，逐题携带可选 context、旧版参数与预期、mock 响应、新版预期及成功边界。
- 本轮逐题执行真实旧解析器和注入 mock 的新解析器，全部匹配预期；输出十四行均标注 `新(mock)`，context 年份场景单独显示上下文。
- 新版结果为 **11 项 ok、1 项 time_parse_invalid_output、2 项 ambiguous_time_reference**。旧版仅“上个月”和“2026年9月份”成功，其余十二项为 `time_params_mismatch`；包括两种裸月与无效闰日，修正后的注释与本轮实测一致。
- 测试明确沿用公共 fixture 的 `2026-10-13T12:00:00+08:00`，并非文档示例的 `2026-11-15T14:00:00`。按该固定时钟核算，最近三周为 `[2026-09-23, 2026-10-14)`，恰为 21 天；72 小时为 `[2026-10-10T12:00:00, 2026-10-13T12:00:00)`；上周一到周五为 `[2026-10-05, 2026-10-10)`，均正确。
- 两个裸月用例分别得到 2025 年和 2026 年 9 月。它们验证指定响应的解析及 context 入参接线，不证明真实模型会按 context 选择年份；文件头已正确声明 mock 的验收边界。

## 日志讨论复核与非阻断建议

### C3-01｜低 / P3｜响应日志断言已避免 INFO 误判，但尚未核对完整响应

- **位置**：`nl2sql/src/tests/db_deepagent/unit/test_time_resolver_llm.py:418`。
- 当前先筛选包含“时间解析模型输出”的记录，再检查其中含 `2026-11-13`，已修复第二轮指出的 INFO 摘要误判；新增 `test_model_failure_still_logs_inputs` 也通过，超时返回 `time_parse_model_error` 时输入 System 标记仍在日志中。
- 本轮仅在内存中替换日志调用做反证：省略模型输出 DEBUG 记录会使现有测试失败；但把该记录的响应正文截成单个 `2026-11-13`，现有测试仍通过。因此“含完整假响应”的说法尚不能由该断言支持。
- **建议**：将 `_ok_json(...)` 保存为预期响应，直接断言该完整字符串出现在筛选后的输出记录中，或提取日志正文后比较 JSON。当前业务实现确实记录完整 `raw`，故维持非阻断建议。

### C3-02｜低 / P3｜finally 建议走服务容器的统一关闭入口

- **位置**：`nl2sql/src/tests/db_deepagent/unit/test_time_resolver_llm.py:284`。
- 当前只关闭假 `business_db`，不会覆盖 `DbAgentServices.close()` 的 join_graph → milvus → business_db 统一释放路径；与本次说明中的“finally 关闭容器”有差异。
- **建议**：改为 `services.close()`，使测试与容器正常使用方式一致。当前这几个对象均为无操作假对象，没有真实资源需要回收，不影响 C2-01 关闭，也无需为此增加隔离框架或测试。

## 本轮验证证据

- 使用项目 `nl2sql/.venv`，对以下八个文件定向回归，**222 passed**：`test_time_resolver_llm.py`、`test_time_resolver_compare.py`、`test_guard_tools.py`、`test_model_observer.py`、`test_assembly.py`、`test_config.py`、`test_sql_guard.py`、旧解析器 `test_time_resolver.py`。此次未排除装配测试；其中新解析器 27 项、对照 14 项均通过。
- pytest 通过 `.venv/bin/python` 内联脚本调用 `pytest.main`，参数为 `-q -o addopts= -p no:cacheprovider` 及上述文件；执行期间将 socket 连接入口替换为抛出断言的哨兵。设置 `PYTHONDONTWRITEBYTECODE=1`，仅在测试进程移除 HTTP/HTTPS/ALL_PROXY（含小写），没有修改环境文件。
- 独立调用装配测试验证 BusinessDbService 哨兵、逐题打印十四行对照，以及两种内存日志变更反证，均得到上述预期结果；没有修改源码。日志反证使用包含“前天”的用户上下文，先确认未替换时测试通过。
- 在 **`nl2sql` 工作目录**运行 `.venv/bin/ruff check --no-cache src/tests/db_deepagent/unit/test_time_resolver_llm.py src/tests/db_deepagent/unit/test_time_resolver_compare.py`：**All checks passed**；同两文件 `black --check`：**2 files would be left unchanged**。
- `git diff --check` 通过。没有重跑所述全量 **1139 passed / 4 skipped**，该数字仍属于实施方回归记录，本报告的独立复测范围是上述 222 项。

## 验收边界

第一轮已修复项的第二轮结论维持，本轮两个 P2 已关闭，可以结束离线实施整改。第二轮已记录的其他非阻断覆盖边界仍不应表述为全部补齐；本轮没有新增这些要求。

真实 flash 的“大前天”“截至今天中午12点”实际输入输出、主 Agent 采用的范围、最终 SQL 与 guard 结果，仍待授权环境验证。本次通过仅指离线实施复审，不代表真实模型语义或带时刻 SQL/guard 已验收通过。
