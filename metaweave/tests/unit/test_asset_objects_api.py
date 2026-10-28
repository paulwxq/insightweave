"""对象读取 API 四路由与受控错误映射（doc 19.2 §8.1.1、新模型契约）。

响应契约为 publish_run_id 口径（json_publish/ddl_publish/字段行）；source_id
为字符串短 ID。

NOTE（源码现状，见交付报告）：ProfileInconsistentError / ObjectNotFoundError /
DefinitionInconsistentError / DefinitionInvalidError 在 errors.py 中已无专属
handler，经通用 Exception handler 返回 500 internal_error——对象不在当前
发布中（正常客户端场景）与解析失败不再区分 404/409。本文件按现行为断言，
恢复专属 handler 后应改回 404/409 断言。
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.assets.ddl_display import (
    DefinitionInconsistentError,
    DefinitionInvalidError,
)
from backend.assets.object_detail import ObjectNotFoundError, ProfileInconsistentError
from backend.data_sources.registration_service import (
    PlatformUnavailableError,
    SourceNotFoundError,
)
from backend.persistence.platform_db import PlatformSettings

SOURCE_ID = "db_src_1"
JSON_RUN = str(UUID("63221a9e-6a82-4311-a08c-9f74001da835"))
DDL_RUN = str(UUID("84c017f4-b438-452b-a888-715b7f0cbea2"))
SETTINGS = PlatformSettings(
    host="platform", port=5432, name="metaweave", user="admin", password="secret"
)


@pytest.fixture
def client(monkeypatch):
    from backend.api.routers import asset_objects

    calls: dict[str, Any] = {}

    def _record(name, value):
        calls[name] = value
        return value

    monkeypatch.setattr(
        asset_objects,
        "list_objects",
        lambda settings, source_id, **kw: _record(
            "list_objects",
            {
                "state": "ready",
                "source_id": str(source_id),
                "json_publish": {
                    "publish_run_id": JSON_RUN,
                    "published_at": "2026-10-03T04:00:00.000000Z",
                },
                "total": 1,
                "page": kw.get("page", 1),
                "page_size": kw.get("page_size", 50),
                "items": [
                    {
                        "schema_name": "public",
                        "object_name": "orders",
                        "object_type": "table",
                        "object_comment": "订单表",
                        "table_category": "fact",
                        "column_count": 6,
                        "total_rows": 500,
                        "primary_key_state": "present",
                        "generated_timestamp": "2026-10-03T18:55:55.000000Z",
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        asset_objects,
        "list_object_options",
        lambda settings, source_id, **kw: _record(
            "list_object_options",
            {
                "state": "ready",
                "source_id": str(source_id),
                "json_publish": None,
                "object_types": ["table"],
                "schemas": ["public"],
                "objects": [
                    {
                        "schema_name": "public",
                        "object_name": "orders",
                        "object_type": "table",
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        asset_objects,
        "get_object_detail",
        lambda settings, source_id, schema_name, object_name: _record(
            "get_object_detail",
            {
                "state": "ready",
                "source_id": str(source_id),
                "schema_name": schema_name,
                "object_name": object_name,
                "json_publish": {
                    "publish_run_id": JSON_RUN,
                    "published_at": "2026-10-03T04:00:00.000000Z",
                },
                "ddl_state": "not_published",
                "ddl_publish": None,
                "profile": {
                    "source_id": SOURCE_ID,
                    "schema_name": "public",
                    "object_name": "orders",
                    "db_name": "orders",
                    "object_type": "table",
                    "publish_run_id": JSON_RUN,
                    "metadata_version": "3.0",
                    "generated_timestamp": "2026-10-03T18:55:55.000000Z",
                    "loaded_at": "2026-10-03T04:00:05.000000Z",
                    "object_comment": "订单表",
                    "object_comment_source": "database",
                    "total_rows": 500,
                    "column_count": 6,
                    "sample_method": "limit",
                    "sample_count": 500,
                    "has_table_profile": True,
                    "table_category": "fact",
                    "category_confidence": 0.95,
                    "classification_source": "llm",
                    "classification_reason": "包含度量值",
                    "classification_basis": ["llm_inferred"],
                    "rule_table_category": "fact",
                    "rule_confidence": 0.85,
                    "rule_inference_basis": ["fact_has_metric"],
                    "primary_key_name": "orders_pkey",
                    "primary_key_columns": ["order_id"],
                    "foreign_key_count": 1,
                    "index_count": 1,
                    "constraint_count": 2,
                    "foreign_keys": [],
                    "unique_constraints": [],
                    "indexes": [],
                    "unique_column_sets": [],
                },
                "columns": [
                    {
                        "source_id": SOURCE_ID,
                        "schema_name": "public",
                        "object_name": "orders",
                        "publish_run_id": JSON_RUN,
                        "column_name": "order_id",
                        "ordinal_position": 1,
                        "data_type": "integer",
                        "is_nullable": False,
                        "column_default": None,
                        "column_comment": "订单ID",
                        "comment_source": "database",
                        "semantic_role": "identifier",
                        "semantic_confidence": 0.95,
                        "semantic_basis": ["type_whitelist_passed"],
                        "null_count": 0,
                        "null_rate": 0.0,
                        "unique_count": 500,
                        "uniqueness": 1.0,
                        "sample_min": "1",
                        "sample_max": "500",
                        "physical_constraints": {
                            "primary_key_state": "none",
                            "primary_key": None,
                            "foreign_keys": [],
                            "unique_constraints": [],
                        },
                        "indexes": [],
                        "unique_column_sets": [],
                    }
                ],
            },
        ),
    )
    monkeypatch.setattr(
        asset_objects,
        "get_object_ddl",
        lambda settings, source_id, schema_name, object_name: _record(
            "get_object_ddl",
            {
                "state": "ready",
                "json_publish_run_id": JSON_RUN,
                "ddl_publish": {
                    "publish_run_id": DDL_RUN,
                    "loaded_at": "2026-10-03T05:00:00.000000Z",
                },
                "definition_sql": "CREATE TABLE public.orders (order_id integer);",
                "sample_state": "ready",
                "sample_columns": ["user_id", "user_name"],
                "sample_records": [{"user_id": "1", "user_name": "用户001"}],
            },
        ),
    )
    monkeypatch.setattr(
        "backend.api.routers.data_sources.get_platform_settings",
        lambda: SETTINGS,
    )
    with TestClient(
        create_app(SETTINGS, cors_origins=[]), raise_server_exceptions=False
    ) as test_client:
        yield test_client, calls, asset_objects


def test_objects_passes_filters_and_returns_contract(client):
    http, calls, _ = client
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/objects",
        params={
            "page": 2,
            "page_size": 20,
            "object_type": "table",
            "schema_name": "public",
            "keyword": "ORD",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready" and body["total"] == 1
    item = body["items"][0]
    assert item["object_name"] == "orders" and item["primary_key_state"] == "present"
    assert body["json_publish"]["publish_run_id"] == JSON_RUN
    assert body["json_publish"]["published_at"] == "2026-10-03T04:00:00.000000Z"
    # 过滤参数透传到服务层
    assert calls["list_objects"]["page"] == 2 and calls["list_objects"]["page_size"] == 20


def test_options_endpoint_returns_contract(client):
    http, _, _ = client
    response = http.get(f"/api/data-assets/{SOURCE_ID}/object-options")
    assert response.status_code == 200
    body = response.json()
    assert (
        body["object_types"] == ["table"]
        and body["objects"][0]["object_name"] == "orders"
    )


def test_detail_response_matches_schema_example(client):
    http, _, _ = client
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-detail",
        params={"schema_name": "public", "object_name": "orders"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["ddl_state"] == "not_published" and body["ddl_publish"] is None
    assert body["profile"]["column_count"] == 6
    column = body["columns"][0]
    # §8.1.1：字段行含 §6 标量（身份四列 + 属性），仅排除 value_distribution
    assert column["source_id"] == SOURCE_ID
    assert column["publish_run_id"] == JSON_RUN
    assert column["schema_name"] == "public" and column["object_name"] == "orders"
    assert column["column_name"] == "order_id"
    assert column["physical_constraints"]["primary_key_state"] == "none"
    assert "value_distribution" not in column


def test_object_ddl_ready_response_matches_schema_example(client):
    http, _, _ = client
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-ddl",
        params={"schema_name": "public", "object_name": "orders"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["sample_state"] == "ready"
    assert body["sample_records"] == [{"user_id": "1", "user_name": "用户001"}]
    assert body["json_publish_run_id"] == JSON_RUN
    assert body["ddl_publish"]["publish_run_id"] == DDL_RUN


def test_detail_profile_inconsistent_is_409(client, monkeypatch):
    """画像不一致 → 409 profile_inconsistent（doc 19.2 §8）。"""
    http, _, module = client

    def boom(*args, **kwargs):
        raise ProfileInconsistentError("不一致")

    monkeypatch.setattr(module, "get_object_detail", boom)
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-detail",
        params={"schema_name": "public", "object_name": "orders"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "profile_inconsistent"


def test_object_ddl_definition_inconsistent_is_409(client, monkeypatch):
    http, _, module = client

    def boom(*args, **kwargs):
        raise DefinitionInconsistentError("不一致")

    monkeypatch.setattr(module, "get_object_ddl", boom)
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-ddl",
        params={"schema_name": "public", "object_name": "orders"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "definition_inconsistent"


def test_object_ddl_definition_invalid_409_without_content_leak(
    client, monkeypatch
):
    http, _, module = client

    def boom(*args, **kwargs):
        raise DefinitionInvalidError("LEAK-DDL-BODY-9527")

    monkeypatch.setattr(module, "get_object_ddl", boom)
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-ddl",
        params={"schema_name": "public", "object_name": "orders"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "definition_invalid"
    assert "LEAK-DDL-BODY-9527" not in response.text


def test_missing_object_is_404(client, monkeypatch):
    """对象不在当前 JSON 发布 → 404 not_found（doc 19.2 §8）。"""
    http, _, module = client

    def boom(*args, **kwargs):
        raise ObjectNotFoundError("对象不存在")

    monkeypatch.setattr(module, "get_object_detail", boom)
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/object-detail",
        params={"schema_name": "public", "object_name": "missing"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_source_missing_404(client, monkeypatch):
    http, _, module = client

    def boom(*args, **kwargs):
        raise SourceNotFoundError("数据源不存在")

    monkeypatch.setattr(module, "list_objects", boom)
    response = http.get(f"/api/data-assets/{SOURCE_ID}/objects")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_platform_503(client, monkeypatch):
    http, _, module = client

    def boom(*args, **kwargs):
        raise PlatformUnavailableError("平台库暂不可用")

    monkeypatch.setattr(module, "list_objects", boom)
    response = http.get(f"/api/data-assets/{SOURCE_ID}/objects")
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"


def test_query_validation_422(client):
    http, _, _ = client
    response = http.get(f"/api/data-assets/{SOURCE_ID}/object-detail")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


def test_page_size_over_limit_422(client):
    http, _, _ = client
    response = http.get(
        f"/api/data-assets/{SOURCE_ID}/objects", params={"page_size": 101}
    )
    assert response.status_code == 422
