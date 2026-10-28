"""domain 发布步骤（doc 21.2 §4、6.2 §8）：预检、发布事务与失败语义。

发布经 create_publish_run + execute_publish_run：成功按源整份替换
table_domain（行挂 publish_run_id）；零表映射与解析失败在执行期记 failed。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.publishing.precheck import PublishPrecheckError, check_step
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from tests.unit.test_domain_publish_loader import (
    DB_NAME,
    write_md,
    write_yaml,
)
from tests.unit.test_publish_service import (
    DB_NAME as REG_DB_NAME,
    FakeSession,
    PUBLISH_JOB_ID,
    RUN,
    SETTINGS,
    SOURCE_ID,
    WORKSPACE_ID,
    install,
    run_step_row,
    write_marker,
)

assert DB_NAME == REG_DB_NAME  # 产物库名与登记库名一致


def domain_ready(tmp_path, payload=None):
    write_marker(tmp_path, "domain")
    write_yaml(tmp_path, payload if payload is not None else "default")
    write_md(tmp_path)


def _domain_job_session():
    return FakeSession(steps=("domain",))


def _publish_domain_once(session):
    result = create_publish_run(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["domain"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)
    return run_id


class TestCheckStepDomain:
    def test_ready_with_artifact_and_marker(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is True
        assert result.input_files == (("domain/db_domains.yaml", str(RUN)),)

    def test_yaml_missing_not_ready(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "domain")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "domain/db_domains.yaml 缺失或不可读"

    def test_marker_missing_beats_file_check(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_yaml(tmp_path)  # 有产物但没有 marker
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "domain/ 目录没有采集 marker，产物不可用"

    def test_zero_rows_still_ready_at_precheck(self, monkeypatch, tmp_path):
        """预检只看文件就绪（doc 21.2 §4）：零表映射留到发布执行期失败。"""
        install(monkeypatch, tmp_path)
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "_未分类_",
                    "description": "无法归入其他业务主题的表",
                    "tables": [],
                }
            ],
        }
        domain_ready(tmp_path, payload)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is True


class TestPublishDomainExecution:
    def test_success_replaces_rows_with_publish_run_id(self, monkeypatch, tmp_path):
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        old_row = SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            table_name="legacy",
            domain_name="旧域",
            publish_run_id=UUID(int=7),
        )
        other_row = SimpleNamespace(
            source_id="db_src_9",
            schema_name="public",
            table_name="keep",
            domain_name="别源域",
            publish_run_id=UUID(int=8),
        )
        session.table_domains.extend([old_row, other_row])

        run_id = _publish_domain_once(session)

        step = run_step_row(session, run_id, "domain")
        assert step.status == "success"
        assert step.message == "Domain 映射发布完成（3 行）"
        rows = [row for row in session.table_domains if row.source_id == SOURCE_ID]
        assert {(row.table_name, row.domain_name) for row in rows} == {
            ("users", "用户管理"),
            ("products", "用户管理"),
            ("users", "订单交易"),
        }
        assert all(row.publish_run_id == run_id for row in rows)
        assert other_row in session.table_domains  # 其他源不动
        assert step.target_manifest == {
            "targets": [
                {
                    "type": "postgresql",
                    "database": "metaweave",
                    "schema": "public",
                    "table": "table_domain",
                }
            ]
        }

    def test_zero_rows_fails_step_and_keeps_old_rows(self, monkeypatch, tmp_path):
        payload = {
            "database": {"name": DB_NAME},
            "domains": [
                {
                    "name": "_未分类_",
                    "description": "无法归入其他业务主题的表",
                    "tables": [],
                }
            ],
        }
        domain_ready(tmp_path, payload)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        old_row = SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            table_name="legacy",
            domain_name="旧域",
            publish_run_id=UUID(int=7),
        )
        session.table_domains.append(old_row)

        run_id = _publish_domain_once(session)

        step = run_step_row(session, run_id, "domain")
        assert step.status == "failed"
        assert step.message == "Domain 产物没有表映射，请检查产物后重试"
        assert session.table_domains == [old_row]  # 旧行保留

    def test_unparsable_yaml_fails_at_execution(self, monkeypatch, tmp_path):
        """预检不解析内容：坏 YAML 在执行期按受控异常记 failed。"""
        write_marker(tmp_path, "domain")
        write_yaml(tmp_path, text="domains: [\n")
        write_md(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        run_id = _publish_domain_once(session)
        step = run_step_row(session, run_id, "domain")
        assert step.status == "failed"
        assert "Domain 产物 YAML 无法解析" in step.message
        assert session.table_domains == []

    def test_md_manifest_missing_fails_at_execution(self, monkeypatch, tmp_path):
        write_marker(tmp_path, "domain")
        write_yaml(tmp_path)
        write_md(tmp_path, manifest=False)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        run_id = _publish_domain_once(session)
        assert run_step_row(session, run_id, "domain").status == "failed"

    def test_precheck_rejects_missing_yaml_before_run(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "domain")
        session = install(monkeypatch, tmp_path, _domain_job_session())
        with pytest.raises(PublishPrecheckError) as excinfo:
            create_publish_run(
                SETTINGS,
                PUBLISH_JOB_ID,
                revision_no=1,
                mode="custom",
                steps=["domain"],
                step_config=None,
            )
        assert excinfo.value.failures[0]["step"] == "domain"
        assert session.runs == [] and session.table_domains == []
