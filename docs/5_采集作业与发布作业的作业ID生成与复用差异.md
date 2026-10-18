# 5. 采集作业与发布作业的作业 ID 生成与复用差异

记录日期：2026-10-18。状态：**现状核对 + 需求澄清与讨论建议（讨论稿，非最终设计），尚未改动代码**。

本文记录数据采集作业（`job_id`）与数据发布作业（`publish_id`）在“重新执行/重新发布并修改步骤”场景下的不同处理方式，作为后续统一或调整作业 ID 语义的依据。第 1–7 节描述当前实现与初步待决策项；第 8–10 节为 2026-10-18 讨论中补充的需求澄清、建议模型与仍需讨论的问题，属于讨论稿，不是最终设计，也未改动代码。

关联文档：[4_多数据源知识隔离与source_id链路改造.md](4_多数据源知识隔离与source_id链路改造.md)。

## 1. 结论摘要

| 维度 | 数据采集作业 | 数据发布作业 |
| --- | --- | --- |
| 标识 | `job_id` | `publish_id` |
| 语义 | **工作区/作业身份**，长期存在 | **一次发布尝试的身份** |
| 重新执行时 | **复用原 `job_id`** | **每次生成新的 `publish_id`** |
| 修改步骤后 | 在同一 `job_id` 下新增/更新步骤行 | 本次勾选步骤写入新 `publish_id` |
| 历史记录 | `job_info` 每个 `(job_id, step_name)` 一行，原地更新，无逐次运行历史 | `publish_info` 每次新增一组行，按 `publish_id` 保留历史 |
| 步骤集合 | 该 `job_id` 下**所有曾经登记过的步骤**的并集 | **本次请求实际勾选**的步骤集合 |
| 当前态 | 无 `current` 标记；`job_status` 汇总全部步骤行 | 每源每步骤至多一行 `is_current_publish=true` |
| 列表一行代表 | 一个工作区及其累积步骤 | 一次发布尝试及其步骤 |

一句话：**采集作业的 `job_id` 是“工作区”，重跑是往同一个工作区里继续写；发布作业的 `publish_id` 是“一次尝试”，每次发布都新开一份记录。**

## 2. 数据采集作业：`job_id` 是稳定的工作区身份

### 2.1 新建作业

从“新建采集作业”入口进入时，URL 不带 `job_id`：

- 路由与入口：[CollectionJobsView.vue:191](../frontend/src/views/CollectionJobsView.vue)（重跑链接）、[CreateCollectionJobView.vue:62](../frontend/src/views/CreateCollectionJobView.vue)（读取 `rerunJobId`）。
- 前端提交时，`activeJobId` 初始为空字符串（[CreateCollectionJobView.vue:448](../frontend/src/views/CreateCollectionJobView.vue)），首个步骤请求不携带 `job_id`（[dataSources.ts:210-232](../frontend/src/api/dataSources.ts)）。
- 后端请求模型允许 `job_id` 为空（[schemas.py:329-335](../metaweave/backend/api/schemas.py)）；工作区由**允许创建新工作区的首个步骤**生成：
  - `db` 步骤：[db_step_service.py:95-96](../metaweave/backend/data_sources/db_step_service.py) 的 `uuid4()`；
  - `ddl` 步骤：[ddl_step_service.py:242-243](../metaweave/backend/data_sources/ddl_step_service.py) 的 `uuid4()`。
  - `json`、`md`、`domain`、`dim_config`、`rel`、`sql` 在 `job_id is None` 时直接报错（例如 [json_step_service.py:255-257](../metaweave/backend/data_sources/json_step_service.py)、[md_step_service.py:250-254](../metaweave/backend/data_sources/md_step_service.py)、[rel_step_service.py:257-259](../metaweave/backend/data_sources/rel_step_service.py)），不会自行伪造工作区。
- 同一请求的后续步骤复用首个步骤返回的 ID（[collection_run.py:286-287](../metaweave/backend/jobs/collection_run.py)）；前端也是一步一个请求，拿到响应后写入 `activeJobId`（[CreateCollectionJobView.vue:477-478](../frontend/src/views/CreateCollectionJobView.vue)）。

### 2.2 重新执行：复用原 `job_id`

列表页的“重新执行”把旧 `job_id` 放进 URL：

```ts
// frontend/src/views/CollectionJobsView.vue:191
:to="`/data-sources/${sourceId}/collection-jobs/new?job_id=${job.job_id}`"
```

重跑页读取该 `job_id` 并作为本次执行的作业 ID：

- 加载旧作业并设置 `jobId.value`：[CreateCollectionJobView.vue:531-533](../frontend/src/views/CreateCollectionJobView.vue)；
- 初始化 `activeJobId` 为已有 `jobId`：[CreateCollectionJobView.vue:448](../frontend/src/views/CreateCollectionJobView.vue)；
- 每个步骤请求都携带该 `job_id`：[dataSources.ts:225](../frontend/src/api/dataSources.ts)；
- 后端直接把请求中的 `job_id` 作为 `active_job_id`：[collection_run.py:199](../metaweave/backend/jobs/collection_run.py)；
- 步骤服务在 `job_id` 非空时先校验工作区归属（例如 [db_step_service.py:112](../metaweave/backend/data_sources/db_step_service.py)），再用 `start_step` 按联合主键 `(job_id, step_name)` 插入新步骤行或把既有行改回 `running`（[repository.py:77-113](../metaweave/backend/jobs/repository.py)）。

测试明确锁定该语义：[test_collection_job_api.py:465-485](../metaweave/tests/unit/test_collection_job_api.py)（`test_rerun_passes_existing_job_id_without_cleaning`）断言重跑请求返回的仍是传入的 `JOB_ID`，且 `clean=False`。

**因此，在采集页面对已有作业修改步骤后执行，不会产生新的 `job_id`；只会改变同一 `job_id` 下的步骤行。**

### 2.3 修改步骤后的实际表现

- **新增步骤**（例如增加 `rel`）：在同一 `job_id` 下新增一行 `(job_id, 'rel')`；原有步骤行不变。
- **取消勾选旧步骤**：只是本次不执行它；原来的 `job_info` 行、产物和状态都保留，不会被删除。
- **作业的“包含步骤”是并集**：采集作业列表按 `job_id` 聚合，`steps` 取该 `job_id` 下全部步骤行（[jobs/listing.py:34-62](../metaweave/backend/jobs/listing.py)），所以展示的是“曾经登记过的步骤”，不是“最近一次勾选的步骤”。
- **执行时间是最近一次状态写入**：`executed_at` 取该作业全部步骤行 `updated_at` 的最大值；执行者仍取最早一条步骤行（[jobs/listing.py:45-70](../metaweave/backend/jobs/listing.py)），没有“本次重跑”的独立记录。
- **状态是全部步骤行的汇总**：`job_status` 按 running 优先、其次 failed、否则 success 汇总（[repository.py:140-145](../metaweave/backend/jobs/repository.py)）。因此旧作业中若存在失败步骤，即使本次只重跑其他步骤并成功，整个作业仍为 `failed`，会继续阻塞发布（[repository.py:148-153](../metaweave/backend/jobs/repository.py) 要求全部步骤行 success）。
- **产物目录不变**：仍是 `output/<source_id>/<job_id>/`；重跑不清理其他步骤的既有产物（[collection_run.py:186-188](../metaweave/backend/jobs/collection_run.py) 明确“两种情况都不清理已有产物”）。

## 3. 数据发布作业：`publish_id` 是一次发布尝试

### 3.1 重新发布：生成新的 `publish_id`

- 列表页“重新发布”把旧 `publish_id` 放进 URL query：[PublishJobsView.vue:176-188](../frontend/src/views/PublishJobsView.vue)；发布首页“重新执行”同样处理（[PublishHomeView.vue:38-39](../frontend/src/views/PublishHomeView.vue)）。
- 发布页读取该 `publish_id` **仅用于加载旧发布的步骤定义并预选复选框**：[PublishView.vue:211-220](../frontend/src/views/PublishView.vue)。
- 点击发布时，请求体只携带 `steps`：[PublishView.vue:256](../frontend/src/views/PublishView.vue)、[dataSources.ts:102-109](../frontend/src/api/dataSources.ts)；服务端请求模型只有 `steps` 且 `extra="forbid"`（[schemas.py:535-538](../metaweave/backend/api/schemas.py)），旧 `publish_id` 不会进入请求。
- 后端在通过全部预检后无条件生成新 ID：[publish_service.py:1701](../metaweave/backend/publishing/publish_service.py) 的 `publish_id = uuid4()`；路由只传 `payload.steps`（[publishing.py:59-72](../metaweave/backend/api/routers/publishing.py)）。
- 测试锁定：连续两次发布同一 `db` 步骤，`publish_id` 必须不同（[test_publish_service.py:784-788](../metaweave/tests/unit/test_publish_service.py)）。

**因此，“重新发布”在语义上是新建一次发布尝试，不是更新旧发布记录。**

### 3.2 修改步骤后的实际表现

- 本次勾选的所有步骤（包括新增的 `rel`）共用同一个新 `publish_id`；每个步骤在 `publish_info` 中写入 `(publish_id, step_name)` 新行（[models.py:173-212](../metaweave/backend/persistence/models.py)）。
- 旧发布记录保留：发布列表按 `publish_id` 分组（[publishing/listing.py:46-78](../metaweave/backend/publishing/listing.py)），旧记录不会因重新发布而消失。
- 每源每步骤至多一行 `is_current_publish=true`（部分唯一索引，[models.py:184-190](../metaweave/backend/persistence/models.py)）；重新发布成功后，该步骤的 current 切换到新行，未重新发布的步骤仍指向旧 `publish_id`。
- 失败步骤也会写在该新 `publish_id` 下（`is_current=False`），因此可凭新 ID 查到本次尝试的失败记录。
- `rel` 发布内部使用新 `publish_id` 作为 provenance 生成 CQL（[publish_service.py:1258](../metaweave/backend/publishing/publish_service.py)），Neo4j 节点/边携带新的 `source_id`/`publish_id`；`output/<source_id>/<job_id>/cql/` 会先清空再重新生成，图数据按源替换。
- 编辑 `rel` 内容本身（`RelEditDialog` 的 `rel-config` PUT）只修改 `rel` 产物，不产生 `publish_id`；之后点击发布才生成新 ID。

## 4. 两种处理的差异对比

| 对比项 | 采集作业（`job_id`） | 发布作业（`publish_id`） |
| --- | --- | --- |
| ID 生成时机 | 新建作业时由 `db`/`ddl` 首个步骤生成；重跑复用 | 每次发布请求无条件生成 |
| ID 的生命周期 | 长期稳定，代表工作区 | 仅代表一次发布尝试 |
| 修改步骤是否产生新 ID | 否 | 是 |
| 平台库记录方式 | `job_info` 按 `(job_id, step_name)` 原地更新 `last_status`/`updated_at` | `publish_info` 按 `(publish_id, step_name)` 追加新行 |
| 是否有逐次运行历史 | 没有 | 有，按 `publish_id` 保留 |
| “步骤集合”的含义 | 该工作区历史登记步骤的并集 | 本次发布实际勾选的步骤 |
| 取消勾选旧步骤 | 旧步骤行/产物/状态保留，仍参与作业状态汇总 | 本次不写入新记录；旧步骤的 current 不变 |
| 状态口径 | `job_status` 汇总该 `job_id` 全部步骤行 | 每步骤 `is_current_publish` + 发布记录 `status` |
| 产物目录 | `output/<source_id>/<job_id>/` 长期复用 | CQL 目录在发布时清理重建；其他发布步骤按各自规则替换 |
| 典型用途 | 采集/画像工作区、步骤依赖、发布准入 | 发布审计、current 指针、Neo4j provenance |

## 5. 影响与风险

1. **采集作业缺少“第几次运行”的维度**：`job_info` 只保留每个步骤的最近一次状态，无法从平台库区分某步骤是哪一次重跑写的，也无法还原每次重跑勾选了哪些步骤。
2. **采集作业的步骤集合会累积**：新增步骤会出现在列表中；取消勾选不会让步骤从作业里消失。用户看到的“包含步骤”与本次选择可能不一致。
3. **旧失败步骤持续阻塞发布**：`job_status` 汇总全部步骤行，旧失败步骤不会因本次重跑其他步骤而清除；必须重跑该失败步骤或另行处理。
4. **发布历史完整，但不能原位修订**：每次“重新发布”都会新增一条发布记录；若希望在同一 `publish_id` 下追加/修改步骤，当前 API 与唯一键语义都不支持（`publish_info` 主键为 `(publish_id, step_name)`，但入口不接收 `publish_id`）。
5. **ID 与下游数据的关联方式不同**：`publish_id` 会写入 Neo4j 节点/边 provenance 和发布 current 指针；`job_id` 主要决定工作区目录和采集步骤行。清理、追溯、审计时不能把两者按同一口径处理。
6. **前端展示口径不同**：采集列表一行是一个工作区（步骤并集），发布列表一行是一次发布尝试（步骤集合）。同一批步骤在两种列表中的“历史”含义不同。

## 6. 复现路径

### 6.1 采集作业

1. 在采集作业列表对已有作业点击“重新执行”，URL 出现 `?job_id=<旧 ID>`。
2. 在重跑页增加 `rel` 步骤并执行。
3. 观察结果：返回的 `job_id` 与旧 ID 相同；`job_info` 中新增 `(旧 job_id, 'rel')` 行，旧步骤行保留；列表中该作业的“包含步骤”变为并集。

### 6.2 发布作业

1. 在发布作业列表对已有发布点击“重新发布”，URL 出现 `?publish_id=<旧 ID>`。
2. 页面预选旧发布步骤；增加 `rel` 步骤后点击发布。
3. 观察结果：返回新的 `publish_id`；`publish_info` 新增一组以新 ID 为主键的行；旧发布记录仍保留；`rel` 的 CQL provenance 与 Neo4j 节点/边使用新 ID。

### 6.3 只读核对 SQL

```sql
-- 采集作业：同一 job_id 下的全部步骤行（步骤并集与最近状态）
SELECT job_id, step_name, last_status, created_at, updated_at
FROM public.job_info
WHERE source_id = :source_id
ORDER BY job_id, step_name;

-- 发布作业：按 publish_id 查看每次发布尝试
SELECT publish_id, step_name, status, is_current_publish, created_at
FROM public.publish_info
WHERE source_id = :source_id
ORDER BY created_at DESC, publish_id, step_name;
```

## 7. 后续待处理事项（均未实施）

以下为最初列出的待决策项；结合 2026-10-18 讨论的更新与建议见第 8–10 节。本文不预设最终结论。

### 7.1 采集作业是否需要“运行次数/尝试”维度

- **选项 A：保持现状**，仅补充文档与前端提示，说明“重跑不新建作业、步骤集合会累积”。
- **选项 B：保留 `job_id` 作为工作区，新增 `run_id`/作业运行记录**，每次重跑记录本次勾选步骤、执行者、结果，列表可按最近一次运行展示。
- **选项 C：重跑即新建 `job_id`**，历史最清晰，但与“同一作业复用既有产物/依赖”的语义冲突，需要处理产物复制或跨作业依赖。

需要确认：采集作业的“作业”究竟应表达工作区，还是应表达一次执行尝试；两者是否需要在数据模型上分开。

### 7.2 修改步骤后，步骤集合是否应收缩

- 当前列表展示历史并集；是否需要区分“本次勾选步骤”和“历史步骤”。
- 取消勾选的步骤，其 `job_info` 行、产物与状态应保留、标记为非活跃，还是删除/归档。
- 若引入运行维度，列表的“包含步骤”应取最近一次运行，还是仍取并集。

### 7.3 旧失败步骤与发布准入

- 重跑其他步骤成功后，旧失败步骤是否应继续阻塞 `job_status`/发布。
- 是否需要在重跑时显式提示“该作业仍有失败步骤未处理”，或提供清理/忽略机制。

### 7.4 发布作业是否需要复用 `publish_id`

- 当前每次发布新 ID；若需要“在同一次发布下追加步骤”，需定义：
  - API 是否接受现有 `publish_id`；
  - `publish_info` 的更新/追加语义与并发控制；
  - `is_current_publish` 的切换规则；
  - Neo4j provenance 与 CQL 产物是保留旧 ID 还是切换到新 ID；
  - 审计上如何区分“同一次发布的补充”和“新一次发布”。
- 若不复用，需接受“重新发布必新增记录”，并在 UI 上明确这是新尝试而非原位编辑。

### 7.5 API、数据模型与前端兼容

- 新增字段（如 `run_id`、步骤活跃标记）时的旧数据回填与默认值。
- 采集/发布列表接口的响应结构是否变化，旧前端是否仍可忽略新字段。
- 迁移脚本、回滚方案与只读核验口径。

### 7.6 验收建议

- 采集：新建作业生成新 `job_id`；重跑修改步骤后 `job_id` 不变；新增/取消步骤后的列表展示与 `job_status` 符合约定；旧失败步骤的阻塞行为符合约定。
- 发布：重新发布生成新 `publish_id`；旧记录保留；新记录的步骤集合等于本次勾选；`rel` 的 CQL/Neo4j provenance 使用新 ID；未勾选步骤的 current 不变。
- 两者均需覆盖失败、并发重跑、数据源禁用/删除等边界，并核对平台库行数据与前端展示一致。

## 8. 需求澄清（2026-10-18 讨论）

以下为讨论中确认的需求背景，用于约束后续设计；细节仍可能调整。

### 8.1 采集作业是长期工作区

- 采集作业是一个**长期存在的工作区**，不是一次性提交。
- 同一作业在实际生产中可能被**定期执行**（调度功能尚未开发），也可能由管理员**不定期重新执行**，因为源库数据持续变化。
- 采集作业整体是一个**相对固定的过程**：固定的是“对哪个数据源、按什么步骤采集”，变化的是每次运行的时间和数据。

### 8.2 运维追溯是明确需求

- 需要能够追溯采集与发布过程，属于运维工作。
- 至少要能回答：某次执行是谁触发的、什么时候、跑了哪些步骤、结果如何；某次发布读的是哪一版采集产物。
- 当前 `job_info` 只保留每步骤最新状态，`publish_info` 只记录发布尝试本身，二者都还不足以完整回答上述问题。

### 8.3 发布侧“重试/补充”的表达仍未确定

- 是否需要在发布记录上表达“这次是上次的重试/补充”，目前没有结论。
- 这个开放问题与本轮讨论的 ID 模型相关，但不是“是否复用 `publish_id`”的同义词；需要先区分发布动作的意图。

## 9. 讨论建议（讨论稿，非最终设计）

### 9.1 总体判断：两种 ID 不是二选一

- 采集 `job_id` 是**资源/工作区身份**：长期存在、可重复使用，目标是产物连续性与步骤依赖。
- 发布 `publish_id` 是**操作/尝试身份**：一次发布请求，短生命周期、不可变历史，目标是审计、current 指针与 provenance。
- 因此现状并非“一个合理、一个不合理”，而是**采集侧缺少与 `publish_id` 对称的“运行身份”**。
- 建议目标模型：
  - 采集：保留 `job_id`，新增 `run_id`；
  - 发布：保持每次发布新 `publish_id`，并记录本次发布依赖的采集运行/步骤版本。
- 不建议为了“统一”让采集每次重跑新建 `job_id`：产物目录、依赖校验、发布准入都按同一 `job_id` 组织，换 ID 会导致新作业没有旧产物，除非复制工作区或允许跨作业依赖，代价和风险都明显更大。

### 9.2 采集侧：作业定义 + 运行 + 步骤结果

建议把当前“`job_id` + `job_info` 状态行”扩展为三层：

| 层 | 标识 | 职责 |
| --- | --- | --- |
| 作业定义 | `job_id` | 长期工作区：数据源、固定步骤集合、调度配置、描述参数、启用状态 |
| 运行 | `run_id`（建议新增） | 一次执行：manual/scheduled/retry、触发人、开始结束时间、本次实际步骤、汇总状态 |
| 步骤结果 | `(run_id, step_name)`（建议新增） | 本次运行中该步骤的状态、耗时、错误摘要、产物指纹/清单 |

要点：

- `job_info` 可继续保留为“每步骤最新状态”的物化表，供发布准入和页面快速判断；历史追溯以运行表为准。
- 手动“重新执行”和未来的定时执行都创建一条 `run_id`，只是 `trigger_type` 不同（manual/scheduled），触发人分别为管理员或调度器。
- 管理员临时修改步骤时，建议作为**本次运行的覆盖**，不自动修改作业定义；若确实要改变固定流程，走显式的“编辑作业定义”操作并记录审计。
- 需要定义运行级并发策略：同一 `job_id` 已有运行在跑时，定时/手动触发应跳过、排队还是拒绝。
- 当前 `job_info.run_by_user_id` 没有实际写入路径；若要满足运维追溯，需要在运行/步骤写入时补齐触发人与执行者。

### 9.3 发布侧：保持新 `publish_id`，补来源快照

- 采集工作区会随定期/手动运行变化，发布是对某一时刻产物的一次快照操作；每次发布新 `publish_id` 仍然合理。
- 当前缺口：发布记录没有保存“本次发布读的是哪一次采集运行的哪一版步骤产物”。`publish_steps` 预检时已捕获每步骤 `run_markers`（步骤行 `updated_at`），但目前只用于并发复查，没有落库。
- 建议增加发布来源快照，例如：

  ```text
  publish_source_marker(publish_id, step_name, run_id, step_updated_at, artifact_fingerprint)
  ```

- 采用**按步骤**记录，而不是发布记录上只放一个 `run_id`：一次发布可能包含来自不同运行的步骤（例如 json 来自较早运行、rel 来自最近运行）。
- 写入时机：发布成功提交时，把本次勾选步骤对应的运行标识、步骤 `updated_at`、必要产物指纹一并写入。
- `rel` 已有内容指纹，可作为第一步；其他步骤是否增加内容指纹，可后续评估。
- 这样发布记录才能与采集运行历史形成闭环，而不是只能靠时间戳猜测。

### 9.4 发布前序关系：先拆意图，再决定是否记录

当前“重新发布”按钮混合了至少三种意图：

1. 重试失败步骤；
2. 重新发布当前产物（步骤不变，工作区已更新）；
3. 增补步骤（例如上次没发布 `rel`）。

建议：

- **`publish_id` 始终新生成，不复用**。
- 若要记录前序关系，增加可空字段 `previous_publish_id`，必要时再加 `relation_type`（如 `retry`/`supplement`/`refresh`/`initial`）。
- 不要因为“从旧发布入口进入”就自动标成 `retry`；只有用户明确选择“重试失败步骤”时才标 `retry`。否则“按旧步骤模板发布最新数据”会被误记为重试。
- 最小方案：先只加 `previous_publish_id`；等 UI 真正拆出不同动作后，再补 `relation_type`。
- 不建议复用 `publish_id` 来追加步骤：那会把不同时间、不同输入的写入混进同一次发布尝试，破坏 `is_current` 与 Neo4j/Milvus provenance 的可解释性。

### 9.5 目标追溯链路

```text
作业定义 job_id
  → 运行 run_id（manual/scheduled，触发人/时间）
  → 步骤结果与产物（artifact、updated_at/fingerprint）
  → 发布 publish_id（含 publish_source_marker）
  → PostgreSQL / Milvus / Neo4j（数据上带 publish_id）
```

### 9.6 建议的落地顺序

1. 采集运行层：手动重跑写入 `run_id` 和步骤结果，先解决“谁在何时跑了哪些步骤”的追溯；`job_info` 暂保留。
2. 作业定义：把固定步骤集合、调度配置从“历史步骤行并集”中独立出来，为定时执行做准备。
3. 发布来源快照：发布成功时写入 `publish_source_marker`，打通发布 → 采集运行的链路。
4. 发布前序关系：等 UI/产品明确“重试/补充/刷新”动作后，再加 `previous_publish_id`/`relation_type`。
5. 迁移与兼容：旧 `job_info` 无法还原逐次运行历史，可作为 legacy 状态保留；新运行从上线后开始记录。

### 9.7 取舍说明

- 若系统仅内部单用户、对逐次追溯要求不高，维持现状 + 文档/UI 提示是成本最低的方案。
- 但根据 §8.1/§8.2 的澄清（长期工作区、定期执行、运维追溯），**采集运行层是必要投入**；否则定时执行上线后，“这次定时跑了什么、发布的是哪次数据”仍然无法回答。
- 发布侧不需要改 ID 生成规则，只需要补来源快照；这是成本较低、收益明确的部分。

## 10. 仍需讨论的问题

以下问题尚未决策，建议在进入详细设计前逐项确认：

1. **运行并发策略**：同一 `job_id` 已有运行在跑时，定时/手动触发应跳过、排队还是拒绝？是否需要按步骤级还是作业级加锁？
2. **运行历史保留**：`run_id`/步骤结果保留多久？是否与采集产物、日志采用同一保留策略？是否需要归档？
3. **作业定义版本**：作业定义的步骤集合/调度配置修改是否需要版本历史？运维是否需要追溯“固定流程何时被谁改过”？
4. **临时覆盖边界**：管理员在重跑页修改步骤，是否允许只影响本次运行？是否需要在 UI 上区分“仅本次”与“更新作业定义”？
5. **发布来源快照粒度**：按步骤记录 `run_id` + `updated_at` 是否足够？是否为所有步骤增加产物内容指纹？
6. **发布前序关系**：是否需要 `previous_publish_id`？是否值得引入 `relation_type`？UI 上如何区分重试/补充/刷新？
7. **旧数据迁移**：现有 `job_info`/`publish_info` 如何标记为 legacy？是否需要为历史作业补一条合成运行记录，还是明确“上线前的运行不可追溯”？
8. **用户与调度器身份**：手动运行的触发人、定时运行的调度器身份如何记录？现有 `run_by_user_id`/`created_by_user_id` 未写入，需要补齐。
9. **术语与展示**：是否在 UI/API 中明确区分工作区（`job_id`）、运行（`run_id`）、发布尝试（`publish_id`）？列表默认展示作业还是运行？
10. **`job_info` 的去留**：保留为最新状态物化表，还是完全由运行表派生？两者并存时如何保证一致性？
11. **定时运行与发布的关系**：定时执行成功后是否需要自动发布，还是仍由人工确认发布？

## 11. 关键代码位置

| 主题 | 位置 |
| --- | --- |
| 采集重跑入口 | [CollectionJobsView.vue:191](../frontend/src/views/CollectionJobsView.vue) |
| 采集重跑页读取 `job_id` | [CreateCollectionJobView.vue:62](../frontend/src/views/CreateCollectionJobView.vue)、[:448](../frontend/src/views/CreateCollectionJobView.vue)、[:531-533](../frontend/src/views/CreateCollectionJobView.vue) |
| 采集请求携带 `job_id` | [dataSources.ts:210-232](../frontend/src/api/dataSources.ts) |
| 采集请求模型 | [schemas.py:329-335](../metaweave/backend/api/schemas.py) |
| 采集路由 | [data_sources.py:96-110](../metaweave/backend/api/routers/data_sources.py) |
| 采集执行与 `job_id` 复用 | [collection_run.py:181-289](../metaweave/backend/jobs/collection_run.py) |
| 新工作区 ID 生成 | [db_step_service.py:95-96](../metaweave/backend/data_sources/db_step_service.py)、[ddl_step_service.py:242-243](../metaweave/backend/data_sources/ddl_step_service.py) |
| `job_info` upsert 与状态汇总 | [repository.py:77-153](../metaweave/backend/jobs/repository.py) |
| 采集作业列表聚合 | [jobs/listing.py:34-70](../metaweave/backend/jobs/listing.py) |
| 采集步骤状态模型 | [models.py:66-90](../metaweave/backend/persistence/models.py) |
| 采集重跑测试 | [test_collection_job_api.py:465-485](../metaweave/tests/unit/test_collection_job_api.py) |
| 发布重跑入口 | [PublishJobsView.vue:176-188](../frontend/src/views/PublishJobsView.vue) |
| 发布页读取旧 `publish_id` | [PublishView.vue:211-220](../frontend/src/views/PublishView.vue) |
| 发布请求只带 `steps` | [PublishView.vue:256](../frontend/src/views/PublishView.vue)、[dataSources.ts:102-109](../frontend/src/api/dataSources.ts) |
| 发布请求模型 | [schemas.py:535-538](../metaweave/backend/api/schemas.py) |
| 发布路由 | [publishing.py:59-72](../metaweave/backend/api/routers/publishing.py) |
| 发布 ID 生成 | [publish_service.py:1701](../metaweave/backend/publishing/publish_service.py) |
| 发布预检捕获步骤 marker | [publish_service.py:1620-1622](../metaweave/backend/publishing/publish_service.py) |
| 发布记录模型 | [models.py:173-212](../metaweave/backend/persistence/models.py) |
| 发布记录仓储 | [publishing/repository.py:27-62](../metaweave/backend/publishing/repository.py) |
| 发布列表聚合 | [publishing/listing.py:46-78](../metaweave/backend/publishing/listing.py) |
| `rel` 发布 provenance | [publish_service.py:1258](../metaweave/backend/publishing/publish_service.py) |
| 发布 ID 测试 | [test_publish_service.py:784-788](../metaweave/tests/unit/test_publish_service.py) |
