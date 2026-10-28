"""受管 Domain 产物读取与解析（doc 21.2 §4）。

只读校验受管目录中的 `db_domains.yaml` 与同一作业的 MD 清单，并映射为
`table_domain` 待插入行；不自行提交、不写平台库。发布事务由
`backend.publishing.publish_service` 负责。

与 json/ddl 加载器的差异（doc 21.2 §4 规则 7/8）：解析失败、零行、至少
一行三态都由调用方先做锁下复查，再按解析结果记失败文案，所以本模块只
抛 `DomainPublishInputError`，不写失败记录。
"""

import json
import logging
import os
import stat
from pathlib import Path

import yaml

from backend.profile_loading.json_loader import RegistrationSnapshot
from metaweave.core.clean_targets import (
    resolve_domain_output_dir,
    resolve_md_output_dir,
)

logger = logging.getLogger("backend.profile_loading")

DOMAIN_STEP = "domain"
DOMAIN_ARTIFACT_NAME = "db_domains.yaml"
# 受管 MD 目录内的采集清单文件名，与采集侧
# backend.data_sources.md_step_service 的同名常量必须一致（测试锁定）。
MD_MANIFEST_NAME = "manifest.json"


class DomainPublishInputError(ValueError):
    """Domain 产物不满足发布输入契约（doc 21.2 §4）。"""


def check_domain_output_ready(source_id: str, workspace_id: str) -> bool:
    """预检：Domain 目录可解析且 `db_domains.yaml` 存在并为普通文件。

    只判断文件就绪，不读内容、不数表映射行数（doc 21.2 §4）：零行产物
    仍可勾选，留到发布时记失败历史。
    """
    try:
        domain_dir = resolve_domain_output_dir(source_id, workspace_id)
        artifact = domain_dir / DOMAIN_ARTIFACT_NAME
        if artifact.is_symlink() or not artifact.is_file():
            logger.warning(
                "Domain 预检产物缺失或非常规文件 source_id=%s workspace_id=%s",
                source_id,
                workspace_id,
            )
            return False
        return True
    except (ValueError, OSError) as exc:
        logger.warning(
            "Domain 预检产物目录不可用 source_id=%s workspace_id=%s type=%s",
            source_id,
            workspace_id,
            type(exc).__name__,
        )
        return False


def _read_regular_text(path: Path, label: str) -> str:
    """按不跟随符号链接的方式读取普通文件（doc 21.2 §4 规则 2）。

    - ``O_NOFOLLOW``：拒绝最后一级符号链接（上级目录由 resolver 逐级拒绝）；
    - ``O_NONBLOCK``：FIFO 打开不阻塞；
    - ``fstat`` 复核常规文件，阻断设备文件等其它形态。
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise DomainPublishInputError(f"{label} 打不开或不是普通文件") from None
    transferred = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise DomainPublishInputError(f"{label} 不是普通文件")
        handle = os.fdopen(fd, "r", encoding="utf-8")
        transferred = True
        with handle:
            return handle.read()
    except DomainPublishInputError:
        raise
    except (OSError, UnicodeError):
        raise DomainPublishInputError(f"{label} 读不出") from None
    finally:
        if not transferred:
            os.close(fd)


def _load_domain_document(source_id: str, workspace_id: str) -> dict:
    """解析 Domain YAML 顶层与 domains 列表；失败抛 DomainPublishInputError。"""
    try:
        domain_dir = resolve_domain_output_dir(source_id, workspace_id)
    except (ValueError, OSError) as exc:
        # 目录解析失败（含权限、I/O 等 OSError）按解析失败处理（doc 21.2 §4 规则 2）
        raise DomainPublishInputError(
            f"Domain 作业目录不可用（{type(exc).__name__}）"
        ) from None
    text = _read_regular_text(
        domain_dir / DOMAIN_ARTIFACT_NAME, "Domain 产物 db_domains.yaml"
    )
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DomainPublishInputError(
            f"Domain 产物 YAML 无法解析（{type(exc).__name__}）"
        ) from None
    if not isinstance(loaded, dict):
        raise DomainPublishInputError("Domain 产物顶层必须是对象")
    if not isinstance(loaded.get("domains"), list):
        raise DomainPublishInputError("Domain 产物 domains 必须是列表")
    return loaded


def _read_md_manifest(source_id: str, workspace_id: str) -> "tuple[Path, set[str]]":
    """读取同一作业 MD 采集清单（doc 21.2 §4 规则 5）。

    清单按 MD 成功时写入的文件名为准；目录现状只用于复核被引用的
    文件现在仍是普通文件。无论 YAML 有没有表映射都要先过这里。
    """
    try:
        md_dir = resolve_md_output_dir(source_id, workspace_id)
    except (ValueError, OSError) as exc:
        raise DomainPublishInputError(
            f"MD 作业目录不可用（{type(exc).__name__}）"
        ) from None
    text = _read_regular_text(md_dir / MD_MANIFEST_NAME, "MD 采集清单 manifest.json")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError:
        raise DomainPublishInputError("MD 采集清单不可读") from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(name, str) or not name.endswith(".md") for name in files)
        or len(set(files)) != len(files)
    ):
        raise DomainPublishInputError("MD 采集清单形状不合法")
    return md_dir, set(files)


def read_domain_rows(
    source_id: str, workspace_id: str, registration: RegistrationSnapshot
) -> "list[dict]":
    """读取并解析受管 Domain 产物，返回 `table_domain` 待插入行（doc 21.2 §4）。

    行只含内容列（schema_name/table_name/domain_name/domain_desc/db_name），
    source_id 与 publish_run_id 由发布事务补齐。任一结构非法整次失败。
    """
    document = _load_domain_document(source_id, workspace_id)

    database = document.get("database")
    if not isinstance(database, dict):
        raise DomainPublishInputError("Domain 产物 database 必须是对象")
    db_name = database.get("name")
    if not isinstance(db_name, str) or not db_name.strip():
        raise DomainPublishInputError(
            "Domain 产物 database.name 必须是去空白后非空的字符串"
        )
    registered_db_name = str(registration.db_name)
    if db_name.strip() != registered_db_name.strip():
        raise DomainPublishInputError("Domain 产物库名与数据源登记库名不一致")

    descriptions: "dict[str, str]" = {}
    domain_items: "list[tuple[str, str, list]]" = []
    for item in document["domains"]:
        if not isinstance(item, dict):
            raise DomainPublishInputError("Domain 产物 domains 项必须是对象")
        name = item.get("name")
        description = item.get("description")
        if not isinstance(name, str) or not name.strip():
            raise DomainPublishInputError("Domain 产物存在 name 缺失或空白的项")
        name = name.strip()
        if not isinstance(description, str) or not description.strip():
            raise DomainPublishInputError(
                f"Domain 产物 {name} 的 description 缺失或空白"
            )
        description = description.strip()
        previous = descriptions.get(name)
        if previous is not None and previous != description:
            raise DomainPublishInputError(f"Domain 产物同名 Domain 描述冲突: {name}")
        descriptions[name] = description
        if "tables" not in item:
            # 字段缺失才跳过；显式 null 不是列表，按结构非法整次失败（规则 4）
            continue
        tables = item["tables"]
        if tables == []:
            # 空列表（含样例中的 _未分类_）不产生行，但上面已校验其结构
            continue
        if not isinstance(tables, list):
            raise DomainPublishInputError(f"Domain 产物 {name} 的 tables 必须是列表")
        for table in tables:
            if not isinstance(table, str):
                raise DomainPublishInputError(
                    f"Domain 产物 {name} 的 tables 存在非字符串条目"
                )
        domain_items.append((name, description, tables))

    # 无论有没有表映射，都先校验清单（doc 21.2 §4 规则 5）：清单不可用
    # 时不再判零行，统一按解析失败处理。
    md_dir, manifest_files = _read_md_manifest(source_id, workspace_id)

    rows: "list[dict]" = []
    seen: "set[tuple[str, str, str]]" = set()
    expected_schemas = set(registration.selected_schemas)
    for name, description, tables in domain_items:
        for table in tables:
            parts = [part.strip() for part in table.split(".")]
            if len(parts) != 3 or any(not part for part in parts):
                raise DomainPublishInputError(
                    f"Domain 产物表名不是三段式: {table.strip()}"
                )
            if parts[0] != registered_db_name.strip():
                raise DomainPublishInputError(
                    f"Domain 产物表名首段与登记库名不一致: {table.strip()}"
                )
            if parts[1] not in expected_schemas:
                raise DomainPublishInputError(
                    f"Domain 产物表名 schema 不在登记范围内: {table.strip()}"
                )
            file_name = ".".join(parts) + ".md"
            if file_name not in manifest_files:
                raise DomainPublishInputError(f"表不在同一作业 MD 清单中: {file_name}")
            md_file = md_dir / file_name
            if md_file.is_symlink() or not md_file.is_file():
                raise DomainPublishInputError(
                    f"对应的 MD 文件已不是普通文件: {file_name}"
                )
            key = (parts[1], parts[2], name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "schema_name": parts[1],
                    "table_name": parts[2],
                    "domain_name": name,
                    "domain_desc": description,
                    "db_name": registered_db_name,
                }
            )
    return rows
