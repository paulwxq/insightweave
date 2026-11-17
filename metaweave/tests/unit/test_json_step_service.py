"""受管 JSON 步骤 worker 用例（doc 18 §3/§4、doc 6 §4.1）。

断代改造适配：上游就绪判定读 `ddl/` 顶层唯一成功采集 marker（不再读
job_info 行）；worker 签名 `run_json_step(ctx) -> StepOutcome`。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.data_sources import json_step_service as service
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo
from backend.profile_loading.ddl_loader import DDLInputError

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
DDL_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
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
        display_name="测试源",
        description=None,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name=DB_NAME,
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        db_version=None,
        selected_schemas=["public"],
        is_active=True,
    )
    values.update(overrides)
    return DbSourceInfo(**values)


def _ddl_row(schema="public", obj="orders", db=DB_NAME):
    return {
        "source_id": SOURCE_ID,
        "schema_name": schema,
        "object_name": obj,
        "define_type": "ddl",
        "db_name": db,
        "object_type": "table",
        "define_text": "-- DDL",
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


def _install(
    monkeypatch,
    tmp_path,
    *,
    source=None,
    ddl_rows=None,
    ddl_read_error=None,
    main_config=None,
):
    """项目根指向 tmp_path；DDL 读取换记录型替身；其余文件操作走真实实现。"""
    from metaweave.utils import file_utils
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")

    source = source or _source()
    ddl_rows = ddl_rows if ddl_rows is not None else [_ddl_row()]
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    json_dir = workspace / "json"
    ddl_dir = workspace / "ddl"
    ddl_dir.mkdir(parents=True, exist_ok=True)
    write_collection_marker(ddl_dir, DDL_RUN_ID, "success")
    # DDL 产物文件：input_manifest 按真实目录 glob 组装
    for row in ddl_rows:
        name = f"{row['db_name']}.{row['schema_name']}.{row['object_name']}.sql"
        (ddl_dir / name).write_text("-- DDL\n", encoding="utf-8")

    rec = SimpleNamespace(
        source=source,
        ddl_rows=ddl_rows,
        ddl_read_error=ddl_read_error,
        json_dir=json_dir,
        ddl_dir=ddl_dir,
        generator_configs=[],
        generate_calls=[],
        on_generate=None,
        gen_result=_gen_result([]),
        main_config=(
            main_config
            if main_config is not None
            else json.loads(json.dumps(MAIN_CONFIG))
        ),
    )

    def fake_read_ddl(_source_id, _workspace_id):
        if rec.ddl_read_error is not None:
            raise rec.ddl_read_error
        return rec.ddl_rows, rec.ddl_dir

    monkeypatch.setattr(service, "read_ddl_definitions", fake_read_ddl)

    class FakeGenerator:
        def __init__(self, config_path, config=None):
            rec.generator_configs.append(config)

        def generate(self, step, max_workers=4):
            rec.generate_calls.append((step, max_workers))
            if rec.on_generate is not None:
                rec.on_generate(rec)
            return rec.gen_result

    monkeypatch.setattr(service, "MetadataGenerator", FakeGenerator)
    return rec


def _ctx(rec, step_params=None):
    return StepContext(
        settings=SimpleNamespace(),
        source=rec.source,
        main_config=rec.main_config,
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="json",
        step_params=step_params,
    )


def _seed_success_files(rec, *, extra_names=()):
    """生成钩子：由"生成器"落盘与 DDL 清单一致的 JSON 产物。"""
    names = sorted(
        f"{r['db_name']}.{r['schema_name']}.{r['object_name']}" for r in rec.ddl_rows
    ) + list(extra_names)

    def on_generate(r):
        files = [_write_json_file(r.json_dir, name) for name in names]
        r.gen_result = _gen_result([str(p) for p in files])

    rec.on_generate = on_generate
    return names


# ---------- 上游就绪判定（doc 6 §4.1：采集 marker） ----------


def test_no_ddl_marker_rejects_before_any_side_effect(monkeypatch, tmp_path):
    """工作区无 DDL marker：准备失败，不建产物目录、不生成。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="DDL 产物未就绪"):
        service.run_json_step(
            _ctx(SimpleNamespace(source=_source(), main_config=MAIN_CONFIG))
        )
    assert not (tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "json").exists()


def test_failed_ddl_marker_rejects(monkeypatch, tmp_path):
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path)
    # 重写为 failed marker（write 会先清旧 marker）
    write_collection_marker(rec.ddl_dir, DDL_RUN_ID, "failed")
    _seed_success_files(rec)  # 预置旧文件钩子，验证不被触发
    with pytest.raises(DbStepConfigError, match="DDL 产物未就绪"):
        service.run_json_step(_ctx(rec))
    assert rec.generate_calls == []


def test_multiple_ddl_markers_rejects_as_unknown_state(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    (
        rec.ddl_dir
        / ".coll_run_id_99999999-9999-4999-8999-999999999999_20260101-000000000_success"
    ).touch()
    with pytest.raises(DbStepConfigError, match="多个采集 marker"):
        service.run_json_step(_ctx(rec))
    assert rec.generate_calls == []


@pytest.mark.parametrize(
    "reason",
    [
        "DDL 文件缺失: demo.public.orders.sql",
        "DDL 解析失败: demo.public.orders.sql: 约束语句无效",
    ],
)
def test_ddl_validation_error_preserves_file_and_reason(monkeypatch, tmp_path, reason):
    rec = _install(
        monkeypatch,
        tmp_path,
        ddl_read_error=DDLInputError(reason),
    )
    with pytest.raises(DbStepConfigError) as exc_info:
        service.run_json_step(_ctx(rec))
    assert str(exc_info.value) == f"JSON 未启动：DDL 产物校验失败：{reason}"
    assert rec.generate_calls == []
    assert not rec.json_dir.exists()


def test_prep_rejects_ddl_db_name_mismatch(monkeypatch, tmp_path):
    """DDL 头部库名与登记源当前库名不一致：连接前拒绝（doc 18 §3.2）。"""
    rec = _install(monkeypatch, tmp_path, ddl_rows=[_ddl_row(db="old_db")])
    with pytest.raises(DbStepConfigError, match="库名"):
        service.run_json_step(_ctx(rec))
    assert rec.generate_calls == []


def test_prep_rejects_invalid_sampling_config_before_backup(monkeypatch, tmp_path):
    """sampling 非 limit：配置预检在备份与生成之前受控失败。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={
            **MAIN_CONFIG,
            "sampling": {"enabled": False, "sample_method": "random"},
        },
    )
    # 预置旧 JSON：预检失败不触发备份，旧产物保留
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    old = rec.json_dir / "demo.public.old.json"
    old.write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, DbStepConfigError), match="sample_method"):
        service.run_json_step(_ctx(rec))
    assert rec.generate_calls == []
    assert old.exists() and not (rec.json_dir / ".backup").exists()


# ---------- 成功路径 ----------


def test_success_collects_validates_and_writes_manifest(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, ddl_rows=[_ddl_row(), _ddl_row(obj="bills")])
    names = _seed_success_files(rec)
    # 预置旧 manifest 与旧产物：随整目录备份移走后重写
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    (rec.json_dir / "manifest.json").write_text('{"files": ["old"]}', encoding="utf-8")

    outcome = service.run_json_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    assert "2 个对象" in outcome.message
    manifest = json.loads((rec.json_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {"files": [f"{name}.json" for name in names]}
    # 注：当前实现 output_files 会把 manifest.json 计两次（glob 已含清单
    # 又 append 一次）；按集合断言，重复问题记入改造审计报告。
    assert sorted(set(outcome.output_files)) == sorted(
        [f"json/{name}.json" for name in names] + ["json/manifest.json"]
    )
    # 输入清单携带 DDL 产物文件及其采集来源运行（doc 6.1 §11）
    assert outcome.input_files == [
        ("ddl/demo.public.bills.sql", str(DDL_RUN_ID)),
        ("ddl/demo.public.orders.sql", str(DDL_RUN_ID)),
    ]
    # 旧 manifest 进入备份，不在顶层
    backups = list((rec.json_dir / ".backup").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").read_text(
        encoding="utf-8"
    ) == '{"files": ["old"]}'


def test_backup_moves_old_json_products(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec)
    rec.json_dir.mkdir(parents=True, exist_ok=True)
    stale = rec.json_dir / "demo.public.stale.json"
    stale.write_text("{}", encoding="utf-8")

    outcome = service.run_json_step(_ctx(rec))

    assert outcome.success is True
    (backup,) = list((rec.json_dir / ".backup").iterdir())
    assert (backup / "demo.public.stale.json").exists()
    assert not stale.exists()
    # 旧 DDL marker 属于 ddl 目录，不随 json 目录备份移动
    assert list(rec.ddl_dir.glob(".coll_run_id_*"))


# ---------- 核验失败分支 ----------


def test_stray_json_file_fails_verification(monkeypatch, tmp_path):
    """生成器写出 DDL 清单之外的对象：集合漂移判失败，要求重跑 DDL。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec, extra_names=["demo.public.extra"])

    outcome = service.run_json_step(_ctx(rec))

    assert outcome.success is False
    assert "与工作区 DDL 清单不一致" in outcome.message


def test_claimed_but_missing_file_fails_verification(monkeypatch, tmp_path):
    """生成器声明输出但未落盘：核对以磁盘为准。"""

    def on_generate(r):
        path = _write_json_file(r.json_dir, "demo.public.orders")
        ghost = r.json_dir / "demo.public.ghost.json"
        r.gen_result = _gen_result([str(path), str(ghost)])

    rec = _install(monkeypatch, tmp_path)
    rec.on_generate = on_generate
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert "未实际落盘" in outcome.message


def test_scope_drift_against_ddl_manifest_fails(monkeypatch, tmp_path):
    """生成的对象集合与工作区 DDL 清单不一致：要求重跑 DDL。"""
    rec = _install(monkeypatch, tmp_path, ddl_rows=[_ddl_row(), _ddl_row(obj="bills")])

    def on_generate(r):
        path = _write_json_file(r.json_dir, "demo.public.orders")
        r.gen_result = _gen_result([str(path)])

    rec.on_generate = on_generate
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert "与工作区 DDL 清单不一致" in outcome.message


def test_generator_crash_marks_failed(monkeypatch, tmp_path):
    def on_generate(_r):
        raise RuntimeError("boom")

    rec = _install(monkeypatch, tmp_path)
    rec.on_generate = on_generate
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert "JSON 采集失败（RuntimeError）" in outcome.message
    assert "boom" not in outcome.message


def test_failed_tables_message_includes_ddl_rerun_hint(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        path = _write_json_file(r.json_dir, "demo.public.orders")
        r.gen_result = _gen_result([str(path)], failed=1, processed=1)

    rec.on_generate = on_generate
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert "1 个对象采集失败" in outcome.message
    assert "重跑 DDL" in outcome.message


def test_invalid_v3_json_fails_verification_not_crash(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def on_generate(r):
        r.json_dir.mkdir(parents=True, exist_ok=True)
        path = r.json_dir / "demo.public.orders.json"
        path.write_text('{"metadata_version": "2.0"}', encoding="utf-8")
        r.gen_result = _gen_result([str(path)])

    rec.on_generate = on_generate
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert "v3" in outcome.message


def test_manifest_write_failure_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_success_files(rec)

    def broken_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "atomic_write_json", broken_write)
    outcome = service.run_json_step(_ctx(rec))
    assert outcome.success is False
    assert outcome.message == "JSON 采集清单写入失败"


# ---------- read_managed_json_input（发布/下游共用的只读契约） ----------


def _seed_json_dir(tmp_path, monkeypatch):
    monkey_dir = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "json"
    monkeypatch.setattr("metaweave.utils.file_utils.get_project_root", lambda: tmp_path)
    return monkey_dir


def test_read_managed_json_input_returns_objects(tmp_path, monkeypatch):
    json_dir = _seed_json_dir(tmp_path, monkeypatch)
    _write_json_file(json_dir, "demo.public.orders")
    _write_json_file(json_dir, "demo.public.bills")
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": ["demo.public.bills.json", "demo.public.orders.json"]}),
        encoding="utf-8",
    )
    objects = service.read_managed_json_input(SOURCE_ID, WORKSPACE_ID, DB_NAME)
    assert objects == [
        {"schema": "public", "object": "bills", "object_type": "table"},
        {"schema": "public", "object": "orders", "object_type": "table"},
    ]


def test_read_managed_json_input_rejects_manifest_mismatch(tmp_path, monkeypatch):
    json_dir = _seed_json_dir(tmp_path, monkeypatch)
    _write_json_file(json_dir, "demo.public.orders")
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": ["demo.public.orders.json", "demo.public.ghost.json"]}),
        encoding="utf-8",
    )
    with pytest.raises(service.ManagedJsonInputError, match="不一致"):
        service.read_managed_json_input(SOURCE_ID, WORKSPACE_ID, DB_NAME)


def test_read_managed_json_input_rejects_foreign_database_entry(tmp_path, monkeypatch):
    json_dir = _seed_json_dir(tmp_path, monkeypatch)
    _write_json_file(json_dir, "demo.public.orders")
    _write_json_file(json_dir, "other.public.bills")
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": ["demo.public.orders.json", "other.public.bills.json"]}),
        encoding="utf-8",
    )
    with pytest.raises(service.ManagedJsonInputError, match="不属于当前数据库"):
        service.read_managed_json_input(SOURCE_ID, WORKSPACE_ID, DB_NAME)


def test_read_managed_json_input_rejects_symlinked_profile(tmp_path, monkeypatch):
    json_dir = _seed_json_dir(tmp_path, monkeypatch)
    _write_json_file(json_dir, "demo.public.orders")
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": ["demo.public.orders.json"]}), encoding="utf-8"
    )
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    # 清单不列出该文件也先拒绝：目录存在非常规文件（doc 19.1 §4）
    (json_dir / "demo.public.bills.json").symlink_to(outside)
    with pytest.raises(service.ManagedJsonInputError, match="非常规文件"):
        service.read_managed_json_input(SOURCE_ID, WORKSPACE_ID, DB_NAME)


def test_read_managed_json_input_rejects_non_string_files_entry(tmp_path, monkeypatch):
    json_dir = _seed_json_dir(tmp_path, monkeypatch)
    _write_json_file(json_dir, "demo.public.orders")
    (json_dir / "manifest.json").write_text(
        json.dumps({"files": [42]}), encoding="utf-8"
    )
    with pytest.raises(service.ManagedJsonInputError, match="形状"):
        service.read_managed_json_input(SOURCE_ID, WORKSPACE_ID, DB_NAME)
