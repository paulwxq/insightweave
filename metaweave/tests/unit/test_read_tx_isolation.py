"""读事务隔离级别实测（doc 19.2 §8.2/§8.3）：SHOW 断言，不依赖调用顺序。"""

import os

import pytest
from sqlalchemy import text

from backend.assets.read_tx import begin_readonly_repeatable_read
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None or os.environ.get("METAWEAVE_RUN_LIVE_TESTS") != "1",
    reason="缺少 PLATFORM_DB_* 环境变量，跳过平台库集成断言",
)


def test_readonly_repeatable_read_actually_in_effect():
    """连接特性必须真实生效：同一事务内 SHOW 两个开关。"""
    with platform_session(SETTINGS) as session:
        begin_readonly_repeatable_read(session)
        isolation = session.execute(text("SHOW transaction_isolation")).scalar_one()
        read_only = session.execute(text("SHOW transaction_read_only")).scalar_one()
        assert isolation == "repeatable read"
        assert read_only == "on"


def test_readonly_transaction_rejects_writes():
    """READ ONLY 事务中的写请求应被数据库拒绝（防配置只停留在调用顺序）。"""
    from sqlalchemy.exc import SQLAlchemyError

    with platform_session(SETTINGS) as session:
        begin_readonly_repeatable_read(session)
        with pytest.raises(SQLAlchemyError):
            session.execute(text("SELECT 1")).scalar_one()  # 建立事务
            session.execute(text("CREATE TEMP TABLE _readonly_probe (id integer)"))
