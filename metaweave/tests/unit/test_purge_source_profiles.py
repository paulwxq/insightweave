"""doc 8.1 §6.1：purge_source_profiles 必须覆盖五张内容表（含 table_domain）。"""

from types import SimpleNamespace

from backend.data_sources.repository import purge_source_profiles
from backend.persistence.models import (
    DbProfile,
    TableColumnProfile,
    TableDefinition,
    TableDomain,
    TableProfile,
)


class PurgeSession:
    """记录 delete 语句的目标表与 where，按池语义删除。"""

    def __init__(self):
        self.deleted_tables = []
        self.profiles = [SimpleNamespace(source_id="db_src_1")]
        self.definitions = [SimpleNamespace(source_id="db_src_1")]
        self.table_profiles = [SimpleNamespace(source_id="db_src_1")]
        self.table_domains = [SimpleNamespace(source_id="db_src_1")]
        self.table_column_profiles = [SimpleNamespace(source_id="db_src_1")]
        self.other_source = [SimpleNamespace(source_id="db_src_2")]

    def _pool(self, entity):
        return {
            DbProfile: self.profiles,
            TableDefinition: self.definitions,
            TableProfile: self.table_profiles,
            TableDomain: self.table_domains,
            TableColumnProfile: self.table_column_profiles,
        }.get(entity, self.other_source)

    def execute(self, statement):
        assert statement.__visit_name__ == "delete"
        table_name = statement.table.name
        pool = self._pool_by_name(table_name)
        self.deleted_tables.append(table_name)
        where = statement.whereclause
        keep = [
            row
            for row in pool
            if not (
                where is not None and getattr(row, "source_id") == where.right.value
            )
        ]
        removed = len(pool) - len(keep)
        pool[:] = keep
        return SimpleNamespace(rowcount=removed)

    def _pool_by_name(self, name):
        return {
            "db_profile": self.profiles,
            "table_definition": self.definitions,
            "table_profile": self.table_profiles,
            "table_domain": self.table_domains,
            "table_column_profile": self.table_column_profiles,
        }.get(name, self.other_source)


def test_purge_covers_all_five_content_tables_for_source():
    session = PurgeSession()
    result = purge_source_profiles(session, "db_src_1")
    assert session.deleted_tables == [
        "table_column_profile",
        "table_profile",
        "table_definition",
        "table_domain",
        "db_profile",
    ]
    assert session.profiles == []
    assert session.definitions == []
    assert session.table_profiles == []
    assert session.table_domains == []
    assert session.table_column_profiles == []
    # 其他源不受影响
    assert len(session.other_source) == 1
    assert result["deleted"]["table_domain"] == 1
