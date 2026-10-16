# 24. `load` 命令改为 `rag_load` 统一加载设计

> 状态：已实施（2026-09-19，含 §7 全部验收）；**修订一**（2026-09-19 用户定案）：
> 默认全量由三类扩为四类——`DEFAULT_RAG_LOAD_TYPES = (sql, table_schema, dim_value, cql)`，
> cql 纳入默认序列末位（前三类清/写 Milvus collection，cql 清/写 Neo4j 整库）。
> 本文中"cql 不参与默认全量/仅显式指定"的表述按此修订为准；
> **修订二**（2026-09-19，docs/update/27）：`--type` 改为必选（单类或 `all`
> 显式全量，裸调用报错），失败语义反转为默认首错即停 + `--continue-on-error`
> 显式继续（`--fail-fast` 删除），并增加重复传入防护。本文"默认全量/默认继续/
> --fail-fast"相关表述以 doc 27 为准
> 日期：2026-09-19
> 前置：9c615d2（dim_config/sql-rag 并入 metadata 命令，`sql-rag` 组已删除）

## 1. 背景与目标

**现状**：`metaweave load --type {cql|content_md|dim|dim_value|sql|table_schema}`，
`--type` 必填。RAG 链路需要的三类加载（sql 示例 / 表结构 / 维表值）加上 Neo4j CQL
加载分散在同一命令下，且每次只能加载一类，无"一键全量"入口。

**目标**：

1. `metaweave load` 更名为 `metaweave rag_load`，旧命令**完全移除、不留别名**
   （沿用 93f51a1 删 `--step cql_llm`、9c615d2 删 `sql-rag` 组的先例）
2. `--type` 改为**可选**：不传时默认依序加载三类向量类型 `sql → table_schema → dim_value`
3. `cql`（Neo4j 图数据库加载）保留为显式类型——图库也是 RAG 链路的组成部分，
   但清库代价大，**不参与默认全量**，仅显式指定时执行
4. `--clean` 语义：每类加载前先清空目标（collection 删除重建 / Neo4j 清库），
   与各 Loader 现有 `load(clean=True)` 行为一致，本次不改核心逻辑
5. CLI choices 收敛：移除 `dim`（与 `dim_value` 完全重复的别名）与
   `content_md`（从未注册实现，运行时报"尚未实现"）——**仅从 CLI choices 移除**，
   `LoaderFactory` 注册表不动，core 层零改动

## 2. 命令契约

```
Usage: metaweave rag_load [OPTIONS]

  RAG 数据统一加载：将生成产物加载到向量库（Milvus）与图库（Neo4j）

  加载类型：
  - sql          : Question-SQL 样例 → 向量库（--generate-sql-rag 产物）
  - table_schema : 表结构语义     → 向量库（json 产物）
  - dim_value    : 维表值        → 向量库（dim_config 产物）
  - cql          : Cypher 脚本   → Neo4j 图库（cql 产物）

  不传 --type 时默认加载三类向量类型（sql → table_schema → dim_value），
  cql 仅显式指定时加载。默认全量时任一类型失败后继续其余类型，
  任一失败最终退出码非 0；--fail-fast 可改为首错即停。

Options:
  -t, --type [sql|table_schema|dim_value|cql]   可选；缺省加载全部三类向量类型
  -c, --config PATH    主配置文件（默认 configs/metadata_config.yaml）
  --clean              每类加载前先清空目标库（谨慎使用；部分失败后果见 §3.3）
  --fail-fast          首个失败即停止后续类型（默认失败后继续加载其余类型）；
                      仅默认全量时有意义，指定 --type 时忽略并告警
  --debug              调试模式
```

### 2.1 前置条件与预检范围（评审修订）

| 类型 | 产物/文件依赖 | 服务依赖 |
|---|---|---|
| sql | `output/sql-rag/qs_{db}_pair.json`（`--generate-sql-rag` 产物） | Milvus + Embedding |
| table_schema | `output/md/` **与** `output/json/`（`--step md`/`json` 产物，validate 双重检查） | Milvus + Embedding |
| dim_value | `output/dim_config/dim_tables.yaml`（`embedding_col` 需人工填写确认） | **PostgreSQL**（采样维表值）+ Milvus + Embedding |
| cql | `output/cql/import_all.*.cypher`（默认 `cql_loader.input_file`，通配符**必须恰好匹配一个**文件——零个或多个匹配均校验失败，见 `cql_loader.py` `_resolve_input_file`） | Neo4j（`--clean` 清空的是**整库**而非 collection） |

公共服务前置：Milvus 可达（`vector_database` 配置 / `MILVUS_*` 环境变量）、
Embedding 可用（`DASHSCOPE_API_KEY`）。

**预检范围**：不引入跨类统一预检阶段；沿用每类加载前的 `loader.validate()`
（输入文件/目录存在性、collection 配置、dim_value 的 metadata 配置加载）。
默认继续模式下，某一类型的校验失败只记该类失败，不影响其余类型。

**等价映射**（改造前 → 后）：

| 旧命令 | 新命令 |
|---|---|
| `metaweave load --type sql --clean` | `metaweave rag_load --type sql --clean` |
| `metaweave load --type table_schema --clean` | `metaweave rag_load --type table_schema --clean` |
| `metaweave load --type dim_value --clean` | `metaweave rag_load --type dim_value --clean` |
| （无，需手动跑三次） | `metaweave rag_load --clean` |
| `metaweave load --type cql --clean` | `metaweave rag_load --type cql --clean` |

## 3. 内部实现设计

### 3.1 `metaweave/cli/loader_cli.py`（主体改造）

- 命令装饰器 `@click.command(name="load")` → `@click.command(name="rag_load")`，
  函数名 `load_command` → `rag_load_command`，docstring 与示例同步
- `--type` 去掉 `required=True`，choices 收敛为四类
- 新增模块级常量：`DEFAULT_RAG_LOAD_TYPES = ("sql", "table_schema", "dim_value")`
- **现有单类执行主体提取为内部函数
  `_run_single_load(load_type, config_path, clean, debug) -> dict`**
  （配置解析、`sql_loader.input_file` 自动推断、工厂创建、validate/load、
  结果横幅输出全部原样搬入）
  返回 `{"success": bool, "execution_time": float, "message": str}`
  （message：失败原因摘要；成功时为 loader 返回的 message）。
  **耗时来源（评审修订）**：`execution_time` 一律由 helper 入口
  `time.perf_counter()` 统一计时（口径 = 该类型完整执行，含配置解析、
  校验与加载），从而覆盖 validate / 工厂创建 / 异常等没有 loader 耗时
  返回的失败路径；成功时**不**采用 loader 自报的 execution_time，
  避免两种口径混杂
- **helper 异常契约（评审修订）**：提取体内现有 5 个失败出口
  （validate 失败、result.success 为假、except FileNotFoundError /
  ValueError / Exception）全部改为**返回失败结果 dict**，
  **不再抛 `click.Abort`**；各出口的错误输出（echo / logger / debug
  traceback）原位保留在 helper 内，单类模式的终端输出与旧版完全一致。
  不捕获 `BaseException` 派生类（`KeyboardInterrupt` / `SystemExit`
  自然穿透，与现行为一致）
- 命令函数变为薄壳：

```
types = [load_type] if load_type else list(DEFAULT_RAG_LOAD_TYPES)
if load_type and fail_fast:
    打印一行 warning："--fail-fast 仅对默认全量有意义，已忽略"
    fail_fast = False
results = []
for t in types:
    r = _run_single_load(t, config_path, clean, debug)
    results.append((t, r))
    if not r["success"] and fail_fast:
        break            # --fail-fast：剩余类型不执行，标记 ⏭️ 未执行
汇总输出：每类 ✅/❌（--fail-fast 时含 ⏭️）+ 耗时 + 失败原因；
  --clean 下失败类型附带补跑提示（见 §3.3）
任一类失败 → click.Abort()（多类型在 Abort 前先输出汇总；
  单类型跳过汇总直接 Abort——Abort 一律由命令函数发出，helper 只返回结果）
```

### 3.2 失败语义（默认继续，评审修订）

- **默认：任一类型失败记录错误并继续**加载其余类型——三类向量 collection
  彼此独立，部分进度严格优于全停；随后可用 `rag_load --type <失败类型>`
  定点补跑
- **可选 `--fail-fast`**：首个失败即停止后续类型（"首错即停"场景），
  剩余类型在汇总中以 ⏭️ 未执行 标出（仅此模式出现"未执行"）
- **`--fail-fast` 与 `--type` 互斥语义（补充修订）**：指定 `--type` 时
  单类型不存在"后续类型"，`--fail-fast` 无意义——**不报错，忽略并打印
  一行 warning**（与 metadata 命令"作用域告警不拦截"的处理风格一致）
- **退出码契约**：任一类失败（无论何种模式、单类型或多类型），命令最终
  退出码非 0（`click.Abort()` → 1），便于 CI/脚本发现异常
- 单类型模式（评审修订：统一失败流程）：helper 同样**只返回失败结果、
  不抛 Abort**；命令函数收到失败结果后**跳过汇总清单、直接
  `click.Abort()`**——错误消息已由 helper 打印，终端输出与旧版一致，
  仅不追加汇总段。即：Abort 永远由命令函数发出，helper 从不抛出

### 3.3 `--clean` 行为与部分失败后果（评审修订）

- 逐类生效：每个类型加载前若 `--clean` 则执行该类清空（现有 Loader 语义：
  Milvus 删除重建 collection，Neo4j 清库），清空前警告横幅沿用现有输出
- 与默认全量组合时，三类**各自**先清后载（不是先清三类再加载三类——
  中途失败时已完成部分的数据是完整可用的）
- **部分失败后果（必须写入帮助与汇总输出）**：某类在清空后加载失败时，
  该类型的目标处于**空或半完成**状态——Milvus 类型（sql / table_schema /
  dim_value）为对应 collection，cql 为 **Neo4j 整库**（此前已成功的类型
  不受影响、保持完整）。汇总输出对 `--clean` 下的失败类型附加补跑提示：
  `补跑: metaweave rag_load --type <失败类型> --clean`

### 3.4 `metaweave/cli/main.py`

- `from metaweave.cli.loader_cli import load_command` → `rag_load_command`
- `cli.add_command(...)` 同步；顶层帮助文本命令列表自动更新

## 4. 明确不改动的部分

| 部分 | 理由 |
|---|---|
| `LoaderFactory` 注册表（含 `dim` 别名注册、`sql` 延迟注册） | core 层稳定；`pipeline load` 的 `LOAD_STEPS = ["cql","table_schema","sql"]` 依赖这些注册名 |
| `loaders` 配置段、各 Loader 类实现 | `--clean` 已由 `load(clean=...)` 实现 |
| `metadata` 命令、`pipeline generate` 等其余子命令 | 与加载无关 |
| 历史设计文档（docs/17/53/57/59/update18） | 点时记录，按惯例不回改 |

### 4.1 pipeline load 定位：待移除的兼容入口（评审修订）

**现状冲突**：`pipeline load` 默认 `cql → table_schema → sql`
（`dim_value` 需 `--with-dim-values` 显式开启、依赖已确认的 dim_tables.yaml），
与 rag_load 默认 `sql → table_schema → dim_value`（不含 cql）在
覆盖范围、顺序、前置条件上均不同——若不声明边界，将构成两个
语义冲突的"统一加载"入口。

**处置（本轮不改 pipeline load 代码）**：

- rag_load 是后续**唯一推荐**的加载入口
- pipeline load 定位为**待移除的兼容入口**：不再新增功能、
  不同步 rag_load 的默认行为，保持现状直至删除
- 删除条件：rag_load 通过全部四类（sql / table_schema / dim_value / cql）
  真实冒烟后，随下一次 CLI 收敛改造移除——**该改造同时清偿文档债务**
  （docs/100 与运行指南届时统一更新为 rag_load，pipeline load 收缩为
  迁移说明），预计不晚于 2026-10-31
- 文档策略（**随移除改造落地，非本轮**）：届时 docs/100 与迁移指南
  **只展示 rag_load** 用法；pipeline load 仅保留一段迁移说明
  （含旧命令 → rag_load 的等价映射）。过渡期内的文档不一致作为债务
  记录在 §6

## 5. 测试设计

新增 `tests/unit/test_rag_load_cli.py`（CliRunner + mock `LoaderFactory.create`）：

**命令契约**

1. `--type sql`：仅创建并执行 sql 加载器，`load(clean=...)` 参数正确传递
2. 不传 `--type`：按 `sql → table_schema → dim_value` 顺序展开三类
3. `--type cql`：仅执行 cql 加载器
4. `--clean` 传递到每次 `load()` 调用
5. `sql_loader.input_file` 为空时自动推断 `qs_{db}_pair.json` 在 rag_load 下仍生效
6. 无效类型被 `click.Choice` 拦截（含 `dim`/`content_md` 已从 choices 移除）

**失败路径与可观察输出（评审修订：默认继续语义）**

7. 默认继续：第一类失败后，其余类型仍被创建/调用（`create` 调用计数断言），
   最终 exit code 1
8. `load()` 返回 `success=False`：该类记失败并继续（多类型下不阻断），
   失败原因出现在汇总中
9. helper 异常契约：mock `create` 分别抛 FileNotFoundError / ValueError /
   Exception → 均转为该类失败结果（不抛出、不中断），汇总含失败原因
   ——与 §3.1「5 个失败出口全部返回失败 dict」契约一一对应
10. `--fail-fast`：首错即停，后续类型不创建，汇总含 ⏭️ 未执行标记；
    `--type x --fail-fast` 组合：正常执行单类、退出码不受影响、
    输出含"已忽略"warning（不报错）
11. 汇总清单：多类型结束时输出每类 ✅/❌ + 耗时 + 失败原因；
    `--clean` 下的失败类型输出补跑提示
    `补跑: metaweave rag_load --type <失败类型> --clean`；
    单类型失败时**不含**汇总清单（与旧版输出一致）

**旧命令消亡（评审修订：必须经顶层 cli group 验证）**

12. `metaweave load`：通过 `CliRunner().invoke(cli, ["load", ...])` 调用
    **顶层命令组**（`metaweave.cli.main.cli`），断言 exit code 2 且输出含
    `No such command 'load'`——直接 invoke `rag_load_command` 只会得到
    未知参数错误，无法验证该契约

**契约锁定（评审补充）**

13. 单类型失败：exit code 1、输出**无汇总段**、无异常 traceback
    （helper 不抛 Abort 的可观察验证——错误消息已由 helper 打印），
    锁定 §3.2「Abort 由命令函数发出」契约
14. 计时契约：mock `time.perf_counter` 为固定序列 → 成功与失败路径的
    `execution_time` 均来自 helper 统一计时（含 validate/异常路径），
    非 loader 自报值，锁定 §3.1 耗时来源契约

## 6. 文档范围（评审定案：本轮不改文档，记录债务）

**范围决定（B）**：本轮**不更新** docs/100 与迁移指南——文档债务延后至
pipeline load 移除的 CLI 收敛改造时统一清偿（见 §4.1）。

记录在案的债务（过渡期内已知的不一致）：

- `docs/100_执行命令完整参考.md`：命令树与 load 章节仍描述旧命令
  `metaweave load`（本轮后该命令已不存在）；`pipeline load` 段未收缩
- `docs/迁移后运行指南.md`：三行加载命令示例仍为旧 `metaweave load` 写法
- 清偿时需一并写入：rag_load 默认全量与默认继续语义、`--fail-fast`、
  clean 部分失败后果、旧命令 → rag_load 等价映射、pipeline load 迁移说明

CLAUDE.md / AGENTS.md：已核实无 `metaweave load` 字面引用，
`loader_cli.py` 条目描述无需变更（此项不属于债务）。

## 7. 验收标准

1. `metaweave rag_load --type sql -c configs/metadata_config.yaml --clean` 行为等价于旧 `load --type sql`（真实 Milvus 冒烟）
2. `metaweave rag_load -c configs/metadata_config.yaml --clean` 三类依序加载、各自先清后载（真实冒烟）
3. `metaweave rag_load --type cql -c configs/metadata_config.yaml --clean` 完成 Neo4j 清库重载（真实冒烟，对应 §4.1 四类冒烟删除前提）
4. `metaweave rag_load` 任一类失败：其余类型仍被加载、汇总含失败原因与耗时、退出码非 0（单测验证；`--fail-fast` 时后续类型不执行）
5. `metaweave load` 报 `No such command`
6. 新增单测全绿；全量回归通过（已知 2 个预存在问题除外）
7. 文档不在本轮范围（债务清单见 §6）
