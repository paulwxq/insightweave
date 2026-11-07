"""受管 table_schema 输入：只读 JSON v3、MD 与采集清单，不连接数据库。"""

import json
import time
from dataclasses import dataclass, field
from typing import List

from metaweave.core.clean_targets import resolve_json_output_dir, resolve_md_output_dir
from metaweave.core.metadata.json_loader import parse_strict_stem
from metaweave.core.metadata.metadata_document import MetadataDocument
from metaweave.core.table_schema.json_extractor import JSONExtractor
from metaweave.core.table_schema.md_parser import MDParser, is_empty_description
from metaweave.core.table_schema.models import (
    LoaderOptions,
    ObjectType,
    SchemaObject,
    check_record_lengths,
)
from metaweave.utils.source_id import (
    normalize_source_id,
    normalize_workspace_id,
)


def configure_managed_loader(config, source_id, workspace_id):
    """覆盖输入路径并标注来源；集合名沿用配置，不再按源拆分；调用方传派生配置，不修改主配置。"""
    source = normalize_source_id(source_id)
    workspace = normalize_workspace_id(workspace_id)
    cfg = config.setdefault("table_schema_loader", {})
    base = str(cfg.get("collection_name", "")).strip()
    if not base:
        raise ValueError("table_schema_loader.collection_name 未配置")
    cfg.update(
        md_directory=str(resolve_md_output_dir(source, workspace)),
        json_llm_directory=str(resolve_json_output_dir(source, workspace)),
        source_id=str(source),
        job_id=str(workspace),
    )


def _manifest(directory, suffix):
    path = directory / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{suffix} 采集清单缺失或不是普通文件")
    data = json.loads(path.read_text(encoding="utf-8"))
    names = data.get("files") if isinstance(data, dict) else None
    if (
        not isinstance(names, list)
        or not names
        or any(
            not isinstance(n, str)
            or not n.endswith(suffix)
            or "/" in n
            or "\\" in n
            or n == "manifest.json"
            for n in names
        )
    ):
        raise ValueError(f"{suffix} 采集清单格式错误")
    if len(names) != len(set(names)):
        raise ValueError("采集清单条目重复")
    actual = set()
    for item in directory.glob(f"*{suffix}"):
        if item.name == "manifest.json":
            continue
        if item.is_symlink() or not item.is_file():
            raise ValueError("采集目录含非常规文件")
        actual.add(item.name)
    if actual != set(names):
        raise ValueError("采集清单与目录文件不一致")
    return data, sorted(names)


def read_managed_pairs(json_dir, md_dir, expected_json_marker=None, database_name=None):
    """返回内存快照；Web 额外核对 JSON 运行标识及登记库名。"""
    _, json_names = _manifest(json_dir, ".json")
    md_manifest, md_names = _manifest(md_dir, ".md")
    marker = md_manifest.get("source_collection_run_id")
    from metaweave.utils.source_id import normalize_run_uuid

    try:
        normalized_marker = str(normalize_run_uuid(marker)) if marker else None
    except ValueError:
        normalized_marker = None
    if normalized_marker is None:
        raise ValueError("MD 清单 source_collection_run_id 不是合法 UUID")
    if expected_json_marker is not None and normalized_marker != str(
        normalize_run_uuid(expected_json_marker)
    ):
        raise ValueError("JSON 已重跑，请重新生成 MD")
    if {n[:-5] for n in json_names} != {n[:-3] for n in md_names}:
        raise ValueError("JSON 与 MD 对象集合不一致")
    pairs = []
    for name in md_names:
        stem = parse_strict_stem(name[:-3])
        if stem is None:
            raise ValueError(f"对象文件名不是严格三段式: {name}")
        data = json.loads((json_dir / f"{name[:-3]}.json").read_text(encoding="utf-8"))
        MetadataDocument.from_dict(data)
        info = data["object_info"]
        if (info["database"], info["schema_name"], info["object_name"]) != stem:
            raise ValueError(f"JSON 身份与文件名不一致: {name}")
        if database_name is not None and stem[0] != database_name:
            raise ValueError("JSON 不属于当前登记数据库")
        pairs.append((name[:-3], (md_dir / name).read_text(encoding="utf-8"), data))
    return pairs


# 预检阶段尚无真实 publish_run_id：用 36 字节的规范化 UUID 占位，其长度与真实值一致。
_PLACEHOLDER_PUBLISH_RUN_ID = "00000000-0000-0000-0000-000000000000"


@dataclass
class PreparedTableSchemaInput:
    """共用输入准备结果。

    groups 每个元素对应一张表：首个对象是表级对象，其后是待加载的字段级对象。
    empty_desc_columns 是因空描述被跳过的完整字段对象 ID。
    """

    groups: List[List[SchemaObject]] = field(default_factory=list)
    empty_desc_columns: List[str] = field(default_factory=list)


def prepare_managed_table_schema_input(
    json_dir,
    md_dir,
    source_id,
    options: LoaderOptions,
    expected_json_marker=None,
    database_name=None,
) -> PreparedTableSchemaInput:
    """受管 JSON/MD 输入的唯一解析与字段校验入口（doc 20.1 §1.2）。

    只读文件、只处理内存数据，不初始化 Embedding / Milvus，不写入或删除任何数据。
    """
    source = str(normalize_source_id(source_id))
    result = PreparedTableSchemaInput()

    for stem, text, data in read_managed_pairs(
        json_dir, md_dir, expected_json_marker, database_name
    ):
        if not text.strip():
            raise ValueError(f"MD 内容为空: {stem}")
        parser = MDParser.from_string(text)
        columns = parser.get_column_descriptions()

        # 过滤前先做身份核对：未知字段（含空描述）与缺失字段都不能被跳过绕过。
        json_columns = set((data.get("column_profiles") or {}).keys())
        unknown = [name for name in columns if name not in json_columns]
        if unknown:
            raise ValueError(f"MD 含 JSON 中不存在的字段: {stem}: {', '.join(unknown)}")
        missing = sorted(json_columns - set(columns))
        if missing:
            raise ValueError(
                f"JSON 字段在 MD 中缺失或无法识别: {stem}: {', '.join(missing)}"
            )

        empty_names = [
            name for name, desc in columns.items() if is_empty_description(desc)
        ]
        if empty_names and not options.skip_empty_desc:
            raise ValueError(
                f"字段没有有效描述（skip_empty_desc=false）: {stem}: {', '.join(empty_names)}"
            )

        extractor = JSONExtractor.from_dict(data)
        now = int(time.time())
        objects = [
            SchemaObject(
                object_type=ObjectType.TABLE,
                object_id=stem,
                parent_id=stem,
                object_desc=parser.get_table_description().strip(),
                time_col_hint=extractor.format_time_col_hint(),
                table_category=extractor.get_table_category(),
                updated_at=now,
            )
        ]
        if options.include_columns:
            for name, desc in columns.items():
                object_id = f"{stem}.{name}"
                if is_empty_description(desc):
                    result.empty_desc_columns.append(object_id)
                    continue
                objects.append(
                    SchemaObject(
                        object_type=ObjectType.COLUMN,
                        object_id=object_id,
                        parent_id=stem,
                        object_desc=desc.strip(),
                        updated_at=now,
                    )
                )

        for obj in objects:
            check_record_lengths(
                obj.to_milvus_dict(
                    source_id=source, publish_run_id=_PLACEHOLDER_PUBLISH_RUN_ID
                )
            )
        result.groups.append(objects)

    return result
