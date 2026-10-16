"""独立平台 PostgreSQL 的连接设置和短生命周期会话。"""

import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from metaweave.db.postgres_options import normalize_sslmode
from metaweave.utils.file_utils import get_project_root


@dataclass(frozen=True)
class PlatformSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)
    sslmode: str | None = None


def load_project_env() -> None:
    """供不经过主配置加载的开发期登记脚本使用。"""
    load_dotenv(get_project_root() / ".env", override=False)


def load_platform_settings_from_env() -> PlatformSettings:
    required = ("PLATFORM_DB_HOST", "PLATFORM_DB_USER", "PLATFORM_DB_PASSWORD")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ValueError(f"缺少平台库环境变量: {', '.join(missing)}")
    try:
        port = int(os.getenv("PLATFORM_DB_PORT", "5432"))
    except ValueError as exc:
        raise ValueError("PLATFORM_DB_PORT 必须是 1–65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("PLATFORM_DB_PORT 必须是 1–65535 的整数")
    return PlatformSettings(
        host=os.environ["PLATFORM_DB_HOST"],
        port=port,
        name=os.getenv("PLATFORM_DB_NAME") or "metaweave",
        user=os.environ["PLATFORM_DB_USER"],
        password=os.environ["PLATFORM_DB_PASSWORD"],
        # doc 18.1 §4.1：与业务库复用同一校验函数，非法值在创建引擎前失败。
        sslmode=normalize_sslmode(os.getenv("PLATFORM_DB_SSLMODE")),
    )


@contextmanager
def platform_session(settings: PlatformSettings):
    """不在 URL/日志中直接拼接密码；离开上下文即关闭会话和连接池。"""
    url = URL.create(
        "postgresql+psycopg",
        username=settings.user,
        password=settings.password,
        host=settings.host,
        port=settings.port,
        database=settings.name,
    )
    connect_args = {"connect_timeout": 10}
    # doc 18.1 §4.1：入口再校验——直接构造的 PlatformSettings 也逃不过，
    # 非法 sslmode 在创建引擎前失败。
    sslmode = normalize_sslmode(settings.sslmode)
    if sslmode is not None:
        connect_args["sslmode"] = sslmode
    engine = create_engine(url, connect_args=connect_args)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
