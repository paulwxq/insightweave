# 27. `rag_load` 显式全量触发与失败语义反转设计

> 状态：已实施（2026-09-19，C1=98225a6 / C2=01918ed，§6 验收见提交记录）；
> **修订**（2026-09-20，docs/update/26 §6.2）：`_run_single_load()` 签名扩展——
> 新增 `expected_clean_target`（pipeline 在 clean 前执行运行期目标一致性复核，
> 独立 CLI 传 None 不比较）、`emit_clean_warning`（pipeline 调用时抑制重复的
> 通用清空警告，由一次性清理计划承担安全提示）、结果新增可选 `metrics`
> （dim_value 零加载可观测性）。核心 `success`/`execution_time`/`message`
> 契约、Abort 归属与统一耗时计量保持不变
> 日期：2026-09-19
> 前置：e2bb425 / eb757be（rag_load 重构与默认四类，doc 24 及其修订、doc 25）

## 1. 背景与定案

**现状**（eb757be 之后）：

- 裸 `metaweave rag_load` 默认依序加载四类（sql → table_schema → dim_value → cql）
- 失败语义默认"记录并继续"，`--fail-fast` 可改为首错即停

**问题**：

1. **与"无隐式动作"原则不一致**（主论据）：doc 25 已确立裸
   `metaweave metadata` 直接 UsageError，而裸 `rag_load` 静默执行四类加载，
   同一 CLI 两种契约并存。**放大器**：误触若再带上习惯性的 `--clean`，
   将清空 3 个 Milvus collection + Neo4j 整库（不带 `--clean` 时 cql 走
   MERGE 幂等重载，主要危害是隐式全量与 Milvus 重复写入，而非清空）。
2. **失败默认方向不合理**：Unix 惯例是失败即停；"出错继续"才是值得显式
   声明的特例。旗标应标记例外（`--continue-on-error`）而非常态（`--fail-fast`）。
   且 `--clean` 模式下"继续"意味着出错后还去清空更多数据库。

**定案（用户确认）**：

1. 全量加载必须显式：`--type all`；裸调用（无 `--type`）→ `click.UsageError`
2. 失败语义反转：**默认 fail-fast**；`--fail-fast` 整删不留别名，
   新增 `--continue-on-error` 作为继续模式的显式开关
3. **重复传入防护**：`--step` / `--type` 两个动作选择选项每次调用仅允许
   传入一次，重复 → 中文 UsageError（原 click"后者静默覆盖"语义废除）。
   与前两项同批实施的原因：同属"显式意图"主题、共用 multiple=True 机制，
   单独立稿成本高于收益（用户指示并入本稿）

## 2. 命令契约

```
Usage: metaweave rag_load [OPTIONS]

  -t, --type [sql|table_schema|dim_value|cql|all]   语义必选：缺省 → 中文 UsageError
      （click 层非 required，由命令体检查保证文案，见 §3）
      all = 依序加载四类：sql → table_schema → dim_value → cql
  -c, --config PATH      默认 configs/metadata_config.yaml
  --clean                每类加载前先清空目标（前三类清对应 Milvus collection，
                         cql 清空 Neo4j 整库）；部分失败后果与补跑提示不变
  --continue-on-error    单类失败后继续加载其余类型（默认首个失败即停止）；
                         仅 all 模式有意义，指定单类型时忽略并告警
  --debug                调试模式
```

**行为矩阵**（成功/失败双列，退出码按结果明确区分）：

| 调用 | 行为 | 全部成功 | 任一类型失败 |
|---|---|---|---|
| `rag_load`（无 --type） | 参数检查即拒 | — | —（UsageError："必须指定 --type（sql/table_schema/dim_value/cql/all）"，**恒 exit 2**，与成败无关） |
| `rag_load --type sql` | 单类加载 | **exit 0** | 错误打印 + exit 1，无汇总清单（doc 24 契约） |
| `rag_load --type all` | 四类依序（sql→table_schema→dim_value→cql） | **exit 0** | **首个失败即停止**，后续 ⏭️ 未执行，汇总后 exit 1（`--clean` 时失败与跳过类型均附补跑提示，见 §3） |
| `rag_load --type all --continue-on-error` | 四类依序 | **exit 0** | 失败记录并继续加载其余类型，汇总后 exit 1（`--clean` 时各失败类型附补跑提示；与第 3 行同源：failed 行统一追加，与模式无关） |
| `rag_load --type sql --continue-on-error` | 同 `--type sql`（单类型无"后续类型"，忽略该旗标并打印告警） | **exit 0** | 同 `--type sql` |

退出码契约不变：任一类型失败（任何模式）→ 退出码非 0；
`--clean` 部分失败后果提示与补跑提示（doc 24 §3.3）不变。

**重复传入防护（评审新增，适用两个动作选择选项）**：
`metadata --step`（值域 ddl/json/cql/md/rel）与 `rag_load --type`
（值域 sql/table_schema/dim_value/cql/all）**每次调用仅允许传入一次**，
重复传入 → 中文 UsageError（exit 2），不再沿用 click 默认的
"后者静默覆盖前者"。其余选项（--config 等）维持 click 默认。

## 3. 实现（`metaweave/cli/loader_cli.py`）

- Choice 增加 `"all"`；`--type` 保持可选（default=None），命令体开头检查：
  `if not load_type: raise click.UsageError("必须指定 --type（sql/table_schema/dim_value/cql/all）")`
  ——不用 `required=True`（click 缺参报错为英文 "Missing option"），
  采用与 metadata 裸调用检查（doc 25 §2.2）一致的模式。
  **中文文案覆盖边界（评审修订）**：仅"完全未传 `--type`"得到中文提示；
  `--type`（缺值）与 `--type foo`（非法值）仍为 click 英文报错
  （"requires an argument" / "Invalid value"），属容忍边界，不额外中文化
- **枚举单一事实源（评审修订）**：`RAG_LOAD_TYPES` 为唯一类型枚举
  （现状它与 `DEFAULT_RAG_LOAD_TYPES` 内容完全重复——四类默认定案后的历史残留）；
  `--type` 用 `click.Choice(RAG_LOAD_TYPES + ("all",), ...)`，
  命令体展开：`types = list(RAG_LOAD_TYPES) if load_type == "all" else [load_type]`；
  **删除 `DEFAULT_RAG_LOAD_TYPES`**，测试中 `import DEFAULT_RAG_LOAD_TYPES`
  的用例（2 处构造 loader 字典）同步改为 `RAG_LOAD_TYPES`
- 删除 `--fail-fast` 选项及其与 `--type` 的互斥告警逻辑；
  新增 `--continue-on-error`（默认 False），循环退出条件改为
  `if not r["success"] and not continue_on_error: break`
- `--continue-on-error` + 单类型：打印一行 warning 后忽略（沿用原
  `--type + --fail-fast` 的处理模式）
- **函数签名**：`rag_load_command(..., fail_fast: bool, ...)` →
  `continue_on_error: bool`（B2 评审补充）
- **常量注释**：`RAG_LOAD_TYPES` 定义处注释更新为"唯一类型枚举源
  （Choice 在此基础上追加 all）"；`DEFAULT_RAG_LOAD_TYPES` 的注释随常量
  一并删除（B1 评审补充）
- **跳过类型的补跑提示（B3 评审拍板）**：`--clean` 下汇总中的 ⏭️ 未执行
  行同样附加 `补跑: metaweave rag_load --type <t> --clean`——被跳过的
  collection 仍持有旧数据（未清理未更新），与"清空后失败"同样偏离
  全量重载意图；无 `--clean` 时跳过行保持朴素输出。`--clean` 帮助文本
  "汇总将给出补跑提示"据此完全成立，不需改写
- **helper 白名单清理（B4，低优先级）**：`_run_single_load` 中
  clean-able 集合 `{"cql", "dim", "dim_value", "table_schema", "sql"}`
  移除已删别名 `"dim"`（Choice 已拒绝该值，集合内属死条目）；
  此为 helper 内部一行改动，不违背"helper 契约零改动"
- docstring、`--type`/新旗标帮助文本、模块头注释同步
- helper `_run_single_load` 契约（返回 dict、Abort 由命令函数发出、
  perf_counter 统一计时）**零改动**

### 3.1 重复传入防护（评审新增）

- 实现：两个选项改为 `multiple=True`，命令体开头校验长度——
  `if len(step) > 1: raise click.UsageError("--step 仅允许传入一次，检测到 N 次: a, b")`
  （`--type` 同款）；随后 `step = step[0] if step else None` 归一化，
  下游逻辑零改动
- 纯 click 原生机制，无需自定义 Option 子类；短别名计数仅涉及
  `-t`（rag_load --type）——**metadata --step 无短别名**（`-s` 属
  `--schemas`）；每次出现的值仍逐个经 Choice 校验（非法值先于重复检查
  报错，两者皆 exit 2）
- **归一化位置（评审修订，防 tuple 泄漏）**：`step = step[0] if step else None`
  必须是命令体**首行**——早于裸调用检查（metadata_cli 当前态 :494）、
  `set_current_step`（:501）、`_warn_out_of_scope_params` 及一切把 step
  当字符串用的分支（:828 `step == "cql"`、:948 `step.lower()`、
  :1031 `step=step`；行号为当前稿锚点，随实施漂移以语义为准）；
  `--type` 同款。注：`if not step` / `if step:` 对 `()`/`('ddl',)`
  天然兼容，风险仅在字符串方法调用点
- **帮助文本（评审修订）**：click 的 multiple=True **不会**自动在 help
  中显示"仅一次"约束，两个选项的 help 须显式写入。建议措辞——
  `--type`："加载类型（仅允许传入一次）：sql / table_schema / dim_value /
  cql；all = 依序加载全部四类"；`--step`："执行步骤（仅允许传入一次）：
  ddl/json/cql/md/rel"
- 覆盖面：仅 `--step` / `--type` 两个动作选择选项；`--config`、
  `--clean` 等**不扩散**（维持 click 默认，双传危害极低）

### 3.2 `_run_single_load()` 签名扩展（docs/update/26 §6.2 修订）

pipeline 编排引入后，helper 内部签名扩展为：

```python
_run_single_load(
    load_type, config_path, clean, debug,
    expected_clean_target=None,   # 仅 pipeline 传入；独立 CLI 传 None 不比较
    emit_clean_warning=True,      # pipeline 传 False 抑制重复通用警告
) -> dict                          # 成功时可选附带 metrics 字段
```

- `expected_clean_target` / `emit_clean_warning` 均为 **pipeline 内部契约**，
  不是 CLI 或 YAML 参数；独立 `rag_load`/`metadata` CLI 行为不变；
- `metrics`（`tables_total`/`tables_processed`/`tables_skipped`/`records_loaded`）
  仅增强可观测性（dim_value 零加载汇总 warning），不改变 `success`、
  退出码或 `--continue-on-error` 语义；
- loader 配置从主配置快照**深拷贝派生**后补充 `input_file` 等运行时值，
  不再就地修改 `full_config["loaders"]` 引用

## 4. 测试设计（`tests/unit/test_rag_load_cli.py` 就地修订，评审修订：逐用例清单）

**改写**（原用例改名/改参，断言意图保留）：

| # | 现存用例 | 修改 | 新断言要点 |
|---|---|---|---|
| 1 | `test_no_type_expands_four_types_in_order` | 改名 `..._type_all_expands...`，参数补 `--type all` | 顺序 `sql→table_schema→dim_value→cql`（不变） |
| 2 | `test_fail_fast_stops_and_marks_skipped` | 改名 `test_default_stop_on_first_failure_marks_skipped`（新名不含 fail_fast 字样，满足 §6-6），去 `--fail-fast`、补 `--type all` | create 1 次、⏭️×3、exit 1（原断言即新默认行为） |
| 3 | `test_fail_fast_with_type_ignored_with_warning` | 改名 `test_continue_on_error_single_type_ignored`（同上，满足 §6-6），参数改 `--type sql --continue-on-error`，**docstring 同步改写**为"--type + --continue-on-error 不报错，忽略并告警"（原 :218 docstring 含 `--fail-fast` 字样，不改则 §6-6 判据仍命中；其 §3.2 引用一并更新为本稿 §3） | 忽略告警 + 正常执行 + exit 0 |
| 4 | `test_default_continue_after_failure` | 改名 `..._continue_on_error_...`，补 `--type all --continue-on-error` | create 4 次、❌sql/✅ 其余三、exit 1（断言不变） |
| 5 | `test_load_returns_failure_recorded_not_blocking` | 补 `--type all --continue-on-error` | count 4、中位失败原因在汇总（断言不变） |
| 6 | `test_helper_exception_contract`（参数化×3） | 补 `--type all --continue-on-error` | count 4、无 traceback（断言不变） |
| 7 | `test_clean_passed_to_every_load` | 补 `--type all` | clean=True 传递（断言不变） |
| 8 | `test_execution_time_from_helper_perf_counter` | 补 `--type all --continue-on-error` | perf 序列与 1.0/2.0/3.0/4.0s 断言不变 |
| 9 | `test_summary_with_clean_gives_rerun_hint` | 补 `--type all`（改走 fail-fast 路径：首败即停、hint 仍在） | 补跑提示断言不变 |

**新增**：

- `test_bare_invocation_requires_type`：裸调用 → exit 2 + 输出含中文
  "必须指定 --type"（文案断言）
- `test_all_token_not_passed_to_factory`：`--type all` 展开后，工厂
  `create` 的每次调用参数均不为 `"all"`（防止展开遗漏、all 透传到
  LoaderFactory 才报错）
- `test_all_success_exit_code_zero`：`--type all` 与
  `--type all --continue-on-error` 全部成功时均 exit 0（对应 §6-3）
- `test_skipped_rows_include_rerun_hint_under_clean`：`--type all
  --clean` + 首类失败 → 3 条 ⏭️ 行均含
  `⏭️ X（未执行） ｜ 补跑: metaweave rag_load --type X --clean`（B3 拍板的直接断言）
- `test_skipped_rows_have_no_hint_without_clean`：同 mock 不带 `--clean` →
  输出中"补跑:"出现次数为 0（对照）
- 重复传入防护（§3.1）：`--type sql --type cql` → exit 2 +
  "仅允许传入一次"（`tests/unit/test_rag_load_cli.py`）；
  `--step ddl --step json` → exit 2 同款中文报错
  （`tests/unit/test_cli_no_orchestration.py`）；
  各含一条单次传入正常的对照
- `test_summary_rerun_hint_under_continue`：`--type all --continue-on-error --clean`
  + 中位失败 → 补跑提示仍输出（补回第 9 条改走 fail-fast 后丢失的原覆盖点）

**同步改名**：`test_rag_load_cli.py:14` 的
`from metaweave.cli.loader_cli import DEFAULT_RAG_LOAD_TYPES` →
`RAG_LOAD_TYPES`（仅改使用处会留 import 报错；使用处即上表 :77/:100
两处构造 loader 字典的用例）。

**零改动**（本就带 `--type`，断言不受默认反转影响）：
`test_type_sql_single_load`、`test_type_cql_single_load`、
`test_sql_input_file_auto_inference`、`test_invalid_type_rejected_by_choice`
（后者已循环断言 `dim`/`content_md` 被拒，无需新增）；
其余用例（含 `TestOldCommandDeath` 两条顶层消亡/注册用例）零改动。

## 5. 文档同步

- `docs/100_执行命令完整参考.md`：rag_load 章节（第三节）参数表、行为矩阵、
  示例全部改写；命令体系总览中 rag_load 一行同步
- `CLAUDE.md` / `AGENTS.md`：loader_cli 条目与"默认加载四类"表述改为
  `--type all` 显式触发；README 加载示例同步
- **metadata 侧契约变更同步**：`--step` 帮助文本加"仅允许传入一次，
  重复传入报错"（对外可见行为从静默覆盖 → exit 2）；docs/100 公共参数表
  `--step` 行同步备注
- `docs/update/24`：状态头追加修订记录（默认四类触发方式与失败语义按 doc 27 反转）

## 6. 验收标准

1. **裸调用**：`metaweave rag_load` → exit 2 + 输出含"必须指定 --type"
2. **展开顺序**：单测断言 `--type all` 的 create 调用序列恰为
   `["sql", "table_schema", "dim_value", "cql"]`
3. **全成功退出码**：单测断言 `--type all` 与
   `--type all --continue-on-error` 在全部成功时均 exit 0
4. **真实冒烟**：`metaweave rag_load --type all -c configs/metadata_config.yaml
   --clean` → 四类依序各自先清后载、退出码 0。前置产物清单（缺任一必然
   非 0，冒烟前逐一核验）：`output/sql-rag/qs_{database.database}_pair.json`
   （`database.database` 解析自配置 + `.env` 的 `DB_NAME`，如 orders 环境
   即 `qs_orders_pair.json`）、
   `output/md/` 与 `output/json/` 非空、`output/dim_config/dim_tables.yaml`
   存在（embedding_col 未填时按表跳过；是否影响退出码**以 loader 返回
   success=True 为准**——跳过不计失败，实施冒烟时确认）、
   `output/cql/import_all.*.cypher` 恰好唯一匹配、Milvus/Neo4j/PG 可达
5. **单测全绿**；全量回归通过，**已知预存在问题仅限以下两项**：
   ① `tests/test_dim_value_search.py:24`（`import src.utils.env` →
   ModuleNotFoundError: No module named 'src'）；
   ② `tests/test_sql_rag_training_scenario.py:133`（调用
   `SQLValidator._extract_relevant_relationships`，`validator.py` 无该方法）
6. **--fail-fast 零残留**（可执行判据，评审修订）：
   `grep -rn --include=*.py -e "--fail-fast" -e "fail_fast" metaweave/ tests/`
   零命中（`--include` 排除 `__pycache__` 二进制误报；`--fail-fast` 带前导
   短横、`fail_fast` 下划线标识符，二者合指"选项名/参数名/测试名"残留，
   不匹配无前导短横的散文用语）；
   **语义用语白名单（非残留、不改写）**：
   `metaweave/cli/metadata_cli.py:597`（`_abort_step` docstring"以 fail-fast
   方式终止"）、`metaweave/core/relationships/candidate_generator.py:319`
   （剪枝顺序注释）——流程语义用语、非选项引用，且 core 层不在本轮范围；
   另 `metaweave rag_load --help` 输出不含 fail-fast
7. **重复传入防护**：`metaweave metadata --step ddl --step json` 与
   `metaweave rag_load --type sql --type cql` 均 exit 2 +
   中文"仅允许传入一次"报错
8. **文档零残留**（可执行判据）：
   `grep -rn -i -e "fail-fast" README.md AGENTS.md CLAUDE.md
   docs/100_执行命令完整参考.md docs/迁移后运行指南.md` 零命中
   （白名单：docs/update/ 归档记录与本设计文档本身不在此列）

## 7. 实施顺序与提交拆分（评审固化）

按改动归属拆两个提交，便于独立回滚；每提交内 TDD（先改测试见红 → 改实现转绿 → 手测）：

| 提交 | 范围 | 文件 | 内容 |
|---|---|---|---|
| C1 | rag_load 主体 | `loader_cli.py`、`tests/unit/test_rag_load_cli.py` | Choice+all、删 --fail-fast、加 --continue-on-error、常量合并为 RAG_LOAD_TYPES（删 DEFAULT）、⏭️ 行补跑提示、helper 白名单删 dim、help/docstring/注释同步、测试按 §4 清单改写+新增 |
| C2 | metadata --step 防护 | `metadata_cli.py`、`tests/unit/test_cli_no_orchestration.py` | --step 改 multiple=True + 首行归一化、help 加"仅允许传入一次"、重复传入 exit 2 用例 + 单次对照 |

实施前置：清理孤儿 `__pycache__`（上轮删除 pipeline_cli/test 文件遗留 pyc，
避免 grep/IDE 读到旧字节码）：
`find . -path ./.venv -prune -o -name "__pycache__" -type d -print0 | xargs -0 rm -rf`

收尾验证（两提交完成后）：§6-6/6-7/6-8 三条 grep 判据 → `rag_load --help` 零
fail-fast → 裸调用/重复传入/单次对照手测 → `pytest tests/unit -q`
（预存在问题按 §6-5 白名单）→ 真实冒烟 §6-4 最后单独执行（前置产物
依赖环境，不混入单测）。
