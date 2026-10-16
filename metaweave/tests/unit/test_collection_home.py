"""采集首页：按作业汇总状态，成功只计最近 30 天，失败不限窗口。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.data_sources.registration_service import SourceNotFoundError
from backend.jobs.home import CollectionHomeStep, assemble_collection_home, get_collection_home
from backend.persistence.platform_db import PlatformSettings

JOB_RUNNING = UUID("11111111-1111-4111-8111-111111111111")
JOB_SUCCESS = UUID("22222222-2222-4222-8222-222222222222")
JOB_OLD_SUCCESS = UUID("33333333-3333-4333-8333-333333333333")
JOB_FAILED = UUID("44444444-4444-4444-8444-444444444444")
SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _step(job_id, step, status, created, updated=None):
    created_at = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
    updated_at = (
        created_at
        if updated is None
        else datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)
    )
    return CollectionHomeStep(
        job_id=job_id,
        step_name=step,
        last_status=status,
        created_at=created_at,
        updated_at=updated_at,
    )


def test_rollup_running_beats_failed_and_success_is_recent_only():
    boundary = (NOW - timedelta(days=30)).replace(tzinfo=None).isoformat()
    older = (NOW - timedelta(days=30, seconds=1)).replace(tzinfo=None).isoformat()
    result = assemble_collection_home(
        [
            _step(JOB_RUNNING, "db", "running", "2026-09-01T00:00:00", "2026-09-28T00:00:00"),
            _step(JOB_RUNNING, "json", "failed", "2026-09-01T01:00:00", "2026-09-20T00:00:00"),
            _step(JOB_SUCCESS, "json", "success", "2026-09-02T01:00:00"),
            _step(JOB_SUCCESS, "db", "success", "2026-09-02T00:00:00", boundary),
            _step(JOB_OLD_SUCCESS, "db", "success", "2026-01-01T00:00:00", older),
            _step(JOB_FAILED, "db", "failed", "2026-01-02T00:00:00", "2026-01-02T00:00:00"),
        ],
        now=NOW,
    )

    by_job = {item["job_id"]: item for item in result["items"]}
    assert by_job[str(JOB_RUNNING)]["status"] == "running"
    assert by_job[str(JOB_RUNNING)]["steps"] == ["db", "json"]
    assert by_job[str(JOB_RUNNING)]["publish_id"] is None
    assert by_job[str(JOB_SUCCESS)]["status"] == "success"
    assert by_job[str(JOB_SUCCESS)]["steps"] == ["db", "json"]
    assert result["counts"] == {"running": 1, "success_recent": 1, "failed": 1}
    assert "running" in result["counts"]
    assert by_job[str(JOB_OLD_SUCCESS)]["status"] == "success"
    assert by_job[str(JOB_FAILED)]["status"] == "failed"


def test_success_on_30_day_boundary_counts_and_older_does_not():
    on_edge = NOW - timedelta(days=30)
    just_outside = NOW - timedelta(days=30, seconds=1)
    result = assemble_collection_home(
        [
            _step(
                JOB_SUCCESS,
                "db",
                "success",
                "2026-08-01T00:00:00",
                on_edge.replace(tzinfo=None).isoformat(),
            ),
            _step(
                JOB_OLD_SUCCESS,
                "db",
                "success",
                "2026-01-01T00:00:00",
                just_outside.replace(tzinfo=None).isoformat(),
            ),
        ],
        now=NOW,
    )
    assert result["counts"]["success_recent"] == 1
    assert result["counts"]["failed"] == 0


def test_failed_outside_30_days_still_counts():
    result = assemble_collection_home(
        [
            _step(JOB_FAILED, "db", "failed", "2025-01-01T00:00:00", "2025-01-01T00:00:00"),
        ],
        now=NOW,
    )
    assert result["counts"]["failed"] == 1
    assert result["counts"]["success_recent"] == 0
    assert result["counts"]["running"] == 0


def test_missing_source_raises(monkeypatch):
    class Session:
        def get(self, _model, _key):
            return None

        def scalars(self, _stmt):
            return []

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.jobs.home.platform_session", fake_session)
    with pytest.raises(SourceNotFoundError):
        get_collection_home(
            PlatformSettings(host="h", port=1, name="n", user="u", password="p"),
            SOURCE_ID,
            now=NOW,
        )


def test_api_missing_source_is_404(monkeypatch):
    monkeypatch.setattr(
        "backend.jobs.home.get_db_source",
        lambda _session, _source_id: None,
    )

    @contextmanager
    def fake_session(_settings):
        yield SimpleNamespace()

    monkeypatch.setattr("backend.jobs.home.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with TestClient(create_app(settings, cors_origins=[]), raise_server_exceptions=False) as client:
        response = client.get(f"/api/data-sources/{SOURCE_ID}/collection-home")
    assert response.status_code == 404
    assert response.json()["message"] == "数据源不存在"
