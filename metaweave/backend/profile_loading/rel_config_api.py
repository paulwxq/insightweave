"""doc 23.2 §4：rel 关系编辑 API（GET/PUT 三端点）。

- GET rel-config：关系列表 + 内容指纹 + 表清单 + database 名 +
  ``score_threshold``（``cql_generation.composite_score_threshold``，与 CQL
  生成同一键；列表仍是完整产物，展示过滤在前端，§4.1）；
- GET rel-config/object-columns：指定表列名（json 画像 ``column_profiles``）；
- PUT rel-config：持源行锁的指纹乐观并发 + 逐条校验（D7/D8）+ statistics
  重算 + 两阶段写回（渲染/写盘分离，先 md 后 json——§4.2/§4.3）；
- 异常在 ``backend/api/errors.py`` 注册 handler（§4.5）。

锁策略与 dim-value-config 有意不同（doc 23.2 §4）：dim_value 保存仅靠
dim_config 行 ``updated_at`` 乐观 409——其发布语义是现读配置、最后写者胜出；
rel 发布要求「发布内容 = 预检核对内容」，PUT 须持源行锁在锁内冻结（§4.3），
与发布 ``_publish_rel``、采集 rel 步骤同一把锁。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from backend.data_sources.repository import get_db_source

from backend.persistence.platform_db import PlatformSettings, platform_session
from backend.profile_loading.file_utils import read_regular_text
from backend.profile_loading.db_loader import (
    MetaLoadSourceDisabledError,
    MetaLoadSourceNotFoundError,
)

from metaweave.core.clean_targets import resolve_json_output_dir, resolve_rel_output_dir
from metaweave.core.metadata.execution import load_main_config_snapshot
from metaweave.core.relationships.models import relation_from_json_entry
from metaweave.core.relationships.writer import render_markdown_report
from metaweave.utils.file_utils import resolve_project_path
from metaweave.utils.source_id import normalize_workspace_id

REL_CONFIG_STEP_NAME = "rel"
JSON_MANIFEST_NAME = "manifest.json"
METADATA_CONFIG = "configs/metadata_config.yaml"
VALID_CARDINALITIES = {"N:1", "1:1", "M:N"}


class RelConfigOutputMissingError(Exception):
    """404 rel_config_output_missing：rel 步骤行缺失/未成功或产物文件不存在。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


class RelConfigInvalidError(ValueError):
    """400 rel_config_invalid：PUT 逐条校验失败（details 带首条错误明细）。"""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.details = {"reason": reason}


class RelRegeneratedError(Exception):
    """409 rel_regenerated：PUT 指纹核对不匹配（编辑期间产物被重跑/他处修改）。"""

    def __init__(
        self,
        message: str = "Rel 产物已重新生成（作业可能已重跑），请刷新后重新编辑",
    ):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


class RelWriteFailedError(Exception):
    """500 rel_write_failed：两阶段写回渲染/替换失败（消息注明 JSON 是否写入）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.details: Dict[str, Any] = {}


# ---- 共享读取（O_NOFOLLOW 加固，dim_value_config_api 同款口径） ----


def cql_score_threshold(config: dict) -> float:
    """与 ``CQLGenerator`` 同一键、同一缺省与校验（推断关系 >= 该值才进 CQL）。"""
    from metaweave.core.cql_generator.generator import (
        DEFAULT_COMPOSITE_SCORE_THRESHOLD,
        CQLGenerator,
    )

    section = config.get("cql_generation") if isinstance(config, dict) else None
    if not isinstance(section, dict):
        section = {}
    raw = section.get("composite_score_threshold", DEFAULT_COMPOSITE_SCORE_THRESHOLD)
    return CQLGenerator._validate_threshold(raw)


def _fingerprint(content: str) -> str:
    """内容指纹：SHA-256 十六进制前 16 位（§4.1；发布侧 §6.2 同口径）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _require_workspace(session, workspace_id: str):
    """工作区归属核对：返回 (workspace, source_id)；并要求 rel 目录成功 marker。"""
    from backend.workspaces.repository import get_workspace
    from backend.workspace_files.markers import read_collection_marker
    from metaweave.core.clean_targets import resolve_rel_output_dir

    workspace = get_workspace(session, workspace_id)
    if workspace is None:
        raise RelConfigOutputMissingError("工作区不存在")
    marker, error = read_collection_marker(
        resolve_rel_output_dir(workspace.source_id, workspace_id)
    )
    if marker is None:
        raise RelConfigOutputMissingError(
            error or "该工作区没有 Rel 采集 marker，请先执行 Rel"
        )
    if marker.status != "success":
        raise RelConfigOutputMissingError(
            "该工作区的 Rel 采集未成功，请先重跑 Rel 后再编辑"
        )
    return workspace


def _require_registration(session, source_id: str):
    source = get_db_source(session, source_id)
    if source is None:
        raise MetaLoadSourceNotFoundError("未找到指定 source_id 的数据源")
    if not source.is_active:
        raise MetaLoadSourceDisabledError("指定数据源已禁用，不能编辑 Rel 产物")
    return source


def _artifact_paths(source_id: str, workspace_id: str, database_name: str):
    rel_dir = resolve_rel_output_dir(source_id, workspace_id)
    return (
        rel_dir,
        rel_dir / f"{database_name}.relationships_global.json",
        rel_dir / f"{database_name}.relationships_global.md",
    )


def _load_rel_artifact(json_path: Path) -> Tuple[str, Dict[str, Any]]:
    """读取并解析 rel json 产物，返回 (原始文本, data)——指纹与解析共用。"""
    status, content = read_regular_text(json_path, "rel json")
    if status == "missing":
        raise RelConfigOutputMissingError("Rel 产物文件不存在，请先执行 Rel")
    if status == "invalid":
        raise RelConfigInvalidError("Rel 产物无法读取", content)
    try:
        data = json.loads(content)
    except ValueError:
        raise RelConfigInvalidError(
            "Rel 产物 JSON 无法解析", "json.loads 失败"
        ) from None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("relationships"), list)
        or not isinstance(data.get("statistics"), dict)
    ):
        raise RelConfigInvalidError(
            "Rel 产物结构不合法", "relationships/statistics 缺失或形态不对"
        ) from None
    return content, data


# ---- json 画像目录：表清单与列清单 ----


def _load_table_columns(json_dir: Path) -> Dict[str, List[str]]:
    """枚举受管 json 画像（排除 manifest.json），返回 {schema.table: 有序列名}。"""

    catalog: Dict[str, List[str]] = {}
    if not json_dir.is_dir():
        return catalog
    for path in sorted(json_dir.glob("*.json")):
        if path.name == JSON_MANIFEST_NAME or path.is_symlink() or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        info = data.get("object_info") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            continue
        schema_name = info.get("schema_name")
        object_name = info.get("object_name")
        if not isinstance(schema_name, str) or not isinstance(object_name, str):
            continue
        columns: List[str] = []
        raw_profiles = data.get("column_profiles")
        if isinstance(raw_profiles, dict):
            profiles = [
                (name, prof)
                for name, prof in raw_profiles.items()
                if isinstance(prof, dict)
            ]
            profiles.sort(key=lambda item: item[1].get("ordinal_position", 0))
            columns = [name for name, _ in profiles]
        catalog[f"{schema_name}.{object_name}"] = columns
    return catalog


# ---- GET rel-config（§4.1） ----


def read_rel_config(settings: PlatformSettings, workspace_id) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace = _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace.source_id)

        _, json_path, _ = _artifact_paths(
            workspace.source_id, workspace_id, source.db_name
        )
        content, data = _load_rel_artifact(json_path)

        catalog = _load_table_columns(
            resolve_json_output_dir(workspace.source_id, workspace_id)
        )
        try:
            main_config = load_main_config_snapshot(
                resolve_project_path(METADATA_CONFIG)
            )
            threshold = cql_score_threshold(main_config)
        except ValueError as exc:
            raise RelWriteFailedError(f"主配置的置信度阈值无效：{exc}") from None
        except Exception as exc:  # noqa: BLE001 —— 配置坏属服务端问题
            raise RelWriteFailedError(
                f"主配置加载失败（{type(exc).__name__}），请重试"
            ) from None
        return {
            "database": source.db_name,
            "fingerprint": _fingerprint(content),
            "tables": sorted(catalog),
            "relationships": data["relationships"],
            "score_threshold": threshold,
        }


# ---- GET object-columns（§4.1） ----


def read_rel_object_columns(
    settings: PlatformSettings, workspace_id, schema_name: str, object_name: str
) -> Dict[str, Any]:
    workspace_id = normalize_workspace_id(workspace_id)
    with platform_session(settings) as session:
        workspace = _require_workspace(session, workspace_id)
        _require_registration(session, workspace.source_id)

        catalog = _load_table_columns(
            resolve_json_output_dir(workspace.source_id, workspace_id)
        )
        columns = catalog.get(f"{schema_name}.{object_name}")
        if columns is None:
            raise RelConfigOutputMissingError(
                f"产物中不存在对象 {schema_name}.{object_name}"
            )
        return {"columns": columns}


# ---- PUT（§4.2/§4.3） ----


def _entry_side_parts(entry: Dict[str, Any], side: str) -> Tuple[str, str, List[str]]:
    """取一侧 (schema, table, columns)：单列 from_column / 复合 from_columns 归一。"""
    table = entry.get(f"{side}_table") or {}
    columns_key = f"{side}_columns"
    if columns_key in entry:
        columns = list(entry[columns_key])
    else:
        columns = [entry[f"{side}_column"]]
    return table.get("schema", ""), table.get("table", ""), columns


def _apply_columns(entry: Dict[str, Any], columns: List[str], side: str) -> None:
    """按列数写单列/复合键形态（type 由列数推导，§4.2 通用约束）。"""
    if len(columns) == 1:
        entry[f"{side}_column"] = columns[0]
    else:
        entry[f"{side}_columns"] = columns


def _build_manual_entry(
    relationship_id: str,
    from_schema: str,
    from_table: str,
    from_columns: List[str],
    to_schema: str,
    to_table: str,
    to_columns: List[str],
    cardinality: str,
) -> Dict[str, Any]:
    """D7 手工条目定型（新增与 D8 重标共用同一口径）。"""
    entry: Dict[str, Any] = {
        "relationship_id": relationship_id,
        "type": "composite" if len(from_columns) > 1 else "single_column",
        "from_table": {"schema": from_schema, "table": from_table},
        "to_table": {"schema": to_schema, "table": to_table},
        "discovery_method": "manual",
        "target_source_type": "manual",
        "source_constraint": None,
        "composite_score": 1.0,
        "confidence_level": "high",
        "cardinality": cardinality,
    }
    _apply_columns(entry, from_columns, "from")
    _apply_columns(entry, to_columns, "to")
    return entry


def _validate_incoming_item(
    item: Any, catalog: Dict[str, List[str]]
) -> Tuple[Optional[str], str, str, List[str], str, str, List[str], str]:
    """校验单个客户端条目的实质字段，返回 (rid, from..., to..., cardinality)。"""
    if not isinstance(item, dict):
        raise RelConfigInvalidError("relationships 每项必须是对象", "条目不是对象")

    def _side(side: str) -> Tuple[str, str, List[str]]:
        table = item.get(f"{side}_table")
        if not isinstance(table, dict):
            raise RelConfigInvalidError(f"{side}_table 必须是对象", "缺少表信息")
        schema = table.get("schema")
        name = table.get("table")
        if not isinstance(schema, str) or not schema.strip():
            raise RelConfigInvalidError(
                f"{side}_table.schema 必须是非空字符串", "schema 缺失或为空"
            )
        if not isinstance(name, str) or not name.strip():
            raise RelConfigInvalidError(
                f"{side}_table.table 必须是非空字符串", "表名缺失或为空"
            )
        columns = item.get(f"{side}_columns")
        if not isinstance(columns, list) or not columns:
            raise RelConfigInvalidError(
                f"{side}_columns 必须是非空列表", "列清单缺失或为空"
            )
        for column in columns:
            if not isinstance(column, str) or not column.strip():
                raise RelConfigInvalidError(
                    f"{side}_columns 每项必须是非空字符串", "列名缺失或为空"
                )
        return schema, name, list(columns)

    from_schema, from_table, from_columns = _side("from")
    to_schema, to_table, to_columns = _side("to")

    if len(from_columns) != len(to_columns):
        raise RelConfigInvalidError(
            "两侧列数必须一致", f"源 {len(from_columns)} 列 / 目标 {len(to_columns)} 列"
        )

    cardinality = item.get("cardinality")
    if cardinality not in VALID_CARDINALITIES:
        raise RelConfigInvalidError(
            f"基数必须是 {'/'.join(sorted(VALID_CARDINALITIES))}",
            f"检测到基数 {cardinality!r}",
        )

    for schema, table in (
        (from_schema, from_table),
        (to_schema, to_table),
    ):
        if f"{schema}.{table}" not in catalog:
            raise RelConfigInvalidError(
                f"表 {schema}.{table} 不在作业画像清单内", "表名不在表清单"
            )
    for side, schema, table, columns in (
        ("源", from_schema, from_table, from_columns),
        ("目标", to_schema, to_table, to_columns),
    ):
        allowed = catalog[f"{schema}.{table}"]
        for column in columns:
            if column not in allowed:
                raise RelConfigInvalidError(
                    f"列名 {column} 不在表 {schema}.{table} 的列清单内",
                    f"{side}列名不在列清单",
                )

    rid = item.get("relationship_id")
    if rid is not None and (not isinstance(rid, str) or not rid.strip()):
        raise RelConfigInvalidError("relationship_id 必须是非空字符串", "id 形态不合法")
    return (
        rid,
        from_schema,
        from_table,
        from_columns,
        to_schema,
        to_table,
        to_columns,
        cardinality,
    )


def _validate_and_build(
    existing_entries: List[Dict[str, Any]],
    incoming: Any,
    catalog: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """§4.2 第 2 步：逐条校验并构造落盘列表。

    - 匹配既有 id 且实质字段未变 → 原条目原样保留（非实质字段服务端原值
      覆盖——客户端带来的分数/明细/方法一律忽略）；
    - 实质字段有变更 → D8 重标（manual/1.0/high/target=manual/constraint=
      null/去 metrics 与 constraint_name、保留 id）；
    - 无 id → D7 新增（服务端生成 rel_manual_<hex10>）；
    - 未知 id → 400（防前端 diff 错乱把本地临时 id 写进产物）。
    """
    if not isinstance(incoming, list):
        raise RelConfigInvalidError(
            "relationships 必须是列表", "relationships 不是列表"
        )

    by_id = {
        entry["relationship_id"]: entry
        for entry in existing_entries
        if isinstance(entry, dict) and isinstance(entry.get("relationship_id"), str)
    }
    seen_ids: set = set()
    seen_endpoints: set = set()
    built: List[Dict[str, Any]] = []

    for item in incoming:
        (
            rid,
            from_schema,
            from_table,
            from_columns,
            to_schema,
            to_table,
            to_columns,
            cardinality,
        ) = _validate_incoming_item(item, catalog)

        if rid is None:
            entry = _build_manual_entry(
                f"rel_manual_{uuid4().hex[:10]}",
                from_schema,
                from_table,
                from_columns,
                to_schema,
                to_table,
                to_columns,
                cardinality,
            )
        elif rid not in by_id:
            raise RelConfigInvalidError(
                f"未知 relationship_id: {rid}", "id 不在当前产物且非新增（请重新加载）"
            )
        else:
            original = by_id[rid]
            (
                orig_fs,
                orig_ft,
                orig_fc,
                orig_ts,
                orig_tt,
                orig_tc,
            ) = (
                *_entry_side_parts(original, "from"),
                *_entry_side_parts(original, "to"),
            )
            orig_cardinality = original.get("cardinality")
            if (
                orig_fs,
                orig_ft,
                orig_fc,
                orig_ts,
                orig_tt,
                orig_tc,
                orig_cardinality,
            ) == (
                from_schema,
                from_table,
                from_columns,
                to_schema,
                to_table,
                to_columns,
                cardinality,
            ):
                # 实质字段一致：非实质字段一律以服务端原值覆盖回写
                entry = json.loads(json.dumps(original, ensure_ascii=False))
            else:
                entry = _build_manual_entry(
                    rid,
                    from_schema,
                    from_table,
                    from_columns,
                    to_schema,
                    to_table,
                    to_columns,
                    cardinality,
                )

        final_id = entry["relationship_id"]
        if final_id in seen_ids:
            raise RelConfigInvalidError(
                f"relationship_id 重复: {final_id}", "全表 id 唯一性不满足"
            )
        endpoint = (
            (from_schema, from_table, tuple(from_columns)),
            (to_schema, to_table, tuple(to_columns)),
        )
        if endpoint in seen_endpoints:
            raise RelConfigInvalidError(
                "这条关系已存在",
                f"{from_schema}.{from_table}({', '.join(from_columns)}) → "
                f"{to_schema}.{to_table}({', '.join(to_columns)})",
            )
        seen_ids.add(final_id)
        seen_endpoints.add(endpoint)
        built.append(entry)

    return built


def _recalculate_statistics(
    statistics: Dict[str, Any], entries: List[Dict[str, Any]]
) -> None:
    """§4.3 基础计数重算（口径对齐采集侧 _calculate_statistics_v32 的 json 谓词）。"""
    total = len(entries)
    foreign_key = len(
        [e for e in entries if e.get("discovery_method") == "foreign_key_constraint"]
    )
    composite = len([e for e in entries if e.get("type") == "composite"])
    single = len([e for e in entries if e.get("type") == "single_column"])
    if composite + single != total:  # 不变式自检（防御，正常不可达）
        raise RelConfigInvalidError(
            "关系类型推导与总数不一致",
            f"composite+single={composite + single} != total={total}",
        )
    statistics["total_relationships_found"] = total
    statistics["foreign_key_relationships"] = foreign_key
    statistics["composite_key_relationships"] = composite
    statistics["single_column_relationships"] = single


def _replace_file(src: Path, dst: Path) -> None:
    """原子替换（独立函数便于失败注入测试，doc 23.2 A25）。"""
    os.replace(src, dst)


def _make_temp_file(directory: Path) -> Tuple[int, Path]:
    import tempfile

    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-rel-", dir=str(directory))
    return fd, Path(name)


def write_rel_config(
    settings: PlatformSettings, workspace_id, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """PUT：工作区行锁下的指纹并发 + 校验 + 文件级备份 + 两阶段写回（§4.2/§4.3、doc 6.2 §5.2）。"""
    workspace_id = normalize_workspace_id(workspace_id)

    # 主配置快照（渲染阈值用，§5.2；锁外加载，只读）
    try:
        main_config = load_main_config_snapshot(resolve_project_path(METADATA_CONFIG))
    except Exception as exc:  # noqa: BLE001 —— 配置坏属服务端问题
        raise RelWriteFailedError(
            f"主配置加载失败（{type(exc).__name__}），请重试"
        ) from None

    with platform_session(settings) as session:
        from backend.persistence.models import WorkspaceInfo
        from backend.workspace_files.backups import (
            EditBackupFailedError,
            backup_files_before_edit,
        )

        workspace_row = session.get(WorkspaceInfo, workspace_id, with_for_update=True)
        if workspace_row is None:
            raise RelConfigOutputMissingError("工作区不存在")
        _require_workspace(session, workspace_id)
        source = _require_registration(session, workspace_row.source_id)
        if not source.is_active:
            raise MetaLoadSourceDisabledError("指定数据源已禁用，不能编辑 Rel 产物")

        rel_dir, json_path, md_path = _artifact_paths(
            workspace_row.source_id, workspace_id, source.db_name
        )

        # 1. 指纹核对（§4.2 第 1 步）+ 解析
        content, data = _load_rel_artifact(json_path)
        if not isinstance(payload, dict) or payload.get("fingerprint") != _fingerprint(
            content
        ):
            raise RelRegeneratedError()

        # 2. 逐条校验与构造（§4.2 第 2 步）
        catalog = _load_table_columns(
            resolve_json_output_dir(workspace_row.source_id, workspace_id)
        )
        entries = _validate_and_build(
            data["relationships"], payload.get("relationships"), catalog
        )

        # 3. json 内容构造（§4.3：statistics 重算、遥测与顶层字段保留）
        data["relationships"] = entries
        _recalculate_statistics(data["statistics"], entries)
        new_json_text = json.dumps(data, ensure_ascii=False, indent=2)

        # 4+5. 渲染与两阶段写回（§4.3）：渲染→临时文件→替换（先 md 后 json）。
        # 渲染/写临时文件失败：两份原文件未动；替换失败：至多 md 领先、
        # json 未变 ⟹ 指纹有效 ⟹ 重试即恢复（对 md 为幂等重渲染）。
        # 文件级备份（doc 6.2 §5.2）：只复制本次将被覆盖的两份旧版；
        # 失败则本次保存终止，不写回、不删除任何现有内容，不触碰 marker。
        try:
            backup_files_before_edit(rel_dir, [json_path.name, md_path.name])
        except EditBackupFailedError as exc:
            raise RelWriteFailedError(
                f"Rel 编辑备份失败，本次保存未执行：{exc}"
            ) from None

        json_tmp: Optional[Path] = None
        md_tmp: Optional[Path] = None
        stage = "render"
        try:
            decision = main_config.get("decision") or {}
            relations = [relation_from_json_entry(entry) for entry in entries]
            md_text = render_markdown_report(
                relations,
                generated_by=data.get("generated_by") or "rel",
                database_name=data.get("database") or source.db_name,
                high_confidence_threshold=decision.get(
                    "high_confidence_threshold", 0.90
                ),
                medium_confidence_threshold=decision.get(
                    "medium_confidence_threshold", 0.80
                ),
            )
            json_tmp_fd, json_tmp = _make_temp_file(rel_dir)
            md_tmp_fd, md_tmp = _make_temp_file(rel_dir)
            for fd, tmp, text in (
                (json_tmp_fd, json_tmp, new_json_text),
                (md_tmp_fd, md_tmp, md_text),
            ):
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.chmod(tmp, 0o644)
            stage = "replace"
            _replace_file(md_tmp, md_path)
            _replace_file(json_tmp, json_path)
        except Exception:
            for tmp in (json_tmp, md_tmp):
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            if stage == "render":
                raise RelWriteFailedError(
                    "Rel 编辑写回失败：渲染或临时文件写入失败，"
                    "两份原文件未动、指纹未失效，请直接重试"
                ) from None
            raise RelWriteFailedError(
                "Rel 编辑写回失败：JSON 未写入（至多 md 领先，无消费者），"
                "指纹未失效，请直接重试"
            ) from None

        return {"fingerprint": _fingerprint(new_json_text), "relationships": entries}
