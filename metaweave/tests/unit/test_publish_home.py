"""发布首页：当前成功步骤数，成功只计最近 30 天，失败不限窗口。"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.data_sources.registration_service import SourceNotFoundError
from backend.persistence.platform_db import PlatformSettings
from backend.publishing.home import PublishHomeStep, assemble_publish_home, get_publish_home

PUBLISH_CURRENT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PUBLISH_FAILED = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PUBLISH_OLD = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
JOB_A = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
JOB_B = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _step(publish_id, job_id, step, status, created, current=False):
    return PublishHomeStep(
        publish_id=publish_id,
        job_id=job_id,
        step_name=step,
        status=status,
        is_current_publish=current,
        created_at=datetime.fromisoformat(created).replace(tzinfo=timezone.utc),
    )


def test_current_counts_success_rows_only_and_items_keep_both_ids():
    on_edge = (NOW - timedelta(days=30)).replace(tzinfo=None).isoformat()
    older = (NOW - timedelta(days=40)).replace(tzinfo=None).isoformat()
    result = assemble_publish_home(
        [
            _step(PUBLISH_CURRENT, JOB_A, "db", "success", on_edge, current=True),
            _step(PUBLISH_CURRENT, JOB_A, "json", "success", "2026-09-28T00:00:00", current=False),
            _step(PUBLISH_FAILED, JOB_B, "db", "failed", "2026-09-01T00:00:00", current=True),
            _step(PUBLISH_OLD, JOB_B, "cql", "success", "2026-08-01T00:00:00", current=False),
            _step(PUBLISH_OLD, JOB_B, "db", "success", older, current=False),
        ],
        now=NOW,
    )

    assert "running" not in result["counts"]
    assert result["counts"]["current"] == 1
    assert result["counts"]["success_recent"] == 1
    assert result["counts"]["failed"] == 1
    current = next(item for item in result["items"] if item["publish_id"] == str(PUBLISH_CURRENT))
    assert current["job_id"] == str(JOB_A)
    assert current["status"] == "success"
    assert current["steps"] == ["db", "json"]
    old = next(item for item in result["items"] if item["publish_id"] == str(PUBLISH_OLD))
    assert old["steps"] == ["db", "cql"]
    assert old["job_id"] == str(JOB_B)


def test_failed_current_and_non_current_success_do_not_increase_current():
    result = assemble_publish_home(
        [
            _step(PUBLISH_FAILED, JOB_A, "db", "failed", "2026-09-01T00:00:00", current=True),
            _step(PUBLISH_OLD, JOB_B, "db", "success", "2026-09-20T00:00:00", current=False),
        ],
        now=NOW,
    )
    assert result["counts"]["current"] == 0
    assert result["counts"]["success_recent"] == 1


def test_failed_publish_outside_window_still_counts():
    result = assemble_publish_home(
        [_step(PUBLISH_FAILED, JOB_A, "db", "failed", "2025-01-01T00:00:00", current=False)],
        now=NOW,
    )
    assert result["counts"] == {"current": 0, "success_recent": 0, "failed": 1}


def test_missing_source_raises(monkeypatch):
    class Session:
        def get(self, _model, _key):
            return None

        def scalars(self, _stmt):
            return []

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.publishing.home.platform_session", fake_session)
    with pytest.raises(SourceNotFoundError):
        get_publish_home(
            PlatformSettings(host="h", port=1, name="n", user="u", password="p"),
            SOURCE_ID,
            now=NOW,
        )


def test_api_missing_source_is_404(monkeypatch):
    @contextmanager
    def fake_session(_settings):
        yield object()

    monkeypatch.setattr("backend.publishing.home.platform_session", fake_session)
    monkeypatch.setattr("backend.publishing.home.get_db_source", lambda _session, _source_id: None)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with TestClient(create_app(settings, cors_origins=[]), raise_server_exceptions=False) as client:
        response = client.get(f"/api/data-sources/{SOURCE_ID}/publish-home")
    assert response.status_code == 404
    assert response.json()["message"] == "数据源不存在"
