"""解析并校验浏览器可精确匹配的 CORS origin。"""

import ipaddress
import re
from urllib.parse import urlsplit


_DNS_HOST = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


def validate_cors_origin(value: str) -> str:
    """只接受规范形式，不自动修正管理员填写的 origin。"""
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("必须是规范形式的 http/https origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("必须是规范形式的 http/https origin") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("必须是规范形式的 http/https origin")

    if ":" in hostname:
        try:
            host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
        except ValueError as exc:
            raise ValueError("主机名必须是规范形式") from exc
    elif _DNS_HOST.fullmatch(hostname):
        host = hostname
        if hostname.replace(".", "").isdigit():
            try:
                host = str(ipaddress.IPv4Address(hostname))
            except ipaddress.AddressValueError as exc:
                raise ValueError("IPv4 地址必须是规范形式") from exc
    else:
        raise ValueError("主机名必须是规范形式的 ASCII 字符串")

    if port == 0:
        raise ValueError("端口必须在 1–65535 之间")

    default_port = 80 if parsed.scheme == "http" else 443
    canonical = f"{parsed.scheme}://{host}"
    if port is not None and port != default_port:
        canonical += f":{port}"
    if value != canonical:
        raise ValueError("必须是规范形式的 http/https origin")
    return value


def parse_cors_origins(raw: str | None) -> tuple[str, ...]:
    """变量整体为空可禁用跨域；分隔后单项为空则是配置错误。"""
    if raw is None or not raw.strip():
        return ()

    origins = []
    for index, item in enumerate(raw.split(","), start=1):
        value = item.strip()
        if not value:
            raise ValueError(f"PLATFORM_CORS_ORIGINS 第 {index} 项为空")
        try:
            origins.append(validate_cors_origin(value))
        except ValueError as exc:
            raise ValueError(
                f"PLATFORM_CORS_ORIGINS 第 {index} 项不是规范形式的 origin: {exc}"
            ) from exc
    return tuple(origins)
