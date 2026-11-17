"""通用分页参数（doc 6.2 §6.1）：limit 默认 20、范围 1–100，offset 默认 0、不得为负。

独立于 jobs/workspaces 两个包，避免相互导入成环；响应统一使用
`items/total/limit/offset` 信封，total 是筛选条件下的全部行数。
"""

from dataclasses import dataclass

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def validate_page(limit: int | None, offset: int | None) -> Page:
    """分页参数校验；None 时取默认（limit 20、offset 0），越界抛 ValueError。"""
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    offset = 0 if offset is None else int(offset)
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit 必须在 1–{MAX_LIMIT} 之间")
    if offset < 0:
        raise ValueError("offset 不能为负")
    return Page(limit, offset)
