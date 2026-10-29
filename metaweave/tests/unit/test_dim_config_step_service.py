"""dim 步骤 worker 用例（doc 22.1 §5、doc 6 §2：dim_config 更名 dim）。

断代改造适配：上游就绪判定读 `json/` 顶层唯一成功采集 marker；产物目录
`dim/`；worker 签名 `run_dim_config_step(ctx) -> StepOutcome`。
"""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import register_exception_handlers
from backend.data_sources import dim_config_step_service as service
from backend.data_sources.dim_config_step_service import DimConfigPreparationError
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
JSON_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

JSON_NOT_READY = "工作区的 JSON 产物未就绪；请先执行 JSON 采集"

DEFAULT_CONFIG = {
    "databases": {"demo": {"tables": {"public.dim_a": {"embedding_col": None}}}}
}


def _dim_profile(table: str) -> str:
    return json.dumps(
        {
            "object_info": {
                "database": "demo",
                "schema_name": "public",
                "object_name": table,
            },
            "table_profile": {"table_category": "dim"},
        }
    )


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        display_name="测试源",
        description="源描述",
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
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
    json_tables=("dim_a",),
    with_manifest=False,
    marker="success",
):
    """项目根指向 tmp_path；生成器换记录型替身；临时文件/落盘走真实实现。"""
    from metaweave.utils import file_utils
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    json_dir = workspace / "json"
    dim_dir = workspace / "dim"
    if json_tables or with_manifest or marker:
        json_dir.mkdir(parents=True, exist_ok=True)
        if marker:
            write_collection_marker(json_dir, JSON_RUN_ID, marker)
        for table in json_tables:
            (json_dir / f"demo.public.{table}.json").write_text(
                _dim_profile(table), encoding="utf-8"
            )
        if with_manifest:
            (json_dir / "manifest.json").write_text(
                json.dumps({"files": [f"demo.public.dim_a.json"]}), encoding="utf-8"
            )

    rec = SimpleNamespace(
        json_dir=json_dir,
        dim_dir=dim_dir,
        generator_kwargs=[],
        write_payload=None,
        raw_output=None,
        on_generate=None,
        readable_count=None,
        recommended_count=None,
        unrecommended_count=None,
    )

    class FakeDimConfigGenerator:
        def __init__(self, json_dir=None, output_path=None, json_files=None):
            rec.generator_kwargs.append(
                {
                    "json_dir": json_dir,
                    "output_path": Path(output_path),
                    "json_files": list(json_files or []),
                }
            )
            self.output_path = Path(output_path)
            self._json_files = json_files
            self.last_readable_count = None

        def generate(self):
            if rec.on_generate is not None:
                rec.on_generate(self)
            # 缺省视为全部可读；测试可经 rec.readable_count 覆盖（审核修订 7）
            self.last_readable_count = (
                rec.readable_count
                if rec.readable_count is not None
                else len(self._json_files or [])
            )
            # doc 7.1 §6：有推荐 / 无推荐数量摘要（缺省 0）
            self.last_recommended_count = rec.recommended_count or 0
            self.last_unrecommended_count = rec.unrecommended_count or 0
            payload = (
                rec.write_payload if rec.write_payload is not None else DEFAULT_CONFIG
            )
            text = (
                rec.raw_output
                if rec.raw_output is not None
                else yaml.safe_dump(payload, allow_unicode=True)
            )
            self.output_path.write_text(text, encoding="utf-8")
            return payload

    monkeypatch.setattr(service, "DimTableConfigGenerator", FakeDimConfigGenerator)
    return rec


def _ctx(rec):
    return StepContext(
        settings=SimpleNamespace(),
        source=_source(),
        main_config={},
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="dim",
        step_params=None,
    )


def _leftover_tmp_files(dim_dir: Path):
    if not dim_dir.is_dir():
        return []
    return list(dim_dir.glob(".dim_tables.*.tmp"))


# ---- 错误类型与 API 映射 ----


def test_dim_config_preparation_error_maps_to_dedicated_503():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise DimConfigPreparationError(
            "Dim Config 准备阶段故障（json 目录枚举：PermissionError）"
        )

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 503
    assert response.json()["code"] == "dim_config_preparation_error"
    assert "PermissionError" not in response.text  # 不回显异常原文


# ---- 上游就绪判定 ----


def test_no_json_marker_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_dim_config_step(
            StepContext(
                settings=SimpleNamespace(),
                source=_source(),
                main_config={},
                config_path=Path("cfg.yaml"),
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                step_name="dim",
                step_params=None,
            )
        )
    assert not (tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "dim").exists()


def test_failed_json_marker_rejects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, marker="failed")
    with pytest.raises(DbStepConfigError, match=JSON_NOT_READY):
        service.run_dim_config_step(_ctx(rec))
    assert rec.generator_kwargs == []


def test_json_dir_with_only_manifest_rejected(monkeypatch, tmp_path):
    """manifest.json 是普通文件但不是画像产物：仅清单不算就绪。"""
    rec = _install(monkeypatch, tmp_path, json_tables=(), with_manifest=True)
    with pytest.raises(DbStepConfigError, match=JSON_NOT_READY):
        service.run_dim_config_step(_ctx(rec))
    assert rec.generator_kwargs == []


def test_json_dir_with_only_symlink_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_tables=())
    outside = tmp_path / "outside.json"
    outside.write_text(_dim_profile("dim_a"), encoding="utf-8")
    (rec.json_dir / "demo.public.dim_a.json").symlink_to(outside)
    with pytest.raises(DbStepConfigError, match=JSON_NOT_READY):
        service.run_dim_config_step(_ctx(rec))
    assert rec.generator_kwargs == []


# ---- 成功路径 ----


def test_success_writes_dim_yaml_with_atomic_replace(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch, tmp_path, json_tables=("dim_a", "dim_b"), with_manifest=True
    )
    outcome = service.run_dim_config_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    final = rec.dim_dir / "dim_tables.yaml"
    assert final.is_file() and not final.is_symlink()
    assert stat.S_IMODE(final.stat().st_mode) & 0o644 == 0o644
    assert yaml.safe_load(final.read_text(encoding="utf-8")) == DEFAULT_CONFIG
    # 临时文件不留残骸
    assert _leftover_tmp_files(rec.dim_dir) == []
    # 生成器只读准备链筛出的画像文件（manifest 排除）
    (kwargs,) = rec.generator_kwargs
    assert [Path(p).name for p in kwargs["json_files"]] == [
        "demo.public.dim_a.json",
        "demo.public.dim_b.json",
    ]
    assert kwargs["output_path"].parent == rec.dim_dir
    assert outcome.input_files == [
        ("json/demo.public.dim_a.json", str(JSON_RUN_ID)),
        ("json/demo.public.dim_b.json", str(JSON_RUN_ID)),
    ]
    assert outcome.output_files == ["dim/dim_tables.yaml"]
    assert "1 个数据库、1 个维度表" in outcome.message


def test_success_message_includes_recommendation_summary(monkeypatch, tmp_path):
    """doc 7.1 §6：成功消息附有自动推荐 / 待人工数量摘要。"""
    rec = _install(monkeypatch, tmp_path)
    rec.recommended_count = 1
    rec.unrecommended_count = 1

    outcome = service.run_dim_config_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    assert "1 个自动推荐、1 个待人工填写" in outcome.message


def test_old_dim_product_backed_up_before_update(monkeypatch, tmp_path):
    """旧 dim/ 产物（含 marker）整目录备份后再生成本次配置。"""
    from backend.workspace_files.markers import write_collection_marker

    rec = _install(monkeypatch, tmp_path)
    rec.dim_dir.mkdir(parents=True, exist_ok=True)
    (rec.dim_dir / "dim_tables.yaml").write_text("databases: {}\n", encoding="utf-8")
    write_collection_marker(
        rec.dim_dir, UUID("99999999-9999-4999-8999-999999999999"), "success"
    )

    outcome = service.run_dim_config_step(_ctx(rec))

    assert outcome.success is True
    (backup,) = list((rec.dim_dir / ".backup").iterdir())
    assert (backup / "dim_tables.yaml").exists()
    assert list(backup.glob(".coll_run_id_*"))
    assert list(rec.dim_dir.glob(".coll_run_id_*")) == []  # 旧 marker 已随备份移走


# ---- 生成与核验失败分支 ----


def test_generator_crash_marks_failed_and_cleans_tmp(monkeypatch, tmp_path):
    def on_generate(_gen):
        raise RuntimeError("boom")

    rec = _install(monkeypatch, tmp_path)
    rec.on_generate = on_generate
    outcome = service.run_dim_config_step(_ctx(rec))
    assert outcome.success is False
    assert "Dim 生成失败（RuntimeError）" in outcome.message
    assert "boom" not in outcome.message
    assert _leftover_tmp_files(rec.dim_dir) == []
    assert not (rec.dim_dir / "dim_tables.yaml").exists()


def test_unparsable_yaml_output_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.raw_output = "{ not: valid"
    outcome = service.run_dim_config_step(_ctx(rec))
    assert outcome.success is False
    assert "YAML 无法解析" in outcome.message
    assert _leftover_tmp_files(rec.dim_dir) == []


def test_output_without_databases_mapping_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.write_payload = {"databases": ["not-a-mapping"]}
    outcome = service.run_dim_config_step(_ctx(rec))
    assert outcome.success is False
    assert "databases 不是对象" in outcome.message


def test_zero_readable_profiles_fails(monkeypatch, tmp_path):
    """可读性底线（doc 22.1 §5.3）：画像无一可解析 → 失败。"""
    rec = _install(monkeypatch, tmp_path)
    rec.readable_count = 0
    outcome = service.run_dim_config_step(_ctx(rec))
    assert outcome.success is False
    assert "所有画像 JSON 均无法解析" in outcome.message
    assert _leftover_tmp_files(rec.dim_dir) == []


def test_replace_failure_discards_tmp_and_fails(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    real_replace = os.replace

    def broken_replace(src, dst, *args, **kwargs):
        if str(dst).endswith("dim_tables.yaml"):
            raise OSError("replace denied")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(service.os, "replace", broken_replace)
    outcome = service.run_dim_config_step(_ctx(rec))
    assert outcome.success is False
    assert "Dim 产物落盘失败" in outcome.message
    assert _leftover_tmp_files(rec.dim_dir) == []
