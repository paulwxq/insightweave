"""受管 JSON 步骤服务用例（doc 18 §3/§4）。

覆盖：job_id=None 拒绝、DDL 前置（状态/清单/库名一致）、配置预检在
start_step 与清理之前、成功路径（快照核对/清理边界/核验/清单）、核验失败
分支、DDL 重跑的锁下复查、准备失败不写步骤行。
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.data_sources import json_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "demo"

MAIN_CONFIG = {
    "database": {
        "host": "cfg",
        "port": 5432,
        "database": "cfgdb",
        "user": "u",
        "password": "p",
    },
    "output": {"output_dir": "output", "ddl_directory": "output/ddl"},
    "sampling": {"enabled": False, "sample_method": "limit"},
}


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name=DB_NAME,
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        selected_schemas=["public"],
        is_active=True,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _ddl_row(schema="public", obj="orders", db=DB_NAME):
    return {
        "source_id": SOURCE_ID,
        "schema_name": schema,
        "object_name": obj,
        "define_type": "ddl",
        "db_name": db,
        "object_type": "table",
        "define_text": "-- DDL",
        "job_id": JOB_ID,
    }


def _gen_result(files, *, success=True, failed=0, processed=None):
    return SimpleNamespace(
        success=success,
        failed_tables=failed,
        processed_tables=processed if processed is not None else len(files),
        output_files=list(files),
    )


def _write_json_file(json_dir, name, *, database=DB_NAME, schema="public", obj=None):
    obj = obj or name.split(".")[-1].removesuffix(".json")
    json_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "metadata_version": "3.0",
        "object_info": {
            "database": database,
            "schema_name": schema,
            "object_name": obj,
            "object_type": "table",
            "comment": "",
            "comment_source": "",
            "total_rows": 0,
        },
        "column_profiles": {},
        "table_profile": None,
        "sample_records": {"sample_method": "none", "records": []},
    }
    path = json_dir / f"{name}.json" if not name.endswith(".json") else json_dir / name
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _install(monkeypatch, tmp_path, source=None, ddl_rows=None, ddl_status="success"):
    """安装记录型替身；文件操作（清理/写入/JsonLoader/核验）走真实实现，
    项目根指向 tmp_path。返回记录器。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    source = source or _source()
    ddl_rows = ddl_rows if ddl_rows is not None else [_ddl_row()]
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    ddl_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)

    ddl_step = SimpleNamespace(last_status=ddl_status, updated_at="marker-1")
    rec = SimpleNamespace(
        source=source,
        ddl_rows=ddl_rows,
        ddl_step=ddl_step,
        json_dir=json_dir,
        ddl_dir=ddl_dir,
        started=[],
        finished=[],
        locked=[],
        generator_configs=[],
        generate_calls=[],
        ddl_reads=[],
    )

    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: json.loads(json.dumps(MAIN_CONFIG)),
    )
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())

    @contextmanager
    def fake_session(_settings):
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(service, "get_db_source", lambda _s, _id: rec.source)
    monkeypatch.setattr(service, "decrypt_password", lambda _c: "secret")
    monkeypatch.setattr(service, "assert_workspace_owner", lambda *a: None)
    monkeypatch.setattr(service, "lock_db_source_row", lambda _s, sid: rec.source)

    def fake_get_step_row(_session, job_id, step_name):
        if step_name != "ddl":
            return None
        rec.ddl_reads.append((job_id, step_name))
        return rec.ddl_step

    monkeypatch.setattr(service, "get_step_row", fake_get_step_row)
    monkeypatch.setattr(
        service, "read_ddl_definitions", lambda sid, jid: (rec.ddl_rows, rec.ddl_dir)
    )
    monkeypatch.setattr(
        service,
        "start_step",
        lambda _s, job_id, source_id, step: rec.started.append(
            (job_id, source_id, step)
        ),
    )
    monkeypatch.setattr(
        service,
        "finish_step",
        lambda _s, job_id, step, status: rec.finished.append((job_id, step, status)),
    )

    class FakeGenerator:
        def __init__(self, config_path, config=None):
            rec.generator_configs.append(config)

        def generate(self, step, max_workers=4):
            rec.generate_calls.append((step, max_workers))
            if rec.on_generate is not None:
                rec.on_generate(rec)
            return rec.gen_result

    rec.gen_result = _gen_result([])
    rec.on_generate = None
    rec.FakeGenerator = FakeGenerator
    monkeypatch.setattr(service, "MetadataGenerator", FakeGenerator)
    return rec


def _seed_success_files(rec, *, rerun_ddl_after=False):
    """生成钩子：清理后由"生成器"落盘与 DDL 清单一致的 JSON 产物。

    rerun_ddl_after=True 模拟生成期间 DDL 被重跑（标识变化，doc 18 §4）。
    """
    names = sorted(
        f"{r['db_name']}.{r['schema_name']}.{r['object_name']}" for r in rec.ddl_rows
    )

    def on_generate(r):
        files = [_write_json_file(r.json_dir, name) for name in names]
        r.gen_result = _gen_result([str(p) for p in files])
        if rerun_ddl_after:
            r.ddl_step.updated_at = "marker-2"

    rec.on_generate = on_generate
    return names


# ---------- job_id=None 与准备链拒绝 ----------


def test_job_id_none_rejects_before_any_side_effect(monkeypatch, tmp_path):
    """doc 18 §3.1：新作业 JSON 单选在准备链最前拒绝，不建工作区。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="DDL 产物未就绪"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), None)
    assert not (tmp_path / "output").exists()


def test_prep_failure_raises_config_error_without_step_row(monkeypatch, tmp_path):
    """DDL 状态非 success：准备失败抛 DbStepConfigError，不写步骤行、不清理。"""
    rec = _install(monkeypatch, tmp_path, ddl_status="failed")
    _seed_success_files(rec)  # 预置旧文件，验证不被清理
    old = list(rec.json_dir.glob("*.json"))

    with pytest.raises(DbStepConfigError, match="DDL 产物未就绪"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == [] and rec.finished == []
    assert list(rec.json_dir.glob("*.json")) == old  # 旧 JSON 保持不变


def test_prep_rejects_ddl_db_name_mismatch(monkeypatch, tmp_path):
    """DDL 头部库名与登记源当前库名不一致：连接前拒绝（doc 18 §3.2）。"""
    rec = _install(monkeypatch, tmp_path, ddl_rows=[_ddl_row(db="old_db")])
    with pytest.raises(DbStepConfigError, match="库名"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_invalid_sampling_config_before_start(monkeypatch, tmp_path):
    """sampling 非 limit：配置预检在 start_step 与清理之前受控失败。"""
    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: {
            **MAIN_CONFIG,
            "sampling": {"enabled": False, "sample_method": "random"},
        },
    )
    with pytest.raises(DbStepConfigError, match="sample_method"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_missing_registration_fields(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, source=_source(username=""))
    with pytest.raises(DbStepConfigError, match="必要的连接字段"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


# ---------- 成功路径 ----------


def test_success_collects_validates_and_writes_manifest(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, ddl_rows=[_ddl_row(), _ddl_row(obj="bills")])
    names = _seed_success_files(rec)
    # 预置旧 manifest：应随清理被移除后重写
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    (rec.json_dir / "manifest.json").write_text('{"files": ["old"]}', encoding="utf-8")

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is True, result
    assert result["job_id"] == str(JOB_ID)
    assert rec.started == [(JOB_ID, SOURCE_ID, "json")]
    assert rec.finished == [(JOB_ID, "json", "success")]
    # 快照：连接与全部输出目录指向同一工作区，json 目录与 resolver 一致
    (snapshot,) = rec.generator_configs
    from metaweave.core.clean_targets import resolve_json_output_dir

    assert snapshot["output"]["json_directory"] == str(
        resolve_json_output_dir(SOURCE_ID, JOB_ID)
    )
    assert snapshot["output"]["ddl_directory"] == str(rec.ddl_dir)
    assert snapshot["database"]["database"] == DB_NAME
    # 清单：排序去重、只列画像文件
    manifest = json.loads((rec.json_dir / "manifest.json").read_text(encoding="utf-8"))
    # 与 DDL 清单同形状：完整文件名（含 .json 后缀）、排序去重
    assert manifest == {"files": [n + ".json" for n in names]}
    assert rec.generate_calls == [("json", 4)]


def test_failure_marks_failed_but_keeps_partial_files(monkeypatch, tmp_path):
    """result.success=True 但 failed>0：标 failed、不写有效清单、保留部分文件。"""
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        p = _write_json_file(r.json_dir, f"{DB_NAME}.public.orders")
        r.gen_result = _gen_result([str(p)], failed=1)

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "json", "failed")]
    assert not (rec.json_dir / "manifest.json").exists()
    assert list(rec.json_dir.glob("*.json"))  # 部分文件保留供排障


def test_stray_json_file_fails_verification(monkeypatch, tmp_path):
    """目录混入清单外/非本库 .json：集合核对判失败（doc 18 §4）。"""
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        main = _write_json_file(r.json_dir, f"{DB_NAME}.public.orders")
        stray = _write_json_file(r.json_dir, f"{DB_NAME}.public.stray")
        r.gen_result = _gen_result([str(main), str(stray)])

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "清单" in result["message"] or "集合" in result["message"]
    assert rec.finished == [(JOB_ID, "json", "failed")]
    assert not (rec.json_dir / "manifest.json").exists()


def test_scope_drift_against_ddl_manifest_fails(monkeypatch, tmp_path):
    """两次运行之间范围变化导致 stem 集合与 DDL 清单不同：判失败并提示重跑 DDL。"""
    rec = _install(monkeypatch, tmp_path)  # DDL 只有 orders

    def on_generate(r):
        # 生成器却产出 orders + bills（模拟 include_object_types 变化）
        files = [
            str(_write_json_file(r.json_dir, f"{DB_NAME}.public.orders")),
            str(_write_json_file(r.json_dir, f"{DB_NAME}.public.bills")),
        ]
        r.gen_result = _gen_result(files, processed=2)

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "重跑" in result["message"]
    assert rec.finished == [(JOB_ID, "json", "failed")]


def test_ddl_rerun_during_generation_blocks_success(monkeypatch, tmp_path):
    """生成期间 DDL 重跑（updated_at 变化）：锁下复查不通过，不写清单。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec, rerun_ddl_after=True)

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "DDL" in result["message"]
    assert rec.finished == [(JOB_ID, "json", "failed")]
    assert not (rec.json_dir / "manifest.json").exists()


def test_generator_crash_marks_failed_and_finish_called(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec)

    def crash(self, step, max_workers=4):
        raise RuntimeError("boom")

    rec.FakeGenerator.generate = crash
    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "RuntimeError" in result["message"]
    assert rec.finished == [(JOB_ID, "json", "failed")]


def test_invalid_v3_json_fails_verification_not_crash(monkeypatch, tmp_path):
    """v3/身份校验失败按 failed 处理，不向调用方抛裸异常（doc 18 验收 3）。"""
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        p = r.json_dir / f"{DB_NAME}.public.orders.json"
        r.json_dir.mkdir(parents=True, exist_ok=True)
        p.write_text('{"metadata_version": "2.0"}', encoding="utf-8")  # 坏 v3
        r.gen_result = _gen_result([str(p)])

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "json", "failed")]
    assert not (rec.json_dir / "manifest.json").exists()


def test_manifest_write_failure_marks_failed_not_stuck_running(monkeypatch, tmp_path):
    """doc 18 §4/验收 3：清单写盘失败标 failed——finish_step 必须被调用，
    不能让 job_info 停在 running 楔死工作区。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec)

    def failing_atomic_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "atomic_write_json", failing_atomic_write)

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "清单写入失败" in result["message"]
    assert rec.finished == [(JOB_ID, "json", "failed")]  # 状态落定，可重跑
    assert not (rec.json_dir / "manifest.json").exists()


def test_missing_workspace_reports_config_error_not_platform(monkeypatch, tmp_path):
    """doc 18 验收 2：不存在的 job_id 是受控准备错误（400 口径），
    不是平台库故障（503）。与 DDL 步骤同款处理。"""
    from backend.jobs.repository import WorkspaceMissingError

    rec = _install(monkeypatch, tmp_path)

    def missing(_session, job_id, source_id):
        raise WorkspaceMissingError(f"指定的 job_id 不存在: {job_id}")

    monkeypatch.setattr(service, "assert_workspace_owner", missing)

    with pytest.raises(DbStepConfigError, match="job_id 不存在"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == [] and rec.finished == []


def test_mismatched_workspace_reports_config_error(monkeypatch, tmp_path):
    """错源工作区同样显式转为 DbStepConfigError（锁定，不靠 ValueError 碰巧）。"""
    from backend.jobs.repository import WorkspaceSourceMismatchError

    rec = _install(monkeypatch, tmp_path)

    def mismatch(_session, job_id, source_id):
        raise WorkspaceSourceMismatchError("该作业属于另一个数据源")

    monkeypatch.setattr(service, "assert_workspace_owner", mismatch)

    with pytest.raises(DbStepConfigError, match="另一个数据源"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == [] and rec.finished == []


def test_cyclic_symlink_output_marks_failed_not_stuck_running(monkeypatch, tmp_path):
    """doc 18 验收 3：符号链接输出标 failed——核验异常也不卡 running。

    循环符号链接会让 Path.resolve() 抛 RuntimeError（Python 3.12 实测），
    核验必须先拒绝非常规文件，且任何核验异常都进入失败状态写回。
    """
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        r.json_dir.mkdir(parents=True, exist_ok=True)
        loop = r.json_dir / f"{DB_NAME}.public.loop.json"
        loop.symlink_to(loop)  # 自指循环
        r.gen_result = _gen_result([str(loop)])

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "json", "failed")]  # 状态落定，不楔死
    assert not (rec.json_dir / "manifest.json").exists()


def test_failed_tables_message_includes_ddl_rerun_hint(monkeypatch, tmp_path):
    """doc 18 §4：源库新增对象而 DDL 缺件时失败提示仍指向重跑同作业 DDL
    （failed_tables 早退路径也给出排障指引，不只是 stem 比对路径）。"""
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        p = _write_json_file(r.json_dir, f"{DB_NAME}.public.orders")
        # 模拟新增对象 bills 在旧 DDL 中缺件：逐表失败计入 failed_tables
        r.gen_result = _gen_result([str(p)], failed=1)

    rec.on_generate = on_generate

    result = service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "对象采集失败" in result["message"]
    assert "重跑同一作业 DDL" in result["message"]


def test_ddl_read_oserror_reports_config_error_not_platform(monkeypatch, tmp_path):
    """read_ddl_definitions 的 OSError（并发删除/权限/IO 抖动）按"DDL 产物
    未就绪"受控拒绝，不得落入"平台库连接或读取失败"前缀变 503（doc 18 §3.2）。
    """
    rec = _install(monkeypatch, tmp_path)

    def flaky_read(source_id, job_id):
        raise OSError("file vanished")

    monkeypatch.setattr(service, "read_ddl_definitions", flaky_read)

    with pytest.raises(DbStepConfigError, match="DDL 产物未就绪"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == [] and rec.finished == []


def test_prep_rejects_non_object_database_section(monkeypatch, tmp_path):
    """database: 5（dict() 会 TypeError）按配置错误拒绝，不误报 503
    （doc 19.1 §8 验收 2）。"""
    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service, "load_main_config_snapshot", lambda _p: {**MAIN_CONFIG, "database": 5}
    )
    with pytest.raises(DbStepConfigError, match="database 必须是对象"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


# --- doc 19.1 §4：exclude_tables 共享检查 + 只读 JSON 输入校验 -----------------


@pytest.mark.parametrize("invalid", ["abc", None])
def test_prep_rejects_non_list_exclude_tables_before_start(
    monkeypatch, tmp_path, invalid
):
    """exclude_tables 非列表（含 YAML 空节点 null）在 start_step 与清理前
    受控拒绝（doc 19.1 §4），旧 JSON 产物保持不变。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec)
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    old = _write_json_file(rec.json_dir, f"{DB_NAME}.public.orders")
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: {
            **MAIN_CONFIG,
            "database": {**MAIN_CONFIG["database"], "exclude_tables": invalid},
        },
    )

    with pytest.raises(DbStepConfigError, match="exclude_tables 必须是列表"):
        service.run_json_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []
    assert old.exists()  # 未被清理


def test_read_managed_json_input_returns_objects(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    _write_json_file(json_dir, f"{DB_NAME}.public.orders")
    _write_json_file(json_dir, f"{DB_NAME}.public.bills")
    import json as _json

    (json_dir / "manifest.json").write_text(
        _json.dumps(
            {"files": [f"{DB_NAME}.public.bills.json", f"{DB_NAME}.public.orders.json"]}
        ),
        encoding="utf-8",
    )

    objects = service.read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)
    assert [(o["schema"], o["object"], o["object_type"]) for o in objects] == [
        ("public", "bills", "table"),
        ("public", "orders", "table"),
    ]


def test_read_managed_json_input_rejects_manifest_mismatch(tmp_path, monkeypatch):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    _write_json_file(json_dir, f"{DB_NAME}.public.orders")
    (json_dir / "manifest.json").write_text('{"files": []}', encoding="utf-8")

    with pytest.raises(ValueError, match="清单"):
        service.read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)


def test_read_managed_json_input_rejects_foreign_database_entry(tmp_path, monkeypatch):
    """清单里混入其他库名前缀的文件：按非当前库拒绝，不静默忽略。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    _write_json_file(json_dir, f"{DB_NAME}.public.orders")
    _write_json_file(json_dir, "other_db.public.stray")
    import json as _json

    (json_dir / "manifest.json").write_text(
        _json.dumps(
            {"files": [f"{DB_NAME}.public.orders.json", "other_db.public.stray.json"]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不属于当前数据库"):
        service.read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)


def test_read_managed_json_input_rejects_symlinked_profile(tmp_path, monkeypatch):
    """画像文件被换成符号链接（指向工作区外）：拒绝，不跟随读取越界内容
    （doc 19.1 §4：符号链接/非普通文件均拒绝）。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    json_dir.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = json_dir / f"{DB_NAME}.public.orders.json"
    link.symlink_to(outside)
    import json as _json

    (json_dir / "manifest.json").write_text(
        _json.dumps({"files": [link.name]}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="非常规文件"):
        service.read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)


def test_read_managed_json_input_rejects_non_string_files_entry(tmp_path, monkeypatch):
    """files 含非字符串元素：按清单形状拒绝，不以 set() 的 TypeError 漏成
    平台库故障（doc 19.1 §4 只读输入契约）。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    json_dir.mkdir(parents=True)
    (json_dir / "manifest.json").write_text('{"files": [["x.json"]]}', encoding="utf-8")

    with pytest.raises(ValueError, match="形状"):
        service.read_managed_json_input(SOURCE_ID, JOB_ID, DB_NAME)
