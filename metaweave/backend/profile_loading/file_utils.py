"""profile_loading 共享的加固文件读取（doc 23.2 实施修订：私有副本提为公共）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Tuple


def read_regular_text(path: Path, label: str) -> Tuple[str, str]:
    """按不跟随符号链接的方式读取普通文件文本，返回 (status, content_or_reason)。

    status：missing（文件不存在）/ invalid（不可读或非普通文件）/ ok。

    - ``O_NOFOLLOW``：拒绝最后一级符号链接（上级目录由各受管 resolver 逐级拒绝）；
    - ``O_NONBLOCK``：FIFO 打开不阻塞；
    - ``fstat`` 复核常规文件，阻断设备文件等其它形态。

    原为 dim_value_config_api 与 rel_config_api 各持一份的同契约私有副本
    （doc 22.2 / 23.2），rel_loader 又跨模块私有导入——提为此公共 helper。
    domain_loader 的同名函数是不同契约（返回 str、抛 DomainPublishInputError），
    不在合并范围。
    """

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "missing", ""
    except OSError:
        return "invalid", f"{label} 打不开或不是普通文件"

    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "invalid", f"{label} 不是普通文件"
        handle = os.fdopen(fd, "r", encoding="utf-8")
    except OSError:
        os.close(fd)
        return "invalid", f"{label} 打不开"
    with handle:
        try:
            return "ok", handle.read()
        except (OSError, UnicodeError):
            return "invalid", f"{label} 读不出"
