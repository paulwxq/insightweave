"""doc 23.2 批次 2：rel 编辑 API 三端点单元测试（A06–A10、A21、A25）。

FakeSession 提供 get(JobInfo)/get(DbSourceInfo) 最小分发；产物目录按受管
布局在 tmp_path 落盘（rel/{db}.relationships_global.{json,md} + json 画像）。
PUT 的源行锁/主配置快照/渲染函数均在模块命名空间打桩。
"""

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.profile_loading.rel_config_api import (
    RelConfigInvalidError,
    RelConfigOutputMissingError,
    RelRegeneratedError,
    RelWriteFailedError,
    read_rel_config,
    read_rel_object_columns,
    write_rel_config,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "demo"
STEP_AT = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc)
REL_DIR_NAME = "rel"


class FakeSession:
    def __init__(self, step_row=None, source_row=None):
        self.step_row = step_row
        self.source_row = source_row

    def get(self, model, key):
        from backend.persistence.models import DbSourceInfo, JobInfo

        if model is JobInfo:
            return self.step_row
        if model is DbSourceInfo:
            return self.source_row
        raise AssertionError(f"unexpected model {model}")  # pragma: no cover


@contextmanager
def _fake_platform_session(session):
    yield session


def _step_row(status="success", updated_at=STEP_AT):
    return SimpleNamespace(
        job_id=JOB_ID, step_name="rel", last_status=status, updated_at=updated_at
    )


def _source_row():
    return SimpleNamespace(
        source_id=SOURCE_ID,
        db_name=DB_NAME,
        is_active=True,
        selected_schemas=["public"],
        updated_at=STEP_AT,
    )


FK_ENTRY = {
    "relationship_id": "rel_fk000000001",
    "type": "single_column",
    "from_table": {"schema": "public", "table": "orders"},
    "from_column": "user_id",
    "to_table": {"schema": "public", "table": "users"},
    "to_column": "id",
    "discovery_method": "foreign_key_constraint",
    "target_source_type": "foreign_key",
    "source_constraint": None,
    "composite_score": 1.0,
    "confidence_level": "high",
    "cardinality": "N:1",
    "constraint_name": "orders_user_id_fkey",
}

RULE_ENTRY = {
    "relationship_id": "rel_rule00000002",
    "type": "single_column",
    "from_table": {"schema": "public", "table": "orders"},
    "from_column": "amount",
    "to_table": {"schema": "public", "table": "daily"},
    "to_column": "store_id",
    "discovery_method": "physical_key_matching",
    "target_source_type": "primary_key",
    "source_constraint": "single_field_unique_constraint",
    "composite_score": 0.85,
    "confidence_level": "medium",
    "metrics": {
        "inclusion_rate": 0.99,
        "name_similarity": 0.1,
        "comment_similarity": 0.0,
        "type_compatibility": 1.0,
        "jaccard_index": 0.5,
    },
    "cardinality": "N:1",
}

LLM_COMPOSITE_ENTRY = {
    "relationship_id": "rel_llm00000003",
    "type": "composite",
    "from_table": {"schema": "public", "table": "orders"},
    "from_columns": ["user_id", "amount"],
    "to_table": {"schema": "public", "table": "users"},
    "to_columns": ["id", "name"],
    "discovery_method": "llm_inferred",
    "target_source_type": "llm_inferred",
    "source_constraint": None,
    "composite_score": 0.92,
    "confidence_level": "high",
    "metrics": {
        "inclusion_rate": 0.98,
        "name_similarity": 0.9,
        "comment_similarity": 0.7,
        "type_compatibility": 1.0,
        "jaccard_index": 0.8,
    },
    "cardinality": "N:1",
}

ORIGINAL_ARTIFACT = {
    "generated_by": "rel_llm",
    "database": DB_NAME,
    "metadata_source": "json_profiles",
    "json_metadata_version": "3.2",
    "json_files_loaded": 3,
    "database_queries_executed": 12,
    "generated_timestamp": "2026-10-01T00:00:00Z",
    "statistics": {
        "total_relationships_found": 3,
        "foreign_key_relationships": 1,
        "composite_key_relationships": 1,
        "single_column_relationships": 2,
        "total_suppressed_single_relations": 1,
        "rule_only_relationships": 1,
        "llm_only_relationships": 0,
        "rule_llm_overlap_relationships": 0,
        "llm_total_pairs": 5,
        "llm_success_pairs": 4,
        "llm_failed_pairs": 1,
        "json_files_loaded": 3,
    },
    "relationships": [FK_ENTRY, RULE_ENTRY, LLM_COMPOSITE_ENTRY],
}

TABLE_COLUMNS = {
    "public.orders": ["id", "user_id", "amount"],
    "public.users": ["id", "name"],
    "public.daily": ["store_id", "date_day"],
}


def _write_workspace(tmp_path: Path, relationships=None) -> Path:
    root = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    rel_dir = root / REL_DIR_NAME
    rel_dir.mkdir(parents=True, exist_ok=True)
    json_dir = root / "json"
    json_dir.mkdir(exist_ok=True)
    (json_dir / "manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    for full_name, columns in TABLE_COLUMNS.items():
        schema, table = full_name.split(".", 1)
        profiles = {
            name: {"ordinal_position": i + 1, "data_type": "text"}
            for i, name in enumerate(columns)
        }
        (json_dir / f"{DB_NAME}.{schema}.{table}.json").write_text(
            json.dumps(
                {
                    "object_info": {
                        "database": DB_NAME,
                        "schema_name": schema,
                        "object_name": table,
                        "object_type": "table",
                    },
                    "column_profiles": profiles,
                }
            ),
            encoding="utf-8",
        )
    artifact = json.loads(json.dumps(ORIGINAL_ARTIFACT))
    if relationships is not None:
        artifact["relationships"] = relationships
    (rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (rel_dir / f"{DB_NAME}.relationships_global.md").write_text(
        "# 表间关系发现报告\n（采集直出）\n", encoding="utf-8"
    )
    return root


def _install(monkeypatch, tmp_path, step_status="success", workspace=True):
    import backend.profile_loading.rel_config_api as api

    if workspace:
        _write_workspace(tmp_path)
    root = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    session = FakeSession(
        step_row=_step_row(status=step_status), source_row=_source_row()
    )
    rec = SimpleNamespace(locked=[])

    monkeypatch.setattr(
        api, "platform_session", lambda settings: _fake_platform_session(session)
    )
    monkeypatch.setattr(
        api,
        "resolve_rel_output_dir",
        lambda s, j: root / REL_DIR_NAME,
    )
    monkeypatch.setattr(api, "resolve_json_output_dir", lambda s, j: root / "json")
    monkeypatch.setattr(api, "assert_workspace_owner", lambda *a: None)

    def fake_lock(_session, source_uuid):
        rec.locked.append(source_uuid)
        return _source_row()

    monkeypatch.setattr(api, "lock_db_source_row", fake_lock)
    monkeypatch.setattr(
        api,
        "load_main_config_snapshot",
        lambda _p: {
            "decision": {
                "high_confidence_threshold": 0.9,
                "medium_confidence_threshold": 0.8,
            }
        },
    )
    return rec


def _fingerprint_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _payload_entry(
    schema_from, columns_from, schema_to, columns_to, rid=None, cardinality="N:1"
):
    entry = {
        "from_table": {
            "schema": schema_from.split(".")[0],
            "table": schema_from.split(".")[1],
        },
        "to_table": {
            "schema": schema_to.split(".")[0],
            "table": schema_to.split(".")[1],
        },
        "from_columns": columns_from,
        "to_columns": columns_to,
        "cardinality": cardinality,
    }
    if rid is not None:
        entry["relationship_id"] = rid
    return entry


def _slim(entry):
    """PUT 客户端只送实质字段：id/表/基数 + 归一为列表的两侧列。"""
    from_cols = (
        entry["from_columns"] if "from_columns" in entry else [entry["from_column"]]
    )
    to_cols = entry["to_columns"] if "to_columns" in entry else [entry["to_column"]]
    return {
        k: v
        for k, v in entry.items()
        if k in {"relationship_id", "from_table", "to_table", "cardinality"}
    } | {"from_columns": from_cols, "to_columns": to_cols}


# ---- GET rel-config（A06） ----


def test_get_returns_relationships_fingerprint_tables_database(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    result = read_rel_config(object(), str(SOURCE_ID), str(JOB_ID))

    assert result["database"] == DB_NAME
    assert [r["relationship_id"] for r in result["relationships"]] == [
        "rel_fk000000001",
        "rel_rule00000002",
        "rel_llm00000003",
    ]
    assert set(result["tables"]) == set(TABLE_COLUMNS)
    rel_json = (
        tmp_path
        / "output"
        / str(SOURCE_ID)
        / str(JOB_ID)
        / "rel"
        / f"{DB_NAME}.relationships_global.json"
    )
    assert result["fingerprint"] == _fingerprint_of(rel_json)
    # 桩配置没有 cql_generation 时与 CQLGenerator 缺省一致
    assert result["score_threshold"] == 0.9


def test_get_returns_configured_score_threshold(monkeypatch, tmp_path):
    import backend.profile_loading.rel_config_api as api

    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api,
        "load_main_config_snapshot",
        lambda _p: {"cql_generation": {"composite_score_threshold": 0.95}},
    )
    result = read_rel_config(object(), str(SOURCE_ID), str(JOB_ID))
    assert result["score_threshold"] == 0.95
    assert len(result["relationships"]) == 3


@pytest.mark.parametrize("status", [None, "failed", "running"])
def test_get_rejects_when_rel_row_not_success(monkeypatch, tmp_path, status):
    _install(monkeypatch, tmp_path, step_status=status)
    with pytest.raises(RelConfigOutputMissingError, match="重跑"):
        read_rel_config(object(), str(SOURCE_ID), str(JOB_ID))


def test_get_missing_artifact_raises(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, workspace=False)
    with pytest.raises(RelConfigOutputMissingError):
        read_rel_config(object(), str(SOURCE_ID), str(JOB_ID))


def test_get_unparseable_artifact_raises(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    rel_json = (
        tmp_path
        / "output"
        / str(SOURCE_ID)
        / str(JOB_ID)
        / "rel"
        / f"{DB_NAME}.relationships_global.json"
    )
    rel_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(RelConfigInvalidError):
        read_rel_config(object(), str(SOURCE_ID), str(JOB_ID))


# ---- GET object-columns ----


def test_object_columns_returns_names_in_ordinal_order(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    result = read_rel_object_columns(
        object(), str(SOURCE_ID), str(JOB_ID), "public", "orders"
    )
    assert result["columns"] == ["id", "user_id", "amount"]


def test_object_columns_unknown_object_raises(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    with pytest.raises(RelConfigOutputMissingError):
        read_rel_object_columns(object(), str(SOURCE_ID), str(JOB_ID), "public", "nope")


# ---- PUT（A07–A10、A21、A25） ----


def _rel_paths(tmp_path):
    rel_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "rel"
    return (
        rel_dir / f"{DB_NAME}.relationships_global.json",
        rel_dir / f"{DB_NAME}.relationships_global.md",
    )


def test_put_rejects_duplicate_endpoints_and_accepts_reverse(monkeypatch, tmp_path):
    """同一起点表/列与终点表/列（列顺序一致）不可再新增；方向相反是另一条。"""
    _install(monkeypatch, tmp_path)
    json_path, _md_path = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)
    base = [_slim(FK_ENTRY), _slim(RULE_ENTRY), _slim(LLM_COMPOSITE_ENTRY)]
    duplicate = _payload_entry(
        "public.orders", ["user_id"], "public.users", ["id"], cardinality="1:1"
    )
    with pytest.raises(RelConfigInvalidError, match="这条关系已存在"):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [*base, duplicate]},
        )

    reverse = _payload_entry("public.users", ["id"], "public.orders", ["user_id"])
    write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {"fingerprint": fp, "relationships": [*base, reverse]},
    )
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["relationships"][-1]["from_table"]["table"] == "users"
    assert data["relationships"][-1]["to_table"]["table"] == "orders"


def test_put_adds_manual_entry_with_d7_schema(monkeypatch, tmp_path):
    """A07：新增手工条目按 D7 定型；statistics 重算、遥测保留、md 重渲染。"""
    rec = _install(monkeypatch, tmp_path)
    json_path, md_path = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    new = _payload_entry(
        "public.daily",
        ["store_id", "date_day"],
        "public.orders",
        ["user_id", "amount"],
        cardinality="M:N",
    )
    result = write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {
            "fingerprint": fp,
            "relationships": [
                _slim(FK_ENTRY),
                _slim(RULE_ENTRY),
                _slim(LLM_COMPOSITE_ENTRY),
                new,
            ],
        },
    )
    assert rec.locked == [SOURCE_ID]

    data = json.loads(json_path.read_text(encoding="utf-8"))
    added = data["relationships"][-1]
    assert added["relationship_id"].startswith("rel_manual_")
    assert added["type"] == "composite"
    assert added["from_columns"] == ["store_id", "date_day"]
    assert added["discovery_method"] == "manual"
    assert added["target_source_type"] == "manual"
    assert added["source_constraint"] is None
    assert added["composite_score"] == 1.0
    assert added["confidence_level"] == "high"
    assert "metrics" not in added and "constraint_name" not in added
    assert added["cardinality"] == "M:N"

    stats = data["statistics"]
    assert stats["total_relationships_found"] == 4
    assert stats["foreign_key_relationships"] == 1
    assert stats["composite_key_relationships"] == 2
    assert stats["single_column_relationships"] == 2
    assert stats["rule_only_relationships"] == 1  # 遥测保留
    assert stats["json_files_loaded"] == 3  # 顶层字段保留

    md_text = md_path.read_text(encoding="utf-8")
    assert "- 手工: 1" in md_text
    assert "- **源列**: `store_id, date_day`" in md_text
    assert "- **关系类型**: manual" in md_text
    assert result["fingerprint"] == _fingerprint_of(json_path)


def test_put_modifying_rule_entry_relabels_d8(monkeypatch, tmp_path):
    """A08：改规则条目实质字段 → 七项重标、保留 id（含 primary_key 溯源清除）。"""
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    changed = _payload_entry(
        "public.orders",
        ["amount"],
        "public.users",
        ["id"],
        rid=RULE_ENTRY["relationship_id"],
    )
    write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {
            "fingerprint": fp,
            "relationships": [_slim(FK_ENTRY), changed, _slim(LLM_COMPOSITE_ENTRY)],
        },
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    relabeled = next(
        r
        for r in data["relationships"]
        if r["relationship_id"] == RULE_ENTRY["relationship_id"]
    )
    assert relabeled["to_table"] == {"schema": "public", "table": "users"}
    assert relabeled["to_column"] == "id"
    assert relabeled["discovery_method"] == "manual"
    assert relabeled["target_source_type"] == "manual"
    assert relabeled["source_constraint"] is None
    assert relabeled["composite_score"] == 1.0
    assert relabeled["confidence_level"] == "high"
    assert "metrics" not in relabeled


def test_put_modifying_fk_entry_clears_constraint_name(monkeypatch, tmp_path):
    """A08 变体：改 FK 条目 → 重标 manual 并清除 constraint_name。"""
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    changed_fk = _payload_entry(
        "public.orders",
        ["user_id"],
        "public.users",
        ["name"],
        rid=FK_ENTRY["relationship_id"],
    )
    write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {
            "fingerprint": fp,
            "relationships": [
                changed_fk,
                _slim(RULE_ENTRY),
                _slim(LLM_COMPOSITE_ENTRY),
            ],
        },
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    entry = next(
        r
        for r in data["relationships"]
        if r["relationship_id"] == FK_ENTRY["relationship_id"]
    )
    assert entry["discovery_method"] == "manual"
    assert "constraint_name" not in entry
    assert data["statistics"]["foreign_key_relationships"] == 0


def test_put_delete_updates_statistics_keeps_telemetry(monkeypatch, tmp_path):
    """A09：删除条目 → 计数重算、遥测保留。"""
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {"fingerprint": fp, "relationships": [_slim(FK_ENTRY), _slim(RULE_ENTRY)]},
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["relationships"]) == 2
    stats = data["statistics"]
    assert stats["total_relationships_found"] == 2
    assert stats["composite_key_relationships"] == 0
    assert stats["single_column_relationships"] == 2
    assert stats["total_suppressed_single_relations"] == 1
    assert stats["llm_total_pairs"] == 5


def test_put_fingerprint_mismatch_raises_409(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    with pytest.raises(RelRegeneratedError):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": "stale0000000000ff", "relationships": []},
        )


def test_put_unknown_relationship_id_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)
    entry = _payload_entry(
        "public.orders", ["user_id"], "public.users", ["id"], rid="rel_ghost0000009"
    )
    with pytest.raises(RelConfigInvalidError, match="未知 relationship_id"):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [entry]},
        )


def test_put_unknown_column_and_bad_cardinality_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    bad_column = _payload_entry(
        "public.orders", ["no_such_col"], "public.users", ["id"]
    )
    with pytest.raises(RelConfigInvalidError, match="列名"):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [bad_column]},
        )

    bad_cardinality = _payload_entry(
        "public.orders", ["user_id"], "public.users", ["id"], cardinality="1:N"
    )
    with pytest.raises(RelConfigInvalidError, match="基数"):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [bad_cardinality]},
        )


def test_put_empty_list_allowed(monkeypatch, tmp_path):
    """D10 衔接：删光全部关系合法——计数归零、md 重渲染、指纹回带。"""
    _install(monkeypatch, tmp_path)
    json_path, md_path = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    result = write_rel_config(
        object(), str(SOURCE_ID), str(JOB_ID), {"fingerprint": fp, "relationships": []}
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["relationships"] == []
    stats = data["statistics"]
    assert stats["total_relationships_found"] == 0
    assert stats["composite_key_relationships"] == 0
    assert stats["single_column_relationships"] == 0
    assert "手工" not in md_path.read_text(encoding="utf-8")
    assert result["fingerprint"] == _fingerprint_of(json_path)


def test_put_substantive_unchanged_overwrites_client_fields(monkeypatch, tmp_path):
    """A21：实质字段未变时，客户端带来的分数/明细/方法一律被服务端原值覆盖。"""
    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    tampered = _slim(FK_ENTRY)
    tampered["composite_score"] = 0.5
    tampered["confidence_level"] = "low"
    tampered["discovery_method"] = "manual"
    tampered["metrics"] = {"inclusion_rate": 0.0}

    write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {"fingerprint": fp, "relationships": [tampered]},
    )

    data = json.loads(json_path.read_text(encoding="utf-8"))
    kept = data["relationships"][0]
    assert kept == FK_ENTRY


def test_put_render_failure_leaves_both_files_untouched(monkeypatch, tmp_path):
    """A25：渲染失败（阶段一）→ 500，两份原文件字节不变、无 .tmp 残留。"""
    import backend.profile_loading.rel_config_api as api

    _install(monkeypatch, tmp_path)
    json_path, md_path = _rel_paths(tmp_path)
    json_before = json_path.read_bytes()
    md_before = md_path.read_bytes()
    fp = _fingerprint_of(json_path)

    def boom(*_a, **_k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(api, "render_markdown_report", boom)
    with pytest.raises(RelWriteFailedError):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [_slim(FK_ENTRY)]},
        )

    assert json_path.read_bytes() == json_before
    assert md_path.read_bytes() == md_before
    rel_dir = json_path.parent
    assert [p for p in rel_dir.iterdir() if p.name.startswith(".tmp")] == []
    # 指纹未失效：用原指纹直接重试成功
    monkeypatch.undo()
    _install(monkeypatch, tmp_path)
    result = write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {"fingerprint": fp, "relationships": [_slim(FK_ENTRY)]},
    )
    assert result["fingerprint"] == _fingerprint_of(json_path)


def test_put_replace_failure_keeps_json_intact(monkeypatch, tmp_path):
    """A25：替换失败（阶段二，json 替换被注入失败）→ json 未变、可原指纹重试。"""
    import backend.profile_loading.rel_config_api as api

    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    json_before = json_path.read_bytes()
    fp = _fingerprint_of(json_path)

    real_replace = api._replace_file
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二次替换是 json（先 md 后 json）
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(api, "_replace_file", flaky)
    with pytest.raises(RelWriteFailedError, match="JSON 未写入"):
        write_rel_config(
            object(),
            str(SOURCE_ID),
            str(JOB_ID),
            {"fingerprint": fp, "relationships": [_slim(FK_ENTRY)]},
        )
    assert json_path.read_bytes() == json_before
    assert [p for p in json_path.parent.iterdir() if p.name.startswith(".tmp")] == []

    monkeypatch.setattr(api, "_replace_file", real_replace)
    result = write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {"fingerprint": fp, "relationships": [_slim(FK_ENTRY)]},
    )
    assert result["fingerprint"] == _fingerprint_of(json_path)


def test_routes_and_error_handlers_registered():
    """§4.5：三码 + 404 handler 与三端点路由注册到位。"""
    from fastapi import FastAPI

    from backend.api.errors import register_exception_handlers
    from backend.api.routers.publishing import router
    from backend.profile_loading import rel_config_api as api

    app = FastAPI()
    register_exception_handlers(app)
    for exc in (
        api.RelConfigOutputMissingError,
        api.RelConfigInvalidError,
        api.RelRegeneratedError,
        api.RelWriteFailedError,
    ):
        assert exc in app.exception_handlers

    paths = {route.path for route in router.routes}
    base = "/api/data-sources/{source_id}/collection-jobs/{job_id}/rel-config"
    assert base in paths
    assert f"{base}/object-columns" in paths


def test_a11_manual_entries_survive_real_generator_chain(monkeypatch, tmp_path):
    """A11（需求 8，§4.4 的可执行化）：PUT 落盘的产物（含 D7 手工复合条目）
    直接过真实 JSONReader/CQLGenerator——生成脚本含对应 JOIN_ON；推断条目
    按阈值过滤（0.85 < 0.9），FK 直通保留。"""
    from pathlib import Path

    from metaweave.core.cql_generator.generator import CQLGenerator

    _install(monkeypatch, tmp_path)
    json_path, _ = _rel_paths(tmp_path)
    fp = _fingerprint_of(json_path)

    new = _payload_entry(
        "public.daily",
        ["store_id", "date_day"],
        "public.orders",
        ["user_id", "amount"],
        cardinality="M:N",
    )
    result = write_rel_config(
        object(),
        str(SOURCE_ID),
        str(JOB_ID),
        {
            "fingerprint": fp,
            "relationships": [
                _slim(FK_ENTRY),
                _slim(RULE_ENTRY),
                _slim(LLM_COMPOSITE_ENTRY),
                new,
            ],
        },
    )
    manual_id = result["relationships"][-1]["relationship_id"]

    root = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    generator = CQLGenerator(
        Path("configs/metadata_config.yaml"),
        config={
            "database": {"database": DB_NAME},
            "output": {
                "json_directory": str(root / "json"),
                "rel_directory": str(root / "rel"),
                "cql_directory": str(root / "cql"),
            },
        },
    )
    gen = generator.generate()
    assert gen.success is True
    script = (root / "cql" / f"import_all.{DB_NAME}.cypher").read_text(encoding="utf-8")

    # 手工复合条目（D7）经八步校验进入 JOIN_ON（需求 8 的核心断言）
    assert manual_id in script
    # JOIN_ON UNWIND 数据键名按 JOINOnRelation.to_cypher_dict：
    # source_columns / target_columns；数据经 json.dumps(indent=2) 落盘，
    # 数组元素分行——用跨行正则匹配
    assert re.search(r'"source_columns": \[\s*"store_id",\s*"date_day"\s*\]', script)
    assert re.search(r'"target_columns": \[\s*"user_id",\s*"amount"\s*\]', script)
    # FK 直通 + LLM 复合（0.92 ≥ 0.9）保留
    assert FK_ENTRY["relationship_id"] in script
    assert LLM_COMPOSITE_ENTRY["relationship_id"] in script
    # 规则条目 0.85 < 默认阈值 0.9 被滤（A12 同口径）
    assert RULE_ENTRY["relationship_id"] not in script
