"""数据源登记用例；不依赖 FastAPI 或 Pydantic。"""

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from backend.data_sources.connection_check import (
    SourceConnectionInput,
    check_connection,
    open_source_connection,
)
from backend.data_sources.repository import (
    find_db_sources_by_connection,
    get_db_source,
    insert_db_source,
    latest_source_activity,
    list_db_sources,
    purge_source_profiles,
    remove_db_source,
    set_db_source_active,
    update_db_source,
)
from backend.jobs.repository import STATUS_RUNNING, list_source_step_rows
from backend.publishing.repository import lock_db_source_row
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.security.source_password import decrypt_password, encrypt_password
from metaweave.utils.time_utils import utc_isoformat
from metaweave.core.clean_targets import resolve_source_workspace_dir
from metaweave.core.metadata.db_step import (
    DbProfileValidationError,
    format_server_version,
    validate_schema_names,
    validate_selected_schemas,
)


class PlatformUnavailableError(RuntimeError):
    """平台库不可用。"""


class DuplicateSourceError(ValueError):
    def __init__(self, details: list[dict[str, str]], *, updating: bool = False):
        super().__init__("发现同连接信息的数据源")
        self.details = details
        self.updating = updating


class SourceNotFoundError(LookupError):
    """按 source_id 未找到已登记数据源。"""


class PasswordRequiredError(ValueError):
    """连接目标已变化，但请求没有提交新密码。"""


class SourceCredentialError(RuntimeError):
    """已存源库密码无法解密；不携带密文或密钥。"""


class OutputDeleteFailedError(RuntimeError):
    """输出目录删除失败；登记记录仍保留。"""


class SourceBusyError(RuntimeError):
    """该数据源仍有步骤在运行，不能删除（doc 16 §6）。"""


@dataclass(frozen=True)
class CreateSourceInput:
    connection: SourceConnectionInput
    display_name: str
    selected_schemas: tuple[str, ...]
    description: str | None = None
    confirm_duplicate: bool = False


def _normalize_description(value: str | None) -> str | None:
    # 与 API 请求模型保持相同规则；修改说明字段限制时须同步两处。
    if value is None:
        return None
    if not isinstance(value, str):
        raise DbProfileValidationError("数据源说明必须是字符串")
    value = value.strip()
    if len(value) > 1000:
        raise DbProfileValidationError("数据源说明不能超过 1000 个字符")
    return value or None


def _source_item(row) -> dict:
    """只投影管理员列表允许展示的字段，且在 Session 关闭前完成。"""
    return {
        "source_id": str(row.source_id),
        "display_name": row.display_name,
        "db_type": row.db_type,
        "host": row.host,
        "port": row.port,
        "db_name": row.db_name,
        "username": row.username,
        "sslmode": row.sslmode,
        "selected_schemas": list(row.selected_schemas),
        "description": row.description,
        "db_version": row.db_version,
        "created_at": utc_isoformat(row.created_at),
        "is_active": row.is_active,
    }


def _duplicate_details(rows) -> list[dict[str, str]]:
    return [
        {"source_id": str(row.source_id), "display_name": row.display_name}
        for row in rows
    ]


def create_data_source(settings: PlatformSettings, fields: CreateSourceInput) -> dict:
    if not fields.display_name.strip():
        raise DbProfileValidationError("数据源显示名称不能为空")
    names = validate_schema_names(fields.selected_schemas)
    description = _normalize_description(fields.description)
    connection = fields.connection
    try:
        with platform_session(settings) as session:
            duplicates = find_db_sources_by_connection(
                session,
                connection.host,
                connection.port,
                connection.db_name,
                connection.username,
            )
            details = _duplicate_details(duplicates)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None

    if details and not fields.confirm_duplicate:
        raise DuplicateSourceError(details)

    with open_source_connection(connection) as (conn, version):
        validate_selected_schemas(conn, names)
    db_version = format_server_version(version)

    ciphertext = encrypt_password(connection.password)

    try:
        with platform_session(settings) as session:
            row = insert_db_source(
                session,
                {
                    "source_id": uuid4(),
                    "display_name": fields.display_name.strip(),
                    "db_type": connection.db_type,
                    "host": connection.host,
                    "port": connection.port,
                    "db_name": connection.db_name,
                    "username": connection.username,
                    "password_ciphertext": ciphertext,
                    "sslmode": connection.sslmode,
                    "db_version": db_version,
                    "selected_schemas": list(names),
                    "description": description,
                    "created_by_user_id": None,
                },
            )
            return _source_item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


@dataclass(frozen=True)
class UpdateSourceInput:
    db_type: str
    host: str
    port: int
    db_name: str
    username: str
    sslmode: str | None
    display_name: str
    selected_schemas: tuple[str, ...]
    password: str | None = field(default=None, repr=False)
    description: str | None = None
    confirm_duplicate: bool = False


def _decrypt_stored_password(ciphertext: str) -> str:
    try:
        return decrypt_password(ciphertext)
    except ValueError:
        raise SourceCredentialError("源库凭据无法解密") from None


def _load_source_for_change(
    settings: PlatformSettings, source_id: UUID, fields: UpdateSourceInput
):
    """读取当前行并查重，关闭平台库会话后返回内存副本。"""
    try:
        with platform_session(settings) as session:
            row = get_db_source(session, source_id)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            current = {
                "host": row.host,
                "port": row.port,
                "db_name": row.db_name,
                "username": row.username,
                "sslmode": row.sslmode,
                "password_ciphertext": row.password_ciphertext,
            }
            duplicates = find_db_sources_by_connection(
                session,
                fields.host,
                fields.port,
                fields.db_name,
                fields.username,
            )
            details = [
                item
                for item in _duplicate_details(duplicates)
                if item["source_id"] != str(source_id)
            ]
            return current, details
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def _connection_target_changed(current: dict, fields: UpdateSourceInput) -> bool:
    return (
        fields.host != current["host"]
        or fields.port != current["port"]
        or fields.db_name != current["db_name"]
        or fields.username != current["username"]
        or fields.sslmode != current["sslmode"]
    )


def get_registered_source(settings: PlatformSettings, source_id: UUID) -> dict:
    try:
        with platform_session(settings) as session:
            row = get_db_source(session, source_id)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            return _source_item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def set_source_activation(
    settings: PlatformSettings, source_id: UUID, is_active: bool
) -> dict:
    """启停数据源；只写 is_active 与 updated_at（doc 11 §5）。"""
    try:
        with platform_session(settings) as session:
            row = set_db_source_active(session, source_id, is_active)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            return _source_item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def check_saved_connection(settings: PlatformSettings, source_id: UUID) -> dict:
    try:
        with platform_session(settings) as session:
            row = get_db_source(session, source_id)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            stored = {
                "db_type": row.db_type,
                "host": row.host,
                "port": row.port,
                "db_name": row.db_name,
                "username": row.username,
                "password_ciphertext": row.password_ciphertext,
                "sslmode": row.sslmode,
            }
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None
    password = _decrypt_stored_password(stored["password_ciphertext"])
    return check_connection(
        SourceConnectionInput(
            db_type=stored["db_type"],
            host=stored["host"],
            port=stored["port"],
            db_name=stored["db_name"],
            username=stored["username"],
            password=password,
            sslmode=stored["sslmode"],
        )
    )


def update_registered_source(
    settings: PlatformSettings, source_id: UUID, fields: UpdateSourceInput
) -> dict:
    if not fields.display_name.strip():
        raise DbProfileValidationError("数据源显示名称不能为空")
    names = validate_schema_names(fields.selected_schemas)
    description = _normalize_description(fields.description)
    current, details = _load_source_for_change(settings, source_id, fields)
    if details and not fields.confirm_duplicate:
        raise DuplicateSourceError(details, updating=True)
    if _connection_target_changed(current, fields) and not fields.password:
        raise PasswordRequiredError("连接信息已修改，请填写密码后重试")

    password = fields.password or _decrypt_stored_password(
        current["password_ciphertext"]
    )
    with open_source_connection(
        SourceConnectionInput(
            db_type=fields.db_type,
            host=fields.host,
            port=fields.port,
            db_name=fields.db_name,
            username=fields.username,
            password=password,
            sslmode=fields.sslmode,
        )
    ) as (conn, version):
        validate_selected_schemas(conn, names)
    db_version = format_server_version(version)
    ciphertext = (
        encrypt_password(password)
        if fields.password
        else current["password_ciphertext"]
    )

    try:
        with platform_session(settings) as session:
            row = update_db_source(
                session,
                source_id,
                {
                    "display_name": fields.display_name.strip(),
                    "db_type": fields.db_type,
                    "host": fields.host,
                    "port": fields.port,
                    "db_name": fields.db_name,
                    "username": fields.username,
                    "password_ciphertext": ciphertext,
                    "sslmode": fields.sslmode,
                    "db_version": db_version,
                    "selected_schemas": list(names),
                    "description": description,
                },
            )
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            return _source_item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def delete_source_workspace(source_id: UUID) -> None:
    try:
        workspace = resolve_source_workspace_dir(source_id)
        if workspace.exists():
            shutil.rmtree(workspace)
    except (OSError, ValueError):
        raise OutputDeleteFailedError(
            "该数据源的输出目录删除失败，登记记录仍保留"
        ) from None


def delete_registered_source(
    settings: PlatformSettings, source_id: UUID, *, delete_output: bool = False
) -> None:
    """删除登记与当前画像，保留作业/发布历史（doc 16 §6）。

    顺序固定：确认登记存在 → 源行锁下核对全部作业、全部步骤无 running
    →（可选）删除工作区 → 同一事务删当前画像、清全部 current、删登记。
    任一步失败都回滚，登记、画像与工作区保持不变（工作区先删而数据库
    提交失败的既有非原子边界仍按 doc 7 报告）。
    """
    try:
        with platform_session(settings) as session:
            if get_db_source(session, source_id) is None:
                raise SourceNotFoundError("数据源不存在")
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None

    try:
        with platform_session(settings) as session:
            if lock_db_source_row(session, source_id) is None:
                raise SourceNotFoundError("数据源不存在")
            if any(
                step.last_status == STATUS_RUNNING
                for step in list_source_step_rows(session, source_id)
            ):
                raise SourceBusyError("该数据源有采集或发布步骤正在执行，不能删除")
            if delete_output:
                delete_source_workspace(source_id)
            purge_source_profiles(session, source_id)
            if not remove_db_source(session, source_id):
                raise SourceNotFoundError("数据源不存在")
            session.commit()
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def list_registered_sources(settings: PlatformSettings) -> list[dict]:
    try:
        with platform_session(settings) as session:
            collected, published = latest_source_activity(session)
            items = []
            for row in list_db_sources(session):
                item = _source_item(row)
                item["last_collected_at"] = _format_optional_time(
                    collected.get(row.source_id)
                )
                item["last_published_at"] = _format_optional_time(
                    published.get(row.source_id)
                )
                items.append(item)
            return items
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def _format_optional_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return utc_isoformat(value)
