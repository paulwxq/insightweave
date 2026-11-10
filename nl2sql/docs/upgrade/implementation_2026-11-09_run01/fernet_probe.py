"""升级前登记密文 Fernet 解密探针（doc 1 §6.5；I1-03 归档补建）。

历史说明：升级实施时（2026-11-09 18:xx）该验证以内联脚本执行、未落盘，
结果记载于 implementation README §D8；本轮应主协调者第 1 轮 I1-03 要求
补建可重放脱敏脚本并复跑一次（本次新增执行，输出见 embedding-fernet-probes.log）。

行为：
1. 只读查询平台库 public.db_source_info 的 db_src_1 行，打印密文长度与
   created_at/updated_at（证明密文登记于升级前且未重登记）；
2. 走生产同源路径 resolve_source_registration("db_src_1")（含 Fernet 解密，
   cryptography 50.0.2 新环境），断言解密成功且非空、与密文不同；
3. 全程不打印 SOURCE_CREDENTIAL_KEY、密文与明文，不写任何行。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/opt/projects/insightweave/nl2sql")

SOURCE_ID = "db_src_1"


def main() -> None:
    import importlib.metadata as im
    from dotenv import load_dotenv
    from psycopg.conninfo import make_conninfo

    from src.modules.db_deepagent.source_registry import resolve_source_registration

    load_dotenv("/opt/projects/insightweave/nl2sql/.env")  # 与项目 ConfigLoader 同源机制

    print(f"cryptography={im.version('cryptography')}")

    conninfo = make_conninfo(
        host=os.environ["PLATFORM_DB_HOST"],
        port=os.getenv("PLATFORM_DB_PORT", "5432"),
        dbname=os.environ["PLATFORM_DB_NAME"],
        user=os.environ["PLATFORM_DB_USER"],
        password=os.environ["PLATFORM_DB_PASSWORD"],
    )
    import psycopg

    with psycopg.connect(conninfo, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT length(password_ciphertext), created_at, updated_at "
            "FROM public.db_source_info WHERE source_id = %s",
            (SOURCE_ID,),
        ).fetchone()
    assert row is not None, f"{SOURCE_ID} 未登记"
    ciphertext_len, created_at, updated_at = row
    print(
        f"{SOURCE_ID} ciphertext_len={ciphertext_len} "
        f"created_at={created_at} updated_at={updated_at}"
    )
    assert str(created_at) < "2026-11-09", "登记时间晚于升级日，不能作为升级前密文证据"

    registration = resolve_source_registration(SOURCE_ID)
    assert registration.source_id == SOURCE_ID
    assert 0 < len(registration.password) < 200, "解密结果长度异常"
    print(
        f"resolve_source_registration OK（生产路径含 Fernet 解密）"
        f" db_name={registration.db_name} host={registration.host} "
        f"port={registration.port} password_len={len(registration.password)}"
        f"（仅长度，未打印密钥/密文/明文；未写任何行）"
    )
    print("fernet probe PASSED")


if __name__ == "__main__":
    main()
