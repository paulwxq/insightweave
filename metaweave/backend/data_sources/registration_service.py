"""数据源登记用例；不依赖 FastAPI 或 Pydantic。

`source_id` 为 `db_src_1` 等 text 短 ID（doc 6.1 §13）：登记入口不接受
客户端指定 ID，由数据库序列默认值生成并取回；不迁移旧 UUID。
"""

from dataclasses import dataclass, field
from datetime import datetime

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
    set_db_source_active,
    update_db_source,
)
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.security.source_password import decrypt_password, encrypt_password
from metaweave.utils.time_utils import utc_isoformat
from metaweave.core.metadata.db_step import (
    DbProfileValidationError,
    format_server_version,
    validate_schema_names,
    validate_selected_schemas,
)
from metaweave.utils.source_id import normalize_source_id


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


class SourceHasWorkspacesError(RuntimeError):
    """该数据源已关联工作区，不能删除（doc 6.2 §6.1、A29）。

    工作区及作业、版本、运行记录本期不提供删除入口；已关联工作区的
    数据源删除一律拒绝，不级联删除，也不借 delete_output 删除工作区目录。
    """


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
        "source_id": row.source_id,
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
        {"source_id": row.source_id, "display_name": row.display_name} for row in rows
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
            # source_id 由数据库序列默认值生成（doc 6.1 §13.1），
            # ORM 通过 INSERT .. RETURNING 取回新短 ID。
            row = insert_db_source(
                session,
                {
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
    settings: PlatformSettings, source_id: str, fields: UpdateSourceInput
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
                if item["source_id"] != source_id
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


def get_registered_source(settings: PlatformSettings, source_id: str) -> dict:
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            row = get_db_source(session, source_id)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            # doc 8.1 §3.3：已删除源详情 410（不因已知 ID 绕过列表过滤）
            if getattr(row, "deleted_at", None) is not None:
                from backend.deletion.errors import ResourceDeletedError

                raise ResourceDeletedError("数据源已删除")
            item = _source_item(row)
            from backend.deletion.repository import task_summary

            task = task_summary(session, source_id=source_id)
            item["deletion_task"] = (
                {**task, "task_id": str(task["task_id"])} if task else None
            )
            return item
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def set_source_activation(
    settings: PlatformSettings, source_id: str, is_active: bool
) -> dict:
    """启停数据源；只写 is_active 与 updated_at（doc 11 §5）。"""
    source_id = normalize_source_id(source_id)
    try:
        with platform_session(settings) as session:
            # doc 8.1 §8.1：写入口先锁源并经守卫复查（已删 410 / 冻结 409）
            from backend.access_guards import require_source
            from backend.deletion.locks import lock_source_row

            lock_source_row(session, source_id)
            require_source(session, source_id, for_write=True)
            # doc 10.2 §5.2：禁用等影响加载的修改在源锁下检查活动发布后
            # 拒绝（409 source_publish_active）；启用方向一并拦截保持一致，
            # 避免"禁用-启用"绕开窗口
            from backend.publishing.repository import (
                SourcePublishActiveError,
                active_publish_runs_for_source,
            )

            blocking_runs = active_publish_runs_for_source(session, source_id)
            if blocking_runs:
                raise SourcePublishActiveError(blocking_runs[0])
            row = set_db_source_active(session, source_id, is_active)
            if row is None:
                raise SourceNotFoundError("数据源不存在")
            return _source_item(row)
    except SQLAlchemyError:
        raise PlatformUnavailableError("平台库暂不可用") from None


def check_saved_connection(settings: PlatformSettings, source_id: str) -> dict:
    source_id = normalize_source_id(source_id)
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
    settings: PlatformSettings, source_id: str, fields: UpdateSourceInput
) -> dict:
    if not fields.display_name.strip():
        raise DbProfileValidationError("数据源显示名称不能为空")
    source_id = normalize_source_id(source_id)
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
            # doc 8.1 §8.1：修改源连接前锁源并经写守卫复查
            from backend.access_guards import require_source
            from backend.deletion.locks import lock_source_row

            lock_source_row(session, source_id)
            require_source(session, source_id, for_write=True)
            # doc 10.2 §5.2：连接/schema 等影响加载的修改在源锁下检查
            # 活动发布后拒绝（409 source_publish_active）
            from backend.publishing.repository import (
                SourcePublishActiveError,
                active_publish_runs_for_source,
            )

            blocking_runs = active_publish_runs_for_source(session, source_id)
            if blocking_runs:
                raise SourcePublishActiveError(blocking_runs[0])
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


def delete_registered_source(settings: PlatformSettings, source_id: str) -> dict:
    """提交数据源删除任务（doc 8.1 §2.4，替换旧同步删除）。

    有无工作区均走同一逻辑删除任务；不再因“无工作区”物理删除登记行，
    也不再提供 delete_output 目录开关。返回任务摘要，202 由路由层表达。
    """
    from backend.deletion.service import submit_source_deletion

    return submit_source_deletion(settings, normalize_source_id(source_id))


def list_registered_sources(settings: PlatformSettings) -> list[dict]:
    try:
        with platform_session(settings) as session:
            collected, published = latest_source_activity(session)
            from backend.deletion.repository import task_summary

            items = []
            for row in list_db_sources(session):
                # doc 8.1 §3.3：普通列表排除已逻辑删除的源
                if getattr(row, "deleted_at", None) is not None:
                    continue
                item = _source_item(row)
                task = task_summary(session, source_id=row.source_id)
                item["deletion_task"] = (
                    {**task, "task_id": str(task["task_id"])} if task else None
                )
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
