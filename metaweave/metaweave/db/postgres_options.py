"""PostgreSQL 连接参数的公共构造规则（doc 18.1 §4.1）。

业务库与平台库共用的参数规范化：``sslmode`` 维护唯一一份 libpq 六模式
集合与校验函数，超时参数为 ``None`` 时不向驱动添加对应选项、不设应用
默认。本模块是纯函数、不建连接、不读配置，也不依赖 ``backend`` 或
元数据生成器；调用方用 ``psycopg.conninfo.make_conninfo(**kwargs)``
构造连接串，不得手工拼接含密码的文本。

日志口径（doc 18.1 §4.1）：本模块与调用方可记录 host/port/dbname/user
等非密码字段，但不得记录密码、密钥、密码长度、完整连接参数字典或
conninfo。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# libpq 支持的六种 sslmode（doc 18.1 §4.1；唯一一份模式集合，勿在其他
# 位置复制这六个字符串）。
SSL_MODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


def normalize_sslmode(value: Optional[str]) -> Optional[str]:
    """规范化并校验 sslmode；未配置返回 ``None``，非法值抛 ``ValueError``。

    ``None``、空串与纯空白表示未配置；非空输入去除首尾空白、统一为
    小写后按值域校验。值域有效只说明输入合法，不替代证书、权限及
    实际连接检查（doc 18.1 §4.1）。
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in SSL_MODES:
        raise ValueError(
            f"sslmode 非法: {normalized!r}；" f"只接受 {', '.join(sorted(SSL_MODES))}"
        )
    return normalized


def build_connect_kwargs(
    *,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    sslmode: Optional[str] = None,
    connect_timeout_seconds: Optional[int] = None,
    statement_timeout_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """构造 psycopg 连接参数字典（doc 18.1 §4.1 的唯一实现）。

    各字段作为独立参数传递，密码含空格或特殊字符时不会被解析成额外
    连接参数；``sslmode`` 经 :func:`normalize_sslmode` 规范化，两个超时
    为 ``None`` 时省略对应选项、不设默认超时——调用方自行决定是否
    传入（``db`` 采集/连接测试传 10 秒/60 秒，DDL/JSON/REL 既有路径
    不传）。
    """
    kwargs: Dict[str, Any] = {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
    }
    normalized_sslmode = normalize_sslmode(sslmode)
    if normalized_sslmode is not None:
        kwargs["sslmode"] = normalized_sslmode
    if connect_timeout_seconds is not None:
        kwargs["connect_timeout"] = connect_timeout_seconds
    if statement_timeout_ms is not None:
        kwargs["options"] = f"-c statement_timeout={statement_timeout_ms}"
    return kwargs
