"""采集作业列表：步骤按登记时间拼接，执行时间取最近一次状态写入。"""

from datetime import datetime, timezone
from uuid import UUID

from backend.jobs.listing import JobStepRecord, assemble_collection_jobs

JOB_A = UUID("8f2c1a94-3e5b-4c7d-9e1f-2a3b4c5d6e7f")
JOB_B = UUID("5d9e2b83-7c4a-4f1e-8d2c-1b3a5c7e9f2d")
USER = UUID("11111111-2222-4333-8444-555555555555")


def _row(job_id, step, created, user=None, updated=None, status="success", db_desc=None):
    created_at = datetime.fromisoformat(created).replace(tzinfo=timezone.utc)
    updated_at = (
        created_at
        if updated is None
        else datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)
    )
    return JobStepRecord(
        job_id=job_id,
        step_name=step,
        last_status=status,
        created_at=created_at,
        updated_at=updated_at,
        run_by_user_id=user,
        db_desc=db_desc,
    )


def test_execution_time_follows_latest_status_write():
    rows = [
        _row(JOB_A, "json", "2026-09-24T10:00:00"),
        _row(JOB_A, "db", "2026-09-24T01:30:15", updated="2026-09-25T08:00:00"),
        _row(JOB_A, "ddl", "2026-09-24T02:00:00", USER),
        _row(JOB_B, "rel", "2026-09-24T12:30:00"),
        _row(JOB_B, "db", "2026-09-24T12:00:00", USER),
    ]

    items = assemble_collection_jobs(rows)

    assert [item["job_id"] for item in items] == [str(JOB_A), str(JOB_B)]
    assert items[0]["steps"] == ["db", "ddl", "json"]
    assert items[0]["executed_at"] == "2026-09-25T08:00:00.000000Z"
    assert items[0]["run_by_user_id"] is None
    assert items[1]["steps"] == ["db", "rel"]
    assert items[1]["executed_at"] == "2026-09-24T12:30:00.000000Z"
    assert items[1]["run_by_user_id"] == str(USER)


def test_empty_rows_have_no_jobs():
    assert assemble_collection_jobs([]) == []


def test_job_status_rollup_running_beats_failed_beats_success():
    rows = [
        _row(JOB_A, "db", "2026-09-24T01:00:00", status="failed"),
        _row(JOB_A, "ddl", "2026-09-24T02:00:00", status="running"),
        _row(JOB_B, "db", "2026-09-24T03:00:00", status="failed"),
        _row(JOB_B, "json", "2026-09-24T04:00:00"),
    ]
    by_job = {item["job_id"]: item["job_status"] for item in assemble_collection_jobs(rows)}
    assert by_job == {str(JOB_A): "running", str(JOB_B): "failed"}
    ok = assemble_collection_jobs([_row(JOB_A, "db", "2026-09-24T01:00:00")])
    assert ok[0]["job_status"] == "success"


# ---- doc 21.1 §7.2：每作业各自的 domain db_desc（供弹窗预填） ----


def test_domain_description_comes_from_own_job_domain_row():
    rows = [
        _row(JOB_A, "md", "2026-09-24T01:00:00"),
        _row(JOB_A, "domain", "2026-09-24T02:00:00", db_desc="订单库定制"),
        _row(JOB_B, "domain", "2026-09-24T03:00:00", db_desc="另一个作业"),
    ]
    by_job = {item["job_id"]: item for item in assemble_collection_jobs(rows)}
    assert by_job[str(JOB_A)]["domain_description"] == "订单库定制"
    assert by_job[str(JOB_B)]["domain_description"] == "另一个作业"


def test_domain_description_null_without_domain_row_or_null_value():
    rows = [
        _row(JOB_A, "md", "2026-09-24T01:00:00"),
        _row(JOB_B, "domain", "2026-09-24T02:00:00"),  # db_desc 缺省 None
    ]
    items = assemble_collection_jobs(rows)
    assert all(item["domain_description"] is None for item in items)
