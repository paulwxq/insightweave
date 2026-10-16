# doc 19.3 平台时间戳统一 UTC 存储与北京时间展示 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按定稿的 doc 19.3 实施平台库时间戳统一为 `timestamptz`（微秒）、API 统一 UTC 六位小数 `Z`、Web UI 统一北京时间六位小数展示、`generated_timestamp_local` 改名 `generated_timestamp`、产物内嵌时间统一 UTC 六位 `Z`。

**Architecture:** 唯一共用 UTC 文本函数 `metaweave/utils/time_utils.py::utc_isoformat`（doc §2.2/§3：产物与后端 API、MD 运行标识共同复用，后端不另写同构实现）；先落产物/API 格式与函数，再改名与加载器契约，随后执行平台库 DDL（DROP+ADD，独立脚本），最后前端共享展示函数与全量回归、文档同步。

**Tech Stack:** Python 3.12 + SQLAlchemy 2 + psycopg3 + FastAPI；pytest（假会话 + monkeypatch `platform_session`）；Vue 3 + TypeScript + vue-tsc/vite。

**Spec:** `docs/data_gov/19.3_平台时间戳统一UTC存储与北京时间展示改造计划.md`（定稿 2026-10-05，五轮评审）。执行每个任务前先读 spec 对应章节。

## Global Constraints

- **不提交**：本仓惯例是全部改动完成后交用户审阅再提交（spec 之外的项目约定）；本计划不含 commit 步骤，任务完成以「测试通过 + 台账记录」为准。
- **直接在当前工作区（master、含未提交的前序改动）实施**：沿用仓库既有惯例；不改动前序会话遗留的未提交文件（doc 17、comment_source 相关、frontend views）之外的无关内容。
- 唯一 UTC 文本函数 `metaweave/utils/time_utils.py::utc_isoformat`（后端导入复用）：带时区检查（`tzinfo is None` 或 `utcoffset() is None` → `ValueError`）→ `astimezone(timezone.utc)` → `isoformat(timespec="microseconds")` → `+00:00` 替换为 `Z`；禁用 `replace(tzinfo=...)` 补标签。
- 平台库 DDL 只处理 `table_profile.generated_timestamp_local`：同事务 DROP 旧列 + ADD `generated_timestamp timestamptz(6) NULL`，无默认值、不迁移旧测试值（spec §6）。
- API 字段一律 `snake_case`；`updated_at` 不新增到 API；JSON 产物内键名 `generated_timestamp` 不变（只变库列/ORM/API 字段名）。
- 全量测试不得回归（当前 1964+ passed，含真库 skipif 用例）；`ruff check`、`black`（如有配置）干净。

## Review Focus

1. naive 输入在共用函数中必须抛 `ValueError`，不得静默按 UTC 处理（spec §2.2/§3）。
2. 带 `+08:00` 偏移的输入必须 `astimezone` 换算为同一瞬间，不得删偏移拼 `Z`（spec §2.2，验收 §5）。
3. 微秒为零时 API 与产物仍输出 `.000000Z`；前端仍显示 `.000000`（spec §5）。
4. 前端共享函数：从原始字符串拼接六位小数（Date 只存毫秒）；`Asia/Shanghai` 固定时区；非法/解析失败返回 `—`（spec §4）。
5. 改名完整性：代码/API/前端不再残留 `generated_timestamp_local`；旧的 `_parse_local_timestamp` 不复存在。
6. 真库验收：列类型 SQL 10 行全 PASS；旧列已删 SQL 返回 PASS。

## 文件结构总览

| 层 | 新建 | 修改 |
| --- | --- | --- |
| metaweave 产物 | `metaweave/utils/time_utils.py` | `core/metadata/db_step.py`、`core/metadata/models.py`、`core/metadata/formatter.py`、`core/relationships/writer.py`、`core/cql_generator/writer.py`、`core/sql_rag/validator.py` |
| backend | —（直接导入共用工具） | `assets/read_tx.py`、`assets/catalog.py`、`assets/object_catalog.py`、`assets/object_detail.py`、`data_sources/registration_service.py`、`data_sources/md_step_service.py`、`jobs/listing.py`、`jobs/home.py`、`jobs/collection_run.py`、`publishing/listing.py`、`publishing/home.py`、`publishing/publish_service.py`、`profile_loading/json_loader.py`、`persistence/models.py`、`api/object_schemas.py` |
| SQL | `docs/data_gov/sql/19.3_table_profile_timestamp.sql` | — |
| frontend | — | `api/format.ts`、`api/types.ts`、11 个 views/components |
| docs | — | doc 3/4/9/12/13/19.1/19.2、ui 原型 `10.数据库画像.html` |
| tests | `tests/unit/metaweave/test_time_utils.py` | 见各任务 |

---

### Task 1: 唯一共用 UTC 文本函数 + 6 处产物 + 2 处生成侧

**Spec:** §2.2（产物侧函数、db_step/models 改动）、§2.3（六处产物表）。

**Files:**
- Create: `metaweave/utils/time_utils.py`
- Modify: `metaweave/core/metadata/db_step.py:253-257`、`metaweave/core/metadata/models.py:212-214`、`metaweave/core/metadata/formatter.py:259-261`、`metaweave/core/relationships/writer.py:205`、`:595`、`metaweave/core/cql_generator/writer.py:252`、`:403`、`metaweave/core/sql_rag/validator.py:485`
- Test: `tests/unit/metaweave/test_time_utils.py`（新增）；更新 `tests/unit/test_generated_timestamp.py`、`tests/unit/metaweave/metadata/test_formatter_ddl_header.py`、`tests/unit/metaweave/relationships/`、`tests/unit/metaweave/cql_generator/`、`tests/unit/metaweave/sql_rag/`、`tests/unit/test_db_step.py` 中受格式影响的断言

**Interfaces（后续任务依赖）:**
```python
# metaweave/utils/time_utils.py
def utc_isoformat(value: datetime) -> str: ...   # aware→UTC 六位微秒 Z；naive → ValueError
```

- [ ] **Step 1: 写失败测试** `tests/unit/metaweave/test_time_utils.py`：
  - `utc_isoformat(datetime(2026,10,5,6,3,7,8123,tzinfo=timezone.utc)) == "2026-10-05T06:03:07.008123Z"`
  - `utc_isoformat(datetime(2026,10,5,14,3,7,8123,tzinfo=timezone(timedelta(hours=8)))) == "2026-10-05T06:03:07.008123Z"`（换算非删偏移）
  - 微秒为零 → `"...07.000000Z"`
  - naive → `pytest.raises(ValueError)`；`tzinfo` 存在但 `utcoffset()` 为空同样拒绝
- [ ] **Step 2: 运行** `uv run pytest tests/unit/metaweave/test_time_utils.py -q` → **Expected:** 5 failed（模块不存在）
- [ ] **Step 3: 实现** `metaweave/utils/time_utils.py`（含 docstring 指向 doc 19.3 §2.2）
- [ ] **Step 4: 运行** → **Expected:** 5 passed
- [ ] **Step 5: 替换 8 个调用点**（db_step `collected_at`、models `generated_timestamp`、rel writer ×2、cql writer ×2、formatter DDL 头、sql_rag 验证时间），全部改为 `utc_isoformat(datetime.now(timezone.utc))`；DDL 头保持 `-- Generated: {值}` 单行
- [ ] **Step 6: 更新受影响断言**（逐个文件：旧断言改成六位微秒 `Z`；`test_generated_timestamp.py` 的 `not endswith("Z")` 反转）
- [ ] **Step 7: 运行相关测试 + 全量** `uv run pytest tests/unit -q` → **Expected:** 全绿

### Task 2: backend 复用唯一 UTC 文本函数 + 全部替换点（生成时间列除外）

**Spec:** §3 全部；§5 `finished_at`。

**Files:**
- Reuse: `metaweave.utils.time_utils.utc_isoformat`，不新增后端同构函数
- Modify: `backend/assets/read_tx.py`（删 `utc_isoformat`）、`backend/assets/catalog.py`、`backend/assets/object_catalog.py`、`backend/assets/object_detail.py`（本任务先把裸 `.isoformat()` 换成 `utc_isoformat`，键名改在 Task 3）、`backend/data_sources/registration_service.py`、`backend/data_sources/md_step_service.py`（`_marker_iso`）、`backend/jobs/listing.py`、`backend/jobs/home.py`、`backend/jobs/collection_run.py`、`backend/publishing/listing.py`、`backend/publishing/home.py`、`backend/publishing/publish_service.py`
- Test: 共用函数用例由 Task 1 覆盖；更新 `test_object_catalog.py`、`test_object_detail.py`、`test_asset_catalog.py`、`test_web_data_sources.py`、`test_collection_job_listing.py`、`test_collection_home.py`、`test_publish_job_listing.py`、`test_publish_home.py`、`test_publish_service.py`、`test_source_activation.py`、`test_meta_loader_db.py`、`test_md_step_service.py`、`test_asset_objects_api.py` 等受格式影响断言

- [ ] **Step 1: 核对共用函数用例**，复用 Task 1 的 UTC/偏移、六位微秒及无时区拒绝用例，不复制函数或测试
- [ ] **Step 2: 导入共用函数** `from metaweave.utils.time_utils import utc_isoformat`，用于全部后端时间输出
- [ ] **Step 3: 删除分散实现**，不保留后端同构函数
- [ ] **Step 4: 验证调用路径**，确认接口与 MD 清单实际调用共用函数
- [ ] **Step 5: 替换全部调用点**（read_tx 删除旧函数；`object_catalog.py:70-72`、`object_detail.py:86-88` 的裸 `.isoformat()` 换 `utc_isoformat`；`_marker_iso` 接收数据库的带时区 `datetime` 并直接调用 `utc_isoformat`，移除字符串原样返回分支，成功路径夹具同样使用 `datetime`；两个 `_finished_at` 改 `utc_isoformat(datetime.now(timezone.utc))`）
- [ ] **Step 6: 更新受影响断言**（秒精度 `Z` → 六位微秒 `Z`；首页移除 `_as_utc`，按 aware 时间点直比——生产输入（`now(timezone.utc)` 与 timestamptz 列）语义不变，naive 入参显式失败、不静默按 UTC）
- [ ] **Step 7: 全量** `uv run pytest tests/unit -q` → **Expected:** 全绿

### Task 3: `generated_timestamp` 改名 + 加载器契约

**Spec:** §2.1、§2.2.4；§3 字段清单。

**Files:**
- Modify: `backend/persistence/models.py:237-240`（`generated_timestamp` + `DateTime(timezone=True)`）、`backend/profile_loading/json_loader.py`（校验翻转要求带时区；`_parse_local_timestamp`→`_parse_aware_timestamp`：空值保留、校验 `tzinfo/utcoffset`、`astimezone(utc)`；键名 `:470`）、`backend/api/object_schemas.py:27,100`、`backend/assets/object_catalog.py:70-72`、`backend/assets/object_detail.py:86-88`（键名）
- Test: `tests/unit/test_json_publish_loader.py`（tz 校验反转 + 改名 + `_parse_aware_timestamp`）、`test_table_profile_models.py`、`test_object_catalog.py`、`test_object_detail.py`、`test_asset_objects_api.py`

- [ ] **Step 1: 写失败测试**（json_loader：naive `generated_timestamp` 拒绝、`+08:00` 接受并入库为 UTC 同瞬间；模型/接口字段名为 `generated_timestamp`）
- [ ] **Step 2: 运行** `uv run pytest tests/unit/test_json_publish_loader.py -q` → **Expected:** 新用例 failed
- [ ] **Step 3: 实现改名与校验翻转**
- [ ] **Step 4: 更新既有引用**（`grep -rn generated_timestamp_local` 到 0；`_parse_local_timestamp` 到 0）
- [ ] **Step 5: 全量** `uv run pytest tests/unit -q` → **Expected:** 全绿

### Task 4: 平台库 SQL 脚本 + 执行 + 真库验收

**Spec:** §5 两个验收 SQL、§6 第一段与 DDL 块。

**Files:**
- Create: `docs/data_gov/sql/19.3_table_profile_timestamp.sql`（BEGIN; DROP; ADD; COMMIT; 注释注明执行前置与验收）

- [ ] **Step 1: 写脚本**（内容与 spec §6 一致）
- [ ] **Step 2: 执行脚本**（经项目 platform_session；执行前 `pg_attribute` 确认旧列存在）
- [ ] **Step 3: 验收 SQL 1**（spec §5 第一个查询）→ **Expected:** 10 行全 `PASS`
- [ ] **Step 4: 验收 SQL 2**（旧列已删）→ **Expected:** `PASS`
- [ ] **Step 5: 真库读路径冒烟**：ORM 读一行 `TableProfile.generated_timestamp` → 重发布前为 `None`；`object_catalog._list_item` 对真实行返回 `generated_timestamp: None`

### Task 5: 前端共享展示函数 + 11 处替换 + 类型改名

**Spec:** §4 全部。

**Files:**
- Modify: `frontend/src/api/format.ts`、`views/DataSourcesView.vue`、`DataSourceDetailView.vue`、`DataAssetsView.vue`、`DatabaseProfileView.vue`、`ObjectDetailView.vue`、`CollectionJobsView.vue`、`SelectPublishJobView.vue`、`PublishJobsView.vue`、`CreateCollectionJobView.vue`、`PublishView.vue`、`components/SourceWorkBrowser.vue`、`api/types.ts:294,365`

- [ ] **Step 1: 重写 `displayDate`**：格式校验 `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$` → 截三位副本给 `new Date` → `Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',hourCycle:'h23',...})` 取年月日时分秒 → 拼接原串六位小数；非法/解析失败 → `'—'`
- [ ] **Step 2: 替换 11 处本地函数**（空值文案保持各页原样：ObjectDetailView 缺失仍“未记录”；其余 `'—'`）；移除浏览器偏移/UTC 后缀/`yyyyMMdd` 格式
- [ ] **Step 3: 类型改名** `types.ts` ×2 + `ObjectDetailView.vue` 注释与引用
- [ ] **Step 4: 验证** `cd frontend && npm run typecheck && npm run build` → **Expected:** 零错误

### Task 6: 全量回归

- [ ] **Step 1:** `uv run pytest tests/ -q` → **Expected:** 全绿（真库用例应通过，含新列）
- [ ] **Step 2:** `uv run ruff check metaweave backend tests` + `uv run black --check`（若项目配置）→ **Expected:** 干净
- [ ] **Step 3:** 前端 build 复跑 → 通过；修正任何遗漏

### Task 7: 设计文档同步（spec §6 两张表）

**Files:** `docs/data_gov/3_...md`（113/166/254 + 119 示例）、`4_...md`（92/170）、`9_...md`（37）、`12_...md`（183/264 + 178 示例）、`13_...md`（64/261/281/501 + 291/479/486 + 533/546）、`19.1_...md`（62/82/119）、`19.2_...md`（§3.1/§5/§6.1-§6.2/§6.3/§7.2-§7.3/§8.1.1/§8 示例）、`ui/数据源管理到发布/10.数据库画像.html`（572 行）

- [ ] **Step 1:** 按 spec §6 表格逐条同步（19.2 的建表文本改 `generated_timestamp timestamptz(6)`；原型示例改 `2026-09-22 21:58:39.000000`）
- [ ] **Step 2:** `grep -rn "秒精度\|固定到秒" docs/data_gov/` 复核无残留冲突表述
