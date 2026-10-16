"""受管 MD 步骤服务用例（doc 19.1 §3/§4/§5）。

覆盖：job_id=None 拒绝、JSON 前置与范围预检在 start/清理前、无凭据
快照、成功路径（清单含 source_json_updated_at）、重渲染逐字节核验的
各失败分支、缓存未命中、终态锁复查与清单写失败落 failed。
"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.data_sources import md_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError
from backend.data_sources.json_step_service import ManagedJsonInputError

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "demo"
JSON_MARKER = datetime(2026, 10, 3, 8, 0, 0, 123456, tzinfo=timezone.utc)

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


def _install(
    monkeypatch,
    tmp_path,
    *,
    objects=None,
    json_status="success",
    json_marker=JSON_MARKER,
    source=None,
):
    """记录型替身；文件操作走真实实现，项目根指向 tmp_path。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    source = source or _source()
    objects = (
        objects
        if objects is not None
        else [{"schema": "public", "object": "orders", "object_type": "table"}]
    )
    md_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "md"
    json_dir = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    json_step = SimpleNamespace(last_status=json_status, updated_at=json_marker)
    rec = SimpleNamespace(
        source=source,
        objects=objects,
        json_step=json_step,
        md_dir=md_dir,
        json_dir=json_dir,
        started=[],
        finished=[],
        locked=[],
        generator_configs=[],
        generate_calls=[],
        decrypted=0,
        on_generate=None,
        gen_result=SimpleNamespace(
            success=True, failed_tables=0, processed_tables=0, output_files=[]
        ),
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
    monkeypatch.setattr(service, "assert_workspace_owner", lambda *a: None)
    monkeypatch.setattr(service, "lock_db_source_row", lambda _s, sid: rec.source)

    def fake_get_step_row(_session, job_id, step_name):
        if step_name != "json":
            return None
        return rec.json_step

    monkeypatch.setattr(service, "get_step_row", fake_get_step_row)
    monkeypatch.setattr(
        service, "read_managed_json_input", lambda sid, jid, db: rec.objects
    )

    def fake_start(_s, job_id, source_id, step):
        rec.started.append((job_id, source_id, step))

    def fake_finish(_s, job_id, step, status):
        rec.finished.append((job_id, step, status))

    monkeypatch.setattr(service, "start_step", fake_start)
    monkeypatch.setattr(service, "finish_step", fake_finish)

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


# ---------- 准备链拒绝 ----------


def test_job_id_none_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), None)
    assert not (tmp_path / "output").exists()


def test_prep_rejects_when_json_not_success(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_status="failed")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_invalid_json_input_as_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def broken(sid, jid, db):
        raise ManagedJsonInputError("JSON 采集清单与目录实际文件不一致")

    monkeypatch.setattr(service, "read_managed_json_input", broken)
    with pytest.raises(DbStepConfigError, match="清单"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_object_out_of_scope(monkeypatch, tmp_path):
    """范围收窄：既有 JSON 对象超出当前 selected_schemas/类型/排除规则，
    在 start 与清理前拒绝（doc 19.1 §4.3）。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        objects=[
            {"schema": "public", "object": "orders", "object_type": "table"},
            {"schema": "sales", "object": "bills", "object_type": "table"},
        ],
    )
    with pytest.raises(DbStepConfigError, match="当前采集范围不一致"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


@pytest.mark.parametrize("invalid", ["abc", None])
def test_prep_rejects_non_list_exclude_tables(monkeypatch, tmp_path, invalid):
    """exclude_tables 非列表（含 YAML 空节点 null）在 start 与清理前
    受控拒绝（doc 19.1 §4）。"""
    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: {
            **MAIN_CONFIG,
            "database": {**MAIN_CONFIG["database"], "exclude_tables": invalid},
        },
    )
    with pytest.raises(DbStepConfigError, match="exclude_tables 必须是列表"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_excluded_object(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        objects=[
            {"schema": "public", "object": "secret_table", "object_type": "table"},
        ],
    )
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: {
            **MAIN_CONFIG,
            "database": {**MAIN_CONFIG["database"], "exclude_tables": ["secret_*"]},
        },
    )
    with pytest.raises(DbStepConfigError, match="当前采集范围不一致"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


def test_prep_rejects_invalid_sampling_node(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: {**MAIN_CONFIG, "sampling": "abc"},
    )
    with pytest.raises(DbStepConfigError, match="sampling"):
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert rec.started == []


# ---------- 成功路径 ----------


@pytest.mark.parametrize(
    ("json_marker", "expected_marker"),
    [
        (JSON_MARKER, "2026-10-03T08:00:00.123456Z"),
        (
            datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc),
            "2026-10-03T08:00:00.000000Z",
        ),
        (
            datetime(
                2026, 10, 3, 16, 0, 0, 123456,
                tzinfo=timezone(timedelta(hours=8)),
            ),
            "2026-10-03T08:00:00.123456Z",
        ),
    ],
)
def test_success_writes_manifest_with_json_marker(
    monkeypatch, tmp_path, json_marker, expected_marker
):
    rec = _install(
        monkeypatch,
        tmp_path,
        json_marker=json_marker,
        objects=[
            {"schema": "public", "object": "orders", "object_type": "table"},
            {"schema": "public", "object": "bills", "object_type": "view"},
        ],
    )
    _seed_md_files(rec)

    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is True, result
    assert rec.started == [(JOB_ID, SOURCE_ID, "md")]
    assert rec.finished == [(JOB_ID, "md", "success")]
    # 快照：无凭据 + md 目录与 resolver 一致
    from metaweave.core.clean_targets import resolve_md_output_dir

    (snapshot,) = rec.generator_configs
    assert snapshot["database"]["host"] is None
    assert snapshot["database"]["password"] is None
    assert snapshot["output"]["markdown_directory"] == str(
        resolve_md_output_dir(SOURCE_ID, JOB_ID)
    )
    # 清单：files + source_json_updated_at（保留精度）
    manifest = json.loads((rec.md_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "files": [f"{DB_NAME}.public.bills.md", f"{DB_NAME}.public.orders.md"],
        "source_json_updated_at": expected_marker,
    }


def test_json_rerun_during_generation_blocks_success(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    def on_generate(r):
        r.on_generate = None  # 防递归
        # 先正常产出
        import tests.unit.test_md_step_service as self_mod

        self_mod._seed_md_files(r)
        r.on_generate(r)
        # 生成完成后 JSON 重跑：标识变化
        r.json_step.updated_at = datetime(2026, 10, 3, 9, 0, 0, tzinfo=timezone.utc)

    rec.on_generate = on_generate
    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "JSON" in result["message"]
    assert rec.finished == [(JOB_ID, "md", "failed")]
    assert not (rec.md_dir / "manifest.json").exists()


# ---------- 核验失败分支 ----------


def test_tampered_md_bytes_fail_verification(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec, content=MD_TEXT + "篡改的额外文本\n")

    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "md", "failed")]
    assert not (rec.md_dir / "manifest.json").exists()


def test_stray_md_file_fails_verification(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    def on_generate(r):
        r.on_generate = None
        import tests.unit.test_md_step_service as self_mod

        self_mod._seed_md_files(r)
        r.on_generate(r)
        stray = r.md_dir / f"{DB_NAME}.public.stray.md"
        stray.write_text(MD_TEXT, encoding="utf-8")
        r.gen_result.output_files.append(str(stray))
        r.gen_result.processed_tables += 1

    rec.on_generate = on_generate
    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "md", "failed")]


def test_manifest_write_failure_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    def failing_atomic(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(service, "atomic_write_json", failing_atomic)
    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "清单写入失败" in result["message"]
    assert rec.finished == [(JOB_ID, "md", "failed")]


def test_generator_crash_marks_failed(monkeypatch, tmp_path):
    from backend.data_sources import md_step_service as svc

    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    class CrashGenerator(svc.MetadataGenerator):
        def generate(self, step, max_workers=4):
            raise RuntimeError("boom")

    monkeypatch.setattr(svc, "MetadataGenerator", CrashGenerator)
    result = svc.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert result["success"] is False
    assert "RuntimeError" in result["message"]
    assert rec.finished == [(JOB_ID, "md", "failed")]


def test_cyclic_symlink_in_md_dir_fails_with_type_message(monkeypatch, tmp_path):
    """md/ 出现循环符号链接：按"非常规文件"受控失败，不以裸 RuntimeError
    收场（doc 19.1 §3：先查类型再 resolve）。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)

    def on_generate(r):
        r.on_generate = None
        import tests.unit.test_md_step_service as self_mod

        self_mod._seed_md_files(r)
        r.on_generate(r)
        # 纯目录杂入的循环链接（不在声明清单）——glob 侧必须先查类型
        loop = r.md_dir / f"{DB_NAME}.public.loop.md"
        loop.symlink_to(loop)

    rec.on_generate = on_generate
    result = service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))

    assert result["success"] is False
    assert "非常规文件" in result["message"]
    assert rec.finished == [(JOB_ID, "md", "failed")]


# --- 预检收敛：已知配置解析异常不冒充平台库故障（doc 19.1 §4.5） ----------


@pytest.mark.parametrize(
    ("label", "database_extra"),
    [
        (
            "sampling.column_statistics: null",
            {"sampling": {"enabled": False, "column_statistics": None}},
        ),
        (
            "sampling.identifier_detection: null",
            {"sampling": {"enabled": False, "identifier_detection": None}},
        ),
        (
            "非法 identifier_patterns 正则",
            {"column_profiling": {"identifier_patterns": ["("]}},
        ),
        (
            "sample_value_count: .inf",
            {"output": {"markdown_options": {"sample_value_count": float("inf")}}},
        ),
    ],
)
def test_prep_rejects_profiling_config_errors_before_clean(
    monkeypatch, tmp_path, label, database_extra
):
    """画像配置的 null 子节点与非法正则在 start 与清理前按配置错误拒绝，
    不落入"平台库连接或读取失败"分支变 503（doc 19.1 §4.5）。"""
    rec = _install(monkeypatch, tmp_path)
    _seed_md_files(rec)
    config = json.loads(json.dumps(MAIN_CONFIG))
    for key, value in database_extra.items():
        if key == "output":
            config["output"] = {**config.get("output", {}), **value}
        else:
            config[key] = value
    monkeypatch.setattr(service, "load_main_config_snapshot", lambda _p: config)

    # 预置旧 MD 与旧清单：准备失败后必须逐字节保持不变（doc 19.1 §4）
    rec.md_dir.mkdir(parents=True, exist_ok=True)
    old_md = rec.md_dir / f"{DB_NAME}.public.orders.md"
    old_md.write_text("旧内容，不应被清理或改写", encoding="utf-8")
    old_manifest = rec.md_dir / "manifest.json"
    old_manifest.write_text('{"files": ["old"]}', encoding="utf-8")

    with pytest.raises(DbStepConfigError) as exc_info:
        service.run_md_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID))
    assert "平台库" not in str(exc_info.value)
    assert rec.started == []
    assert old_md.read_bytes() == "旧内容，不应被清理或改写".encode("utf-8")
    assert old_manifest.read_bytes() == b'{"files": ["old"]}'


def test_write_mode_bytes_matches_save_text(tmp_path):
    """期望编码与实际写盘成对一致（doc 19.1 §5）：``_write_mode_bytes()``
    与 ``save_text()`` 对同一含 LF/CRLF 混合的文本产出相同字节。

    局限（变异验证确认）：在 ``os.linesep == "\n"`` 的平台（Linux），
    文本模式翻译是无操作，本用例对"实现改回纯 encode"不可区分；它真实
    失效的场景是换行相关平台（Windows），以及两机制将来不同步漂移。
    跨平台保障本身来自实现与写盘同用 ``TextIOWrapper(newline=None)``
    机制，属于代码结构性质，Linux 单测无法锁住。monkeypatch
    ``os.linesep`` 同样不生效（翻译在 io 的 C 层，不查该 Python 变量）。
    """
    from metaweave.utils.file_utils import save_text

    text = "a\nb\r\nc\n"  # 样例值可能同时含 LF 与 CRLF
    path = tmp_path / "x.md"
    assert save_text(text, path) is True
    assert path.read_bytes() == service._write_mode_bytes(text)


def test_marker_iso_formats_aware_datetime_as_utc_z():
    """生产路径：marker 是数据库 timestamptz 读回的带时区 datetime（doc 19.3 §3）。

    +08:00 会话值必须换算为同一瞬间的 UTC 六位小数、Z 结尾文本。
    """
    aware = datetime(
        2026, 10, 3, 18, 55, 55, 30584, tzinfo=timezone(timedelta(hours=8))
    )
    assert service._marker_iso(aware) == "2026-10-03T10:55:55.030584Z"


def test_marker_iso_rejects_naive_datetime():
    with pytest.raises(ValueError):
        service._marker_iso(datetime(2026, 10, 3, 18, 55, 55, 30584))
