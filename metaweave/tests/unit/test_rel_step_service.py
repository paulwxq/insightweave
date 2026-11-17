"""rel 步骤 worker 用例（doc 23.1 §5、doc 6 §4.1/§6）。

断代改造适配：上游就绪判定读 `json/`、`domain/` 顶层唯一成功采集 marker；
重跑前整目录备份取代仅保留最近一份的旧备份；构造先于备份（R6）。
"""

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import register_exception_handlers
from backend.data_sources import rel_step_service as service
from backend.data_sources.json_step_service import ManagedJsonInputError
from backend.data_sources.rel_step_service import RelPreparationError
from backend.data_sources.step_context import DbStepConfigError, StepContext
from backend.persistence.models import DbSourceInfo

SOURCE_ID = "db_src_1"
WORKSPACE_ID = "ws_1"
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")
JSON_RUN_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
DOMAIN_RUN_ID = UUID("cccccccc-dddd-4eee-8fff-000000000000")
DB_NAME = "demo"

MAIN_CONFIG = {
    "database": {"database": "cfgdb"},
    "output": {"output_dir": "output"},
    "relationships": {"llm_candidates": {"enabled": False}},
}

DOMAIN_YAML = yaml.safe_dump(
    {
        "database": {"name": DB_NAME, "description": "d"},
        "domains": [{"name": "订单", "tables": ["demo.public.orders"]}],
    },
    allow_unicode=True,
)


def _source(**overrides):
    values = dict(
        source_id=SOURCE_ID,
        display_name="测试源",
        description="源描述",
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


class _FakeConnector:
    def __init__(self, rec):
        self._rec = rec

    def close(self):
        self._rec.close_calls.append("connector")
        if self._rec.on_close is not None:
            raise self._rec.on_close


def _install(
    monkeypatch,
    tmp_path,
    *,
    objects=None,
    domain_yaml=DOMAIN_YAML,
    json_marker="success",
    domain_marker="success",
    main_config=None,
):
    """项目根指向 tmp_path；JSON 读取与 LLM 换替身；管线与备份走真实实现。"""
    from metaweave.utils import file_utils
    from backend.workspace_files.markers import write_collection_marker

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    import backend.security.source_password as crypto

    monkeypatch.setattr(crypto, "decrypt_password", lambda _cipher: "secret")

    objects = (
        objects
        if objects is not None
        else [{"schema": "public", "object": "orders", "object_type": "table"}]
    )
    workspace = tmp_path / "output" / SOURCE_ID / WORKSPACE_ID
    json_dir = workspace / "json"
    domain_dir = workspace / "domain"
    rel_dir = workspace / "rel"
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / "demo.public.orders.json").write_text("{}", encoding="utf-8")
    if json_marker:
        write_collection_marker(json_dir, JSON_RUN_ID, json_marker)
    domain_dir.mkdir(parents=True, exist_ok=True)
    if domain_yaml is not None:
        (domain_dir / "db_domains.yaml").write_text(domain_yaml, encoding="utf-8")
    if domain_marker:
        write_collection_marker(domain_dir, DOMAIN_RUN_ID, domain_marker)

    rec = SimpleNamespace(
        objects=objects,
        json_dir=json_dir,
        domain_dir=domain_dir,
        rel_dir=rel_dir,
        main_config=(
            main_config
            if main_config is not None
            else json.loads(json.dumps(MAIN_CONFIG))
        ),
        pipeline_kwargs=[],
        discover_calls=0,
        close_calls=[],
        on_init=None,
        on_discover=None,
        on_close=None,
        result_database=None,
        result_relationships=[{"from": "a", "to": "b"}],
        total=2,
        fk=1,
        inferred=1,
        output_override=None,
    )
    monkeypatch.setattr(
        service,
        "read_managed_json_input",
        lambda _sid, _wid, _db: rec.objects,
    )

    class FakeLLMService:
        def __init__(self, _config):
            rec.llm_constructed = True

    rec.llm_constructed = False
    monkeypatch.setattr(service, "LLMService", FakeLLMService)

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
            rec.pipeline_kwargs.append(
                {
                    "domain_filter": domain_filter,
                    "cross_domain": cross_domain,
                    "domain_resolver": domain_resolver,
                }
            )
            self._rec = rec
            self.config = config
            self.connector = _FakeConnector(rec)
            if rec.on_init is not None:
                rec.on_init(self)

        def discover(self):
            rec.discover_calls += 1
            if rec.on_discover is not None:
                rec.on_discover(self)
            rel_dir = Path(self.config["output"]["rel_directory"])
            db = self.config["database"]["database"]
            json_path = rel_dir / f"{db}.relationships_global.json"
            md_path = rel_dir / f"{db}.relationships_global.md"
            json_path.parent.mkdir(parents=True, exist_ok=True)
            outputs = (
                rec.output_override
                if rec.output_override is not None
                else [str(json_path), str(md_path)]
            )
            for path in outputs:
                if str(path).endswith(".json"):
                    Path(path).write_text(
                        json.dumps(
                            {
                                "database": (
                                    rec.result_database
                                    if rec.result_database is not None
                                    else db
                                ),
                                "relationships": rec.result_relationships,
                                "statistics": {"total_relationships_found": rec.total},
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    Path(path).write_text(
                        f"# 表间关系发现报告\ndatabase: {db}\n", encoding="utf-8"
                    )
            return SimpleNamespace(
                success=True,
                output_files=list(outputs),
                total_relations=rec.total,
                foreign_key_relations=rec.fk,
                inferred_relations=rec.inferred,
            )

    monkeypatch.setattr(service, "RelationshipDiscoveryPipeline", _FakePipeline)
    return rec


def _ctx(rec):
    return StepContext(
        settings=SimpleNamespace(),
        source=_source(),
        main_config=rec.main_config,
        config_path=Path("configs/metadata_config.yaml"),
        source_id=SOURCE_ID,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        step_name="rel",
        step_params=None,
    )


# ---- 错误类型与 API 映射 ----


def test_rel_preparation_error_maps_to_dedicated_503():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise RelPreparationError("Rel 准备阶段故障（快照构造：TypeError）")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 503
    assert response.json()["code"] == "rel_preparation_error"
    assert "TypeError" not in response.text  # 不回显异常原文


# ---- 上游就绪判定（json + domain 双 marker） ----


def test_no_json_marker_rejects_without_side_effect(monkeypatch, tmp_path):
    from metaweave.utils import file_utils

    monkeypatch.setattr(file_utils, "get_project_root", lambda: tmp_path)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_rel_step(
            StepContext(
                settings=SimpleNamespace(),
                source=_source(),
                main_config=json.loads(json.dumps(MAIN_CONFIG)),
                config_path=Path("cfg.yaml"),
                source_id=SOURCE_ID,
                workspace_id=WORKSPACE_ID,
                run_id=RUN_ID,
                step_name="rel",
                step_params=None,
            )
        )
    assert not (tmp_path / "output" / SOURCE_ID / WORKSPACE_ID / "rel").exists()


def test_failed_json_marker_rejects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, json_marker="failed")
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


def test_json_input_error_wrapped_as_not_ready(monkeypatch, tmp_path):
    def boom(*_args):
        raise ManagedJsonInputError("JSON 采集清单与目录实际文件不一致")

    rec = _install(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "read_managed_json_input", boom)
    with pytest.raises(DbStepConfigError, match="JSON 产物未就绪"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


def test_no_domain_marker_rejects(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path, domain_marker=None)
    with pytest.raises(DbStepConfigError, match="Domain 产物未就绪"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


@pytest.mark.parametrize(
    ("domain_yaml", "fragment"),
    [
        (None, "Domain 产物缺失"),
        ("{ not: valid", "YAML 无法解析"),
        ("database: {}\n", "domains 为空"),
    ],
)
def test_domain_artifact_invalid_rejected(monkeypatch, tmp_path, domain_yaml, fragment):
    rec = _install(monkeypatch, tmp_path, domain_yaml=domain_yaml)
    with pytest.raises(DbStepConfigError, match=fragment):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


def test_empty_domain_list_rejected_when_domain_effective(monkeypatch, tmp_path):
    """domain 生效但 YAML 无有效业务域（如 domains 全为非对象成员，可通过
    发布侧逐项校验前的核验）：明确拒绝（doc 23.1 §5.2）。"""
    rec = _install(
        monkeypatch,
        tmp_path,
        domain_yaml=yaml.safe_dump(
            {
                "database": {"name": DB_NAME},
                # 非空列表可通过 verify_domain_artifact，但解析不出任何业务域
                "domains": ["仅字符串成员"],
            }
        ),
        main_config={
            **MAIN_CONFIG,
            "relationships": {"domain": "all", "llm_candidates": {"enabled": False}},
        },
    )
    with pytest.raises(DbStepConfigError, match="未定义任何业务域"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


@pytest.mark.parametrize(
    ("objects", "config_extra"),
    [
        # schema 不在登记选中范围
        ([{"schema": "sales", "object": "orders", "object_type": "table"}], None),
        # 对象类型不在白名单
        ([{"schema": "public", "object": "orders", "object_type": "sequence"}], None),
        # 命中排除规则
        (
            [{"schema": "public", "object": "orders", "object_type": "table"}],
            {"database": {"exclude_tables": ["public.orders"]}},
        ),
    ],
)
def test_scope_mismatch_rejected_before_pipeline(
    monkeypatch, tmp_path, objects, config_extra
):
    main_config = json.loads(json.dumps(MAIN_CONFIG))
    if config_extra:
        main_config.update(config_extra)
    rec = _install(monkeypatch, tmp_path, objects=objects, main_config=main_config)
    with pytest.raises(DbStepConfigError, match="采集范围"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


# ---- 构造预检（_precheck_pipeline_config 纯函数族） ----


def _snapshot(**updates):
    snapshot = {
        "relationships": {"llm_candidates": {"enabled": False}},
        "sampling": {},
        "decision": {},
    }
    snapshot.update(updates)
    return snapshot


def test_precheck_top_k_invalid():
    with pytest.raises(ValueError, match="top_k"):
        service._precheck_pipeline_config(
            _snapshot(relationships={"llm_candidates": {"top_k": "abc"}})
        )


def test_precheck_null_not_exempt_for_decision():
    with pytest.raises(ValueError, match="decision"):
        service._precheck_pipeline_config(_snapshot(decision=None))


def test_precheck_null_not_exempt_for_relationships_weights():
    with pytest.raises(ValueError, match="weights"):
        service._precheck_pipeline_config(
            _snapshot(
                relationships={"llm_candidates": {"enabled": False}, "weights": None}
            )
        )


def test_precheck_null_allowed_for_nullable_families():
    # relationships/sampling/llm_candidates/candidate_matching/scoring 的
    # null 均按空对象豁免（doc 23.1 §5.2）
    service._precheck_pipeline_config(
        _snapshot(
            relationships=None,
            sampling=None,
        )
    )


def test_precheck_top_level_weights_used_when_absent_in_relationships():
    # relationships 下没有 weights 时回退校验顶层 weights
    with pytest.raises(ValueError, match="weights"):
        service._precheck_pipeline_config(_snapshot(weights=None))
    service._precheck_pipeline_config(_snapshot(weights={}))


def test_precheck_llm_batch_size_must_be_int():
    config = _snapshot(
        relationships={
            "llm_candidates": {"enabled": True},
            "llm": {"langchain_config": {"batch_size": "abc"}},
        }
    )
    with pytest.raises(ValueError, match="batch_size"):
        service._precheck_pipeline_config(config)


def test_precheck_name_similarity_degrades_not_config_error():
    # name_similarity 配置错误按降级语义处理（doc 15 §5），不是配置错误
    service._precheck_pipeline_config(
        _snapshot(
            relationships={
                "llm_candidates": {"enabled": False},
                "name_similarity": "not-an-object",
            }
        )
    )


# ---- LLM 预检 ----


def test_llm_precheck_value_error_maps_to_config_error(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={**MAIN_CONFIG, "relationships": {}},  # llm_candidates 默认 enabled
    )

    def boom(_config, _path):
        raise ValueError("relationships.llm 配置非法")

    monkeypatch.setattr(service, "resolve_module_llm_config", boom)
    with pytest.raises(DbStepConfigError, match="配置非法"):
        service.run_rel_step(_ctx(rec))
    assert rec.pipeline_kwargs == []


def test_llm_precheck_skipped_when_disabled(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)

    def boom(*_args):
        raise AssertionError("llm_candidates.enabled=false 不应构造 LLM")

    monkeypatch.setattr(service, "LLMService", boom)
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is True


def test_llm_precheck_unexpected_error_is_preparation_error(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={**MAIN_CONFIG, "relationships": {}},
    )
    monkeypatch.setattr(service, "resolve_module_llm_config", lambda *_args: {})

    class _Boom(Exception):
        pass

    def boom(_config):
        raise _Boom("unexpected")

    monkeypatch.setattr(service, "LLMService", boom)
    with pytest.raises(RelPreparationError, match="LLM 预检"):
        service.run_rel_step(_ctx(rec))


# ---- 构造先于备份（R6）与连接池归属（R10） ----


def test_construction_failure_keeps_old_artifacts(monkeypatch, tmp_path):
    """管线构造失败：不触发备份，旧产物原地保留（R6）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / f"{DB_NAME}.relationships_global.json").write_text(
        '{"database": "demo"}', encoding="utf-8"
    )
    (rec.rel_dir / f"{DB_NAME}.relationships_global.md").write_text(
        "old", encoding="utf-8"
    )

    def on_init(_pipeline):
        raise RuntimeError("pool creation failed")

    rec.on_init = on_init
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is False
    assert "Rel 采集失败（RuntimeError）" in outcome.message
    assert rec.discover_calls == 0
    assert rec.close_calls == []  # 构造期失败无连接池可关
    assert (rec.rel_dir / f"{DB_NAME}.relationships_global.json").exists()
    assert not (rec.rel_dir / ".backup").exists()


def test_backup_failure_closes_connector_and_keeps_artifacts(monkeypatch, tmp_path):
    """构造后、发现前（备份阶段）失败：显式关闭连接池，旧产物保留（R10）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / "old.json").write_text("{}", encoding="utf-8")

    def broken_backup(_directory, **_kwargs):
        raise RuntimeError("backup io error")

    monkeypatch.setattr(service, "backup_directory_before_update", broken_backup)
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is False
    assert "Rel 采集失败（RuntimeError）" in outcome.message
    assert rec.discover_calls == 0
    assert rec.close_calls == ["connector"]
    assert (rec.rel_dir / "old.json").exists()
    assert not (rec.rel_dir / ".backup").exists()


def test_discover_failure_marks_step_failed(monkeypatch, tmp_path):
    """发现阶段失败：按步骤失败计（发现已启动，不在构造期关闭分支内）。"""
    rec = _install(monkeypatch, tmp_path)
    rec.rel_dir.mkdir(parents=True, exist_ok=True)
    (rec.rel_dir / "old.json").write_text("{}", encoding="utf-8")

    def on_discover(_pipeline):
        raise RuntimeError("sampling failed")

    rec.on_discover = on_discover
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is False
    assert "Rel 采集失败（RuntimeError）" in outcome.message
    # 备份在 discover 之前完成
    (backup,) = list((rec.rel_dir / ".backup").iterdir())
    assert (backup / "old.json").exists()


# ---- 核验（verify_rel_collection 与集成失败分支） ----


def test_verify_rejects_wrong_output_pair(tmp_path):
    rel_dir = tmp_path / "rel"
    rel_dir.mkdir()
    single = rel_dir / "only.json"
    single.write_text("{}", encoding="utf-8")
    result = SimpleNamespace(success=True, output_files=[str(single)])
    ok, reason = service.verify_rel_collection(result, rel_dir, DB_NAME)
    assert not ok and "json 与 md 各一份" in reason


def test_verify_rejects_db_name_mismatch(tmp_path):
    rel_dir = tmp_path / "rel"
    rel_dir.mkdir()
    json_path = rel_dir / f"{DB_NAME}.relationships_global.json"
    json_path.write_text(
        json.dumps({"database": "other_db", "relationships": [], "statistics": {}}),
        encoding="utf-8",
    )
    md_path = rel_dir / f"{DB_NAME}.relationships_global.md"
    md_path.write_text("# 表间关系发现报告\n", encoding="utf-8")
    result = SimpleNamespace(success=True, output_files=[str(json_path), str(md_path)])
    ok, reason = service.verify_rel_collection(result, rel_dir, DB_NAME)
    assert not ok and "库名与登记源不一致" in reason


def test_verify_ignores_dot_entries_in_top_level(tmp_path):
    """marker 与 .backup/ 不参与顶层产物比对（doc 6 §6）。"""
    rel_dir = tmp_path / "rel"
    rel_dir.mkdir()
    json_path = rel_dir / f"{DB_NAME}.relationships_global.json"
    json_path.write_text(
        json.dumps({"database": DB_NAME, "relationships": [], "statistics": {}}),
        encoding="utf-8",
    )
    md_path = rel_dir / f"{DB_NAME}.relationships_global.md"
    md_path.write_text("# 表间关系发现报告\n", encoding="utf-8")
    (
        rel_dir
        / ".coll_run_id_99999999-9999-4999-8999-999999999999_20260101-000000000_success"
    ).touch()
    (rel_dir / ".backup").mkdir()
    result = SimpleNamespace(success=True, output_files=[str(json_path), str(md_path)])
    assert service.verify_rel_collection(result, rel_dir, DB_NAME) == (True, "")


def test_extra_top_level_file_fails_verification(monkeypatch, tmp_path):
    """顶层混入清单外普通文件：集合不一致判失败。"""
    rec = _install(monkeypatch, tmp_path)

    def on_discover(pipeline):
        extra = Path(pipeline.config["output"]["rel_directory"]) / "extra.txt"
        extra.write_text("stray", encoding="utf-8")

    rec.on_discover = on_discover
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is False
    assert "实际输出文件与声明清单不一致" in outcome.message


# ---- 成功路径与零关系告警 ----


def test_success_reports_relation_counts_and_inputs(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    outcome = service.run_rel_step(_ctx(rec))

    assert outcome.success is True, outcome.message
    assert "2 条关系：外键 1 / 推断 1" in outcome.message
    (kwargs,) = rec.pipeline_kwargs
    assert kwargs["domain_filter"] is None  # relationships.domain 未配置 → 不分组
    assert kwargs["domain_resolver"] is None
    # 输入清单：json 产物 + domain yaml，各自携带采集来源运行（doc 6.1 §11）
    assert outcome.input_files == [
        ("json/demo.public.orders.json", str(JSON_RUN_ID)),
        ("domain/db_domains.yaml", str(DOMAIN_RUN_ID)),
    ]
    assert outcome.output_files == [
        f"rel/{DB_NAME}.relationships_global.json",
        f"rel/{DB_NAME}.relationships_global.md",
    ]


def test_domain_effective_uses_resolver(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={
            **MAIN_CONFIG,
            "relationships": {"domain": "all", "llm_candidates": {"enabled": False}},
        },
    )
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is True
    (kwargs,) = rec.pipeline_kwargs
    assert kwargs["domain_filter"] == "all"
    assert kwargs["domain_resolver"] is not None


def test_zero_relations_with_null_domain_warns(monkeypatch, tmp_path):
    rec = _install(monkeypatch, tmp_path)
    rec.total = 0
    rec.fk = 0
    rec.inferred = 0
    rec.result_relationships = []
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is True
    assert "0 条关系" in outcome.message
    assert "外键与推断均为空" in outcome.message


def test_zero_inferred_with_domain_warns(monkeypatch, tmp_path):
    rec = _install(
        monkeypatch,
        tmp_path,
        main_config={
            **MAIN_CONFIG,
            "relationships": {"domain": "all", "llm_candidates": {"enabled": False}},
        },
    )
    rec.inferred = 0
    outcome = service.run_rel_step(_ctx(rec))
    assert outcome.success is True
    assert "domain 分组下推断为空" in outcome.message
