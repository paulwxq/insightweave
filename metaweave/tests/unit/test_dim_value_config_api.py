"""doc 22.2 任务 2、6.2 §5.2：dim_value 配置编辑 API 的单元测试（新模型）。

按工作区定位；准入 = dim 目录顶层唯一成功采集 marker；版本锚 = dim_tables.yaml
内容 sha256（GET 返回 version，PUT 校验，409 DimConfigRegeneratedError）；
保存前文件级备份（.backup/bak_时间戳/，失败本次保存终止且不动原内容）。
发布拦截三态 classify 函数与 GET 共用同一口径。
"""

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

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
from backend.workspace_files.backups import EditBackupFailedError
from backend.workspace_files.markers import list_collection_markers
from backend.workspace_files.markers import write_collection_marker

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("33333333-4444-4555-8666-777777777777")
DB_NAME = "orders"
SETTINGS = SimpleNamespace(database_url="fake")


class FakeSession:
    def __init__(self, workspace_row=None, source_row=None):
        self.workspace_row = workspace_row
        self.source_row = source_row

    def get(self, model, key, with_for_update=None):
        from backend.persistence.models import DbSourceInfo, WorkspaceInfo

        if model is WorkspaceInfo:
            if self.workspace_row is None or key != self.workspace_row.workspace_id:
                return None
            return self.workspace_row
        if model is DbSourceInfo:
            if self.source_row is None or key != self.source_row.source_id:
                return None
            return self.source_row
        raise AssertionError(f"unexpected model {model}")  # pragma: no cover


@contextmanager
def _fake_platform_session(session):
    yield session


def _workspace_row():
    return SimpleNamespace(workspace_id=WORKSPACE_ID, source_id=SOURCE_ID)


def _source_row():
    return SimpleNamespace(
        source_id=SOURCE_ID,
        db_name=DB_NAME,
        is_active=True,
        selected_schemas=["public"],
    )


def _install(monkeypatch, tmp_path: Path, session=None, *, marker="success"):
    import backend.profile_loading.dim_value_config_api as api

    session = session if session is not None else FakeSession(
        _workspace_row(), _source_row()
    )
    root = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    dim_dir = root / "dim"
    dim_dir.mkdir(parents=True, exist_ok=True)
    (root / "json").mkdir(exist_ok=True)
    if marker is not None:
        write_collection_marker(dim_dir, RUN_ID, marker)

    monkeypatch.setattr(
        api, "platform_session", lambda settings: _fake_platform_session(session)
    )
    monkeypatch.setattr(api, "get_db_source", lambda _s, _sid: session.source_row)
    monkeypatch.setattr(
        "backend.workspaces.repository.get_workspace",
        lambda _s, _wid: session.workspace_row,
    )
    monkeypatch.setattr(api, "resolve_dim_output_dir", lambda s, w: root / "dim")
    monkeypatch.setattr(api, "resolve_json_output_dir", lambda s, w: root / "json")
    return root


def _write_yaml(root: Path, content: str):
    (root / "dim" / "dim_tables.yaml").write_text(content, encoding="utf-8")


def _version_of(root: Path) -> str:
    return hashlib.sha256((root / "dim" / "dim_tables.yaml").read_bytes()).hexdigest()


def _write_json_products(root: Path):
    json_dir = root / "json"
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


# ---- A04 GET dim-config ----


def test_a04_get_returns_tables_and_content_hash_version(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    content = yaml.safe_dump(
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
    )
    _write_yaml(root, content)

    result = read_dim_value_config(SETTINGS, WORKSPACE_ID)

    # 版本锚 = 文件内容 sha256（doc 6.2 §5.2）
    assert result["version"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert len(result["version"]) == 64
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
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, content)

    result = read_dim_value_config(SETTINGS, WORKSPACE_ID)
    assert result["tables"] == []


def test_a04_missing_artifact_raises_404(tmp_path, monkeypatch):
    _install(monkeypatch, tmp_path)  # 目录在、文件不在
    with pytest.raises(DimConfigOutputMissingError):
        read_dim_value_config(SETTINGS, WORKSPACE_ID)


@pytest.mark.parametrize(
    "marker,match",
    [(None, "没有 Dim 采集 marker"), ("failed", "重跑")],
)
def test_a04_marker_not_success_raises_404(tmp_path, monkeypatch, marker, match):
    root = _install(monkeypatch, tmp_path, marker=marker)
    _write_yaml(root, "databases: {}\n")
    with pytest.raises(DimConfigOutputMissingError, match=match):
        read_dim_value_config(SETTINGS, WORKSPACE_ID)


def test_a04_workspace_missing_raises_404(tmp_path, monkeypatch):
    root = _install(
        monkeypatch, tmp_path, FakeSession(None, _source_row()), marker=None
    )
    _write_yaml(root, "databases: {}\n")
    with pytest.raises(DimConfigOutputMissingError, match="工作区不存在"):
        read_dim_value_config(SETTINGS, WORKSPACE_ID)


def test_a04_invalid_yaml_raises_400(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "not: [valid\n")

    with pytest.raises(DimValueConfigInvalidError) as excinfo:
        read_dim_value_config(SETTINGS, WORKSPACE_ID)
    assert excinfo.value.details["reason"]


# ---- A05 GET objects ----


def test_a05_objects_listing(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_json_products(root)

    result = list_dim_value_objects(SETTINGS, WORKSPACE_ID)
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
    root = _install(monkeypatch, tmp_path)
    (root / "json").rmdir()
    with pytest.raises(DimConfigOutputMissingError):
        list_dim_value_objects(SETTINGS, WORKSPACE_ID)


# ---- A06 GET object-columns ----


def test_a06_columns_sorted_by_ordinal(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_json_products(root)

    result = read_dim_value_object_columns(SETTINGS, WORKSPACE_ID, "public", "t1")
    assert [c["name"] for c in result["columns"]] == ["a", "b"]
    assert result["columns"][0] == {
        "name": "a",
        "data_type": "integer",
        "comment": "A列",
        "sample_value": "",
    }


def test_a06_sample_value_from_first_non_null_record(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    json_dir = root / "json"
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

    result = read_dim_value_object_columns(SETTINGS, WORKSPACE_ID, "public", "t1")
    by_name = {c["name"]: c for c in result["columns"]}
    assert by_name["a"]["sample_value"] == "7"
    assert by_name["note"]["sample_value"] == "line1\nline2"


def test_a06_unknown_object_raises_404(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_json_products(root)

    with pytest.raises(DimConfigOutputMissingError):
        read_dim_value_object_columns(SETTINGS, WORKSPACE_ID, "public", "nope")


# ---- A07/A08 PUT ----


def _payload(tables, version=None):
    return {
        "tables": [
            {"schema_name": s, "object_name": o, "embedding_col": c}
            for s, o, c in tables
        ],
        "version": version if version is not None else "0" * 64,
    }


def test_a07_empty_embedding_col_rejected(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    with pytest.raises(DimValueColumnMissingError) as excinfo:
        write_dim_value_config(
            SETTINGS,
            WORKSPACE_ID,
            _payload([("public", "a", "name"), ("public", "b", "  ")]),
        )
    assert excinfo.value.details["tables"] == ["public.b"]


def test_a07_structural_violations_rejected(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    # schema 含点
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, _payload([("pu.blic", "t", "c")])
        )
    # 表清单非列表
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, {"tables": "x", "version": "v"}
        )
    # 重复键
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS,
            WORKSPACE_ID,
            _payload([("public", "t", "a"), ("public", "t", "b")]),
        )
    # 含逗号（多列形态）
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, _payload([("public", "t", "a, b")])
        )


def test_a08_write_roundtrip_and_yaml_dump(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    result = write_dim_value_config(
        SETTINGS,
        WORKSPACE_ID,
        _payload(
            [("public", "yes", "true"), ("pub", "my.table", "col name")],
            version=_version_of(root),
        ),
    )
    assert len(result["tables"]) == 2

    written = (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
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

    # GET 读回一致（schema+object 分开返回；version 随新内容变化）
    got = read_dim_value_config(SETTINGS, WORKSPACE_ID)
    got_map = {
        t["schema_name"] + "." + t["object_name"]: t["embedding_col"]
        for t in got["tables"]
    }
    assert got_map == {"public.yes": "true", "pub.my.table": "col name"}
    assert got["version"] == _version_of(root)

    # 无临时文件残留
    assert not list((root / "dim").glob("*.tmp"))


def test_a08_empty_list_allowed_and_writes_full_structure(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    write_dim_value_config(
        SETTINGS, WORKSPACE_ID, _payload([], version=_version_of(root))
    )
    written = (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(written)
    # 空清单写完整带键结构（审核修订 3），与采集原生 databases: {} 区分
    assert parsed["databases"][DB_NAME]["tables"] == {}


def test_a08_version_mismatch_raises_409(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    with pytest.raises(DimConfigRegeneratedError):
        write_dim_value_config(
            SETTINGS,
            WORKSPACE_ID,
            _payload([("public", "t", "c")], version="0" * 64),
        )
    # 版本不匹配时不得写文件（内容仍是占位）
    assert (
        (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
        == "databases: {}\n"
    )


# ---- 文件级备份（doc 6.2 §5.2、A28） ----


def test_a08_write_backs_up_old_yaml_and_keeps_marker(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")
    marker_names = [p.name for p in list_collection_markers(root / "dim")]

    write_dim_value_config(
        SETTINGS,
        WORKSPACE_ID,
        _payload([("public", "t", "c")], version=_version_of(root)),
    )

    backup_root = root / "dim" / ".backup"
    backups = sorted(backup_root.iterdir())
    assert len(backups) == 1 and backups[0].name.startswith("bak_")
    assert [p.name for p in sorted(backups[0].iterdir())] == ["dim_tables.yaml"]
    assert (backups[0] / "dim_tables.yaml").read_text(encoding="utf-8") == (
        "databases: {}\n"
    )
    assert [p.name for p in list_collection_markers(root / "dim")] == marker_names


def test_a08_backup_failure_aborts_save_without_touching_anything(
    tmp_path, monkeypatch
):
    """A28：备份失败 → 本次保存未执行，原文件与 marker 不动，无备份目录。"""
    import backend.workspace_files.backups as backups_module

    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")
    marker_names = [p.name for p in list_collection_markers(root / "dim")]
    calls = []

    def failing_backup(directory, relative_paths, **kwargs):
        calls.append((Path(directory), list(relative_paths)))
        raise EditBackupFailedError("注入的备份失败")

    monkeypatch.setattr(backups_module, "backup_files_before_edit", failing_backup)
    from backend.profile_loading.dim_value_config_api import (
        DimConfigBackupFailedError,
    )

    with pytest.raises(DimConfigBackupFailedError):
        write_dim_value_config(
            SETTINGS,
            WORKSPACE_ID,
            _payload([("public", "t", "c")], version=_version_of(root)),
        )
    # 备份失败 → 独立 500 错误（与 domain/rel 口径一致，A28）
    assert calls == [(root / "dim", ["dim_tables.yaml"])]
    assert (
        (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
        == "databases: {}\n"
    )
    assert [p.name for p in list_collection_markers(root / "dim")] == marker_names
    assert not (root / "dim" / ".backup").exists()


# ---- 发布拦截三态 classify（供发布链路复用的共享读取函数）----


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
