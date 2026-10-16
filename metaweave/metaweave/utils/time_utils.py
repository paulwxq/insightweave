"""唯一共用 UTC 文本函数（doc 19.3 §2.2）。

供 `database.json`、逐表 JSON、§2.3 六处产物及后端 API、MD 运行标识直接调用：
后端导入此工具，不另写同构实现。本模块只依赖 Python 标准库，不导入后端模块。

输入必须带时区：检查 `tzinfo` 与 `utcoffset()` 均非空，先换算到 UTC，再输出
六位微秒、`Z` 结尾的 ISO 8601 文本；无时区输入抛 ``ValueError``（可空字段由
调用方处理，不传入本函数）。禁用 ``replace(tzinfo=...)`` 给本地读数补标签——
那会改变非 UTC 输入表示的瞬间。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_isoformat(value: datetime) -> str:
    """``datetime`` → ``YYYY-MM-DDTHH:MM:SS.ffffffZ``；无时区输入抛 ``ValueError``。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("utc_isoformat 需要带时区的 datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
