"""manifest 构造与写入前校验（doc 6.1 §11、§11.5；doc 6.2 §8.4）。

- `input_manifest`：实际读取的工作区文件，每项 `path` + 所在目录采集
  marker 对应的 `collection_run_id`（合法 UUID 字符串，辅助文件允许
  JSON null）；path 为工作区相对路径，不含点号开头成员。
- `output_manifest`：成功生成的文件相对路径数组。
- `target_manifest`：实际写入或尝试写入的下游目标（postgresql/milvus/
  neo4j），成功与否由步骤 status 表达。
没有对应内容时字段保持 SQL NULL，不写顶层 JSON null 或 `files: null`。
非法结构与 UUID 在写入前明确报错，不静默修复。
"""

from uuid import UUID


class ManifestValidationError(ValueError):
    """manifest 结构或取值非法；写入前拒绝。"""


def _validate_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise ManifestValidationError("manifest 路径必须是非空字符串")
    if path.startswith("/") or "\\" in path:
        raise ManifestValidationError(f"manifest 路径必须是工作区相对路径: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ManifestValidationError(f"manifest 路径不合法: {path!r}")
    if any(part.startswith(".") for part in parts):
        raise ManifestValidationError(
            f"manifest 路径不得包含点号开头的文件或目录: {path!r}"
        )
    return path


def _validate_collection_run_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestValidationError("collection_run_id 必须是 UUID 字符串或 null")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ManifestValidationError("collection_run_id 不是合法 UUID") from exc


def build_input_manifest(entries: list[tuple[str, object]]) -> dict:
    """构造并校验 input_manifest；entries 为 (path, collection_run_id|None)。"""
    files = []
    for path, run_id in entries:
        files.append(
            {
                "path": _validate_relative_path(path),
                "collection_run_id": _validate_collection_run_id(run_id),
            }
        )
    return {"files": files}


def build_output_manifest(paths: list[str]) -> dict:
    """构造并校验 output_manifest（仅成功生成的文件，A19）。"""
    return {"files": [_validate_relative_path(path) for path in paths]}


_TARGET_REQUIRED_KEYS = {
    "postgresql": ("database", "schema", "table"),
    "milvus": ("collection",),
    "neo4j": ("database",),
}


def build_target_manifest(targets: list[dict]) -> dict:
    """构造并校验 target_manifest（doc 6.1 §11.3）。"""
    validated = []
    for target in targets:
        if not isinstance(target, dict):
            raise ManifestValidationError("target_manifest 每项必须是对象")
        target_type = target.get("type")
        if target_type not in _TARGET_REQUIRED_KEYS:
            raise ManifestValidationError(
                f"下游目标类型必须是 postgresql/milvus/neo4j，收到 {target_type!r}"
            )
        entry = {"type": target_type}
        for key in _TARGET_REQUIRED_KEYS[target_type]:
            value = target.get(key)
            if not isinstance(value, str) or not value:
                raise ManifestValidationError(
                    f"下游目标 {target_type} 缺少有效字段 {key}"
                )
            entry[key] = value
        validated.append(entry)
    return {"targets": validated}
