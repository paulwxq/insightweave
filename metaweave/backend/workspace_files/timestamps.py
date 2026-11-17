"""工作区文件时间戳：统一使用 Asia/Shanghai（UTC+08:00）、24 小时制、毫秒三位。

doc 6 §6：备份目录名 `bak_yyyyMMdd_HHmmss_SSS` 与采集 marker 文件名中的
`yyyyMMdd-HHmmssSSS` 均固定上海时区，不随部署机器默认时区变化。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now() -> datetime:
    """当前时刻（Asia/Shanghai，带时区）。"""
    return datetime.now(SHANGHAI_TZ)


def backup_dir_name(moment: datetime) -> str:
    """备份目录名 `bak_yyyyMMdd_HHmmss_SSS`。"""
    return "bak_{:%Y%m%d_%H%M%S}_{:03d}".format(moment, moment.microsecond // 1000)


def marker_timestamp(moment: datetime) -> str:
    """采集 marker 文件名时间戳 `yyyyMMdd-HHmmssSSS`。"""
    return "{:%Y%m%d-%H%M%S}{:03d}".format(moment, moment.microsecond // 1000)


def parse_marker_timestamp(text: str) -> datetime:
    """解析 marker 文件名时间戳为上海时区的 aware datetime。"""
    if len(text) != 18:
        raise ValueError(f"marker 时间戳长度不合法: {text!r}")
    raw = f"{text[:8]}{text[9:]}"
    try:
        naive = datetime.strptime(raw, "%Y%m%d%H%M%S%f")
    except ValueError as exc:
        raise ValueError(f"marker 时间戳格式不合法: {text!r}") from exc
    return naive.replace(tzinfo=SHANGHAI_TZ)
