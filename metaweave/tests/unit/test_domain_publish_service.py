"""domain 发布步骤（doc 21.2 §4、6.2 §8；doc 7.2 §7 改造后）。

准入最小集：domain 目录成功 marker + 同工作区 MD 清单合法 + 本次有效
域清单（版本默认值 + 允许覆盖）校验；不再读取 db_domains.yaml 作为运行
配置。有效清单固化在运行步骤 step_config；发布事务在锁下按同一映射
规则由配置清单重算发布行并整份替换 table_domain（行挂 publish_run_id）。
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
    sample_payload,
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


def version_domains():
    """与 sample_payload 对应的运行/版本域清单（doc 7.2 §7.2 参数结构）。"""
    return sample_payload()["domains"]


def domain_version_config(domains=None):
    return {"domain": {"domains": domains if domains is not None else version_domains()}}


def _domain_job_session(domains=None):
    return FakeSession(
        steps=("domain",), step_config=domain_version_config(domains)
    )


class TestCheckStepDomain:
    def test_ready_with_marker_and_md_manifest(self, monkeypatch, tmp_path):
        """publish-check GET（无有效参数）：domain marker + MD 清单合法即就绪。"""
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        write_marker(tmp_path, "md")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is True
        assert result.input_files == (("md/manifest.json", str(RUN)),)
        assert result.payload is None

    def test_yaml_missing_still_ready(self, monkeypatch, tmp_path):
        """doc 7.2 §5.2.1：db_domains.yaml 缺失不再影响准入（有效配置来自版本/覆盖）。"""
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "domain")
        write_md(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is True

    def test_md_manifest_missing_not_ready(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_marker(tmp_path, "domain")
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is False
        assert "MD 采集清单不可用" in result.reason

    def test_marker_missing_beats_file_check(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        write_yaml(tmp_path)  # 有产物但没有 marker
        write_md(tmp_path)
        result = check_step(None, SOURCE_ID, WORKSPACE_ID, "domain", db_name=DB_NAME)
        assert result.ready is False
        assert result.reason == "domain/ 目录没有采集 marker，产物不可用"

    def test_effective_missing_domains_key(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        result = check_step(
            None,
            SOURCE_ID,
            WORKSPACE_ID,
            "domain",
            db_name=DB_NAME,
            effective_params={"domain": {}},
        )
        assert result.ready is False
        assert "缺少必需配置" in result.reason

    def test_effective_ready_payload_and_manifest(self, monkeypatch, tmp_path):
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "domain",
            db_name=DB_NAME,
            effective_params={"domain": {"domains": version_domains()}},
        )
        assert result.ready is True
        assert result.payload == {"domains": version_domains()}
        paths = [path for path, _ in result.input_files]
        assert "md/manifest.json" in paths
        assert f"md/{DB_NAME}.public.users.md" in paths
        # 不登记未读取的 Domain YAML
        assert "domain/db_domains.yaml" not in paths

    def test_effective_empty_domains_rejected(self, monkeypatch, tmp_path):
        """显式 domains: [] 不等同于缺失，沿用无有效表归属规则拒绝（§7.2）。"""
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "domain",
            db_name=DB_NAME,
            effective_params={"domain": {"domains": []}},
        )
        assert result.ready is False
        assert "未配置任何业务域" in result.reason

    def test_effective_zero_table_assignment_rejected(self, monkeypatch, tmp_path):
        """域清单全部无表归属 → 无有效表归属不可发布。"""
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        domains = [
            {
                "name": "_未分类_",
                "description": "无法归入其他业务主题的表",
                "tables": [],
            }
        ]
        session = install(monkeypatch, tmp_path, _domain_job_session(domains))
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "domain",
            db_name=DB_NAME,
            effective_params={"domain": {"domains": domains}},
        )
        assert result.ready is False
        assert "没有任何有效表归属" in result.reason

    def test_effective_unknown_table_rejected(self, monkeypatch, tmp_path):
        """有效清单引用不在 MD 清单的表 → 拒绝并列出（§7.4 同一映射规则）。"""
        install(monkeypatch, tmp_path)
        domain_ready(tmp_path)
        domains = [
            {
                "name": "用户管理",
                "description": "负责用户基础信息",
                "tables": [f"{DB_NAME}.public.ghost"],
            }
        ]
        session = install(monkeypatch, tmp_path, _domain_job_session(domains))
        result = check_step(
            session,
            SOURCE_ID,
            WORKSPACE_ID,
            "domain",
            db_name=DB_NAME,
            effective_params={"domain": {"domains": domains}},
        )
        assert result.ready is False
        assert "ghost" in result.reason


class TestPublishDomainExecution:
    def test_success_replaces_rows_with_publish_run_id(self, monkeypatch, tmp_path):
        """成功按源整份替换；创建后改坏共享 YAML 不影响运行（验收 11/13/16）。"""
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

        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["domain"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        # 运行快照固化完整有效清单（本步骤参数子对象，无外层步骤键）
        assert run_step_row(session, run_id, "domain").step_config == {
            "domains": version_domains()
        }
        # 创建后损坏共享 YAML：执行仍按快照清单发布，不读取 YAML
        write_yaml(tmp_path, text="domains: [\n")
        execute_publish_run(SETTINGS, run_id)

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

    def test_zero_rows_rejected_before_run(self, monkeypatch, tmp_path):
        """全部域无表归属 → 创建前整单拒绝（不再是执行期失败），旧行保留。"""
        domain_ready(tmp_path)
        domains = [
            {
                "name": "_未分类_",
                "description": "无法归入其他业务主题的表",
                "tables": [],
            }
        ]
        session = install(monkeypatch, tmp_path, _domain_job_session(domains))
        old_row = SimpleNamespace(
            source_id=SOURCE_ID,
            schema_name="public",
            table_name="legacy",
            domain_name="旧域",
            publish_run_id=UUID(int=7),
        )
        session.table_domains.append(old_row)

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
        assert "没有任何有效表归属" in excinfo.value.failures[0]["reason"]
        assert session.runs == [] and session.table_domains == [old_row]

    def test_md_file_removed_blocks_at_execution(self, monkeypatch, tmp_path):
        """执行期复查外部条件：清单引用的 MD 文件在创建后失效 → blocked。"""
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["domain"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        # 创建后删除被引用的 MD 文件
        from tests.unit.test_publish_service import step_dir

        (step_dir(tmp_path, "md") / f"{DB_NAME}.public.products.md").unlink()
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "domain")
        assert step.status == "blocked"
        assert session.table_domains == []

    def test_md_manifest_removed_blocks_at_execution(self, monkeypatch, tmp_path):
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["domain"],
            step_config=None,
        )
        run_id = UUID(result["publish_run_id"])
        from tests.unit.test_publish_service import step_dir

        (step_dir(tmp_path, "md") / "manifest.json").unlink()
        execute_publish_run(SETTINGS, run_id)
        assert run_step_row(session, run_id, "domain").status == "blocked"

    def test_precheck_rejects_legacy_revision_without_domains(
        self, monkeypatch, tmp_path
    ):
        """断代（doc 7.2 §7.3）：存量版本缺少 domain.domains → 创建前整单拒绝。"""
        domain_ready(tmp_path)  # YAML 合法也不构成回退
        session = install(monkeypatch, tmp_path, FakeSession(steps=("domain",)))
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
        assert "缺少必需配置" in excinfo.value.failures[0]["reason"]
        assert session.runs == [] and session.table_domains == []

    def test_custom_override_replaces_version_domains(self, monkeypatch, tmp_path):
        """完整替换（§7.2/验收 12）：custom 覆盖清单完整固化，删去的域不补回。"""
        domain_ready(tmp_path)
        session = install(monkeypatch, tmp_path, _domain_job_session())
        override = [
            {
                "name": "订单交易",
                "description": "只保留订单交易",
                "tables": [f"{DB_NAME}.public.users"],
            }
        ]
        result = create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["domain"],
            step_config={"domain": {"domains": override}},
        )
        run_id = UUID(result["publish_run_id"])
        assert run_step_row(session, run_id, "domain").step_config == {
            "domains": override
        }
        execute_publish_run(SETTINGS, run_id)
        step = run_step_row(session, run_id, "domain")
        assert step.status == "success"
        assert step.message == "Domain 映射发布完成（1 行）"
        rows = [row for row in session.table_domains if row.source_id == SOURCE_ID]
        assert {(row.table_name, row.domain_name) for row in rows} == {
            ("users", "订单交易")
        }


# ---- 验收 24（domain 参数路径）与作业级预检（doc 7.2 §7.4/§5.2） ----


def test_step_config_for_step_outside_revision_rejected(monkeypatch, tmp_path):
    """对引用版本不包含的步骤提交覆盖 → 400，不创建运行（验收 24）。"""
    from backend.publishing.publish_service import PublishRunError

    make_ready_db_for_domain(tmp_path, monkeypatch)
    session = install(monkeypatch, tmp_path, FakeSession(steps=("db",)))
    with pytest.raises(PublishRunError):
        create_publish_run(
            SETTINGS,
            PUBLISH_JOB_ID,
            revision_no=1,
            mode="custom",
            steps=["db"],
            step_config={"domain": {"domains": version_domains()}},
        )
    assert session.runs == [] and session.run_steps == []


def make_ready_db_for_domain(tmp_path, monkeypatch):
    from tests.unit.test_publish_service import make_ready_db

    make_ready_db(tmp_path)


def test_job_precheck_domain_override_invalid_table(monkeypatch, tmp_path):
    """作业级预检：domain 覆盖引用未知表 → not ready，不创建运行。"""
    from backend.publishing.publish_service import precheck_publish_job

    domain_ready(tmp_path)
    session = install(monkeypatch, tmp_path, _domain_job_session())
    result = precheck_publish_job(
        SETTINGS,
        PUBLISH_JOB_ID,
        revision_no=1,
        mode="custom",
        steps=["domain"],
        step_config={
            "domain": {
                "domains": [
                    {
                        "name": "用户管理",
                        "description": "用户域",
                        "tables": [f"{DB_NAME}.public.ghost"],
                    }
                ]
            }
        },
    )
    step = result["steps"][0]
    assert step["ready"] is False
    assert "ghost" in step["reason"]
    assert session.runs == []
