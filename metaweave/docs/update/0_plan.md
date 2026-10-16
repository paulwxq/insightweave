# 0. 待办计划（未来功能与改造）

> 用途：记录已明确方向、但暂缓实施的功能需求。每条包含需求描述、
> 当前状态与实施要点，便于未来直接开工。

---

## 1. `metadata --step ddl` 增量更新（`--incremental`）

**需求（用户描述）**：检查数据库中是否有**新增的表**，如果有，只把新增表
写入 DDL 产物（增量更新），而不是全量重新生成。

**当前状态（2026-09-21）**：
- `--incremental` CLI 参数**已删除**（原为 `metadata -i/--incremental`）。
  删除原因：该参数自引入起从未实现——`MetadataGenerator.generate()` 的
  `incremental` 形参仅存在于签名与 docstring（标注"暂未实现"），函数体
  零使用；共享执行层重构后参数值更是被静默丢弃（构造
  `MetadataExecutionRequest` 时无对应字段），保留只会误导用户。
- `MetadataGenerator.generate()` 的 `incremental: bool = False` **形参保留**
  （core API，作为未来实现的接线点；当前所有调用依赖默认值）。
- `MetadataExecutionRequest` **无** incremental 字段——重加时需新增。

**重加实施要点**：
1. **语义定义**：增量 = 仅处理数据库中"当前 DDL 产物尚未覆盖"的表；
   需先确定比较基准（如 `output/ddl/{db}.{schema}.{table}.sql` 文件存在性
   或 DDL 内表清单），以及被删除表的处理策略（保留产物 / 清理 / 告警）。
2. **前置依赖**：需要数据库连接（列举当前表）与既有 DDL 产物的读取；
   `--clean` 与增量语义互斥，需在 CLI 层校验拒绝组合。
3. **接线点**：
   - CLI：重加 `-i/--incremental` 选项（flag）+ 命令签名参数 +
     `_MODE_SCOPED_PARAMS["step"]` 与 `_PARAM_DISPLAY` 条目；
   - 请求层：`MetadataExecutionRequest` 增 `incremental: bool = False` 字段，
     `execute_ddl_json_md` 显式校验并传入 `generator.generate(incremental=...)`
     （仅 ddl 步骤有意义；json/md 传入时应拒绝或告警）；
   - pipeline：若需 YAML 暴露，新增步骤选项键（如 `ddl: {incremental: true}`）
     并同步 `_validate_step_options` 白名单与文档。
4. **测试**：新增表检测（新表产出、旧表跳过）、无新增表（幂等空跑）、
   与 `--clean` 组合拒绝、产物对照（增量结果与全量结果一致）。

**关联记录**：删除该参数的提交见本轮 CLI 收敛（用户审核第 1 条，2026-09-21）。
