# doc 19.2 对象画像数据字典与数据画像展示 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按定稿的 doc 19.2 实施采集侧样例块当前格式修复、平台库两张画像表、受管 JSON 发布、对象列表/详情/DDL 读取 API 与前端页面。

**Architecture:** 三个层次依次落地：① metaweave core 的 JSON 注释块统一编码/解码（`*` 转义、`data` 拆包修复、旧标记拒绝）；② backend 的 ORM 两表 + `profile_loading/json_loader.py` 发布加载器 + `publish_service` 扩展 `json` 步骤；③ `backend/assets` 三个只读服务（REPEATABLE READ + READ ONLY 事务）+ FastAPI 路由 + Vue 页面。状态枚举、空值语义、时间契约一律以 doc 19.2 §8.1.1/§8.2 为准。

**Tech Stack:** Python 3.12 + SQLAlchemy 2.0.45 + psycopg3 + FastAPI + Pydantic v2；pytest（假会话 + monkeypatch `platform_session` 模式）；Vue 3 + TypeScript + vue-tsc/vite。

**Spec:** `docs/data_gov/19.2_Web表画像数据字典与数据画像展示设计.md`（定稿 2026-10-03）。执行者实施每个任务前必须先读 spec 对应章节。

## Global Constraints

- 平台库表由管理员按 spec §6.2 建表 SQL 手工创建；应用不建表（spec §7.1）。
- 发布步骤白名单只扩为 `db|ddl|json`，不新增 `md`（spec §7.4）。
- 错误响应与日志不泄露样例值、DDL 原文、JSON 内容或凭据；只报文件名/字段路径/异常类别（spec §7.2）。
- API 字段一律 `snake_case`；`published_at/loaded_at` 返回 UTC ISO 8601（`Z` 结尾）；`generated_timestamp_local` 返回无 `Z` 的本地时间字符串（spec §8.1.1）。
- 本版不做鉴权（spec §8.4）；页面文案简体中文。
- 全量既有测试（1700+ 通过）不得回归；`ruff check` 与 `black` 干净；测试用 `uv run pytest`，前端用 `npm run typecheck || npx vue-tsc --noEmit` 与 `npm run build`。

## Review Focus

1. `keyword` 含 `%`、`_`、`\` 时按普通字符匹配，不得放大结果 → Task 7 测试。
2. 当前格式记录字段名恰为 `data`/`label` 时不得被当作旧包装拆解 → Task 2、9 测试。
3. 行注释或普通正文中的 `SAMPLE_RECORDS` 字样不触发旧标记拒绝 → Task 2、9 测试。
4. DDL 行存在但对象不在当前 JSON 集合：`object-ddl` 返回 404 `not_found` 而非 `not_published` → Task 10 测试。
5. 读事务隔离级别真实生效：同事务内 `SHOW transaction_isolation`=`repeatable read`、`SHOW transaction_read_only`=`on` → Task 10 集成测试。

## 文件结构总览

| 层 | 新建 | 修改 |
| --- | --- | --- |
| core | `metaweave/core/metadata/sample_block.py` | `formatter.py`、`ddl_loader.py` |
| persistence | — | `backend/persistence/models.py` |
| profile_loading | `backend/profile_loading/json_loader.py` | `backend/data_sources/json_step_service.py` |
| publishing | — | `publish_service.py`、`backend/data_sources/repository.py`、`backend/api/schemas.py` |
| assets | `backend/assets/read_tx.py`、`object_catalog.py`、`object_detail.py`、`ddl_display.py` | — |
| api | `backend/api/object_schemas.py`、`backend/api/routers/asset_objects.py` | `backend/api/errors.py`、`backend/api/main.py` |
| frontend | `views/ObjectListView.vue`、`views/ObjectDetailView.vue` | `api/types.ts`、`api/assets.ts`、`router/index.ts`、`PublishView.vue`、`DatabaseProfileView.vue`、`DataSourceDetailView.vue` |
| tests | `test_sample_block.py`、`test_table_profile_models.py`、`test_json_publish_loader.py`、`test_json_publish_service.py`、`test_object_catalog.py`、`test_object_detail.py`、`test_ddl_display.py`、`test_asset_objects_api.py`、`test_read_tx_isolation.py` | `test_ddl_loader.py`、`test_formatter_sample_records_block.py` |

---

### Task 1: sample_block 共享模块与 `*` 转义（生成侧）

**Spec:** §8.2 倒数第三段（序列化规则）、§2.2、§9.16。

**Files:**
- Create: `metaweave/core/metadata/sample_block.py`
- Modify: `metaweave/core/metadata/formatter.py`（`_build_sample_records_block` 约 771-797 行、`_build_view_object_metadata_block` 约 373-398 行）
- Test: `tests/unit/metaweave/metadata/test_sample_block.py`（新增）、`tests/unit/metaweave/metadata/test_formatter_sample_records_block.py`（扩充）

**Interfaces（后续任务依赖）:**
```python
# metaweave/core/metadata/sample_block.py
SAMPLED_RECORDS_MARKER = "SAMPLED_RECORDS"
LEGACY_SAMPLE_RECORDS_MARKER = "SAMPLE_RECORDS"

def serialize_comment_payload(payload: dict) -> str: ...
def build_comment_block(marker: str, payload: dict) -> str: ...
def find_sampled_records_blocks(content: str) -> list[re.Match]: ...     # 当前标记完整块，全部
def find_legacy_sample_records_block(content: str) -> re.Match | None: ...
def has_sampled_records_marker_hint(content: str) -> bool: ...          # 有起始标记文本但未必成块
def decode_current_records(payload: dict) -> list[dict]: ...            # 当前格式校验，data/label 按普通字段
```

- [ ] **Step 1: 写失败测试** `tests/unit/metaweave/metadata/test_sample_block.py`：

```python
"""JSON 注释块统一编码/解码（doc 19.2 §8.2）：\\u002a 转义与当前格式解码。"""

import json

import pytest

from metaweave.core.metadata.sample_block import (
    build_comment_block,
    decode_current_records,
    find_legacy_sample_records_block,
    find_sampled_records_blocks,
    has_sampled_records_marker_hint,
    serialize_comment_payload,
)

# 相邻与嵌套分隔符序列：两趟替换会静默改值的反例（spec §8.2）
TRICKY_VALUES = [
    "a*/b", "}*/", "/*", "/**/", "/*/", "*/*", "/*/*", "100%*/*",
    "\\u002a", "反斜杠\\值", "换行\n值", None, 42,
]


class TestSerializeCommentPayload:
    @pytest.mark.parametrize("value", TRICKY_VALUES)
    def test_no_bare_star_and_roundtrip(self, value):
        payload = {"k": value}
        serialized = serialize_comment_payload(payload)
        assert "*" not in serialized          # 无裸 *，即无 /* 与 */
        assert "/*" not in serialized and "*/" not in serialized
        assert json.loads(serialized) == payload  # 解码还原原值

    def test_star_in_key_also_escaped(self):
        serialized = serialize_comment_payload({"a*b": "c"})
        assert "*" not in serialized
        assert json.loads(serialized) == {"a*b": "c"}

    def test_build_block_wraps_marker(self):
        block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert block.startswith("/* SAMPLED_RECORDS\n")
        assert block.endswith("\n*/")
        assert "*" not in block.split("\n", 1)[1]  # 注释体内无裸 *


class TestBlockDetection:
    def test_current_block_found(self):
        content = "CREATE TABLE t (id int);\n" + build_comment_block(
            "SAMPLED_RECORDS", {"records": [{"id": "1*/"}]}
        )
        blocks = find_sampled_records_blocks(content)
        assert len(blocks) == 1
        assert json.loads(blocks[0].group("body")) == {"records": [{"id": "1*/"}]}

    def test_two_blocks_both_found(self):
        block = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert len(find_sampled_records_blocks(block + "\n" + block)) == 2

    def test_legacy_block_detected_and_not_confused_with_current(self):
        legacy = "/* SAMPLE_RECORDS\n{\"table\": \"public.t\", \"records\": []}\n*/"
        current = build_comment_block("SAMPLED_RECORDS", {"records": []})
        assert find_legacy_sample_records_block(legacy) is not None
        assert find_legacy_sample_records_block(current) is None
        assert find_sampled_records_blocks(legacy) == []
        assert len(find_sampled_records_blocks(current)) == 1

    def test_marker_hint_without_complete_block(self):
        assert has_sampled_records_marker_hint("/* SAMPLED_RECORDS\n{\"a\": 1")
        assert not has_sampled_records_marker_hint("-- 提到 SAMPLE_RECORDS 的行注释")

    def test_line_comment_text_is_not_marker_hint(self):
        content = "-- SAMPLED_RECORDS 字样\nCREATE TABLE t (id int);"
        assert not has_sampled_records_marker_hint(content)
        assert find_legacy_sample_records_block(content) is None


class TestDecodeCurrentRecords:
    def test_data_and_label_are_plain_fields(self):
        records = [{"data": "x", "label": "y"}, {"data": None, "label": 2}]
        assert decode_current_records({"records": records}) == records

    def test_rejects_non_list_and_non_dict(self):
        with pytest.raises(ValueError):
            decode_current_records({"records": "x"})
        with pytest.raises(ValueError):
            decode_current_records({"records": ["x"]})

    def test_missing_records_rejected(self):
        with pytest.raises(ValueError):
            decode_current_records({})
```

- [ ] **Step 2: 运行确认失败**：`uv run pytest tests/unit/metaweave/metadata/test_sample_block.py -v` → FAIL（ModuleNotFoundError）。
- [ ] **Step 3: 实现** `metaweave/core/metadata/sample_block.py`：

```python
"""SAMPLED_RECORDS / OBJECT_METADATA JSON 注释块的统一编码与解码（doc 19.2 §8.2）。

生成、DDL/JSON 步骤回读与 Web 展示共用当前格式规则；旧 SAMPLE_RECORDS
标记只检测并拒绝，不保留旧格式解码或 {label, data} 包装分支。
"""

import json
import re

SAMPLED_RECORDS_MARKER = "SAMPLED_RECORDS"
LEGACY_SAMPLE_RECORDS_MARKER = "SAMPLE_RECORDS"

_current_block_re = re.compile(
    r"/\*\s*SAMPLED_RECORDS\s*(?P<body>\{.*?\})\s*\*/", re.DOTALL
)
# 旧标记不能被当前标记的前缀误匹配：SAMPLE 之后是 "_" 而非 "D"。
_legacy_block_re = re.compile(
    r"/\*\s*SAMPLE_RECORDS\s*(?P<body>\{.*?\})\s*\*/", re.DOTALL
)
_marker_hint_re = re.compile(r"/\*\s*SAMPLED_RECORDS(?![A-Z_])")
_legacy_marker_hint_re = re.compile(r"/\*\s*SAMPLE_RECORDS(?![A-Z_])")


def serialize_comment_payload(payload: dict) -> str:
    """json.dumps 后把每个 `*` 写成 JSON 等价的 `\\u002a`。

    JSON 结构字符不含 `*`，单趟全文替换只作用于字符串值且不会产生新
    `*`；序列化正文因此不含裸 `*`，既不会出现 `/*` 也不会出现 `*/`，
    SQL 注释边界与非贪婪正则都不会被内容破坏；json.loads 解码回原值。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2).replace(
        "*", "\\u002a"
    )


def build_comment_block(marker: str, payload: dict) -> str:
    return "\n".join([f"/* {marker}", serialize_comment_payload(payload), "*/"])


def find_sampled_records_blocks(content: str) -> list:
    return list(_current_block_re.finditer(content))


def find_legacy_sample_records_block(content: str):
    return _legacy_block_re.search(content)


def has_sampled_records_marker_hint(content: str) -> bool:
    """块注释形式出现的当前/旧标记起始文本（不含行注释等普通文字）。"""
    return bool(_marker_hint_re.search(content) or _legacy_marker_hint_re.search(content))
```

注意 `_marker_hint_re` 的负向前瞻 `(?![A-Z_])`：`/* SAMPLED_RECORDS_EXTRA` 不算标记，而 `/* SAMPLED_RECORDS` 后跟空白/JSON 都算。旧标记同理。

- [ ] **Step 4: formatter 两个块改用共用函数**（`metaweave/core/metadata/formatter.py`）：

`_build_view_object_metadata_block` 末两行替换：
```python
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        return ["/* OBJECT_METADATA", *serialized.splitlines(), "*/"]
```
改为：
```python
        return build_comment_block("OBJECT_METADATA", payload).splitlines()
```
`_build_sample_records_block` 末两行替换：
```python
        json_block = json.dumps(payload, ensure_ascii=False, indent=2)
        return "\n".join(["/* SAMPLED_RECORDS", json_block, "*/"])
```
改为：
```python
        return build_comment_block(SAMPLED_RECORDS_MARKER, payload)
```
顶部 import 增加：`from metaweave.core.metadata.sample_block import (SAMPLED_RECORDS_MARKER, build_comment_block)`。

- [ ] **Step 5: 扩充 formatter 块测试**（`test_formatter_sample_records_block.py`）：对 `generate_ddl` 产出的 DDL 断言——样例值取 `TRICKY_VALUES` 全集时，样例块与 View/MV 的 `OBJECT_METADATA` 块注释体均无裸 `*`，且用 `find_sampled_records_blocks` 解出的 payload 与写入值相等；View 场景给 `object_comment="含 */ 与 /* 的注释"`、某列 `comment="c*/"` 断言 `OBJECT_METADATA` 块完整（`OBJECT_METADATA_PATTERN` 能解出原文注释）。
- [ ] **Step 6: 运行** `uv run pytest tests/unit/metaweave/metadata/test_sample_block.py tests/unit/metaweave/metadata/test_formatter_sample_records_block.py tests/unit/metaweave/test_ddl_loader.py -v`（ddl_loader 现有对旧文件回读的断言可能失败，Task 2 处理；本步只确认新测试过、记录失败清单）。
- [ ] **Step 7: Commit** `feat: JSON 注释块统一 * 转义与共享编码模块（doc 19.2 §8.2）`。

---

### Task 2: 回读侧当前格式与旧标记拒绝（ddl_loader + formatter 回读）

**Spec:** §8.2 第二类缺陷段、倒数第二段；§9.5/§9.16。

**Files:**
- Modify: `metaweave/core/metadata/ddl_loader.py`（`SAMPLE_BLOCK_PATTERN` 29-33 行、`_parse_sample_records` 258-284 行）
- Modify: `metaweave/core/metadata/formatter.py`（`_extract_sample_records_from_ddl` 约 623-694 行）
- Test: `tests/unit/metaweave/test_ddl_loader.py`（调整 + 新增）、`tests/unit/metaweave/metadata/test_formatter_sample_records_block.py`（新增回读用例）

**Interfaces:**
- Consumes: Task 1 的 `sample_block` 全部函数。
- Produces: `DDLLoader._parse_sample_records` 遇旧标记抛 `DDLLoaderError`；`OutputFormatter._extract_sample_records_from_ddl` 遇旧标记抛 `ValueError`（由调用方按对象失败处理），无块时返回 `None`，数字转换行为保留。

- [ ] **Step 1: 写失败测试**（`test_ddl_loader.py` 新增；旧的 `SAMPLE_RECORDS`/`{label, data}` 兼容断言改为拒绝断言）：

```python
class TestSampleRecordsCurrentFormat:
    def _ddl(self, records_json: str) -> str:
        return (
            "CREATE TABLE public.orders (order_id integer);\n"
            f"/* SAMPLED_RECORDS\n{records_json}\n*/"
        )

    def test_current_records_direct_and_data_is_plain_field(self, tmp_path):
        path = tmp_path / "orders.public.orders.sql"
        path.write_text(self._ddl(
            '{"object_type":"table","object_name":"public.orders",'
            '"records":[{"data":"1","label":"x"}]}'
        ), encoding="utf-8")
        records = DDLLoader(tmp_path).parse_content(path.read_text(), path).sample_records
        assert records == [{"data": "1", "label": "x"}]   # 不拆包、不跳过、不转数字

    def test_legacy_marker_rejected(self, tmp_path):
        path = tmp_path / "orders.public.orders.sql"
        path.write_text(
            "CREATE TABLE public.orders (order_id integer);\n"
            '/* SAMPLE_RECORDS\n{"table":"public.orders","records":[{"label":"a","data":{"id":1}}]\n*/',
            encoding="utf-8",
        )
        with pytest.raises(DDLLoaderError):
            DDLLoader(tmp_path).parse_content(path.read_text(), path)

    def test_line_comment_with_marker_word_not_rejected(self, tmp_path):
        path = tmp_path / "orders.public.orders.sql"
        content = (
            "-- 历史说明：SAMPLE_RECORDS 已废弃\n"
            "CREATE TABLE public.orders (order_id integer);\n"
        )
        records = DDLLoader(tmp_path).parse_content(content, path).sample_records
        assert records == []

    def test_generated_file_roundtrip_with_tricky_values(self, tmp_path):
        # 用 formatter 生成含 */ 与 /* 样例值的 DDL，再完整回读
        from metaweave.core.metadata.formatter import OutputFormatter
        from tests.unit.metaweave.metadata.test_sample_block import TRICKY_VALUES
        ...  # 构造 TableMetadata + DataFrame 样例，generate_ddl 后 parse_content
        ...  # 断言 sample_records 的键值与写入 DataFrame 的原值一致
```

（`test_generated_file_roundtrip_with_tricky_values` 的完整写法参照该文件既有的 formatter↔loader 往返用例，把样例值换成 `TRICKY_VALUES`。）

- [ ] **Step 2: 运行确认失败**：`uv run pytest tests/unit/metaweave/test_ddl_loader.py -v`。
- [ ] **Step 3: 实现** `ddl_loader.py`：

```python
# 删除模块级 SAMPLE_BLOCK_PATTERN（29-33 行），改为：
from metaweave.core.metadata.sample_block import (
    decode_current_records,
    find_legacy_sample_records_block,
    find_sampled_records_blocks,
    has_sampled_records_marker_hint,
)

    def _parse_sample_records(self, content: str, ddl_path: Path) -> List[Dict]:
        if find_legacy_sample_records_block(content):
            raise DDLLoaderError(f"检测到旧 SAMPLE_RECORDS 样例块: {ddl_path}")
        blocks = find_sampled_records_blocks(content)
        if not blocks:
            if has_sampled_records_marker_hint(content):
                raise DDLLoaderError(f"样例块边界不完整: {ddl_path}")
            logger.warning(f"未在 DDL 中找到样例数据注释块: {ddl_path}")
            return []
        if len(blocks) > 1:
            raise DDLLoaderError(f"存在多个 SAMPLED_RECORDS 样例块: {ddl_path}")
        body = blocks[0].group("body").strip()
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("样例块必须是对象")
            return [dict(record) for record in decode_current_records(payload)]
        except (json.JSONDecodeError, ValueError) as exc:
            raise DDLLoaderError(f"样例数据解析失败 ({ddl_path}): {exc}") from exc
```

（多块/边界不完整在此处也拒绝——回读入口是发布链的守门；显示层 Task 9 另有同规则实现。）

- [ ] **Step 4: 实现** `formatter.py:_extract_sample_records_from_ddl`——重写为：

```python
    def _extract_sample_records_from_ddl(self, metadata) -> Optional[Dict[str, Any]]:
        """从 DDL 文件提取当前格式样例记录；旧标记按输入错误上抛。"""
        try:
            filename = self._get_filename(metadata, "sql")
            ddl_file = self.ddl_dir / filename
            if not ddl_file.exists():
                return None
            content = ddl_file.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"读取 DDL 失败 ({metadata.full_name}): {type(e).__name__}")
            return None
        # 旧标记与畸形块是输入错误，不吞成“无块”（doc 19.2 §8.2）
        if find_legacy_sample_records_block(content) or has_sampled_records_marker_hint(content) and not find_sampled_records_blocks(content):
            raise ValueError("DDL 样例块为旧格式或边界不完整")
        blocks = find_sampled_records_blocks(content)
        if not blocks:
            return None
        payload = json.loads(blocks[0].group("body"))
        records = decode_current_records(payload)   # data/label 按普通字段
        if not records:
            return None
        # 保留现有“数字样式字符串转数字”行为（spec §8.2 明示不变）
        converted = []
        for record in records[: self.sample_record_options["count"]]:
            converted.append({
                key: (self._to_number(value) if isinstance(value, str) else value)
                for key, value in record.items()
            })
        return {
            "sample_method": payload.get("sample_method", "limit"),
            "sample_size": len(converted),
            "total_rows": metadata.row_count,
            "records": converted,
        }

    @staticmethod
    def _to_number(value: str):
        try:
            return int(value) if "." not in value else float(value)
        except (ValueError, TypeError):
            return value
```

注意条件表达式加括号：`if find_legacy_sample_records_block(content) or (has_... and not blocks)`——先取 `blocks = find_sampled_records_blocks(content)` 再组合条件，实现时写成三个明确 if，不要一行与或混合。

- [ ] **Step 5: 运行** `uv run pytest tests/unit/metaweave/ tests/unit/test_ddl_step_service.py tests/unit/test_json_step_service.py -v`，修掉旧断言（旧格式兼容用例改为拒绝用例）。
- [ ] **Step 6: Commit** `feat: DDL 回读仅认当前样例块格式并拒绝旧标记（doc 19.2 §8.2）`。

---

### Task 3: 平台库两表 ORM 映射

**Spec:** §6.2、§6.3。

**Files:**
- Modify: `backend/persistence/models.py`（文件末尾追加）
- Test: `tests/unit/test_table_profile_models.py`（新增）

**Interfaces（后续任务依赖）:**
- Produces: `TableProfile`、`TableColumnProfile` ORM 类；列名与 §6.2 建表 SQL 逐字一致；`table_column_profile_object_order_idx` 唯一索引与 `ON DELETE CASCADE` 外键在 `__table_args__` 声明。

- [ ] **Step 1: 写失败测试**：

```python
"""table_profile / table_column_profile ORM 映射与 doc 19.2 §6.2 对照。"""

from backend.persistence.models import TableColumnProfile, TableProfile


def _columns(model):
    return {column.name for column in model.__table__.columns}


def test_table_profile_columns_match_ddl():
    cols = _columns(TableProfile)
    expected = {
        "source_id", "schema_name", "object_name", "db_name", "object_type",
        "publish_id", "job_id", "metadata_version", "generated_timestamp_local",
        "loaded_at", "object_comment", "object_comment_source", "total_rows",
        "column_count", "sample_method", "sample_count", "has_table_profile",
        "table_category", "category_confidence", "classification_source",
        "classification_reason", "classification_basis", "rule_table_category",
        "rule_confidence", "rule_inference_basis", "primary_key_name",
        "primary_key_columns", "foreign_key_count", "index_count",
        "constraint_count", "foreign_keys", "unique_constraints", "indexes",
        "unique_column_sets", "json_sample_records",
    }
    assert cols == expected
    pk = [c.name for c in TableProfile.__table__.primary_key.columns]
    assert pk == ["source_id", "schema_name", "object_name"]


def test_table_column_profile_columns_constraints_and_index():
    cols = _columns(TableColumnProfile)
    expected = {
        "source_id", "schema_name", "object_name", "publish_id", "column_name",
        "ordinal_position", "data_type", "is_nullable", "column_default",
        "column_comment", "comment_source", "semantic_role", "semantic_confidence",
        "semantic_basis", "null_count", "null_rate", "unique_count",
        "uniqueness", "sample_min", "sample_max", "value_distribution",
    }
    assert cols == expected
    indexes = {idx.name: idx for idx in TableColumnProfile.__table__.indexes}
    order_idx = indexes["table_column_profile_object_order_idx"]
    assert order_idx.unique is True
    fks = list(TableColumnProfile.__table__.foreign_key_constraints)
    assert len(fks) == 1 and fks[0].ondelete == "CASCADE"
```

- [ ] **Step 2: 运行确认失败**（ImportError）。
- [ ] **Step 3: 实现**——`models.py` import 区加 `ForeignKeyConstraint, Numeric`，文件末尾追加（列清单逐字对照 §6.2；数值比例 `Numeric(8, 6)`；`generated_timestamp_local` 用 `DateTime(timezone=False)`；`loaded_at` 与既有表一致 `server_default=text("now()")`；JSONB/ARRAY 沿用现有 import；`TableColumnProfile.__table_args__` 含 `ForeignKeyConstraint(["source_id","schema_name","object_name"], ["table_profile.source_id","table_profile.schema_name","table_profile.object_name"], ondelete="CASCADE")`、唯一 `Index("table_column_profile_object_order_idx", "source_id","schema_name","object_name","ordinal_position", unique=True)` 与 `{"schema": "public"}`）。
- [ ] **Step 4: 运行通过**；同时在本地平台库执行 §6.2 建表 SQL（dev 环境部署动作，读 `.env` 的 `PLATFORM_DB_*` 连接执行两段 CREATE TABLE + 唯一索引；已存在则跳过）。
- [ ] **Step 5: Commit** `feat: 平台库 JSON 画像两表 ORM 映射（doc 19.2 §6）`。

---

### Task 4: 受管 JSON 发布加载器 profile_loading/json_loader.py

**Spec:** §7.1-§7.3 全部、§6.3 空值语义。

**Files:**
- Create: `backend/profile_loading/json_loader.py`
- Modify: `backend/data_sources/json_step_service.py`（`read_managed_json_input` 改为薄包装）
- Test: `tests/unit/test_json_publish_loader.py`（新增）

**Interfaces:**
- Consumes: `resolve_json_output_dir`、`JsonLoader`、`MetadataDocument.from_dict`、Task 3 的两类 ORM 名（仅用于类型注释）。
- Produces:
```python
JSON_STEP = "json"

class JsonPublishInputError(ValueError): ...

def inspect_json_profile_context(session, source_id, job_id) -> tuple[str | None, datetime | None]
    # 与 inspect_ddl_definition_context 同语义：源存在/启用/工作区归属 + json 步骤行 (状态, updated_at)

def check_json_output_ready(source_id, job_id) -> bool
    # 目录、非空清单、清单/对象文件集合一致（复用 read_managed_json_documents 的清单校验，捕获错误返回 False）

def read_registration_snapshot(session, source_id) -> RegistrationSnapshot
    # dataclass(updated_at, db_name, selected_schemas)；源不存在抛 MetaLoadSourceNotFoundError、禁用抛 MetaLoadSourceDisabledError

def read_managed_json_documents(source_id, job_id, database_name) -> list[dict]
    # 清单/目录/符号链接/严格三段式/v3 身份校验后的完整文档内存快照（深拷贝 dict）；
    # 失败抛 JsonPublishInputError；错误消息只含文件名与类别

def build_profile_rows(source_id, job_id, publish_id, documents, selected_schemas)
    -> tuple[list[dict], list[dict]]
    # §7.2 静态校验 + 结构引用核对 + §7.3/§6.3 映射，返回 (table_rows, column_rows) 普通 dict
```

- [ ] **Step 1: 写失败测试** `tests/unit/test_json_publish_loader.py`——fixture 用 `tmp_path` 伪造 `output/<source>/<job>/json/`（monkeypatch `metaweave.utils.file_utils.get_project_root`），写入 §2 样本结构的最小 JSON 文档（复制真实样本 `orders.public.users.json` 的骨架）。用例清单（每个都是独立 test 函数，Assert 消息不含样例值）：
  - `test_inspect_context_mirrors_ddl`：假会话（SimpleNamespace）下源禁用抛 `MetaLoadSourceDisabledError`、步骤行缺失返回 `(None, None)`。
  - `test_read_documents_returns_validated_snapshots`：2 个合法文件 + manifest → 返回 2 份文档；`read_managed_json_input` 仍返回摘要列表（签名/返回值不变，新测试锁定）。
  - 清单/目录类：缺件、多件、manifest 缺失、symlink、非三段式文件名、混库 → `JsonPublishInputError`。
  - `test_empty_column_profiles_rejected`：某对象 `column_profiles={}` → 整批拒绝。
  - 校验类（每个构造畸形文档断言 `JsonPublishInputError`）：`ordinal_position=0`；重复序号；`is_nullable` 非布尔；`confidence=1.5`、`null_rate="x"`、bool 冒充计数；`inference_basis` 非字符串数组；`generated_timestamp="not-a-time"`；`value_distribution` 非对象；对象类型 `sequence`。
  - 结构引用类：主键 `columns` 引用不存在字段；外键 `source_columns=[]`；`source_columns/target_columns` 不等长；索引 `columns` 引用不存在字段；外键目标对象不在本批 → **允许**（正向断言）；字段序号空档 `1,3` → 允许；`key_expressions` 不校验 → 允许。
  - 映射类（对合法文档逐列断言）：`table_profile=null` → `has_table_profile=False`、全部分类/约束/索引/候选列与三个计数为 `None`；pk 显式 `null` → `primary_key_name=None, primary_key_columns=[]`；`physical_constraints` 键缺失 → `primary_key_columns=None`；`foreign_keys=[]` 显式空数组 → `[]` 且 `foreign_key_count=0`；节点缺失 → `None` 且计数 `None`；`constraint_count` 仅三部分齐全时计算；字段行 `publish_id` 与对象行一致；`sample_count`/`total_rows` 缺失 → `None`；`generated_timestamp` 缺失 → `None`；字段统计单项缺失逐项 `None`；`unique_column_sets` 原样 JSONB。
  - `test_schema_outside_selected_rejected`：`schema_name` 不在 `selected_schemas` → 拒绝。
- [ ] **Step 2: 运行确认失败**（ModuleNotFoundError）。
- [ ] **Step 3: 实现** `backend/profile_loading/json_loader.py`。骨架（完整实现按 spec §7.2/§7.3 逐条落；关键函数全文如下）：

```python
"""受管 JSON 画像产物读取与行映射（doc 19.2 §7）。

只读校验受管目录中的对象 JSON 并映射为平台库两表行；不自行提交。
"""

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from backend.data_sources.json_step_service import JSON_MANIFEST_NAME, ManagedJsonInputError
from backend.data_sources.repository import get_db_source
from backend.jobs.repository import assert_workspace_owner, get_step_row
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from metaweave.core.clean_targets import resolve_json_output_dir
from metaweave.core.metadata.metadata_document import MetadataDocument

JSON_STEP = "json"
_SUPPORTED_OBJECT_TYPES = {"table", "view", "materialized_view"}
_CONFIDENCE_KEYS = ...  # 见校验函数


class JsonPublishInputError(ValueError):
    """JSON 产物不满足画像两表的发布输入契约。"""


@dataclass(frozen=True)
class RegistrationSnapshot:
    updated_at: datetime
    db_name: str
    selected_schemas: tuple[str, ...]


def inspect_json_profile_context(session, source_id, job_id):
    # 与 ddl_loader.inspect_ddl_definition_context 逐行同构，step 名换成 JSON_STEP
    ...


def read_registration_snapshot(session, source_id) -> RegistrationSnapshot:
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
    return RegistrationSnapshot(
        updated_at=source.updated_at,
        db_name=source.db_name,
        selected_schemas=tuple(source.selected_schemas),
    )


def check_json_output_ready(source_id, job_id) -> bool:
    try:
        read_managed_json_documents(source_id, job_id, database_name="*")
        return True
    except ManagedJsonInputError:
        return False
    except Exception:
        return False
```

`check_json_output_ready` 的库名核对问题：预检时尚未读登记行库名。实现为：先尝试用登记库名——由调用方传入；签名定为 `check_json_output_ready(source_id, job_id, database_name: str)`，publish_service 在预检时从登记行取库名后调用。清单/目录/文件名结构性失败与库名无关，混库校验用登记库名。

```python
def read_managed_json_documents(source_id, job_id, database_name) -> list[dict]:
    """清单/目录/v3/身份校验后的文档快照（doc 19.2 §7.2）。

    结构规则与 read_managed_json_input 一致；身份与 v3 校验逐文件读取
    而非依赖 JsonLoader 枚举，避免其按库名前缀枚举忽略文件。
    """
    json_dir = resolve_json_output_dir(source_id, job_id)
    files = _read_manifest(json_dir)              # 沿用 json_step_service 的清单校验规则（抽取为共享私有函数）
    documents = []
    for name in files:
        path = json_dir / name
        if path.is_symlink() or not path.is_file():
            raise JsonPublishInputError(f"JSON 产物不是普通文件: {name}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise JsonPublishInputError(f"JSON 产物不可读或不是 UTF-8 JSON: {name}") from None
        try:
            MetadataDocument.from_dict(document)
        except MetadataDocumentError:
            raise JsonPublishInputError(f"JSON 产物未通过 v3 校验: {name}") from None
        info = document["object_info"]
        if info.get("database") != database_name:
            raise JsonPublishInputError(f"JSON 文件不属于当前数据库: {name}")
        documents.append(document)
    return documents
```

清单/目录校验逻辑从 `json_step_service.read_managed_json_input` 中抽成共享函数 `_verify_json_manifest(json_dir) -> list[str]`（保留其现有全部规则：symlink、双向一致、严格三段式、混库），两个入口共用；`read_managed_json_input` 改为：调 `read_managed_json_documents`（或共享清单函数 + JsonLoader 路径，保持既有行为与返回值不变——**其现有调用（受管 MD 准备链）行为必须逐字节不变**，用既有 `test_json_step_service.py` 全部通过来锁定）。

`build_profile_rows` 的校验顺序（每条失败消息只含文件级对象名 + 字段/路径 + 类别）：
1. `object_info` 身份非空、`object_type ∈ _SUPPORTED_OBJECT_TYPES`、`schema_name in selected_schemas`；
2. `column_profiles` 非空；逐字段：`ordinal_position` 正整数且不重复（`isinstance(v, int) and not isinstance(v, bool) and v >= 1`）、`is_nullable` 布尔、`data_type` 非空字符串；
3. 计数非负整数（拒 bool）：`object_info.total_rows`、`profiling.sample_count`、`statistics.null_count/unique_count`；
4. 比例/置信度有限且 0-1（拒 bool）：`table_profile.confidence`、`rule_based_classification.confidence`、`semantic_analysis.semantic_confidence`、`statistics.null_rate/uniqueness`、`unique_column_sets[].confidence_score/uniqueness/null_rate`（`isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and 0<=v<=1`）；
5. 字符串数组：`classification_reason` 之外的 `inference_basis` 类键（`table_profile.inference_basis`、`rule_based_classification.inference_basis`、`semantic_analysis.inference_basis`）；
6. 结构数组：`physical_constraints.foreign_keys/unique_constraints`、`indexes`、`unique_column_sets` 为 list[dict]；
7. `statistics.value_distribution` 为 dict；`generated_timestamp` 若存在 `datetime.fromisoformat` 可解析；
8. 结构引用：`primary_key.columns`、`unique_constraints[].columns`、`unique_column_sets[].columns`、`foreign_keys[].source_columns`（非空、与 `target_columns` 等长）、`indexes[].columns/included_columns` ⊆ `column_profiles` 键；`key_expressions` 不校验；目标对象不要求在本批。

映射按 §7.3 两张表逐行生成 dict；"显式 null vs 键缺失"用 `"physical_constraints" in tp` / `"primary_key" in pc` 区分；`foreign_keys` 等三个数组显式 `[]` 存 `[]`、计数 0，键缺失存 `None`、计数 `None`；`json_sample_records` = 顶层 `sample_records` 原样。

- [ ] **Step 4: 运行** `uv run pytest tests/unit/test_json_publish_loader.py tests/unit/test_json_step_service.py tests/unit/test_md_step_service.py -v`（后两个锁定旧契约不变）。
- [ ] **Step 5: Commit** `feat: 受管 JSON 发布加载器与共享快照读取（doc 19.2 §7.2/§7.3）`。

---

### Task 5: publish_service 扩展 json 步骤 + API 枚举 + 删源清理

**Spec:** §7.4 全部、§8.3 末段（枚举）、§9.9/§9.10/§9.15。

**Files:**
- Modify: `backend/publishing/publish_service.py`、`backend/api/schemas.py:341,361`、`backend/data_sources/repository.py:120-122`
- Test: `tests/unit/test_json_publish_service.py`（新增，复用 `test_publish_service.py` 的 FakePublishSession/install_session——直接 import）、`tests/unit/test_publish_service.py`（白名单断言微调）

**Interfaces:**
- Consumes: Task 4 全部导出；Task 3 ORM 类。
- Produces:
  - `JSON_STEP = "json"`；白名单 `(DB_STEP, DDL_STEP, JSON_STEP)`。
  - `REGISTRATION_CHANGED_MESSAGE = "数据源登记信息已变化，请重新发布"`。
  - `is_primary_key_conflict` 的集合增加 `"table_profile_pkey"`、`"table_column_profile_pkey"`、`"table_column_profile_object_order_idx"`。
  - `schemas.py`：`PublishCheckStep.step_name` 与 `PublicationStepResult.step` 的 `Literal` 扩为 `"db" | "ddl" | "json"`。

- [ ] **Step 1: 写失败测试** `tests/unit/test_json_publish_service.py`：

```python
"""json 发布步骤：预检、整批事务、登记版本复查与失败语义（doc 19.2 §7.4）。"""

from tests.unit.test_publish_service import (
    JOB_ID, RUN_MARKER, SETTINGS, SOURCE_ID,
    FakePublishSession, install_session,
)

# 用例（每个独立函数；受管目录 fixture 写合法/畸形 JSON + manifest）：
# 1 test_json_step_whitelisted_and_prechecked
#   normalize_publish_steps(['db','ddl','json']) 顺序去重；check_publication
#   返回三行，json 行 output_ready=False 时 publishable=False。
# 2 test_publish_json_success_replaces_current
#   预检（登记行 updated_at=T0, json 步骤 success）→ 发布：table_profile/
#   table_column_profile 行写入且 publish_id 一致；旧 current 被翻、新
#   publish_info(json, is_current=True) 写入；结果 message 成功。
# 3 test_publish_json_rejects_bad_input_and_keeps_old_state
#   畸形输入（如 ordinal_position=0）：结果 failed + INPUT_SUMMARY；旧画像
#   行与旧 current 不变；失败 publish_info(is_current=False)。
# 4 test_registration_changed_under_lock_fails_controlled
#   预检登记行 updated_at=T0，提交时假会话把源行 updated_at 改为 T1：
#   结果 failed + REGISTRATION_CHANGED_MESSAGE；不误报 platform_unavailable。
# 5 test_step_rerun_during_read_reports_not_ready
#   提交时步骤行 updated_at 变化：结果 failed + NOT_READY_MESSAGE。
# 6 test_combined_steps_partial_success
#   db 成功 + json 输入坏：db 保持成功，json failed；publish_id 共用。
# 7 test_conflict_predicates_include_new_tables
#   integrity_error("23505","table_profile_pkey") / ("table_column_profile_object_order_idx")
#   → is_primary_key_conflict True。
# 8 test_delete_source_removes_profile_rows
#   data_sources.repository 的删源清理（FakePublishSession.execute 记录
#   delete 语句）：断言包含对 table_profile 的 delete。
```

- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现** `publish_service.py`：
  - import 增加：`from backend.persistence.models import TableDefinition, TableColumnProfile, TableProfile`、`from backend.profile_loading.json_loader import (JSON_STEP, JsonPublishInputError, build_profile_rows, check_json_output_ready, inspect_json_profile_context, read_managed_json_documents, read_registration_snapshot)`；新 `REGISTRATION_CHANGED_MESSAGE` 常量。
  - `normalize_publish_steps`：合法集加 `JSON_STEP`，错误文案改"当前仅支持 db、ddl、json 步骤"。
  - `check_publication`：循环 `(DB_STEP, DDL_STEP, JSON_STEP)`；inspector 映射加 json；json 的 output 检查 `check_json_output_ready(source, job, database_name)`——库名在 inspector 会话内先 `read_registration_snapshot`（同时拿登记 updated_at，但预检响应不需要返回它）。json 行复用同一 `steps.append` 结构。
  - `publish_steps`：预检循环内 json 分支额外 `registration = read_registration_snapshot(session, source_uuid)`（同一会话）记 `registration_snapshots[JSON_STEP]`；执行循环 json 分支调 `_publish_json(settings, source_uuid, job_uuid, publish_id, run_markers[JSON_STEP], registration)`，其余步骤走 `_PUBLISHERS`。
  - `_publish_json`：与 `_publish_ddl` 同构——`read_managed_json_documents(source, job, registration.db_name)` 捕 `(OSError, UnicodeError, ValueError)`（`JsonPublishInputError` 是 ValueError 子类）→ `_store_failure(INPUT_SUMMARY)`；`build_profile_rows(...)` 同样捕获 → INPUT_SUMMARY；然后 `_commit_json_current_state(...)`，异常映射与 ddl 相同，另加 `RegistrationChangedError → _store_failure(REGISTRATION_CHANGED_MESSAGE)`。
  - `_commit_json_current_state(settings, source_id, job_id, publish_id, table_rows, column_rows, expected_run_marker, expected_registration)`：

```python
    with platform_session(settings) as session:
        source = lock_db_source_row(session, source_id)
        if source is None:
            raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能加载画像")
        if source.updated_at != expected_registration.updated_at:
            raise RegistrationChangedError(REGISTRATION_CHANGED_MESSAGE)
        _verify_step_current_under_lock(session, job_id, JSON_STEP, expected_run_marker)
        clear_current(session, source_id, JSON_STEP)
        session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
        session.add_all(TableProfile(**row) for row in table_rows)
        session.add_all(TableColumnProfile(**row) for row in column_rows)
        insert_publish_row(session, publish_id, source_id, job_id, JSON_STEP,
                           STATUS_SUCCESS, is_current=True)
        session.commit()
```

  `class RegistrationChangedError(RuntimeError)` 定义在 publish_service（仅供内部转 `_store_failure`，不映射 HTTP——组合发布的失败体现在该步结果里）。
  - `is_primary_key_conflict` 集合扩三 entries（见 Interfaces）。
- [ ] **Step 4: schemas.py 枚举**：341 行 `step_name: Literal["db", "ddl", "json"]`、361 行 `step: Literal["db", "ddl", "json"]`（检查 `PublishCheckResponse/PublicationResponse` 周边是否有默认值/说明需同步）。
- [ ] **Step 5: 删源清理**：`backend/data_sources/repository.py` 120-122 行的清理事务追加一行：

```python
    session.execute(delete(TableProfile).where(TableProfile.source_id == source_id))
```

（放在 `delete(TableDefinition)` 之后；字段行由 `ON DELETE CASCADE` 级联。）
- [ ] **Step 6: 运行** `uv run pytest tests/unit/test_json_publish_service.py tests/unit/test_publish_service.py tests/unit/test_ddl_publish.py tests/unit/test_web_publishing.py -v`；随后全量 `uv run pytest tests/unit -q`。
- [ ] **Step 7: Commit** `feat: Web 发布扩展 json 步骤与整批画像替换事务（doc 19.2 §7.4）`。

---

### Task 6: 前端发布枚举与 json 项

**Spec:** §7.4 首段、§8.3 末段。

**Files:**
- Modify: `frontend/src/api/types.ts:227,246`、`frontend/src/views/PublishView.vue`

- [ ] **Step 1: types.ts**：`PublishCheckStep.step_name: 'db' | 'ddl' | 'json'`；`PublicationStepResult.step: 'db' | 'ddl' | 'json'`。
- [ ] **Step 2: PublishView.vue**：`SELECTABLE_STEPS` 从 `['db','ddl']` 改为 `['db','ddl','json']`（第 24 行）；静态步骤组中 `json` 从"暂不支持"组（35-38 行的展示组）移入选中组；页面说明文案（225 行）"当前支持 db 与 ddl"改"当前支持 db、ddl 与 json"。预检结果 `applyCheck` 对 json 行复用既有 publishable 门控，无需新逻辑。
- [ ] **Step 3: 验证**：`cd frontend && npx vue-tsc --noEmit && npm run build`。
- [ ] **Step 4: Commit** `feat: 发布页开放 json 步骤选择（doc 19.2 §7.4）`。

---

### Task 7: 只读事务辅助 + object_catalog（列表与候选）

**Spec:** §8.1 前两个接口、§8.1.1 前两行、§8.2 首末段、§3.7 列表计数。

**Files:**
- Create: `backend/assets/read_tx.py`、`backend/assets/object_catalog.py`
- Test: `tests/unit/test_object_catalog.py`（新增）

**Interfaces:**
- Produces:
```python
# backend/assets/read_tx.py
READ_ONLY_EXECUTION_OPTIONS = {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}

def begin_readonly_repeatable_read(session) -> None:
    """在首条数据查询前配置连接（doc 19.2 §8.2）。调用方须在 platform_session
    建立后、任何查询/session.get() 之前调用本函数。"""

# backend/assets/object_catalog.py
def list_objects(settings, source_id, *, page, page_size, object_type=None, schema_name=None, keyword=None) -> dict
def list_object_options(settings, source_id, *, object_type=None, schema_name=None, keyword=None) -> dict
def get_current_json_publishes(session, source_id) -> list[PublishInfo]   # 供 object_detail 复用
def escape_like(keyword: str) -> str
JSON_STEP = "json"
```
- 消费方（Task 8/10）：`begin_readonly_repeatable_read`、`get_current_json_publishes`。

- [ ] **Step 1: 写失败测试**（FakePublishSession 风格假会话，SimpleNamespace 行对象；对 `platform_session` monkeypatch）：

```python
# 用例清单（组装函数与 service 分离测：组装函数吃 SimpleNamespace 行）：
# 1 test_source_missing_raises_404_domain          # SourceNotFoundError
# 2 test_no_current_returns_not_published          # json_publish=None,total=0,items=[],page/page_size 回显
# 3 test_current_without_rows_is_inconsistent      # state=inconsistent 且 json_publish=None,total=0,items=[]
# 4 test_multiple_current_is_inconsistent          # 防御：两条 current 行
# 5 test_rows_version_mismatch_is_inconsistent     # 部分行 publish_id != current → inconsistent
# 6 test_ready_lists_sorted_and_paged              # 按 schema_name,object_name 排序；page/page_size 切片；total=筛选后数
# 7 test_filters_object_type_and_schema_exact      # 精确匹配
# 8 test_keyword_substring_case_insensitive        # 'Ord' 命中 orders
# 9 test_keyword_percent_underscore_are_literal    # keyword='%'、'_'、'a\\%' 均按普通字符，不放大结果
# 10 test_primary_key_state_projection             # pk 列 [] → 'none'；None → 'unknown'；非空 → 'present'
# 11 test_generated_timestamp_local_without_z      # 原样无 Z 字符串
# 12 test_options_cascade_and_keyword              # object_types 固定顺序 table<view<materialized_view；
#                                                  # schemas 按类型收窄；objects 按类型+schema+keyword 收窄；keyword 不改前两级
# 13 test_options_not_published_empty_arrays
# 14 test_service_calls_begin_readonly_before_queries   # 假会话记录调用顺序：connection(execution_options=...) 先于首个 scalars
```

- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现** `read_tx.py`：

```python
"""只读 REPEATABLE READ 连接配置（doc 19.2 §8.2）。"""

READ_ONLY_EXECUTION_OPTIONS = {
    "isolation_level": "REPEATABLE READ",
    "postgresql_readonly": True,
}


def begin_readonly_repeatable_read(session) -> None:
    session.connection(execution_options=READ_ONLY_EXECUTION_OPTIONS)
```

- [ ] **Step 4: 实现** `object_catalog.py`（组装函数与 IO 函数分离；`_utc(value)` 沿用 `catalog.py:_format_time` 的 UTC Z 格式，抽到 read_tx 或本模块并让 Task 8 复用——放 `read_tx.py` 导出 `utc_isoformat`）：

```python
def list_objects(settings, source_id, *, page, page_size, object_type=None, schema_name=None, keyword=None):
    try:
        with platform_session(settings) as session:
            begin_readonly_repeatable_read(session)
            source = get_db_source(session, source_id)
            if source is None:
                raise SourceNotFoundError("数据源不存在")
            currents = get_current_json_publishes(session, source_id)
            if not currents:
                return _empty("not_published", source_id, page, page_size)
            current = currents[0]
            if len(currents) > 1 or not _source_rows_consistent(session, source_id, current.publish_id):
                return _empty("inconsistent", source_id, page, page_size)
            conditions = [TableProfile.source_id == source_id,
                          TableProfile.publish_id == current.publish_id]
            if object_type:
                conditions.append(TableProfile.object_type == object_type)
            if schema_name:
                conditions.append(TableProfile.schema_name == schema_name)
            if keyword:
                conditions.append(TableProfile.object_name.ilike(
                    f"%{escape_like(keyword)}%", escape="\\"))
            stmt = select(TableProfile).where(*conditions).order_by(
                TableProfile.schema_name.asc(), TableProfile.object_name.asc())
            total = session.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(session.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
            return {
                "state": "ready", "source_id": str(source_id),
                "json_publish": _publish_block(current),
                "total": int(total), "page": page, "page_size": page_size,
                "items": [_list_item(row) for row in rows],
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
```

`_source_rows_consistent`：`count(source rows) > 0 且 count(publish_id != current 的行) == 0`（两次标量子查询或一次 `group by`）。`_list_item` 投影 §8.1.1 九字段；`primary_key_state` 由 `primary_key_columns` 判（None→unknown，[]→none，非空→present）；`generated_timestamp_local` 用 `value.isoformat()`（无 Z）。`list_object_options` 同一状态判定后在 Python 侧对全量当前行做三级收窄（行数有限，无分页）；`object_types` 按 `{"table":0,"view":1,"materialized_view":2}` 排序；`keyword` 用 `keyword.lower() in object_name.lower()`。
- [ ] **Step 5: 运行通过** `uv run pytest tests/unit/test_object_catalog.py -v`。
- [ ] **Step 6: Commit** `feat: 对象列表与级联候选读取服务（doc 19.2 §8.1）`。

---

### Task 8: object_detail（详情与 DDL 摘要、字段投影）

**Spec:** §8.1 后两个接口、§8.1.1 后两行与投影段、§8.2 前三段。

**Files:**
- Create: `backend/assets/object_detail.py`
- Test: `tests/unit/test_object_detail.py`（新增）

**Interfaces:**
- Consumes: Task 7 的 `get_current_json_publishes`、`begin_readonly_repeatable_read`、`utc_isoformat`；Task 3 ORM。
- Produces:
```python
class ProfileInconsistentError(RuntimeError): ...
class ObjectNotFoundError(RuntimeError): ...

def get_object_detail(settings, source_id, schema_name, object_name) -> dict
def get_object_ddl(settings, source_id, schema_name, object_name) -> dict
def _ddl_summary(session, source_id, *, db_name, object_type, schema_name, object_name)
    -> tuple[str, dict | None]     # ('not_published'|'inconsistent'|'incompatible'|'ready', ddl_publish|None)
```

- [ ] **Step 1: 写失败测试**（用例清单）：
  - `test_no_current_returns_not_published_payload`（§8.1.1 第三行：`ddl_state=not_published`、`columns=[]`）。
  - `test_current_without_any_rows_raises_profile_inconsistent`。
  - `test_object_absent_raises_404_domain`（源级有效但对象不在 → `ObjectNotFoundError`）。
  - `test_row_version_mismatch_raises_profile_inconsistent`。
  - `test_column_version_or_count_mismatch_raises_profile_inconsistent`（字段行 `publish_id` 不符 / 行数 != `column_count`）。
  - `test_ready_detail_projects_columns_in_ordinal_order`（含 `physical_constraints` 四键：`primary_key_state` 三态、pk 命中返回完整对象、`foreign_keys/unique_constraints` 各自 null-vs-[] 语义、`indexes`/`unique_column_sets` 同；`profile` 排除 `json_sample_records`；字段排除 `value_distribution`）。
  - `test_pk_node_missing_keeps_other_projections`（pk 缺失不清外键/唯一约束明细）。
  - DDL 摘要五态：`test_ddl_not_published_when_no_marker_and_no_row`、`test_ddl_not_published_when_marker_but_no_row`、`test_ddl_inconsistent_orphan_row`、`test_ddl_inconsistent_version_mismatch`、`test_ddl_incompatible_identity`（`db_name/object_type` 与 JSON 行不同）、`test_ddl_ready_returns_publish_block`。
  - `test_object_ddl_states`：json current 无 → `ObjectNotFoundError`；ddl_state=not_published/incompatible → 200 空 payload；inconsistent → `DefinitionInconsistentError`（从 `backend.assets.ddl_display` import，Task 9 先建类则本任务可 import；为解耦，Task 8 先在文件顶部 `from backend.assets.ddl_display import DefinitionInconsistentError, split_definition_sql, extract_sample` 并把解析调用留到 Task 9 实现后补测——**顺序调整：先做 Task 9 再回来补 Step 4 的 object-ddl 解析测试**）。
- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现** `get_object_detail`（事务开头 `begin_readonly_repeatable_read(session)`；状态判定顺序按 §8.2：current → 源级行数 → 对象行 → 版本 → 字段行核对 → DDL 摘要；`profile` 块 = §6 表级字段全量减 `json_sample_records`；字段投影函数 `_column_structures(row, column_name)`）：

```python
def _column_structures(row, column_name: str) -> dict:
    def hits(items, key):
        if items is None:
            return None
        return [item for item in items if column_name in (item.get(key) or [])]

    if row.primary_key_columns is None:
        pk_state, pk = "unknown", None
    elif column_name in row.primary_key_columns:
        pk_state = "present"
        pk = {"constraint_name": row.primary_key_name,
              "columns": list(row.primary_key_columns)}
    else:
        pk_state, pk = "none", None
    return {
        "primary_key_state": pk_state,
        "primary_key": pk,
        "foreign_keys": hits(row.foreign_keys, "source_columns"),
        "unique_constraints": hits(row.unique_constraints, "columns"),
    }

def _column_indexes(row, column_name):
    if row.indexes is None:
        return None
    return [idx for idx in row.indexes
            if column_name in (idx.get("columns") or [])
            or column_name in (idx.get("included_columns") or [])]

def _column_unique_sets(row, column_name):
    return hits(row.unique_column_sets, "columns")  # hits(None)→None 语义复用
```

  `get_object_ddl`：json current 校验（无 → `ObjectNotFoundError("对象不在当前 JSON 发布中")`；current 有但源无行 → `ProfileInconsistentError`）→ 对象行存在与版本 → `_ddl_summary`：`not_published`/`incompatible` → 200 空 payload（`definition_sql=None, sample_state=None, sample_columns=[], sample_records=[]`，带 `json_publish_id`）；`inconsistent` → `DefinitionInconsistentError`；`ready` → 取 `define_text` 调 Task 9 的 `split_definition_sql`/`extract_sample` 组装。
- [ ] **Step 4: Commit**（与 Task 9 合并提交亦可，见 Task 9 Step 5）。

---

### Task 9: ddl_display（纯函数切分与样例解析）

**Spec:** §3.2、§3.6、§8.2 DDL 段全部、§9.5/§9.16。

**Files:**
- Create: `backend/assets/ddl_display.py`
- Test: `tests/unit/test_ddl_display.py`（新增）

**Interfaces:**
- Consumes: Task 1 的 `sample_block` 函数。
- Produces:
```python
class DefinitionInvalidError(ValueError): ...
class DefinitionInconsistentError(RuntimeError): ...

def split_definition_sql(define_text: str) -> tuple[str, dict | None]
    # 返回 (剥离末尾唯一当前块后的 SQL, 块 payload | None)
    # 旧标记 / 多块 / 边界不完整 / 块后仍有非空白内容 / JSON 解析失败 → DefinitionInvalidError

def extract_sample(payload: dict | None, *, schema_name: str, object_name: str, object_type: str)
    -> tuple[str, list[str], list[dict]]
    # 返回 (sample_state, columns, records<=5)；身份不符/records 畸形/键集合不一致 → DefinitionInvalidError
```

- [ ] **Step 1: 写失败测试**：
  - `test_plain_ddl_without_block_returns_sql_and_none`。
  - `test_block_stripped_from_sql`：SQL 前文（含 `--` 头、COMMENT、CREATE INDEX）逐字保留，块不出现。
  - `test_tricky_values_roundtrip`：用 Task 1 `build_comment_block` 生成含 `TRICKY_VALUES` 的块 → 切分后 payload 原值。
  - `test_legacy_marker_rejected`、`test_legacy_marker_hint_rejected`（旧标记起始不完整）、`test_multiple_blocks_rejected`、`test_broken_boundary_rejected`（`/* SAMPLED_RECORDS {"a":` 无 `*/`）、`test_content_after_block_rejected`（块后还有 `CREATE INDEX ...`）。
  - `test_line_comment_marker_word_not_rejected`（`-- SAMPLE_RECORDS 说明` 不触发）。
  - `test_identity_mismatch_rejected`：`object_name` 是限定名核对（`public.orders` vs 行内 `schema='public', object='orders'`）；裸名比对不算通过；`object_type` 不符拒绝。
  - `test_sample_states`：无块 → `no_sample_block`；`records: []` → `empty`；正常 → `ready`、列序取首条键序、最多 5 行。
  - `test_records_keyset_mismatch_rejected`（第二条记录多一个键）。
  - `test_data_and_label_plain_fields`（含 `"data": {...}` 值也不拆包）。
- [ ] **Step 2: 运行确认失败。**
- [ ] **Step 3: 实现**：

```python
def split_definition_sql(define_text: str) -> tuple[str, dict | None]:
    if find_legacy_sample_records_block(define_text) or _legacy_hint(define_text):
        raise DefinitionInvalidError("检测到旧 SAMPLE_RECORDS 样例块")
    blocks = find_sampled_records_blocks(define_text)
    if not blocks:
        if has_sampled_records_marker_hint(define_text):
            raise DefinitionInvalidError("样例块边界不完整")
        return define_text, None
    if len(blocks) > 1:
        raise DefinitionInvalidError("存在多个样例块")
    match = blocks[0]
    if define_text[match.end():].strip():
        raise DefinitionInvalidError("样例块之后存在额外内容")
    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        raise DefinitionInvalidError("样例块 JSON 解析失败") from None
    if not isinstance(payload, dict):
        raise DefinitionInvalidError("样例块必须是对象")
    return define_text[: match.start()].rstrip("\n"), payload


def extract_sample(payload, *, schema_name, object_name, object_type):
    if payload is None:
        return "no_sample_block", [], []
    if payload.get("object_name") != f"{schema_name}.{object_name}":
        raise DefinitionInvalidError("样例块对象名与 DDL 行不一致")
    if payload.get("object_type") != object_type:
        raise DefinitionInvalidError("样例块对象类型与 DDL 行不一致")
    records = decode_current_records(payload)     # ValueError → DefinitionInvalidError
    if not records:
        return "empty", [], []
    first_keys = list(records[0].keys())
    if any(list(record.keys()) != first_keys for record in records):
        # 键集合按 set 比较（顺序无关），集合不一致才拒绝
        ...
    return "ready", first_keys, records[:5]
```

键集合核对用 `set(record) != set(first_keys)`；列顺序取首条键序。`_legacy_hint` 复用 Task 1 的正则（如 sample_block 未导出旧 hint，则在本模块用 `re.compile(r"/\*\s*SAMPLE_RECORDS(?![A-Z_])")`）。
- [ ] **Step 4: 回补 Task 8 Step 1 的 object-ddl 解析集成用例**（`get_object_ddl` ready 路径 + 三种 DDL 状态的完整响应形状）。
- [ ] **Step 5: 运行并 Commit** `feat: DDL 展示切分与样例解析（doc 19.2 §8.2）`（含 Task 8 文件）。

---

### Task 10: API 路由、响应模型、错误映射与隔离级别集成测试

**Spec:** §8.1 路由表、§8.1.1 契约与示例、§8.2 末段、§8.3 表前两行与末段。

**Files:**
- Create: `backend/api/object_schemas.py`、`backend/api/routers/asset_objects.py`、`tests/unit/test_asset_objects_api.py`、`tests/unit/test_read_tx_isolation.py`
- Modify: `backend/api/errors.py`、`backend/api/main.py`

**Interfaces:**
- Consumes: Task 7/8 全部 service 与异常。
- Produces: 四个 GET 路由（挂在既有 `/api/data-assets` 前缀下）：
  - `GET /{source_id}/objects?page=1&page_size=50&object_type=&schema_name=&keyword=`
  - `GET /{source_id}/object-options?object_type=&schema_name=&keyword=`
  - `GET /{source_id}/object-detail?schema_name=&object_name=`（必填，缺省 422 由 FastAPI 校验）
  - `GET /{source_id}/object-ddl?schema_name=&object_name=`

- [ ] **Step 1: 写失败测试** `tests/unit/test_asset_objects_api.py`（模式照抄 `test_asset_profile.py`：`create_app()` + TestClient + monkeypatch 路由模块的 service 符号）：

```python
# 用例（每个断言 body 的 code/message/status，不出现 DDL 原文）：
# 1 test_objects_passes_filters_and_returns_contract   # monkeypatch list_objects 记录入参（page/page_size 上限、keyword 原样）
# 2 test_object_detail_409_profile_inconsistent        # ProfileInconsistentError → 409 {"code":"profile_inconsistent"}
# 3 test_object_ddl_409_definition_inconsistent        # → 409 {"code":"definition_inconsistent"}
# 4 test_object_ddl_409_definition_invalid             # → 409 {"code":"definition_invalid"}，message 不含正文
# 5 test_missing_object_404_not_found                  # ObjectNotFoundError → 404 {"code":"not_found"}
# 6 test_source_missing_404                            # SourceNotFoundError → 404（复用既有处理器）
# 7 test_platform_503                                  # PlatformUnavailableError → 503
# 8 test_detail_response_matches_schema_example        # 返回 §8.1.1 最小结构示例逐键断言（fixture dict）
# 9 test_object_ddl_ready_response_matches_schema_example
# 10 test_query_validation_422                         # 缺 schema_name → 422 {"code":"invalid_request"}
# 11 test_ddl_row_without_json_current_404             # Review Focus #4：DDL 行存在但对象不在当前 JSON → 404 而非 not_published
```

- [ ] **Step 2: 实现** `backend/api/object_schemas.py`（Pydantic v2，字段逐一对照 §8.1.1 表格与示例 JSON）：

```python
"""对象读取接口响应模型（doc 19.2 §8.1.1）。"""

from pydantic import BaseModel


class JsonPublishInfo(BaseModel):
    publish_id: str
    job_id: str
    published_at: str


class DdlPublishInfo(BaseModel):
    publish_id: str
    job_id: str
    loaded_at: str


class ObjectListItem(BaseModel):
    schema_name: str
    object_name: str
    object_type: str
    object_comment: str | None
    table_category: str | None
    column_count: int
    total_rows: int | None
    primary_key_state: str
    generated_timestamp_local: str | None


class ObjectsResponse(BaseModel):
    state: str
    source_id: str
    json_publish: JsonPublishInfo | None
    total: int
    page: int
    page_size: int
    items: list[ObjectListItem]


class ObjectOptionItem(BaseModel):
    schema_name: str
    object_name: str
    object_type: str


class ObjectOptionsResponse(BaseModel):
    state: str
    source_id: str
    json_publish: JsonPublishInfo | None
    object_types: list[str]
    schemas: list[str]
    objects: list[ObjectOptionItem]


class ColumnPhysicalConstraints(BaseModel):
    primary_key_state: str
    primary_key: dict | None
    foreign_keys: list[dict] | None
    unique_constraints: list[dict] | None


class ObjectDetailColumn(BaseModel):
    column_name: str
    ordinal_position: int
    data_type: str
    is_nullable: bool
    column_default: str | None
    column_comment: str | None
    comment_source: str | None
    semantic_role: str | None
    semantic_confidence: float | None
    semantic_basis: list[str] | None
    null_count: int | None
    null_rate: float | None
    unique_count: int | None
    uniqueness: float | None
    sample_min: str | None
    sample_max: str | None
    physical_constraints: ColumnPhysicalConstraints
    indexes: list[dict] | None
    unique_column_sets: list[dict] | None


class ObjectDetailProfile(BaseModel):
    # §6 表级标量全量（除 json_sample_records）：db_name, object_type, publish_id,
    # job_id, metadata_version, generated_timestamp_local, object_comment,
    # object_comment_source, total_rows, column_count, sample_method, sample_count,
    # has_table_profile, table_category, category_confidence, classification_source,
    # classification_reason, classification_basis, rule_table_category, rule_confidence,
    # rule_inference_basis, primary_key_name, primary_key_columns, foreign_key_count,
    # index_count, constraint_count, foreign_keys, unique_constraints, indexes,
    # unique_column_sets —— 类型逐一对照 §6.2 列定义
    ...


class ObjectDetailResponse(BaseModel):
    state: str
    source_id: str
    schema_name: str
    object_name: str
    json_publish: JsonPublishInfo | None
    ddl_state: str
    ddl_publish: DdlPublishInfo | None
    profile: ObjectDetailProfile | None
    columns: list[ObjectDetailColumn]


class ObjectDdlResponse(BaseModel):
    state: str
    json_publish_id: str | None
    ddl_publish: DdlPublishInfo | None
    definition_sql: str | None
    sample_state: str | None
    sample_columns: list[str]
    sample_records: list[dict]
```

- [ ] **Step 3: 实现** `routers/asset_objects.py`（结构照抄 `routers/assets.py`；`page: int = Query(1, ge=1)`、`page_size: int = Query(50, ge=1, le=100)`；空串参数归一为 None：`object_type or None`；`page_size` 超上限由 422 拒绝）；`main.py` include_router。
- [ ] **Step 4: errors.py 增加四个处理器**（插在既有业务处理器区）：

```python
from backend.assets.ddl_display import (
    DefinitionInconsistentError,
    DefinitionInvalidError,
)
from backend.assets.object_detail import (
    ObjectNotFoundError,
    ProfileInconsistentError,
)

    @app.exception_handler(ProfileInconsistentError)
    async def profile_inconsistent(_request, _exc):
        return _response(409, "profile_inconsistent", "已发布画像状态不一致，暂无法展示")

    @app.exception_handler(DefinitionInconsistentError)
    async def definition_inconsistent(_request, _exc):
        return _response(409, "definition_inconsistent", "已发布 DDL 状态不一致，暂无法展示")

    @app.exception_handler(DefinitionInvalidError)
    async def definition_invalid(_request, _exc):
        return _response(409, "definition_invalid", "DDL 样例块格式异常，暂无法展示")

    @app.exception_handler(ObjectNotFoundError)
    async def object_not_found(_request, _exc):
        return _response(404, "not_found", "对象不存在")
```

- [ ] **Step 5: 隔离级别集成测试** `tests/unit/test_read_tx_isolation.py`（Review Focus #5）：

```python
"""读事务隔离级别实测（doc 19.2 §8.2/§8.3）：SHOW 断言，不依赖调用顺序。"""

import pytest
from sqlalchemy import text

from backend.assets.read_tx import begin_readonly_repeatable_read
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过平台库集成断言"
)


def test_readonly_repeatable_read_actually_in_effect():
    with platform_session(SETTINGS) as session:
        begin_readonly_repeatable_read(session)
        isolation = session.execute(text("SHOW transaction_isolation")).scalar_one()
        read_only = session.execute(text("SHOW transaction_read_only")).scalar_one()
        assert isolation == "repeatable read"
        assert read_only == "on"
```

本地 `.env` 有 `PLATFORM_DB_*` 时该测试真实执行；无则跳过。
- [ ] **Step 6: 运行** `uv run pytest tests/unit/test_asset_objects_api.py tests/unit/test_read_tx_isolation.py -v`，再全量 `uv run pytest tests/unit -q` + `ruff check` + `black --check`。
- [ ] **Step 7: Commit** `feat: 对象读取 API 四路由与受控错误映射（doc 19.2 §8）`。

---

### Task 11: 前端 API 层、对象列表页、路由与入口

**Spec:** §3.7（13 号原型、路由、计数文案）、§8.1.1 契约。

**Files:**
- Modify: `frontend/src/api/types.ts`、`frontend/src/api/assets.ts`、`frontend/src/router/index.ts`、`frontend/src/views/DatabaseProfileView.vue:218-221`、`frontend/src/views/DataSourceDetailView.vue`
- Create: `frontend/src/views/ObjectListView.vue`
- 参照原型：`docs/data_gov/ui/数据源管理到发布/13.数据库对象列表.html`（先读）

- [ ] **Step 1: types.ts 增加类型**（与 Task 10 的 Pydantic 字段一一对应）：

```ts
export type ObjectState = 'ready' | 'not_published' | 'inconsistent'

export interface JsonPublishInfo { publish_id: string; job_id: string; published_at: string }
export interface DdlPublishInfo { publish_id: string; job_id: string; loaded_at: string }

export interface ObjectListItem {
  schema_name: string; object_name: string; object_type: string
  object_comment: string | null; table_category: string | null
  column_count: number; total_rows: number | null
  primary_key_state: 'present' | 'none' | 'unknown'
  generated_timestamp_local: string | null
}
export interface ObjectsResponse {
  state: ObjectState; source_id: string; json_publish: JsonPublishInfo | null
  total: number; page: number; page_size: number; items: ObjectListItem[]
}
export interface ObjectOptionItem { schema_name: string; object_name: string; object_type: string }
export interface ObjectOptionsResponse {
  state: ObjectState; source_id: string; json_publish: JsonPublishInfo | null
  object_types: string[]; schemas: string[]; objects: ObjectOptionItem[]
}
export interface ColumnPhysicalConstraints {
  primary_key_state: 'present' | 'none' | 'unknown'
  primary_key: Record<string, unknown> | null
  foreign_keys: Record<string, unknown>[] | null
  unique_constraints: Record<string, unknown>[] | null
}
export interface ObjectDetailColumn {
  column_name: string; ordinal_position: number; data_type: string
  is_nullable: boolean; column_default: string | null
  column_comment: string | null; comment_source: string | null
  semantic_role: string | null; semantic_confidence: number | null
  semantic_basis: string[] | null
  null_count: number | null; null_rate: number | null
  unique_count: number | null; uniqueness: number | null
  sample_min: string | null; sample_max: string | null
  physical_constraints: ColumnPhysicalConstraints
  indexes: Record<string, unknown>[] | null
  unique_column_sets: Record<string, unknown>[] | null
}
export interface ObjectDetailProfile { /* 同 Task 10 ObjectDetailProfile 的 TS 镜像，逐字段 */ }
export interface ObjectDetailResponse {
  state: 'ready' | 'not_published'
  source_id: string; schema_name: string; object_name: string
  json_publish: JsonPublishInfo | null
  ddl_state: 'ready' | 'not_published' | 'incompatible' | 'inconsistent'
  ddl_publish: DdlPublishInfo | null
  profile: ObjectDetailProfile | null
  columns: ObjectDetailColumn[]
}
export interface ObjectDdlResponse {
  state: 'ready' | 'not_published' | 'incompatible'
  json_publish_id: string | null; ddl_publish: DdlPublishInfo | null
  definition_sql: string | null
  sample_state: 'ready' | 'no_sample_block' | 'empty' | null
  sample_columns: string[]; sample_records: Record<string, unknown>[]
}
```

- [ ] **Step 2: assets.ts 增加**（沿用该文件现有 http 封装与错误处理模式）：

```ts
export function listSourceObjects(sourceId: string, params: {
  page?: number; page_size?: number; object_type?: string
  schema_name?: string; keyword?: string
}): Promise<ObjectsResponse>
export function listObjectOptions(sourceId: string, params: {
  object_type?: string; schema_name?: string; keyword?: string
}): Promise<ObjectOptionsResponse>
export function getObjectDetail(sourceId: string, schemaName: string, objectName: string): Promise<ObjectDetailResponse>
export function getObjectDdl(sourceId: string, schemaName: string, objectName: string): Promise<ObjectDdlResponse>
```

（GET query 传参；409/404 错误按现有 http.ts 的统一错误路径冒泡，列表页只需处理 200 三态。）
- [ ] **Step 3: 路由**：`router/index.ts` 增两条（`DataAssetsView` 同级）：

```ts
    { path: '/data-assets/:sourceId/objects', name: 'source-objects', component: ObjectListView },
    {
      path: '/data-assets/:sourceId/objects/detail',
      name: 'source-object-detail',
      component: ObjectDetailView,
    },
```

- [ ] **Step 4: ObjectListView.vue**（对照 13 号 HTML 结构与配色；script setup + TS）：
  - 状态：`filters = reactive({ object_type: '', schema_name: '', keyword: '' })`（提交后才生效的 `appliedFilters`）、`page/pageSize=20/total/items/state/jsonPublish/loading`。
  - 计数文案（spec §3.7）：`state==='ready'` 且无生效筛选 → `共 {total} 个对象`；有筛选 → `符合条件的 {total} 个对象`；`ready` 且 `total===0` → `未找到符合条件的对象`；`not_published` → 列表区显示"尚未发布 JSON 画像"，计数区不显示数字；`inconsistent` → "JSON 画像发布状态不一致，暂无法展示对象"。
  - 对象类型下拉 `table/view/materialized_view`（表/视图/物化视图）；SCHEMA 下拉取自 `listObjectOptions` 的 `schemas`（`object_type` 过滤后）；搜索触发 `page=1` 重新请求。
  - 列：对象类型、SCHEMA、对象名称（链接 → `/data-assets/{sourceId}/objects/detail?schema_name=..&object_name=..`）、中文名称（`object_comment` 原文，空显示"暂无注释"）、字段数量（`column_count`）、记录数（`total_rows`，null 显示"—"）、主键（`present→有 / none→无 / unknown→未知`）。
  - 服务端分页条；`inconsistent/not_published` 时不渲染表格。
- [ ] **Step 5: 入口接线**：`DatabaseProfileView.vue` 218-221 行禁用占位改为 `<RouterLink :to="`/data-assets/${sourceId}/objects`">查看当前库表的画像</RouterLink>`（保留按钮样式）；`DataSourceDetailView.vue` 在操作区增加同目标入口（读该文件后按其现有按钮风格放置）。
- [ ] **Step 6: 验证**：`npx vue-tsc --noEmit && npm run build`。
- [ ] **Step 7: Commit** `feat: 对象列表页与资产入口（doc 19.2 §3.7）`。

---

### Task 12: 对象详情页（两组 Tab + 级联搜索栏 + 状态处理）

**Spec:** §3、§3.1-§3.6 全部、§3.7 后半、§9.1/§9.2/§9.5/§9.13。

**Files:**
- Create: `frontend/src/views/ObjectDetailView.vue`
- 参照原型：14/15/16/17 号 HTML + 14.1 PNG（先读全部四份 HTML）

- [ ] **Step 1: 组件结构**（script setup + TS；样式类沿用原型/既有视图的 BEM 风格）：
  - 路由参数：`sourceId`（path）+ `schemaName/objectName`（query，`String(route.query.schema_name ?? '')`）。
  - 状态：`detail: ObjectDetailResponse | null`、`ddl: ObjectDdlResponse | null`、`ddlError: {code,message} | null`、`upperTab: 'info' | 'sql'`、`lowerTab: 'dict' | 'profile' | 'sample'`、`dictPage/profilePage`（各 1 起）、级联 `cascade = { objectType: '', schema: '', object: '', query: '' }`、`options: ObjectOptionsResponse | null`。
  - 载入流程（`watch` 路由 query，含首载）：请求 `getObjectDetail`；`ddl=null; ddlError=null`（清前一对象缓存）；级联按当前 `schema_name/object_name` 回填（从 `options` 找 `object_type`；`listObjectOptions` 不带 keyword 先取全量候选）。
  - `object-detail` 返回 `state==='not_published'`：来源说明行"JSON 画像尚未发布"，两组 Tab 区域显示同名空态，不请求 DDL。`409 profile_inconsistent`：来源说明行"JSON 画像发布状态不一致，暂无法展示"，清空画像与 DDL 缓存，正文区仅错误卡（标题/来源说明/级联栏保留）。其它错误走现有全局错误提示。
  - DDL 懒加载：首次进入 `upperTab==='sql'` 或 `lowerTab==='sample'` 时请求 `getObjectDdl` 并缓存；409 `definition_invalid/definition_inconsistent` 记入 `ddlError` 只影响两个 DDL 区域；JSON 内容照常。切换对象（级联搜索）时 `ddl/ddlError` 清空、两组 Tab 与两个页码重置。
  - 级联搜索栏：一级类型/二级 Schema/三级可搜索对象名（约 250ms 防抖，过时响应丢弃——用请求序号守卫）；上游变化清空下游；候选来自 `listObjectOptions`（带当前上游 + keyword）；`state` 为 `not_published/inconsistent` 时按 §3.7 专用空态文案；点"搜索"且已选定确切对象 → `router.replace` 更新 query（触发重载）；自由文本不充当身份。
  - 来源说明行：`detail.json_publish?.published_at` → `JSON 画像发布于：{本地时区格式化，标注时区}`；DDL 可展示时 SQL Tab 另标 `ddl_publish.loaded_at`。
- [ ] **Step 2: 上部基本信息 Tab**——12 项三列布局，逐项对照 §3.1 表格（对象名合并 `schema.object`；表分类 `事实表(置信度95%)`——`category_confidence` 0-1 转整数百分比，缺失只显示分类名，`has_table_profile===false` 显示"未生成"；主键字段按 `primary_key_columns` 原序列出、`[]` 显示"无"；计数缺失显示"未生成"；`total_rows/sample_count` 千分位、缺失"未记录"；`generated_timestamp_local` 截取 `YYYY-MM-DD HH:mm:ss`、缺失"未记录"）。上部切到 SQL Tab 时 12 项不渲染，标题与来源说明保留。
- [ ] **Step 3: 上部 SQL Tab**——`ddl.state==='ready'` 显示 `<pre>` 代码区（`definition_sql`，横向滚动 + 复制按钮）；`not_published` → "暂无已发布 DDL 数据"；`incompatible` → "当前已发布 DDL 与 JSON 对象身份不一致，暂无法展示"；`ddlError.definition_*` → 对应受控文案。`detail.state==='not_published'` 时同样显示空态。
- [ ] **Step 4: 下部数据字典 Tab**——11 列表格（§3.3 列清单），按 `ordinal_position` 升序、每页 10 行前端分页 + 横向滚动 + 固定前两列（CSS `position: sticky`）；注释来源空显示"未标注"；默认值 null → "—"；主键/外键/唯一约束命中单元格逐项列出约束名与完整列组（复合标"成员"）；"字段参与的索引"按 `columns→键列 / included_columns→INCLUDE 列` 精确匹配渲染 `索引名（键列；唯一或非唯一[；支持主键约束|支持约束：名]）`，`indexes===null` → "未生成"、`[]` → "无"；`is_constraint_backed` 渲染支持约束标注。
- [ ] **Step 5: 下部数据画像 Tab**——§3.5 抽样提醒（三态文案逐字对照 spec，`N=profile.sample_count`）+ 分类理由区（`classification_reason`/`classification_basis` + `rule_*` 标"规则分类备份"）+ 17 列表格（§3.4 列清单与顺序）；比例 0-1 → 百分比；候选列组列 `unique_column_sets` 命中显示完整列组并标"画像候选"；统计缺失显示"—"；`has_table_profile===false` → "未生成"。
- [ ] **Step 6: 下部样例数据 Tab**——`ddl.sample_state==='ready'`：`sample_columns` 表头 + `sample_records` 行（≤5，不分页）+ 标注"本页显示已发布 DDL 中的最多 5 条样例记录，仅供查看，不代表全表或数据画像的抽样规模"；`no_sample_block` → "本次已发布 DDL 未包含样例数据"；`empty` → "本次已发布 DDL 包含 0 条样例记录"；DDL 状态/错误文案与 SQL Tab 同源。
- [ ] **Step 7: 验证**：`npx vue-tsc --noEmit && npm run build`；人工冒烟（后端已可跑）：无 JSON 发布、有 JSON 无 DDL、有两者、`definition_invalid` 旧正文（平台库手工造一行旧标记 `define_text` 验证 409 文案）。
- [ ] **Step 8: Commit** `feat: 对象详情页两组 Tab 与级联搜索栏（doc 19.2 §3）`。

---

### Task 13: 全量回归与收尾

- [ ] **Step 1**: `uv run pytest tests/ -q` 全量通过（含既有 1700+）。
- [ ] **Step 2**: `uv run ruff check metaweave/ backend/ tests/`、`uv run black --check metaweave/ backend/ tests/`（与仓库现状一致即可，不重排无关文件）。
- [ ] **Step 3**: `cd frontend && npx vue-tsc --noEmit && npm run build`。
- [ ] **Step 4**: 端到端冒烟（本地平台库）：登记源 → 采集 ddl+json → 发布 db/ddl/json → 打开 `/data-assets/{id}/objects` 与详情页五态（not_published → ready → DDL 各态）逐项核对 §9 验收 1/5/7/11/13/16。
- [ ] **Step 5**: 更新 CLAUDE.md 的模块清单（`backend/assets/`、`profile_loading/json_loader.py`、asset_objects 路由）与输出结构说明（如涉及）。
- [ ] **Step 6**: Commit `test: doc 19.2 全量回归与文档同步`。

---

## Self-Review 记录

- **Spec 覆盖**：§6.2→Task 3；§7.1-7.3→Task 4；§7.4→Task 5/6；§8.1/8.1.1→Task 7/8/10；§8.2 采集修复→Task 1/2，读事务→Task 7/10，DDL 段→Task 8/9；§3.x 前端→Task 11/12；§9 验收 16 条分散于各任务测试与 Task 13 冒烟。spec §7.4 计划表的 PublishView 行由 Task 6 覆盖；删源清理由 Task 5 Step 5 覆盖。
- **类型一致性**：`begin_readonly_repeatable_read`/`get_current_json_publishes`/`utc_isoformat`（Task 7 定义，Task 8/10 消费）；`DefinitionInconsistentError/DefinitionInvalidError`（Task 9 定义，Task 8/10 消费）；`split_definition_sql/extract_sample` 签名在 Task 8/9 一致；ORM 类名 Task 3 定义、Task 4/5/7/8 消费。
- **Review Focus 五条**均已有归属任务与测试步骤。
