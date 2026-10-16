"""只读 REPEATABLE READ 连接配置（doc 19.2 §8.1.1/§8.2）。"""

READ_ONLY_EXECUTION_OPTIONS = {
    "isolation_level": "REPEATABLE READ",
    "postgresql_readonly": True,
}


def begin_readonly_repeatable_read(session) -> None:
    """在首条数据查询前把连接配置为 REPEATABLE READ + READ ONLY。

    必须在 platform_session 建立后、任何 ORM 查询或 ``session.get()``
    之前调用：Session 已持有连接时 ``execution_options`` 只会告警并被
    忽略，连接层对进行中的事务修改隔离级别则直接抛错（doc 19.2 §8.2）。
    psycopg 把两项设置合进下一条 ``BEGIN ISOLATION LEVEL ... READ ONLY``。
    """
    session.connection(execution_options=READ_ONLY_EXECUTION_OPTIONS)
