# db_deepagent 步骤 5 前设计补齐（待评审）

> 状态：**评审通过（2026-10-15 第五轮收口；v5 含四轮评审意见与收口修正，随即折回 doc 5）。**本文补齐 doc 5 §21 要求在步骤 5 前完成的两项设计：
> ① 本地计算与 SQL 下推的一致性（NULL、空集、类型、精度与舍入规则）；
> ② 旧线程清理粒度（按线程最后活动／结束时间，保护当前线程、待恢复线程与运行中线程）。
> 评审通过后按文末"并入计划"折回 doc 5，再开始步骤 5 实施。
>
> v2 修订（第一轮 9 项）：空集聚合按有无分组键区分；compare 统一空值/参数错误；聚合前转换 `SUM(x::numeric)`；动态精度；类型白名单＋typname 归一化；top_n 次排序键与显式投影；distinct_values 值集合语义；下推重查标注；清理取 `max(last_active, retired_at)`；advisory lock 互斥；sessions/threads 落库与历史发现列为交付。
>
> v3 修订（第二轮 6 项＋3 处小修正）：锁范围扩至 P0 读取前→P3 完成后外层 finally 释放，取锁失败不做任何元数据写入；checkpoint 前缀枚举转义 LIKE 通配符＋删除前精确归属复核＋相似会话名测试；精度公式改"最大整数位数＋最大小数位数＋进位余量"（覆盖指数跨度，如 `1E+28 + 1E-28`），除法与量化也显式定局部精度；**确定性 collation ≠ 码点序**——文本族第一阶段禁本地排序/截断去重（仅相等性分组可用，且剔除 bpchar），次排序键只含可比较列；补本地/下推路由决策表（含"仅下推"失败时的明确错误，不静默退回）；严格原时点场景用 `allow_pushdown=false`＋源截断返回 `incomplete_source`，重查结果不得冒充原时点；`AnalysisRecord.result_generated_at = 输出 ResultMeta.created_at`（不新增同义字段）；`time`/`timetz` 第一阶段剔出白名单；本地计算拒绝 NaN／Infinity；并入计划补 §18 一致性测试映射。
>
> v4 修订（第三轮 4 项＋2 测试建议）：路由表前置**单路径操作**（describe 任何时候本地、compare 截断→`incomplete_source` 且不下推不套通用表）与**统一数据源检查**（输入间异源→`result_datasource_mismatch` 最优先，下推要求同当前源用专用码，不再归入 incomplete_source/invalid_argument）；线程归属改**解析末段序号＋前缀精确相等**并限制会话 ID 字符集（禁冒号，`^[A-Za-z0-9_-]+$`）；清理 `effective_at` 纳入**最新 checkpoint 时间**（优先公开 `StateSnapshot.created_at`，SQL 兜底，统一时区忽略空值）；top_n 无次键路径返回**不确定性 warning** 且测试不得跳过该分支；内部别名冲突时**递增后缀**生成不冲突名并加测试。
>
> v5 修订（第四轮 4 项＋2 小补充）：**线程归属校验进入 Runner 入口**（共用解析函数；显式 `--thread-id` 与 sessions 记录中的当前线程都必须归属本会话，跨会话拒绝）；轮换编号按**会话已分配最大序号**递增（恢复旧线程不回退、锁内分配且校验目标线程不存在，`sessions.thread_seq` 语义改为最大已分配序号）；**文本族第一阶段整族仅下推**（ColumnMeta 只有 name/pg_type，无法证明输出表达式有效 collation——判等也不放行，不凭 pg_type 或库默认 collation）；清理时**状态读取失败即跳过删除**（时间兜底只补 effective_at，不替代 `.next` 待恢复检查）；describe 补均值与非有限值规则；并入计划测试章节引用更正为 §18.3。
>
> 基线：doc 5（含 doc 6 折入版）；代码基线 commit c8011b2。

## 1. 本地计算与 SQL 下推的一致性（§7.10 增补）

原则：**本地计算逐条镜像 PostgreSQL 语义**；两路径对同一输入产出相同结果。无法保证严格一致的场景（浮点算术、无 `round_digits` 的除法、文本排序）显式声明差异、**限定路由**（§1.6）或给出收敛手段，不默默承诺。

### 1.1 NULL 与空集规则（逐操作）

`aggregate` 遵循 PG 聚合语义（无 `GROUP BY` 的聚合对空输入**返回 1 行**而非 0 行）：

| 场景 | SQL（下推）语义 | 本地计算规则 |
| --- | --- | --- |
| `aggregate` 有分组键、空输入 | 0 行 | 0 行 |
| `aggregate` 无分组键、空输入 | **1 行：count=0，sum/avg/min/max=NULL** | 同：输出 1 行，sum/avg/min/max 为 `null`，count 为 0 |
| `aggregate` 分组键 NULL | NULL 为独立一组 | None 为独立组键，输出该列为 `null` |
| `aggregate` 指标 | `SUM/AVG/MIN/MAX/COUNT(col)/COUNT(DISTINCT col)` 跳过 NULL；**组内全 NULL 时 SUM/AVG/MIN/MAX 为 NULL（不是 0）** | 同：跳过 None，结果 `None`，不以 0 填充；count 为 0 |
| `top_n` 排序 NULL | PG 默认：`ASC → NULLS LAST`，`DESC → NULLS FIRST` | 完全复刻（None 按此置位） |
| `top_n` ties | `strict`：排序＋LIMIT；`include`：`RANK() OVER (ORDER BY k)` ≤ n（**RANK 只按 k，不加次键**） | `strict`：主排序键＋次排序键（1.4）取前 n；`include`：与已入列元素 k 相等的行全部保留 |
| `share` 分子 | `x::numeric / NULLIF(SUM(x::numeric) OVER (…), 0)`：x 为 NULL 的行 share 为 NULL | 同：分子 None → 该行 share 为 `None` |
| `share` 分母 | 总和 0 → NULLIF → 全部 NULL；无非空行 → 同 NULL | 同；两种情形均追加 `warnings: ["分母为零或无有效值，占比无定义"]` |
| `compare` | 不下推（单路径）；**行不存在或 `row_index` 越界 → `invalid_argument`**；单元格 NULL → `diff`／`pct_change` 为 `null`＋warning 指明哪侧无效；`b` 为 0 → `pct_change=null`＋F06 警告 | 同（单路径，无一致性问题） |
| `distinct_values` | `SELECT DISTINCT col`：NULL 是一个合法独立值，保留一次 | 同：None 只保留一个 |
| `describe` | 基于已保存行（单路径） | 显式 `null_count`；min/max/均值口径排除 NULL；**均值仅对数值白名单列输出**（求和保持精确；均值遵循 §1.3 的除法精度与容忍规则——整数/numeric 也会出现无限循环小数如 1/3；浮点列为 `Decimal(str)` 近似并注明）；**列含 NaN/Infinity 时 count/null_count 照常，min/max/均值省略并加 warning**（"列含非有限值，min/max/均值已省略"） |
| 空输入（其余操作） | `top_n`／`share`／`distinct_values`：0 行，不报错、不造 0 值行 | 同 |

### 1.2 类型规则：白名单、别名归一化与可比较性

**归一化**：输入结果 `meta.columns[].pg_type` 取自 `pg_type.typname`。归一化映射与能力：

| 族 | typname | 判等（分组/去重） | 排序（order_by/min/max/次键/截断选择） | 算术（sum/avg/share） |
| --- | --- | --- | --- | --- |
| 整数 | `int2` `int4` `int8` | ✓ 精确 | ✓ | ✓ 精确 |
| numeric | `numeric` | ✓ 精确 | ✓ | ✓ 精确 |
| 浮点 | `float4` `float8` | ✓（`Decimal(str(v))` 最短表示保序保等） | ✓ | **仅下推**（本地拒绝） |
| 文本 | `text` `varchar` | **仅下推（第一阶段整族）**：`ColumnMeta` 只有 name/pg_type，**无法证明输出表达式的有效 collation**（函数/表达式可改写 collation，库默认 collation 也不代表该列）——判等（分组/去重）不放行，不凭 `pg_type=text` 放行，也不只检查库默认 collation；涉及文本列的 `group_by`／`count_distinct`／`distinct_values` 走下推，不可下推时返回明确 `invalid_argument` | 仅下推（同左） | 不适用 |
| `bpchar` | `bpchar` | **仅下推**（PG 比较忽略尾随空格，本地字符串判等不等价） | 仅下推 | 不适用 |
| 时间 | `date` `timestamp` `timestamptz` | ✓（按恢复类型判等） | ✓（恢复为 `date`/`datetime`，保留时区，不做字符串比较） | 不适用 |
| `time`/`timetz` | `time` `timetz` | **第一阶段不支持**（剔出白名单；下推可用，后续版本再补 `datetime.time` 恢复） | 同左 | 不适用 |
| 布尔 | `bool` | ✓ | ✓ | 不适用 |
| 白名单外 | `citext`、`bytea`（编码只存长度）、`json`/`jsonb`、数组、`uuid`、枚举（typname 为类型名）等 | **仅下推** | 仅下推 | 仅下推 |

- **NaN／Infinity 拒绝**：本地计算读取输入值时，发现浮点/numeric 列含 `NaN`／`±Infinity`（编码值或哨兵文本）→ 该操作走"仅下推"路由（本地比较/相等/算术规则对 NaN 均不成立）。
- `distinct_values` 的**截断选择依赖排序**：文本族/`bpchar`/白名单外列 → **仅下推**（本地截断选出的前 N 与库排序可能不同）；数值/时间/布尔列本地允许（两路径排序一致）。
- 参数校验期按 `meta.columns` 的 `pg_type` 静态判定路由（§1.6），错误信息含列名与实际类型。

### 1.3 精度、舍入与两路径一致性

- **求和精度按指数跨度动态确定**：对参与求和的精确值集合，取
  `precision ≥ 最大整数位数（max(adjusted)+1，下界 1）＋ 最大小数位数（max(−exponent)，下界 0）＋ ceil(log10(行数))（进位余量）＋ 2`，
  以局部 `localcontext` 执行——覆盖 `1E+28 + 1E-28`（各 1 位有效数字但跨度 57 位）与 `10^28 + 1` 类用例，整数／numeric 的 `sum`/`count`/`count_distinct`/`min`/`max`/分组键**精确一致**。
- **除法与量化同样显式定精度**：`avg`／`share`／`pct_change` 的除法在局部精度 `分子分母精确位数上界 ＋ 目标小数位（round_digits，缺省取容忍基准 10 位）＋ 2` 下执行；仅显式传 `round_digits` 时量化（`ROUND_HALF_UP`，半值远离零，与 PG `round(numeric, d)` 一致）。
- **浮点算术仅下推**（§1.2）——回避"PG 逐行二进制→numeric 精确转换 vs 本地 `Decimal(str)` 最短表示"的路径分歧。
- **聚合前转换**（下推统一写法）：`SUM(x::numeric)`／`AVG(x::numeric)`。
- **除法的已知差异**：不带 `round_digits` 时本地与 PG `numeric` 除法不保证逐位相同——精确相等断言仅适用"整数/numeric＋无除法"；`avg`／无 `round_digits` 的 `share`／`pct_change` 按 10 位小数量化容忍比较；Skill 指引占比/环比显式传 `round_digits`。**`aggregate` 无 `round_digits` 参数（§7.10 既有接口不增改）**，`avg` 验收按容忍规则。
- 输出编码：数值经 §6.8 编码（`Decimal → {"$dec"}`）；时间 ISO 字符串＋列类型标注。

### 1.4 下推 SQL 形态

- 源：`(<输入结果 meta.executed_sql 去末尾 ;>) AS src`。
- `aggregate`：`SELECT <group_by…>, SUM(src."x"::numeric) / AVG(src."x"::numeric) / MIN(…) / MAX(…) / COUNT(…) / COUNT(DISTINCT …) … [GROUP BY …]`。
- `top_n`：主排序键＋次排序键。**次排序键只接受白名单内可比较类型列**（数值/时间/布尔；浮点可比较）：源结果全部输出列均属可比较类型 → 次键＝全部列依序（边界并列确定化；整行完全重复不可区分，属预期并声明）；否则**不加次键**，边界并列时选中记录**不确定**（PG LIMIT 语义如此），且该路径工具返回追加 `warnings: ["排序键在边界并列，选中记录不确定"]`。**无次键路径不得跳过测试**：断言返回数量（strict 恰为 n 或不足）、选中记录的排序键均不劣于未选中记录（合法 Top-N 范围）、include 保留全部边界并列、warning 存在——不断言两路径选中完全相同的并列记录。`strict` 加 `LIMIT n`；`include` 写为
  `SELECT <显式投影原始列> FROM (SELECT src.*, RANK() OVER (ORDER BY src."k" [DESC]) AS <rank别名> FROM …) r WHERE r.<rank别名> <= n ORDER BY <同上次键>`——外层显式投影原始列（不含 rank 别名）。**rank 别名冲突规避**：从 `__dbda_rank` 起生成，若与输入结果任一输出列名冲突则追加序号（`__dbda_rank_2`、`__dbda_rank_3`…）直到不冲突（确定性生成，SQL 仍可复现）；输入列已含 `__dbda_rank` 的场景列入测试。
- `share`：`SELECT src.<group_by…>, src."x"::numeric / NULLIF(SUM(src."x"::numeric) OVER (<PARTITION BY …>), 0) AS share`，`round_digits` 存在时整体 `round(…::numeric, d)`。
- `distinct_values`：`SELECT DISTINCT src."col" FROM … ORDER BY 1 LIMIT max_values + 1`——读到第 `max_values + 1` 行 → 截断标记，返回前 `max_values` 个、剔除多读行。一致性断言比较**值集合**（multiset）；本地路径仅对可比较类型列允许（§1.2），按恢复类型排序后取前 `max_values`。

### 1.5 下推重查不是原结果快照

下推按源 SQL 在**当前库重新执行**，数据库变化后与旧结果文件不是同一输入：

- 下推产出的 `AnalysisRecord.result_generated_at` ＝ 输出结果的 `ResultMeta.created_at`（既有字段赋值关系，**不新增同义字段**）；工具返回追加 `warnings: ["结果由源 SQL 重新执行获得，非原结果快照"]`。
- 一致性测试（本地 vs 下推）在冻结／静态数据集上执行（验收库数据冻结，§3）。
- **严格原时点场景**（用户明确要求"按当时那次查询的结果"分析）：Skill 指引 Agent 使用 `allow_pushdown=false`——源结果截断时返回 `incomplete_source`（无法恢复完整原快照），如实告知；当前库重查只能作为**另一次明确标注的分析**（带上述 warning 与新时间戳），**不得冒充原时点答案**。

### 1.6 路由决策：单路径操作与通用表

路由按三步执行：**第 0 步统一数据源检查 → 第 1 步单路径操作（describe／compare）专用规则 → 第 2 步通用表（aggregate／top_n／share／distinct_values）**。

**第 0 步（所有操作前置）——数据源检查**（doc 5 §7.10 既有约定，错误码专用、优先级最高）：

- 两个输入结果的 `meta.datasource_id` **互不相同** → `result_datasource_mismatch`（输入间必须同源，即使全部走本地）。
- 走下推分支时另要求输入 `datasource_id` **等于当前数据源**，不等 → `result_datasource_mismatch`（提示在当前数据源重新查询），**不归入** `incomplete_source`／`invalid_argument`。
- 数据源检查先于参数校验与截断判定；本地计算允许输入来自非当前数据源（输出保留输入 `datasource_id`）。

**第 1 步——单路径操作（不进入通用表）**：

| 操作 | 截断输入 | 类型不支持（§1.2 本地能力外） |
| --- | --- | --- |
| `describe` | **任何时候本地执行**（基于已保存行，结果标注该口径；不因截断下推或返回 `incomplete_source`） | 不拒绝执行：`count`／`null_count`／非空数照常；`min`/`max` 仅对白名单可比较列输出，其余列省略并注明"该类型不提供 min/max" |
| `compare` | **不下推**（doc 5 §7.10）；输入截断 → `incomplete_source`（无法确认截断结果上的标量完整性），不套用通用表第 2 条 | 取值列必须为数值族（`diff` 语义），文本等 → `invalid_argument`；含 NaN/Infinity → `invalid_argument`（compare 无下推可回避）；浮点允许（单路径，`Decimal(str(v))` 近似） |

**第 2 步——通用路由表（`aggregate`／`top_n`／`share`／`distinct_values`）**：

| # | 条件 | 路由 |
| --- | --- | --- |
| 1 | 本地支持该操作/列 **且** 输入完整 | **本地**（`executed_sql=None`） |
| 2 | 本地支持 **且** 输入截断 **且**（`allow_pushdown=true` 且源 SQL 可用（`executed_sql` 非空）且输入 `datasource_id` == 当前数据源〔否则第 0 步已拒〕） | **下推**（§8.4 管线；含 warning §1.5） |
| 3 | 本地支持 **且** 输入截断 **且** 不满足 2 的下推条件（缺 `allow_pushdown`／源 SQL） | `incomplete_source`（既有语义） |
| 4 | 本地**不支持**（浮点算术／文本排序／`bpchar` 判等／白名单外类型／含 NaN／Infinity）**且** `allow_pushdown=true` 且源 SQL 可用且同当前数据源 | **下推**（数据库侧合法性由管线三层校验/EXPLAIN 把关，不假定任意类型可下推） |
| 5 | 本地不支持 **且** 不满足 4 的下推条件 | `invalid_argument`（明确错误："该操作/列仅支持下推，且当前不可下推（缺 allow_pushdown／源 SQL）"），**不静默退回本地** |
| 6 | 下推执行失败（校验／超时／只读违例） | 按错误返回，**不改走本地** |

**错误优先级**：数据源检查（第 0 步，`result_datasource_mismatch`）→ 单路径规则／参数校验（`invalid_argument`）→ 截断路由（`incomplete_source`）→ 下推执行错误（原样）。

## 2. 旧线程清理粒度（§13.5 增补）

### 2.1 线程活动轨迹与 sessions 落库（**步骤 5 交付项**）

- 新增命名空间 `("db_deepagent", "threads", session_id)`，key=`thread_id`：`{thread_id, session_id, seq, last_active_at, retired: bool, retired_at: str | null}`。
- **Runner P3 落库（当前实现缺失，列为本步骤交付）**：每次运行结束（含归档）同批写入——`sessions` upsert ＋当前线程 `threads` upsert；轮换时旧线程标 `retired`＋新线程建记录。写入失败按 persistence 失败路径处理。
- **历史线程候选发现**：① 序号推导（`dbda:<session_id>:<seq>`，从 `sessions.thread_seq` 反推）；② checkpoint 枚举兜底——**参数化＋转义 LIKE 通配符**（会话 ID 含 `_`/`%` 时 `dbda:s_1:%` 会误匹配 `dbda:sA1:…`：对 session_id 逐字符转义 `\`、`%`、`_` 并声明 `ESCAPE '\'`）。
- **精确会话归属判定（共用函数，两道防线）**：
  1. **会话 ID 字符集限制**：CLI 与 Runner 校验 `session_id` 匹配 `^[A-Za-z0-9_-]+$`（**禁冒号**——`startswith` 前缀匹配对 `dbda:a:b:1`（会话 `a:b` 的线程）会被会话 `a` 的前缀吞掉；也禁 `\`／`%` 之外的 LIKE 风险字符）。不合规 → `invalid_argument`。
  2. **归属解析（`parse_thread_id(thread_id) -> tuple[str, int] | None`）**：函数只负责解析格式——`rsplit(":", 1)` → `(prefix, seq)`，`prefix` 须以 `dbda:` 起头、`seq` 为正整数，返回 `(session_id, seq)`；**调用方**把返回的会话 ID 与预期会话比较（Runner 入口、清理候选复核共用此逻辑，比较动作不藏在解析函数内）。
- **Runner 入口归属校验（步骤 5 交付）**：读取任何 checkpoint 之前，**显式传入的 `--thread-id` 与 sessions 记录中的 `current_thread_id` 都必须经归属解析属于本会话**，否则拒绝运行（P0 runtime 失败，"thread_id 不属于本会话"）——否则会话 `a` 持锁却读写会话 `b` 的 checkpoint，破坏会话隔离并绕过会话 `b` 的清理互斥。
- **轮换编号分配（步骤 5 交付，写入 §13.4 接线约定）**：区分"当前选择线程序号"与"**会话已分配的最大序号**"（`sessions.thread_seq` 语义改为后者）：新编号 `= max(会话最大已分配序号, 当前序号) + 1`，**在会话锁内分配**、分配时校验目标 `thread_id` 尚无 checkpoint 状态（不存在才用，存在则继续 +1）；恢复旧线程不使最大序号回退。杜绝"恢复线程 1 后轮换复用已存在的线程 2、交接消息写入旧线程"。
- **Store 查询分页**：threads／sessions 遍历经 `store.search` 分页参数循环取全。
- 测试：重启续会话、轮换后指针切换、无轨迹旧线程被发现、分页超默认页、**相似会话名（`s_1` vs `sA1`）与嵌套名（`a` vs `a:b`）互不影响**、含冒号会话 ID 被拒、**跨会话 `--thread-id`（会话 a 传 `dbda:b:1`）被 Runner 拒绝**、**恢复线程 1 后轮换不复用已有线程 2**（新编号取会话最大序号+1）。

### 2.2 清理判定（替换 §13.5 第 1 步的会话级判定为线程级）

1. **当前线程保护**：`thread_id == sessions.current_thread_id` → 不清理。
2. **待恢复保护**：`get_state(config).next` 非空 → 不清理；**`get_state` 抛异常（无法可靠判断待恢复状态）→ 跳过该线程并报告"状态读取失败"**——时间可查到不代表已确认无待恢复任务，SQL 时间兜底只补充 `effective_at`，**不替代待恢复状态检查**。
3. **过期判定**：`effective_at = max(last_active_at, retired_at, 最新 checkpoint 时间)`。**最新 checkpoint 时间始终参与**——不只在"无轨迹"兜底：P3 轨迹写入可能失败（已退休线程被复用、新 checkpoint 已写、轨迹更新失败），会话锁不能修复过时的活动记录；取值优先公开 API `get_state(config).created_at`（`StateSnapshot.created_at`）；**已成功确认无待恢复任务但 `created_at` 缺失时**，允许以 `checkpoints.checkpoint->>'ts'` 最大值兜底（已实测可用）；`get_state` 异常时按第 2 条直接跳过，不做时间兜底。时间统一转 UTC、空值忽略（`max` 跳过 None）；三个来源全部取不到 → 不清理，预览列"无法判定"。`retired` 只作清理优先级排序，不单独构成过期。`effective_at` 早于窗口（`checkpoint_retention_days`，可被 `--before` 收紧）即过期；`--before` 只收紧不放宽。
4. 结果目录／`qa`／`run_errors` 保护不变（§13.5 第 4、5 步）。

### 2.3 运行与清理互斥（PostgreSQL advisory lock）

会话级互斥键：`pg_try_advisory_lock(hashtext('dbda_session:' || session_id))`。

- **Runner 取锁时机覆盖全部持久化读写**：进入 P0 **读取 sessions／checkpoint／决定轮换之前**取锁（专用连接持有），保持至 **P2 读取最终状态、P3 归档与轨迹写入完成**，由外层 `finally` 解锁并关闭连接——不留"P0 锁外读、清理删线程"或"模型结束即释放、归档前被删"窗口。**取锁失败**：本轮按 runtime 失败返回（"会话正在另一进程运行或清理中"），**不做任何 sessions／threads／qa 元数据写入**（避免失败请求覆盖运行中进程的指针），仍写 P4 本地记录。memory 模式无 PG，跳过互斥。
- **维护 cleanup**：逐会话 `pg_try_advisory_lock`，拿不到 → 跳过该会话全部线程（报告"运行中"）；拿到后**重新核对** 2.2 资格（含删除前逐线程精确归属复核，§2.1）再删除，完成或异常后解锁。每会话独立取放锁。

### 2.4 预览与报告

- 预览按线程列出：thread_id、判定依据（retired 优先／不活跃／无法判定‑跳过／运行中‑跳过）、checkpoint 行数估计、拟删结果目录数；`--apply` 后报告成功／失败／跳过（含原因），批次有失败以非零码结束。
- 测试（`test_maintenance_cleanup.py`）：当前线程保留、待恢复保留、retired 优先、`effective_at` 三来源取 max 保护近期活动（含**轨迹落后于 checkpoint 的 P3 失败路径**：复用已退休线程＋新 checkpoint＋轨迹未更新 → 不清理）、**`get_state` 抛异常且 SQL 查到过期时间仍不删除**（待恢复检查不可替代）、无轨迹兜底（有／无 `ts`）、`--before` 收紧、结果目录引用保护、**互斥**（持锁会话被跳过；Runner 取锁失败路径不写元数据；P0 前／P3 前的并发删除窗口由锁覆盖——用例模拟"持锁期间发起清理被跳过"）。

## 3. 步骤 5 实施清单（按 doc 5 既有规格＋本文补齐）

| 项 | 规格 |
| --- | --- |
| `analyze_result` 服务＋工具 | §7.10 ＋ 本文 §1（NULL／空集／白名单／动态精度／下推形态／路由表／重查标注） |
| `read_result` 工具 | §7.9（分页、会话检查、跨数据源 warning） |
| 装配清单切换 15 件 | §10.6（`FULL_TOOLS`、`test_assembly` 补断言、提示词/Skill 恢复下推指引并写入 §1.5–§1.6 的路由与原时点规则） |
| sessions／threads 落库＋历史发现＋分页＋LIKE 转义 | 本文 §2.1 |
| 维护命令 `cleanup` ＋ advisory lock 互斥 | §13.5 ＋ 本文 §2.2–§2.4；Runner 侧全程持锁（§2.3） |
| 轮换完整接线 | §13.4（`/rotate`、轨迹 retired 标记） |
| 冻结参考结果 | §18.3/§18.4——**独立冻结**：正式评测前以人工核对／直连 SQL 建立参考 SQL 与参考结果，**不以 Agent 首轮答案为基准**；`acceptance_cases.yaml`＋`cli acceptance run` 三轮验收 |

## 4. 并入计划（评审通过后）

| 本文条目 | 折回 doc 5 位置 |
| --- | --- |
| 1.1–1.6 一致性规则（NULL／空集／白名单／动态精度／下推形态／路由表／原时点规则） | §7.10 增补小节 |
| 2.1 threads 轨迹命名空间＋落库＋历史发现（LIKE 转义）＋分页 | §13.3 Store 命名空间表 ＋ §14.3（P3 写入） |
| 2.2 线程级清理判定与兜底 | §13.5 第 1 步替换 |
| 2.3 advisory lock 互斥（P0 前取锁至 P3 后释放） | §13.5 ＋ §14.3（P1 段改为"进入 P0 前"） |
| 2.1 Runner 入口归属校验＋轮换编号分配（共用 `parse_thread_id`） | §14.3（P0）＋ §13.4（轮换接线：`sessions.thread_seq`=会话最大已分配序号） |
| 2.4 预览报告与测试要求 | §13.5 ＋ §18 测试映射 |
| §18 一致性测试映射（新增行）：精度用例（`1E+28+1E-28`、`10^28+1`、除法容忍）、空集/NULL 语义、**数据源检查优先级（输入间异源/下推异源均 `result_datasource_mismatch`）**、**单路径规则（describe 截断仍本地、compare 截断 `incomplete_source` 不下推、compare 非数值列拒绝、describe 均值/非有限值省略）**、路由表六分支、**文本族整族仅下推（分组/去重含文本列即下推，不可下推明确报错）**、NaN/Infinity、下推非快照 warning、`allow_pushdown=false`＋截断→`incomplete_source`、**top_n 无次键不确定性（数量/合法范围/并列保留/warning）**、**`__dbda_rank` 别名冲突**、LIKE 转义与相似会话名（`s_1`/`sA1`、`a`/`a:b`）、会话 ID 字符集拒绝、互斥窗口、**轨迹落后于 checkpoint 仍受保护**、**`get_state` 异常＋SQL 过期时间仍不删除**、**跨会话 thread_id 拒绝**、**轮换编号不复用已有线程** | §18.3 验收用例映射表 |
| §21 表 | 两行（旧线程清理粒度、本地计算与 SQL 下推的一致性）标记已补齐 |
