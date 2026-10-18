"""平台登记源解析（doc 4 §4.7/§5.3 的公共装配层）

从 MetaWeave 平台库 ``public.db_source_info`` 读取指定 source_id 的登记
信息并解密密码，供 CLI 会话与后续 Web 会话装配共用——使检索、图查询与
**业务库连接**保持同一来源（doc 4 §4.7：不得沿用 YAML 固定连接）。

- 平台库连接取环境变量 ``PLATFORM_DB_HOST/PORT/NAME/USER/PASSWORD``；
- 密码用 Fernet 解密（``SOURCE_CREDENTIAL_KEY``），与 MetaWeave
  ``backend/security/source_password.py`` 同一算法的两模块独立实现
  （与 keyspace 判定式相同的镜像模式，不建立跨项目依赖）；
- 仅读取，不写平台库；错误信息不含密码或密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

from src.utils.logger import get_module_logger

logger = get_module_logger("nl2sql.db_deepagent.source_registry")

_QUERY = (
    "SELECT source_id, db_type, host, port, db_name, username, "
    "password_ciphertext, sslmode, selected_schemas, is_active "
    "FROM public.db_source_info WHERE source_id = %s"
)


class SourceResolutionError(RuntimeError):
    """登记源解析失败（不存在/禁用/类型不符/平台库或密钥不可用）。"""


@dataclass(frozen=True)
class SourceRegistration:
    """一个登记源的连接与归属信息（密码已解密，仅在后端内存中传递）。"""

    source_id: str
    db_name: str
    host: str
    port: int
    username: str
    password: str
    sslmode: str | None
    selected_schemas: tuple[str, ...]
    is_active: bool


def _platform_conninfo() -> str:
    """从环境变量构造平台库连接串（值缺失即配置错误，不回显密码）。

    用 ``psycopg.conninfo.make_conninfo`` 组装——手工拼接会在密码/主机名
    含空格或引号时产生解析歧义（与业务库 ``_make_business_conninfo`` 同一
    构造方式）。
    """
    required = ("PLATFORM_DB_HOST", "PLATFORM_DB_USER", "PLATFORM_DB_PASSWORD")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SourceResolutionError(
            f"缺少平台库环境变量: {', '.join(missing)}（db_source_info 登记读取需要）"
        )
    from psycopg.conninfo import make_conninfo

    return make_conninfo(
        host=os.environ["PLATFORM_DB_HOST"],
        port=os.getenv("PLATFORM_DB_PORT", "5432"),
        dbname=os.getenv("PLATFORM_DB_NAME") or "metaweave",
        user=os.environ["PLATFORM_DB_USER"],
        password=os.environ["PLATFORM_DB_PASSWORD"],
        application_name="db_deepagent_source_registry",
    )


def _decrypt_password(ciphertext: str) -> str:
    """Fernet 解密登记密码（与 MetaWeave source_password.py 同一算法）。"""
    key = os.getenv("SOURCE_CREDENTIAL_KEY")
    if not key:
        raise SourceResolutionError(
            "缺少 SOURCE_CREDENTIAL_KEY：无法解密登记的源库密码"
        )
    try:
        from cryptography.fernet import Fernet

        return (
            Fernet(key.encode("ascii"))
            .decrypt(ciphertext.encode("ascii"))
            .decode("utf-8")
        )
    except Exception as exc:  # 密钥/密文问题统一报错，不回显内容
        raise SourceResolutionError(
            "登记的源库密码无法解密；请检查 SOURCE_CREDENTIAL_KEY 与登记记录"
        ) from exc


def resolve_source_registration(source_id: str | UUID) -> SourceRegistration:
    """按 source_id 读取并校验登记源（doc 4 §5.3 的后端解析）。

    Raises:
        SourceResolutionError: source_id 非法、登记不存在、已禁用、
            db_type 不受支持，或平台库/密钥不可用。
    """
    import psycopg

    try:
        normalized = str(UUID(str(source_id).strip()))
    except (ValueError, AttributeError):
        raise SourceResolutionError(f"source_id 不是合法 UUID: {source_id!r}") from None

    try:
        with psycopg.connect(_platform_conninfo(), connect_timeout=10) as conn:
            row = conn.execute(_QUERY, (normalized,)).fetchone()
    except psycopg.Error as exc:
        # 只报错误类型与 SQLSTATE，不回显异常文本（可能内嵌连接串/密码）
        sqlstate = getattr(exc, "sqlstate", None)
        raise SourceResolutionError(
            "读取平台库 db_source_info 失败: "
            f"{type(exc).__name__}" + (f"（SQLSTATE {sqlstate}）" if sqlstate else "")
        ) from exc

    if row is None:
        raise SourceResolutionError(f"source_id 未在平台库登记: {normalized}")
    (
        _sid,
        db_type,
        host,
        port,
        db_name,
        username,
        ciphertext,
        sslmode,
        selected_schemas,
        is_active,
    ) = row
    if not is_active:
        raise SourceResolutionError(f"登记源已禁用: {normalized}")
    if db_type != "postgresql":
        raise SourceResolutionError(
            f"登记源 db_type 不受支持: {db_type!r}（当前仅 postgresql）"
        )
    registration = SourceRegistration(
        source_id=normalized,
        db_name=db_name,
        host=host,
        port=int(port),
        username=username,
        password=_decrypt_password(ciphertext),
        sslmode=sslmode,
        selected_schemas=tuple(selected_schemas or ()),
        is_active=bool(is_active),
    )
    logger.info(
        "登记源已解析 source_id=%s db_name=%s host=%s port=%s schemas=%s",
        registration.source_id,
        registration.db_name,
        registration.host,
        registration.port,
        list(registration.selected_schemas),
    )
    return registration


def apply_registration_to_config(cfg, registration: SourceRegistration):
    """把登记源注入运行配置（build_services 之前调用，doc 4 §4.7）。

    - ``database/expected_database/table_key_prefix`` 三键统一取登记 db_name
      （``apply_source_runtime_config``，含键空间校验）；
    - ``business_db`` 连接五键（host/port/user/password/sslmode）取登记值
      ——业务库连接与会话绑定保持同一来源，同名库也不再有"检索 B 连接 A"
      的缺口；
    - ``allowed_schemas`` 取登记 selected_schemas：约束表键归一
      （normalize_table_id）、实时目录访问（get_columns）与连接
      search_path——登记范围之外的对象不进入查询链路。
    """
    from src.modules.db_deepagent.config import apply_source_runtime_config

    updated = apply_source_runtime_config(cfg, registration.db_name)
    updated.business_db.host = registration.host
    updated.business_db.port = registration.port
    updated.business_db.user = registration.username
    updated.business_db.password = registration.password
    updated.business_db.sslmode = registration.sslmode or "prefer"
    updated.business_db.allowed_schemas = list(registration.selected_schemas)
    return updated
