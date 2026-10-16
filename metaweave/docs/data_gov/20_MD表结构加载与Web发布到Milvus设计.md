# 20. MD 表结构加载与 Web 发布到 Milvus设计

> **修订提示**：集合命名与替换方式（共用集合 + `entity_id`、先 upsert 再按源清理）、CLI 缩为 UUID 定位的调试入口、Web/API 固定 `include_columns`/`skip_empty_desc` 为 `true`、空描述字段处理、失败时 current 的处理及作业级发布准入，均以 [doc 20.1](20.1_表结构发布选项统一与空描述字段处理改造计划.md) 为准；本文下列与之冲突的 §2–§5 表述已按 20.1 更新。

## 1. 目标与范围

将受管作业的 MD 文档及同名 JSON v3 发布为 Milvus 表结构向量。先扩展 `metaweave rag_load --type table_schema`，后台与 WebUI 复用同一加载器。无需连接业务数据库，不迁移旧数据，不兼容旧清单时间格式；旧测试产物重新执行 JSON、MD 采集。

本次不开放 sql、dim_value、cql 发布，不改 doc 19.3 时间契约。平台库只存发布记录，表结构向量和描述存入 Milvus。

## 2. 脚本入口与目录

```bash
uv run metaweave rag_load --type table_schema \
  --source_id b4a87275-7c37-4feb-bf1c-e5a8446c174f \
  --job_id cc959452-6e87-416c-8297-52a5754480a1
```

- 两个参数必须同时传入，必须是 UUID，当前仅用于 `--type table_schema`。
- 输入固定为项目根目录 `output/<source_id>/<job_id>/md`、`json`，覆盖主配置中该加载器的输入路径。使用既有受管路径函数，拒绝路径中的符号链接。
- table_schema 必须同时传两个参数，不再读取全局 `output/md`、`output/json`；加载器内部仍保留供测试使用的非受管读取分支，不作为 CLI 入口。
- `--clean` 对 table_schema 无额外清理：不论是否传入，都先写入本批记录，再仅删除当前 `source_id` 下不属于本批次的旧记录，绝不 drop 共用集合；受管作业不允许 `max_tables` 截断。`rag_load --type all` 已移除。
- CLI 仅供调试：不查询平台库、不登记 `publish_info`、不改变正式 current，因此不能核对工作区归属、登记启用状态、Job 状态或 JSON 当前运行标识；这些由 Web 发布负责。CLI 使用者负责保证采集已经结束，加载期间不重跑同一作业。

## 3. 当前产物格式与共用输入校验

核心模块 `metaweave/core/table_schema/managed_input.py` 负责目录配置、清单校验与文件快照。加载前完成解析，再初始化外部服务和清理集合。

1. JSON、MD 各自必须有普通文件 `manifest.json`，清单非空、无重复、无路径片段，且与对应目录下同后缀的对象文件集合完全一致：JSON 目录核对 `.json` 文件，MD 目录核对 `.md` 文件，均排除 `manifest.json`。其他后缀文件不参与加载和集合校验。
2. JSON 和 MD stem 集合一致，名称为 `database.schema.object`；读取同名文件。
3. JSON 经现有 `MetadataDocument.from_dict` 校验 v3，`object_info` 身份与文件名一致。Web 另核对登记数据库名。
4. MD 清单 `source_json_updated_at` 必须符合 doc 19.3 的 UTC 六位微秒、Z 结尾契约。Web 将当前 JSON 步骤 `updated_at` 用共用 `utc_isoformat` 格式化后比较，JSON 重跑而未重新生成 MD 时拒绝发布。
5. 表级描述使用完整 MD 文本；字段级描述解析 `## 字段列表`，支持 `numeric(10,2)` 等带括号的类型。字段集合必须与 JSON `column_profiles` 一致；没有有效描述（空、纯空白、恰为 `无注释`）的字段按固定选项跳过并记录名字，畸形字段行和重复字段报错（doc 20.1 §5）。
6. 表分类取 `table_profile.table_category`，时间字段提示取 `column_profiles.*.data_type`。table、view、materialized_view 均可加载，沿用现有搜索对象 table/column 两类。
7. 待写入记录的全部 VARCHAR 字段在向量化前按 UTF-8 字节数核对 Milvus 上限（doc 20.1 §10.2）。输入保存在内存中，本次加载不再次读取产物。

生成时间使用 doc 19.3 新格式不会改变以上字段结构；它不作为 Milvus 主键或发布成功判断依据。

## 4. Milvus 目标与加载结果

所有数据源共用 `loaders.table_schema_loader.collection_name` 指定的同一集合（默认 `table_schema_embeddings`），不再追加 source_id。主键为单字段 `entity_id = source_id + ":" + object_id`，另有 `source_id`、`publish_id` 标量字段；`object_id` 保持 `database.schema.object[.column]`。检索某个数据源时须显式按 `source_id` 过滤。

Web 发布按源替换：全部向量化成功后分批 upsert 本批记录，全部成功后删除该源 `publish_id` 非本次的旧记录；不会清除其他源，也不 drop 集合。该替换不是原子操作，细节与失败边界见 doc 20.1 §10.3。调试 CLI 采用同一替换流程，`publish_id` 为每次生成的调试批次 UUID。

Embedding 使用主配置，Milvus 使用 `vector_database.providers.milvus`。沿用 batch_size 和向量维度配置。受管加载只在全部对象成功写入时成功；零对象、表跳过、向量缺失、重试耗尽、写入数量不足均失败。保留加载数量和耗时供日志、脚本结果查看。

## 5. 后台与 API

复用现有接口，无新增端点：

- `GET /api/data-sources/{source_id}/collection-jobs/{job_id}/publish-check` 增加 `table_schema` 条目。
- `POST /api/data-sources/{source_id}/collection-jobs/{job_id}/publications` 的 `steps` 支持 `table_schema`；结果的 `step` 支持该值。

`table_schema.job_status` 来自同一作业 MD 步骤，并要求 JSON 同时成功。预检只查平台库、读取文件，不连接 Milvus 或调用 Embedding。不符合清单或运行标识要求时不可勾选，提示重新采集 JSON、MD。

执行顺序：

1. 现有统一前置核验检查源存在、启用、作业归属及步骤成功，记录 MD 运行标识。
2. 取得源行锁，复查 MD 与 JSON 状态，读取完整输入快照，核对 JSON 运行标识和登记库名，校验外部服务配置及连接。
3. 持源行锁执行按源替换，直到结果登记提交才释放；同源采集启动、登记修改和发布会等待，不同源不互相阻塞。不在替换前提交事务，避免释放锁后出现两个发布交错。
4. 全部加载成功后撤销旧 current，插入 `publish_info(step_name='table_schema', status='success', is_current_publish=true)` 并提交。已开始首批 upsert 之后失败时，同一事务撤销旧 current 并插入 failed、非 current 记录；首批 upsert 之前（输入、长度预检、配置、连接、集合结构校验、向量化）失败时保留旧 current，按既有失败记录机制处理。通过加载器公开 `close()` 释放 Milvus 连接。

Milvus 与 PostgreSQL 没有跨库事务。首批 upsert 之前失败保留旧 current；之后失败并成功登记时撤销旧 current，该源在 Milvus 中可能新旧记录混合，需要重新发布。Milvus 已成功而平台事务提交失败时也报告失败，不声称自动回滚；进程中断或平台库提交失败可能使记录与集合暂时不一致，重新发布完整替换恢复一致。

发布历史、发布首页现有 RAG 分组已包含 table_schema，沿用当前发布 ID、步骤结果与时间展示。批量选择时其他步骤按既有规则分别提交。

## 6. WebUI

发布页开放 table_schema 复选框，依据预检结果决定是否可选。页面说明 db、ddl、json 发布到平台库，table_schema 发布到 Milvus。沿用发布中禁止重复提交、结果列表与发布历史跳转；sql、dim_value、cql 仍不可选。

## 7. 验证与实施文件

- CLI 参数缺一、非法 UUID、用于其他类型均在加载前报错；两参数传入后路径和集合准确。
- 当前 JSON v3 + MD 的表、字段、分类和时间提示解析正确；受管清单不当成对象。
- 缺文件、清单集合不符、JSON 身份错误、MD 缺字段、旧 MD 清单时间、JSON 已重跑均拒绝。
- 假 Embedding、Milvus 验证完整写入成功、部分/零写入失败，确认旧批次清理只针对当前 `source_id`。
- API 验证 table_schema 预检与成功/失败响应、发布记录及未就绪拒绝；覆盖原有 db、ddl、json 回归。
- 前端执行类型检查与构建，浏览器人工核对勾选、禁用原因、结果与历史。
- 不启动 Milvus、不连接业务数据库的验证只能确认解析与编排逻辑；真实 Embedding/Milvus 写入仍需服务启动后的验收。

主要改动：`metaweave/cli/loader_cli.py`、`metaweave/core/loaders/table_schema_loader.py`、共用输入模块、`backend/profile_loading/table_schema_loader.py`、`backend/publishing/publish_service.py`、`backend/api/schemas.py`、`frontend/src/api/types.ts`、`frontend/src/views/PublishView.vue` 及对应测试。

## 8. 本次实现验证记录

2026-10-05：上述脚本、后台、API 与页面改动已实现。相关加载与发布测试 **174 项通过**；前端 `build`（含类型检查）通过，构建时显式指定本地测试用 `VITE_API_BASE_URL=http://127.0.0.1:8000`。

对 `output/b4a87275-7c37-4feb-bf1c-e5a8446c174f/cc959452-6e87-416c-8297-52a5754480a1` 的 JSON、MD 使用临时副本核对；仅在副本中替换生成时间及 MD 清单时间为 doc 19.3 格式，原产物保持原样。解析得到 **7 个表级对象、23 个字段对象，共 30 条**，未初始化外部服务。该目录原始 MD 清单仍是旧 `+00:00` 文本，不能直接作为新受管加载输入，需重新生成 JSON、MD。

测试使用假平台会话、假 Embedding 与 Milvus；没有连接或写入真实 Milvus，也未处理历史平台数据或业务数据库。浏览器人工操作、真实服务发布尚需环境启动后核验。
