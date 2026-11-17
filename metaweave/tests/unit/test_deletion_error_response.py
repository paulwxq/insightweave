"""doc 8.1 §7.3 错误载荷序列化：阻断运行的 UUID/datetime 必须可 JSON 化。

作业删除被未结束运行阻断时，payload 直接携带 ORM 原生类型；此前
_response 原样交给 JSONResponse → TypeError → 500。本文件锁定：
409、code 不变、ID/时间正确转换。只验证响应处理，不连库。
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.errors import register_exception_handlers
from backend.deletion.errors import ResourceHasUnfinishedRunsError

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
CREATED_AT = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.delete("/api/collection-jobs/{job_id}")
    def delete_job_route(job_id: str):
        raise ResourceHasUnfinishedRunsError(
            "该作业存在未结束运行，不能删除",
            payload={
                "blocking_runs": [
                    {
                        "type": "collection",
                        "run_id": RUN_ID,
                        "status": "pending",
                        "job_id": job_id,
                        "created_at": CREATED_AT,
                        "started_at": None,
                    }
                ]
            },
        )

    return TestClient(app, raise_server_exceptions=False)


def test_blocking_runs_payload_serializes_uuid_and_datetime_to_409():
    response = _client().delete("/api/collection-jobs/coll_job_1")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "resource_has_unfinished_runs"
    run = body["details"]["blocking_runs"][0]
    assert run["run_id"] == str(RUN_ID)
    assert run["status"] == "pending"
    assert run["created_at"] == "2026-10-29T08:00:00+00:00"
    assert run["started_at"] is None
