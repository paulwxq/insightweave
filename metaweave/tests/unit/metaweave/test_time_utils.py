"""唯一共用 UTC 文本函数（doc 19.3 §2.2/§3）：微秒精度、Z 结尾、naive 拒绝。

一组用例覆盖 spec §5：UTC 输入、非 UTC 偏移输入、微秒非零/为零、无时区拒绝。
"""

from datetime import datetime, timedelta, tzinfo, timezone

import pytest

from metaweave.utils.time_utils import utc_isoformat


class _NoneOffset(tzinfo):
    """tzinfo 存在但 utcoffset() 为空：同样视为无时区输入。"""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


def test_utc_isoformat_formats_aware_utc_with_microseconds():
    value = datetime(2026, 10, 5, 6, 3, 7, 8123, tzinfo=timezone.utc)
    assert utc_isoformat(value) == "2026-10-05T06:03:07.008123Z"


def test_utc_isoformat_converts_offset_instead_of_dropping_it():
    value = datetime(2026, 10, 5, 14, 3, 7, 8123, tzinfo=timezone(timedelta(hours=8)))
    assert utc_isoformat(value) == "2026-10-05T06:03:07.008123Z"


def test_utc_isoformat_pads_zero_microseconds():
    value = datetime(2026, 10, 5, 6, 3, 7, 0, tzinfo=timezone.utc)
    assert utc_isoformat(value) == "2026-10-05T06:03:07.000000Z"


def test_utc_isoformat_rejects_naive_datetime():
    with pytest.raises(ValueError):
        utc_isoformat(datetime(2026, 10, 5, 6, 3, 7, 8123))


def test_utc_isoformat_rejects_tzinfo_without_offset():
    with pytest.raises(ValueError):
        utc_isoformat(datetime(2026, 10, 5, 6, 3, 7, 8123, tzinfo=_NoneOffset()))
