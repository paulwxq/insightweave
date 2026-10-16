"""发布作业列表：按 publish_id 聚合，发布时间取 created_at 最大值。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.persistence.platform_db import PlatformSettings
from backend.publishing.listing import (
    PublishStepRecord,
    assemble_publish_jobs,
    get_publish_definition,
    list_publish_jobs,
)

SOURCE_ID = UUID("b4a87275-7c37-4feb-bf1c-e5a8446c174f")
OTHER_SOURCE_ID = UUID("11111111-1111-4111-8111-111111111111")
PUBLISH_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PUBLISH_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
JOB_A = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
JOB_B = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
USER_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def _row(**overrides):
    values = {
        "publish_id": PUBLISH_A,
        "job_id": JOB_A,
        "step_name": "db",
        "created_at": datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc),
        "created_by_user_id": None,
    }
    values.update(overrides)
    return PublishStepRecord(**values)


def test_assemble_groups_steps_and_sorts_by_latest_created_at():
    items = assemble_publish_jobs(
        [
            _row(
                step_name="json",
                created_at=datetime(2026, 9, 27, 1, 2, tzinfo=timezone.utc),
            ),
            _row(
                step_name="db",
                created_at=datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc),
                created_by_user_id=USER_ID,
            ),
            _row(
                step_name="rel",
                created_at=datetime(2026, 9, 27, 1, 1, tzinfo=timezone.utc),
            ),
            _row(
                publish_id=PUBLISH_B,
                job_id=JOB_B,
                step_name="ddl",
                created_at=datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    assert [item["publish_id"] for item in items] == [str(PUBLISH_B), str(PUBLISH_A)]
    older = items[1]
    assert older["job_id"] == str(JOB_A)
    assert older["published_at"] == "2026-09-27T01:02:00.000000Z"
    assert older["profile_steps"] == ["db", "json"]
    assert older["rag_steps"] == ["rel"]
    assert older["created_by_user_id"] == str(USER_ID)
    assert items[0]["profile_steps"] == ["ddl"]
    assert items[0]["rag_steps"] == []
    assert items[0]["created_by_user_id"] is None


def test_assemble_empty_is_empty():
    assert assemble_publish_jobs([]) == []


def test_list_publish_jobs_reads_source_rows(monkeypatch):
    source = SimpleNamespace(
        source_id=SOURCE_ID,
        display_name="订单库pg",
        host="172.29.128.1",
        port=5432,
        db_name="orders",
        db_type="postgresql",
        db_version="18.1",
        description="包含订单、影院等数据。",
        is_active=True,
    )
    rows = [
        SimpleNamespace(
            publish_id=PUBLISH_A,
            job_id=JOB_A,
            step_name="db",
            created_at=datetime(2026, 9, 27, 23, 48, 35, tzinfo=timezone.utc),
            created_by_user_id=None,
        )
    ]

    class Session:
        def get(self, _model, key):
            return source if key == SOURCE_ID else None

        def scalars(self, _stmt):
            return rows

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.publishing.listing.platform_session", fake_session)
    result = list_publish_jobs(
        PlatformSettings(host="h", port=1, name="n", user="u", password="p"),
        SOURCE_ID,
    )
    assert result["source"]["display_name"] == "订单库pg"
    assert result["source"]["db_type_version"] == "postgresql 18.1"
    assert result["job_count"] == 1
    assert result["items"][0]["published_at"] == "2026-09-27T23:48:35.000000Z"
    assert result["items"][0]["profile_steps"] == ["db"]


def test_get_publish_definition_returns_stored_steps(monkeypatch):
    source = SimpleNamespace(source_id=SOURCE_ID)
    rows = [
        SimpleNamespace(
            publish_id=PUBLISH_A,
            source_id=SOURCE_ID,
            job_id=JOB_A,
            step_name="json",
            created_at=datetime(2026, 9, 27, 1, 2, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            publish_id=PUBLISH_A,
            source_id=SOURCE_ID,
            job_id=JOB_A,
            step_name="db",
            created_at=datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc),
        ),
    ]

    class Session:
        def get(self, _model, key):
            return source if key == SOURCE_ID else None

        def scalars(self, _stmt):
            return rows

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.publishing.listing.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    result = get_publish_definition(settings, SOURCE_ID, PUBLISH_A)
    assert result == {
        "publish_id": str(PUBLISH_A),
        "job_id": str(JOB_A),
        "steps": ["db", "json"],
    }
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get(f"/api/data-sources/{SOURCE_ID}/publish-jobs/{PUBLISH_A}")
    assert response.status_code == 200
    assert response.json()["steps"] == ["db", "json"]


def test_unknown_publish_is_404(monkeypatch):
    class Session:
        def get(self, _model, key):
            return SimpleNamespace(source_id=SOURCE_ID) if key == SOURCE_ID else None

        def scalars(self, _stmt):
            return []

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.publishing.listing.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get(f"/api/data-sources/{SOURCE_ID}/publish-jobs/{PUBLISH_B}")
    assert response.status_code == 404
    assert response.json()["message"] == "发布不存在或不属于该数据源"


def test_missing_source_is_404(monkeypatch):
    class Session:
        def get(self, _model, _key):
            return None

        def scalars(self, _stmt):
            raise AssertionError("不存在的数据源不应查询发布记录")

    @contextmanager
    def fake_session(_settings):
        yield Session()

    monkeypatch.setattr("backend.publishing.listing.platform_session", fake_session)
    settings = PlatformSettings(host="h", port=1, name="n", user="u", password="p")
    with TestClient(
        create_app(settings, cors_origins=[]), raise_server_exceptions=False
    ) as client:
        response = client.get(f"/api/data-sources/{OTHER_SOURCE_ID}/publish-jobs")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_assemble_groups_domain_into_profile_steps_after_json():
    """domain 加入 PROFILE_STEPS：发布历史按画像步骤展示且排在 json 之后（doc 21.2 §5）。"""
    items = assemble_publish_jobs(
        [
            _row(
                step_name="domain",
                created_at=datetime(2026, 9, 27, 1, 3, tzinfo=timezone.utc),
            ),
            _row(
                step_name="json",
                created_at=datetime(2026, 9, 27, 1, 2, tzinfo=timezone.utc),
            ),
            _row(
                step_name="db",
                created_at=datetime(2026, 9, 27, 1, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    assert items[0]["profile_steps"] == ["db", "json", "domain"]
    assert items[0]["rag_steps"] == []
