"""一次性迁移脚本单元测试（doc 4 §4.8.1）

fake manager 覆盖：预检（归属/唯一所属/端点一致/实体键冲突/逻辑键归一）、
回填事务（单事务 UNWIND + 核验）、约束替换语句、迁移后核验、main 的
默认只读 / --apply 前置 / 失败非零退出与报告留档。
"""

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # metaweave 项目根
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migrate_neo4j_source_identity.py"

SOURCE = "0f0e8d64-9d3a-4d0f-8d5f-2a3b4c5d6e7f"
PUBLISH = "1a2b3c4d-5e6f-4a5b-8c9d-0e1f2a3b4c5d"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "migrate_neo4j_source_identity", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mig():
    return _load_script()


class _Record(dict):
    def single(self):
        return self


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def single(self):
        return self._rows[0] if self._rows else None

    def consume(self):
        return None


# 查询路由：规范化文本按序匹配标记 → data 键（顺序敏感：具体标记在前）
_ROUTES: list[tuple[str, str]] = [
    ("SHOW CONSTRAINTS", "constraints"),
    ("nodes_missing", "backfill_verify"),
    ("-[h:HAS_COLUMN]->", "has_column"),
    ("-[r:JOIN_ON]->", "join_on"),
    ("r.source_id IS NULL", "edge_unattributed"),
    ("IS NOT NULL", "source_counts"),
    ("t.publish_id", "tables"),
    ("Column) RETURN c.id", "columns"),
    ("IS NULL", "unattributed"),
]


class _Session:
    def __init__(self, data: dict):
        self.data = data
        self.run_log: list[tuple[str, dict]] = []

    def run(self, query, **params):
        text = " ".join(query.split())
        self.run_log.append((text, params))
        for marker, key in _ROUTES:
            if marker in text:
                return _Result([dict(r) for r in self.data.get(key, [])])
        return _Result([])


class _Tx(_Session):
    pass


class FakeManager:
    """按查询标记路由的连接管理器假实现（见 _ROUTES）。"""

    def __init__(self, data: dict | None = None):
        self.data = {
            "constraints": [],
            "tables": [],
            "columns": [],
            "has_column": [],
            "join_on": [],
            "source_counts": [],
            "unattributed": [],
            "edge_unattributed": [],
            "backfill_verify": [],
            **(data or {}),
        }
        self.constraint_statements: list[str] = []
        self.sessions: list[_Session] = []
        self.write_tx_log: list[list[tuple[str, dict]]] = []
        self._cleanup_execute_write = None

    @contextmanager
    def get_session(self, database=None):
        session = _Session(self.data)
        self.sessions.append(session)
        yield session

    def execute_write_transaction(self, query, parameters=None, database=None):
        self.constraint_statements.append(query)

    def install_execute_write(self):
        """为 _Session 装 execute_write（apply_backfill_transaction 使用）。"""
        manager = self

        def _execute_write(session_self, work):
            tx = _Tx(session_self.data)
            try:
                result = work(tx)
            except Exception:
                manager.write_tx_log.append(tx.run_log)
                raise
            manager.write_tx_log.append(tx.run_log)
            return result

        _Session.execute_write = _execute_write

        def _cleanup():
            if hasattr(_Session, "execute_write"):
                del _Session.execute_write

        self._cleanup_execute_write = _cleanup
        return _cleanup

    def close(self):
        if self._cleanup_execute_write is not None:
            self._cleanup_execute_write()


def _base_data():
    """一个健康的一源旧图：8 表风格缩样（1 表 1 列 1 边）。"""
    return {
        "tables": [
            {
                "id": "orders.public.orders",
                "full_name": "public.orders",
                "source_id": SOURCE,
                "publish_id": PUBLISH,
                "entity_id": None,
            }
        ],
        "columns": [
            {
                "id": "orders.public.orders.id",
                "full_name": "public.orders.id",
                "entity_id": None,
                "source_id": None,
            }
        ],
        "has_column": [
            {
                "table_full_name": "public.orders",
                "column_full_name": "public.orders.id",
                "table_source_id": SOURCE,
                "table_publish_id": PUBLISH,
                "edge_source_id": None,
                "edge_publish_id": None,
            }
        ],
        "join_on": [],
        "source_counts": [
            {"source_id": SOURCE, "label": "Table", "count": 1},
            {"source_id": SOURCE, "label": "Column", "count": 1},
        ],
        "unattributed": [
            {"label": "Table", "count": 1},
            {"label": "Column", "count": 1},
        ],
        "backfill_verify": [{"nodes_missing": 0, "edges_missing": 0}],
        "constraints": [
            {
                "name": "table_id",
                "type": "UNIQUENESS",
                "entityType": "Table",
                "properties": ["id"],
            },
        ],
    }


class TestPrecheck:
    def test_healthy_old_graph_passes(self, mig):
        manager = FakeManager(_base_data())
        report = mig.collect_precheck(manager)
        assert report["ok"] is True
        assert report["errors"] == []
        assert report["counts"] == {
            "tables": 1,
            "columns": 1,
            "has_column": 1,
            "join_on": 0,
        }

    def test_table_missing_source_id_aborts(self, mig):
        data = _base_data()
        data["tables"][0]["source_id"] = None
        report = mig.collect_precheck(FakeManager(data))
        assert report["ok"] is False
        assert any("缺少 source_id" in e for e in report["errors"])

    def test_orphan_column_aborts(self, mig):
        data = _base_data()
        data["has_column"] = []
        report = mig.collect_precheck(FakeManager(data))
        assert any("没有所属 Table" in e for e in report["errors"])

    def test_column_with_two_owners_aborts(self, mig):
        data = _base_data()
        data["tables"].append(
            {
                "id": "orders.public.orders2",
                "full_name": "public.orders2",
                "source_id": SOURCE,
                "publish_id": PUBLISH,
                "entity_id": None,
            }
        )
        data["has_column"].append(
            {
                "table_full_name": "public.orders2",
                "column_full_name": "public.orders.id",
                "table_source_id": SOURCE,
                "table_publish_id": PUBLISH,
                "edge_source_id": None,
                "edge_publish_id": None,
            }
        )
        report = mig.collect_precheck(FakeManager(data))
        assert any("多个所属 Table" in e for e in report["errors"])

    def test_join_on_cross_source_aborts(self, mig):
        data = _base_data()
        other = str(uuid4())
        data["tables"].append(
            {
                "id": "orders.public.orders2",
                "full_name": "public.orders2",
                "source_id": other,
                "publish_id": PUBLISH,
                "entity_id": None,
            }
        )
        data["join_on"] = [
            {
                "src_full_name": "public.orders",
                "dst_full_name": "public.orders2",
                "src_source_id": SOURCE,
                "dst_source_id": other,
                "src_publish_id": PUBLISH,
                "dst_publish_id": PUBLISH,
                "relationship_id": "rel_x",
                "edge_source_id": None,
                "edge_publish_id": None,
            }
        ]
        report = mig.collect_precheck(FakeManager(data))
        assert any("端点跨源" in e for e in report["errors"])

    def test_existing_entity_id_conflict_aborts(self, mig):
        data = _base_data()
        data["tables"][0]["entity_id"] = "wrong:orders.public.orders"
        report = mig.collect_precheck(FakeManager(data))
        assert any("entity_id 与期望冲突" in e for e in report["errors"])

    def test_logical_id_shape_mismatch_aborts(self, mig):
        data = _base_data()
        data["tables"][0]["id"] = "public.orders"  # 两段旧键
        report = mig.collect_precheck(FakeManager(data))
        assert any("三段" in e or "拼接不一致" in e for e in report["errors"])

    def test_dotted_db_name_in_id_aborts(self, mig):
        data = _base_data()
        data["tables"][0]["id"] = "my.orders.public.orders"
        data["tables"][0]["full_name"] = "public.orders"
        report = mig.collect_precheck(FakeManager(data))
        # 含点号库名使 id 分段错位——按拼接不一致（或前缀非法）中止
        assert any("db 前缀非法" in e or "拼接不一致" in e for e in report["errors"])


class TestApplyBackfill:
    def test_backfill_runs_updates_and_verification_in_one_tx(self, mig):
        manager = FakeManager(_base_data())
        cleanup = manager.install_execute_write()
        try:
            precheck = mig.collect_precheck(manager)
            assert precheck["ok"]
            result = mig.apply_backfill_transaction(manager, precheck)
        finally:
            cleanup()
        assert result == {"tables": 1, "columns": 1, "has_column": 1, "join_on": 0}
        assert len(manager.write_tx_log) == 1  # 单一数据事务
        queries = manager.write_tx_log[0]
        texts = [q for q, _ in queries]
        assert any("SET t.entity_id" in t for t in texts)
        assert any("SET c.entity_id" in t and "c.source_id" in t for t in texts)
        assert any("SET h.source_id" in t for t in texts)
        # 事务末尾核验无归属节点/边；第二段必须 OPTIONAL MATCH——
        # 全部回填成功时普通 MATCH 零匹配会吞掉返回行，核验被误判失败
        verify_query = next(t for t in texts if "nodes_missing" in t)
        assert "OPTIONAL MATCH ()-[r:HAS_COLUMN|JOIN_ON]->()" in verify_query

    def test_backfill_verification_failure_rolls_back(self, mig):
        data = _base_data()
        # 核验查询（含 nodes_missing）返回仍有缺失 → 事务抛错
        data["backfill_verify"] = []
        manager = FakeManager(data)
        cleanup = manager.install_execute_write()
        try:
            precheck = mig.collect_precheck(manager)
            # 让核验查询走到空实现 → single() 返回 None → -1 → 抛错
            precheck["_table_meta"] = {}
            precheck["_column_owner"] = {}
            with pytest.raises(RuntimeError, match="回填核验失败"):
                mig.apply_backfill_transaction(manager, precheck)
        finally:
            cleanup()


class TestConstraintMigration:
    def test_statements_drop_old_then_create_new(self, mig):
        manager = FakeManager()
        applied = mig.apply_constraint_migration(manager)
        assert len(applied) == 6
        drops = [s for s in applied if s.startswith("DROP CONSTRAINT")]
        creates = [s for s in applied if s.startswith("CREATE CONSTRAINT")]
        assert len(drops) == 4
        assert {s.split()[2] for s in drops} == {
            "table_id",
            "table_full_name",
            "column_id",
            "column_full_name",
        }
        assert any("table_entity_id" in s for s in creates)
        assert any("column_entity_id" in s for s in creates)
        assert applied.index(drops[-1]) < applied.index(creates[0])

    def test_verify_after_apply_detects_old_constraint_left(self, mig):
        data = _base_data()
        data["constraints"] = [
            {"name": "table_id", "type": "UNIQUENESS", "properties": ["id"]},
        ]
        report = mig.verify_after_apply(FakeManager(data))
        assert report["ok"] is False
        assert any("旧约束仍存在" in p for p in report["problems"])

    def test_verify_after_apply_rejects_wrong_label(self, mig):
        """同名约束但标签错误（如 table_entity_id 约束了 Column）→ 明确报错，
        不凭名称与属性判定通过。"""
        data = _base_data()
        data["constraints"] = [
            {
                "name": "table_entity_id",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["Column"],
                "properties": ["entity_id"],
            },
            {
                "name": "column_entity_id",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["Column"],
                "properties": ["entity_id"],
            },
        ]
        data["unattributed"] = []
        report = mig.verify_after_apply(FakeManager(data))
        assert report["ok"] is False
        assert any(
            "定义异常" in p and "table_entity_id" in p for p in report["problems"]
        )

    def test_verify_after_apply_rejects_relationship_entity_type(self, mig):
        """同名约束但 entityType 非 NODE（如关系约束）→ 报错。"""
        data = _base_data()
        data["constraints"] = [
            {
                "name": "table_entity_id",
                "type": "UNIQUENESS",
                "entityType": "RELATIONSHIP",
                "labelsOrTypes": ["JOIN_ON"],
                "properties": ["entity_id"],
            },
            {
                "name": "column_entity_id",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["Column"],
                "properties": ["entity_id"],
            },
        ]
        data["unattributed"] = []
        report = mig.verify_after_apply(FakeManager(data))
        assert report["ok"] is False
        assert any("table_entity_id" in p for p in report["problems"])

    def test_verify_after_apply_passes_with_entity_constraints(self, mig):
        data = _base_data()
        data["constraints"] = [
            {
                "name": "table_entity_id",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["Table"],
                "properties": ["entity_id"],
            },
            {
                "name": "column_entity_id",
                "type": "UNIQUENESS",
                "entityType": "NODE",
                "labelsOrTypes": ["Column"],
                "properties": ["entity_id"],
            },
        ]
        data["unattributed"] = []
        report = mig.verify_after_apply(FakeManager(data))
        assert report["ok"] is True


class TestMain:
    def _run_main(self, mig, argv, monkeypatch, tmp_path):
        monkeypatch.setattr(
            mig,
            "_load_manager",
            lambda config_path: (
                FakeManager(_base_data()),
                {
                    "uri": "bolt://redacted:7687",
                    "database": "neo4j",
                },
            ),
        )
        monkeypatch.setattr(mig, "REPORT_ROOT", tmp_path / "reports")
        argv = [*argv, "--config", str(mig.DEFAULT_CONFIG)]
        return mig.main(argv)

    def test_precheck_only_writes_report_exit_zero(self, mig, monkeypatch, tmp_path):
        code = self._run_main(mig, [], monkeypatch, tmp_path)
        assert code == 0
        reports = list((tmp_path / "reports").glob("*/report.json"))
        assert len(reports) == 1
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        assert payload["mode"] == "precheck"
        assert payload["stages"][0]["stage"] == "precheck"
        assert payload["stages"][0]["ok"] is True
        assert "password" not in json.dumps(payload)

    def test_apply_requires_backup_archive(self, mig, monkeypatch, tmp_path):
        code = self._run_main(mig, ["--apply"], monkeypatch, tmp_path)
        assert code == 2

    def test_precheck_failure_exits_nonzero_with_report(
        self, mig, monkeypatch, tmp_path
    ):
        data = _base_data()
        data["tables"][0]["source_id"] = None
        monkeypatch.setattr(
            mig,
            "_load_manager",
            lambda config_path: (
                FakeManager(data),
                {
                    "uri": "bolt://redacted:7687",
                    "database": "neo4j",
                },
            ),
        )
        monkeypatch.setattr(mig, "REPORT_ROOT", tmp_path / "reports")
        code = mig.main(["--config", str(mig.DEFAULT_CONFIG)])
        assert code == 1
        reports = list((tmp_path / "reports").glob("*/report.json"))
        assert reports, "失败也留档"
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        assert payload["stages"][0]["ok"] is False

    def test_apply_happy_path(self, mig, monkeypatch, tmp_path):
        manager = FakeManager(_base_data())
        cleanup = manager.install_execute_write()
        try:
            monkeypatch.setattr(
                mig,
                "_load_manager",
                lambda config_path: (
                    manager,
                    {
                        "uri": "bolt://redacted:7687",
                        "database": "neo4j",
                    },
                ),
            )
            monkeypatch.setattr(mig, "REPORT_ROOT", tmp_path / "reports")
            # 迁移后核验读到的约束与计数
            manager.data["constraints"] = [
                {
                    "name": "table_entity_id",
                    "type": "UNIQUENESS",
                    "entityType": "NODE",
                    "labelsOrTypes": ["Table"],
                    "properties": ["entity_id"],
                },
                {
                    "name": "column_entity_id",
                    "type": "UNIQUENESS",
                    "entityType": "NODE",
                    "labelsOrTypes": ["Column"],
                    "properties": ["entity_id"],
                },
            ]
            # 回填后：无归属节点/边清零
            manager.data["unattributed"] = []
            manager.data["edge_unattributed"] = []
            code = mig.main(
                [
                    "--apply",
                    "--backup-archive",
                    "/backup/dump",
                    "--config",
                    str(mig.DEFAULT_CONFIG),
                ]
            )
            assert code == 0
            report_payload = json.loads(
                next((tmp_path / "reports").glob("*/report.json")).read_text(
                    encoding="utf-8"
                )
            )
            stages = [s["stage"] for s in report_payload["stages"]]
            assert stages == ["precheck", "backfill", "constraints", "verify"]
            assert any("DROP CONSTRAINT" in s for s in manager.constraint_statements)
        finally:
            cleanup()
