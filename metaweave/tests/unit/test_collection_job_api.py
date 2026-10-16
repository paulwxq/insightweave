"""新建采集作业 API：接受 db 与受管 ddl，分别复用 run_db_step / run_ddl_step。"""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.data_sources.db_step_service import DbStepConfigError, SourceDisabledError
from backend.data_sources.registration_service import SourceNotFoundError
from backend.jobs import collection_run
from backend.persistence.platform_db import PlatformSettings

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
JOB_ID = "65de2930-e024-45d7-a292-1408bc8b59b1"


def _client():
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    )


def _post(client, steps, source_id=SOURCE_ID):
    return client.post(
        f"/api/data-sources/{source_id}/collection-jobs",
        json={"steps": steps},
    )


def test_rejects_unsupported_steps(monkeypatch):
    """doc 18 验收 2：用真正不支持的步骤覆盖 step_not_supported（json 已接入）。"""
    monkeypatch.setattr(
        collection_run, "run_db_step", lambda *_args, **_kwargs: pytest.fail("不应执行")
    )
    monkeypatch.setattr(
        collection_run,
        "run_ddl_step",
        lambda *_args, **_kwargs: pytest.fail("不应执行"),
    )
    monkeypatch.setattr(
        collection_run,
        "run_json_step",
        lambda *_args, **_kwargs: pytest.fail("不应执行"),
    )
    with _client() as client:
        response = _post(client, ["db", "cql"])
    assert response.status_code == 400
    assert response.json()["code"] == "step_not_supported"
    assert (
        response.json()["message"]
        == "当前仅支持 db、ddl、json、md、domain、dim_config、rel、sql 步骤"
    )


def test_normalize_accepts_json_and_orders_ddl_first():
    assert collection_run.normalize_collection_steps(
        ["json", "ddl", "ddl", "json"]
    ) == [
        "ddl",
        "json",
    ]


# ---- doc 21.1 批次 3：domain 进入白名单、偏序与阻断、描述透传 ----


def test_normalize_accepts_domain_and_orders_after_md():
    assert collection_run.normalize_collection_steps(
        ["domain", "md", "json", "ddl"]
    ) == ["ddl", "json", "md", "domain"]
    # §6.2：同请求未勾选 md 时 domain 保持传入位置（读既有 md 产物）
    assert collection_run.normalize_collection_steps(["domain", "ddl"]) == [
        "domain",
        "ddl",
    ]


def test_normalize_unknown_step_message_lists_domain():
    with pytest.raises(collection_run.UnsupportedCollectionStepError) as excinfo:
        collection_run.normalize_collection_steps(["db", "cql"])
    assert "domain" in str(excinfo.value)


# ---- doc 22.1 批次 2：dim_config 进入白名单、拓扑排序与阻断 ----


def test_normalize_accepts_dim_config_and_orders_after_json():
    assert collection_run.normalize_collection_steps(["dim_config", "json"]) == [
        "json",
        "dim_config",
    ]
    # 审核修订 3：交错顺序——md 与 dim_config 无依赖边，保持传入相对序
    assert collection_run.normalize_collection_steps(["md", "dim_config", "json"]) == [
        "json",
        "md",
        "dim_config",
    ]
    # 审核修订 4：确定性反例——md 被 ddl→md 阻塞期间 dim_config 先行；
    # 锁定规则结果，验收以规则为准而非普遍相对序
    assert collection_run.normalize_collection_steps(["md", "dim_config", "ddl"]) == [
        "dim_config",
        "ddl",
        "md",
    ]
    # §6.2：同请求未勾选 json 时 dim_config 没有有效边，保持传入位置
    assert collection_run.normalize_collection_steps(["dim_config", "ddl"]) == [
        "dim_config",
        "ddl",
    ]


def test_normalize_unknown_step_message_lists_dim_config():
    with pytest.raises(collection_run.UnsupportedCollectionStepError) as excinfo:
        collection_run.normalize_collection_steps(["db", "cql"])
    assert "dim_config" in str(excinfo.value)


def test_json_failure_in_same_request_blocks_dim_config(monkeypatch):
    """§6.3：同请求 json 已在 results 失败 → 阻断 dim_config，不调用服务、不登记。"""
    job_id = UUID(JOB_ID)

    def fail_json(_config, _source, active_job=None):
        assert active_job == job_id
        return {
            "success": False,
            "message": "JSON 生成失败（RuntimeError）",
            "job_id": str(job_id),
        }

    monkeypatch.setattr(collection_run, "run_json_step", fail_json)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    monkeypatch.setattr(
        collection_run,
        "run_dim_config_step",
        lambda *_a, **_k: pytest.fail("被阻断的步骤不得执行"),
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["json", "dim_config"], "job_id": JOB_ID},
        )
    assert response.status_code == 200
    rows = {row["step"]: row for row in response.json()["results"]}
    assert rows["json"]["status"] == "failed"
    assert rows["dim_config"]["status"] == "failed"
    assert "JSON 未成功" in rows["dim_config"]["message"]


def test_dim_config_first_step_without_job_id_is_400(monkeypatch):
    """首步 dim_config 且无 job_id：真实服务在入口拒绝，400 invalid_request、不登记。"""
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["dim_config"])
    assert response.status_code == 400
    assert "JSON 产物未就绪" in response.json()["message"]


def test_dim_config_dispatched_to_service(monkeypatch):
    """编排层把 dim_config 路由到 run_dim_config_step（§6.1/§7）。"""
    captured = {}

    def fake_dim_config(_config, _source, active_job=None, **_kwargs):
        captured["active_job"] = active_job
        return {"success": True, "message": "ok", "job_id": str(JOB_ID)}

    monkeypatch.setattr(collection_run, "run_dim_config_step", fake_dim_config)
    result = collection_run.start_db_collection(SOURCE_ID, ["dim_config"], UUID(JOB_ID))
    assert result["job_id"] == JOB_ID
    assert captured["active_job"] == UUID(JOB_ID)
    assert result["results"][0]["step"] == "dim_config"
    assert result["results"][0]["status"] == "success"


def test_md_failure_in_same_request_blocks_domain(monkeypatch):
    """§6.3：同请求中 md 已在 results 中失败 → 阻断 domain，不调用 run_domain_step。"""
    job_id = UUID(JOB_ID)

    def fail_md(_config, _source, active_job=None):
        assert active_job == job_id
        return {
            "success": False,
            "message": "MD 生成失败（RuntimeError）",
            "job_id": str(job_id),
        }

    monkeypatch.setattr(collection_run, "run_md_step", fail_md)
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_a: {"source_id": "x"})
    monkeypatch.setattr(
        collection_run,
        "run_domain_step",
        lambda *_a, **_k: pytest.fail("被阻断的步骤不得执行"),
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["md", "domain"], "job_id": JOB_ID},
        )
    assert response.status_code == 200
    rows = {row["step"]: row for row in response.json()["results"]}
    assert rows["md"]["status"] == "failed"
    assert rows["domain"]["status"] == "failed"
    assert "MD 未成功" in rows["domain"]["message"]


def test_domain_first_step_without_job_id_is_400(monkeypatch):
    """首步 domain 且无 job_id：真实服务在入口拒绝，400 invalid_request、不登记。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_a: {"source_id": "x"})
    with _client() as client:
        response = _post(client, ["domain"])
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "MD 产物未就绪" in response.json()["message"]


def test_domain_rerun_missing_workspace_is_400_not_503(monkeypatch):
    """重跑只选 domain 且工作区不存在：受控配置错误 400，不是平台库 503（批次 3）。"""

    def boom(*_a, **_k):
        raise DbStepConfigError(f"指定的 job_id 不存在: {JOB_ID}")

    monkeypatch.setattr(collection_run, "run_domain_step", boom)
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_a: {"source_id": "x"})
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["domain"], "job_id": JOB_ID},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "指定的 job_id 不存在" in response.json()["message"]


def test_domain_step_receives_description_and_sync_flags(monkeypatch):
    """§7.1：domain_description 与 sync 标志经编排层原样透传到 run_domain_step。"""
    captured = {}

    def fake_domain(_config, _source, active_job=None, **kwargs):
        captured["active_job"] = active_job
        captured.update(kwargs)
        return {"success": True, "message": "ok", "job_id": str(JOB_ID)}

    monkeypatch.setattr(collection_run, "run_domain_step", fake_domain)
    result = collection_run.start_db_collection(
        SOURCE_ID,
        ["domain"],
        UUID(JOB_ID),
        domain_description="新描述",
        sync_domain_description=True,
    )
    assert result["job_id"] == JOB_ID
    assert captured["active_job"] == UUID(JOB_ID)
    assert captured["domain_description"] == "新描述"
    assert captured["sync_domain_description"] is True


def test_domain_step_defaults_pass_sentinel(monkeypatch):
    """未携带：编排层传服务哨兵，不把缺省误当成显式 None（§5.3）。"""
    from backend.data_sources.domain_step_service import _UNSET

    captured = {}

    def fake_domain(_config, _source, active_job=None, **kwargs):
        captured.update(kwargs)
        return {"success": True, "message": "ok", "job_id": str(JOB_ID)}

    monkeypatch.setattr(collection_run, "run_domain_step", fake_domain)
    collection_run.start_db_collection(SOURCE_ID, ["domain"], UUID(JOB_ID))
    assert captured["domain_description"] is _UNSET
    assert captured["sync_domain_description"] is False


# ---- doc 21.1 §7.1：请求 schema 校验与路由透传 ----


@pytest.mark.parametrize(
    "payload",
    [
        {"steps": ["md"], "domain_description": None},
        {"steps": ["md"], "domain_description": ""},
        {"steps": ["md"], "domain_description": "   "},
        {"steps": ["md"], "domain_description": "x" * 1001},
        {"steps": ["md"], "domain_description": 123},
        {"steps": ["domain"], "sync_domain_description": True},
        {"steps": ["md"], "domain_description": "a", "sync_domain_description": True},
        {"steps": ["domain"], "domain_description": "a", "sync_domain_description": "true"},
        {"steps": ["domain"], "domain_description": "a", "sync_domain_description": 1},
        {"steps": ["domain"], "domain_description": "a", "unknown_field": 1},
    ],
)
def test_create_job_schema_rejections(payload):
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs", json=payload
        )
    assert response.status_code == 422


def test_create_job_passes_stripped_description_and_sync(monkeypatch):
    from backend.api.routers import data_sources as data_sources_router

    captured = {}

    async def fake_start(settings, source_id, steps, job_id=None, **kwargs):
        captured["steps"] = steps
        captured.update(kwargs)
        return {"job_id": JOB_ID, "results": []}

    monkeypatch.setattr(data_sources_router, "start_collection_job", fake_start)
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={
                "steps": ["md", "domain"],
                "domain_description": "  新描述  ",
                "sync_domain_description": True,
            },
        )
    assert response.status_code == 200
    assert captured["domain_description"] == "新描述"
    assert captured["sync_domain_description"] is True


def test_create_job_omitted_description_passes_sentinel(monkeypatch):
    from backend.api.routers import data_sources as data_sources_router
    from backend.data_sources.domain_step_service import _UNSET

    captured = {}

    async def fake_start(settings, source_id, steps, job_id=None, **kwargs):
        captured.update(kwargs)
        return {"job_id": JOB_ID, "results": []}

    monkeypatch.setattr(data_sources_router, "start_collection_job", fake_start)
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["md", "domain"]},
        )
    assert response.status_code == 200
    assert captured["domain_description"] is _UNSET
    assert captured["sync_domain_description"] is False


def test_json_without_ddl_new_job_reports_ddl_not_ready(monkeypatch):
    """doc 18 §3.1：新作业 JSON 单选由准备链受控拒绝，首步 400 invalid_request。"""
    calls = []

    def fail_json(config_path, source_id, job_id=None):
        calls.append(job_id)
        raise DbStepConfigError(
            "同一作业的 DDL 产物未就绪；请先执行 DDL 或指定已有 job_id"
        )

    monkeypatch.setattr(collection_run, "run_json_step", fail_json)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["json"])
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "DDL 产物未就绪" in response.json()["message"]
    assert calls == [None]  # 未伪造 job_id


def test_json_runs_after_ddl_with_same_job_id(monkeypatch):
    """doc 18 §3：同请求即使乱序传入也 DDL 先于 JSON，共用同一 job_id。"""
    from uuid import UUID as _UUID

    order = []
    db_job = "22222222-2222-2222-2222-222222222222"

    def fake_db(config_path, source_id, clean, job_id=None):
        used = job_id or _UUID(db_job)
        order.append(("db", job_id))
        return {"success": True, "message": "ok", "job_id": str(used)}

    def fake_ddl(config_path, source_id, job_id=None):
        order.append(("ddl", job_id))
        return {"success": True, "message": "ok", "job_id": str(job_id)}

    def fake_json(config_path, source_id, job_id=None):
        order.append(("json", job_id))
        return {"success": True, "message": "ok", "job_id": str(job_id)}

    monkeypatch.setattr(collection_run, "run_db_step", fake_db)
    monkeypatch.setattr(collection_run, "run_ddl_step", fake_ddl)
    monkeypatch.setattr(collection_run, "run_json_step", fake_json)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["json", "db", "ddl"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == db_job
    assert [step for step, _ in order] == ["db", "ddl", "json"]
    assert order[1][1] == _UUID(db_job)  # DDL 复用 db 建立的 job_id
    assert order[2][1] == _UUID(db_job)  # JSON 复用同一 job_id
    assert [r["step"] for r in body["results"]] == ["db", "ddl", "json"]
    assert all(r["status"] == "success" for r in body["results"])


def test_rejects_empty_steps(monkeypatch):
    monkeypatch.setattr(
        collection_run, "run_db_step", lambda *_args, **_kwargs: pytest.fail("不应执行")
    )
    with _client() as client:
        response = _post(client, [])
    assert response.status_code == 400
    assert response.json()["message"] == "请至少选择一个步骤"


def test_missing_source_is_not_found_before_collection(monkeypatch):
    def missing(_settings, _source_id):
        raise SourceNotFoundError("数据源不存在")

    monkeypatch.setattr(collection_run, "get_registered_source", missing)
    monkeypatch.setattr(
        collection_run, "run_db_step", lambda *_args, **_kwargs: pytest.fail("不应执行")
    )
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_runs_db_step_without_reusing_job_or_cleaning(monkeypatch):
    seen = {}

    def fake_source(_settings, source_id):
        return {"source_id": str(source_id)}

    def fake_run(config_path, source_id, clean, job_id=None):
        seen["config"] = config_path
        seen["source_id"] = source_id
        seen["clean"] = clean
        seen["job_id"] = job_id
        return {"success": True, "message": "数据库概览采集完成", "job_id": JOB_ID}

    monkeypatch.setattr(collection_run, "get_registered_source", fake_source)
    monkeypatch.setattr(collection_run, "run_db_step", fake_run)
    with _client() as client:
        response = _post(client, ["db", "db"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["results"][0]["step"] == "db"
    assert body["results"][0]["status"] == "success"
    assert body["results"][0]["message"] == "数据库概览采集完成"
    assert seen["source_id"] == SOURCE_ID
    assert seen["clean"] is False
    assert seen["job_id"] is None
    assert seen["config"].name == "metadata_config.yaml"


def test_rerun_passes_existing_job_id_without_cleaning(monkeypatch):
    seen = {}

    def fake_run(config_path, source_id, clean, job_id=None):
        seen["source_id"] = source_id
        seen["clean"] = clean
        seen["job_id"] = job_id
        return {"success": True, "message": "数据库概览采集完成", "job_id": str(job_id)}

    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(collection_run, "run_db_step", fake_run)
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["db"], "job_id": JOB_ID},
        )
    assert response.status_code == 200
    assert response.json()["job_id"] == JOB_ID
    assert seen["source_id"] == SOURCE_ID
    assert seen["clean"] is False
    assert seen["job_id"] == UUID(JOB_ID)


def test_collection_failure_keeps_job_id(monkeypatch):
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": False,
            "message": "源库版本检查失败: 连接被拒绝",
            "job_id": JOB_ID,
        },
    )
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["results"][0]["status"] == "failed"
    assert "连接被拒绝" in body["results"][0]["message"]


def test_config_error_is_bad_request(monkeypatch):
    def bad_config(*_args, **_kwargs):
        raise DbStepConfigError("主配置加载或校验失败（ValueError）")

    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(collection_run, "run_db_step", bad_config)
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_disabled_source_is_conflict(monkeypatch):
    """禁用数据源 → 409 source_disabled（子类 handler 优先于 DbStepConfigError 的 400）。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_a: {})

    def disabled(*_args, **_kwargs):
        raise SourceDisabledError("该数据源已禁用，不能执行作业")

    monkeypatch.setattr(collection_run, "run_db_step", disabled)
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 409
    assert response.json() == {
        "code": "source_disabled",
        "message": "该数据源已禁用，不能发起采集作业",
    }


def test_platform_failure_before_registration_is_unavailable(monkeypatch):
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": False,
            "message": "平台库连接或读取失败（SQLAlchemyError）",
        },
    )
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"


def test_later_step_config_error_returns_partial_result(monkeypatch):
    """db 成功后 ddl 报准备链配置错误：带 job_id 返回部分结果，不整体抛。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": True,
            "message": "数据库概览采集完成",
            "job_id": JOB_ID,
        },
    )

    def bad_ddl(_config_path, _source_id, job_id=None):
        raise DbStepConfigError(f"指定的 job_id 不存在: {job_id}")

    monkeypatch.setattr(collection_run, "run_ddl_step", bad_ddl)
    with _client() as client:
        response = _post(client, ["db", "ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert [item["step"] for item in body["results"]] == ["db", "ddl"]
    assert body["results"][0]["status"] == "success"
    assert body["results"][1]["status"] == "failed"
    assert "job_id 不存在" in body["results"][1]["message"]


def test_later_step_disabled_source_still_returns_partial_result(monkeypatch):
    """首步成功后源被禁用：后续步骤计入结果而非整体 409。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": True,
            "message": "数据库概览采集完成",
            "job_id": JOB_ID,
        },
    )

    def disabled_ddl(*_args, **_kwargs):
        raise SourceDisabledError("该数据源已禁用，不能执行作业")

    monkeypatch.setattr(collection_run, "run_ddl_step", disabled_ddl)
    with _client() as client:
        response = _post(client, ["db", "ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["results"][1]["status"] == "failed"
    assert "已禁用" in body["results"][1]["message"]


def test_later_step_platform_failure_returns_partial_result(monkeypatch):
    """db 成功后 ddl 平台库故障：带 job_id 返回部分结果，不再整体 503。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": True,
            "message": "数据库概览采集完成",
            "job_id": JOB_ID,
        },
    )
    # 平台库故障发生在 ddl 登记 job_info 之前：失败结果不带 job_id
    monkeypatch.setattr(
        collection_run,
        "run_ddl_step",
        lambda *_args, **_kwargs: {
            "success": False,
            "message": "平台库连接或读取失败（SQLAlchemyError）",
        },
    )
    with _client() as client:
        response = _post(client, ["db", "ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert [item["step"] for item in body["results"]] == ["db", "ddl"]
    assert body["results"][0]["status"] == "success"
    assert body["results"][1]["status"] == "failed"
    assert "平台库连接或读取失败" in body["results"][1]["message"]


def test_rerun_first_step_config_error_is_not_partial_result(monkeypatch):
    """重跑请求自带 job_id 但第一步就报工作区校验错误：本次请求还没有
    任何已执行步骤，不能拿请求的 job_id 冒充部分结果，应整体 400。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})

    def bad_workspace(_config_path, _source_id, _clean=False, job_id=None):
        raise DbStepConfigError(f"指定的 job_id 不存在: {job_id}")

    monkeypatch.setattr(collection_run, "run_db_step", bad_workspace)
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["db"], "job_id": JOB_ID},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert "job_id 不存在" in response.json()["message"]


def test_rerun_first_step_platform_failure_is_503(monkeypatch):
    """重跑请求第一步就遇平台库故障：整体 503，不返回部分结果。"""
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(
        collection_run,
        "run_db_step",
        lambda *_args, **_kwargs: {
            "success": False,
            "message": "平台库连接或读取失败（SQLAlchemyError）",
        },
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["db"], "job_id": JOB_ID},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"


def test_db_then_ddl_reuse_same_job_id(monkeypatch):
    """一次请求选 db、ddl：沿同一 job_id 顺序执行，各步骤分别记录结果。"""
    calls = []

    def fake_db(config_path, source_id, clean, job_id=None):
        calls.append(("db", source_id, clean, job_id))
        return {"success": True, "message": "数据库概览采集完成", "job_id": JOB_ID}

    def fake_ddl(config_path, source_id, job_id=None):
        calls.append(("ddl", source_id, job_id))
        return {
            "success": True,
            "message": "受管 DDL 采集完成（3 个对象）",
            "job_id": str(job_id),
        }

    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(collection_run, "run_db_step", fake_db)
    monkeypatch.setattr(collection_run, "run_ddl_step", fake_ddl)
    with _client() as client:
        response = _post(client, ["db", "ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert [item["step"] for item in body["results"]] == ["db", "ddl"]
    assert all(item["status"] == "success" for item in body["results"])
    assert "3 个对象" in body["results"][1]["message"]
    assert calls == [
        ("db", SOURCE_ID, False, None),
        ("ddl", SOURCE_ID, UUID(JOB_ID)),
    ]


def test_ddl_only_creates_new_workspace(monkeypatch):
    seen = {}

    def fake_ddl(config_path, source_id, job_id=None):
        seen["source_id"] = source_id
        seen["job_id"] = job_id
        return {
            "success": True,
            "message": "受管 DDL 采集完成（2 个对象）",
            "job_id": JOB_ID,
        }

    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})
    monkeypatch.setattr(collection_run, "run_ddl_step", fake_ddl)
    with _client() as client:
        response = _post(client, ["ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["results"][0]["step"] == "ddl"
    assert seen["source_id"] == SOURCE_ID and seen["job_id"] is None


def test_ddl_failure_recorded_and_keeps_job_id(monkeypatch):
    monkeypatch.setattr(collection_run, "get_registered_source", lambda *_args: {})

    def failing_ddl(_config_path, _source_id, job_id=None):
        return {
            "success": False,
            "message": "选中范围内没有可采集对象",
            "job_id": JOB_ID,
        }

    monkeypatch.setattr(collection_run, "run_ddl_step", failing_ddl)
    with _client() as client:
        response = _post(client, ["ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["results"][0]["status"] == "failed"
    assert "没有可采集对象" in body["results"][0]["message"]


def test_normalize_preserves_db_ddl_request_order():
    """doc 18 §3 只授权 ddl 先于 json：db/ddl 的传入顺序保持不变。"""
    assert collection_run.normalize_collection_steps(["ddl", "db"]) == ["ddl", "db"]
    assert collection_run.normalize_collection_steps(["db", "ddl"]) == ["db", "ddl"]


def test_db_success_then_json_prep_failure_returns_partial_result(monkeypatch):
    """doc 18 验收 2 逐字组合：db+json 时 DB 结果保留，JSON 准备失败计入
    逐步结果（带 job_id 返回部分结果）。"""
    from uuid import UUID as _UUID

    db_job = "33333333-3333-3333-3333-333333333333"
    json_calls = []

    def fake_db(config_path, source_id, clean, job_id=None):
        return {"success": True, "message": "数据库概览采集完成", "job_id": db_job}

    def fail_json(config_path, source_id, job_id=None):
        json_calls.append(job_id)
        raise DbStepConfigError(
            "同一作业的 DDL 产物未就绪；请先执行 DDL 或指定已有 job_id"
        )

    monkeypatch.setattr(collection_run, "run_db_step", fake_db)
    monkeypatch.setattr(collection_run, "run_json_step", fail_json)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["db", "json"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == db_job
    assert [(r["step"], r["status"]) for r in body["results"]] == [
        ("db", "success"),
        ("json", "failed"),
    ]
    assert "DDL 产物未就绪" in body["results"][1]["message"]
    assert json_calls == [_UUID(db_job)]  # JSON 复用 DB 建立的 job_id


# --- doc 19.1 §3：md 接入、依赖排序与同请求编排阻断 --------------------------


def test_normalize_orders_managed_chain_with_md():
    assert collection_run.normalize_collection_steps(["md", "json", "ddl"]) == [
        "ddl",
        "json",
        "md",
    ]
    # db 保持传入相对位置，仅链内偏序生效
    assert collection_run.normalize_collection_steps(["md", "db", "ddl"]) == [
        "db",
        "ddl",
        "md",
    ]


def test_ddl_failure_blocks_json_and_md_in_same_request(monkeypatch):
    """doc 19.1 §3（含 doc 18 §3.3 修订）：本次 DDL 失败时，编排层按
    results 阻断后继步骤——不调服务、不登记 running、不清理旧目录。"""
    calls = []

    def fail_ddl(config_path, source_id, job_id=None):
        calls.append("ddl")
        return {
            "success": False,
            "execution_time": 0,
            "message": "DDL 采集失败（RuntimeError）",
            "job_id": "44444444-4444-4444-4444-444444444444",
        }

    def must_not_run(*_args, **_kwargs):
        calls.append("violated")
        raise AssertionError("被阻断步骤不应执行")

    monkeypatch.setattr(collection_run, "run_ddl_step", fail_ddl)
    monkeypatch.setattr(collection_run, "run_json_step", must_not_run)
    monkeypatch.setattr(collection_run, "run_md_step", must_not_run)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["ddl", "json", "md"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == ["ddl"]
    assert [(r["step"], r["status"]) for r in body["results"]] == [
        ("ddl", "failed"),
        ("json", "failed"),
        ("md", "failed"),
    ]
    assert "本次请求的 DDL 未成功，JSON 未启动" in body["results"][1]["message"]
    assert "如需使用已有 DDL 产物，请单独重跑 JSON" in body["results"][1]["message"]
    # 选了 JSON 时 MD 看直接上游（doc 19.1 §3 消息表）；根因仍是 DDL
    assert "本次请求的 JSON 未成功，MD 未启动" in body["results"][2]["message"]


def test_json_failure_blocks_md_in_same_request(monkeypatch):
    calls = []

    def ok_ddl(config_path, source_id, job_id=None):
        calls.append("ddl")
        return {
            "success": True,
            "message": "ok",
            "job_id": "55555555-5555-5555-5555-555555555555",
        }

    def fail_json(config_path, source_id, job_id=None):
        calls.append("json")
        return {
            "success": False,
            "execution_time": 0,
            "message": "JSON 采集失败（RuntimeError）",
            "job_id": "55555555-5555-5555-5555-555555555555",
        }

    def must_not_run(*_a, **_k):
        raise AssertionError("MD 不应执行")

    monkeypatch.setattr(collection_run, "run_ddl_step", ok_ddl)
    monkeypatch.setattr(collection_run, "run_json_step", fail_json)
    monkeypatch.setattr(collection_run, "run_md_step", must_not_run)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["ddl", "json", "md"])
    body = response.json()
    assert calls == ["ddl", "json"]
    assert "本次请求的 JSON 未成功，MD 未启动" in body["results"][2]["message"]
    assert "如需使用已有 JSON 产物，请单独重跑 MD" in body["results"][2]["message"]


def test_ddl_md_without_json_blocked_by_ddl_failure(monkeypatch):
    """db+ddl+md（未选 json）：DDL 失败阻断 MD（doc 19.1 §3）。"""

    def ok_db(config_path, source_id, clean, job_id=None):
        return {
            "success": True,
            "message": "ok",
            "job_id": "66666666-6666-6666-6666-666666666666",
        }

    def fail_ddl(config_path, source_id, job_id=None):
        return {
            "success": False,
            "execution_time": 0,
            "message": "DDL 采集失败（RuntimeError）",
            "job_id": "66666666-6666-6666-6666-666666666666",
        }

    def must_not_run(*_a, **_k):
        raise AssertionError("MD 不应执行")

    monkeypatch.setattr(collection_run, "run_db_step", ok_db)
    monkeypatch.setattr(collection_run, "run_ddl_step", fail_ddl)
    monkeypatch.setattr(collection_run, "run_md_step", must_not_run)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_args: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["md", "ddl", "db"])
    body = response.json()
    # db 传入时排在 md 之后，保持其相对位置；链内 DDL 先于 MD（doc 19.1 §3）
    assert [r["step"] for r in body["results"]] == ["ddl", "md", "db"]
    assert body["results"][1]["status"] == "failed"
    assert "本次请求的 DDL 未成功，MD 未启动" in body["results"][1]["message"]
    assert body["results"][2]["status"] == "success"  # db 不受链内失败影响


# ---- rel 步骤（doc 23.1 §6） ----


def test_normalize_accepts_rel_and_orders_after_json_and_domain():
    """§6.2：json→rel、domain→rel 偏序成立；无有效边时保持传入序。"""
    assert collection_run.normalize_collection_steps(
        ["rel", "json", "ddl", "md", "domain"]
    ) == ["ddl", "json", "md", "domain", "rel"]
    # json/domain 未选时 rel 没有有效边，保持传入位置（与 dim_config 同规则）
    assert collection_run.normalize_collection_steps(["rel", "ddl"]) == ["rel", "ddl"]


def test_normalize_unknown_step_message_lists_rel():
    with pytest.raises(collection_run.UnsupportedCollectionStepError) as excinfo:
        collection_run.normalize_collection_steps(["db", "cql"])
    assert "rel" in str(excinfo.value)


def test_json_failure_in_same_request_blocks_rel(monkeypatch):
    """§6.3：同请求 json 失败 → 阻断 rel，不调用服务、不登记。"""
    job_id = UUID(JOB_ID)

    def fail_json(_config, _source, active_job=None):
        return {
            "success": False,
            "message": "JSON 生成失败（RuntimeError）",
            "job_id": str(job_id),
        }

    monkeypatch.setattr(collection_run, "run_json_step", fail_json)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    monkeypatch.setattr(
        collection_run,
        "run_rel_step",
        lambda *_a, **_k: pytest.fail("被阻断的步骤不得执行"),
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["json", "rel"], "job_id": JOB_ID},
        )
    assert response.status_code == 200
    rows = {row["step"]: row for row in response.json()["results"]}
    assert rows["json"]["status"] == "failed"
    assert rows["rel"]["status"] == "failed"
    assert "JSON 未成功" in rows["rel"]["message"]


def test_domain_failure_in_same_request_blocks_rel(monkeypatch):
    """§6.3：同请求 domain 失败 → 阻断 rel（json 未选不阻断，由准备链查既有产物）。"""
    job_id = UUID(JOB_ID)

    def fail_domain(_config, _source, active_job=None, **_kwargs):
        return {
            "success": False,
            "message": "Domain 生成失败（RuntimeError）",
            "job_id": str(job_id),
        }

    monkeypatch.setattr(collection_run, "run_domain_step", fail_domain)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    monkeypatch.setattr(
        collection_run,
        "run_rel_step",
        lambda *_a, **_k: pytest.fail("被阻断的步骤不得执行"),
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["domain", "rel"], "job_id": JOB_ID},
        )
    assert response.status_code == 200
    rows = {row["step"]: row for row in response.json()["results"]}
    assert rows["domain"]["status"] == "failed"
    assert rows["rel"]["status"] == "failed"
    assert "Domain 未成功" in rows["rel"]["message"]


def test_rel_first_step_without_job_id_is_400(monkeypatch):
    """首步 rel 且无 job_id：真实服务在入口拒绝，400 invalid_request、不登记。"""
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    with _client() as client:
        response = _post(client, ["rel"])
    assert response.status_code == 400
    assert "JSON 产物未就绪" in response.json()["message"]


def test_rel_dispatched_to_service(monkeypatch):
    """编排层把 rel 路由到 run_rel_step（§6.1）。"""
    captured = {}

    def fake_rel(_config, _source, active_job=None):
        captured["active_job"] = active_job
        return {
            "success": True,
            "message": "受管 Rel 关系发现完成（2 条关系）",
            "job_id": str(JOB_ID),
        }

    monkeypatch.setattr(collection_run, "run_rel_step", fake_rel)
    result = collection_run.start_db_collection(SOURCE_ID, ["rel"], UUID(JOB_ID))
    assert result["job_id"] == JOB_ID
    assert captured["active_job"] == UUID(JOB_ID)
    assert result["results"][0]["step"] == "rel"
    assert result["results"][0]["status"] == "success"


def test_rel_preparation_error_first_step_is_rel_preparation_503(monkeypatch):
    """RelPreparationError 在 results 为空时整体 503 rel_preparation_error（§9）。"""
    from backend.data_sources.rel_step_service import RelPreparationError

    def boom(_config, _source, active_job=None):
        raise RelPreparationError("Rel 准备阶段故障（快照构造：PermissionError）")

    monkeypatch.setattr(collection_run, "run_rel_step", boom)
    monkeypatch.setattr(
        collection_run, "get_registered_source", lambda *_a: {"source_id": "x"}
    )
    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs",
            json={"steps": ["rel"], "job_id": JOB_ID},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "rel_preparation_error"
