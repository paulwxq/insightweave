"""doc 24.1、6.2 §5.2：发布页 domain 配置编辑（读取 / 校验 / 原子写回）。

按工作区定位；保存前对将被覆盖的 ``db_domains.yaml`` 做文件级备份（只
复制旧版，不触碰采集 marker 与其他产物）；备份失败本次保存终止且不改动
任何现有内容。保存只改受管 ``db_domains.yaml`` 的 domain 列表，库名、
库描述和 ``llm_inference`` 不接受请求里的新值；指纹对文件原始字节计算。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from backend.data_sources.repository import get_db_source
from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)
from backend.profile_loading.file_utils import read_regular_text
from backend.workspace_files.backups import (
    EditBackupFailedError,
    backup_files_before_edit,
)
from backend.workspace_files.markers import read_collection_marker
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
)
from metaweave.core.metadata.domain_generator import LiteralDumper
from metaweave.utils.source_id import normalize_workspace_id

DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
DOMAIN_STEP_NAME = "domain"
MD_MANIFEST_NAME = "manifest.json"
MD_MANIFEST_REASON = "MD 采集清单缺失或不合法，请重跑 md 后再编辑"
_YAML_HEADER = "# 数据库业务主题配置\n"


class DomainConfigOutputMissingError(Exception):
    """404 domain_config_output_missing：步骤行缺失、未成功，或产物不是普通文件。"""

    def __init__(self, message: str = "Domain 产物或步骤记录不存在"):
        super().__init__(message)
        self.details: dict[str, Any] = {}


class DomainConfigInvalidError(ValueError):
    """400 domain_config_invalid。"""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.details = {"reason": reason}


class DomainConfigRegeneratedError(Exception):
    """409 domain_config_regenerated：指纹与当前文件不一致。"""

    def __init__(
        self,
        message: str = "文件已被重跑或另一次保存改过，请关闭后重新打开",
    ):
        super().__init__(message)
        self.details: dict[str, Any] = {}


class DomainConfigBackupFailedError(Exception):
    """500 domain_config_backup_failed：文件级备份失败，本次保存终止。"""

    def __init__(self, message: str = "domain 配置备份失败，本次保存未执行"):
        super().__init__(message)
        self.details: dict[str, Any] = {}


class DomainConfigWriteFailedError(Exception):
    """500 domain_config_write_failed：临时文件写入或替换失败，已完成备份保留。"""

    def __init__(
        self, message: str = "domain 配置写回失败；已完成的备份保留供人工核对"
    ):
        super().__init__(message)
        self.details: dict[str, Any] = {}


def replace_domain_file(src: Path, dst: Path) -> None:
    """原子替换。独立函数便于测试注入失败。"""

    os.replace(src, dst)


def _require_context(session, source_id: str, workspace_id: str):
    """工作区归属与 domain 产物准入（doc 6 §4.1：唯一成功 marker）。"""
    from backend.workspaces.repository import get_workspace

    workspace = get_workspace(session, workspace_id)
    if workspace is None:
        raise DomainConfigOutputMissingError("工作区不存在")
    if workspace.source_id != source_id:
        raise DomainConfigOutputMissingError("工作区不属于该数据源")
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用")
    marker, error = read_collection_marker(
        resolve_domain_output_dir(source_id, workspace_id)
    )
    if marker is None:
        raise DomainConfigOutputMissingError(
            error or "该工作区没有 Domain 采集 marker，请先执行 Domain"
        )
    if marker.status != "success":
        raise DomainConfigOutputMissingError(
            "该工作区的 Domain 采集未成功，请先重跑 Domain 后再编辑"
        )
    return source


def _yaml_path(source_id: str, workspace_id: str) -> Path:
    return resolve_domain_output_dir(source_id, workspace_id) / DOMAIN_ARTIFACT_NAME


def _read_regular_bytes(path: Path) -> tuple[str, bytes | str]:
    """按二进制读取普通文件，供指纹使用。

    ``read_regular_text`` 用文本模式，会把 CRLF 折成 LF。指纹必须对原始字节
    计算，所以 YAML 不走那条返回值，只沿用同样的打开标志。
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "missing", b""
    except OSError:
        return "invalid", f"{path.name} 打不开或不是普通文件"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return "invalid", f"{path.name} 不是普通文件"
        handle = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return "invalid", f"{path.name} 打不开"
    with handle:
        try:
            return "ok", handle.read()
        except OSError:
            return "invalid", f"{path.name} 读不出"


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_yaml_bytes(path: Path) -> tuple[bytes, dict]:
    status, payload = _read_regular_bytes(path)
    if status == "missing":
        raise DomainConfigOutputMissingError("Domain 产物缺失或不是普通文件")
    if status != "ok":
        raise DomainConfigOutputMissingError("Domain 产物缺失或不是普通文件")
    assert isinstance(payload, bytes)
    return payload, _parse_yaml_bytes(payload)


def _parse_yaml_bytes(payload: bytes) -> dict:
    """解析已读取的原始字节，使指纹与配置内容使用同一份数据。"""
    if not payload.strip():
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤", "空白文件"
        )
    try:
        text = payload.decode("utf-8")
        loaded = yaml.safe_load(text)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤",
            f"YAML 无法解析（{type(exc).__name__}）",
        ) from None
    if not isinstance(loaded, dict):
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤", "顶层不是对象"
        )
    return loaded


def _max_domains(llm_inference: Any) -> int | None:
    if not isinstance(llm_inference, dict):
        return None
    value = llm_inference.get("max_domains_per_table")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _file_description(database: dict) -> Any:
    if "description" not in database:
        return None
    return database.get("description")


def _config_view(raw: bytes, document: dict, registered_db_name: str) -> dict:
    database = document.get("database")
    if not isinstance(database, dict):
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤", "database 不是对象"
        )
    name = database.get("name")
    if not isinstance(name, str) or name.strip() != str(registered_db_name).strip():
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤", "库名与登记库名不一致"
        )
    description = _file_description(database)
    if description is not None and not isinstance(description, str):
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤",
            "database.description 必须是字符串",
        )
    domains = _validated_file_domains(document.get("domains"))
    return {
        "fingerprint": _fingerprint(raw),
        "database": {"name": name, "description": description},
        "max_domains_per_table": _max_domains(document.get("llm_inference")),
        "domains": domains,
    }


def _validated_file_domains(domains: Any) -> list[dict]:
    if not isinstance(domains, list):
        raise DomainConfigInvalidError(
            "Domain 产物不可编辑，请重跑 domain 步骤", "domains 不是列表"
        )
    seen: set[str] = set()
    rows: list[dict] = []
    for item in domains:
        if not isinstance(item, dict):
            raise DomainConfigInvalidError(
                "Domain 产物不可编辑，请重跑 domain 步骤", "domain 项不是对象"
            )
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise DomainConfigInvalidError(
                "Domain 产物不可编辑，请重跑 domain 步骤", "domain 名称缺失或空白"
            )
        if not isinstance(description, str) or not description.strip():
            raise DomainConfigInvalidError(
                "Domain 产物不可编辑，请重跑 domain 步骤", "domain 描述缺失或空白"
            )
        key = name.strip()
        if key in seen:
            raise DomainConfigInvalidError(
                "Domain 产物不可编辑，请重跑 domain 步骤",
                f"同名 domain 不支持编辑，请重跑 domain：{key}",
            )
        seen.add(key)
        if "tables" not in item:
            tables: list[str] = []
        else:
            tables_raw = item.get("tables")
            if not isinstance(tables_raw, list) or any(
                not isinstance(table, str) for table in tables_raw
            ):
                raise DomainConfigInvalidError(
                    "Domain 产物不可编辑，请重跑 domain 步骤",
                    f"{key} 的 tables 必须是字符串列表",
                )
            tables = list(tables_raw)
        rows.append({"name": name, "description": description, "tables": tables})
    return rows


def _manifest_files(source_id: str, workspace_id: str) -> tuple[Path, list[str]]:
    try:
        md_dir = resolve_md_output_dir(source_id, workspace_id)
    except (ValueError, OSError):
        raise DomainConfigInvalidError(MD_MANIFEST_REASON, MD_MANIFEST_REASON) from None
    status, content = read_regular_text(md_dir / MD_MANIFEST_NAME, "MD 采集清单")
    if status != "ok" or not isinstance(content, str):
        raise DomainConfigInvalidError(MD_MANIFEST_REASON, MD_MANIFEST_REASON)
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError:
        raise DomainConfigInvalidError(MD_MANIFEST_REASON, MD_MANIFEST_REASON) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(name, str) or not name.endswith(".md") for name in files)
        or len(set(files)) != len(files)
    ):
        raise DomainConfigInvalidError(MD_MANIFEST_REASON, MD_MANIFEST_REASON)
    return md_dir, files


def _candidate_tables(source, source_id: str, workspace_id: str) -> list[dict]:
    md_dir, files = _manifest_files(source_id, workspace_id)
    db_name = str(source.db_name).strip()
    schemas = {str(item).strip() for item in (source.selected_schemas or [])}
    tables: list[dict] = []
    for filename in files:
        stem = filename[: -len(".md")]
        parts = stem.split(".")
        if len(parts) != 3 or any(not part for part in parts):
            continue
        if parts[0] != db_name or parts[1] not in schemas:
            continue
        status, _content = read_regular_text(md_dir / filename, filename)
        if status != "ok":
            continue
        tables.append({"id": stem, "label": f"{parts[1]}.{parts[2]}"})
    return tables


def _normalize_request_domains(
    domains: Any, candidate_ids: set[str], *, check_candidates: bool = True
) -> list[dict]:
    if not isinstance(domains, list):
        raise DomainConfigInvalidError("domain 配置不合法", "domains 必须是列表")
    seen_names: set[str] = set()
    normalized: list[dict] = []
    for item in domains:
        if not isinstance(item, dict):
            raise DomainConfigInvalidError("domain 配置不合法", "domain 项必须是对象")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise DomainConfigInvalidError("domain 配置不合法", "domain 名称不能为空")
        if not isinstance(description, str) or not description.strip():
            raise DomainConfigInvalidError("domain 配置不合法", "domain 描述不能为空")
        stripped_name = name.strip()
        stripped_description = description.strip()
        if stripped_name in seen_names:
            raise DomainConfigInvalidError(
                "domain 配置不合法", f"domain 名称重复：{stripped_name}"
            )
        seen_names.add(stripped_name)
        tables = item.get("tables")
        if not isinstance(tables, list) or any(
            not isinstance(table, str) for table in tables
        ):
            raise DomainConfigInvalidError(
                "domain 配置不合法", f"{stripped_name} 的 tables 必须是字符串列表"
            )
        seen_tables: set[str] = set()
        kept: list[str] = []
        for table in tables:
            if check_candidates and table not in candidate_ids:
                raise DomainConfigInvalidError("domain 配置不合法", f"未知表: {table}")
            if table in seen_tables:
                raise DomainConfigInvalidError(
                    "domain 配置不合法", f"{stripped_name} 内表名重复: {table}"
                )
            seen_tables.add(table)
            kept.append(table)
        normalized.append(
            {
                "name": stripped_name,
                "description": stripped_description,
                "tables": kept,
            }
        )
    return normalized


def _render(document: dict, domains: list[dict]) -> str:
    payload: dict[str, Any] = {}
    database = document.get("database")
    if not isinstance(database, dict):
        database = {}
    written_database: dict[str, Any] = {"name": document["__registered_name__"]}
    if "description" in database:
        written_database["description"] = database.get("description")
    payload["database"] = written_database
    if "llm_inference" in document:
        payload["llm_inference"] = document["llm_inference"]
    payload["domains"] = domains
    body = yaml.dump(
        payload,
        Dumper=LiteralDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    return _YAML_HEADER + body


def read_domain_config(settings: PlatformSettings, workspace_id) -> dict:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace, source_id = _workspace_source(session, workspace_id)
        source = _require_context(session, source_id, workspace_id)
        raw, document = _load_yaml_bytes(_yaml_path(source_id, workspace_id))
        return _config_view(raw, document, source.db_name)


def list_domain_tables(settings: PlatformSettings, workspace_id) -> dict:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace, source_id = _workspace_source(session, workspace_id)
        source = _require_context(session, source_id, workspace_id)
        _load_yaml_bytes(_yaml_path(source_id, workspace_id))
        return {"tables": _candidate_tables(source, source_id, workspace_id)}


def _workspace_source(session, workspace_id: str):
    from backend.workspaces.repository import get_workspace as _get_ws

    workspace = _get_ws(session, workspace_id)
    if workspace is None:
        raise DomainConfigOutputMissingError("工作区不存在")
    return workspace, workspace.source_id


def write_domain_config(
    settings: PlatformSettings, workspace_id, payload: dict
) -> dict:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        # 工作区行锁与采集步骤认领共用，覆盖指纹读取至原子替换（doc 6.2 §5.1）
        from backend.persistence.models import WorkspaceInfo

        workspace_row = session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        if workspace_row is None:
            raise DomainConfigOutputMissingError("工作区不存在")
        source_id = workspace_row.source_id
        source = _require_context(session, source_id, workspace_id)
        path = _yaml_path(source_id, workspace_id)
        status, raw = _read_regular_bytes(path)
        if status != "ok" or not isinstance(raw, bytes):
            raise DomainConfigOutputMissingError("Domain 产物缺失或不是普通文件")
        if not isinstance(payload, dict) or payload.get("fingerprint") != _fingerprint(
            raw
        ):
            raise DomainConfigRegeneratedError()
        document = _parse_yaml_bytes(raw)

        database = document.get("database")
        if not isinstance(database, dict):
            raise DomainConfigInvalidError("domain 配置不合法", "database 不是对象")
        request_database = payload.get("database")
        if not isinstance(request_database, dict):
            raise DomainConfigInvalidError("domain 配置不合法", "database 必须是对象")
        request_name = request_database.get("name")
        if (
            not isinstance(request_name, str)
            or request_name.strip() != str(source.db_name).strip()
        ):
            raise DomainConfigInvalidError("domain 配置不合法", "库名与登记库名不一致")
        file_description = _file_description(database)
        if request_database.get("description", None) != file_description:
            raise DomainConfigInvalidError(
                "domain 配置不合法", "库描述不可在发布页修改"
            )

        requested = _normalize_request_domains(
            payload.get("domains"), set(), check_candidates=False
        )
        current_domains = [
            {
                "name": item["name"].strip(),
                "description": item["description"].strip(),
                "tables": list(item["tables"]),
            }
            for item in _validated_file_domains(document.get("domains"))
        ]
        if requested == current_domains:
            return _config_view(raw, document, source.db_name)

        candidates = _candidate_tables(source, source_id, workspace_id)
        domains = _normalize_request_domains(
            payload.get("domains"), {item["id"] for item in candidates}
        )
        registered_name = str(source.db_name).strip()
        document = dict(document)
        document["__registered_name__"] = registered_name
        rendered = _render(document, domains)
        loaded = yaml.safe_load(rendered)
        if not isinstance(loaded, dict) or loaded.get("domains") != domains:
            raise DomainConfigWriteFailedError()

        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        # 文件级备份（doc 6.2 §5.2）：只复制将被覆盖的旧版；失败则本次
        # 保存终止，不写回、不删除任何现有内容，不触碰采集 marker。
        try:
            backup_files_before_edit(directory, [DOMAIN_ARTIFACT_NAME])
        except EditBackupFailedError as exc:
            raise DomainConfigBackupFailedError(str(exc)) from None
        temp_path: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".tmp-domain-", dir=str(directory))
            temp_path = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(rendered.encode("utf-8"))
            os.chmod(temp_path, 0o644)
            replace_domain_file(temp_path, path)
            temp_path = None
        except DomainConfigWriteFailedError:
            raise
        except Exception as exc:
            raise DomainConfigWriteFailedError() from exc
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        written, written_doc = _load_yaml_bytes(path)
        view = _config_view(written, written_doc, source.db_name)
        return view
