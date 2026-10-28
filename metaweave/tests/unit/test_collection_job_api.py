"""采集作业 API（新模型）：作业创建冒烟 + normalize_collection_steps 排序用例。

旧端点 POST /api/data-sources/{sid}/collection-jobs（创建即执行）已随四层模型
改造删除；其执行/阻断/部分结果语义由 test_run_request_and_params.py、
test_run_lifecycle_live.py、test_job_service_live.py 承接。本文件保留：
1. normalize_collection_steps 的白名单与同请求拓扑排序规则（算法未改写）；
2. 新端点 POST /api/workspaces/{wid}/collection-jobs 的创建冒烟
   （201、非法步骤 400 step_config_invalid、请求透传）。
"""

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.api.routers import workspaces as workspaces_router
from backend.jobs import collection_run, job_service
from backend.persistence.platform_db import PlatformSettings

WORKSPACE_ID = "ws_1"
SOURCE_ID = "db_src_1"


def _client():
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    )


# ---- normalize_collection_steps：白名单与同请求偏序拓扑排序 ----


def test_normalize_accepts_json_and_orders_ddl_first():
    assert collection_run.normalize_collection_steps(
        ["json", "ddl", "ddl", "json"]
    ) == [
        "ddl",
        "json",
    ]


def test_normalize_accepts_domain_and_orders_after_md():
    assert collection_run.normalize_collection_steps(
        ["domain", "md", "json", "ddl"]
    ) == ["ddl", "json", "md", "domain"]
    # §6.2：同请求未勾选 md 时 domain 保持传入位置（读既有 md 产物）
    assert collection_run.normalize_collection_steps(["domain", "ddl"]) == [
        "domain",
        "ddl",
    ]


def test_normalize_unknown_step_message_lists_whitelist():
    with pytest.raises(collection_run.UnsupportedCollectionStepError) as excinfo:
        collection_run.normalize_collection_steps(["db", "cql"])
    message = str(excinfo.value)
    assert "domain" in message and "rel" in message
    # dim_config 已随断代改造更名 dim
    assert "dim" in message and "dim_config" not in message


def test_normalize_rejects_empty_steps():
    with pytest.raises(collection_run.UnsupportedCollectionStepError, match="至少"):
        collection_run.normalize_collection_steps([])


def test_normalize_accepts_dim_and_orders_after_json():
    assert collection_run.normalize_collection_steps(["dim", "json"]) == [
        "json",
        "dim",
    ]
    # 审核修订 3：交错顺序——md 与 dim 无依赖边，保持传入相对序
    assert collection_run.normalize_collection_steps(["md", "dim", "json"]) == [
        "json",
        "md",
        "dim",
    ]
    # 审核修订 4：确定性反例——md 被 ddl→md 阻塞期间 dim 先行；
    # 锁定规则结果，验收以规则为准而非普遍相对序
    assert collection_run.normalize_collection_steps(["md", "dim", "ddl"]) == [
        "dim",
        "ddl",
        "md",
    ]
    # §6.2：同请求未勾选 json 时 dim 没有有效边，保持传入位置
    assert collection_run.normalize_collection_steps(["dim", "ddl"]) == [
        "dim",
        "ddl",
    ]


def test_normalize_accepts_rel_and_orders_after_json_and_domain():
    """§6.2：json→rel、domain→rel 偏序成立；无有效边时保持传入序。"""
    assert collection_run.normalize_collection_steps(
        ["rel", "json", "ddl", "md", "domain"]
    ) == ["ddl", "json", "md", "domain", "rel"]
    assert collection_run.normalize_collection_steps(["rel", "ddl"]) == ["rel", "ddl"]


def test_normalize_orders_sql_after_md_domain_rel():
    assert collection_run.normalize_collection_steps(
        ["sql", "rel", "domain", "md"]
    ) == ["md", "domain", "rel", "sql"]
    # 上游未选时 sql 无有效边，保持传入位置
    assert collection_run.normalize_collection_steps(["sql", "db"]) == [
        "sql",
        "db",
    ]


def test_normalize_preserves_db_ddl_request_order():
    """doc 18 §3 只授权 ddl 先于 json：db/ddl 的传入顺序保持不变。"""
    assert collection_run.normalize_collection_steps(["ddl", "db"]) == ["ddl", "db"]
    assert collection_run.normalize_collection_steps(["db", "ddl"]) == ["db", "ddl"]


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


# ---- 新端点冒烟：POST /api/workspaces/{wid}/collection-jobs ----


def _job_item(**overrides):
    values = {
        "job_id": "coll_job_1",
        "workspace_id": WORKSPACE_ID,
        "job_name": "日常采集",
        "job_desc": None,
        "created_at": "2026-10-22T00:00:00.000000Z",
        "updated_at": "2026-10-22T00:00:00.000000Z",
        "job_type": "collection",
        "latest_revision_no": 1,
        "revision": {
            "job_id": "coll_job_1",
            "revision_no": 1,
            "steps": ["db", "ddl"],
            "step_config": None,
            "change_note": None,
            "created_at": "2026-10-22T00:00:00.000000Z",
        },
    }
    values.update(overrides)
    return values


def test_create_job_returns_201_and_passes_payload(monkeypatch):
    seen = {}

    def fake_create(settings, kind, workspace_id, name, desc, payload):
        seen.update(
            kind=kind,
            workspace_id=workspace_id,
            name=name,
            desc=desc,
            steps=payload.steps,
            step_config=payload.step_config,
            change_note=payload.change_note,
        )
        return _job_item(job_name=name, job_desc=desc)

    monkeypatch.setattr(workspaces_router, "create_job", fake_create)
    with _client() as client:
        response = client.post(
            f"/api/workspaces/{WORKSPACE_ID}/collection-jobs",
            json={
                "job_name": "日常采集",
                "job_desc": None,
                "config": {
                    "steps": ["db", "ddl"],
                    "step_config": {"domain": {"description": " 订单 "}},
                    "change_note": "初始版本",
                },
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_id"] == "coll_job_1"
    assert body["job_type"] == "collection"
    assert body["revision"]["revision_no"] == 1
    assert seen["kind"] == "collection"
    assert seen["workspace_id"] == WORKSPACE_ID
    assert seen["steps"] == ["db", "ddl"]
    assert seen["change_note"] == "初始版本"


def test_create_job_unknown_step_is_400_step_config_invalid(monkeypatch):
    """非法步骤经真实 job_service 校验 → 400 step_config_invalid，不触库。"""
    from contextlib import contextmanager

    @contextmanager
    def no_db_session(_settings):
        raise AssertionError("校验失败不应触库")
        yield  # pragma: no cover

    monkeypatch.setattr(job_service, "platform_session", no_db_session)
    monkeypatch.setattr(workspaces_router, "create_job", job_service.create_job)
    with _client() as client:
        response = client.post(
            f"/api/workspaces/{WORKSPACE_ID}/collection-jobs",
            json={
                "job_name": "采集",
                "config": {"steps": ["db", "cql"]},
            },
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "step_config_invalid"
    assert "未知步骤" in body["message"]
    assert "dim" in body["message"]  # 白名单文本含更名后的 dim


def test_create_job_unknown_param_is_400_step_config_invalid(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def no_db_session(_settings):
        raise AssertionError("校验失败不应触库")
        yield  # pragma: no cover

    monkeypatch.setattr(job_service, "platform_session", no_db_session)
    monkeypatch.setattr(workspaces_router, "create_job", job_service.create_job)
    with _client() as client:
        response = client.post(
            f"/api/workspaces/{WORKSPACE_ID}/collection-jobs",
            json={
                "job_name": "采集",
                "config": {
                    "steps": ["rel"],
                    "step_config": {"rel": {"threshold": 0.9}},
                },
            },
        )
    assert response.status_code == 400
    assert response.json()["code"] == "step_config_invalid"
    assert "不支持参数" in response.json()["message"]


def test_create_job_missing_config_is_422(monkeypatch):
    monkeypatch.setattr(
        workspaces_router,
        "create_job",
        lambda *_a, **_kw: pytest.fail("校验未通过却进入了服务层"),
    )
    with _client() as client:
        response = client.post(
            f"/api/workspaces/{WORKSPACE_ID}/collection-jobs",
            json={"job_name": "采集"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_create_publish_job_unknown_step_is_400(monkeypatch):
    """发布作业白名单独立（table_schema/dim_value 等，无 md/dim）。"""
    from contextlib import contextmanager

    @contextmanager
    def no_db_session(_settings):
        raise AssertionError("校验失败不应触库")
        yield  # pragma: no cover

    monkeypatch.setattr(job_service, "platform_session", no_db_session)
    monkeypatch.setattr(workspaces_router, "create_job", job_service.create_job)
    with _client() as client:
        response = client.post(
            f"/api/workspaces/{WORKSPACE_ID}/publish-jobs",
            json={"job_name": "发布", "config": {"steps": ["md"]}},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "step_config_invalid"


def test_legacy_immediate_execution_endpoint_is_gone():
    """旧「创建即执行」端点已删除：请求 404，且 data_sources 路由器不再含它。"""
    from backend.api.routers import data_sources as data_sources_router

    with _client() as client:
        response = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs", json={"steps": ["db"]}
        )
    assert response.status_code == 404
    data_source_paths = {
        getattr(route, "path", "") for route in data_sources_router.router.routes
    }
    assert not any("collection-jobs" in path for path in data_source_paths)
    # 新端点挂在 workspaces 路由器下
    from backend.api.routers.workspaces import router as workspaces_routes

    assert (
        "/api/workspaces/{workspace_id}/collection-jobs"
        in {getattr(route, "path", "") for route in workspaces_routes.routes}
    )
