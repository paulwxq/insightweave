"""doc 24.1：domain 配置编辑 API。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import yaml

from backend.profile_loading.domain_config_api import (
    DomainConfigInvalidError,
    DomainConfigOutputMissingError,
    DomainConfigRegeneratedError,
    DomainConfigWriteFailedError,
    list_domain_tables,
    read_domain_config,
    write_domain_config,
)
from backend.profile_loading.domain_loader import read_domain_rows
from backend.profile_loading.json_loader import RegistrationSnapshot
from backend.data_sources.domain_step_service import verify_domain_artifact

SOURCE_ID = uuid4()
JOB_ID = uuid4()
DB_NAME = "orders"
STEP_AT = datetime(2026, 10, 9, tzinfo=timezone.utc)
SETTINGS = SimpleNamespace(database_url="fake")


class FakeSession:
    def __init__(self, step_row=None, source_row=None):
        self.step_row = step_row
        self.source_row = source_row

    def get(self, model, key):
        from backend.persistence.models import DbSourceInfo, JobInfo

        if model is JobInfo:
            return self.step_row
        if model is DbSourceInfo:
            return self.source_row
        raise AssertionError(f"unexpected model {model}")


@contextmanager
def _fake_platform_session(session):
    yield session


def _install(monkeypatch, session, tmp_path: Path | None = None):
    import backend.profile_loading.domain_config_api as api

    monkeypatch.setattr(
        api, "platform_session", lambda settings: _fake_platform_session(session)
    )
    monkeypatch.setattr(api, "assert_workspace_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "lock_db_source_row", lambda _session, _id: session.source_row)
    if tmp_path is not None:
        root = tmp_path / str(SOURCE_ID) / str(JOB_ID)

        def _domain_dir(source_id, job_id):
            return root / "domain"

        def _md_dir(source_id, job_id):
            return root / "md"

        monkeypatch.setattr(api, "resolve_domain_output_dir", _domain_dir)
        monkeypatch.setattr(api, "resolve_md_output_dir", _md_dir)


def _step_row(status="success"):
    return SimpleNamespace(
        job_id=JOB_ID,
        source_id=SOURCE_ID,
        step_name="domain",
        last_status=status,
        updated_at=STEP_AT,
        db_desc="采集描述",
    )


def _source_row(db_name=DB_NAME, schemas=("public",)):
    return SimpleNamespace(
        source_id=SOURCE_ID,
        db_name=db_name,
        is_active=True,
        selected_schemas=list(schemas),
        description="数据源描述",
        updated_at=STEP_AT,
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / str(SOURCE_ID) / str(JOB_ID)
    (root / "domain").mkdir(parents=True)
    (root / "md").mkdir(parents=True)
    return root


def _write_yaml(root: Path, text: str) -> None:
    (root / "domain" / "db_domains.yaml").write_text(text, encoding="utf-8")


def _write_manifest(root: Path, files: list[str]) -> None:
    import json

    (root / "md" / "manifest.json").write_text(
        json.dumps({"files": files}), encoding="utf-8"
    )
    for name in files:
        (root / "md" / name).write_text("# md\n", encoding="utf-8")


SAMPLE = """# 数据库业务主题配置
database:
  name: orders
  description: 库描述原文
llm_inference:
  max_domains_per_table: 3
domains:
- name: 用户管理
  description: 用户
  tables:
  - orders.public.users
"""


def _session():
    return FakeSession(_step_row(), _source_row())


def test_get_sample_and_stable_fingerprint(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _install(monkeypatch, _session(), tmp_path)
    first = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    second = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert first["fingerprint"] == second["fingerprint"]
    assert len(first["fingerprint"]) == 64
    assert first["domains"][0]["tables"] == ["orders.public.users"]
    assert first["database"]["description"] == "库描述原文"
    assert first["max_domains_per_table"] == 3


@pytest.mark.parametrize(
    "text,reason_part",
    [
        ("", "空白"),
        (": [\n", "无法解析"),
        ("[]\n", "顶层不是对象"),
        ("database: []\ndomains: []\n", "database 不是对象"),
        ("database:\n  name: orders\ndomains: {}\n", "domains 不是列表"),
        ("database:\n  name: other\n  description: x\ndomains: []\n", "库名"),
        (
            "database:\n  name: orders\n  description: x\ndomains:\n  - 不是对象\n",
            "不是对象",
        ),
        (
            "database:\n  name: orders\n  description: x\ndomains:\n  - name: '  '\n    description: 有\n",
            "名称",
        ),
        (
            "database:\n  name: orders\n  description: x\ndomains:\n"
            "  - name: 甲\n    description: '  '\n",
            "描述",
        ),
        (
            "database:\n  name: orders\n  description: x\ndomains:\n"
            "  - name: 甲\n    description: 一\n  - name: 甲\n    description: 一\n",
            "同名",
        ),
        (
            "database:\n  name: orders\n  description: x\ndomains:\n"
            "  - name: 甲\n    description: 一\n    tables: [1]\n",
            "tables",
        ),
    ],
)
def test_get_rejects_invalid_yaml(tmp_path, monkeypatch, text, reason_part):
    root = _root(tmp_path)
    _write_yaml(root, text)
    _install(monkeypatch, _session(), tmp_path)
    with pytest.raises(DomainConfigInvalidError) as exc:
        read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert "domains" not in exc.value.details
    assert reason_part in exc.value.details["reason"]


def test_get_missing_tables_key_is_empty_list(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(
        root,
        "database:\n  name: orders\n  description: x\ndomains:\n  - name: 甲\n    description: 一\n",
    )
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert view["domains"][0]["tables"] == []


def test_get_does_not_reject_table_outside_candidates(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(
        root,
        "database:\n  name: orders\n  description: x\ndomains:\n"
        "  - name: 甲\n    description: 一\n    tables: ['orders.public.gone']\n",
    )
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert view["domains"][0]["tables"] == ["orders.public.gone"]


def test_max_domains_missing_or_invalid_returns_null_and_write_keeps_original(
    tmp_path, monkeypatch
):
    root = _root(tmp_path)
    text = (
        "database:\n  name: orders\n  description: 库\n"
        "llm_inference:\n  max_domains_per_table: 0\n"
        "domains:\n  - name: 甲\n    description: 一\n    tables: []\n"
    )
    _write_yaml(root, text)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert view["max_domains_per_table"] is None
    saved = write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, _payload(view, view["domains"]))
    assert saved["max_domains_per_table"] is None
    written = yaml.safe_load((root / "domain" / "db_domains.yaml").read_text(encoding="utf-8"))
    assert written["llm_inference"]["max_domains_per_table"] == 0


def test_candidates_filter_and_manifest_errors(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(
        root,
        [
            "orders.public.users.md",
            "other.public.users.md",
            "orders.secret.users.md",
            "orders.public.md",
        ],
    )
    _install(monkeypatch, _session(), tmp_path)
    listed = list_domain_tables(SETTINGS, SOURCE_ID, JOB_ID)
    assert listed["tables"] == [{"id": "orders.public.users", "label": "public.users"}]

    (root / "md" / "manifest.json").unlink()
    with pytest.raises(DomainConfigInvalidError) as exc:
        list_domain_tables(SETTINGS, SOURCE_ID, JOB_ID)
    assert "重跑 md" in exc.value.details["reason"]
    with pytest.raises(DomainConfigInvalidError) as put_exc:
        write_domain_config(
            SETTINGS,
            SOURCE_ID,
            JOB_ID,
            _payload(read_domain_config(SETTINGS, SOURCE_ID, JOB_ID), []),
        )
    assert "未知表" not in put_exc.value.details["reason"]


def test_empty_candidate_list_is_ok(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["other.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    assert list_domain_tables(SETTINGS, SOURCE_ID, JOB_ID) == {"tables": []}


def test_step_missing_or_not_success_is_404(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, FakeSession(None, _source_row()), tmp_path)
    with pytest.raises(DomainConfigOutputMissingError):
        read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    _install(monkeypatch, FakeSession(_step_row("failed"), _source_row()), tmp_path)
    with pytest.raises(DomainConfigOutputMissingError):
        list_domain_tables(SETTINGS, SOURCE_ID, JOB_ID)
    with pytest.raises(DomainConfigOutputMissingError):
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, {"fingerprint": "x"})


def test_put_roundtrip_preserves_description_and_block_style(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(
        root,
        """database:
  name: orders
  description: 库描述原文
llm_inference:
  max_domains_per_table: 3
domains:
- name: 用户管理
  description: |
    用户
    第二行
  tables:
  - orders.public.users
""",
    )
    _write_manifest(root, ["orders.public.users.md", "orders.public.orders.md"])
    source = _source_row()
    _install(monkeypatch, FakeSession(_step_row(), source), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    domains = [
        {
            "name": "订单",
            "description": "订单\n明细",
            "tables": ["orders.public.orders"],
        },
        {
            "name": "用户管理",
            "description": "用户\n第二行\n",
            "tables": ["orders.public.users"],
        },
    ]
    saved = write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, _payload(view, domains))
    assert [item["name"] for item in saved["domains"]] == ["订单", "用户管理"]
    assert saved["database"]["description"] == "库描述原文"
    assert source.description == "数据源描述"
    text = (root / "domain" / "db_domains.yaml").read_text(encoding="utf-8")
    assert "description: |" in text
    assert "db_desc" not in text
    again = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    assert again["domains"] == saved["domains"]
    assert again["fingerprint"] == saved["fingerprint"]


@pytest.mark.parametrize(
    "mutate,reason_part",
    [
        (lambda domains: domains + [domains[0]], "重复"),
        (lambda domains: [{**domains[0], "tables": ["orders.public.missing"]}], "未知表"),
        (lambda domains: [{**domains[0], "name": "  "}], "名称"),
        (lambda domains: [{**domains[0], "description": ""}], "描述"),
    ],
)
def test_put_rejects_without_writing(tmp_path, monkeypatch, mutate, reason_part):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    payload = _payload(view, mutate(view["domains"]))
    with pytest.raises(DomainConfigInvalidError) as exc:
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, payload)
    assert reason_part in exc.value.details["reason"]
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before


def test_put_rejects_renamed_database_fields(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    renamed = _payload(view, view["domains"])
    renamed["database"] = {"name": "other", "description": view["database"]["description"]}
    with pytest.raises(DomainConfigInvalidError):
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, renamed)
    changed = _payload(view, view["domains"])
    changed["database"] = {"name": DB_NAME, "description": "改过"}
    with pytest.raises(DomainConfigInvalidError) as exc:
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, changed)
    assert "库描述" in exc.value.details["reason"]
    stale = _payload(view, view["domains"])
    stale["fingerprint"] = "0" * 64
    with pytest.raises(DomainConfigRegeneratedError):
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, stale)
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before


def test_put_unchanged_does_not_rewrite(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    saved = write_domain_config(
        SETTINGS, SOURCE_ID, JOB_ID, _payload(view, view["domains"])
    )
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before
    assert saved["fingerprint"] == view["fingerprint"]


@pytest.mark.parametrize("matching_fingerprint", [False, True])
def test_put_checks_fingerprint_before_parsing_yaml(
    tmp_path, monkeypatch, matching_fingerprint
):
    import hashlib

    root = _root(tmp_path)
    _install(monkeypatch, _session(), tmp_path)
    raw = b": [\n"
    path = root / "domain" / "db_domains.yaml"
    path.write_bytes(raw)
    fingerprint = hashlib.sha256(raw).hexdigest() if matching_fingerprint else "stale"
    error = (
        DomainConfigInvalidError if matching_fingerprint else DomainConfigRegeneratedError
    )

    with pytest.raises(error):
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, {"fingerprint": fingerprint})

    assert path.read_bytes() == raw


def test_put_strips_domain_whitespace(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    saved = write_domain_config(
        SETTINGS,
        SOURCE_ID,
        JOB_ID,
        _payload(
            view,
            [{"name": "  用户管理  ", "description": "  用户  ", "tables": ["orders.public.users"]}],
        ),
    )
    assert saved["domains"] == [
        {"name": "用户管理", "description": "用户", "tables": ["orders.public.users"]}
    ]


def test_same_table_in_two_domains_and_empty_tables(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    saved = write_domain_config(
        SETTINGS,
        SOURCE_ID,
        JOB_ID,
        _payload(
            view,
            [
                {"name": "甲", "description": "一", "tables": ["orders.public.users"]},
                {"name": "乙", "description": "二", "tables": ["orders.public.users"]},
                {"name": "丙", "description": "三", "tables": []},
            ],
        ),
    )
    assert saved["domains"][0]["tables"] == saved["domains"][1]["tables"]
    assert saved["domains"][2]["tables"] == []
    registration = RegistrationSnapshot(STEP_AT, DB_NAME, ("public",))
    import backend.profile_loading.domain_loader as loader

    monkeypatch.setattr(
        loader, "resolve_domain_output_dir", lambda s, j: root / "domain"
    )
    monkeypatch.setattr(loader, "resolve_md_output_dir", lambda s, j: root / "md")
    rows = read_domain_rows(SOURCE_ID, JOB_ID, registration)
    assert len(rows) == 2
    assert {row["domain_name"] for row in rows} == {"甲", "乙"}


def test_replace_failure_keeps_original(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    import backend.profile_loading.domain_config_api as api

    def _boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(api, "replace_domain_file", _boom)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    before = (root / "domain" / "db_domains.yaml").read_bytes()
    changed = [{**view["domains"][0], "description": "已修改描述"}]
    with pytest.raises(DomainConfigWriteFailedError):
        write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, _payload(view, changed))
    assert (root / "domain" / "db_domains.yaml").read_bytes() == before
    assert list((root / "domain").glob(".tmp-domain-*")) == []


def test_put_locks_source_before_fingerprint_read_and_replace(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    session = _session()
    _install(monkeypatch, session, tmp_path)
    import backend.profile_loading.domain_config_api as api

    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    events = []
    original_read = api._read_regular_bytes
    original_replace = api.replace_domain_file

    def lock(_session, _source):
        events.append("lock")
        return session.source_row

    def read(path):
        events.append("read")
        return original_read(path)

    def replace(src, dst):
        events.append("replace")
        original_replace(src, dst)

    monkeypatch.setattr(api, "lock_db_source_row", lock)
    monkeypatch.setattr(api, "_read_regular_bytes", read)
    monkeypatch.setattr(api, "replace_domain_file", replace)
    changed = [{**view["domains"][0], "description": "新描述"}]
    write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, _payload(view, changed))
    # 替换前只读一次，用于核对指纹并解析；替换后保留一次核验读取。
    assert events == ["lock", "read", "replace", "read"]


def test_empty_domains_fail_verify_artifact(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _write_yaml(root, SAMPLE)
    _write_manifest(root, ["orders.public.users.md"])
    _install(monkeypatch, _session(), tmp_path)
    view = read_domain_config(SETTINGS, SOURCE_ID, JOB_ID)
    write_domain_config(SETTINGS, SOURCE_ID, JOB_ID, _payload(view, []))
    ok, reason = verify_domain_artifact(root / "domain" / "db_domains.yaml")
    assert ok is False
    assert "domains 为空" in reason


def _payload(view: dict, domains: list) -> dict:
    return {
        "fingerprint": view["fingerprint"],
        "database": view["database"],
        "max_domains_per_table": view["max_domains_per_table"],
        "domains": domains,
    }
