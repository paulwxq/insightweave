"""发布预检与发布端点的 HTTP 契约。业务调用真实发布服务。"""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.persistence.platform_db import PlatformSettings
from tests.unit.test_publish_service import (
    JOB_ID,
    LEAK_PARAM,
    LEAK_SQL,
    SOURCE_ID,
    FakePublishSession,
    assert_safe_primary_key_exception,
    install_session,
    integrity_error,
    write_profile,
)
from tests.unit.test_ddl_publish import (
    DEFAULT_FILES,
    _add_ddl_step,
    write_ddl,
)

MISSING = UUID("11111111-1111-4111-8111-111111111111")


def _client(*, raise_server_exceptions=False):
    settings = PlatformSettings(
        host="platform", port=5432, name="metaweave", user="admin", password="secret"
    )
    return TestClient(
        create_app(settings, cors_origins=[]),
        raise_server_exceptions=raise_server_exceptions,
    )


def _check(client, source_id=SOURCE_ID, job_id=JOB_ID):
    return client.get(
        f"/api/data-sources/{source_id}/collection-jobs/{job_id}/publish-check"
    )


def _post(client, steps, source_id=SOURCE_ID, job_id=JOB_ID):
    return client.post(
        f"/api/data-sources/{source_id}/collection-jobs/{job_id}/publications",
        json={"steps": steps},
    )


@pytest.fixture
def ready(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    return session


def test_check_ready_profile(ready):
    with _client() as client:
        response = _check(client)
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == str(SOURCE_ID)
    assert body["job_id"] == str(JOB_ID)
    step = body["steps"][0]
    assert step == {
        "step_name": "db",
        "job_status": "success",
        "output_ready": True,
        "publishable": True,
        "reason": None,
    }


def test_check_missing_db_row_is_not_404(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path, FakePublishSession())
    session.steps[0].step_name = "ddl"
    write_profile(tmp_path)
    with _client() as client:
        response = _check(client)
    assert response.status_code == 200
    assert response.json()["steps"][0]["reason"] == "没有执行记录"


def test_check_and_post_reject_missing_or_disabled(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.source = None
    with _client() as client:
        missing = _check(client)
        posted = _post(client, ["db"])
    assert missing.status_code == 404 and missing.json()["message"] == "数据源不存在"
    assert posted.status_code == 404
    session.source = FakePublishSession().source
    session.source.is_active = False
    with _client() as client:
        disabled = _post(client, ["db"])
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "source_disabled"
    assert session.publishes == [] and session.profiles == []


def test_post_rejects_bad_steps_and_shape(ready):
    with _client() as client:
        empty = _post(client, [])
        unknown = _post(client, ["md"])
        extra = client.post(
            f"/api/data-sources/{SOURCE_ID}/collection-jobs/{JOB_ID}/publications",
            json={"steps": ["db"], "clean": True},
        )
        bad_id = client.post(
            "/api/data-sources/not-a-uuid/collection-jobs/" f"{JOB_ID}/publications",
            json={"steps": ["db"]},
        )
    assert empty.status_code == 400 and empty.json()["code"] == "step_not_supported"
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "step_not_supported"
    assert extra.status_code == 422 and extra.json()["code"] == "invalid_request"
    assert bad_id.status_code == 422
    assert ready.publishes == []


@pytest.mark.parametrize("status", ["failed", "running"])
def test_post_selected_step_failed_or_running_is_job_not_publishable(ready, status):
    """所选步骤自身 failed/running 时整个 Job 不可发布：409，而非 400（doc 20.1 §11.2）。"""
    ready.steps[0].last_status = status
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 409
    assert response.json()["code"] == "job_not_publishable"
    assert ready.publishes == []


def test_post_selected_step_without_record_is_step_not_ready(monkeypatch, tmp_path):
    """作业整体成功但所选步骤没有执行记录：仍是步骤级未就绪（400）。"""
    session = install_session(monkeypatch, tmp_path)
    write_profile(tmp_path)
    session.steps[0].step_name = "ddl"  # 作业只有成功的 ddl 行，没有 db 行
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 400
    assert response.json()["code"] == "step_not_ready"
    assert response.json()["message"] == "步骤尚未采集成功"
    assert session.publishes == []


def test_post_success_and_duplicate_db(ready):
    with _client() as client:
        response = _post(client, ["db", "db"])
    assert response.status_code == 200
    body = response.json()
    assert UUID(body["publish_id"])
    assert len(body["results"]) == 1
    assert body["results"][0]["status"] == "success"
    assert body["results"][0]["step"] == "db"
    assert len(ready.profiles) == 1
    assert ready.profiles[0].publish_id == ready.publishes[0].publish_id


def test_post_missing_file_is_recorded_failure(ready, tmp_path):
    (
        tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db" / "database.json"
    ).unlink()
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "failed"
    assert response.json()["results"][0]["message"] == "画像输入不可用或校验失败"
    assert ready.profiles == []


def test_post_current_conflict_is_409(ready):
    ready.commit_errors.append(integrity_error("23505", "publish_info_current_idx"))
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 409
    assert response.json()["code"] == "publish_conflict"
    assert response.json()["message"] == "发布发生并发冲突，请重新发布"
    assert LEAK_SQL not in response.text
    assert ready.publishes == [] and ready.profiles == []


def test_post_primary_key_conflict_is_500(ready):
    ready.commit_errors.append(integrity_error("23505", "publish_info_pkey"))
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["message"] == "服务暂时无法处理请求"
    assert LEAK_SQL not in response.text
    assert LEAK_PARAM not in response.text
    assert ready.publishes == [] and ready.profiles == []

    ready.commit_errors.append(integrity_error("23505", "db_profile_pkey"))
    with pytest.raises(RuntimeError) as caught:
        with _client(raise_server_exceptions=True) as client:
            _post(client, ["db"])
    assert_safe_primary_key_exception(caught.value)
    assert ready.publishes == [] and ready.profiles == []


def test_post_failure_record_unavailable_is_503(ready, tmp_path):
    (
        tmp_path / "output" / str(SOURCE_ID) / str(JOB_ID) / "db" / "database.json"
    ).unlink()
    ready.commit_errors.append(RuntimeError("DRIVER-SECRET-XYZ"))
    with _client() as client:
        response = _post(client, ["db"])
    assert response.status_code == 503
    assert response.json()["code"] == "platform_unavailable"
    assert "DRIVER-SECRET" not in response.text
    assert ready.publishes == []


def test_unknown_job_is_404(ready):
    with _client() as client:
        response = _check(client, job_id=MISSING)
    assert response.status_code == 404
    assert response.json()["message"] == "作业不存在或不属于该数据源"


@pytest.fixture
def ddl_ready(monkeypatch, tmp_path):
    session = install_session(monkeypatch, tmp_path)
    _add_ddl_step(session)
    write_ddl(tmp_path, DEFAULT_FILES)
    write_profile(tmp_path)
    return session


def test_check_reports_db_and_ddl_steps(ddl_ready):
    with _client() as client:
        response = _check(client)
    assert response.status_code == 200
    steps = response.json()["steps"]
    assert [step["step_name"] for step in steps] == [
        "db",
        "ddl",
        "json",
        "domain",
        "rel",
        "table_schema",
        "dim_value",
        "sql",
    ]
    by_name = {step["step_name"]: step for step in steps}
    assert by_name["db"]["publishable"] and by_name["ddl"]["publishable"]
    # json 未采集：不可发布，原因落在产物/状态上（doc 19.2 §7.2）
    assert by_name["json"]["publishable"] is False


def test_post_ddl_publishes_definitions(ddl_ready):
    with _client() as client:
        response = _post(client, ["ddl"])
    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["step"] == "ddl"
    assert body["results"][0]["status"] == "success"
    assert len(ddl_ready.definitions) == len(DEFAULT_FILES)
    assert all(
        row.publish_id == UUID(body["publish_id"]) for row in ddl_ready.definitions
    )
    assert ddl_ready.publishes[0].step_name == "ddl"


def test_post_db_and_ddl_in_one_call(ddl_ready):
    with _client() as client:
        response = _post(client, ["db", "ddl"])
    assert response.status_code == 200
    body = response.json()
    assert [row["step"] for row in body["results"]] == ["db", "ddl"]
    assert all(row["status"] == "success" for row in body["results"])
    publish_id = UUID(body["publish_id"])
    assert ddl_ready.profiles[0].publish_id == publish_id
    assert all(row.publish_id == publish_id for row in ddl_ready.definitions)
