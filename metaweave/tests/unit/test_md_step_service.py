"""受管 MD 步骤 worker 用例（doc 19.1 §3/§4/§5、doc 6 §4.1）。

断代改造适配：上游就绪判定读 `json/` 顶层唯一成功采集 marker；worker
签名 `run_md_step(ctx) -> StepOutcome`；清单记录 JSON 产物来源
`source_collection_run_id`。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.data_sources import md_step_service as service
from backend.data_sources.json_step_service import ManagedJsonInputError
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
JSON_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DB_NAME = "demo"

MAIN_CONFIG = {
    "database": {
        "host": "cfg-host",
        "port": 5432,
        "database": "cfgdb",
        "user": "u",
        "password": "p",
        "include_object_types": ["table", "view", "materialized_view"],
    },
    "output": {"output_dir": "output"},
    "sampling": {"enabled": False, "sample_method": "limit"},
}

MD_TEXT = "# public.orders [table]（订单表）\n\n## 字段列表：\n- order_id (integer) - 订单ID\n"
RENDERED_BY_FAKE = MD_TEXT


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


def _install(
    monkeypatch,
    tmp_path,
    *,
    objects=None,
    source=None,
    main_config=None,
):
    """项目根指向 tmp_path；JSON 读取换替身；文件操作走真实实现。"""
    from metaweave.utils import file_utils
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")

    source = source or _source()
    objects = (
        objects
        if objects is not None
        else [{"schema": "public", "object": "orders", "object_type": "table"}]
    )
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    md_dir = workspace / "md"
    json_dir = workspace / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    write_collection_marker(json_dir, JSON_RUN_ID, "success")
    for obj in objects:
        (json_dir / f"{DB_NAME}.{obj['schema']}.{obj['object']}.json").write_text(
            "{}", encoding="utf-8"
        )

    rec = SimpleNamespace(
        source=source,
        objects=objects,
        md_dir=md_dir,
        json_dir=json_dir,
        generator_configs=[],
        generate_calls=[],
        on_generate=None,
        gen_result=SimpleNamespace(
            success=True, failed_tables=0, processed_tables=0, output_files=[]
        ),
        main_config=(
            main_config
            if main_config is not None
            else json.loads(json.dumps(MAIN_CONFIG))
        ),
    )
    monkeypatch.setattr(
        service,
        "read_managed_json_input",
        lambda _sid, _wid, _db: rec.objects,
    )

    class FakeFormatter:
        def generate_markdown(self, metadata, sample_data=None):
            return RENDERED_BY_FAKE

    class FakeLoader:
        def __init__(self, objects):
            self._objects = {(o["schema"], o["object"]) for o in objects}

        def load_cached_object(self, schema, name):
            if (schema, name) not in self._objects:
                from metaweave.core.metadata.json_loader import JsonLoaderError

                raise JsonLoaderError(f"JSON 文档缓存未命中: {schema}.{name}")
            from metaweave.core.metadata.models import TableMetadata

            return TableMetadata(schema_name=schema, table_name=name, columns=[])

    class FakeGenerator:
        formatter = FakeFormatter()

        def __init__(self, config_path, config=None):
            rec.generator_configs.append(config)
            self.managed_json_loader = FakeLoader(rec.objects)

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
        step_name="md",
        step_params=step_params,
    )


def _seed_md_files(rec, *, content=MD_TEXT, objects=None):
    """生成钩子：由"生成器"写出与期望一致的 MD 文件并声明路径。"""
    objects = objects if objects is not None else rec.objects

    def on_generate(r):
        files = []
        for obj in objects:
            r.md_dir.mkdir(parents=True, exist_ok=True)
            path = r.md_dir / f"{DB_NAME}.{obj['schema']}.{obj['object']}.md"
            path.write_text(content, encoding="utf-8")
            files.append(str(path))
        r.gen_result = SimpleNamespace(
            success=True,
            failed_tables=0,
            processed_tables=len(files),
            output_files=files,
        )

    rec.on_generate = on_generate


# ---------- 上游就绪判定与范围预检（备份之前） ----------


def test_no_json_marker_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_md_step(
            StepContext(
                settings=SimpleNamespace(),
                source=_source(),
                main_config=dict(MAIN_CONFIG),
                config_path=Path("cfg.yaml"),
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                step_name="md",
                step_params=None,
            )
        )
    assert not (tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "md").exists()


def test_failed_json_marker_rejects(monkeypatch, tmp_path):
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path)
    write_collection_marker(rec.json_dir, JSON_RUN_ID, "failed")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_md_step(_ctx(rec))
    assert rec.generate_calls == []


def test_invalid_json_input_wrapped_as_config_error(monkeypatch, tmp_path):
    def boom(*_args):
        raise ManagedJsonInputError("JSON 采集清单与目录实际文件不一致")

    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "read_managed_json_input", boom)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_md_step(_ctx(rec))
    assert rec.generate_calls == []


@pytest.mark.parametrize(
    ("objects", "fragment"),
    [
        # schema 不在登记选中范围
        ([{"schema": "sales", "object": "orders", "object_type": "table"}], "采集范围"),
        # 对象类型不在 include_object_types
        (
            [{"schema": "public", "object": "orders", "object_type": "sequence"}],
            "采集范围",
        ),
    ],
)
def test_prep_rejects_object_out_of_scope(monkeypatch, tmp_path, objects, fragment):
    rec = _install(monkeypatch, tmp_path, objects=objects)
    with pytest.raises(DbStepConfigError, match=fragment):
        service.run_md_step(_ctx(rec))
    assert rec.generate_calls == []


def test_prep_rejects_excluded_object(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    config = json.loads(json.dumps(MAIN_CONFIG))
    config["database"]["exclude_tables"] = ["public.orders"]
    rec.main_config = config
    with pytest.raises(DbStepConfigError, match="采集范围"):
        service.run_md_step(_ctx(rec))
    assert rec.generate_calls == []


def test_prep_rejects_profiling_config_errors_before_backup(monkeypatch, tmp_path):
    """sampling 节非法：生成器构造期解析前移，失败在备份之前。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={**MAIN_CONFIG, "sampling": "not-an-object"},
    )
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    old = rec.md_dir / "demo.public.old.md"
    old.write_text("# old", encoding="utf-8")
    with pytest.raises((ValueError, DbStepConfigError), match="sampling"):
        service.run_md_step(_ctx(rec))
    assert rec.generate_calls == []
    assert old.exists() and not (rec.md_dir / ".backup").exists()


# ---------- 成功路径 ----------


def test_success_writes_manifest_with_json_source_run(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        objects=[
            {"schema": "public", "object": "orders", "object_type": "table"},
            {"schema": "public", "object": "bills", "object_type": "table"},
        ],
    )
    _seed_md_files(rec)
    # 预置旧 md 产物：随整目录备份移走
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    (rec.md_dir / "demo.public.stale.md").write_text("# stale", encoding="utf-8")

    outcome = service.run_md_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    manifest = json.loads((rec.md_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "files": ["demo.public.bills.md", "demo.public.orders.md"],
        "source_collection_run_id": str(JSON_RUN_ID),
    }
    # 输入清单：json 产物文件及其采集来源运行（doc 6.1 §11）
    assert outcome.input_files == [
        ("json/demo.public.bills.json", str(JSON_RUN_ID)),
        ("json/demo.public.orders.json", str(JSON_RUN_ID)),
    ]
    assert sorted(set(outcome.output_files)) == sorted(
        [
            "md/demo.public.bills.md",
            "md/demo.public.orders.md",
            "md/manifest.json",
        ]
    )
    # 旧产物进入备份
    (backup,) = list((rec.md_dir / ".backup").iterdir())
    assert (backup / "demo.public.stale.md").exists()


def test_snapshot_is_credential_free(monkeypatch, tmp_path):
    """MD 使用无凭据快照：不解密源库密码、连接字段显式 None（doc 19.1 §3）。"""
    import backend.security.source_password as crypto

    detonated = []

    def no_decrypt(_cipher):
        detonated.append("decrypt")
        return "must-not-happen"

    monkeypatch.setattr(crypto, "decrypt_password", no_decrypt)
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is True
    assert detonated == []
    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["host"] is None
    assert snapshot["database"]["password"] is None
    assert snapshot["database"]["database"] == DB_NAME


# ---------- 核验失败分支 ----------


def test_tampered_md_bytes_fail_verification(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec, content=MD_TEXT + "\n<!-- 篡改 -->\n")
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is False
    assert "重渲染结果不一致" in outcome.message


def test_stray_md_file_fails_verification(monkeypatch, tmp_path):
    """生成器未声明的 MD 出现在目录：集合不一致判失败。"""

    def on_generate(r):
        _seed_md_files(r)  # 先装标准钩子
        r.on_generate = None
        # 只声明并写出一个文件，目录里再混入一个未声明文件
        r.md_dir.mkdir(parents=True, exist_ok=True)
        declared = r.md_dir / "demo.public.orders.md"
        declared.write_text(MD_TEXT, encoding="utf-8")
        (r.md_dir / "demo.public.stray.md").write_text(MD_TEXT, encoding="utf-8")
        r.gen_result = SimpleNamespace(
            success=True,
            failed_tables=0,
            processed_tables=1,
            output_files=[str(declared)],
        )

    rec = _install(monkeypatch, tmp_path)
    rec.on_generate = on_generate
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is False
    assert "实际输出文件与声明清单不一致" in outcome.message


def test_manifest_write_failure_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    def broken_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "atomic_write_json", broken_write)
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is False
    assert outcome.message == "MD 采集清单写入失败"


def test_generator_crash_marks_failed(monkeypatch, tmp_path):
    def on_generate(_r):
        raise RuntimeError("boom")

    rec = _install(monkeypatch, tmp_path)
    rec.on_generate = on_generate
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is False
    assert "MD 生成失败（RuntimeError）" in outcome.message


def test_cyclic_symlink_in_md_dir_fails_with_type_message(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    original = rec.on_generate
    hook_state = {"ran": False}

    def on_generate(r):
        if not hook_state["ran"]:
            hook_state["ran"] = True
            # 生成完成后放入循环符号链接，核验遍历目录时发现非常规文件
            link = r.md_dir / "demo.public.cycle.md"
            link.symlink_to(link)
        original(r)

    rec.on_generate = on_generate
    outcome = service.run_md_step(_ctx(rec))
    assert outcome.success is False
    assert "非常规文件" in outcome.message


# ---------- 编码细节（doc 19.1 §5） ----------


def test_write_mode_bytes_matches_save_text():
    """期望文本按写盘同源规则编码（universal newline → UTF-8 字节）。"""
    text = "# 表\n\n- id (integer) - 标识\n"
    encoded = service._write_mode_bytes(text)
    assert encoded.decode("utf-8") == text
    assert b"\r\n" not in encoded
