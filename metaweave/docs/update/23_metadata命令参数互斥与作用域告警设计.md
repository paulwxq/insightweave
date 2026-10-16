# metadata 命令参数互斥与作用域告警设计

> 状态：已实施（2026-09-18 初版三模式 + 作用域告警；同日新增 `generate-sql-rag` 第四模式，见文末演进记录）
> 涉及代码：`metaweave/cli/metadata_cli.py`、`tests/unit/test_metadata_generate_dim_config_cli.py`、`tests/unit/test_metadata_generate_sql_rag_cli.py`

## 1. 背景

`metadata` 命令经过多轮演进后聚合了三类互不兼容的执行模式（`--step` 流程、
`--generate-domains`、`--generate-dim-config`），但所有参数都是**命令级**
option——click 的模型决定了它们对整个命令可见。这带来两个问题：

1. **硬冲突**：模式之间语义互斥（如 `--generate-domains` 与 `--domain`），
   同时传入时行为不明确；
2. **软无效**：参数只在对应分支被消费（如 `--description` 只被
   generate-domains 分支读取），传给其他模式时被**静默忽略**——命令照常
   执行，但用户可能误以为参数已生效。

处理原则（fail-fast 与容错的分层）：

| 冲突类型 | 处理方式 | 理由 |
|---|---|---|
| 模式互斥（语义冲突） | **报错**（`click.UsageError`，非 0 退出） | 两模式同时激活必然行为错乱，必须拦截 |
| 参数软无效（作用域不匹配） | **仅告警**，不拦截执行 | 用户意图大概率仍是执行当前模式，只是带了多余参数；提示即可 |

## 2. 激活模式定义

校验按"当前激活模式"判定，四选一（优先级从上到下）：

| 模式 | 触发条件 | 说明 |
|---|---|---|
| `generate-domains` | `--generate-domains` | 生成业务域配置 `db_domains.yaml`，完成后提前返回 |
| `generate-dim-config` | `--generate-dim-config` | 生成维度表配置 `dim_tables.yaml`，完成后提前返回 |
| `generate-sql-rag` | `--generate-sql-rag` | 生成 Question-SQL 样例并按 `sql_rag.validation.enabled` 自动校验，完成后提前返回 |
| `step` | 三者皆未传（默认） | `--step` 流程（ddl/json/md/rel/cql/standard，默认 standard） |

## 3. 第一层：互斥校验（报错）

发生在配置加载之前，纯 CLI 级判定，命中即 `UsageError` 终止：

| 冲突组合 | 错误信息 |
|---|---|
| `--generate-domains` × `--domain` / `--cross-domain` | `--generate-domains 与 --domain/--cross-domain 互斥，请分两步执行` |
| `--generate-dim-config` × `--domain` / `--cross-domain` | `--generate-dim-config 与 --domain/--cross-domain 互斥，请分两步执行` |
| `--generate-domains` × `--generate-dim-config` | `--generate-domains 与 --generate-dim-config 互斥，请分两步执行` |
| `--generate-sql-rag` × `--domain` / `--cross-domain` | `--generate-sql-rag 与 --domain/--cross-domain 互斥，请分两步执行` |
| `--generate-sql-rag` × `--generate-domains` | `--generate-sql-rag 与 --generate-domains 互斥，请分两步执行` |
| `--generate-sql-rag` × `--generate-dim-config` | `--generate-sql-rag 与 --generate-dim-config 互斥，请分两步执行` |

注：`--domain` 是 flag（`flag_value="all"`），不带值。测试中直接传裸 `--domain`。

## 4. 第二层：作用域告警（warning，不拦截）

### 4.1 作用域矩阵

"✅"表示该参数在此模式下被消费；"⚠️"表示无效——若从命令行**显式传入**，
执行时逐参数输出告警后继续运行：

| 参数 | generate-domains | generate-dim-config | generate-sql-rag | step 流程 |
|---|:---:|:---:|:---:|:---:|
| `--description` | ✅ | ⚠️ | ⚠️ | ⚠️ |
| `--md-context-dir` | ✅ | ⚠️ | ✅ | ⚠️ |
| `--md-context-mode` | ✅ | ⚠️ | ⚠️ | ⚠️ |
| `--md-limit` | ✅ | ⚠️ | ⚠️ | ⚠️ |
| `--domains-config` | ✅（写入） | ⚠️ | ✅（读取） | ✅（rel/cql 读取） |
| `--dim-config` | ⚠️ | ✅ | ⚠️ | ⚠️ |
| `--clean` | ⚠️ | ✅ | ✅ | ✅ |
| `--schemas/-s` | ⚠️ | ⚠️ | ⚠️ | ✅ |
| `--tables/-t` | ⚠️ | ⚠️ | ⚠️ | ✅ |
| `--max-workers/-w` | ⚠️ | ⚠️ | ⚠️ | ✅ |
| `--incremental/-i` | ⚠️ | ⚠️ | ⚠️ | ✅（本身暂未实现） |
| `--domain` | ❌ 报错 | ❌ 报错 | ❌ 报错 | ✅ |
| `--cross-domain` | ❌ 报错 | ❌ 报错 | ❌ 报错 | ✅ |
| `--config/-c` | ✅ | ✅ | ✅ | ✅（全局通用，不参与矩阵） |

### 4.2 矩阵设计要点

1. **`--domains-config` 是跨分支参数，不做限制**：generate-domains 分支写入
   `db_domains.yaml`；generate-sql-rag 分支读取同一文件；step 流程中 rel
   （域过滤）与 cql（DomainResolver）也读取。它在三种模式下都合法；
2. **`--md-context-dir` 现为双模式参数**：generate-domains 读 md 摘要做域
   推断，generate-sql-rag 读 md 表结构文档生成 Question-SQL——两者共用
   同一目录解析函数 `_resolve_md_context_dir`（CLI 显式 >
   `output.markdown_directory` > `output.output_dir/md` > `output/md`）；
3. **`--clean` 的模式差异**：
   - step 流程：清理对应步骤输出目录（ddl/json/md 仅删顶层文件，rel/cql
     清空目录，含子目录）；
   - generate-dim-config：清理 `dim_tables.yaml` 所在目录（解析结果跟随
     `--dim-config` > `loaders.dim_loader.config_file` > 默认
     `output/dim_config/dim_tables.yaml` 三级优先级）的**顶层文件**，
     保留子目录——与 ddl/json/md 语义一致；清理前经
     `_ensure_in_project_root` 校验目录必须在项目根内；
   - generate-sql-rag：调用 `generator.clean_output(db_name)` 删除**当前库
     旧样例文件** `qs_{db}_pair.json`（单文件删除，非清目录；db_name 取自
     db_domains.yaml 的 `database.name`）——原 `sql-rag generate --clean`
     语义原样继承；
   - generate-domains：**不消费**（`db_domains.yaml` 单文件直接覆盖，无目录
     清理语义）→ 显式传入时告警；
4. **`--incremental` 归入 step 作用域**：代码注释标注"暂未实现"，传入不生效，
   但其设计意图属于 step 流程，不因未实现而告警；
5. **`--config` 不参与矩阵**：全局通用。

### 4.3 判定规则：只认"显式传入"

告警判定的关键在区分"用户显式传入"与"默认值"：

- 使用 `click.Context.get_parameter_source(param_name)`，仅当来源为
  `COMMANDLINE` 时才可能告警；
- 默认值（`DEFAULT`，如 `--md-limit` 默认 100、`--max-workers` 默认 4、
  `--md-context-mode` 默认 `name_comment`）**永不触发告警**——否则每次执行
  都会误报一串；
- 该机制在文件中已有先例：`_resolve_md_context_dir` 用同一手段判定
  `--md-context-dir` 是否显式传入（CLI 显式 > 配置项）。

### 4.4 告警输出形式

同时输出到 stderr（click.echo）与日志（logger.warning），每参数一条，命令
**继续执行**：

```
⚠️  参数 --description 在当前模式（generate-dim-config）下不生效，已忽略
⚠️  参数 --tables/-t 在当前模式（generate-dim-config）下不生效，已忽略
```

## 5. 实现位置

| 组件 | 位置 | 说明 |
|---|---|---|
| `_MODE_SCOPED_PARAMS` | `metadata_cli.py`（模块级常量） | 三种模式的有效参数白名单 |
| `_PARAM_DISPLAY` | 同上 | 参数名 → CLI 显示形式（`--tables/-t`）映射 |
| `_warn_out_of_scope_params(active_mode)` | 同上 | 遍历不在白名单内的参数，仅对 COMMANDLINE 来源告警 |
| 模式判定 + 调用 | 命令体内"阶段 1.2"（互斥校验之后） | 顺序：互斥报错（阶段 1）→ 作用域告警（阶段 1.2）→ 各分支执行 |
| `--clean` 清理逻辑 | generate-dim-config 分支内 | 路径解析后、扫描前执行；复用 `_ensure_in_project_root` 安全校验 |

校验顺序的意义：硬冲突先报错拦截（不会走到告警），软无效仅在通过互斥
校验后才提示。

## 6. 示例

### 6.1 告警（软无效，不拦截）

```bash
uv run metaweave metadata --generate-dim-config --description "测试" --tables orders --clean
```

```
📋 加载配置: /opt/projects/metaweave/configs/metadata_config.yaml
⚠️  参数 --description 在当前模式（generate-dim-config）下不生效，已忽略
⚠️  参数 --tables/-t 在当前模式（generate-dim-config）下不生效，已忽略
🧹 已清理 /opt/projects/metaweave/output/dim_config 下 1 个文件（保留子目录）
🔍 开始扫描维度表...
...（正常完成）
```

`--clean` 在该模式下有效 → 无告警且实际执行了清理。

### 6.2 报错（硬互斥，非 0 退出）

```bash
uv run metaweave metadata --generate-domains --generate-dim-config
```

```
Error: --generate-domains 与 --generate-dim-config 互斥，请分两步执行
```

## 7. 测试

`tests/unit/test_metadata_generate_dim_config_cli.py`（13 个用例）中与本设计
直接相关的：

- 端到端告警：`--generate-dim-config` + `--description`/`--md-limit`/`--tables`
  → 3 条告警 + exit 0 + 生成照常完成；
- 反例：`--generate-dim-config --dim-config ... --clean`（全有效）→ 无告警；
- 矩阵断言（`test_mode_scoped_params_matrix`）：逐参数断言三种模式下的
  归属与 4.1 表一致——防止未来新增参数时矩阵失同步；
- 互斥：`--generate-dim-config` × `--generate-domains` / `--domain` 各一条；
- `--clean` 清理语义：顶层文件删除、子目录及内容保留；不带 `--clean` 时
  无关文件不动。

## 8. 演进约定

后续向 `metadata` 命令新增参数时：

1. 判断参数的作用域（全局 / 某一模式 / 跨模式），同步更新
   `_MODE_SCOPED_PARAMS` 与 `_PARAM_DISPLAY`；
2. 若与现有模式产生语义冲突，在阶段 1 增加互斥报错；仅是"不生效"则只入
   矩阵告警；
3. 更新 `test_mode_scoped_params_matrix` 断言，保持矩阵-测试-文档三方一致
   （本文档 4.1 表为权威版本）。

## 9. 演进记录

- **2026-09-18（初版）**：三模式（generate-domains / generate-dim-config /
  step）+ 互斥报错 3 条 + 作用域告警机制 + `--generate-dim-config --clean`
  顶层文件清理。
- **2026-09-18（同日第二批）**：新增第四模式 `generate-sql-rag`（原
  `sql-rag generate/validate/run-all` 三命令合并，`sql-rag` 命令组整组移除）。
  变化点：
  - 互斥报错 +3 条（sql-rag × domains/dim-config/domain）；
  - `--md-context-dir` 从单模式参数变为 generate-domains + generate-sql-rag
    双模式参数（共用模块级 `_resolve_md_context_dir`，由命令体嵌套函数提升）；
  - `--clean` 增加 generate-sql-rag 语义（删除当前库 `qs_{db}_pair.json`
    单文件）；
  - 校验段由 `sql_rag.validation.enabled`（默认 true）控制，行为参数全部
    来自配置（`enable_sql_repair` 无 CLI 覆盖）；
  - 质量门禁对齐 pipeline 步骤 9：`enable_sql_repair=false` 且存在无效 SQL
    时非 0 退出；
  - `test_mode_scoped_params_matrix` 已同步更新；新测试见
    `tests/unit/test_metadata_generate_sql_rag_cli.py`（16 用例）。
