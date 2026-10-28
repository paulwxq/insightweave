"""DDL 当前态发布（doc 16 §5、6.1 §15）：产物校验链与发布事务。

读取与校验链沿用真实 DDLLoader；发布事务经 create_publish_run +
execute_publish_run 走新发布模型，验证按源整批替换与 publish_run_id。
"""

import json
from uuid import UUID

import pytest

from backend.profile_loading.ddl_loader import (
    DDLInputError,
    check_ddl_output_ready,
    read_ddl_definitions,
)
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from tests.unit.test_publish_service import (
    PUBLISH_JOB_ID,
    RUN,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    make_ready_db,
    run_step_row,
    step_dir,
    write_marker,
)

DB = "appdb"


def table_ddl(database=DB, schema="public", name="users"):
    """普通表产物：注释头（含 Database 行）+ CREATE TABLE。"""
    return (
        "-- ====================================\n"
        f"-- Database: {database}\n"
        f"-- Object Name: {schema}.{name}\n"
        "-- Object Type: table\n"
        "-- Generated: 2026-09-25 08:00:00\n"
        "-- ====================================\n"
        "\n"
        f"CREATE TABLE {schema}.{name} (\n"
        "    id integer NOT NULL,\n"
        "    name character varying(100)\n"
        ");\n"
    )


def object_ddl(object_type, database=DB, schema="public", name="v_orders"):
    """view / materialized_view 产物：CREATE 语句 + OBJECT_METADATA 块。"""
    create_prefix = {
        "view": f"CREATE OR REPLACE VIEW {schema}.{name} AS",
        "materialized_view": f"CREATE MATERIALIZED VIEW IF NOT EXISTS {schema}.{name} AS",
    }[object_type]
    metadata = json.dumps(
        {
            "object_type": object_type,
            "object_name": f"{schema}.{name}",
            "object_comment": "",
            "columns": [
                {"column_name": "id", "data_type": "integer", "column_comment": ""},
            ],
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "-- ====================================\n"
        f"-- Database: {database}\n"
        f"-- Object Name: {schema}.{name}\n"
        f"-- Object Type: {object_type}\n"
        "-- ====================================\n"
        "\n"
        f"{create_prefix}\n"
        "SELECT 1;\n"
        "\n"
        "/* OBJECT_METADATA\n"
        f"{metadata}\n"
        "*/\n"
    )


DEFAULT_FILES = {
    "appdb.public.users.sql": table_ddl(),
    "appdb.public.v_orders.sql": object_ddl("view"),
    "appdb.public.mv_summary.sql": object_ddl("materialized_view", name="mv_summary"),
}


def write_ddl(tmp_path, files, source_id=SOURCE_ID, workspace_id=WORKSPACE_ID):
    """把产物与采集清单（manifest.json）写入受管 DDL 目录，返回目录 Path。"""
    ddl_dir = step_dir(tmp_path, "ddl", source_id=source_id, workspace_id=workspace_id)
    ddl_dir.mkdir(parents=True, exist_ok=True)
    for file_name, content in files.items():
        (ddl_dir / file_name).write_text(content, encoding="utf-8")
    (ddl_dir / "manifest.json").write_text(
        json.dumps({"files": sorted(files)}), encoding="utf-8"
    )
    return ddl_dir


def ddl_ready(tmp_path, files=None):
    write_marker(tmp_path, "ddl")
    return write_ddl(tmp_path, files or DEFAULT_FILES)


# ------------------------------------------------------------------ #
# 读取与校验链（read_ddl_definitions / check_ddl_output_ready）
# ------------------------------------------------------------------ #


@pytest.fixture(autouse=True)
def _project_root(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path)


def test_read_ddl_definitions_builds_rows_for_all_object_types(tmp_path):
    write_ddl(tmp_path, DEFAULT_FILES)
    rows, returned_dir = read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)
    assert returned_dir == step_dir(tmp_path, "ddl")
    by_object = {row["object_name"]: row for row in rows}
    assert set(by_object) == {"users", "v_orders", "mv_summary"}
    # 行只含内容列：无 job_id，source_id 为短 ID（doc 6.1 §15）
    assert by_object["users"] == {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "users",
        "define_type": "ddl",
        "db_name": DB,
        "object_type": "table",
        "define_text": DEFAULT_FILES["appdb.public.users.sql"],
    }
    assert by_object["v_orders"]["object_type"] == "view"
    assert by_object["mv_summary"]["object_type"] == "materialized_view"


def test_read_ddl_definitions_keeps_file_name_case(tmp_path):
    write_ddl(tmp_path, {"appdb.Public.Users.sql": table_ddl()})
    rows, _ = read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)
    assert [(row["schema_name"], row["object_name"]) for row in rows] == [
        ("Public", "Users")
    ]


def test_read_ddl_definitions_accepts_non_ascii_name(tmp_path):
    write_ddl(tmp_path, {"appdb.public.订单表.sql": table_ddl(name="订单表")})
    rows, _ = read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)
    assert [(row["schema_name"], row["object_name"]) for row in rows] == [
        ("public", "订单表")
    ]


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        ("missing-dir", "DDL 目录不存在"),
        ("no-sql", "未找到可发布的 DDL 文件"),
    ],
)
def test_read_ddl_definitions_rejects_unusable_directories(tmp_path, prepare, message):
    if prepare == "no-sql":
        ddl_dir = step_dir(tmp_path, "ddl")
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DDLInputError, match=message):
        read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("symlink", "不是普通文件"),
        ("name-two-parts", "不符合严格三段式"),
        ("name-four-parts", "不符合严格三段式"),
        ("name-with-space", "DDL 解析失败"),
        ("header-missing", "缺少唯一的 Database 头"),
        ("header-duplicate", "缺少唯一的 Database 头"),
        ("header-mismatch", "文件头 Database 与文件名不一致"),
        ("identity-mismatch", "解析身份与文件名不一致"),
        ("mixed-db", "混入多个数据库"),
    ],
)
def test_read_ddl_definitions_rejects_bad_file(tmp_path, kind, message):
    files = {"appdb.public.users.sql": table_ddl()}
    if kind == "name-two-parts":
        files["public.orders.sql"] = table_ddl(name="orders")
    elif kind == "name-four-parts":
        files["appdb.public.orders.extra.sql"] = table_ddl(name="orders")
    elif kind == "name-with-space":
        files["appdb.public.my table.sql"] = table_ddl(name="my table")
    elif kind == "header-missing":
        files["appdb.public.orders.sql"] = table_ddl(name="orders").replace(
            "-- Database: appdb\n", ""
        )
    elif kind == "header-duplicate":
        files["appdb.public.orders.sql"] = table_ddl(name="orders").replace(
            "-- Object Type: table\n", "-- Database: appdb\n"
        )
    elif kind == "header-mismatch":
        files["appdb.public.orders.sql"] = table_ddl(database="otherdb", name="orders")
    elif kind == "identity-mismatch":
        files["appdb.public.orders.sql"] = table_ddl(name="clients")
    elif kind == "mixed-db":
        files["otherdb.public.orders.sql"] = table_ddl(
            database="otherdb", name="orders"
        )
    ddl_dir = write_ddl(tmp_path, files)
    if kind == "symlink":
        staged = tmp_path / "staged.sql"
        staged.write_text(table_ddl(name="orders"), encoding="utf-8")
        (ddl_dir / "appdb.public.orders.sql").symlink_to(staged)
    with pytest.raises(DDLInputError, match=message):
        read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)


def test_read_ddl_definitions_rejects_symlink_before_parsing(tmp_path):
    ddl_dir = write_ddl(tmp_path, {"appdb.public.users.sql": table_ddl()})
    staged = tmp_path / "staged.sql"
    staged.write_text("not a ddl at all", encoding="utf-8")
    (ddl_dir / "appdb.public.orders.sql").symlink_to(staged)
    with pytest.raises(DDLInputError, match="不是普通文件"):
        read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)


def test_read_ddl_definitions_rejects_unparseable_file(tmp_path):
    write_ddl(
        tmp_path, {"appdb.public.orders.sql": "-- Database: appdb\nnot a ddl at all"}
    )
    with pytest.raises(DDLInputError, match="DDL 解析失败: appdb.public.orders.sql"):
        read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("missing-file", "DDL 文件缺失: appdb.public.v_orders.sql"),
        ("extra-file", "采集清单之外的 DDL 文件: appdb.public.extra.sql"),
        ("missing-manifest", "DDL 采集清单缺失"),
        ("broken-manifest", "DDL 采集清单不可读"),
        ("empty-manifest", "DDL 采集清单内容无效"),
    ],
)
def test_read_ddl_definitions_rejects_manifest_mismatch(tmp_path, kind, message):
    files = {"appdb.public.users.sql": table_ddl()}
    if kind == "missing-file":
        write_ddl(tmp_path, {**files, "appdb.public.v_orders.sql": object_ddl("view")})
        (step_dir(tmp_path, "ddl") / "appdb.public.v_orders.sql").unlink()
    elif kind == "extra-file":
        ddl_dir = write_ddl(tmp_path, files)
        (ddl_dir / "appdb.public.extra.sql").write_text(
            table_ddl(name="extra"), encoding="utf-8"
        )
    elif kind == "missing-manifest":
        ddl_dir = write_ddl(tmp_path, files)
        (ddl_dir / "manifest.json").unlink()
    elif kind == "broken-manifest":
        ddl_dir = write_ddl(tmp_path, files)
        (ddl_dir / "manifest.json").write_text("{not json", encoding="utf-8")
    else:
        ddl_dir = write_ddl(tmp_path, files)
        (ddl_dir / "manifest.json").write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
    with pytest.raises(DDLInputError, match=message):
        read_ddl_definitions(SOURCE_ID, WORKSPACE_ID)


def test_manifest_name_is_shared_with_collection_side():
    from backend.data_sources.ddl_step_service import DDL_MANIFEST_NAME as collector
    from backend.profile_loading.ddl_loader import DDL_MANIFEST_NAME as publisher

    assert collector == publisher == "manifest.json"


@pytest.mark.parametrize(
    ("prepare", "ready"),
    [
        ("sql-file", True),
        ("empty-dir", False),
        ("only-text", False),
        ("missing-dir", False),
        ("symlink-workspace", False),
        ("manifest-mismatch", False),
    ],
)
def test_check_ddl_output_ready(tmp_path, prepare, ready):
    if prepare == "sql-file":
        write_ddl(tmp_path, DEFAULT_FILES)
    elif prepare == "manifest-mismatch":
        ddl_dir = write_ddl(tmp_path, DEFAULT_FILES)
        (ddl_dir / "manifest.json").unlink()
    elif prepare == "empty-dir":
        step_dir(tmp_path, "ddl").mkdir(parents=True)
    elif prepare == "only-text":
        ddl_dir = step_dir(tmp_path, "ddl")
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "notes.txt").write_text("x", encoding="utf-8")
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        source_dir = tmp_path / "output" / SOURCE_ID
        source_dir.parent.mkdir(parents=True)
        source_dir.symlink_to(outside, target_is_directory=True)
    assert check_ddl_output_ready(SOURCE_ID, WORKSPACE_ID) is ready


# ------------------------------------------------------------------ #
# 发布事务（新模型：create_publish_run + execute_publish_run）
# ------------------------------------------------------------------ #


def _publish_once(session):
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["ddl"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return session, run_id


def test_publish_ddl_success_writes_definitions(monkeypatch, tmp_path):
    ddl_ready(tmp_path)
    session = install(monkeypatch, tmp_path)
    session, run_id = _publish_once(session)

    step = run_step_row(session, run_id, "ddl")
    assert step.status == "success"
    assert step.message == f"表定义发布完成（{len(DEFAULT_FILES)} 个对象）"
    assert len(session.definitions) == len(DEFAULT_FILES)
    assert {row.publish_run_id for row in session.definitions} == {run_id}
    assert {row.object_name for row in session.definitions} == {
        "users",
        "v_orders",
        "mv_summary",
    }
    assert step.input_manifest == {
        "files": [
            {"path": f"ddl/{name}", "collection_run_id": str(RUN)}
            for name in sorted(DEFAULT_FILES)
        ]
    }
    assert session.runs[0].status == "success"


def test_publish_ddl_twice_replaces_only_last_batch(monkeypatch, tmp_path):
    from backend.persistence.models import TableDefinition

    ddl_ready(tmp_path)
    session = install(monkeypatch, tmp_path)
    _session, first_run = _publish_once(session)
    # 预置一条其他 define_type 的行：不在 ddl 替换范围内
    md_row = TableDefinition(
        source_id=SOURCE_ID,
        schema_name="public",
        object_name="users",
        define_type="md",
        db_name=DB,
        object_type="table",
        define_text="-- markdown",
        publish_run_id=first_run,
    )
    session.definitions.append(md_row)
    # 换一批产物后再次发布
    ddl_dir = step_dir(tmp_path, "ddl")
    for name in DEFAULT_FILES:
        (ddl_dir / name).unlink()
    write_ddl(tmp_path, {"appdb.public.orders.sql": table_ddl(name="orders")})
    session, second_run = _publish_once(session)

    ddl_rows = [row for row in session.definitions if row.define_type == "ddl"]
    assert [(row.schema_name, row.object_name) for row in ddl_rows] == [
        ("public", "orders")
    ]
    assert all(row.publish_run_id == second_run for row in ddl_rows)
    assert md_row in session.definitions  # md 行不受影响
    assert second_run != first_run


def test_publish_ddl_bad_file_fails_step_without_writes(monkeypatch, tmp_path):
    # 清单一致的坏内容：预检（只看文件就绪）通过，执行期解析失败
    files = {
        **DEFAULT_FILES,
        "appdb.public.orders.sql": table_ddl(database="otherdb", name="orders"),
    }
    ddl_ready(tmp_path, files)
    session = install(monkeypatch, tmp_path)
    session, run_id = _publish_once(session)
    step = run_step_row(session, run_id, "ddl")
    assert step.status == "failed"
    assert "DDL 文件头 Database 与文件名不一致" in step.message
    assert session.definitions == []
    assert session.runs[0].status == "failed"


def test_precheck_rejects_manifest_broken_before_run(monkeypatch, tmp_path):
    from backend.publishing.precheck import PublishPrecheckError

    ddl_dir = ddl_ready(tmp_path)
    (ddl_dir / "manifest.json").unlink()
    session = install(monkeypatch, tmp_path)
    with pytest.raises(PublishPrecheckError) as excinfo:
        create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["ddl"],
            step_config=None,
        )
    failures = excinfo.value.failures
    assert len(failures) == 1 and failures[0]["step"] == "ddl"
    assert "清单" in failures[0]["reason"] or "缺失" in failures[0]["reason"]
    assert session.runs == [] and session.definitions == []


def test_db_and_ddl_in_one_run_share_publish_run_id(monkeypatch, tmp_path):
    make_ready_db(tmp_path)
    ddl_ready(tmp_path)
    session = install(monkeypatch, tmp_path)
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="default",
        steps=None,
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    assert session.runs[0].status == "success"
    assert session.profiles[0].publish_run_id == run_id
    assert {row.publish_run_id for row in session.definitions} == {run_id}
