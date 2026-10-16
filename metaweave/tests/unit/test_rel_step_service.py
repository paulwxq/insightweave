"""rel 步骤服务：错误类型、准备链判定、生成核验与连接池归属（doc 23.1 §5）。

覆盖批次 2：入口/平台库/本地三段失败分类、json+domain 双依赖判定与 marker
复查、构造预检（N1/R3/R8）、对象-范围核对（N2/N4）、构造→清理→生成顺序
（R6）、连接池归属（R10）、核验与告警互斥分支（D3/R4/R7）、状态写回失败。
"""

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from backend.api.errors import register_exception_handlers
from backend.data_sources import rel_step_service as service
from backend.data_sources.db_step_service import DbStepConfigError, SourceDisabledError
from backend.data_sources.json_step_service import ManagedJsonInputError
from backend.data_sources.registration_service import PlatformUnavailableError
from backend.data_sources.rel_step_service import RelPreparationError
from backend.jobs.repository import (
    StepAlreadyRunningError,
    WorkspaceMissingError,
    WorkspaceSourceMismatchError,
)

SOURCE_ID = uuid4()
JOB_ID = uuid4()

JSON_NOT_READY = "同一作业的 JSON 产物未就绪；请先执行 JSON 或指定已有 job_id"
DOMAIN_NOT_READY = "同一作业的 Domain 产物未就绪；请先执行 Domain 或指定已有 job_id"
MARKER = datetime(2026, 10, 9, 5, 0, 0, tzinfo=timezone.utc)

MAIN_CONFIG = {
    "database": {"database": "cfgdb"},
    "output": {"output_dir": "output"},
    "relationships": {"llm_candidates": {"enabled": False}},
}


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        db_type="postgresql",
        host="localhost",
        port=5432,
        db_name="demo",
        username="reader",
        password_ciphertext="cipher",
        sslmode=None,
        selected_schemas=["public"],
        is_active=True,
        description="源描述",
        updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeConnector:
    def __init__(self, rec):
        self._rec = rec

    def close(self):
        self._rec.close_calls.append("connector")
        if self._rec.on_close is not None:
            raise self._rec.on_close


class _FakePipeline:
    """记录型管线替身：真实文件写盘走 result 配置；构造/discover 可注入异常。"""

    def __init__(
        self,
        config_path,
        domain_filter=None,
        cross_domain=False,
        domain_resolver=None,
        config=None,
    ):
        rec = config["_rec"]
        rec.pipeline_kwargs.append(
            {
                "domain_filter": domain_filter,
                "cross_domain": cross_domain,
                "domain_resolver": domain_resolver,
                "config": config,
            }
        )
        self._rec = rec
        self.config = config
        self.connector = _FakeConnector(rec)
        if rec.on_init is not None:
            rec.on_init(self)

    def discover(self):
        rec = self._rec
        rec.discover_calls += 1
        if rec.on_discover is not None:
            rec.on_discover(self)
        rel_dir = Path(self.config["output"]["rel_directory"])
        db = self.config["database"]["database"]
        json_path = rel_dir / f"{db}.relationships_global.json"
        md_path = rel_dir / f"{db}.relationships_global.md"
        json_path.write_text(
            json.dumps(
                {
                    "database": (
                        rec.result_database if rec.result_database is not None else db
                    ),
                    "relationships": rec.result_relationships,
                    "statistics": {"total_relationships_found": rec.total},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        md_path.write_text("# rel\n", encoding="utf-8")
        return SimpleNamespace(
            success=rec.result_success,
            total_relations=rec.total,
            foreign_key_relations=rec.fk,
            inferred_relations=rec.inferred,
            output_files=[str(json_path), str(md_path)],
            errors=[] if rec.result_success else ["x"],
        )


def _install(
    monkeypatch,
    tmp_path,
    *,
    config=None,
    json_status="success",
    domain_status="success",
    json_objects=None,
    domain_yaml_tables=("demo.public.t0",),
    source=None,
):
    """记录型替身；json/domain 产物按参数落盘，项目根指向 tmp_path。"""
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    source = source if source is not None else _source()
    workspace = tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID)
    json_dir = workspace / "json"
    domain_dir = workspace / "domain"
    rel_dir = workspace / "rel"
    json_dir.mkdir(parents=True, exist_ok=True)
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "database": {"name": "demo"},
                "domains": [
                    {
                        "name": "业务域",
                        "description": "d",
                        "tables": list(domain_yaml_tables),
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rec = SimpleNamespace(
        source=source,
        json_dir=json_dir,
        domain_dir=domain_dir,
        rel_dir=rel_dir,
        json_status=json_status,
        domain_status=domain_status,
        json_objects=(
            json_objects
            if json_objects is not None
            else [{"schema": "public", "object": "t0", "object_type": "table"}]
        ),
        session1_error=None,
        json_input_error=None,
        json_marker=MARKER,
        domain_marker=MARKER,
        json_step_error=None,
        events=[],
        started=[],
        finished=[],
        locked=[],
        pipeline_kwargs=[],
        discover_calls=0,
        clean_calls=[],
        close_calls=[],
        on_init=None,
        on_discover=None,
        on_close=None,
        result_success=True,
        result_database=None,
        total=2,
        fk=1,
        inferred=1,
        result_relationships=[{"relationship_id": "rel_x"}],
    )

    effective_config = config if config is not None else MAIN_CONFIG
    monkeypatch.setattr(
        service,
        "load_main_config_snapshot",
        lambda _p: json.loads(json.dumps(effective_config)),
    )
    monkeypatch.setattr(service, "load_platform_settings_from_env", lambda: object())

    @contextmanager
    def fake_session(_settings):
        if rec.session1_error is not None:
            raise rec.session1_error
        yield object()

    monkeypatch.setattr(service, "platform_session", fake_session)
    monkeypatch.setattr(service, "get_db_source", lambda _s, _id: rec.source)
    monkeypatch.setattr(service, "assert_workspace_owner", lambda *a: None)
    monkeypatch.setattr(service, "decrypt_password", lambda _c: "plain")

    def fake_lock(_session, source_id):
        rec.locked.append(source_id)
        return rec.source

    monkeypatch.setattr(service, "lock_db_source_row", fake_lock)

    def fake_get_step_row(_session, job_id, step_name):
        if step_name == "json":
            if rec.json_step_error is not None:
                raise rec.json_step_error
            if rec.json_status is None:
                return None
            return SimpleNamespace(
                last_status=rec.json_status, updated_at=rec.json_marker
            )
        if step_name == "domain":
            if rec.domain_status is None:
                return None
            return SimpleNamespace(
                last_status=rec.domain_status, updated_at=rec.domain_marker
            )
        return None

    monkeypatch.setattr(service, "get_step_row", fake_get_step_row)

    def fake_start(_session, job_id, source_id, step, **kwargs):
        rec.events.append(("start", step))
        rec.started.append((job_id, source_id, step, kwargs.get("db_desc", "<未传>")))

    monkeypatch.setattr(service, "start_step", fake_start)

    def fake_finish(_session, job_id, step, status):
        rec.events.append(("finish", status))
        rec.finished.append((job_id, step, status))

    monkeypatch.setattr(service, "finish_step", fake_finish)

    def fake_read_json_input(source_id, job_id, database_name):
        if rec.json_input_error is not None:
            raise rec.json_input_error
        return rec.json_objects

    monkeypatch.setattr(service, "read_managed_json_input", fake_read_json_input)

    def clean_with_record(path):
        rec.events.append(("clean", Path(path)))
        rec.clean_calls.append(Path(path))
        file_utils.clean_top_level_files(path)

    monkeypatch.setattr(service, "clean_top_level_files", clean_with_record)

    def fake_pipeline(*args, **kwargs):
        config = kwargs.get("config")
        if config is not None:
            config = dict(config)
            config["_rec"] = rec
            kwargs["config"] = config
        return _FakePipeline(*args, **kwargs)

    monkeypatch.setattr(service, "RelationshipDiscoveryPipeline", fake_pipeline)
    return rec


def _run(**overrides):
    return service.run_rel_step("cfg.yaml", str(SOURCE_ID), str(JOB_ID), **overrides)


def _config_with(**overrides):
    config = json.loads(json.dumps(MAIN_CONFIG))
    for key, value in overrides.items():
        parts = key.split(".")
        node = config
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return config


# ---- 错误类型与 API 映射 ----


def test_rel_preparation_error_is_platform_unavailable_subclass():
    """编排层复用既有 except PlatformUnavailableError 分支的前提（§5）。"""
    assert issubclass(RelPreparationError, PlatformUnavailableError)


def test_rel_preparation_error_handler_maps_before_parent():
    """子类 handler 按 MRO 先于 platform_unavailable 命中（§9）。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/child")
    def _child():
        raise RelPreparationError("rel 准备阶段故障（json 产物校验：PermissionError）")

    @app.get("/parent")
    def _parent():
        raise PlatformUnavailableError("平台库暂不可用")

    client = TestClient(app, raise_server_exceptions=False)
    child = client.get("/child")
    assert child.status_code == 503
    assert child.json()["code"] == "rel_preparation_error"
    assert child.json()["message"] == "Rel 准备阶段故障，请查看日志"
    assert "PermissionError" not in child.text

    parent = client.get("/parent")
    assert parent.status_code == 503
    assert parent.json()["code"] == "platform_unavailable"


# ---- a) 入口与平台库段的受控拒绝 ----


def test_job_id_none_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_rel_step("cfg.yaml", str(SOURCE_ID), None)
    assert not (tmp_path / "output").exists()


def test_invalid_uuid_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    with pytest.raises(DbStepConfigError):
        service.run_rel_step("cfg.yaml", "not-a-uuid", str(JOB_ID))


def test_main_config_value_error_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(_p):
        raise ValueError("检测到已废弃的顶层配置节点")

    monkeypatch.setattr(service, "load_main_config_snapshot", boom)
    with pytest.raises(DbStepConfigError, match="已废弃"):
        _run()


def test_platform_settings_value_error_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom():
        raise ValueError("缺少平台库环境变量: PLATFORM_DB_HOST")

    monkeypatch.setattr(service, "load_platform_settings_from_env", boom)
    with pytest.raises(DbStepConfigError, match="缺少平台库环境变量"):
        _run()


def test_source_not_found_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.source = None
    with pytest.raises(DbStepConfigError, match="未找到指定 source_id 的数据源"):
        _run()


def test_source_disabled_raises_source_disabled_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, source=_source(is_active=False))
    with pytest.raises(SourceDisabledError):
        _run()


def test_workspace_missing_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise WorkspaceMissingError(f"指定的 job_id 不存在: {JOB_ID}")

    monkeypatch.setattr(service, "assert_workspace_owner", boom)
    with pytest.raises(DbStepConfigError, match="指定的 job_id 不存在"):
        _run()
    assert rec.started == []


def test_workspace_mismatch_maps_to_config_error(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise WorkspaceSourceMismatchError(f"指定的 job_id 不属于该数据源: {JOB_ID}")

    monkeypatch.setattr(service, "assert_workspace_owner", boom)
    with pytest.raises(DbStepConfigError, match="不属于该数据源"):
        _run()


def test_platform_read_error_returns_unregistered_result(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.session1_error = OperationalError("SELECT 1", {}, Exception("down"))

    result = _run()

    assert result["success"] is False
    assert result["message"] == "平台库连接或读取失败（OperationalError）"
    assert "job_id" not in result
    assert rec.started == []


def test_json_row_read_error_returns_platform_result(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.json_step_error = OperationalError("SELECT", {}, Exception("down"))

    result = _run()

    assert result["success"] is False
    assert result["message"].startswith("平台库连接或读取失败（")


# ---- b) 依赖判定与本地段的受控/故障分类 ----


def test_json_row_not_success_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_status="failed")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()
    assert rec.started == [] and rec.pipeline_kwargs == []


def test_json_row_missing_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, json_status=None)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        _run()


def test_domain_row_not_success_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, domain_status="failed")
    with pytest.raises(DbStepConfigError, match="Domain 产物未就绪"):
        _run()
    assert rec.started == [] and rec.pipeline_kwargs == []


def test_domain_row_missing_rejected(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, domain_status=None)
    with pytest.raises(DbStepConfigError, match="Domain 产物未就绪"):
        _run()


def test_json_input_error_wrapped_as_not_ready(monkeypatch, tmp_path):
    """read_managed_json_input 失败：与 md 同款前缀包装（§5.2 第 1 步）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.json_input_error = ManagedJsonInputError("JSON 采集清单与目录实际文件不一致")
    with pytest.raises(DbStepConfigError) as excinfo:
        _run()
    assert "同一作业的 JSON 产物未就绪" in str(excinfo.value)
    assert "清单与目录实际文件不一致" in str(excinfo.value)
    assert rec.started == []


def test_scope_mismatch_outside_schema(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        json_objects=[{"schema": "other", "object": "t0", "object_type": "table"}],
    )
    with pytest.raises(DbStepConfigError) as excinfo:
        _run()
    assert "再运行 Rel" in str(excinfo.value)
    assert rec.started == [] and rec.pipeline_kwargs == []


def test_scope_mismatch_object_type(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        json_objects=[{"schema": "public", "object": "v0", "object_type": "view"}],
    )
    with pytest.raises(DbStepConfigError, match="再运行 Rel"):
        _run()
    assert rec.started == []


def test_scope_mismatch_excluded_table(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"database.exclude_tables": ["public.t0"]}),
    )
    with pytest.raises(DbStepConfigError, match="再运行 Rel"):
        _run()
    assert rec.started == []


def test_domain_artifact_missing_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    (rec.domain_dir / "db_domains.yaml").unlink()
    with pytest.raises(DbStepConfigError, match="Domain"):
        _run()
    assert rec.started == []


def test_domain_artifact_bad_yaml_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    (rec.domain_dir / "db_domains.yaml").write_text(
        "domains: [not: valid", encoding="utf-8"
    )
    with pytest.raises(DbStepConfigError, match="Domain"):
        _run()


def test_domain_artifact_empty_domains_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, domain_yaml_tables=())
    (rec.domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump({"database": {"name": "demo"}, "domains": []}),
        encoding="utf-8",
    )
    with pytest.raises(DbStepConfigError, match="Domain"):
        _run()


def test_get_all_domains_empty_rejected_when_domain_effective(monkeypatch, tmp_path):
    """L2：domains 列表非空但条目无有效 name → get_all_domains 空 → 拒绝。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.domain": "all"}),
    )
    (rec.domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump({"database": {"name": "demo"}, "domains": [{"tables": []}]}),
        encoding="utf-8",
    )
    with pytest.raises(DbStepConfigError, match="业务域"):
        _run()
    assert rec.started == []


def test_snapshot_dir_mismatch_rejected(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "resolve_rel_output_dir",
        lambda _s, _j: tmp_path / "elsewhere",
    )
    with pytest.raises(DbStepConfigError, match="不一致"):
        _run()
    assert rec.started == []


def test_llm_precheck_value_error_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.llm_candidates": {"enabled": True}}),
    )

    class Boom:
        def __init__(self, _config):
            raise ValueError("LLM API Key 未配置: qwen")

    monkeypatch.setattr(service, "LLMService", Boom)
    monkeypatch.setattr(
        service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
    )
    with pytest.raises(DbStepConfigError, match="API Key"):
        _run()
    assert rec.started == []


def test_llm_precheck_skipped_when_disabled(monkeypatch, tmp_path):
    """llm_candidates.enabled=false：不预检 LLM 与 batch_size（A15）。"""
    _install(monkeypatch, tmp_path)

    class Boom:
        def __init__(self, _config):
            raise AssertionError("disabled 时不应构造 LLMService")

    monkeypatch.setattr(service, "LLMService", Boom)
    result = _run()
    assert result["success"] is True


# ---- 构造预检（N1/R3/R8）----


def test_precheck_top_k_invalid(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(
            **{"relationships.llm_candidates": {"enabled": False, "top_k": 0}}
        ),
    )
    with pytest.raises(DbStepConfigError, match="top_k"):
        _run()
    assert rec.started == [] and rec.pipeline_kwargs == []


def test_precheck_null_exempt_node_as_string_rejected(monkeypatch, tmp_path):
    """null 豁免族：sampling 配成字符串 → 400（键存在且非对象非 null）。"""
    rec = _install(monkeypatch, tmp_path, config=_config_with(sampling="fast"))
    with pytest.raises(DbStepConfigError, match="sampling"):
        _run()
    assert rec.started == []


def test_precheck_null_exempt_node_null_allowed(monkeypatch, tmp_path):
    """null 豁免族：sampling: null 视为空对象，放行。"""
    _install(monkeypatch, tmp_path, config=_config_with(sampling=None))
    result = _run()
    assert result["success"] is True


def test_precheck_null_reject_family_top_decision(monkeypatch, tmp_path):
    """null 也拒绝族：顶层 decision: null → 400（运行时 None.get 即炸）。"""
    rec = _install(monkeypatch, tmp_path, config=_config_with(decision=None))
    with pytest.raises(DbStepConfigError, match="decision"):
        _run()
    assert rec.started == []


def test_precheck_null_reject_family_relationships_weights(monkeypatch, tmp_path):
    """null 也拒绝族：relationships.weights 配成 list → 400（现行配置实际位置）。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.weights": [0.4, 0.2]}),
    )
    with pytest.raises(DbStepConfigError, match="weights"):
        _run()
    assert rec.started == []


def test_precheck_top_level_weights_conditional(monkeypatch, tmp_path):
    """R8：relationships.weights 存在时，非法顶层 weights 被 CLI 忽略 → 放行。"""
    config = _config_with(weights="bad")
    config["relationships"]["weights"] = {"inclusion_rate": 0.4}
    _install(monkeypatch, tmp_path, config=config)
    result = _run()
    assert result["success"] is True


def test_precheck_top_level_weights_checked_when_absent_in_relationships(
    monkeypatch, tmp_path
):
    """R8：relationships 无 weights 键时，非法顶层 weights → 400（fallback 路径）。"""
    rec = _install(monkeypatch, tmp_path, config=_config_with(weights=None))
    with pytest.raises(DbStepConfigError, match="weights"):
        _run()
    assert rec.started == []


def test_precheck_name_similarity_non_object_not_config_error(monkeypatch, tmp_path):
    """R3：name_similarity 非对象属降级语义，不判配置错误。"""
    _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.name_similarity": "oops"}),
    )
    result = _run()
    assert result["success"] is True


def test_precheck_langchain_config_non_object(monkeypatch, tmp_path):
    config = _config_with(
        **{
            "relationships.llm_candidates": {"enabled": True},
            "relationships.llm.langchain_config": "fast",
        }
    )
    rec = _install(monkeypatch, tmp_path, config=config)
    monkeypatch.setattr(
        service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
    )

    class FakeLLMService:
        def __init__(self, _config):
            pass

    monkeypatch.setattr(service, "LLMService", FakeLLMService)
    with pytest.raises(DbStepConfigError, match="langchain_config"):
        _run()
    assert rec.started == []


def test_precheck_batch_size_non_numeric_string_rejected(monkeypatch, tmp_path):
    config = _config_with(
        **{
            "relationships.llm_candidates": {"enabled": True},
            "relationships.llm.langchain_config": {"batch_size": "abc"},
        }
    )
    rec = _install(monkeypatch, tmp_path, config=config)
    monkeypatch.setattr(
        service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
    )
    monkeypatch.setattr(service, "LLMService", lambda _c: None)
    with pytest.raises(DbStepConfigError, match="batch_size"):
        _run()
    assert rec.started == []


def test_precheck_batch_size_type_error_maps_to_config_error(monkeypatch, tmp_path):
    """list 的 TypeError 不得落入 RelPreparationError/503（§5.2 d）。"""
    config = _config_with(
        **{
            "relationships.llm_candidates": {"enabled": True},
            "relationships.llm.langchain_config": {"batch_size": ["x"]},
        }
    )
    _install(monkeypatch, tmp_path, config=config)
    monkeypatch.setattr(
        service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
    )
    monkeypatch.setattr(service, "LLMService", lambda _c: None)
    with pytest.raises(DbStepConfigError, match="batch_size"):
        _run()


def test_precheck_batch_size_lenient_values_allowed(monkeypatch, tmp_path):
    """浮点/数字字符串/0/None/负数放行（镜像 CLI max(1, int(x or 50)) 语义）。"""
    for value in (12.5, "30", 0, None, -3, True):
        config = _config_with(
            **{
                "relationships.llm_candidates": {"enabled": True},
                "relationships.llm.langchain_config": {"batch_size": value},
            }
        )
        _install(monkeypatch, tmp_path, config=config)
        monkeypatch.setattr(
            service, "resolve_module_llm_config", lambda _c, _p: {"active": "qwen"}
        )
        monkeypatch.setattr(service, "LLMService", lambda _c: None)
        result = _run()
        assert result["success"] is True, f"batch_size={value!r} 应放行"


def test_local_permission_error_is_preparation_error(monkeypatch, tmp_path, caplog):
    rec = _install(monkeypatch, tmp_path)

    def boom(_sid, _jid, _db):
        raise PermissionError("denied")

    monkeypatch.setattr(service, "read_managed_json_input", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RelPreparationError) as excinfo:
            _run()
    assert "json 产物校验" in str(excinfo.value)
    assert "PermissionError" in str(excinfo.value)
    assert rec.started == []
    assert "denied" not in caplog.text


# ---- c) 成功路径、顺序与连接池归属（R6/R10）----


def test_success_path_construct_clean_discover_order(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / "old.json").write_text("old", encoding="utf-8")

    result = _run()

    assert result["success"] is True
    assert result["job_id"] == str(JOB_ID)
    # 顺序：构造（pipeline_kwargs）→ clean → discover（R6）
    assert rec.events == [
        ("start", "rel"),
        ("clean", rec.rel_dir),
        ("finish", "success"),
    ]
    assert not (rec.rel_dir / "old.json").exists()
    assert (rec.rel_dir / "demo.relationships_global.json").exists()
    assert (rec.rel_dir / "demo.relationships_global.md").exists()
    # 快照键正确
    kwargs = rec.pipeline_kwargs[0]
    assert kwargs["config"]["output"]["json_directory"] == str(rec.json_dir)
    assert kwargs["config"]["output"]["rel_directory"] == str(rec.rel_dir)
    assert kwargs["config"]["database"]["host"] == "localhost"
    assert kwargs["config"]["database"]["password"] == "plain"
    assert kwargs["config"]["database"]["database"] == "demo"
    # domain 合并链：null → 无 resolver（D1）
    assert kwargs["domain_filter"] is None
    assert kwargs["domain_resolver"] is None
    assert kwargs["cross_domain"] is False
    # discover 开始后关闭只归 discover 的替身语义：服务侧不再调用（close 恰零次，
    # 由 FakeConnector 记录；成功路径的关闭责任在 discover() finally——替身不模拟）
    assert rec.close_calls == []


def test_success_path_domain_all_uses_managed_resolver(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(
            **{
                "relationships.domain": "all",
                "relationships.cross_domain": True,
            }
        ),
    )
    result = _run()
    assert result["success"] is True
    kwargs = rec.pipeline_kwargs[0]
    assert kwargs["domain_filter"] == "all"
    assert kwargs["cross_domain"] is True
    resolver = kwargs["domain_resolver"]
    assert resolver is not None
    assert resolver.get_all_domains() == ["业务域"]


def test_construction_failure_keeps_old_artifacts(monkeypatch, tmp_path):
    """R6：构造期异常（如建池失败）→ 目录未清理、旧产物保留、行 failed。"""
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / "demo.relationships_global.json").write_text(
        "旧产物", encoding="utf-8"
    )

    def on_init(_self):
        raise RuntimeError("pool failed")

    rec.on_init = on_init
    result = _run()

    assert result["success"] is False
    assert result["message"] == "rel 采集失败（RuntimeError）"
    assert rec.clean_calls == []
    assert (rec.rel_dir / "demo.relationships_global.json").read_text(
        encoding="utf-8"
    ) == "旧产物"
    assert rec.finished == [(JOB_ID, "rel", "failed")]


def test_clean_failure_closes_connector_and_marks_failed(monkeypatch, tmp_path):
    """R10：构造成功后 clean 抛错 → 显式 connector.close()、行 failed。"""

    def boom(_path):
        raise OSError("denied")

    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "clean_top_level_files", boom)
    result = _run()

    assert result["success"] is False
    assert result["message"] == "rel 采集失败（OSError）"
    assert rec.close_calls == ["connector"]
    assert rec.finished == [(JOB_ID, "rel", "failed")]
    assert rec.discover_calls == 0


def test_discover_started_no_extra_close(monkeypatch, tmp_path):
    """R10：discover() 一旦开始，服务侧不再调 close（全程恰零次显式关闭）。"""
    rec = _install(monkeypatch, tmp_path)

    def on_discover(_self):
        raise RuntimeError("sampling down")

    rec.on_discover = on_discover
    result = _run()

    assert result["success"] is False
    assert rec.close_calls == []
    assert rec.finished == [(JOB_ID, "rel", "failed")]


def test_step_already_running_returns_failure_with_job_id(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise StepAlreadyRunningError("当前步骤可能已有作业正在执行")

    monkeypatch.setattr(service, "start_step", boom)
    result = _run()

    assert result["success"] is False
    assert result["job_id"] == str(JOB_ID)
    assert result["message"] == "当前步骤可能已有作业正在执行"
    assert rec.pipeline_kwargs == []


# ---- d) 核验与告警互斥分支（D3/R4/R7）----


def test_verify_failure_marks_failed(monkeypatch, tmp_path):
    """result.success=False → failed；旧产物已随 clean 删除、不恢复。"""
    rec = _install(monkeypatch, tmp_path)
    rec.result_success = False
    result = _run()
    assert result["success"] is False
    assert rec.finished == [(JOB_ID, "rel", "failed")]


def test_verify_json_database_mismatch_marks_failed(monkeypatch, tmp_path):
    """产物 json 的 database ≠ 登记库名 → failed（核验第 4 条）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.result_database = "mismatched"
    result = _run()
    assert result["success"] is False
    assert "库名" in result["message"]
    assert rec.finished == [(JOB_ID, "rel", "failed")]


def test_zero_relations_domain_null_success_with_warning(monkeypatch, tmp_path, caplog):
    """分支 i：domain 未生效 + 零关系 → success + 零关系告警段。"""
    rec = _install(monkeypatch, tmp_path)
    rec.total = 0
    rec.fk = 0
    rec.inferred = 0
    rec.result_relationships = []
    with caplog.at_level(logging.WARNING):
        result = _run()
    assert result["success"] is True
    assert "0 条关系" in result["message"]
    assert "外键与推断均为空" in result["message"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_zero_inferred_domain_effective_success_with_warning(
    monkeypatch, tmp_path, caplog
):
    """分支 ii：domain 生效 + 推断为 0（total > 0）→ success + 零推断告警段。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.domain": "all"}),
    )
    rec.total = 3
    rec.fk = 3
    rec.inferred = 0
    with caplog.at_level(logging.WARNING):
        result = _run()
    assert result["success"] is True
    assert "推断 0" in result["message"]
    assert "db_domains.yaml" in result["message"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_warning_intersection_uses_zero_inferred_branch(monkeypatch, tmp_path, caplog):
    """R7 交集：domain 生效 + total == 0 + inferred == 0 → 零推断形态，不用零关系文案。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        config=_config_with(**{"relationships.domain": "all"}),
    )
    rec.total = 0
    rec.fk = 0
    rec.inferred = 0
    rec.result_relationships = []
    with caplog.at_level(logging.WARNING):
        result = _run()
    assert result["success"] is True
    assert "推断 0" in result["message"]
    assert "外键与推断均为空" not in result["message"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_normal_result_no_warning(monkeypatch, tmp_path, caplog):
    _install(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING):
        result = _run()
    assert result["success"] is True
    assert "2 条关系" in result["message"]
    assert "外键 1 / 推断 1" in result["message"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == []


# ---- e) marker 复查（R6）----


def test_session3_json_marker_changed_marks_failed(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_get_step_row(_session, job_id, step_name):
        if step_name == "json":
            calls["n"] += 1
            marker = (
                MARKER
                if calls["n"] <= 2
                else datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
            )
            return SimpleNamespace(last_status="success", updated_at=marker)
        if step_name == "domain":
            return SimpleNamespace(last_status="success", updated_at=MARKER)
        return None

    monkeypatch.setattr(service, "get_step_row", flaky_get_step_row)
    result = _run()

    assert result["success"] is False
    assert "JSON" in result["message"]
    assert "已重跑" in result["message"]
    assert rec.finished == [(JOB_ID, "rel", "failed")]


def test_session3_both_markers_changed_names_joined(monkeypatch, tmp_path):
    """R6：两行同时变化 → 消息中「JSON 与 Domain」两名并列。"""
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_get_step_row(_session, job_id, step_name):
        if step_name in ("json", "domain"):
            calls["n"] += 1
            marker = (
                MARKER
                if calls["n"] <= 4
                else datetime(2026, 10, 9, 6, 0, 0, tzinfo=timezone.utc)
            )
            return SimpleNamespace(last_status="success", updated_at=marker)
        return None

    monkeypatch.setattr(service, "get_step_row", flaky_get_step_row)
    result = _run()

    assert result["success"] is False
    assert "JSON 与 Domain" in result["message"]
    assert rec.finished == [(JOB_ID, "rel", "failed")]


# ---- f) 状态写回失败 ----


def test_finish_step_failure_reported(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "finish_step", boom)
    result = _run()

    assert result["success"] is False
    assert "作业状态更新失败" in result["message"]
    assert str(JOB_ID) in result["message"]


def test_finish_lock_missing_reports_registration_gone(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    calls = {"n": 0}

    def flaky_lock(_session, source_id):
        calls["n"] += 1
        return rec.source if calls["n"] == 1 else None

    monkeypatch.setattr(service, "lock_db_source_row", flaky_lock)
    result = _run()

    assert result["success"] is False
    assert "数据源登记已不存在" in result["message"]
    assert str(JOB_ID) in result["message"]


# ---- g) 降级语义 ----


def test_name_similarity_degradation_allowed(monkeypatch, tmp_path):
    """无 embedding 环境（构造非 InvalidNameSimilarityConfig 异常）→ 放行（§5.2 c）。"""
    _install(monkeypatch, tmp_path, config=_config_with(embedding={}))
    result = _run()
    assert result["success"] is True


# ---- h) 公开化共用（N2/N4/L2）----


def test_shared_helpers_public_names_exist():
    """md/domain 的私有函数公开化后，rel 服务按公开名导入（§5.2）。"""
    from backend.data_sources.domain_step_service import verify_domain_artifact
    from backend.data_sources.md_step_service import check_objects_in_scope

    assert callable(check_objects_in_scope)
    assert callable(verify_domain_artifact)


def test_precheck_relationships_non_object_is_config_error_not_503(
    monkeypatch, tmp_path
):
    """R11：relationships 配成字符串在 LLM 预检的 .get 即炸——构造预检前置后
    收成 400，不得落入 RelPreparationError/503（A15）。"""
    rec = _install(monkeypatch, tmp_path, config=_config_with(relationships="oops"))
    with pytest.raises(DbStepConfigError, match="relationships"):
        _run()
    assert rec.started == [] and rec.pipeline_kwargs == []


def test_precheck_llm_candidates_non_object_is_config_error(monkeypatch, tmp_path):
    """R11：llm_candidates 配成 list 同样先于 LLM 预检收成 400。"""
    config = _config_with(**{"relationships.llm_candidates": ["on"]})
    rec = _install(monkeypatch, tmp_path, config=config)
    with pytest.raises(DbStepConfigError, match="llm_candidates"):
        _run()
    assert rec.started == []


def test_clean_failure_close_failure_logs_inner_type(monkeypatch, tmp_path, caplog):
    """clean 与 close 双失败：警告记录 close 失败的类型，不被外层异常盖住。"""
    rec = _install(monkeypatch, tmp_path)

    def boom(_path):
        raise OSError("denied")

    monkeypatch.setattr(service, "clean_top_level_files", boom)
    rec.on_close = RuntimeError("pool close broke")
    with caplog.at_level(logging.WARNING):
        result = _run()

    assert result["success"] is False
    assert result["message"] == "rel 采集失败（OSError）"
    warning_text = " ".join(r.getMessage() for r in caplog.records)
    assert "连接池关闭失败（RuntimeError）" in warning_text


def test_rerun_backs_up_previous_artifacts(monkeypatch, tmp_path):
    """A18（doc 23.2 §5.3/D5）：受管重跑前把上一份 json+md 移入 rel/.backup/。

    备份目录先清空、只保留最近一份；备份在 clean 之前发生（旧产物先移走，
    再清顶层）；不影响采集核验（顶层恰两份新产物）与发布解冻（子目录不参与）。
    """
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / "demo.relationships_global.json").write_text(
        '{"old": true}', encoding="utf-8"
    )
    (rec.rel_dir / "demo.relationships_global.md").write_text(
        "# old report", encoding="utf-8"
    )

    result = _run()

    assert result["success"] is True
    backup = rec.rel_dir / ".backup"
    assert (backup / "demo.relationships_global.json").read_text(
        encoding="utf-8"
    ) == '{"old": true}'
    assert (backup / "demo.relationships_global.md").read_text(
        encoding="utf-8"
    ) == "# old report"
    top_files = sorted(p.name for p in rec.rel_dir.iterdir() if not p.is_dir())
    assert top_files == [
        "demo.relationships_global.json",
        "demo.relationships_global.md",
    ]

    # 再跑一次：备份被替换为上一轮新产物，仍只保留一份
    result_again = _run()
    assert result_again["success"] is True
    backed_files = sorted(p.name for p in backup.iterdir())
    assert backed_files == [
        "demo.relationships_global.json",
        "demo.relationships_global.md",
    ]
    backed_json = json.loads(
        (backup / "demo.relationships_global.json").read_text(encoding="utf-8")
    )
    assert backed_json["relationships"] == [{"relationship_id": "rel_x"}]
