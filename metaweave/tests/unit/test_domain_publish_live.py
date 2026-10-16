"""domain 发布整份替换的实库验证（doc 21.2 §4/§7 的真实事务证据）。

沿用 test_domain_transaction_live.py 的 skipif 模式：缺少平台库环境变量
时跳过。用例自建临时源行、作业行、受管产物目录与旧发布状态，结束后清理：

- 成功：clear_current + 按源替换 table_domain + 成功 current 发布行一次提交；
- 零行：失败记录独立提交，旧映射与旧 current 保持不变；
- 锁下标标识已变：上抛 NOT_READY，不写任何行。
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import text

from backend.persistence.platform_db import (
    load_platform_settings_from_env,
    load_project_env,
    platform_session,
)
from backend.profile_loading.json_loader import RegistrationSnapshot
from backend.publishing.errors import PublicationStepNotReadyError
from backend.publishing.publish_service import (
    DOMAIN_EMPTY_MESSAGE,
    _publish_domain,
    publish_steps,
)


def _settings():
    load_project_env()
    try:
        return load_platform_settings_from_env()
    except Exception:
        return None


SETTINGS = _settings()
pytestmark = pytest.mark.skipif(
    SETTINGS is None, reason="缺少 PLATFORM_DB_* 环境变量，跳过平台库事务实库断言"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def live_workspace():
    """临时源行、domain 成功作业行、旧 current 与旧映射行；结束时全部清理。"""
    source_id = uuid4()
    job_id = uuid4()
    old_publish_id = uuid4()
    db_name = f"tx_domain_{source_id.hex[:8]}"
    workspace_root = PROJECT_ROOT / "output" / str(source_id)
    try:
        with platform_session(SETTINGS) as session:
            session.execute(
                text(
                    """
                    INSERT INTO public.db_source_info
                        (source_id, display_name, description, db_type, host, port,
                         db_name, username, password_ciphertext, selected_schemas, is_active)
                    VALUES
                        (:sid, :name, '旧源描述', 'postgresql', 'tx-domain-live.local', 6543,
                         :dbname, 'reader', 'cipher', ARRAY['public'], true)
                    """
                ),
                {
                    "sid": source_id,
                    "name": f"tx-domain-{source_id.hex[:8]}",
                    "dbname": db_name,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.job_info (job_id, step_name, source_id, last_status)
                    VALUES (:jid, 'domain', :sid, 'success')
                    """
                ),
                {"jid": job_id, "sid": source_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.publish_info
                        (publish_id, step_name, source_id, job_id, status, is_current_publish)
                    VALUES (:pid, 'domain', :sid, :jid, 'success', true)
                    """
                ),
                {"pid": old_publish_id, "sid": source_id, "jid": job_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO public.table_domain
                        (source_id, schema_name, table_name, domain_name, domain_desc,
                         db_name, job_id, publish_id)
                    VALUES (:sid, 'public', 'legacy', '旧域', '旧描述', :dbname, :jid, :pid)
                    """
                ),
                {
                    "sid": source_id,
                    "dbname": db_name,
                    "jid": job_id,
                    "pid": old_publish_id,
                },
            )
            session.commit()
        yield SimpleNamespace(
            source_id=source_id,
            job_id=job_id,
            old_publish_id=old_publish_id,
            db_name=db_name,
            workspace_root=workspace_root,
        )
    finally:
        with platform_session(SETTINGS) as session:
            session.execute(
                text("DELETE FROM public.table_domain WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.execute(
                text("DELETE FROM public.publish_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.execute(
                text("DELETE FROM public.job_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.execute(
                text("DELETE FROM public.db_source_info WHERE source_id = :sid"),
                {"sid": source_id},
            )
            session.commit()
        shutil.rmtree(workspace_root, ignore_errors=True)


def md_files(db_name):
    return (f"{db_name}.public.users.md", f"{db_name}.public.products.md")


def write_artifacts(ws, tables):
    """写受管 domain 产物与 md 清单；tables 为空时产出一份零行 YAML。

    tables 元素是完整的点分表名（首段必须是登记库名）。
    """
    names = md_files(ws.db_name)
    domain_dir = ws.workspace_root / str(ws.job_id) / "domain"
    md_dir = ws.workspace_root / str(ws.job_id) / "md"
    domain_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (md_dir / name).write_text("# 表\n", encoding="utf-8")
    (md_dir / "manifest.json").write_text(
        '{"files": [' + ", ".join(f'"{n}"' for n in names) + "]}", encoding="utf-8"
    )
    payload = {
        "database": {"name": ws.db_name, "description": "库说明"},
        "domains": [
            {
                "name": "_未分类_",
                "description": "无法归入其他业务主题的表",
                "tables": [],
            },
            {"name": "用户管理", "description": "用户域", "tables": list(tables)},
        ],
    }
    (domain_dir / "db_domains.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return domain_dir


def _read_back(ws):
    """新连接核验（对照 psycopg3 会话关闭即回滚的实测语义）。"""
    with platform_session(SETTINGS) as session:
        rows = session.execute(
            text(
                """
                SELECT schema_name, table_name, domain_name, domain_desc,
                       db_name, job_id, publish_id
                  FROM public.table_domain
                 WHERE source_id = :sid
                 ORDER BY table_name, domain_name
                """
            ),
            {"sid": ws.source_id},
        ).all()
        publishes = session.execute(
            text(
                """
                SELECT publish_id, status, is_current_publish, error_summary
                  FROM public.publish_info
                 WHERE source_id = :sid AND step_name = 'domain'
                 ORDER BY created_at, publish_id
                """
            ),
            {"sid": ws.source_id},
        ).all()
        return rows, publishes


def test_live_publish_replaces_rows_and_current_in_one_commit(live_workspace):
    ws = live_workspace
    write_artifacts(ws, (f"{ws.db_name}.public.users", f"{ws.db_name}.public.products"))

    result = publish_steps(SETTINGS, ws.source_id, ws.job_id, ["domain"])

    assert result["results"][0]["status"] == "success"
    publish_id = result["publish_id"]
    rows, publishes = _read_back(ws)
    assert {(row[1], row[2]) for row in rows} == {
        ("users", "用户管理"),
        ("products", "用户管理"),
    }
    assert all(str(row[6]) == publish_id for row in rows)
    assert all(str(row[5]) == str(ws.job_id) and row[4] == ws.db_name for row in rows)
    assert "legacy" not in {row[1] for row in rows}
    old = next(row for row in publishes if str(row[0]) == str(ws.old_publish_id))
    assert old[2] is False
    new = next(row for row in publishes if str(row[0]) == publish_id)
    assert new[1] == "success" and new[2] is True


def test_live_zero_rows_fails_and_keeps_old_state(live_workspace):
    ws = live_workspace
    write_artifacts(ws, ())

    result = publish_steps(SETTINGS, ws.source_id, ws.job_id, ["domain"])

    step = result["results"][0]
    assert step["status"] == "failed"
    assert step["message"] == DOMAIN_EMPTY_MESSAGE
    rows, publishes = _read_back(ws)
    assert [row[1] for row in rows] == ["legacy"]
    old = next(row for row in publishes if str(row[0]) == str(ws.old_publish_id))
    assert old[2] is True
    failed = [row for row in publishes if row[1] == "failed"]
    assert len(failed) == 1 and failed[0][2] is False
    assert failed[0][3] == DOMAIN_EMPTY_MESSAGE


def test_live_stale_marker_under_lock_writes_nothing(live_workspace):
    ws = live_workspace
    write_artifacts(ws, (f"{ws.db_name}.public.users",))

    with platform_session(SETTINGS) as session:
        source = session.execute(
            text("SELECT updated_at FROM public.db_source_info WHERE source_id = :sid"),
            {"sid": ws.source_id},
        ).scalar_one()
    registration = RegistrationSnapshot(
        updated_at=source, db_name=ws.db_name, selected_schemas=("public",)
    )

    from datetime import datetime, timezone

    stale = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(PublicationStepNotReadyError):
        _publish_domain(SETTINGS, ws.source_id, ws.job_id, uuid4(), stale, registration)

    rows, publishes = _read_back(ws)
    assert [row[1] for row in rows] == ["legacy"]
    assert len(publishes) == 1  # 只有旧 current，不写失败记录
