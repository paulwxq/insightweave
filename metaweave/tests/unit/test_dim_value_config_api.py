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

    def scalars(self, statement):
        # doc 8.1 §7.3：删除任务查询（本假件不建模任务表 → 无未解决任务）
        return iter([])

    def execute(self, _statement):
        # doc 10.2 §5.2：活动发布准入查询（本假件不建模发布运行 → 无冲突）
        from types import SimpleNamespace as _NS

        return _NS(
            scalar_one_or_none=lambda: None,
            first=lambda: None,
            all=lambda: [],
            scalar_one=lambda: 0,
        )

    def get(self, model, key, **kwargs):
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
        "public.multi": ["x", "y"],
    }  # doc 7.2 §3.1.1：人工多字段列表完整保留，不再归 null


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
    assert result["comma_string_tables"] == []


def test_a04_comma_string_listed_as_warning(tmp_path, monkeypatch):
    """逗号串读取仍归未指定（不拆分、不截取），同时经 comma_string_tables
    独立告知显式初始化页改用字段列表（doc 7.2 §3.1.1/验收 25）。"""
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {
                        "tables": {
                            "public.legacy": {"embedding_col": "hall_id, screen_name"},
                            "public.ok": {"embedding_col": "name"},
                            "public.multi": {"embedding_col": ["x", "y"]},
                        }
                    }
                }
            }
        ),
    )

    result = read_dim_value_config(SETTINGS, WORKSPACE_ID)

    tables = {t["schema_name"] + "." + t["object_name"]: t for t in result["tables"]}
    assert tables["public.legacy"]["embedding_col"] is None  # 逗号串按未指定
    assert tables["public.ok"]["embedding_col"] == "name"
    assert tables["public.multi"]["embedding_col"] == ["x", "y"]
    assert result["comma_string_tables"] == ["public.legacy"]


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
    from backend.deletion.errors import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError, match="工作区不存在"):
        # doc 8.1 §7.3：只读守卫先于领域错误（404 resource_not_found）
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
    # 含逗号（多列形态）→ 拒绝，提示改用字段列表（doc 7.2 §3.1.1）
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, _payload([("public", "t", "a, b")])
        )
    # 列表元素含逗号同样拒绝，不拆分
    with pytest.raises(DimValueConfigInvalidError):
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, _payload([("public", "t", ["a, b"])])
        )
    # 空列表视为未配置（既有未配置规则）
    with pytest.raises(DimValueColumnMissingError) as excinfo:
        write_dim_value_config(
            SETTINGS, WORKSPACE_ID, _payload([("public", "t", [])])
        )
    assert excinfo.value.details["tables"] == ["public.t"]


def test_a07_multi_column_list_roundtrip(tmp_path, monkeypatch):
    """doc 7.2 §3.1.1/验收 22：PUT 接受合法多字段列表并完整写回、读回。"""
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    result = write_dim_value_config(
        SETTINGS,
        WORKSPACE_ID,
        _payload(
            [
                ("public", "a", ["hall_id", "hall_name", "hall_id"]),
                ("public", "b", "name"),
            ],
            version=_version_of(root),
        ),
    )
    # 列表按首次出现顺序去重；单字段字符串保持字符串形态
    assert result["tables"] == [
        {"schema_name": "public", "object_name": "a", "embedding_col": ["hall_id", "hall_name"]},
        {"schema_name": "public", "object_name": "b", "embedding_col": "name"},
    ]

    written = (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(written)
    tables = parsed["databases"][DB_NAME]["tables"]
    assert tables["public.a"]["embedding_col"] == ["hall_id", "hall_name"]
    assert tables["public.b"]["embedding_col"] == "name"

    # GET 完整读回列表，不归 null
    got = read_dim_value_config(SETTINGS, WORKSPACE_ID)
    got_map = {
        t["schema_name"] + "." + t["object_name"]: t["embedding_col"]
        for t in got["tables"]
    }
    assert got_map == {"public.a": ["hall_id", "hall_name"], "public.b": "name"}


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


# ---- doc 7.1 §4.4：GET 附带只读推荐信息 ----


def _rec_profile(
    root: Path,
    table: str,
    columns="missing",
    confidence="missing",
    column_profiles=None,
):
    json_dir = root / "json"
    table_profile: dict = {"table_category": "dim"}
    if columns != "missing":
        table_profile["dimension_value_columns"] = columns
    if confidence != "missing":
        table_profile["dimension_value_confidence"] = confidence
    payload = {
        "object_info": {
            "database": DB_NAME,
            "schema_name": "public",
            "object_name": table,
            "object_type": "table",
        },
        "column_profiles": column_profiles
        if column_profiles is not None
        else {"name": {}, "id": {}},
        "table_profile": table_profile,
    }
    (json_dir / f"{DB_NAME}.public.{table}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _rows_by_key(result):
    return {t["schema_name"] + "." + t["object_name"]: t for t in result["tables"]}


def test_get_attaches_valid_recommendation(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {
                        "tables": {
                            "public.t1": {"embedding_col": "name"},
                            "public.t2": {"embedding_col": None},
                        }
                    }
                }
            }
        ),
    )
    _rec_profile(root, "t1", ["name"], 0.8)

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    assert rows["public.t1"]["dimension_value_columns"] == ["name"]
    assert rows["public.t1"]["dimension_value_confidence"] == 0.8
    assert rows["public.t1"]["recommendation_available"] is True
    # 画像中不存在的表：推荐信息不可用，但不影响配置打开
    assert rows["public.t2"]["recommendation_available"] is False
    assert rows["public.t2"]["dimension_value_columns"] == []
    assert rows["public.t2"]["dimension_value_confidence"] is None


def test_get_missing_recommendation_attrs_is_no_recommendation(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": "name"}}}
                }
            }
        ),
    )
    _rec_profile(root, "t1")

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    row = rows["public.t1"]
    assert row["recommendation_available"] is True
    assert row["dimension_value_columns"] == []
    assert row["dimension_value_confidence"] is None


@pytest.mark.parametrize(
    "columns,confidence",
    [
        (["name", "id"], 0.9),
        (["nope"], 0.9),
        (["name"], "missing"),
        (["name"], 2.0),
    ],
    ids=["multi", "unknown-col", "missing-confidence", "bad-confidence"],
)
def test_get_invalid_recommendation_treated_as_no_recommendation(
    tmp_path, monkeypatch, columns, confidence
):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": None}}}
                }
            }
        ),
    )
    _rec_profile(root, "t1", columns, confidence)

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    row = rows["public.t1"]
    assert row["recommendation_available"] is True
    assert row["dimension_value_columns"] == []
    assert row["dimension_value_confidence"] is None


def test_get_json_dir_missing_keeps_config_editable(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": "name"}}}
                }
            }
        ),
    )
    (root / "json").rmdir()

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    assert rows["public.t1"]["recommendation_available"] is False


def test_get_unreadable_profile_is_unavailable(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": "name"}}}
                }
            }
        ),
    )
    (root / "json" / f"{DB_NAME}.public.t1.json").write_text("{ not json", "utf-8")

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    assert rows["public.t1"]["recommendation_available"] is False


def test_get_invalid_yaml_still_raises_with_recommendations_present(
    tmp_path, monkeypatch
):
    root = _install(monkeypatch, tmp_path)
    _rec_profile(root, "t1", ["name"], 0.8)
    _write_yaml(root, "not: [valid\n")

    with pytest.raises(DimValueConfigInvalidError):
        read_dim_value_config(SETTINGS, WORKSPACE_ID)


def test_put_ignores_readonly_recommendation_fields(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)
    _write_yaml(root, "databases: {}\n")

    write_dim_value_config(
        SETTINGS,
        WORKSPACE_ID,
        {
            "tables": [
                {
                    "schema_name": "public",
                    "object_name": "t1",
                    "embedding_col": "name",
                    "dimension_value_columns": ["name"],
                    "dimension_value_confidence": 0.8,
                    "recommendation_available": True,
                }
            ],
            "version": _version_of(root),
        },
    )

    written = (root / "dim" / "dim_tables.yaml").read_text(encoding="utf-8")
    assert "dimension_value" not in written
    assert "recommendation_available" not in written
    # Web 保存文件头说明注释生命周期（doc 7.1 §4.4）
    assert "配置弹窗" in written


def _get_with_profile(root: Path, payload: dict):
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": "name"}}}
                }
            }
        ),
    )
    (root / "json" / f"{DB_NAME}.public.t1.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))


def test_get_broken_table_profile_degrades_to_unavailable(tmp_path, monkeypatch):
    """doc 7.1 §4.4：画像 table_profile 结构异常只降级推荐，不阻断配置读取。"""
    root = _install(monkeypatch, tmp_path)

    rows = _get_with_profile(
        root,
        {
            "object_info": {
                "database": DB_NAME,
                "schema_name": "public",
                "object_name": "t1",
                "object_type": "table",
            },
            "column_profiles": {"name": {}},
            "table_profile": ["broken"],
        },
    )

    assert rows["public.t1"]["recommendation_available"] is False


def test_get_broken_column_profiles_degrades_to_unavailable(tmp_path, monkeypatch):
    root = _install(monkeypatch, tmp_path)

    rows = _get_with_profile(
        root,
        {
            "object_info": {
                "database": DB_NAME,
                "schema_name": "public",
                "object_name": "t1",
                "object_type": "table",
            },
            "column_profiles": ["broken"],
            "table_profile": {
                "table_category": "dim",
                "dimension_value_columns": ["name"],
                "dimension_value_confidence": 0.8,
            },
        },
    )

    assert rows["public.t1"]["recommendation_available"] is False


def test_get_json_path_resolution_failure_degrades(tmp_path, monkeypatch):
    """doc 7.1 §4.4：json 路径解析异常（如符号链接）只降级推荐，不阻断 GET。"""
    import backend.profile_loading.dim_value_config_api as api

    root = _install(monkeypatch, tmp_path)
    _write_yaml(
        root,
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {"tables": {"public.t1": {"embedding_col": "name"}}}
                }
            }
        ),
    )
    _rec_profile(root, "t1", ["name"], 0.8)
    monkeypatch.setattr(
        api,
        "resolve_json_output_dir",
        lambda _s, _w: (_ for _ in ()).throw(
            ValueError("步骤产物目录路径包含符号链接")
        ),
    )

    rows = _rows_by_key(read_dim_value_config(SETTINGS, WORKSPACE_ID))

    assert rows["public.t1"]["embedding_col"] == "name"
    assert rows["public.t1"]["recommendation_available"] is False


# ---- doc 7.2 §3.1.1：读取口径（字符串/列表/null 保形） ----


def test_classify_preserves_multi_column_list(tmp_path):
    """人工多字段列表完整保留，不再归 null；去重按首次出现顺序。"""
    path = tmp_path / "dim_tables.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "databases": {
                    DB_NAME: {
                        "tables": {
                            "public.multi": {"embedding_col": ["x", "y", "x"]},
                            "public.single": {"embedding_col": "name"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state, _, cfg = classify_dim_tables_yaml(path, DB_NAME)
    assert state == "tables"
    assert cfg.tables["public.multi"].embedding_col == ["x", "y"]
    assert cfg.tables["public.single"].embedding_col == "name"


@pytest.mark.parametrize(
    "value",
    ["a, b", "a,b", "", "   ", ["a", "b,c"], ["a", ""], ["a", 1], 5, {"x": 1}],
    ids=[
        "comma-spaced",
        "comma",
        "empty-str",
        "blank-str",
        "list-comma-element",
        "list-empty-element",
        "list-non-str-element",
        "int",
        "dict",
    ],
)
def test_classify_illegal_values_become_unset(tmp_path, value):
    """含逗号字符串与含非法元素的列表读取为未指定；不拆分、不截取首项。"""
    path = tmp_path / "dim_tables.yaml"
    path.write_text(
        yaml.safe_dump(
            {"databases": {DB_NAME: {"tables": {"public.t": {"embedding_col": value}}}}}
        ),
        encoding="utf-8",
    )
    state, _, cfg = classify_dim_tables_yaml(path, DB_NAME)
    assert state == "tables"
    assert cfg.tables["public.t"].embedding_col is None


# ---- doc 7.2 §4：独立推荐读取（与被编辑 YAML 解耦） ----


def test_recommendations_endpoint_reads_profiles_without_yaml(tmp_path, monkeypatch):
    """独立推荐读取不依赖 dim YAML：YAML 损坏/缺失时推荐仍按画像返回。"""
    root = _install(monkeypatch, tmp_path)
    (root / "dim" / "dim_tables.yaml").write_text("not: [valid\n", encoding="utf-8")
    _rec_profile(root, "t1", ["name"], 0.8)
    _rec_profile(root, "t2")  # 无推荐属性 → 合法无推荐

    from backend.profile_loading.dim_value_config_api import (
        read_dim_value_recommendations,
    )

    result = read_dim_value_recommendations(SETTINGS, WORKSPACE_ID)
    rows = _rows_by_key(result)
    assert rows["public.t1"]["dimension_value_columns"] == ["name"]
    assert rows["public.t1"]["dimension_value_confidence"] == 0.8
    assert rows["public.t1"]["recommendation_available"] is True
    assert rows["public.t2"]["dimension_value_columns"] == []
    assert rows["public.t2"]["recommendation_available"] is True


def test_recommendations_endpoint_keeps_01_rule(tmp_path, monkeypatch):
    """独立读取与文件 GET 共用 7.1 §5.2 校验器：非法多字段推荐整组清空。"""
    root = _install(monkeypatch, tmp_path)
    _rec_profile(root, "t1", ["name", "id"], 0.9)

    from backend.profile_loading.dim_value_config_api import (
        read_dim_value_recommendations,
    )

    result = read_dim_value_recommendations(SETTINGS, WORKSPACE_ID)
    row = _rows_by_key(result)["public.t1"]
    assert row["dimension_value_columns"] == []
    assert row["dimension_value_confidence"] is None
    assert row["recommendation_available"] is True


def test_recommendations_endpoint_workspace_missing_404(monkeypatch, tmp_path):
    from backend.profile_loading import dim_value_config_api as api

    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "backend.workspaces.repository.get_workspace", lambda _s, _wid: None
    )
    with pytest.raises(DimConfigOutputMissingError):
        api.read_dim_value_recommendations(SETTINGS, WORKSPACE_ID)


def test_write_rejected_while_publish_active(monkeypatch, tmp_path):
    """doc 10.2 §5.2（DeepSeek R1 #4）：活动发布期间 dim 配置保存 409。"""
    from types import SimpleNamespace as _NS

    import backend.profile_loading.dim_value_config_api as dim_api
    from backend.publishing.repository import (
        WorkspacePublishActiveError,
    )
    from backend.persistence.models import WorkspaceInfo, DbSourceInfo

    workspace_row = SimpleNamespace(
        workspace_id="ws_41",
        source_id="db_src_41",
        deletion_state="active",
        deleted_at=None,
    )
    source_row = SimpleNamespace(
        source_id="db_src_41", is_active=True, db_name="d",
        updated_at=None, selected_schemas=["public"],
    )

    blocking = [("run-9", "job-9", "running")]

    class BusySession:
        def get(self, model, key, **_kw):
            if model is WorkspaceInfo and key == "ws_41":
                return workspace_row
            if model is DbSourceInfo:
                return source_row
            return None

        def execute(self, _statement):
            return _NS(
                all=lambda: list(blocking),
                first=lambda: blocking[0],
                scalar_one_or_none=lambda: None,
                scalar_one=lambda: 0,
            )

        def scalars(self, _statement):
            return iter([])

        def commit(self):
            raise AssertionError("拒绝路径不应提交")

    from contextlib import contextmanager

    @contextmanager
    def fake_ps(_settings):
        yield BusySession()

    monkeypatch.setattr(dim_api, "platform_session", fake_ps)
    with pytest.raises(WorkspacePublishActiveError):
        dim_api.write_dim_value_config(
            SETTINGS, "ws_41", {"tables": [], "fingerprint": "x"}
        )
