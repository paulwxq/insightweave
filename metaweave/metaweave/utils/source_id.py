"""身份 ID 校验：数据源短 ID、工作区/作业短 ID 与运行 UUID 分开校验。

doc 6 §1 / 6.2 §4.1：`source_id` 字段名不变，值改为序列生成的 `db_src_1`
等 text 短 ID；`workspace_id`、两类作业 ID 使用 `ws_1`、`coll_job_1`、
`pub_job_1` 等短 ID；采集与发布运行 ID、用户 ID 继续使用 UUID。短 ID 与
UUID 使用不同校验函数，不得把原 UUID 检查统一改成任意字符串检查。
"""

import re
from uuid import UUID

class ShortIdError(ValueError):
    """短 ID 格式非法（路径/查询参数的客户端输入错误，HTTP 422）。"""


_SHORT_ID_KINDS = {
    "数据源": re.compile(r"^db_src_[1-9][0-9]*$"),
    "工作区": re.compile(r"^ws_[1-9][0-9]*$"),
    "采集作业": re.compile(r"^coll_job_[1-9][0-9]*$"),
    "发布作业": re.compile(r"^pub_job_[1-9][0-9]*$"),
}


def _normalize_short_id(value: object, kind: str) -> str:
    pattern = _SHORT_ID_KINDS[kind]
    if not isinstance(value, str):
        raise ShortIdError(f"{kind} ID 必须是字符串")
    if not pattern.match(value):
        raise ShortIdError(f"{kind} ID 不是合法短 ID（形如 {pattern.pattern}）")
    return value


def normalize_source_id(value: object) -> str:
    """数据源短 ID（`db_src_1` 等，doc 6.1 §13）；字段名不变，值为 text。"""
    return _normalize_short_id(value, "数据源")


def normalize_workspace_id(value: object) -> str:
    """工作区短 ID（`ws_1` 等，doc 6.1 §1）。"""
    return _normalize_short_id(value, "工作区")


def normalize_collection_job_id(value: object) -> str:
    """采集作业短 ID（`coll_job_1` 等，doc 6.1 §2）。"""
    return _normalize_short_id(value, "采集作业")


def normalize_publish_job_id(value: object) -> str:
    """发布作业短 ID（`pub_job_1` 等，doc 6.1 §6）。"""
    return _normalize_short_id(value, "发布作业")


def normalize_run_uuid(value: object) -> UUID:
    """采集/发布运行 ID：仍是 UUID（doc 6.2 §4.1）。"""
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("运行 ID 必须是 UUID 字符串")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("运行 ID 不是合法 UUID") from exc
