"""手工重置脚本的离线验证：所有数据库驱动入口默认禁止联网。

执行分支只使用模拟连接，覆盖默认不执行、库名确认、作用范围、执行顺序、
事务失败与连接释放；不会创建、删除或修改真实数据库。
"""

from contextlib import contextmanager
import os
from types import SimpleNamespace

import pytest

from backend.persistence.platform_db import PlatformSettings
from scripts import manual_reset_platform_milvus as milvus
from scripts import manual_reset_platform_neo4j as neo4j
from scripts import manual_reset_platform_pg as pg


def forbidden(*args, **kwargs):
    raise AssertionError("测试禁止使用真实数据库驱动")


@pytest.fixture(autouse=True)
def no_real_drivers(monkeypatch):
    monkeypatch.setattr(pg.psycopg, "connect", forbidden)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", forbidden)
    monkeypatch.setattr(milvus, "_lazy_import_milvus", forbidden)


@pytest.fixture
def targets():
    return {
        pg: PlatformSettings(
            "pg.test", 5432, "manual_pg", "tester", "never-log-secret", "require"
        ),
        neo4j: {
            "uri": "bolt://graph.test:7687",
            "database": "manual_graph",
            "user": "tester",
            "password": "never-log-secret",
        },
        milvus: {
            "host": "vector.test",
            "port": 19530,
            "database": "manual_vectors",
            "user": "tester",
            "password": "never-log-secret",
        },
    }


@pytest.mark.parametrize("module", [pg, neo4j, milvus])
@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_preview_never_connects_or_resets(module, argv, targets, monkeypatch, capsys):
    monkeypatch.setattr(module, "load_target", lambda: targets[module])
    monkeypatch.setattr(module, "reset_database", forbidden)
    assert module.main(argv) == 0
    output = capsys.readouterr().out
    assert "未连接数据库" in output
    assert "never-log-secret" not in output
    name = targets[module].name if module is pg else targets[module]["database"]
    assert name in output


@pytest.mark.parametrize("module", [pg, neo4j, milvus])
@pytest.mark.parametrize("confirmation", [None, "wrong_database"])
def test_execute_requires_exact_database_before_driver_access(
    module, confirmation, targets, monkeypatch
):
    monkeypatch.setattr(module, "load_target", lambda: targets[module])
    monkeypatch.setattr(module, "reset_database", forbidden)
    argv = ["--execute"]
    if confirmation is not None:
        argv += ["--confirm-database", confirmation]
    with pytest.raises(SystemExit) as error:
        module.main(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("module", [pg, neo4j, milvus])
def test_confirmation_alone_or_conflicting_modes_cannot_execute(
    module, targets, monkeypatch
):
    monkeypatch.setattr(module, "load_target", lambda: targets[module])
    monkeypatch.setattr(module, "reset_database", forbidden)
    for argv in (["--confirm-database", "anything"], ["--execute", "--dry-run"]):
        with pytest.raises(SystemExit) as error:
            module.main(argv)
        assert error.value.code == 2


@pytest.mark.parametrize(
    "name", ["postgres", "template0", "template1", "", " bad", "bad\nname", "库" * 22]
)
def test_pg_protects_system_and_ambiguous_names_before_connect(name):
    settings = PlatformSettings("pg.test", 5432, name, "tester", "secret")
    with pytest.raises(ValueError):
        pg.reset_database(settings, "CREATE TABLE public.example(id integer);")


@pytest.mark.parametrize(
    "module,name", [(neo4j, "system"), (neo4j, ""), (milvus, "default"), (milvus, "")]
)
def test_graph_and_vector_protect_system_or_missing_database(module, name, targets):
    config = {**targets[module], "database": name}
    with pytest.raises(ValueError):
        module.reset_database(config)


@pytest.mark.parametrize("content", [None, "  \n"])
def test_pg_reads_ddl_before_allowing_destruction(
    content, tmp_path, targets, monkeypatch
):
    path = tmp_path / "schema.sql"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(pg, "SQL_FILE", path)
    monkeypatch.setattr(pg, "load_target", lambda: targets[pg])
    with pytest.raises(SystemExit) as error:
        pg.main(["--execute", "--confirm-database", targets[pg].name])
    assert error.value.code == 2


class FakePG:
    def __init__(self, fail=None, can_create=True):
        self.events = []
        self.fail = fail
        self.can_create = can_create

    def connect(self, **kwargs):
        owner = self
        owner.events.append(("connect", kwargs))

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                owner.events.append(("close", kwargs["dbname"]))

            def execute(self, statement, **options):
                text = (
                    statement if isinstance(statement, str) else statement.as_string()
                )
                owner.events.append(("execute", kwargs["dbname"], text, options))
                if owner.fail and owner.fail in text:
                    raise RuntimeError("never-log-secret driver failure")
                return SimpleNamespace(fetchone=lambda: (owner.can_create,))

            @contextmanager
            def transaction(self):
                try:
                    yield
                except Exception:
                    owner.events.append(("rollback", kwargs["dbname"]))
                    raise
                else:
                    owner.events.append(("commit", kwargs["dbname"]))

        return Connection()


def test_pg_recreates_only_target_then_runs_exact_existing_ddl(targets, monkeypatch):
    fake = FakePG()
    monkeypatch.setattr(pg.psycopg, "connect", fake.connect)
    settings = targets[pg]
    ddl = pg.SQL_FILE.read_text(encoding="utf-8")
    pg.reset_database(settings, ddl)
    connects = [event[1] for event in fake.events if event[0] == "connect"]
    assert [item["dbname"] for item in connects] == ["postgres", settings.name]
    assert connects[0]["autocommit"] is True
    assert "autocommit" not in connects[1]
    assert all(item["sslmode"] == "require" for item in connects)
    commands = [event for event in fake.events if event[0] == "execute"]
    assert commands == [
        (
            "execute",
            "postgres",
            "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user",
            {},
        ),
        ("execute", "postgres", 'DROP DATABASE IF EXISTS "manual_pg"', {}),
        ("execute", "postgres", 'CREATE DATABASE "manual_pg"', {}),
        ("execute", "manual_pg", ddl, {"prepare": False}),
    ]
    assert ("commit", settings.name) in fake.events


def test_pg_missing_create_permission_preserves_original_database(targets, monkeypatch):
    fake = FakePG(can_create=False)
    monkeypatch.setattr(pg.psycopg, "connect", fake.connect)
    with pytest.raises(PermissionError):
        pg.reset_database(targets[pg], "CREATE TABLE x(id integer);")
    commands = [event[2] for event in fake.events if event[0] == "execute"]
    assert len(commands) == 1 and commands[0].startswith("SELECT")
    assert len([event for event in fake.events if event[0] == "connect"]) == 1


def test_pg_quotes_database_identifiers_instead_of_interpolating_sql(monkeypatch):
    fake = FakePG()
    monkeypatch.setattr(pg.psycopg, "connect", fake.connect)
    name = 'manual"; SELECT 1; --'
    pg.reset_database(
        PlatformSettings("test", 5432, name, "u", "p"), "CREATE TABLE x(id int);"
    )
    commands = [event[2] for event in fake.events if event[0] == "execute"]
    assert commands[1:3] == [
        'DROP DATABASE IF EXISTS "manual""; SELECT 1; --"',
        'CREATE DATABASE "manual""; SELECT 1; --"',
    ]


@pytest.mark.parametrize(
    "failure", ["DROP DATABASE", "CREATE DATABASE", "CREATE SEQUENCE"]
)
def test_pg_failure_stops_and_does_not_report_success(
    failure, targets, monkeypatch, capsys
):
    fake = FakePG(fail=failure)
    monkeypatch.setattr(pg.psycopg, "connect", fake.connect)
    monkeypatch.setattr(pg, "load_target", lambda: targets[pg])
    assert pg.main(["--execute", "--confirm-database", targets[pg].name]) == 1
    output = capsys.readouterr()
    assert "PG 重置完成" not in output.out
    assert "never-log-secret" not in output.out + output.err
    connects = [event[1] for event in fake.events if event[0] == "connect"]
    if failure == "CREATE SEQUENCE":
        assert ("rollback", targets[pg].name) in fake.events
    else:
        assert len(connects) == 1


class FakeGraph:
    def __init__(self, remaining=0, fail=False):
        self.remaining = remaining
        self.fail = fail
        self.events = []
        self.closed = False

    def driver(self, uri, **kwargs):
        self.events.append(("driver", uri, kwargs))
        return self

    @contextmanager
    def session(self, **kwargs):
        self.events.append(("session", kwargs))
        yield self

    def execute_write(self, work):
        try:
            work(self)
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def run(self, query):
        self.events.append(query)
        if self.fail:
            raise RuntimeError("never-log-secret driver failure")
        return SimpleNamespace(
            consume=lambda: None,
            single=lambda: {"remaining": self.remaining},
        )

    def close(self):
        self.closed = True


def test_neo4j_clears_and_verifies_in_same_database_transaction(targets, monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", fake.driver)
    config = targets[neo4j]
    neo4j.reset_database(config)
    assert ("session", {"database": config["database"]}) in fake.events
    assert [event for event in fake.events if isinstance(event, str)] == [
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) RETURN count(n) AS remaining",
        "MATCH ()-[r]->() RETURN count(r) AS remaining",
        "commit",
    ]
    assert fake.closed


@pytest.mark.parametrize("remaining,fail", [(1, False), (0, True)])
def test_neo4j_error_rolls_back_closes_driver_and_reports_failure(
    remaining, fail, targets, monkeypatch, capsys
):
    fake = FakeGraph(remaining=remaining, fail=fail)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", fake.driver)
    monkeypatch.setattr(neo4j, "load_target", lambda: targets[neo4j])
    assert (
        neo4j.main(["--execute", "--confirm-database", targets[neo4j]["database"]]) == 1
    )
    assert "rollback" in fake.events and "commit" not in fake.events
    assert fake.closed
    output = capsys.readouterr()
    assert "Neo4j 清理完成" not in output.out
    assert "never-log-secret" not in output.out + output.err


def fake_vector(existing=True, fail_collection=False):
    state = SimpleNamespace(
        databases={"default": {"keep_default"}, "unrelated": {"keep_other"}},
        current={},
        events=[],
        disconnected=[],
    )
    if existing:
        state.databases["manual_vectors"] = {"managed", "custom"}

    def connect(**kwargs):
        alias = kwargs["alias"]
        assert alias.startswith("manual_reset_")
        state.current[alias] = "default"
        state.events.append(("connect", alias))

    def use(name, using):
        assert name in state.databases
        state.current[using] = name
        state.events.append(("use", name))

    def drop_collection(name, using):
        state.events.append(("drop_collection", state.current[using], name))
        if fail_collection:
            raise RuntimeError("never-log-secret driver failure")
        state.databases[state.current[using]].remove(name)

    def drop_database(name, using):
        assert state.current[using] == "default"
        assert not state.databases[name]
        del state.databases[name]
        state.events.append(("drop_database", name))

    def create_database(name, using):
        assert name not in state.databases
        state.databases[name] = set()
        state.events.append(("create_database", name))

    state.modules = (
        SimpleNamespace(connect=connect, disconnect=state.disconnected.append),
        SimpleNamespace(
            list_database=lambda using: list(state.databases),
            using_database=use,
            drop_database=drop_database,
            create_database=create_database,
        ),
        None,
        None,
        lambda name, using: SimpleNamespace(drop=lambda: drop_collection(name, using)),
        None,
        SimpleNamespace(
            list_collections=lambda using: list(state.databases[state.current[using]])
        ),
    )
    return state


@pytest.mark.parametrize("existing", [True, False])
def test_milvus_recreates_only_target_as_empty_database(existing, targets, monkeypatch):
    fake = fake_vector(existing)
    monkeypatch.setattr(milvus, "_lazy_import_milvus", lambda: fake.modules)
    milvus.reset_database(targets[milvus])
    assert fake.databases == {
        "default": {"keep_default"},
        "unrelated": {"keep_other"},
        "manual_vectors": set(),
    }
    assert len(fake.disconnected) == 1
    assert fake.disconnected[0] == fake.events[0][1]
    assert ("create_database", "manual_vectors") in fake.events
    if existing:
        drops = [event for event in fake.events if event[0] == "drop_collection"]
        assert {event[2] for event in drops} == {"managed", "custom"}
        idx = fake.events.index(("drop_database", "manual_vectors"))
        assert fake.events[idx - 1] == ("use", "default")
        assert fake.events[idx + 1] == ("create_database", "manual_vectors")
    else:
        assert not any(event[0].startswith("drop_") for event in fake.events)


def test_milvus_failure_disconnects_and_does_not_recreate_or_report_success(
    targets, monkeypatch, capsys
):
    fake = fake_vector(fail_collection=True)
    monkeypatch.setattr(milvus, "_lazy_import_milvus", lambda: fake.modules)
    monkeypatch.setattr(milvus, "load_target", lambda: targets[milvus])
    assert (
        milvus.main(["--execute", "--confirm-database", targets[milvus]["database"]])
        == 1
    )
    assert fake.disconnected
    assert not any(
        event[0] in {"drop_database", "create_database"} for event in fake.events
    )
    output = capsys.readouterr()
    assert "Milvus 重置完成" not in output.out
    assert "never-log-secret" not in output.out + output.err


@pytest.mark.parametrize("stage", ["drop_database", "create_database"])
def test_milvus_database_failure_closes_connection_without_success(
    stage, targets, monkeypatch, capsys
):
    fake = fake_vector()

    def fail(*args, **kwargs):
        raise RuntimeError("never-log-secret driver failure")

    monkeypatch.setattr(fake.modules[1], stage, fail)
    monkeypatch.setattr(milvus, "_lazy_import_milvus", lambda: fake.modules)
    monkeypatch.setattr(milvus, "load_target", lambda: targets[milvus])
    assert (
        milvus.main(["--execute", "--confirm-database", targets[milvus]["database"]])
        == 1
    )
    assert fake.disconnected
    assert ("create_database", "manual_vectors") not in fake.events
    assert fake.databases["default"] == {"keep_default"}
    assert fake.databases["unrelated"] == {"keep_other"}
    if stage == "drop_database":
        assert fake.databases["manual_vectors"] == set()
    else:
        assert "manual_vectors" not in fake.databases
    output = capsys.readouterr()
    assert "Milvus 重置完成" not in output.out
    assert "never-log-secret" not in output.out + output.err


def test_pg_loads_project_env_and_honors_explicit_process_override(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "environ", {})
    env = tmp_path / ".env"
    env.write_text(
        "PLATFORM_DB_HOST=pg.test\nPLATFORM_DB_NAME=from_file\n"
        "PLATFORM_DB_USER=tester\nPLATFORM_DB_PASSWORD=env-secret\n"
        "PLATFORM_DB_SSLMODE=require\n",
    )
    monkeypatch.setattr(pg, "ENV_FILE", env)
    monkeypatch.chdir(tmp_path.parent)
    assert pg.load_target().name == "from_file"
    os.environ["PLATFORM_DB_NAME"] = "from_process"
    assert pg.load_target().name == "from_process"


@pytest.mark.parametrize("module", [neo4j, milvus])
def test_loads_only_target_yaml_section_and_not_unrelated_secrets(
    module, tmp_path, monkeypatch
):
    monkeypatch.setattr(os, "environ", {})
    env = tmp_path / ".env"
    env.write_text(
        "NEO4J_URI=bolt://graph.test:7687\nNEO4J_DATABASE=from_env_graph\n"
        "NEO4J_USER=tester\nNEO4J_PASSWORD=env-secret\n"
        "MILVUS_HOST=VECTOR.TEST\nMILVUS_PORT=19530\nMILVUS_DATABASE=from_env_vectors\n",
    )
    config = tmp_path / "metadata_config.yaml"
    config.write_text(
        "embedding:\n  api_key: ${UNRELATED_SECRET_MUST_NOT_BE_REQUIRED}\n"
        "loaders:\n  cql_loader:\n    neo4j:\n"
        "      uri: ${NEO4J_URI}\n      database: ${NEO4J_DATABASE}\n"
        "      user: ${NEO4J_USER}\n      password: ${NEO4J_PASSWORD}\n"
        "vector_database:\n  active: milvus\n  providers:\n    milvus:\n"
        "      host: ${MILVUS_HOST}\n      port: ${MILVUS_PORT}\n"
        "      database: ${MILVUS_DATABASE}\n",
    )
    monkeypatch.setattr(module, "ENV_FILE", env)
    monkeypatch.setattr(module, "CONFIG_FILE", config)
    monkeypatch.chdir(tmp_path.parent)
    result = module.load_target()
    assert result["database"] == (
        "from_env_graph" if module is neo4j else "from_env_vectors"
    )
    if module is milvus:
        assert result["host"] == "vector.test"
        assert result["port"] == 19530


def test_milvus_refuses_inactive_provider(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "vector_database:\n  active: pgvector\n  providers:\n    milvus: {}\n"
    )
    monkeypatch.setattr(milvus, "CONFIG_FILE", config)
    monkeypatch.setattr(milvus, "load_dotenv", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="不是 Milvus"):
        milvus.load_target()
