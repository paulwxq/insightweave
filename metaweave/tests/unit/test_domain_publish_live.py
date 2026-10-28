"""domain 发布事务的实库验证（doc 21.2 §4、6.2 §8.3 的真实事务证据）。

沿用 skipif 模式：缺少平台库环境变量时跳过。用例经 create_workspace +
create_job + create_publish_run + execute_publish_run 走完整新模型链路，
自建临时数据，结束后按外键逆序清理。
"""

import shutil
from pathlib import Path

import pytest
import json

import yaml
from sqlalchemy import text

from backend.jobs.job_service import JobConfigPayload, create_job
from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.publishing.publish_service import (
    create_publish_run,
    execute_publish_run,
)
from metaweave.core.clean_targets import resolve_workspace_root
from backend.workspaces.service import create_workspace


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


def _new_model_schema_ready(settings) -> bool:
    """平台库已按 6.1 重建（短 ID 序列 + workspace_info）才运行实库断言。

    断代改造的一次性重建（doc 6.2 §12）尚未在当前环境执行时，旧库
    （source_id 仍为 uuid、无 workspace_info）不具备新模型表结构，跳过
    而不是误报用例失败。
    """
    try:
        with platform_session(settings) as session:
            sequence = session.execute(
                text(
                    "SELECT count(*) FROM pg_sequences"
                    " WHERE schemaname = 'public'"
                    "   AND sequencename = 'db_source_info_id_seq'"
                )
            ).scalar_one()
            workspace_table = session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables"
                    " WHERE table_schema = 'public' AND table_name = 'workspace_info'"
                )
            ).scalar_one()
            return sequence == 1 and workspace_table == 1
    except Exception:
        return False


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None or not _new_model_schema_ready(SETTINGS),
    reason="缺少 PLATFORM_DB_* 环境变量或平台库尚未按 6.1 重建，跳过实库断言",
)


@pytest.fixture
def live_domain_workspace(tmp_path, monkeypatch):
    """临时数据源 + 工作区 + 发布作业（steps=["domain"]）；结束时逆序清理。

    产物目录按真实 resolver 写在项目 output/ 下（resolve_workspace_root），
    测试结束删除整个 output/<source>/ 目录。
    """
    db_name = "tx_domain_live"
    with platform_session(SETTINGS) as session:
        source_id = session.execute(
            text(
                "INSERT INTO public.db_source_info"
                " (display_name, db_type, host, port, db_name, username,"
                "  password_ciphertext, selected_schemas)"
                " VALUES ('domain 发布实库验证', 'postgresql', 'h', 5432, :dbname,"
                "  'u', 'cipher', ARRAY['public'])"
                " RETURNING source_id"
            ),
            {"dbname": db_name},
        ).scalar_one()
        session.commit()
    workspace = create_workspace(SETTINGS, str(source_id), "domain-live")
    job = create_job(
        SETTINGS,
        "publish",
        workspace["workspace_id"],
        "domain 实库发布",
        None,
        JobConfigPayload(steps=["domain"]),
    )
    try:
        yield {
            "source_id": str(source_id),
            "workspace_id": workspace["workspace_id"],
            "job_id": job["job_id"],
            "db_name": db_name,
        }
    finally:
        shutil.rmtree(
            Path(
                resolve_workspace_root(str(source_id), workspace["workspace_id"])
            ).parent,
            ignore_errors=True,
        )
        with platform_session(SETTINGS) as session:
            session.execute(
                text(
                    "DELETE FROM public.publish_run_step WHERE publish_run_id IN"
                    " (SELECT publish_run_id FROM public.publish_run_info"
                    "   WHERE publish_job_id = :jid)"
                ),
                {"jid": job["job_id"]},
            )
            session.execute(
                text("DELETE FROM public.table_domain WHERE source_id = :sid"),
                {"sid": str(source_id)},
            )
            session.execute(
                text("DELETE FROM public.publish_run_info WHERE publish_job_id = :jid"),
                {"jid": job["job_id"]},
            )
            session.execute(
                text(
                    "DELETE FROM public.publish_job_revision WHERE publish_job_id = :jid"
                ),
                {"jid": job["job_id"]},
            )
            session.execute(
                text("DELETE FROM public.publish_job_info WHERE publish_job_id = :jid"),
                {"jid": job["job_id"]},
            )
            session.execute(
                text("DELETE FROM public.workspace_info WHERE workspace_id = :ws"),
                {"ws": workspace["workspace_id"]},
            )
            session.execute(
                text("DELETE FROM public.db_source_info WHERE source_id = :sid"),
                {"sid": str(source_id)},
            )
            session.commit()


def write_artifacts(ws):
    """写 domain/md 受管产物与成功 marker（走真实 resolver 的目录）。"""
    from datetime import datetime

    from backend.workspace_files.markers import write_collection_marker
    from backend.workspace_files.timestamps import SHANGHAI_TZ

    root = resolve_workspace_root(ws["source_id"], ws["workspace_id"])
    domain_dir = root / "domain"
    md_dir = root / "md"
    domain_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    names = [f"{ws['db_name']}.public.users.md", f"{ws['db_name']}.public.products.md"]
    for name in names:
        (md_dir / name).write_text("# 表\n", encoding="utf-8")
    (md_dir / "manifest.json").write_text(
        '{"files": [' + ", ".join(f'"{n}"' for n in names) + "]}", encoding="utf-8"
    )
    payload = {
        "database": {"name": ws["db_name"], "description": "库说明"},
        "domains": [
            {
                "name": "用户管理",
                "description": "用户域",
                "tables": [
                    f"{ws['db_name']}.public.users",
                    f"{ws['db_name']}.public.products",
                ],
            },
        ],
    }
    (domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    run = write_collection_marker(
        domain_dir,
        "11111111-1111-4111-8111-111111111111",
        "success",
        moment=datetime(2026, 10, 21, 12, 0, 0, tzinfo=SHANGHAI_TZ),
    )
    write_collection_marker(
        md_dir,
        "11111111-1111-4111-8111-111111111111",
        "success",
        moment=datetime(2026, 10, 21, 12, 0, 0, tzinfo=SHANGHAI_TZ),
    )
    return run


def test_live_domain_publish_replaces_rows_in_one_commit(live_domain_workspace):
    from uuid import UUID

    ws = live_domain_workspace
    marker = write_artifacts(ws)
    result = create_publish_run(
        SETTINGS,
        ws["job_id"],
        revision_no=1,
        mode="custom",
        steps=["domain"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)

    with platform_session(SETTINGS) as session:
        rows = session.execute(
            text(
                "SELECT table_name, domain_name, publish_run_id::text"
                "  FROM public.table_domain WHERE source_id = :sid"
                " ORDER BY table_name"
            ),
            {"sid": ws["source_id"]},
        ).all()
        step = session.execute(
            text(
                "SELECT status, message, input_manifest IS NOT NULL"
                "  FROM public.publish_run_step"
                " WHERE publish_run_id = :rid AND step_name = 'domain'"
            ),
            {"rid": str(run_id)},
        ).one()
        run_status = session.execute(
            text(
                "SELECT status FROM public.publish_run_info WHERE publish_run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).scalar_one()

    assert run_status == "success"
    assert step[0] == "success"
    assert step[2] is True  # input_manifest 与成功状态同一事务落库
    assert {row[0] for row in rows} == {"users", "products"}
    assert all(row[2] == str(run_id) for row in rows)
    assert marker.status == "success"  # 发布只读采集 marker，不改写


def test_live_zero_rows_fails_step_and_keeps_nothing(live_domain_workspace):
    from uuid import UUID

    ws = live_domain_workspace
    root = resolve_workspace_root(ws["source_id"], ws["workspace_id"])
    domain_dir = root / "domain"
    md_dir = root / "md"
    domain_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    # MD 清单必须有效（doc 21.2 §4 规则 5：零表映射也要先过清单校验）
    md_name = f"{ws['db_name']}.public.users.md"
    (md_dir / md_name).write_text("# users\n", encoding="utf-8")
    (md_dir / "manifest.json").write_text(
        json.dumps({"files": [md_name]}), encoding="utf-8"
    )
    (domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump(
            {
                "database": {"name": ws["db_name"]},
                "domains": [{"name": "_未分类_", "description": "无表", "tables": []}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    from backend.workspace_files.markers import write_collection_marker

    write_collection_marker(
        domain_dir, "22222222-2222-4222-8222-222222222222", "success"
    )

    result = create_publish_run(
        SETTINGS,
        ws["job_id"],
        revision_no=1,
        mode="custom",
        steps=["domain"],
        step_config=None,
    )
    run_id = UUID(result["publish_run_id"])
    execute_publish_run(SETTINGS, run_id)

    with platform_session(SETTINGS) as session:
        step_status = session.execute(
            text(
                "SELECT status, message FROM public.publish_run_step"
                " WHERE publish_run_id = :rid AND step_name = 'domain'"
            ),
            {"rid": str(run_id)},
        ).one()
        count = session.execute(
            text("SELECT count(*) FROM public.table_domain WHERE source_id = :sid"),
            {"sid": ws["source_id"]},
        ).scalar_one()
        run_status = session.execute(
            text(
                "SELECT status FROM public.publish_run_info WHERE publish_run_id = :rid"
            ),
            {"rid": str(run_id)},
        ).scalar_one()
    assert run_status == "failed"
    assert step_status[0] == "failed"
    assert "没有表映射" in step_status[1]
    assert count == 0
