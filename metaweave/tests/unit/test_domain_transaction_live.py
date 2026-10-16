"""Domain 会话 2 两表事务的实库验证（doc 21.1 §11 批次 1 证据）。

沿用 `test_read_tx_isolation.py` 的 skipif 模式：缺少平台库环境变量时跳过。
用例自建临时源行与作业行、结束后清理，不改动既有数据：

- 一次 commit 同时提交步骤行与源描述（§5.2 固定顺序）；
- 真实失败分支（StepAlreadyRunningError）与真实 commit flush 失败
  （CheckConstraint 违反）时，两处修改一起回滚。
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.jobs.repository import (
    STATUS_RUNNING,
    StepAlreadyRunningError,
    get_step_row,
    start_step,
)
from backend.persistence.models import DbSourceInfo
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing.repository import lock_db_source_row


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过平台库事务实库断言"
)


@pytest.fixture
def live_workspace():
    """临时源行 + 新作业 id；结束时删除全部临时行。"""
    source_id = uuid4()
    job_id = uuid4()
    with platform_session(SETTINGS) as session:
        session.execute(
            text(
                """
                INSERT INTO public.db_source_info
                    (source_id, display_name, description, db_type, host, port,
                     db_name, username, password_ciphertext, selected_schemas, is_active)
                VALUES
                    (:sid, :name, '旧源描述', 'postgresql', 'tx-live-test.local', 6543,
                     :dbname, 'reader', 'cipher', ARRAY['public'], true)
                """
            ),
            {
                "sid": source_id,
                "name": f"tx-live-{source_id.hex[:8]}",
                "dbname": f"tx_live_{source_id.hex[:8]}",
            },
        )
        session.commit()
    try:
        yield source_id, job_id
    finally:
        with platform_session(SETTINGS) as session:
            session.execute(
                text("DELETE FROM public.job_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.execute(
                text("DELETE FROM public.db_source_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.commit()


def _read_back(source_id, job_id):
    """新连接核验（对照 psycopg3 会话关闭即回滚的实测语义）。"""
    with platform_session(SETTINGS) as session:
        source = session.get(DbSourceInfo, source_id)
        row = get_step_row(session, job_id, "domain")
        return source, row


def test_live_single_commit_persists_step_row_and_source_description(live_workspace):
    """§5.2 固定顺序：先改锁下源行描述，start_step 唯一一次 commit 提交两处。"""
    source_id, job_id = live_workspace

    with platform_session(SETTINGS) as session:
        source_row = lock_db_source_row(session, source_id)
        assert source_row is not None
        source_row.description = "新的源描述"
        start_step(session, job_id, source_id, "domain", db_desc="新的作业描述")

    source, row = _read_back(source_id, job_id)
    assert source.description == "新的源描述"
    assert row is not None
    assert row.db_desc == "新的作业描述"
    assert row.last_status == STATUS_RUNNING


def test_live_rollback_on_step_already_running(live_workspace):
    """真实失败分支：start_step 抛错时挂起的源描述更新随会话关闭一起回滚。"""
    source_id, job_id = live_workspace

    with platform_session(SETTINGS) as session:
        start_step(session, job_id, source_id, "domain", db_desc="第一版")

    with pytest.raises(StepAlreadyRunningError):
        with platform_session(SETTINGS) as session:
            source_row = lock_db_source_row(session, source_id)
            source_row.description = "不应落库的描述"
            start_step(session, job_id, source_id, "domain", db_desc="不应落库")

    source, row = _read_back(source_id, job_id)
    assert source.description == "旧源描述"
    assert row.db_desc == "第一版"
    assert row.last_status == STATUS_RUNNING


def test_live_commit_failure_rolls_back_both_tables(live_workspace):
    """真实提交失败（CheckConstraint 在 start_step 的 commit flush 时触发）：
    job_info 新行与源描述更新一起回滚（§9「执行时同步的两表事务写入失败」）。"""
    source_id, job_id = live_workspace

    with pytest.raises(IntegrityError):
        with platform_session(SETTINGS) as session:
            source_row = lock_db_source_row(session, source_id)
            source_row.description = "不应落库的描述"
            source_row.port = 0  # 违反 port BETWEEN 1 AND 65535
            start_step(session, job_id, source_id, "domain", db_desc="不应落库")

    source, row = _read_back(source_id, job_id)
    assert source.description == "旧源描述"
    assert source.port == 6543
    assert row is None
