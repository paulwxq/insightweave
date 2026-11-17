"""doc 6.3: real PostgreSQL lock/cleanup integration tests, with no model calls.

Run explicitly with --run-db and DBDA_PG_TEST_DATABASE=doc63_test_<suffix>.
The database must already exist and carry the dedicated ownership comment.
Only this marked test database's db_deepagent schema is created/dropped.
Credentials come from the existing platform config; no Docker is started.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langgraph.store.postgres import PostgresStore
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from src.modules.db_deepagent.config import DbAgentConfig
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.checkpoint import SessionAdvisoryLock
from src.modules.db_deepagent.persistence.maintenance import MaintenanceService
from src.modules.db_deepagent.persistence.platform_pool import (
    build_platform_pool,
    init_platform,
    make_platform_conninfo,
)
from src.modules.db_deepagent.persistence.session_guard import (
    lease_from_advisory_lock,
    validate_lease,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

_DATABASE_MARKER = "insightweave:doc63-isolated-integration:v1"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class PgCase:
    """One exclusive schema and temporary result directory in the marked DB."""

    cfg: DbAgentConfig
    pool: ConnectionPool
    archive: QaArchive
    store: PostgresStore
    session: str
    root: Path

    @property
    def conninfo(self) -> str:
        return make_platform_conninfo(self.cfg.platform_db)

    @property
    def thread(self) -> str:
        return f"dbda:{self.session}:1"

    def cleanup(self, *, apply: bool = True):
        # No checkpoint threads are seeded, so no model/graph is needed.
        return MaintenanceService(self.cfg, self.archive, None).cleanup(apply=apply)

    def runs(self) -> list[str]:
        return sorted(
            s["run_id"] for s in self.archive.list_run_snapshots(self.session)
        )

    def result_rows(self) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM result_snapshots WHERE session_id = %s",
                (self.session,),
            ).fetchone()
        return row["n"]


@pytest.fixture()
def pg_case(db_agent_config: DbAgentConfig, tmp_path: Path) -> Iterator[PgCase]:
    """Fail closed before DDL unless the explicitly named test DB is marked."""
    database = os.environ.get("DBDA_PG_TEST_DATABASE", "")
    if not database:
        pytest.skip("Set DBDA_PG_TEST_DATABASE to an explicitly created test database")
    if not re.fullmatch(r"doc63_test_[a-z0-9_]{1,40}", database):
        pytest.fail("Refusing a database without the dedicated doc63_test_ prefix")
    cfg = db_agent_config.model_copy(deep=True)
    target = cfg.platform_db.model_copy(deep=True)
    target.database = target.expected_database = database
    target.schema_name = "db_deepagent"
    target.pool_min_size = 1
    target.pool_max_size = 4
    target.connect_timeout_seconds = 5
    target.statement_timeout_ms = 10000
    cfg.platform_db = target
    cfg.persistence.mode = "postgres"
    cfg.workspace.root = str(tmp_path)
    cfg.thread.archive_retention_days = 7
    cfg.thread.result_retention_days = 7
    cfg.thread.checkpoint_retention_days = 7
    admin_info = make_platform_conninfo(target, with_search_path=False)
    with psycopg.connect(admin_info, autocommit=True) as admin:
        actual, marker = admin.execute(
            "SELECT current_database(), shobj_description(oid, 'pg_database') "
            "FROM pg_database WHERE datname = current_database()"
        ).fetchone()
        if actual != database or marker != _DATABASE_MARKER:
            pytest.fail(
                "Refusing DDL: the target is not the marked integration test DB"
            )
        acquired = admin.execute(
            "SELECT pg_try_advisory_lock(hashtext('doc63_test_schema_setup'))"
        ).fetchone()[0]
        if not acquired:
            pytest.fail("Another integration run holds the test schema lease")
        admin.execute("SET lock_timeout = '3s'")
        admin.execute("DROP SCHEMA IF EXISTS db_deepagent CASCADE")
        pool = None
        try:
            init_platform(target)
            pool = build_platform_pool(target)
            pool.open(wait=True, timeout=10)
            store = PostgresStore(pool)
            yield PgCase(
                cfg=cfg,
                pool=pool,
                archive=QaArchive(store),
                store=store,
                session="s_pg_" + uuid.uuid4().hex[:12],
                root=tmp_path,
            )
        finally:
            if pool is not None:
                pool.close()
            admin.execute("DROP SCHEMA IF EXISTS db_deepagent CASCADE")


def _seed_result(case: PgCase) -> str:
    """Synthetic expired result in both the test filesystem and real PG table."""
    ref = "res_" + uuid.uuid4().hex
    old = datetime.now(UTC) - timedelta(days=20)
    meta = {
        "result_ref": ref,
        "session_id": case.session,
        "thread_id": case.thread,
        "created_at": old.isoformat(),
        "saved_row_count": 1,
    }
    path = case.root / "results" / ref
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (path / "rows.jsonl").write_text('[{"kind":"integer","value":"1"}]\n')
    with case.pool.connection() as conn:
        conn.execute(
            "INSERT INTO result_snapshots "
            "(result_ref, session_id, thread_id, saved_row_count, rows_snapshot, "
            "snapshot_row_count, meta_jsonb, created_at, last_referenced_at) "
            "VALUES (%s, %s, %s, 1, %s, 1, %s, %s, %s)",
            (ref, case.session, case.thread, Jsonb([[1]]), Jsonb(meta), old, old),
        )
    return ref


def _seed_run(case: PgCase, run: str, ref: str, *, source: str | None = None) -> None:
    """Minimal synthetic source records for maintenance, not model qualification."""
    record = {
        "session_id": case.session,
        "thread_id": case.thread,
        "run_id": run,
        "evidence_id": "E-" + run,
        "result_ref": ref,
        "purpose": "query",
        "sub_task_id": "T1",
        "sub_task_revision": 1,
        "input_result_refs": [],
    }
    if source is not None:
        record["direct_reuse_from"] = {
            "evidence_ref": {
                "session_id": case.session,
                "thread_id": case.thread,
                "run_id": source,
                "evidence_id": "E-" + source,
            },
            "source_task_ref": {
                "session_id": case.session,
                "thread_id": case.thread,
                "run_id": source,
                "sub_task_id": "T1",
                "revision": 1,
            },
        }
    case.archive.write_run_snapshot(
        case.session,
        case.thread,
        run,
        {
            "run_snapshot_schema_version": 1,
            "ledger_protocol_version": 1,
            "session_id": case.session,
            "thread_id": case.thread,
            "run_id": run,
            "sealed_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
            "tasks": [],
            "executions": [record],
            "analyses": [],
        },
    )


def _assert_no_graph_failure(report) -> None:
    assert report.errors == [], report.errors
    assert not any("缺源" in r["reason"] for r in report.skipped), report.skipped


_WORKER = r"""
import json, os, sys
from types import SimpleNamespace
import psycopg
from psycopg.rows import dict_row
from langgraph.store.postgres import PostgresStore
from src.modules.db_deepagent.config import PlatformDbConfig
from src.modules.db_deepagent.persistence.archive import QaArchive
from src.modules.db_deepagent.persistence.checkpoint import SessionAdvisoryLock
from src.modules.db_deepagent.persistence.maintenance import MaintenanceService
from src.modules.db_deepagent.persistence.session_guard import lease_from_advisory_lock, validate_lease
p = json.loads(sys.stdin.buffer.readline())
assert p['platform']['database'].startswith('doc63_test_')
def ready():
    print('DOC63_READY:' + json.dumps({'pid': os.getpid()}), flush=True)
    assert sys.stdin.buffer.readline().strip() == b'release'
if p['mode'] == 'holder':
    lock = SessionAdvisoryLock(p['conninfo'], p['session'])
    try:
        assert lock.try_acquire()
        validate_lease(lease_from_advisory_lock(lock, p['session']), p['session'])
        ready()
    finally:
        lock.release()
else:
    cfg = SimpleNamespace(
        platform_db=PlatformDbConfig.model_validate(p['platform']),
        persistence=SimpleNamespace(mode='postgres'),
        workspace=SimpleNamespace(root=p['root']),
        thread=SimpleNamespace(**p['thread']),
        maintenance=SimpleNamespace(statement_timeout_ms=10000, lock_timeout_ms=1000),
    )
    with psycopg.connect(p['conninfo'], autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        svc = MaintenanceService(cfg, QaArchive(PostgresStore(conn)), None)
        original = svc._cleanup_session_resources
        def gate(*a, **kw):
            ready()
            return original(*a, **kw)
        svc._cleanup_session_resources = gate
        report = svc.cleanup(apply=True)
        assert report.errors == [], report.errors
"""


@contextmanager
def _worker(case: PgCase, mode: str) -> Iterator[dict[str, Any]]:
    """Independent interpreter/connection; explicit ready/release handshake."""
    payload = {
        "mode": mode,
        "conninfo": case.conninfo,
        "platform": case.cfg.platform_db.model_dump(),
        "session": case.session,
        "thread": case.cfg.thread.model_dump(),
        "root": str(case.root),
    }
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", _WORKER],
        cwd=_PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        # Credentials stay on the private pipe, not argv or report output.
        proc.stdin.write(json.dumps(payload).encode() + b"\n")
        proc.stdin.flush()
        buffer = b""
        deadline = time.monotonic() + 20
        ready = None
        while ready is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                pytest.fail("PostgreSQL worker did not reach the lock handshake")
            block = os.read(proc.stdout.fileno(), 4096)
            if not block:
                pytest.fail(
                    "PostgreSQL worker exited before readiness: " + buffer.decode()
                )
            buffer += block
            for line in buffer.splitlines():
                if line.startswith(b"DOC63_READY:"):
                    ready = json.loads(line.removeprefix(b"DOC63_READY:"))
        assert ready["pid"] != os.getpid()
        yield ready
        proc.stdin.write(b"release\n")
        proc.stdin.flush()
        output, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0, output.decode()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_live_run_guard_blocks_cleanup_across_processes(pg_case: PgCase):
    ref = _seed_result(pg_case)
    _seed_run(pg_case, "source", ref)
    with _worker(pg_case, "holder"):
        other = SessionAdvisoryLock(pg_case.conninfo, pg_case.session + "_other")
        try:
            assert other.try_acquire(), "An unrelated session must remain independent"
        finally:
            other.release()
        report = pg_case.cleanup()
        assert any("互斥锁" in r["reason"] for r in report.skipped)
        assert pg_case.runs() == ["source"]
        assert pg_case.result_rows() == 1
        assert (pg_case.root / "results" / ref).is_dir()
    report = pg_case.cleanup()
    _assert_no_graph_failure(report)
    assert pg_case.runs() == []
    assert pg_case.result_rows() == 0
    assert not (pg_case.root / "results" / ref).exists()


def test_cleanup_holds_lock_until_actual_deletion(pg_case: PgCase):
    ref = _seed_result(pg_case)
    _seed_run(pg_case, "source", ref)
    contender = SessionAdvisoryLock(pg_case.conninfo, pg_case.session)
    try:
        with _worker(pg_case, "cleanup"):
            assert not contender.try_acquire()
            assert pg_case.result_rows() == 1
            assert (pg_case.root / "results" / ref).is_dir()
        assert contender.try_acquire(), "Cleanup must release its lock after deletion"
        validate_lease(
            lease_from_advisory_lock(contender, pg_case.session), pg_case.session
        )
    finally:
        contender.release()
    assert pg_case.result_rows() == 0
    assert pg_case.runs() == []


def test_retained_root_protects_sources_then_last_root_removal_reclaims(
    pg_case: PgCase,
):
    protected = _seed_result(pg_case)
    orphan = _seed_result(pg_case)
    _seed_run(pg_case, "source", protected)
    _seed_run(pg_case, "consumer", protected, source="source")
    _seed_run(pg_case, "orphan", orphan)
    pg_case.archive.write_qa(
        pg_case.session,
        "root",
        {
            "snapshot_ref": {
                "session_id": pg_case.session,
                "thread_id": pg_case.thread,
                "run_id": "consumer",
            }
        },
    )
    report = pg_case.cleanup()
    _assert_no_graph_failure(report)
    assert pg_case.runs() == ["consumer", "source"]
    assert pg_case.result_rows() == 1
    assert (pg_case.root / "results" / protected).is_dir()
    assert not (pg_case.root / "results" / orphan).exists()
    pg_case.store.delete(("db_deepagent", "qa", pg_case.session), "root")
    report = pg_case.cleanup()
    _assert_no_graph_failure(report)
    assert pg_case.runs() == []
    assert pg_case.result_rows() == 0
    assert not (pg_case.root / "results" / protected).exists()


def test_dry_run_does_not_delete_pg_rows_or_files(pg_case: PgCase):
    ref = _seed_result(pg_case)
    _seed_run(pg_case, "source", ref)
    report = pg_case.cleanup(apply=False)
    _assert_no_graph_failure(report)
    assert report.snapshot_deleted == 1  # Preview count, not actual DELETE.
    assert pg_case.result_rows() == 1
    assert pg_case.runs() == ["source"]
    assert (pg_case.root / "results" / ref).is_dir()


@pytest.mark.parametrize(
    "source,consumer", [("a-source", "z-consumer"), ("z-source", "a-consumer")]
)
def test_real_delete_error_keeps_source_and_recovers(
    pg_case: PgCase, source: str, consumer: str
):
    ref = _seed_result(pg_case)
    _seed_run(pg_case, source, ref)
    _seed_run(pg_case, consumer, ref, source=source)
    key = pg_case.archive._run_snapshot_key(pg_case.thread, consumer)
    with pg_case.pool.connection() as conn:
        prefix = conn.execute(
            "SELECT prefix FROM store WHERE key = %s", (key,)
        ).fetchone()["prefix"]
        conn.execute("CREATE SEQUENCE doc63_fail_once")
        conn.execute(sql.SQL("""
            CREATE FUNCTION doc63_reject_once() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF OLD.prefix = {prefix} AND OLD.key = {key} THEN
                IF nextval('db_deepagent.doc63_fail_once') = 1 THEN
                  RAISE EXCEPTION 'doc63 synthetic one-shot DELETE failure' USING ERRCODE = '40001';
                END IF;
              END IF;
              RETURN OLD;
            END $$
            """).format(prefix=sql.Literal(prefix), key=sql.Literal(key)))
        conn.execute(
            "CREATE TRIGGER doc63_delete_guard BEFORE DELETE ON store FOR EACH ROW EXECUTE FUNCTION doc63_reject_once()"
        )
    first = pg_case.cleanup()
    assert pg_case.runs() == sorted([source, consumer])
    assert any("清理失败" in r["reason"] for r in first.skipped)
    assert any("停止该会话本轮剩余" in r["reason"] for r in first.skipped)
    with pg_case.pool.connection() as conn:
        assert (
            conn.execute("SELECT last_value FROM doc63_fail_once").fetchone()[
                "last_value"
            ]
            == 1
        )
    second = pg_case.cleanup()
    _assert_no_graph_failure(second)
    assert pg_case.runs() == []
    with pg_case.pool.connection() as conn:
        assert (
            conn.execute("SELECT last_value FROM doc63_fail_once").fetchone()[
                "last_value"
            ]
            == 2
        )


def test_broken_source_graph_prevents_real_resource_deletion(pg_case: PgCase):
    ref = _seed_result(pg_case)
    _seed_run(pg_case, "consumer", ref, source="missing-source")
    report = pg_case.cleanup()
    assert any("缺源" in r["reason"] for r in report.skipped)
    assert any("跳过该会话全部" in r["reason"] for r in report.skipped)
    assert pg_case.runs() == ["consumer"]
    assert pg_case.result_rows() == 1
    assert (pg_case.root / "results" / ref).is_dir()
