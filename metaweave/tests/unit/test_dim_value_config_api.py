"""doc 22.2 任务 2：dim_value 配置编辑 API 的单元测试（A04–A08）。

FakeSession 提供 get(JobInfo)/get(DbSourceInfo) 两个最小分发；产物目录用 tmp_path
构造受管路径。发布拦截三态 classify 函数在任务 3 的测试中覆盖，本文件聚焦编辑端点。
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from backend.profile_loading.dim_value_config_api import (
    DimConfigOutputMissingError,
    DimConfigRegeneratedError,
    DimValueColumnMissingError,
    DimValueConfigInvalidError,
    classify_dim_tables_yaml,
    list_dim_value_objects,
    read_dim_value_config,
    read_dim_value_object_columns,
    write_dim_value_config,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "orders"
STEP_AT = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)


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


def _install(monkeypatch, session, tmp_path=None):
    import backend.profile_loading.dim_value_config_api as api

    monkeypatch.setattr(
        api, "platform_session", lambda settings: _fake_platform_session(session)
    )
    if tmp_path is not None:
        sid, jid = str(SOURCE_ID), str(JOB_ID)
        root = tmp_path / sid / jid
        monkeypatch.setattr(
            api, "resolve_dim_config_output_dir", lambda s, j: root / "dim_config"
        )
        monkeypatch.setattr(api, "resolve_json_output_dir", lambda s, j: root / "json")


def _step_row(updated_at=STEP_AT, status="success"):
    return SimpleNamespace(
        job_id=JOB_ID, step_name="dim_config", last_status=status, updated_at=updated_at
    )


def _source_row():
    return SimpleNamespace(
        source_id=SOURCE_ID, db_name=DB_NAME, is_active=True, updated_at=STEP_AT
    )


def _artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / str(SOURCE_ID) / str(JOB_ID)
    (root / "dim_config").mkdir(parents=True)
    return root


def _write_yaml(root: Path, content: str):
    (root / "dim_config" / "dim_tables.yaml").write_text(content, encoding="utf-8")


def _write_json_products(root: Path):
    json_dir = root / "json"
    json_dir.mkdir()
    (json_dir / "manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    (json_dir / f"{DB_NAME}.public.t1.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": DB_NAME,
                    "schema_name": "public",
                    "object_name": "t1",
                    "object_type": "table",
                },
                "column_profiles": {
                    "b": {"ordinal_position": 2, "data_type": "text", "comment": "B列"},
                    "a": {
                        "ordinal_position": 1,
                        "data_type": "integer",
                        "comment": "A列",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (json_dir / f"{DB_NAME}.public.mv1.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": DB_NAME,
                    "schema_name": "public",
                    "object_name": "mv1",
                    "object_type": "materialized_view",
                },
                "column_profiles": {},
            }
        ),
        encoding="utf-8",
    )
    # schema 含点：GET objects 排除（审核修订 18）
    (json_dir / f"{DB_NAME}.we.ird.t2.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": DB_NAME,
                    "schema_name": "we.ird",
                    "object_name": "t2",
                    "object_type": "table",
                },
                "column_profiles": {},
            }
        ),
        encoding="utf-8",
    )
    return json_dir


SETTINGS = SimpleNamespace(database_url="fake")


# ---- A04 GET dim-config ----


def test_a04_get_returns_tables_and_version(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {
                        "tables": {
                            "public.a": {"embedding_col": "name"},
                            "public.b": {"embedding_col": None},
                            "public.multi": {"embedding_col": ["x", "y"]},
                        }
                    }
                }
            }
        ),
    )
    session = FakeSession(step_row=_step_row(), source_row=_source_row())
    _install(monkeypatch, session, tmp_path)

    result = read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)

    assert result["version"] == STEP_AT.isoformat()
    tables = {
        t["schema_name"] + "." + t["object_name"]: t["embedding_col"]
        for t in result["tables"]
    }
    assert tables == {
        "public.a": "name",
        "public.b": None,
        "public.multi": None,
    }  # 多列归 null（§5.3）


@pytest.mark.parametrize(
    "content",
    ["databases: {}\n", yaml.safe_dump({"databases": {DB_NAME: {"tables": {}}}})],
    ids=["native-empty", "saved-empty"],
)
def test_a04_empty_variants_return_empty_list(tmp_path, monkeypatch, content):
    root = _artifact_root(tmp_path)
    _write_yaml(root, content)
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    result = read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert result["tables"] == []


def test_a04_missing_artifact_raises_404(tmp_path, monkeypatch):
    _artifact_root(tmp_path)  # 目录在、文件不在
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    with pytest.raises(DimConfigOutputMissingError):
        read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)


def test_a04_missing_step_row_raises_404(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    _install(
        monkeypatch, FakeSession(step_row=None, source_row=_source_row()), tmp_path
    )

    with pytest.raises(DimConfigOutputMissingError):
        read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)


def test_a04_invalid_yaml_raises_400(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "not: [valid\n")
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    with pytest.raises(DimValueConfigInvalidError) as excinfo:
        read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert excinfo.value.details["reason"]


# ---- A05 GET objects ----


def test_a05_objects_listing(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_json_products(root)
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    result = list_dim_value_objects(SETTINGS, SOURCE_ID, JOB_ID)
    objects = {
        (o["schema_name"], o["object_name"]): o["object_type"]
        for o in result["objects"]
    }
    assert ("public", "t1") in objects
    assert objects[("public", "mv1")] == "materialized_view"
    assert ("we.ird", "t2") not in objects  # schema 含点排除
    assert all(
        o["object_name"] != "manifest" for o in result["objects"]
    )  # manifest 排除


def test_a05_objects_missing_dir_raises_404(tmp_path, monkeypatch):
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )
    with pytest.raises(DimConfigOutputMissingError):
        list_dim_value_objects(SETTINGS, SOURCE_ID, JOB_ID)


# ---- A06 GET object-columns ----


def test_a06_columns_sorted_by_ordinal(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_json_products(root)
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    result = read_dim_value_object_columns(SETTINGS, SOURCE_ID, JOB_ID, "public", "t1")
    assert [c["name"] for c in result["columns"]] == ["a", "b"]
    assert result["columns"][0] == {
        "name": "a",
        "data_type": "integer",
        "comment": "A列",
        "sample_value": "",
    }


def test_a06_sample_value_from_first_non_null_record(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    json_dir = root / "json"
    json_dir.mkdir()
    (json_dir / "manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    (json_dir / f"{DB_NAME}.public.t1.json").write_text(
        json.dumps(
            {
                "object_info": {
                    "database": DB_NAME,
                    "schema_name": "public",
                    "object_name": "t1",
                    "object_type": "table",
                },
                "column_profiles": {
                    "a": {"ordinal_position": 1, "data_type": "integer", "comment": ""},
                    "note": {"ordinal_position": 2, "data_type": "text", "comment": ""},
                },
                "sample_records": {
                    "records": [
                        {"a": None, "note": "line1\nline2"},
                        {"a": 7, "note": "fallback"},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    result = read_dim_value_object_columns(SETTINGS, SOURCE_ID, JOB_ID, "public", "t1")
    by_name = {c["name"]: c for c in result["columns"]}
    assert by_name["a"]["sample_value"] == "7"
    assert by_name["note"]["sample_value"] == "line1\nline2"


def test_a06_unknown_object_raises_404(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_json_products(root)
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    with pytest.raises(DimConfigOutputMissingError):
        read_dim_value_object_columns(SETTINGS, SOURCE_ID, JOB_ID, "public", "nope")


# ---- A07/A08 PUT ----


def _payload(tables, version=STEP_AT.isoformat()):
    return {
        "tables": [
            {"schema_name": s, "object_name": o, "embedding_col": c}
            for s, o, c in tables
        ],
        "version": version,
    }


def test_a07_empty_embedding_col_rejected(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    with pytest.raises(DimValueColumnMissingError) as excinfo:
        write_dim_value_config(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            _payload([("public", "a", "name"), ("public", "b", "  ")]),
        )
    assert excinfo.value.details["tables"] == ["public.b"]


def test_a07_structural_violations_rejected(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    # schema 含点
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, SOURCE_ID, JOB_ID, _payload([("pu.blic", "t", "c")])
        )
    # 表清单非列表
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, SOURCE_ID, JOB_ID, {"tables": "x", "version": "v"}
        )
    # 重复键
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            _payload([("public", "t", "a"), ("public", "t", "b")]),
        )
    # 含逗号（多列形态）
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, SOURCE_ID, JOB_ID, _payload([("public", "t", "a, b")])
        )


def test_a08_write_roundtrip_and_yaml_dump(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    session = FakeSession(step_row=_step_row(), source_row=_source_row())
    _install(monkeypatch, session, tmp_path)

    result = write_dim_value_config(
        SETTINGS,
        SOURCE_ID,
        JOB_ID,
        _payload([("public", "yes", "true"), ("pub", "my.table", "col name")]),
    )
    assert len(result["tables"]) == 2

    written = (root / "dim_config" / "dim_tables.yaml").read_text(encoding="utf-8")
    # 与生成器同款 yaml.dump 渲染：裸词/特殊键自动引号，往返仍为字符串
    parsed = yaml.safe_load(written)
    tables = parsed["databases"][DB_NAME]["tables"]
    assert set(tables) == {"public.yes", "pub.my.table"}
    assert tables["public.yes"]["embedding_col"] == "true"
    assert tables["pub.my.table"]["embedding_col"] == "col name"

    # 可被 from_yaml 无损读回
    from metaweave.core.dim_value.models import DimTablesConfig

    cfg = DimTablesConfig.from_yaml(parsed, database=DB_NAME)
    assert cfg.tables["pub.my.table"].embedding_col == "col name"

    # GET 读回一致（schema+object 分开返回）
    got = read_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID)
    got_map = {
        t["schema_name"] + "." + t["object_name"]: t["embedding_col"]
        for t in got["tables"]
    }
    assert got_map == {"public.yes": "true", "pub.my.table": "col name"}

    # 无临时文件残留
    assert not list((root / "dim_config").glob("*.tmp"))


def test_a08_empty_list_allowed_and_writes_full_structure(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    write_dim_value_config(SETTINGS, SOURCE_ID, JOB_ID, _payload([]))
    written = (root / "dim_config" / "dim_tables.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(written)
    # 空清单写完整带键结构（审核修订 3），与采集原生 databases: {} 区分
    assert parsed["databases"][DB_NAME]["tables"] == {}


def test_a08_version_mismatch_raises_409(tmp_path, monkeypatch):
    root = _artifact_root(tmp_path)
    _write_yaml(root, "databases: {}\n")
    _install(
        monkeypatch,
        FakeSession(step_row=_step_row(), source_row=_source_row()),
        tmp_path,
    )

    with pytest.raises(DimConfigRegeneratedError):
        write_dim_value_config(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            _payload([("public", "t", "c")], version="1999-01-01T00:00:00+00:00"),
        )
    # 版本不匹配时不得写文件（内容仍是占位）
    assert (root / "dim_config" / "dim_tables.yaml").read_text(
        encoding="utf-8"
    ) == "databases: {}\n"


# ---- 发布拦截三态 classify（供任务 3 复用的共享读取函数）----


@pytest.mark.parametrize(
    "content,expected",
    [
        ("databases: {}\n", "empty"),
        (yaml.safe_dump({"databases": {DB_NAME: {"tables": {}}}}), "empty"),
        (
            yaml.safe_dump(
                {
                    "databases": {
                        "other": {"tables": {"public.a": {"embedding_col": "x"}}}
                    }
                }
            ),
            "empty",
        ),
        ("not: [valid\n", "invalid"),
        ("- a\n- b\n", "invalid"),
        ("", "invalid"),
    ],
    ids=[
        "native-empty",
        "saved-empty",
        "other-db",
        "bad-yaml",
        "top-level-list",
        "blank",
    ],
)
def test_classify_states(tmp_path, content, expected):
    path = tmp_path / "dim_tables.yaml"
    path.write_text(content, encoding="utf-8")
    state, reason, cfg = classify_dim_tables_yaml(path, DB_NAME)
    assert state == expected
    if expected == "empty":
        assert cfg is not None and cfg.tables == {}
    if expected == "invalid":
        assert reason


def test_classify_missing_file_is_invalid(tmp_path):
    state, reason, _ = classify_dim_tables_yaml(tmp_path / "absent.yaml", DB_NAME)
    assert state == "invalid"
    assert "不存在" in reason


def test_classify_tables_with_unconfigured(tmp_path):
    path = tmp_path / "dim_tables.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {
                        "tables": {
                            "public.a": {"embedding_col": "name"},
                            "public.b": {"embedding_col": None},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state, _, cfg = classify_dim_tables_yaml(path, DB_NAME)
    assert state == "tables"
    assert cfg.tables["public.b"].embedding_col is None
