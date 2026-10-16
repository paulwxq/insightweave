"""DDL 当前态发布：产物校验链与发布事务（doc 16 §5）。

平台库沿用 test_publish_service 的假会话（definitions 列表承接
table_definition 行）；DDL 产物按 formatter 的真实格式在临时目录构造，
覆盖文件级校验、整批失败语义、每源一批替换与 db/ddl 互不覆盖。
"""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.persistence.models import TableDefinition
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.profile_loading.ddl_loader import (
    DDLInputError,
    check_ddl_output_ready,
    read_ddl_definitions,
)
from backend.publishing import publish_service
from backend.publishing.publish_service import (
    INPUT_SUMMARY,
    WRITE_SUMMARY,
    PublicationConflictError,
    PublicationStepNotReadyError,
    check_publication,
    publish_steps,
)
from tests.unit.test_publish_service import (
    JOB_ID,
    RUN_MARKER,
    SETTINGS,
    SOURCE_ID,
    assert_safe_primary_key_exception,
    install_session,
    integrity_error,
    write_profile,
)

OTHER_JOB_ID = UUID("660e8400-e29b-41d4-a716-446655440000")


def table_ddl(database="appdb", schema="public", name="users"):
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


def object_ddl(object_type, database="appdb", schema="public", name="v_orders"):
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


def write_ddl(tmp_path, files, source_id=SOURCE_ID, job_id=JOB_ID):
    """把产物与采集清单（manifest.json）写入受管 DDL 目录，返回目录 Path。"""
    ddl_dir = tmp_path / "output" / str(source_id) / str(job_id) / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    for file_name, content in files.items():
        (ddl_dir / file_name).write_text(content, encoding="utf-8")
    (ddl_dir / "manifest.json").write_text(
        json.dumps({"files": sorted(files)}), encoding="utf-8"
    )
    return ddl_dir


def _add_ddl_step(session, *, job_id=JOB_ID, status="success"):
    session.steps.append(
        SimpleNamespace(
            job_id=job_id,
            source_id=SOURCE_ID,
            step_name="ddl",
            last_status=status,
            updated_at=RUN_MARKER,
        )
    )


def ddl_ready_session(
    monkeypatch, tmp_path, *, files=None, job_id=JOB_ID, status="success"
):
    session = install_session(monkeypatch, tmp_path)
    _add_ddl_step(session, job_id=job_id, status=status)
    write_ddl(tmp_path, files or DEFAULT_FILES, job_id=job_id)
    return session


# ------------------------------------------------------------------ #
# 读取与校验链（read_ddl_definitions / check_ddl_output_ready）
# ------------------------------------------------------------------ #


def test_read_ddl_definitions_builds_rows_for_all_object_types(monkeypatch, tmp_path):
    install_session(monkeypatch, tmp_path)
    ddl_dir = write_ddl(tmp_path, DEFAULT_FILES)
    rows, returned_dir = read_ddl_definitions(SOURCE_ID, JOB_ID)
    assert returned_dir == ddl_dir
    by_object = {row["object_name"]: row for row in rows}
    assert set(by_object) == {"users", "v_orders", "mv_summary"}
    assert by_object["users"] == {
        "source_id": SOURCE_ID,
        "schema_name": "public",
        "object_name": "users",
        "define_type": "ddl",
        "db_name": "appdb",
        "object_type": "table",
        "define_text": DEFAULT_FILES["appdb.public.users.sql"],
        "job_id": JOB_ID,
    }
    assert by_object["v_orders"]["object_type"] == "view"
    assert by_object["mv_summary"]["object_type"] == "materialized_view"


def test_read_ddl_definitions_keeps_file_name_case(monkeypatch, tmp_path):
    """保留大小写的文件名通过身份校验后，行内保留文件名原大小写。"""
    install_session(monkeypatch, tmp_path)
    write_ddl(tmp_path, {"appdb.Public.Users.sql": table_ddl()})
    rows, _ = read_ddl_definitions(SOURCE_ID, JOB_ID)
    assert [(row["schema_name"], row["object_name"]) for row in rows] == [
        ("Public", "Users")
    ]


def test_read_ddl_definitions_accepts_non_ascii_name(monkeypatch, tmp_path):
    """非 ASCII、无空白的对象名不触发文件名/身份两类拒绝（doc 16 §4）。

    需要加引号但未触发严格三段式与身份不一致问题的名称，发布不要求失败。
    """
    install_session(monkeypatch, tmp_path)
    write_ddl(tmp_path, {"appdb.public.订单表.sql": table_ddl(name="订单表")})
    rows, _ = read_ddl_definitions(SOURCE_ID, JOB_ID)
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
def test_read_ddl_definitions_rejects_unusable_directories(
    monkeypatch, tmp_path, prepare, message
):
    install_session(monkeypatch, tmp_path)
    if prepare == "no-sql":
        ddl_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DDLInputError, match=message):
        read_ddl_definitions(SOURCE_ID, JOB_ID)


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
def test_read_ddl_definitions_rejects_bad_file(monkeypatch, tmp_path, kind, message):
    """好文件旁混入一个坏文件：整批拒绝并点名具体文件或对象。"""
    install_session(monkeypatch, tmp_path)
    files = {"appdb.public.users.sql": table_ddl()}
    if kind == "name-two-parts":
        files["public.orders.sql"] = table_ddl(name="orders")
    elif kind == "name-four-parts":
        files["appdb.public.orders.extra.sql"] = table_ddl(name="orders")
    elif kind == "name-with-space":
        # 对象名含空白：DDLLoader 的 CREATE 语句解析直接失败（doc 16 §4）。
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
        read_ddl_definitions(SOURCE_ID, JOB_ID)


def test_read_ddl_definitions_rejects_symlink_before_parsing(monkeypatch, tmp_path):
    """符号链接指向坏内容：在读内容/解析之前就被拒绝（doc 16 §5）。

    链接目标故意放无法解析的内容——若 load_all 先读，会报"DDL 解析失败"。
    """
    install_session(monkeypatch, tmp_path)
    ddl_dir = write_ddl(tmp_path, {"appdb.public.users.sql": table_ddl()})
    staged = tmp_path / "staged.sql"
    staged.write_text("not a ddl at all", encoding="utf-8")
    (ddl_dir / "appdb.public.orders.sql").symlink_to(staged)
    with pytest.raises(DDLInputError, match="不是普通文件"):
        read_ddl_definitions(SOURCE_ID, JOB_ID)


def test_read_ddl_definitions_rejects_unparseable_file(monkeypatch, tmp_path):
    """非空但无法解析的内容：逐文件报错并点名具体文件。"""
    install_session(monkeypatch, tmp_path)
    write_ddl(
        tmp_path, {"appdb.public.orders.sql": "-- Database: appdb\nnot a ddl at all"}
    )
    with pytest.raises(DDLInputError, match="DDL 解析失败: appdb.public.orders.sql"):
        read_ddl_definitions(SOURCE_ID, JOB_ID)


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        # 采集成功后缺一份文件：剩余文件不能被当作完整集合发布
        ("missing-file", "DDL 文件缺失: appdb.public.v_orders.sql"),
        # 目录混入清单之外的 .sql：与缺件同样整批拒绝
        ("extra-file", "采集清单之外的 DDL 文件: appdb.public.extra.sql"),
        # 没有采集清单的目录没有"完整集合"凭据
        ("missing-manifest", "DDL 采集清单缺失"),
        ("broken-manifest", "DDL 采集清单不可读"),
        ("empty-manifest", "DDL 采集清单内容无效"),
    ],
)
def test_read_ddl_definitions_rejects_manifest_mismatch(
    monkeypatch, tmp_path, kind, message
):
    """目录现状与采集清单不一致：整批拒绝，无提示缩小集合（doc 16 §5）。"""
    install_session(monkeypatch, tmp_path)
    files = {"appdb.public.users.sql": table_ddl()}
    if kind == "missing-file":
        write_ddl(tmp_path, {**files, "appdb.public.v_orders.sql": object_ddl("view")})
        (
            tmp_path
            / "output"
            / str(SOURCE_ID)
            / str(JOB_ID)
            / "ddl"
            / "appdb.public.v_orders.sql"
        ).unlink()
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
        read_ddl_definitions(SOURCE_ID, JOB_ID)


def test_manifest_name_is_shared_with_collection_side():
    """采集/发布两侧的清单文件名必须一致，否则清单永远对不上。"""
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
def test_check_ddl_output_ready(monkeypatch, tmp_path, prepare, ready):
    install_session(monkeypatch, tmp_path)
    if prepare == "sql-file":
        write_ddl(tmp_path, DEFAULT_FILES)
    elif prepare == "manifest-mismatch":
        # 有 .sql 但清单对不上（缺清单/缺件同样）：预检即不可发布
        ddl_dir = write_ddl(tmp_path, DEFAULT_FILES)
        (ddl_dir / "manifest.json").unlink()
    elif prepare == "empty-dir":
        (tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl").mkdir(parents=True)
    elif prepare == "only-text":
        ddl_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
        ddl_dir.mkdir(parents=True)
        (ddl_dir / "notes.txt").write_text("x", encoding="utf-8")
    elif prepare == "symlink-workspace":
        outside = tmp_path / "outside"
        outside.mkdir()
        source_dir = tmp_path / "output" / str(SOURCE_ID)
        source_dir.parent.mkdir(parents=True)
        source_dir.symlink_to(outside, target_is_directory=True)
    assert check_ddl_output_ready(SOURCE_ID, JOB_ID) is ready


# ------------------------------------------------------------------ #
# 预检（check_publication 的 ddl 步骤）
# ------------------------------------------------------------------ #


def test_check_publication_reports_db_and_ddl(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    _add_ddl_step(session)
    write_ddl(tmp_path, DEFAULT_FILES)
    steps = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"]
    assert [step["step_name"] for step in steps] == [
        "db",
        "ddl",
        "json",
        "domain",
        "rel",
        "table_schema",
        "dim_value",
        "sql",
    ]
    by_name = {step["step_name"]: step for step in steps}
    assert by_name["db"]["publishable"] is True
    assert by_name["ddl"]["publishable"] is True
    # json 未采集（无 json 步骤行）：不可发布（doc 19.2 §7.2）
    assert by_name["json"]["publishable"] is False
    assert by_name["db"]["reason"] is None and by_name["ddl"]["reason"] is None
    assert by_name["json"]["reason"] is not None


@pytest.mark.parametrize(
    ("ddl_status", "files", "reason"),
    [
        (None, True, "没有执行记录"),
        ("failed", True, "最近一次采集失败"),
        ("running", True, "正在执行"),
        ("success", False, "产物不存在或不可用"),
    ],
)
def test_check_ddl_step_reasons(monkeypatch, tmp_path, ddl_status, files, reason):
    session = install_session(monkeypatch, tmp_path)
    if ddl_status is not None:
        _add_ddl_step(session, status=ddl_status)
    if files:
        write_ddl(tmp_path, DEFAULT_FILES)
    step = check_publication(SETTINGS, SOURCE_ID, JOB_ID)["steps"][1]
    assert step["step_name"] == "ddl"
    assert step["publishable"] is False
    assert step["reason"] == reason


# ------------------------------------------------------------------ #
# 发布（publish_steps 选 ddl）
# ------------------------------------------------------------------ #


def test_publish_ddl_success(monkeypatch, tmp_path):
    session = ddl_ready_session(monkeypatch, tmp_path)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert result["results"][0]["step"] == "ddl"
    assert result["results"][0]["status"] == "success"
    assert result["results"][0]["message"] == publish_service.DDL_SUCCESS_MESSAGE
    publish_id = UUID(result["publish_id"])
    assert len(session.definitions) == len(DEFAULT_FILES)
    assert all(
        row.publish_id == publish_id and row.job_id == JOB_ID
        for row in session.definitions
    )
    current = [row for row in session.publishes if row.is_current_publish]
    assert len(current) == 1
    assert current[0].step_name == "ddl"
    assert current[0].publish_id == publish_id
    assert current[0].status == "success"


@pytest.mark.parametrize("status", ["failed", "running"])
def test_publish_ddl_not_ready(monkeypatch, tmp_path, status):
    session = ddl_ready_session(monkeypatch, tmp_path, status=status)
    with pytest.raises(PublicationStepNotReadyError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert session.publishes == [] and session.definitions == []


def test_publish_ddl_without_step_row_is_not_ready(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_ddl(tmp_path, DEFAULT_FILES)
    with pytest.raises(PublicationStepNotReadyError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert session.publishes == [] and session.definitions == []


@pytest.mark.parametrize(
    "kind",
    ["symlink", "header-mismatch", "invalid-utf8", "parse-failure"],
)
def test_publish_ddl_input_failure_keeps_previous_batch(monkeypatch, tmp_path, kind):
    """坏文件整批拒绝：旧一批 define_type='ddl' 行与旧 current 保持不变。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    first = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    first_rows = list(session.definitions)
    ddl_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
    if kind == "symlink":
        staged = tmp_path / "staged.sql"
        staged.write_text(table_ddl(name="orders"), encoding="utf-8")
        (ddl_dir / "appdb.public.orders.sql").symlink_to(staged)
    elif kind == "header-mismatch":
        (ddl_dir / "appdb.public.orders.sql").write_text(
            table_ddl(database="otherdb", name="orders"), encoding="utf-8"
        )
    elif kind == "invalid-utf8":
        (ddl_dir / "appdb.public.orders.sql").write_bytes(b"\xff\xfe\x00")
    else:
        (ddl_dir / "appdb.public.orders.sql").write_text("", encoding="utf-8")

    second = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert second["results"][0]["status"] == "failed"
    assert second["results"][0]["message"] == INPUT_SUMMARY
    assert session.definitions == first_rows
    current = [row for row in session.publishes if row.is_current_publish]
    assert len(current) == 1
    assert current[0].publish_id == UUID(first["publish_id"])
    assert current[0].status == "success"
    failures = [row for row in session.publishes if row.status == "failed"]
    assert len(failures) == 1
    assert failures[0].error_summary == INPUT_SUMMARY
    assert failures[0].is_current_publish is False


def test_publish_ddl_parse_error_keeps_definition_text_out_of_logs(
    monkeypatch, tmp_path, caplog
):
    """畸形列定义（含 DEFAULT 字面量）：异常文本与发布日志都不外泄定义原文。"""
    bad_ddl = table_ddl(name="orders").replace(
        "    name character varying(100)\n",
        # 剥掉 DEFAULT 后只剩列名，触发"无法解析字段定义"；
        # 旧消息会内联整段定义原文（含 SAMPLE_SECRET 字面量）。
        "    password DEFAULT 'SAMPLE_SECRET'\n",
    )
    ddl_ready_session(monkeypatch, tmp_path, files={"appdb.public.orders.sql": bad_ddl})
    with pytest.raises(DDLInputError) as excinfo:
        read_ddl_definitions(SOURCE_ID, JOB_ID)
    # 异常只报类别、序号与文件路径（受控路径可进日志），不含定义原文。
    assert "DDL 解析失败: appdb.public.orders.sql" in str(excinfo.value)
    assert "SAMPLE_SECRET" not in str(excinfo.value)

    with caplog.at_level(logging.ERROR, logger="backend.publishing"):
        result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == INPUT_SUMMARY
    assert "SAMPLE_SECRET" not in caplog.text
    assert "SAMPLE_SECRET" not in result["results"][0]["message"]


def test_publish_ddl_twice_keeps_only_last_batch_and_md_rows(monkeypatch, tmp_path):
    """同源连续两次成功发布：只留最后一批 ddl 行，预先存在的 md 行原样保留。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    first = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    md_row = TableDefinition(
        source_id=SOURCE_ID,
        schema_name="public",
        object_name="users",
        define_type="md",
        db_name="appdb",
        object_type="table",
        define_text="-- markdown 文档",
        job_id=JOB_ID,
        publish_id=UUID(int=7),
    )
    session.definitions.append(md_row)
    ddl_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
    for file_name in DEFAULT_FILES:
        (ddl_dir / file_name).unlink()
    write_ddl(tmp_path, {"appdb.public.orders.sql": table_ddl(name="orders")})

    second = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    second_id = UUID(second["publish_id"])
    assert second_id != UUID(first["publish_id"])
    ddl_rows = [row for row in session.definitions if row.define_type == "ddl"]
    assert [(row.schema_name, row.object_name) for row in ddl_rows] == [
        ("public", "orders")
    ]
    assert all(row.publish_id == second_id and row.job_id == JOB_ID for row in ddl_rows)
    assert md_row in session.definitions
    current = [
        row
        for row in session.publishes
        if row.is_current_publish and row.step_name == "ddl"
    ]
    assert len(current) == 1
    assert current[0].publish_id == second_id


def test_publish_db_and_ddl_keep_separate_currents(monkeypatch, tmp_path):
    """db 与 ddl 各自从不同 job/publish 发布：current 互不覆盖。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    db_result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db"])
    _add_ddl_step(session, job_id=OTHER_JOB_ID)
    write_ddl(tmp_path, DEFAULT_FILES, job_id=OTHER_JOB_ID)
    ddl_result = publish_steps(SETTINGS, SOURCE_ID, OTHER_JOB_ID, ["ddl"])

    db_id = UUID(db_result["publish_id"])
    ddl_id = UUID(ddl_result["publish_id"])
    assert db_id != ddl_id
    current_by_step = {
        row.step_name: row for row in session.publishes if row.is_current_publish
    }
    assert set(current_by_step) == {"db", "ddl"}
    assert current_by_step["db"].publish_id == db_id
    assert current_by_step["db"].job_id == JOB_ID
    assert current_by_step["ddl"].publish_id == ddl_id
    assert current_by_step["ddl"].job_id == OTHER_JOB_ID
    assert session.profiles[0].publish_id == db_id
    assert all(
        row.publish_id == ddl_id and row.job_id == OTHER_JOB_ID
        for row in session.definitions
    )


def test_check_and_publish_switch_log_step_per_step(monkeypatch, tmp_path):
    """预检与发布都按步骤切换日志分流（doc 15）：日志落各自的 step.log。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    _add_ddl_step(session)
    write_ddl(tmp_path, DEFAULT_FILES)
    calls = []
    monkeypatch.setattr(publish_service, "set_current_step", calls.append)
    check_publication(SETTINGS, SOURCE_ID, JOB_ID)
    publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db", "ddl"])
    # 三轮：check_publication 预检、publish_steps 内部预检重验、实际执行；
    # 每轮进入一个步骤前都切换，各步骤日志落各的 step.log。check_publication
    # 固定覆盖 db、ddl、json、table_schema 四步，后两轮只重验/执行所选步骤。
    assert calls == [
        "db",
        "ddl",
        "json",
        "domain",
        "rel",
        "table_schema",
        "dim_value",
        "sql",
        "db",
        "ddl",
        "db",
        "ddl",
    ]


@pytest.mark.parametrize("order", [["db", "ddl"], ["ddl", "db"]])
def test_publish_db_and_ddl_in_one_call(monkeypatch, tmp_path, order):
    """一次发布同时选 db 与 ddl：共用 publish_id，结果按所选顺序返回。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    _add_ddl_step(session)
    write_ddl(tmp_path, DEFAULT_FILES)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, order)
    publish_id = UUID(result["publish_id"])
    assert [row["step"] for row in result["results"]] == order
    assert all(row["status"] == "success" for row in result["results"])
    assert session.profiles[0].publish_id == publish_id
    assert all(row.publish_id == publish_id for row in session.definitions)
    current_steps = {
        row.step_name for row in session.publishes if row.is_current_publish
    }
    assert current_steps == {"db", "ddl"}
    assert all(row.publish_id == publish_id for row in session.publishes)


def test_publish_db_success_then_ddl_rerun_returns_partial_result(
    monkeypatch, tmp_path
):
    """db 已提交后 ddl 才被重跑：部分成功——带 publish_id 返回各步骤结果。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    _add_ddl_step(session)
    write_ddl(tmp_path, DEFAULT_FILES)
    ddl_step = next(step for step in session.steps if step.step_name == "ddl")
    real_commit = publish_service._commit_ddl_current_state

    def rerun_before_ddl_commit(*args):
        # db 步骤此时已提交成功；ddl 提交前重跑恰好完整走完
        # （状态仍 success，但运行标识已变），读到的 DDL 不再代表当前态。
        ddl_step.updated_at = RUN_MARKER + timedelta(seconds=9)
        return real_commit(*args)

    monkeypatch.setattr(
        publish_service, "_commit_ddl_current_state", rerun_before_ddl_commit
    )
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["db", "ddl"])
    publish_id = UUID(result["publish_id"])
    assert [row["status"] for row in result["results"]] == ["success", "failed"]
    assert result["results"][1]["message"] == publish_service.NOT_READY_MESSAGE
    # db 的当前态已提交且可追溯：画像与 current 发布行挂本次 publish_id
    assert session.profiles[0].publish_id == publish_id
    current_steps = {
        row.step_name for row in session.publishes if row.is_current_publish
    }
    assert current_steps == {"db"}
    # ddl 的失败尝试留非 current 历史行，同一 publish_id 可查，不留定义行
    failures = [row for row in session.publishes if row.status == "failed"]
    assert len(failures) == 1
    assert failures[0].step_name == "ddl"
    assert failures[0].publish_id == publish_id
    assert failures[0].is_current_publish is False
    assert failures[0].error_summary == publish_service.NOT_READY_MESSAGE
    assert session.definitions == []


def test_publish_ddl_missing_or_disabled_source_does_not_record(monkeypatch, tmp_path):
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.source = None
    with pytest.raises(MetaLoadSourceNotFoundError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=False)
    with pytest.raises(MetaLoadSourceDisabledError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert session.publishes == [] and session.definitions == []


def test_commit_ddl_rechecks_source_under_lock(monkeypatch, tmp_path):
    """锁下重查发现源已删除或禁用：直接抛错，不写失败记录（doc 16 §5）。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    rows, _ = read_ddl_definitions(SOURCE_ID, JOB_ID)
    session.source = None
    with pytest.raises(MetaLoadSourceNotFoundError):
        publish_service._commit_ddl_current_state(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), rows, RUN_MARKER
        )
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=False)
    with pytest.raises(MetaLoadSourceDisabledError):
        publish_service._commit_ddl_current_state(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), rows, RUN_MARKER
        )
    assert session.publishes == [] and session.definitions == []


def test_publish_ddl_step_rerun_during_execution_is_reported_per_step(
    monkeypatch, tmp_path
):
    """读取 DDL 与提交之间该作业步骤被重跑：按未就绪计入结果并写 failed 历史行。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    real_read = publish_service.read_ddl_definitions
    ddl_step = next(step for step in session.steps if step.step_name == "ddl")

    def racing_read(source_id, job_id):
        result = real_read(source_id, job_id)
        # 读取期间重跑开始：start_step 翻回 running 并清空产物目录。
        ddl_step.last_status = "running"
        return result

    monkeypatch.setattr(publish_service, "read_ddl_definitions", racing_read)
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == publish_service.NOT_READY_MESSAGE
    # 失败尝试留历史行（可凭 publish_id 查到），不留定义行。
    assert len(session.publishes) == 1
    assert session.publishes[0].status == "failed"
    assert session.publishes[0].publish_id == UUID(result["publish_id"])
    assert session.publishes[0].is_current_publish is False
    assert session.definitions == []


def test_commit_ddl_rejects_changed_run_marker(monkeypatch, tmp_path):
    """直调提交：重跑已完成（状态仍 success）但运行标识已变，拒绝写入。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    rows, _ = read_ddl_definitions(SOURCE_ID, JOB_ID)
    with pytest.raises(PublicationStepNotReadyError):
        publish_service._commit_ddl_current_state(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            UUID(int=1),
            rows,
            RUN_MARKER + timedelta(seconds=5),
        )
    assert session.publishes == [] and session.definitions == []


def test_store_failure_rechecks_source_under_lock(monkeypatch, tmp_path):
    """输入/写库失败记录也在源行锁下重查：源已删除或禁用时不新增 failed 行。"""
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.source = None
    with pytest.raises(MetaLoadSourceNotFoundError):
        publish_service._store_failure(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), "ddl", INPUT_SUMMARY
        )
    session.source = SimpleNamespace(source_id=SOURCE_ID, is_active=False)
    with pytest.raises(MetaLoadSourceDisabledError):
        publish_service._store_failure(
            SETTINGS, SOURCE_ID, JOB_ID, UUID(int=1), "ddl", INPUT_SUMMARY
        )
    assert session.publishes == []


def test_publish_ddl_current_conflict_raises(monkeypatch, tmp_path):
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.commit_errors.append(integrity_error("23505", "publish_info_current_idx"))
    with pytest.raises(PublicationConflictError):
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert session.publishes == [] and session.definitions == []


@pytest.mark.parametrize("constraint", ["publish_info_pkey", "table_definition_pkey"])
def test_publish_ddl_primary_key_conflict_propagates(monkeypatch, tmp_path, constraint):
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.commit_errors.append(integrity_error("23505", constraint))
    with pytest.raises(RuntimeError) as caught:
        publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert_safe_primary_key_exception(caught.value)
    assert session.publishes == [] and session.definitions == []


def test_publish_ddl_other_constraint_records_write_failure(monkeypatch, tmp_path):
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.commit_errors.append(
        integrity_error("23514", "table_definition_object_type_ck")
    )
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == WRITE_SUMMARY
    assert session.definitions == []
    assert session.publishes[0].error_summary == WRITE_SUMMARY


def test_publish_ddl_write_failure_hides_driver_text(monkeypatch, tmp_path):
    session = ddl_ready_session(monkeypatch, tmp_path)
    session.commit_errors.append(
        RuntimeError("database password should never be shown")
    )
    result = publish_steps(SETTINGS, SOURCE_ID, JOB_ID, ["ddl"])
    assert result["results"][0]["status"] == "failed"
    assert result["results"][0]["message"] == WRITE_SUMMARY
    assert "password" not in result["results"][0]["message"]
    assert session.definitions == []
